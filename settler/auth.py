"""API key authentication for settle402."""

from __future__ import annotations

from fastapi import HTTPException, Request


def verify_api_key(request: Request) -> None:
    """Verify X-Settler-Token header against configured API keys.

    Skips validation when no API keys are configured (dev mode).
    """
    api_keys: list[str] = request.app.state.api_keys
    if not api_keys:
        return  # no keys configured — open access (dev/testing)

    token = request.headers.get("x-settler-token", "")
    if not token or token not in api_keys:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Settler-Token")
