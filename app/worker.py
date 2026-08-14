from __future__ import annotations

import asyncio
import logging

from telegram import Bot
from telegram.error import TelegramError

from .config import Settings
from .instagram import InstagramAuthenticationError, InstagramClient, InstagramError
from .redis_store import Job, QueueStore

logger = logging.getLogger(__name__)


async def send_file(bot: Bot, chat_id: int, path, media_type: str) -> None:
    with path.open("rb") as file_handle:
        if media_type == "photo":
            try:
                await bot.send_photo(chat_id=chat_id, photo=file_handle)
            except TelegramError:
                file_handle.seek(0)
                await bot.send_document(chat_id=chat_id, document=file_handle)
        else:
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
    def __init__(self, bot: Bot, settings: Settings, queue: QueueStore, instagram: InstagramClient) -> None:
        self.bot = bot
        self.settings = settings
        self.queue = queue
        self.instagram = instagram
        self._stop = asyncio.Event()

    async def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        await self.queue.recover_processing()
        logger.info("Download worker started")

        while not self._stop.is_set():
            try:
                job = await self.queue.claim()
                if job is None:
                    continue
                await self.process(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Worker loop error")
                await asyncio.sleep(2)

        logger.info("Download worker stopped")

    async def process(self, job: Job) -> None:
        logger.info("Processing job %s for chat=%s url=%s attempt=%s", job.job_id, job.chat_id, job.url, job.attempts + 1)
        files = []

        try:
            await self.bot.send_message(job.chat_id, "⬇️ دانلود شروع شد")
            files = await asyncio.to_thread(self.instagram.download, job.url)

            for index, media in enumerate(files, start=1):
                await send_file(self.bot, job.chat_id, media.path, media.media_type)
                logger.info("Uploaded %s for job %s (%s/%s)", media.path.name, job.job_id, index, len(files))

            await self.bot.send_message(job.chat_id, f"✅ انجام شد. {len(files)} فایل ارسال شد.")
            await self.queue.acknowledge(job, success=True)

        except InstagramAuthenticationError as exc:
            logger.error("Instagram auth error: %s", exc)
            await self.bot.send_message(job.chat_id, "❌ Session اینستاگرام منقضی شده است. ادمین باید Session را به‌روزرسانی کند.")
            await self.queue.acknowledge(job, success=False)

        except InstagramError as exc:
            logger.warning("Instagram download error for %s: %s", job.job_id, exc)
            await self.bot.send_message(job.chat_id, f"❌ {exc}")
            await self.queue.acknowledge(job, success=False)

        except TelegramError as exc:
            logger.exception("Telegram error for job %s", job.job_id)
            if job.attempts < self.settings.max_retries:
                await asyncio.sleep(self.settings.retry_backoff_seconds * (job.attempts + 1))
                await self.queue.requeue(job)
                await self.bot.send_message(job.chat_id, "⚠️ ارسال ناموفق بود؛ درخواست دوباره در صف قرار گرفت.")
            else:
                await self.queue.acknowledge(job, success=False)
                await self.bot.send_message(job.chat_id, "❌ ارسال فایل به تلگرام چند بار ناموفق بود.")

        except Exception as exc:
            logger.exception("Unexpected error for job %s", job.job_id)
            if job.attempts < self.settings.max_retries:
                await asyncio.sleep(self.settings.retry_backoff_seconds * (job.attempts + 1))
                await self.queue.requeue(job)
                await self.bot.send_message(job.chat_id, "⚠️ پردازش ناموفق بود؛ درخواست دوباره در صف قرار گرفت.")
            else:
                await self.queue.acknowledge(job, success=False)
                await self.bot.send_message(job.chat_id, "❌ پردازش درخواست ناموفق بود. دوباره امتحان کنید.")

        finally:
            if files:
                await asyncio.to_thread(self.instagram.cleanup_files, files)
