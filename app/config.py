from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc


@dataclass(frozen=True)
class Settings:
    bot_token: str
    redis_url: str
    webhook_url: str
    webhook_secret: str
    instagram_username: str
    instagram_session_b64: str

    port: int = 10000
    rate_limit_count: int = 5
    rate_limit_window_seconds: int = 3600
    cooldown_seconds: int = 10
    duplicate_ttl_seconds: int = 3600
    max_queue_size: int = 20
    max_retries: int = 2
    retry_backoff_seconds: float = 5.0
    request_timeout_seconds: int = 60
    max_media_bytes: int = 500 * 1024 * 1024
    temp_dir: Path = Path("/tmp/instagram-bot")

    @classmethod
    def from_env(cls) -> "Settings":
        required = {
            "BOT_TOKEN": os.getenv("BOT_TOKEN"),
            "REDIS_URL": os.getenv("REDIS_URL"),
            "WEBHOOK_URL": os.getenv("WEBHOOK_URL"),
            "WEBHOOK_SECRET": os.getenv("WEBHOOK_SECRET"),
            "INSTAGRAM_USERNAME": os.getenv("INSTAGRAM_USERNAME"),
            "INSTAGRAM_SESSION_B64": os.getenv("INSTAGRAM_SESSION_B64"),
        }
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise RuntimeError("Missing environment variables: " + ", ".join(missing))

        temp_dir = Path(os.getenv("TEMP_DIR", "/tmp/instagram-bot"))
        temp_dir.mkdir(parents=True, exist_ok=True)

        return cls(
            bot_token=required["BOT_TOKEN"],
            redis_url=required["REDIS_URL"],
            webhook_url=required["WEBHOOK_URL"].rstrip("/"),
            webhook_secret=required["WEBHOOK_SECRET"],
            instagram_username=required["INSTAGRAM_USERNAME"],
            instagram_session_b64=required["INSTAGRAM_SESSION_B64"],
            port=env_int("PORT", 10000),
            rate_limit_count=env_int("RATE_LIMIT_COUNT", 5),
            rate_limit_window_seconds=env_int("RATE_LIMIT_WINDOW_SECONDS", 3600),
            cooldown_seconds=env_int("COOLDOWN_SECONDS", 10),
            duplicate_ttl_seconds=env_int("DUPLICATE_TTL_SECONDS", 3600),
            max_queue_size=env_int("MAX_QUEUE_SIZE", 20),
            max_retries=env_int("MAX_RETRIES", 2),
            retry_backoff_seconds=env_float("RETRY_BACKOFF_SECONDS", 5.0),
            request_timeout_seconds=env_int("REQUEST_TIMEOUT_SECONDS", 60),
            max_media_bytes=env_int("MAX_MEDIA_BYTES", 500 * 1024 * 1024),
            temp_dir=temp_dir,
        )
