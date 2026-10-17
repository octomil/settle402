"""settle402 — Batch settlement for x402 micro-payments via Multicall3.

Accepts batches of signed EIP-3009 transferWithAuthorization payloads and
submits them on-chain in a single transaction, amortizing gas costs across
hundreds of micro-payments.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from pathlib import Path

from eth_account import Account
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from web3 import AsyncHTTPProvider, AsyncWeb3

from .auth import _check_rate_limit, hash_key, verify_admin, verify_api_key
from .config import CHAIN_NAMES, USDC_CONTRACTS, USDC_EIP712_DOMAINS, SettlerConfig
from .schemas import (
    SettleBatchRequest,
    SettleBatchResponse,
    SettlerStats,
    StatusResponse,
)
from .settler import _to_bytes32, settle_batch
from .validation import validate_batch

logger = logging.getLogger("settle402")

app = FastAPI(
    title="settle402",
    description="Batch settlement for x402 micro-payments via Multicall3",
    version="0.1.0",
)

# Server-to-server API: no cross-origin access unless explicitly configured
_cors_origins = [
    o.strip() for o in os.environ.get("SETTLER_CORS_ORIGINS", "").split(",") if o.strip()
]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["content-type", "x-settler-token", "x-admin-token"],
    )

# Populated on startup
_config: SettlerConfig | None = None
_w3: AsyncWeb3 | None = None
_account = None
_stats = SettlerStats()
_start_time = 0.0
_STATUS_TTL = 15.0
_status_cache: tuple[float, int] | None = None  # (monotonic ts, balance wei)
# Fee authorizations already in flight or collected, keyed (from, nonce) -> validBefore
_fee_nonces: dict[tuple[str, bytes], int] = {}


@app.on_event("startup")
async def startup() -> None:
    global _config, _w3, _account, _start_time, _KEYS_FILE

    _config = SettlerConfig.from_env()

    # The nonce lock in settler.py is process-local
    if int(os.environ.get("WEB_CONCURRENCY", "1") or 1) > 1:
        raise RuntimeError("settle402 must run as a single process (WEB_CONCURRENCY > 1)")

    logging.basicConfig(level=getattr(logging, _config.log_level.upper(), logging.INFO))

    if not _config.rpc_url:
        logger.warning("SETTLER_RPC_URL not set — settler will reject /settle requests")
    else:
        _w3 = AsyncWeb3(AsyncHTTPProvider(_config.rpc_url))

    if _config.private_key:
        _account = Account.from_key(_config.private_key)
        logger.info("Settler address: %s", _account.address)
    else:
        logger.warning("SETTLER_PRIVATE_KEY not set — settler will reject /settle requests")

    _KEYS_FILE = Path(_config.keys_file)
    app.state.api_keys = [hash_key(k) for k in _config.api_keys] + _load_keys()
    app.state.rate_limit = _config.rate_limit
    app.state.admin_token = _config.admin_token
    if not _config.admin_token:
        logger.warning("ADMIN_TOKEN not set — /keys and /status are disabled")
    _start_time = time.monotonic()

    logger.info("settle402 ready on chain %d (%s)", _config.chain_id, _config.network_name)


@app.on_event("shutdown")
async def shutdown() -> None:
    pass


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


_KEYS_FILE = Path("/data/keys.json")
_KEY_DIGEST_LEN = 64


def _save_keys() -> None:
    """Persist API key digests atomically; raises on failure."""
    _KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _KEYS_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(app.state.api_keys, f)
    os.replace(tmp, _KEYS_FILE)


def _load_keys() -> list[str]:
    """Load persisted key digests; older plaintext entries are hashed on read."""
    if not _KEYS_FILE.exists():
        return []
    try:
        stored = json.loads(_KEYS_FILE.read_text())
    except Exception:
        logger.exception("Could not read %s — persisted API keys not loaded", _KEYS_FILE)
        return []
    return [k if len(k) == _KEY_DIGEST_LEN else hash_key(k) for k in stored]


def _client_ip_limit(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(f"ip:{ip}", 5):
        raise HTTPException(429, "Too many requests")


@app.post("/keys", dependencies=[Depends(_client_ip_limit), Depends(verify_admin)])
async def create_key() -> dict:
    """Generate a new API key (admin only). The plaintext is returned once."""
    if len(app.state.api_keys) >= _config.max_keys:
        raise HTTPException(429, "API key limit reached")
    key = f"s402_{secrets.token_urlsafe(32)}"
    digest = hash_key(key)
    app.state.api_keys.append(digest)
    try:
        _save_keys()
    except Exception:
        app.state.api_keys.remove(digest)
        logger.exception("Could not persist API keys to %s", _KEYS_FILE)
        raise HTTPException(500, "Could not persist API key") from None
    return {"key": key}


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "chain_id": _config.chain_id if _config else 0,
    }


async def _get_balance() -> int:
    global _status_cache
    now = time.monotonic()
    if _status_cache and now - _status_cache[0] < _STATUS_TTL:
        return _status_cache[1]
    try:
        balance = await _w3.eth.get_balance(_account.address)
    except Exception:
        logger.exception("RPC get_balance failed")
        raise HTTPException(503, "RPC unavailable") from None
    _status_cache = (now, balance)
    return balance


@app.get("/status", dependencies=[Depends(verify_admin)])
async def status() -> StatusResponse:
    if not _config or not _w3 or not _account:
        raise HTTPException(503, "Settler not configured")

    balance = await _get_balance()
    _stats.uptime_seconds = time.monotonic() - _start_time

    return StatusResponse(
        status="ok",
        settler_address=_account.address,
        eth_balance=f"{balance / 1e18:.6f}",
        chain_id=_config.chain_id,
        network=CHAIN_NAMES.get(_config.chain_id, f"chain-{_config.chain_id}"),
        stats=_stats,
    )


@app.post("/settle", dependencies=[Depends(verify_api_key)])
async def settle(request: SettleBatchRequest) -> SettleBatchResponse:
    """Submit a batch of EIP-3009 authorizations for on-chain settlement."""
    if not _config or not _w3 or not _account:
        raise HTTPException(503, "Settler not configured — missing RPC URL or private key")

    chain_id = request.chainId or _config.chain_id
    if chain_id != _config.chain_id:
        raise HTTPException(
            400,
            f"Chain mismatch: settler configured for {_config.chain_id}, got {chain_id}",
        )

    default_token = USDC_CONTRACTS.get(chain_id, "")
    token_contract = request.tokenContract or default_token
    allowed_tokens = {t.lower() for t in [default_token, *_config.token_allowlist] if t}
    if not token_contract or token_contract.lower() not in allowed_tokens:
        raise HTTPException(400, "Token contract not allowed for this chain")

    if not request.authorizations:
        raise HTTPException(400, "No authorizations provided")

    if len(request.authorizations) > 1000:
        raise HTTPException(400, "Maximum 1,000 authorizations per batch")

    # Fee validation (before balance check — no RPC call if fee is invalid)
    fee_auth = None
    if _config.fee_enabled:
        fee_auth = request.feeAuthorization
        if fee_auth is None:
            raise HTTPException(
                status_code=402,
                detail="Fee authorization required. Include a feeAuthorization "
                f"paying >= {_config.fee_amount} base units to {_account.address}",
            )
        fee_to = fee_auth.authorization.to
        if fee_to.lower() != _account.address.lower():
            raise HTTPException(
                status_code=402,
                detail=f"Fee authorization 'to' must be {_account.address}, got {fee_to}",
            )
        fee_value = fee_auth.authorization.value
        if fee_value < _config.fee_amount:
            raise HTTPException(
                status_code=402,
                detail=f"Fee value {fee_value} below minimum {_config.fee_amount}",
            )

    # Reject anything that could not settle before any transaction is sent
    domain = (
        USDC_EIP712_DOMAINS.get(chain_id)
        if token_contract.lower() == default_token.lower()
        else None
    )
    labelled = [(f"authorizations[{i}]", a) for i, a in enumerate(request.authorizations)]
    if fee_auth is not None:
        labelled.append(("feeAuthorization", fee_auth))
    try:
        await asyncio.to_thread(
            validate_batch,
            labelled,
            chain_id=chain_id,
            token_contract=token_contract,
            now=int(time.time()),
            min_value=_config.min_value,
            domain=domain,
            payee_allowlist=_config.payee_allowlist,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None

    # A fee authorization pays for one batch; refuse concurrent or repeated use
    fee_key = None
    if fee_auth is not None:
        fa = fee_auth.authorization
        fee_key = (fa.from_.lower(), _to_bytes32(fa.nonce))
        now = int(time.time())
        for k in [k for k, exp in _fee_nonces.items() if exp <= now]:
            del _fee_nonces[k]
        if fee_key in _fee_nonces:
            raise HTTPException(409, "Fee authorization already in use")
        _fee_nonces[fee_key] = fa.validBefore

    # Check ETH balance
    try:
        balance = await _w3.eth.get_balance(_account.address)
    except Exception:
        logger.exception("RPC get_balance failed")
        if fee_key:
            _fee_nonces.pop(fee_key, None)
        raise HTTPException(503, "RPC unavailable") from None
    if balance < 100_000_000_000_000:  # < 0.0001 ETH
        if fee_key:
            _fee_nonces.pop(fee_key, None)
        raise HTTPException(503, "Settler EOA has insufficient ETH for gas")

    result = await settle_batch(
        w3=_w3,
        settler_account=_account,
        chain_id=_config.chain_id,
        token_contract=token_contract,
        auths=request.authorizations,
        max_calls_per_tx=_config.max_calls_per_tx,
        gas_multiplier=_config.gas_price_multiplier,
        fee_auth=fee_auth,
        max_fee_per_gas_wei=int(_config.max_fee_per_gas_gwei * 1e9),
    )

    # Free the fee auth for retry only when it provably was not collected
    if fee_key and not result.fee_collected and result.total_pending == 0:
        _fee_nonces.pop(fee_key, None)

    # Update lifetime stats
    _stats.batches_settled += 1
    _stats.total_auths_submitted += result.total_submitted
    _stats.total_auths_succeeded += result.total_succeeded
    _stats.total_auths_failed += result.total_failed
    _stats.total_gas_spent_wei += result.total_gas_cost_wei
    _stats.total_value_settled += sum(
        request.authorizations[r.index].authorization.value for r in result.results if r.success
    )

    logger.info(
        "Batch settled: %s — %d/%d succeeded, gas=%d",
        result.status,
        result.total_succeeded,
        result.total_submitted,
        result.total_gas_used,
    )

    return result
