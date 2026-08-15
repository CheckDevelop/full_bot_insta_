from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger(__name__)

QUEUE_KEY = "igbot:queue"
PROCESSING_KEY = "igbot:processing"
RETRY_KEY = "igbot:retry"
ACTIVE_KEY = "igbot:active"
RATE_KEY_PREFIX = "igbot:rate:"
COOLDOWN_KEY_PREFIX = "igbot:cooldown:"
DUPLICATE_KEY_PREFIX = "igbot:dup:"

# One atomic operation:
# - clean old rate-limit entries
# - enforce cooldown
# - enforce 5/hour style rate limit
# - reject duplicates
# - enforce global active queue limit
# - register job
# - push job to queue
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

# One command:
# - move a bounded number of due retries back to the main queue
# - claim one job
# - move it to processing
CLAIM_LUA = r"""
local queue_key = KEYS[1]
local processing_key = KEYS[2]
local retry_key = KEYS[3]

local now_ms = tonumber(ARGV[1])
local promote_limit = tonumber(ARGV[2])

local due = redis.call(
    'ZRANGEBYSCORE',
    retry_key,
    '-inf',
    now_ms,
    'LIMIT',
    0,
    promote_limit
)

for _, raw in ipairs(due) do
    redis.call('ZREM', retry_key, raw)
    redis.call('RPUSH', queue_key, raw)
end

local raw = redis.call('RPOP', queue_key)

if raw then
    redis.call('LPUSH', processing_key, raw)
    return raw
end

return false
"""

# One command instead of LREM + SREM + DEL.
ACK_LUA = r"""
local processing_key = KEYS[1]
local active_key = KEYS[2]
local duplicate_key = KEYS[3]

local raw_job = ARGV[1]
local job_id = ARGV[2]

local removed = redis.call('LREM', processing_key, 1, raw_job)
redis.call('SREM', active_key, job_id)
redis.call('DEL', duplicate_key)

return removed
"""

# One command instead of LREM + RPUSH.
# Retry is stored in a ZSET so the worker does not sleep/block.
REQUEUE_LUA = r"""
local processing_key = KEYS[1]
local retry_key = KEYS[2]

local raw_job = ARGV[1]
local retry_at_ms = tonumber(ARGV[2])

local removed = redis.call('LREM', processing_key, 1, raw_job)

if removed == 1 then
    redis.call('ZADD', retry_key, retry_at_ms, raw_job)
end

return removed
"""

# One command to recover all jobs that were in processing when the
# container restarted.
RECOVER_LUA = r"""
local processing_key = KEYS[1]
local queue_key = KEYS[2]

local count = 0

while true do
    local raw = redis.call('LPOP', processing_key)

    if not raw then
        break
    end

    redis.call('RPUSH', queue_key, raw)
    count = count + 1
end

return count
"""


@dataclass(frozen=True)
class Job:
    job_id: str
    chat_id: int
    url: str
    attempts: int = 0

    def to_json(self) -> str:
        return json.dumps(
            asdict(self),
            separators=(",", ":"),
            ensure_ascii=False,
        )

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
    """
    Minimal async Upstash Redis REST client.

    Important:
    - No redis-py dependency.
    - No long-lived Redis TCP connection.
    - One HTTP request per logical Redis command.
    """

    def __init__(
        self,
        url: str,
        token: str,
    ) -> None:
        import aiohttp

        self.url = url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self):
        import aiohttp

        if (
            self._session is None
            or self._session.closed
        ):
            timeout = aiohttp.ClientTimeout(
                total=20,
                connect=10,
            )

            self._session = aiohttp.ClientSession(
                headers=self.headers,
                timeout=timeout,
            )

        return self._session

    async def execute(
        self,
        command: list[Any],
    ) -> Any:
        session = await self._get_session()

        try:
            async with session.post(
                self.url,
                json=command,
            ) as response:

                payload = await response.json(
                    content_type=None
                )

                if response.status >= 400:
                    raise RuntimeError(
                        f"Upstash Redis HTTP "
                        f"{response.status}: {payload}"
                    )

                if (
                    isinstance(payload, dict)
                    and payload.get("error")
                ):
                    raise RuntimeError(
                        "Upstash Redis error: "
                        f"{payload['error']}"
                    )

                if isinstance(payload, dict):
                    return payload.get("result")

                return payload

        except Exception:
            logger.exception(
                "Upstash REST request failed: %s",
                command[:1],
            )
            raise

    async def close(self) -> None:
        if (
            self._session is not None
            and not self._session.closed
        ):
            await self._session.close()


class QueueStore:
    def __init__(
        self,
        redis_url: str,
        token: str,
    ) -> None:
        self.redis = RestRedis(
            redis_url,
            token,
        )

    @staticmethod
    def normalize_url(url: str) -> str:
        return url.strip().rstrip("/")

    @staticmethod
    def url_hash(url: str) -> str:
        return hashlib.sha256(
            url.encode("utf-8")
        ).hexdigest()

    async def ping(self) -> bool:
        result = await self.redis.execute(
            ["PING"]
        )
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
        now_ms = int(
            time.time() * 1000
        )

        user_key = str(chat_id)

        duplicate_hash = self.url_hash(
            normalized
        )

        job = Job(
            job_id=job_id,
            chat_id=chat_id,
            url=normalized,
        )

        result = await self.redis.execute(
            [
                "EVAL",
                ENQUEUE_LUA,
                "5",
                RATE_KEY_PREFIX + user_key,
                COOLDOWN_KEY_PREFIX + user_key,
                DUPLICATE_KEY_PREFIX + duplicate_hash,
                ACTIVE_KEY,
                QUEUE_KEY,
                str(now_ms),
                str(
                    rate_limit_window_seconds
                    * 1000
                ),
                str(rate_limit_count),
                str(cooldown_seconds),
                str(duplicate_ttl_seconds),
                str(max_queue_size),
                job.to_json(),
                job_id,
            ]
        )

        if not result:
            raise RuntimeError(
                "Upstash enqueue returned no result."
            )

        status = str(result[0])
        value = int(result[1] or 0)

        return (
            status,
            value,
            job_id,
        )

    async def claim(
        self,
        *,
        promote_limit: int = 20,
    ) -> Job | None:

        now_ms = int(
            time.time() * 1000
        )

        raw = await self.redis.execute(
            [
                "EVAL",
                CLAIM_LUA,
                "3",
                QUEUE_KEY,
                PROCESSING_KEY,
                RETRY_KEY,
                str(now_ms),
                str(promote_limit),
            ]
        )

        if not raw:
            return None

        return Job.from_json(
            str(raw)
        )

    async def acknowledge(
        self,
        job: Job,
    ) -> None:

        duplicate_key = (
            DUPLICATE_KEY_PREFIX
            + self.url_hash(
                self.normalize_url(
                    job.url
                )
            )
        )

        await self.redis.execute(
            [
                "EVAL",
                ACK_LUA,
                "3",
                PROCESSING_KEY,
                ACTIVE_KEY,
                duplicate_key,
                job.to_json(),
                job.job_id,
            ]
        )

    async def requeue(
        self,
        job: Job,
        *,
        delay_seconds: int,
    ) -> None:
    
        retry_job = Job(
            job_id=job.job_id,
            chat_id=job.chat_id,
            url=job.url,
            attempts=job.attempts + 1,
        )
    
        retry_at_ms = int(
            (
                time.time()
                + max(
                    1,
                    int(delay_seconds),
                )
            )
            * 1000
        )
    
        await self.redis.execute(
            [
                "EVAL",
                REQUEUE_LUA,
                "2",
                PROCESSING_KEY,
                RETRY_KEY,
                retry_job.to_json(),   # ← تغییر این خط
                str(retry_at_ms),
            ]
        )
    
        logger.info(
            "Job %s scheduled for retry in %ss (attempt=%s)",
            job.job_id,
            delay_seconds,
            retry_job.attempts,
        )

    async def recover_processing(self) -> int:

        result = await self.redis.execute(
            [
                "EVAL",
                RECOVER_LUA,
                "2",
                PROCESSING_KEY,
                QUEUE_KEY,
            ]
        )

        count = int(result or 0)

        if count:
            logger.warning(
                "Recovered %d unfinished jobs "
                "from processing queue",
                count,
            )

        return count

    async def active_count(self) -> int:
        return int(
            await self.redis.execute(
                ["SCARD", ACTIVE_KEY]
            )
        )

    async def close(self) -> None:
        await self.redis.close()
