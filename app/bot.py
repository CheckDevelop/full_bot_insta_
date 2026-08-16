from __future__ import annotations

import asyncio
import logging
import re
import time

from aiohttp import web
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import Settings
from .instagram import InstagramClient, InstagramError
from .redis_store import QueueStore
from .v2ray_manager import V2RayError, V2RayManager
from .worker import DownloadWorker

logger = logging.getLogger(__name__)


v2ray_manager = V2RayManager("v2ray_data")
waiting_v2ray_username: set[int] = set()
waiting_v2ray_vless: set[int] = set()
v2ray_test_username: dict[int, str] = {}


def build_application(
    settings: Settings,
) -> tuple[Application, QueueStore, DownloadWorker]:

    queue = QueueStore(
        settings.upstash_redis_rest_url,
        settings.upstash_redis_rest_token,
    )

    instagram = InstagramClient(
        settings.instagram_username,
        settings.instagram_session_b64,
        settings.temp_dir,
        settings.max_media_bytes,
        settings.request_timeout_seconds,
    )

    application = (
        Application.builder()
        .token(settings.bot_token)
        .concurrent_updates(True)
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(60)
        .pool_timeout(60)
        .build()
    )

    application.bot_data["instagram"] = instagram
    application.bot_data["v2ray_manager"] = v2ray_manager

    worker = DownloadWorker(
        application.bot,
        settings,
        queue,
        instagram,
    )

    async def start_cmd(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if update.effective_message:
            await update.effective_message.reply_text(
                "سلام.\n"
                "لینک Instagram را بفرستید.\n\n"
                "برای تنظیم V2Ray جهت گرفتن Owner ID استوری: /v2ray\n\n"
                "حداکثر ۵ دانلود در هر ساعت و حداقل ۱۰ ثانیه فاصله بین درخواست‌ها."
            )

    async def v2ray_cmd(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not update.effective_user or not update.effective_message:
            return

        user_id = update.effective_user.id
        waiting_v2ray_username.add(user_id)

        await update.effective_message.reply_text(
            "1️⃣ اول Username اینستاگرامی را که می‌خواهی V2Ray روی آن تست شود بفرست.\n\n"
            "مثلاً: jesspopko.tattoo\n\n"
            "بعد از دریافت Username، ربات VLESS را ازت می‌گیرد و فقط تست User ID را انجام می‌دهد.\n"
            "دانلودها از V2Ray عبور نمی‌کنند."
        )
    async def receive_test_username(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not update.effective_user or not update.effective_message:
            return

        telegram_user_id = update.effective_user.id
        if telegram_user_id not in waiting_v2ray_username:
            return

        username = (update.effective_message.text or "").strip()
        username = username.lstrip("@").strip()

        if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", username):
            await update.effective_message.reply_text(
                "❌ Username اینستاگرام معتبر نیست.\n"
                "مثلاً: jesspopko.tattoo"
            )
            return

        waiting_v2ray_username.discard(telegram_user_id)
        v2ray_test_username[telegram_user_id] = username
        waiting_v2ray_vless.add(telegram_user_id)

        await update.effective_message.reply_text(
            f"✅ Username ثبت شد: @{username}\n\n"
            "2️⃣ حالا لینک VLESS را ارسال کن.\n\n"
            "ربات VLESS را اجرا می‌کند و با آن فقط User ID همین Username را تست می‌کند."
        )

    async def receive_vless(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not update.effective_user or not update.effective_message:
            return

        telegram_user_id = update.effective_user.id

        if telegram_user_id not in waiting_v2ray_vless:
            return

        vless = (update.effective_message.text or "").strip()
        waiting_v2ray_vless.discard(telegram_user_id)

        test_username = v2ray_test_username.get(telegram_user_id)
        if not test_username:
            await update.effective_message.reply_text(
                "❌ Username تست پیدا نشد. دوباره /v2ray را بزن."
            )
            return

        status_message = await update.effective_message.reply_text(
            "⏳ در حال تست VLESS روی Instagram..."
        )

        try:
            instagram: InstagramClient = context.application.bot_data["instagram"]
            manager: V2RayManager = context.application.bot_data["v2ray_manager"]

            # Remove current owner-ID proxy before testing a replacement.
            instagram.clear_story_owner_proxy_session()

            # The Instagram session is shared by the whole bot, so there is
            # one active owner-ID proxy at a time. Stop any previous Xray.
            for active_user_id in list(manager.processes.keys()):
                manager.stop(active_user_id)

            # Parse and start Xray, but do not save anything yet.
            parsed = await asyncio.to_thread(
                manager._parse_vless,
                vless,
            )

            port = await asyncio.to_thread(
                manager._find_free_port,
            )

            config_path = await asyncio.to_thread(
                manager._create_config,
                parsed,
                port,
            )

            await asyncio.to_thread(
                manager._start,
                telegram_user_id,
                config_path,
                port,
            )

            proxy_session = manager.get_requests_session(
                telegram_user_id
            )

            # Test ONLY the requested Instagram username.
            owner_id = await asyncio.to_thread(
                instagram.get_user_id_from_html_with_proxy,
                proxy_session,
                test_username,
            )

            if owner_id is None:
                raise InstagramError(
                    f"User ID برای @{test_username} پیدا نشد."
                )

            ip_response = await asyncio.to_thread(
                proxy_session.get,
                "https://api.ipify.org?format=text",
                15,
            )
            public_ip = ip_response.text.strip() if ip_response.ok else None

            # IMPORTANT: save only after the requested User ID test succeeds.
            manager.save(
                telegram_user_id,
                vless,
                public_ip,
            )

            instagram.set_story_owner_proxy_session(
                proxy_session
            )

            await status_message.edit_text(
                "✅ VLESS تأیید شد و ذخیره شد.\n\n"
                f"👤 Username تست: @{test_username}\n"
                f"🆔 User ID: {owner_id}\n"
                f"🌐 IP خروجی: {public_ip or 'نامشخص'}\n\n"
                "🎯 V2Ray فقط برای گرفتن Owner/User ID استوری استفاده می‌شود.\n"
                "⬇️ دانلودها از session عادی Instagram انجام می‌شوند."
            )

            v2ray_test_username.pop(telegram_user_id, None)

        except (V2RayError, InstagramError) as exc:
            logger.warning(
                "V2Ray test failed for Telegram user %s: %s",
                telegram_user_id,
                exc,
            )

            try:
                context.application.bot_data["instagram"].clear_story_owner_proxy_session()
                context.application.bot_data["v2ray_manager"].stop(telegram_user_id)
            except Exception:
                logger.exception("Failed to clean failed V2Ray session")

            v2ray_test_username.pop(telegram_user_id, None)
            waiting_v2ray_vless.discard(telegram_user_id)
            await status_message.edit_text(
                "❌ VLESS تأیید نشد.\n\n"
                f"{exc}\n\n"
                "این کانفیگ ذخیره نشد."
            )

        except Exception as exc:
            logger.exception(
                "Unexpected V2Ray setup error for Telegram user %s",
                telegram_user_id,
            )

            try:
                context.application.bot_data["instagram"].clear_story_owner_proxy_session()
                context.application.bot_data["v2ray_manager"].stop(telegram_user_id)
            except Exception:
                logger.exception("Failed to clean V2Ray session")

            await status_message.edit_text(
                "❌ خطای غیرمنتظره هنگام راه‌اندازی V2Ray:\n"
                f"{exc}"
            )

    async def v2ray_off_cmd(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not update.effective_message:
            return

        instagram: InstagramClient = context.application.bot_data["instagram"]
        instagram.clear_story_owner_proxy_session()

        # Stop all active Xray instances because the Instagram session is shared.
        manager: V2RayManager = context.application.bot_data["v2ray_manager"]
        for telegram_user_id in list(manager.processes.keys()):
            manager.stop(telegram_user_id)

        await update.effective_message.reply_text(
            "✅ V2Ray مخصوص Owner ID استوری غیرفعال شد.\n"
            "دانلودها مثل قبل با session عادی انجام می‌شوند."
        )

    async def receive_url(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not update.effective_message or not update.effective_chat:
            return

        url = (update.effective_message.text or "").strip()

        # A VLESS link sent while /v2ray is waiting is handled here as a fallback.
        if url.lower().startswith("vless://"):
            await receive_vless(update, context)
            return

        try:
            normalized = instagram.validate_url(url)
        except InstagramError as exc:
            await update.effective_message.reply_text(f"❌ {exc}")
            return

        status, value, _job_id = await queue.enqueue(
            update.effective_chat.id,
            normalized,
            rate_limit_count=settings.rate_limit_count,
            rate_limit_window_seconds=settings.rate_limit_window_seconds,
            cooldown_seconds=settings.cooldown_seconds,
            duplicate_ttl_seconds=settings.duplicate_ttl_seconds,
            max_queue_size=settings.max_queue_size,
        )

        if status == "OK":
            await update.effective_message.reply_text(
                "⏳ درخواست ثبت شد و وارد صف دانلود شد."
            )
        elif status == "COOLDOWN":
            await update.effective_message.reply_text(
                f"⏱️ لطفاً {settings.cooldown_seconds} ثانیه بین درخواست‌ها فاصله بگذارید."
            )
        elif status == "RATE_LIMIT":
            seconds = max(1, (value - time.time() * 1000) / 1000)
            minutes = int(seconds // 60) + (1 if seconds % 60 else 0)
            await update.effective_message.reply_text(
                f"🚫 سقف {settings.rate_limit_count} دانلود در یک ساعت پر شده است.\n"
                f"حدود {minutes} دقیقه بعد دوباره امتحان کنید."
            )
        elif status == "DUPLICATE":
            await update.effective_message.reply_text(
                "🔁 این لینک همین حالا در صف است یا اخیراً پردازش شده است."
            )
        elif status == "QUEUE_FULL":
            await update.effective_message.reply_text(
                "📥 صف موقتاً پر است. چند دقیقه بعد دوباره امتحان کنید."
            )
        else:
            await update.effective_message.reply_text(
                "❌ ثبت درخواست ناموفق بود."
            )

    application.add_handler(
        CommandHandler("start", start_cmd)
    )
    application.add_handler(
        CommandHandler("v2ray", v2ray_cmd)
    )
    application.add_handler(
        CommandHandler("v2ray_off", v2ray_off_cmd)
    )
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            receive_test_username,
        ),
        group=-1,
    )
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.Regex(r"^vless://"),
            receive_vless,
        ),
        group=0,
    )
    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND
            & ~filters.Regex(r"^vless://"),
            receive_url,
        ),
        group=1,
    )

    return application, queue, worker


async def health(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


def make_web_app(application: Application, settings: Settings) -> web.Application:
    app = web.Application(client_max_size=10 * 1024 * 1024)

    async def webhook(request: web.Request) -> web.Response:
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if secret != settings.webhook_secret:
            return web.Response(status=403, text="forbidden")
        try:
            data = await request.json()
            update = Update.de_json(data, application.bot)
            if update is None:
                return web.Response(status=400, text="invalid update")
            await application.process_update(update)
            return web.Response(text="ok")
        except Exception:
            logger.exception("Webhook processing failed")
            return web.Response(status=500, text="error")

    app.router.add_get("/health", health)
    app.router.add_post("/telegram", webhook)
    return app


async def main_async() -> None:
    settings = Settings.from_env()
    application, queue, worker = build_application(settings)

    await application.initialize()
    await application.start()

    webhook_url = f"{settings.webhook_url}/telegram"
    await application.bot.set_webhook(
        url=webhook_url,
        secret_token=settings.webhook_secret,
        drop_pending_updates=False,
        max_connections=20,
    )

    web_app = make_web_app(application, settings)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(
        runner,
        host="0.0.0.0",
        port=settings.port,
    )
    await site.start()

    worker_task = asyncio.create_task(
        worker.run(),
        name="download-worker",
    )

    logger.info(
        "Bot server listening on port %s",
        settings.port,
    )
    logger.info(
        "Telegram webhook: %s",
        webhook_url,
    )

    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        raise
    finally:
        logger.info("Shutting down...")
        await worker.stop()
        worker_task.cancel()
        await asyncio.gather(
            worker_task,
            return_exceptions=True,
        )

        try:
            application.bot_data["instagram"].clear_story_owner_proxy_session()
            manager = application.bot_data["v2ray_manager"]
            for telegram_user_id in list(manager.processes.keys()):
                manager.stop(telegram_user_id)
        except Exception:
            logger.exception("Failed to shut down V2Ray owner-ID proxy")

        await application.bot.delete_webhook(
            drop_pending_updates=False
        )
        await queue.close()
        await application.stop()
        await application.shutdown()
        await runner.cleanup()


def main() -> None:
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
