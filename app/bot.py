from __future__ import annotations

import asyncio
import logging

from aiohttp import web
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .config import Settings
from .instagram import InstagramClient, InstagramError
from .redis_store import QueueStore
from .worker import DownloadWorker

logger = logging.getLogger(__name__)


def build_application(settings: Settings) -> tuple[Application, QueueStore, DownloadWorker]:
    redis_client = __import__("redis.asyncio", fromlist=["Redis"]).from_url(
        settings.redis_url,
        decode_responses=True,
        health_check_interval=30,
        socket_connect_timeout=10,
        socket_timeout=10,
    )
    queue = QueueStore(redis_client)
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

    worker = DownloadWorker(application.bot, settings, queue, instagram)

    async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_message:
            await update.effective_message.reply_text(
                "سلام.\nلینک Instagram را بفرستید.\n\n"
                "حداکثر ۵ دانلود در هر ساعت و حداقل ۱۰ ثانیه فاصله بین درخواست‌ها."
            )

    async def receive_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message or not update.effective_chat:
            return

        url = (update.effective_message.text or "").strip()
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
            await update.effective_message.reply_text("⏳ درخواست ثبت شد و وارد صف دانلود شد.")
        elif status == "COOLDOWN":
            await update.effective_message.reply_text(
                f"⏱️ لطفاً {settings.cooldown_seconds} ثانیه بین درخواست‌ها فاصله بگذارید."
            )
        elif status == "RATE_LIMIT":
            seconds = max(1, (value - __import__("time").time() * 1000) / 1000)
            minutes = int(seconds // 60) + (1 if seconds % 60 else 0)
            await update.effective_message.reply_text(
                f"🚫 سقف {settings.rate_limit_count} دانلود در یک ساعت پر شده است.\n"
                f"حدود {minutes} دقیقه بعد دوباره امتحان کنید."
            )
        elif status == "DUPLICATE":
            await update.effective_message.reply_text("🔁 این لینک همین حالا در صف است یا اخیراً پردازش شده است.")
        elif status == "QUEUE_FULL":
            await update.effective_message.reply_text("📥 صف موقتاً پر است. چند دقیقه بعد دوباره امتحان کنید.")
        else:
            await update.effective_message.reply_text("❌ ثبت درخواست ناموفق بود.")

    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_url))
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
    site = web.TCPSite(runner, host="0.0.0.0", port=settings.port)
    await site.start()

    worker_task = asyncio.create_task(worker.run(), name="download-worker")
    logger.info("Bot server listening on port %s", settings.port)
    logger.info("Telegram webhook: %s", webhook_url)

    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        raise
    finally:
        logger.info("Shutting down...")
        await worker.stop()
        worker_task.cancel()
        await asyncio.gather(worker_task, return_exceptions=True)
        await application.bot.delete_webhook(drop_pending_updates=False)
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
