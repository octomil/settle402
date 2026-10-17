"""Up-front validation of authorizations, before anything is sent on-chain."""

from __future__ import annotations

from collections.abc import Sequence

from eth_account import Account
from eth_account.messages import encode_typed_data

from .config import MULTICALL3_ADDRESS
from .schemas import AuthorizationItem
from .settler import _to_bytes32, split_signature

ZERO_ADDRESS = "0x" + "00" * 20
EXPIRY_MARGIN_SECONDS = 60  # leave time for the tx to be mined before validBefore

_TWA_TYPES = {
    "TransferWithAuthorization": [
        {"name": "from", "type": "address"},
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"},
        {"name": "nonce", "type": "bytes32"},
    ]
}


def recover_signer(
    item: AuthorizationItem,
    chain_id: int,
    token_contract: str,
    domain: tuple[str, str],
) -> str:
    a = item.authorization
    v, r, s = split_signature(item.signature)
    signable = encode_typed_data(
        domain_data={
            "name": domain[0],
            "version": domain[1],
            "chainId": chain_id,
            "verifyingContract": token_contract,
        },
        message_types=_TWA_TYPES,
        message_data={
            "from": a.from_,
            "to": a.to,
            "value": a.value,
            "validAfter": a.validAfter,
            "validBefore": a.validBefore,
            "nonce": _to_bytes32(a.nonce),
        },
    )
    return Account.recover_message(
        signable, vrs=(v, int.from_bytes(r, "big"), int.from_bytes(s, "big"))
    )


def validate_authorization(
    item: AuthorizationItem,
    *,
    chain_id: int,
    token_contract: str,
    now: int,
    min_value: int,
    domain: tuple[str, str] | None = None,
    payee_allowlist: Sequence[str] = (),
) -> None:
    """Raise ValueError if the authorization could not possibly settle."""
    a = item.authorization
    if a.from_.lower() == ZERO_ADDRESS:
        raise ValueError("from is the zero address")
    forbidden = {ZERO_ADDRESS, token_contract.lower(), MULTICALL3_ADDRESS.lower()}
    if a.to.lower() in forbidden:
        raise ValueError("to is not a valid recipient")
    if payee_allowlist and a.to.lower() not in {p.lower() for p in payee_allowlist}:
        raise ValueError("to is not an allowed recipient")
    if a.value < min_value:
        raise ValueError(f"value below minimum {min_value}")
    if a.validAfter >= now:
        raise ValueError("authorization not yet valid")
    if a.validBefore <= now + EXPIRY_MARGIN_SECONDS:
        raise ValueError("authorization expired or expiring too soon")
    try:
        split_signature(item.signature)
        _to_bytes32(a.nonce)
    except ValueError as exc:
        raise ValueError(f"malformed signature or nonce: {exc}") from None
    if domain is not None:
        signer = recover_signer(item, chain_id, token_contract, domain)
        if signer.lower() != a.from_.lower():
            raise ValueError("signature does not match from")


def validate_batch(items: Sequence[tuple[str, AuthorizationItem]], **kwargs) -> None:
    """Validate labelled items and reject duplicate (from, nonce) pairs."""
    seen: set[tuple[str, bytes]] = set()
    for label, item in items:
        try:
            validate_authorization(item, **kwargs)
        except ValueError as exc:
            raise ValueError(f"{label}: {exc}") from None
        key = (item.authorization.from_.lower(), _to_bytes32(item.authorization.nonce))
        if key in seen:
            raise ValueError(f"{label}: duplicate (from, nonce)")
        seen.add(key)
