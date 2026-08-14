from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, asdict
from typing import Any

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

QUEUE_KEY = "igbot:queue"
PROCESSING_KEY = "igbot:processing"
ACTIVE_KEY = "igbot:active"
RATE_KEY_PREFIX = "igbot:rate:"
COOLDOWN_KEY_PREFIX = "igbot:cooldown:"
DUPLICATE_KEY_PREFIX = "igbot:dup:"

ENQUEUE_LUA = r"""
local rate_key = KEYS[1]
local cooldown_key = KEYS[2]
local duplicate_key = KEYS[3]
local active_key = KEYS[4]
local queue_key = KEYS[5]

local now_ms = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local rate_limit = tonumber(ARGV[3])
local cooldown_seconds = tonumber(ARGV[4])
local duplicate_ttl = tonumber(ARGV[5])
local max_queue = tonumber(ARGV[6])
local job_json = ARGV[7]
local job_id = ARGV[8]

redis.call('ZREMRANGEBYSCORE', rate_key, 0, now_ms - window_ms)

if redis.call('EXISTS', cooldown_key) == 1 then
    return {'COOLDOWN', 0}
end

local used = redis.call('ZCARD', rate_key)
if used >= rate_limit then
    local oldest = redis.call('ZRANGE', rate_key, 0, 0, 'WITHSCORES')
    local retry_at = 0
    if oldest[2] then
        retry_at = tonumber(oldest[2]) + window_ms
    end
    return {'RATE_LIMIT', retry_at}
end

if redis.call('EXISTS', duplicate_key) == 1 then
    return {'DUPLICATE', 0}
end

if redis.call('SCARD', active_key) >= max_queue then
    return {'QUEUE_FULL', 0}
end

redis.call('SETEX', cooldown_key, cooldown_seconds, '1')
redis.call('ZADD', rate_key, now_ms, tostring(now_ms) .. ':' .. job_id)
redis.call('EXPIRE', rate_key, math.ceil(window_ms / 1000))
redis.call('SETEX', duplicate_key, duplicate_ttl, job_id)
redis.call('SADD', active_key, job_id)
redis.call('RPUSH', queue_key, job_json)

return {'OK', used + 1}
"""


@dataclass(frozen=True)
class Job:
    job_id: str
    chat_id: int
    url: str
    attempts: int = 0

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "Job":
        data: dict[str, Any] = json.loads(raw)
        return cls(
            job_id=str(data["job_id"]),
            chat_id=int(data["chat_id"]),
            url=str(data["url"]),
            attempts=int(data.get("attempts", 0)),
        )


class RestRedis:
    """Small async Upstash REST Redis client. Uses HTTPS, not a long-lived TCP socket."""

    def __init__(self, url: str, token: str) -> None:
        import aiohttp
        self.url = url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self):
        import aiohttp
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=20, connect=10)
            self._session = aiohttp.ClientSession(headers=self.headers, timeout=timeout)
        return self._session

    async def execute(self, command: list[Any]) -> Any:
        session = await self._get_session()
        try:
            async with session.post(self.url, json=command) as response:
                payload = await response.json(content_type=None)
                if response.status >= 400:
                    raise RuntimeError(f"Upstash Redis HTTP {response.status}: {payload}")
                if isinstance(payload, dict) and payload.get("error"):
                    raise RuntimeError(f"Upstash Redis error: {payload['error']}")
                return payload.get("result") if isinstance(payload, dict) else payload
        except Exception:
            logger.exception("Upstash REST request failed: %s", command[:1])
            raise

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()


CLAIM_LUA = r"""
local raw = redis.call('RPOP', KEYS[1])
if raw then
    redis.call('LPUSH', KEYS[2], raw)
    return raw
end
return false
"""


class QueueStore:
    def __init__(self, redis_url: str, token: str) -> None:
        self.redis = RestRedis(redis_url, token)

    @staticmethod
    def normalize_url(url: str) -> str:
        return url.strip().rstrip("/")

    @staticmethod
    def url_hash(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    async def ping(self) -> bool:
        result = await self.redis.execute(["PING"])
        return result == "PONG"

    async def enqueue(
        self,
        chat_id: int,
        url: str,
        *,
        rate_limit_count: int,
        rate_limit_window_seconds: int,
        cooldown_seconds: int,
        duplicate_ttl_seconds: int,
        max_queue_size: int,
    ) -> tuple[str, int, str]:
        normalized = self.normalize_url(url)
        job_id = uuid.uuid4().hex
        now_ms = int(time.time() * 1000)
        user_key = str(chat_id)
        duplicate_hash = self.url_hash(normalized)
        job = Job(job_id=job_id, chat_id=chat_id, url=normalized)

        result = await self.redis.execute([
            "EVAL",
            ENQUEUE_LUA,
            "5",
            RATE_KEY_PREFIX + user_key,
            COOLDOWN_KEY_PREFIX + user_key,
            DUPLICATE_KEY_PREFIX + duplicate_hash,
            ACTIVE_KEY,
            QUEUE_KEY,
            str(now_ms),
            str(rate_limit_window_seconds * 1000),
            str(rate_limit_count),
            str(cooldown_seconds),
            str(duplicate_ttl_seconds),
            str(max_queue_size),
            job.to_json(),
            job_id,
        ])

        status = result[0]
        value = int(result[1] or 0)
        return status, value, job_id

    async def claim(self) -> Job | None:
        raw = await self.redis.execute(["EVAL", CLAIM_LUA, "2", QUEUE_KEY, PROCESSING_KEY])
        if not raw:
            return None
        return Job.from_json(raw)

    async def acknowledge(self, job: Job, *, success: bool) -> None:
        await self.redis.execute(["LREM", PROCESSING_KEY, "1", job.to_json()])
        await self.redis.execute(["SREM", ACTIVE_KEY, job.job_id])
        await self.redis.execute(["DEL", DUPLICATE_KEY_PREFIX + self.url_hash(self.normalize_url(job.url))])

    async def requeue(self, job: Job) -> None:
        await self.redis.execute(["LREM", PROCESSING_KEY, "1", job.to_json()])
        retry_job = Job(
            job_id=job.job_id,
            chat_id=job.chat_id,
            url=job.url,
            attempts=job.attempts + 1,
        )
        await self.redis.execute(["RPUSH", QUEUE_KEY, retry_job.to_json()])

    async def recover_processing(self) -> int:
        count = 0
        while True:
            raw = await self.redis.execute(["LPOP", PROCESSING_KEY])
            if raw is None:
                break
            await self.redis.execute(["RPUSH", QUEUE_KEY, raw])
            count += 1
        if count:
            logger.warning("Recovered %d unfinished jobs from processing queue", count)
        return count

    async def active_count(self) -> int:
        return int(await self.redis.execute(["SCARD", ACTIVE_KEY]))

    async def close(self) -> None:
        await self.redis.close()
