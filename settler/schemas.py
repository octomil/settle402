"""Pydantic models for the settle402 API."""

from __future__ import annotations

from typing import Annotated, Any, Optional

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


def _reject_non_int(v: Any) -> Any:
    if isinstance(v, (bool, float)):
        raise ValueError("must be an integer")
    return v


Uint256 = Annotated[int, BeforeValidator(_reject_non_int), Field(ge=0, lt=2**256)]
Address = Annotated[str, StringConstraints(pattern=r"^0x[0-9a-fA-F]{40}$")]


class Authorization(BaseModel):
    """EIP-3009 transferWithAuthorization parameters."""

    model_config = ConfigDict(populate_by_name=True)

    from_: Address = Field(alias="from")
    to: Address
    value: Uint256
    validAfter: Uint256
    validBefore: Uint256
    nonce: str | Uint256  # 32-byte hex or integer

    @field_validator("nonce")
    @classmethod
    def _nonce_is_32_bytes(cls, v: str | int) -> str | int:
        if isinstance(v, str):
            h = v.removeprefix("0x")
            if len(h) != 64 or any(c not in "0123456789abcdefABCDEF" for c in h):
                raise ValueError("nonce must be 32 bytes of hex")
        return v

    def as_call_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True)


class AuthorizationItem(BaseModel):
    """A single EIP-3009 authorization with signature."""

    authorization: Authorization
    signature: Annotated[str, StringConstraints(pattern=r"^(0x)?[0-9a-fA-F]{130}$")]
    payer: Address
    amount: Uint256  # base units; must equal the signed value

    @model_validator(mode="after")
    def _bind_to_signed_fields(self) -> AuthorizationItem:
        if self.payer.lower() != self.authorization.from_.lower():
            raise ValueError("payer does not match authorization.from")
        if self.amount != self.authorization.value:
            raise ValueError("amount does not match authorization.value")
        return self


class SettleBatchRequest(BaseModel):
    """Batch settlement request."""

    authorizations: list[AuthorizationItem]
    feeAuthorization: AuthorizationItem | None = None
    # Optional — server fills from config if omitted
    network: str | None = None
    chainId: int | None = None
    tokenContract: str | None = None


class AuthorizationResult(BaseModel):
    """Per-authorization settlement result."""

    index: int
    nonce: str
    success: bool
    status: str = "failed"  # "success" | "failed" | "pending" | "unknown"
    error: Optional[str] = None
    tx_hash: Optional[str] = None


class SubBatchResult(BaseModel):
    """Result of a single on-chain transaction (sub-batch)."""

    tx_hash: str
    gas_used: int
    gas_price_gwei: float
    gas_cost_wei: int = 0
    status: str = "confirmed"  # "confirmed" | "pending" | "unknown"
    call_count: int
    success_count: int
    failure_count: int


class SettleBatchResponse(BaseModel):
    """Full batch settlement response with per-auth results."""

    status: str  # "settled" | "partial" | "failed" | "pending"
    total_submitted: int
    total_succeeded: int
    total_failed: int
    total_pending: int = 0  # pending or unknown outcome; do not retry blindly
    total_gas_used: int
    total_gas_cost_wei: int = 0
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
