from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from telegram import Bot
from telegram.error import TelegramError

from .config import Settings
from .instagram import (
    InstagramAuthenticationError,
    InstagramClient,
    InstagramError,
    InstagramRateLimitError,
)
from .redis_store import Job, QueueStore

logger = logging.getLogger(__name__)


STORY_URL_RE = re.compile(
    r"/stories/(?!highlights/)[^/]+/[0-9]+(?:[/?#]|$)",
    re.I,
)


def _is_story_url(url: str) -> bool:
    return bool(STORY_URL_RE.search(url.strip()))


async def send_file(
    bot: Bot,
    chat_id: int,
    path: Path,
    media_type: str,
) -> None:

    with path.open("rb") as file_handle:

        if media_type == "photo":

            try:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=file_handle,
                )

            except TelegramError:

                file_handle.seek(0)

                await bot.send_document(
                    chat_id=chat_id,
                    document=file_handle,
                )

            return

        try:

            await bot.send_video(
                chat_id=chat_id,
                video=file_handle,
                supports_streaming=True,
                read_timeout=120,
                write_timeout=120,
                connect_timeout=30,
                pool_timeout=60,
            )

        except TelegramError:

            file_handle.seek(0)

            await bot.send_document(
                chat_id=chat_id,
                document=file_handle,
                read_timeout=120,
                write_timeout=120,
                connect_timeout=30,
                pool_timeout=60,
            )


class DownloadWorker:

    def __init__(
        self,
        bot: Bot,
        settings: Settings,
        queue: QueueStore,
        instagram: InstagramClient,
    ) -> None:

        self.bot = bot
        self.settings = settings
        self.queue = queue
        self.instagram = instagram
        self._stop = asyncio.Event()

        # Idle polling starts at 1 second and backs off until 20 sec.
        # When a job arrives, the delay immediately resets to 0.
        self._idle_delay = 1.0

    async def stop(self) -> None:
        self._stop.set()

    async def _wait_idle(self) -> None:
        """
        Adaptive idle polling.

        Old behavior:
            claim -> sleep(1) -> claim -> sleep(1) -> ...

        New behavior:
            1s -> 2s -> 4s -> 8s -> 15s -> 20s

        This reduces empty-queue Upstash commands dramatically.
        """

        delay = self._idle_delay

        try:
            await asyncio.wait_for(
                self._stop.wait(),
                timeout=delay,
            )

        except asyncio.TimeoutError:
            pass

        self._idle_delay = min(
            20.0,
            delay * 2,
        )

    async def run(self) -> None:

        await self.queue.recover_processing()

        logger.info(
            "Download worker started"
        )

        while not self._stop.is_set():

            try:

                job = await self.queue.claim(
                    promote_limit=20,
                )

                if job is None:

                    await self._wait_idle()

                    continue

                # A real job arrived:
                # next claim should happen immediately.
                self._idle_delay = 1.0

                await self.process(job)

            except asyncio.CancelledError:
                raise

            except Exception:

                logger.exception(
                    "Worker loop error"
                )

                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=2,
                    )

                except asyncio.TimeoutError:
                    pass

        logger.info(
            "Download worker stopped"
        )

    def _retry_delay(
        self,
        job: Job,
        *,
        retry_after: int | None = None,
    ) -> int:
    
        if _is_story_url(job.url):
            # Story rate limit:
            # فقط یک retry بعد از 60 ثانیه
            if retry_after is not None:
                return max(60, int(retry_after))
    
            return 60
    
        if retry_after is not None:
            return max(
                5,
                min(
                    int(retry_after),
                    1800,
                ),
            )
    
        base = max(
            1,
            self.settings.retry_backoff_seconds,
        )
    
        delay = base * (
            2 ** max(
                0,
                job.attempts,
            )
        )
    
        return min(
            delay,
            300,
        )

    async def _retry_job(
        self,
        job: Job,
        message: str,
        *,
        delay_seconds: int,
    ) -> None:

        if (
            job.attempts
            >= self.settings.max_retries
        ):

            await self.queue.acknowledge(job)

            await self.bot.send_message(
                job.chat_id,
                "❌ "
                + message
                + "\n"
                "تعداد تلاش‌های مجاز تمام شد.",
            )

            return

        await self.queue.requeue(
            job,
            delay_seconds=delay_seconds,
        )

        await self.bot.send_message(
            job.chat_id,
            "⚠️ "
            + message
            + f"\n"
            f"دوباره بعد از حدود "
            f"{delay_seconds} ثانیه تلاش می‌کنم.",
        )

    async def process(
        self,
        job: Job,
    ) -> None:

        logger.info(
            "Processing job %s "
            "for chat=%s "
            "url=%s "
            "attempt=%s",
            job.job_id,
            job.chat_id,
            job.url,
            job.attempts + 1,
        )

        files = []

        try:

            await self.bot.send_message(
                job.chat_id,
                "⬇️ دانلود شروع شد",
            )

            files = await asyncio.to_thread(
                self.instagram.download,
                job.url,
            )

            for index, media in enumerate(
                files,
                start=1,
            ):

                await send_file(
                    self.bot,
                    job.chat_id,
                    media.path,
                    media.media_type,
                )

                logger.info(
                    "Uploaded %s "
                    "for job %s "
                    "(%s/%s)",
                    media.path.name,
                    job.job_id,
                    index,
                    len(files),
                )

            await self.bot.send_message(
                job.chat_id,
                f"✅ انجام شد. "
                f"{len(files)} فایل ارسال شد.",
            )

            await self.queue.acknowledge(
                job
            )

        except InstagramAuthenticationError as exc:

            logger.error(
                "Instagram auth error "
                "for job %s: %s",
                job.job_id,
                exc,
            )

            await self.queue.acknowledge(
                job
            )

            await self.bot.send_message(
                job.chat_id,
                "❌ Session اینستاگرام "
                "نیاز به بررسی دارد. "
                "ادمین باید Session را بررسی/به‌روزرسانی کند.",
            )

        except InstagramRateLimitError as exc:
        
            # فقط 2 تلاش مجاز:
            # تلاش اول fail شد -> یک retry
            # تلاش دوم fail شد -> stop
        
            if job.attempts >= 1:
                logger.warning(
                    "Instagram rate limit exhausted for job %s",
                    job.job_id,
                )
        
                await self.queue.acknowledge(job)
        
                await self.bot.send_message(
                    job.chat_id,
                    "❌ Instagram بعد از 2 تلاش هنوز محدودیت اعمال کرده. "
                    "دانلود متوقف شد.",
                )
        
                return
        
            delay = self._retry_delay(
                job,
                retry_after=exc.retry_after,
            )
        
            logger.warning(
                "Instagram rate limit "
                "for job %s. retry in %ss",
                job.job_id,
                delay,
            )
        
            await self._retry_job(
                job,
                "Instagram موقتاً درخواست‌ها "
                "را محدود کرده.",
                delay_seconds=delay,
            )

        except InstagramError as exc:

            logger.warning(
                "Instagram error "
                "for job %s: %s",
                job.job_id,
                exc,
            )

            await self.queue.acknowledge(
                job
            )

            await self.bot.send_message(
                job.chat_id,
                f"❌ {exc}",
            )

        except TelegramError as exc:

            logger.warning(
                "Telegram error "
                "for job %s: %s",
                job.job_id,
                exc,
            )

            delay = self._retry_delay(
                job
            )

            await self._retry_job(
                job,
                "ارسال فایل به تلگرام "
                "ناموفق بود.",
                delay_seconds=delay,
            )

        except Exception:

            logger.exception(
                "Unexpected error "
                "for job %s",
                job.job_id,
            )

            delay = self._retry_delay(
                job
            )

            await self._retry_job(
                job,
                "پردازش درخواست "
                "موقتاً ناموفق بود.",
                delay_seconds=delay,
            )

        finally:

            if files:

                await asyncio.to_thread(
                    self.instagram.cleanup_files,
                    files,
                )
