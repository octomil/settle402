"""API key authentication and rate limiting for settle402."""

from __future__ import annotations

import hashlib
import hmac
import threading
import time

from fastapi import HTTPException, Request

# Per-key rate limiter: max N batches per minute
_DEFAULT_RATE_LIMIT = 10  # batches per minute
_rate_buckets: dict[str, list[float]] = {}
_rate_lock = threading.Lock()
_SWEEP_THRESHOLD = 1024


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _matches_any(token: str, stored: list[str]) -> bool:
    # Compare against every entry so timing doesn't reveal which one matched
    digest = hash_key(token)
    ok = False
    for entry in stored:
        ok |= hmac.compare_digest(digest, entry)
    return ok


def verify_api_key(request: Request) -> None:
    """Verify X-Settler-Token header and enforce rate limits.

    request.app.state.api_keys holds sha256 digests. Fails closed when empty.
    """
    api_keys: list[str] = request.app.state.api_keys
    if not api_keys:
        raise HTTPException(status_code=503, detail="No API keys configured")

    token = request.headers.get("x-settler-token", "")
    if not token or not _matches_any(token, api_keys):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Settler-Token")

    # Rate limit per key
    rate_limit = getattr(request.app.state, "rate_limit", _DEFAULT_RATE_LIMIT)
    if not _check_rate_limit(hash_key(token), rate_limit):
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: max {rate_limit} batches/min",
        )


def verify_admin(request: Request) -> None:
    """Gate operator endpoints behind ADMIN_TOKEN; disabled when it is unset."""
    admin_token: str = getattr(request.app.state, "admin_token", "")
    if not admin_token:
        raise HTTPException(status_code=503, detail="Admin endpoints disabled")
    token = request.headers.get("x-admin-token", "")
    if not hmac.compare_digest(hash_key(token), hash_key(admin_token)):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Admin-Token")


def _check_rate_limit(key: str, limit: int, window: float = 60.0) -> bool:
    """Sliding window rate limiter. Returns True if request is allowed."""
    now = time.monotonic()

    with _rate_lock:
        if len(_rate_buckets) > _SWEEP_THRESHOLD:
            for k in [k for k, v in _rate_buckets.items() if not v or now - v[-1] >= window]:
                del _rate_buckets[k]

        bucket = [t for t in _rate_buckets.get(key, []) if now - t < window]
        if len(bucket) >= limit:
            _rate_buckets[key] = bucket
            return False

        bucket.append(now)
        _rate_buckets[key] = bucket
        return True
