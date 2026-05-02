"""
app/services/cache.py — Redis-backed AI response cache
======================================================
Caches the FULL conversation context → AI reply mapping in Redis. When a
user sends a message that produces an identical request to one we've seen
in the last hour, we return the cached reply instead of calling Ollama.

Why cache?
  Ollama calls are the slowest and most expensive thing this service does.
  Even a 10–30% cache hit rate cuts user-visible latency dramatically and
  reduces token costs proportionally.

Cache key
---------
The key is `chat:cache:<sha256(model + canonical messages JSON)>`.

The hash inputs are:
  - model name (so changing OLLAMA_MODEL invalidates the cache automatically)
  - the FULL conversation context — history + the new user message

Including full history (option A in the design) means a follow-up message
in conversation X never collides with a follow-up in conversation Y, even
if both end with the same words. The downside is lower hit rate, but the
correctness benefit (never serving a stranger's reply context) outweighs it.

JSON serialisation uses sort_keys=True + separators=(",", ":") so the same
logical messages always produce identical bytes — without these flags,
Python's dict ordering or whitespace could change the hash for identical inputs.

TTL
---
1 hour (3600s). Long enough that repeated questions in a session hit cache,
short enough that updated model behaviour or a deploy isn't masked for days.
Bypass for one item: pass `ttl_seconds=0` (Redis doesn't accept that — we
just never call set_cached). Flush everything: see RUNBOOK.md.

Fail-open behaviour
-------------------
If Redis is unreachable, get_cached() and set_cached() log a warning and
return None / silently skip. The chat endpoint still works — it just
doesn't get cache wins. Same philosophy as the rate limiter.
"""

import hashlib
import json
from typing import Any

import redis.asyncio as aioredis
import structlog

logger = structlog.get_logger(__name__)

# Key prefix — namespaces cache keys away from rate limiter / future Redis use.
# Use a colon separator (Redis convention) so RedisInsight groups them visually.
_KEY_PREFIX = "chat:cache:"

# 1 hour. Trade-off explained at the top of the file.
DEFAULT_TTL_SECONDS = 3600


def make_cache_key(model: str, messages: list[dict[str, str]]) -> str:
    """
    Deterministic SHA-256 key for (model, full conversation context).

    Same input → same key, every time, on every replica. Different model or
    any difference in any message → different key.

    We hash the JSON-serialised messages (with sort_keys + compact separators)
    rather than concatenating strings: JSON guarantees an unambiguous,
    canonical encoding, so we never get a hash collision from punctuation
    like "user|hi" colliding with "user", "|hi".
    """
    payload = json.dumps(
        {"model": model, "messages": messages},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{_KEY_PREFIX}{digest}"


async def get_cached(
    redis_client: aioredis.Redis,
    key: str,
) -> dict[str, Any] | None:
    """
    Look up a cached AI response.

    Returns the deserialised dict on hit, or None on miss / Redis error.
    Never raises — the chat path treats `None` as "compute the response".
    """
    try:
        raw = await redis_client.get(key)
    except Exception as exc:
        # Fail-open: log and pretend the cache is empty. The caller will
        # fall through to Ollama and the chat still works.
        await logger.awarning("cache get failed", key=key, error=str(exc))
        return None

    if raw is None:
        return None

    try:
        return json.loads(raw)  # type: ignore[no-any-return]
    except json.JSONDecodeError as exc:
        # Corrupted cache entry — treat as a miss and let it be overwritten.
        await logger.awarning("cache value not JSON", key=key, error=str(exc))
        return None


async def set_cached(
    redis_client: aioredis.Redis,
    key: str,
    value: dict[str, Any],
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> None:
    """
    Store an AI response in Redis with a TTL.

    Fail-open: if Redis is down we log and return. The user already got
    their reply from Ollama — failing to cache it is a missed optimisation,
    not a bug worth raising.
    """
    try:
        # SET key value EX ttl  — atomic write + expiry in one round-trip.
        await redis_client.set(
            key,
            json.dumps(value, ensure_ascii=False),
            ex=ttl_seconds,
        )
    except Exception as exc:
        await logger.awarning("cache set failed", key=key, error=str(exc))
