"""API key authentication and rate limiting for settle402."""

from __future__ import annotations

import threading
import time

from fastapi import HTTPException, Request

# Per-key rate limiter: max N batches per second
_DEFAULT_RATE_LIMIT = 10  # batches per second
_rate_buckets: dict[str, list[float]] = {}
_rate_lock = threading.Lock()


def verify_api_key(request: Request) -> None:
    """Verify X-Settler-Token header and enforce rate limits.

    Skips validation when no API keys are configured (dev mode).
    """
    api_keys: list[str] = request.app.state.api_keys
    if not api_keys:
        return  # no keys configured — open access (dev/testing)

    token = request.headers.get("x-settler-token", "")
    if not token or token not in api_keys:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Settler-Token")

    # Rate limit per key
    rate_limit = getattr(request.app.state, "rate_limit", _DEFAULT_RATE_LIMIT)
    if not _check_rate_limit(token, rate_limit):
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: max {rate_limit} batches/sec",
        )


def _check_rate_limit(key: str, limit: int) -> bool:
    """Sliding window rate limiter. Returns True if request is allowed."""
    now = time.monotonic()
    window = 1.0  # 1 second

    with _rate_lock:
        if key not in _rate_buckets:
            _rate_buckets[key] = []

        # Prune old entries
        _rate_buckets[key] = [t for t in _rate_buckets[key] if now - t < window]

        if len(_rate_buckets[key]) >= limit:
            return False

        _rate_buckets[key].append(now)
        return True
