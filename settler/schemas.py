"""Pydantic models for the settle402 API."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel


class AuthorizationItem(BaseModel):
    """A single EIP-3009 authorization with signature."""

    authorization: dict[str, Any]  # from, to, value, validAfter, validBefore, nonce
    signature: str  # 65-byte hex (r[32] + s[32] + v[1])
    payer: str  # 0x address
    amount: int  # base units (informational)


class SettleBatchRequest(BaseModel):
    """Batch settlement request."""

    network: str  # "base", "base-sepolia", etc.
    chainId: int  # 8453, 84532, etc.
    tokenContract: str  # EIP-3009 token (e.g. USDC)
    authorizations: list[AuthorizationItem]
    feeAuthorization: AuthorizationItem | None = None


class AuthorizationResult(BaseModel):
    """Per-authorization settlement result."""

    index: int
    nonce: str
    success: bool
    error: Optional[str] = None
    tx_hash: Optional[str] = None


class SubBatchResult(BaseModel):
    """Result of a single on-chain transaction (sub-batch)."""

    tx_hash: str
    gas_used: int
    gas_price_gwei: float
    call_count: int
    success_count: int
    failure_count: int


class SettleBatchResponse(BaseModel):
    """Full batch settlement response with per-auth results."""

    status: str  # "settled" | "partial" | "failed"
    total_submitted: int
    total_succeeded: int
    total_failed: int
    total_gas_used: int
    total_gas_cost_eth: str
    sub_batches: list[SubBatchResult]
    results: list[AuthorizationResult]
    fee_collected: bool = False


class SettlerStats(BaseModel):
    """Lifetime settler statistics."""

    batches_settled: int = 0
    total_auths_submitted: int = 0
    total_auths_succeeded: int = 0
    total_auths_failed: int = 0
    total_gas_spent_wei: int = 0
    total_value_settled: int = 0  # base units
    uptime_seconds: float = 0.0


class StatusResponse(BaseModel):
    """Health + stats response."""

    status: str
    settler_address: str
    eth_balance: str
    chain_id: int
    network: str
    stats: SettlerStats
