"""settle402 — Batch settlement for x402 micro-payments via Multicall3.

Accepts batches of signed EIP-3009 transferWithAuthorization payloads and
submits them on-chain in a single transaction, amortizing gas costs across
hundreds of micro-payments.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from pathlib import Path

from eth_account import Account
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from web3 import AsyncHTTPProvider, AsyncWeb3

from .auth import verify_api_key
from .config import CHAIN_NAMES, SettlerConfig
from .schemas import (
    SettleBatchRequest,
    SettleBatchResponse,
    SettlerStats,
    StatusResponse,
)
from .settler import settle_batch

logger = logging.getLogger("settle402")

app = FastAPI(
    title="settle402",
    description="Batch settlement for x402 micro-payments via Multicall3",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Populated on startup
_config: SettlerConfig | None = None
_w3: AsyncWeb3 | None = None
_account = None
_stats = SettlerStats()
_start_time = 0.0


@app.on_event("startup")
async def startup() -> None:
    global _config, _w3, _account, _start_time

    _config = SettlerConfig.from_env()

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

    app.state.api_keys = _config.api_keys + _load_keys()
    app.state.rate_limit = _config.rate_limit
    _start_time = time.monotonic()

    logger.info("settle402 ready on chain %d (%s)", _config.chain_id, _config.network_name)


@app.on_event("shutdown")
async def shutdown() -> None:
    pass


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


_KEYS_FILE = Path("/data/keys.json")


def _save_keys() -> None:
    """Persist API keys to disk."""
    try:
        _KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _KEYS_FILE.write_text(json.dumps(app.state.api_keys))
    except Exception:
        pass  # non-fatal — keys still in memory


def _load_keys() -> list[str]:
    """Load persisted API keys from disk."""
    try:
        if _KEYS_FILE.exists():
            return json.loads(_KEYS_FILE.read_text())
    except Exception:
        pass
    return []


@app.post("/keys")
async def create_key() -> dict:
    """Generate a new API key. Rate-limited by IP via throttling."""
    key = f"s402_{secrets.token_urlsafe(32)}"
    app.state.api_keys.append(key)
    _save_keys()
    return {"key": key}


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "chain_id": _config.chain_id if _config else 0,
        "settler": _account.address if _account else "not configured",
    }


@app.get("/status")
async def status() -> StatusResponse:
    if not _config or not _w3 or not _account:
        raise HTTPException(503, "Settler not configured")

    balance = await _w3.eth.get_balance(_account.address)
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

    if request.chainId != _config.chain_id:
        raise HTTPException(
            400,
            f"Chain mismatch: settler configured for {_config.chain_id}, got {request.chainId}",
        )

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
        fee_to = fee_auth.authorization.get("to", "")
        if fee_to.lower() != _account.address.lower():
            raise HTTPException(
                status_code=402,
                detail=f"Fee authorization 'to' must be {_account.address}, got {fee_to}",
            )
        fee_value = int(fee_auth.authorization.get("value", 0))
        if fee_value < _config.fee_amount:
            raise HTTPException(
                status_code=402,
                detail=f"Fee value {fee_value} below minimum {_config.fee_amount}",
            )

    # Check ETH balance
    balance = await _w3.eth.get_balance(_account.address)
    if balance < 100_000_000_000_000:  # < 0.0001 ETH
        raise HTTPException(503, "Settler EOA has insufficient ETH for gas")

    result = await settle_batch(
        w3=_w3,
        settler_account=_account,
        chain_id=_config.chain_id,
        token_contract=request.tokenContract,
        auths=request.authorizations,
        max_calls_per_tx=_config.max_calls_per_tx,
        gas_multiplier=_config.gas_price_multiplier,
        fee_auth=fee_auth,
    )

    # Update lifetime stats
    _stats.batches_settled += 1
    _stats.total_auths_submitted += result.total_submitted
    _stats.total_auths_succeeded += result.total_succeeded
    _stats.total_auths_failed += result.total_failed
    _stats.total_gas_spent_wei += result.total_gas_used
    _stats.total_value_settled += sum(
        a.amount for a, r in zip(request.authorizations, result.results) if r.success
    )

    logger.info(
        "Batch settled: %s — %d/%d succeeded, gas=%d",
        result.status,
        result.total_succeeded,
        result.total_submitted,
        result.total_gas_used,
    )

    return result
