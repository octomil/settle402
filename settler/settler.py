"""Core batch settlement logic — Multicall3 + EIP-3009."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from decimal import Decimal
from typing import Any

from eth_abi import encode
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_utils import keccak
from web3 import AsyncWeb3

from .config import MULTICALL3_ADDRESS
from .schemas import (
    AuthorizationItem,
    AuthorizationResult,
    SettleBatchResponse,
    SubBatchResult,
)

logger = logging.getLogger("settle402")

# ---------------------------------------------------------------------------
# ABI constants
# ---------------------------------------------------------------------------

# transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,uint8,bytes32,bytes32)
TRANSFER_WITH_AUTH_SELECTOR = bytes.fromhex("e3ee160e")

# aggregate3((address,bool,bytes)[]) → (bool,bytes)[]
AGGREGATE3_SELECTOR = bytes.fromhex("82ad56cb")

# FiatTokenV2 events: AuthorizationUsed(address indexed, bytes32 indexed), Transfer(from, to, value)
AUTHORIZATION_USED_TOPIC = keccak(text="AuthorizationUsed(address,bytes32)")
TRANSFER_TOPIC = keccak(text="Transfer(address,address,uint256)")

SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141

# Process-local: nonce safety assumes exactly one process and one replica
_settlement_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Signature helpers
# ---------------------------------------------------------------------------


def split_signature(sig_hex: str) -> tuple[int, bytes, bytes]:
    """Split a 65-byte hex signature into (v, r, s)."""
    sig_bytes = bytes.fromhex(sig_hex.removeprefix("0x"))
    if len(sig_bytes) != 65:
        raise ValueError(f"Signature must be 65 bytes, got {len(sig_bytes)}")
    r = sig_bytes[0:32]
    s = sig_bytes[32:64]
    v = sig_bytes[64]
    if v < 27:
        v += 27
    if v not in (27, 28):
        raise ValueError(f"Invalid signature v: {v}")
    r_int = int.from_bytes(r, "big")
    s_int = int.from_bytes(s, "big")
    if not 0 < r_int < SECP256K1_N or not 0 < s_int <= SECP256K1_N // 2:
        raise ValueError("Signature r/s out of range or s not in lower half")
    return v, r, s


def _to_bytes32(val: Any) -> bytes:
    """Convert a nonce value to exactly 32 bytes."""
    if isinstance(val, bytes):
        out = val
    elif isinstance(val, str):
        out = bytes.fromhex(val.removeprefix("0x"))
    elif isinstance(val, int) and not isinstance(val, bool):
        if not 0 <= val < 2**256:
            raise ValueError("nonce out of uint256 range")
        return val.to_bytes(32, "big")
    else:
        raise ValueError(f"Cannot convert {type(val)} to bytes32")
    if len(out) != 32:
        raise ValueError(f"Expected 32 bytes, got {len(out)}")
    return out


# ---------------------------------------------------------------------------
# Calldata encoding
# ---------------------------------------------------------------------------


def encode_transfer_with_authorization(
    auth: dict[str, Any],
    v: int,
    r: bytes,
    s: bytes,
) -> bytes:
    """Encode a single transferWithAuthorization call."""
    calldata = TRANSFER_WITH_AUTH_SELECTOR + encode(
        [
            "address",
            "address",
            "uint256",
            "uint256",
            "uint256",
            "bytes32",
            "uint8",
            "bytes32",
            "bytes32",
        ],
        [
            auth["from"],
            auth["to"],
            int(auth["value"]),
            int(auth["validAfter"]),
            int(auth["validBefore"]),
            _to_bytes32(auth["nonce"]),
            v,
            r,
            s,
        ],
    )
    return calldata


def encode_aggregate3(calls: list[tuple[str, bool, bytes]]) -> bytes:
    """Encode a Multicall3 aggregate3 call.

    Each call is (target_address, allow_failure, calldata).
    """
    encoded_calls = [(call[0], call[1], call[2]) for call in calls]
    calldata = AGGREGATE3_SELECTOR + encode(
        ["(address,bool,bytes)[]"],
        [encoded_calls],
    )
    return calldata


def decode_aggregate3_results(raw_output: bytes) -> list[tuple[bool, bytes]]:
    """Decode aggregate3 return data into per-call (success, returnData)."""
    from eth_abi import decode as abi_decode

    decoded = abi_decode(["(bool,bytes)[]"], raw_output)
    return [(success, data) for success, data in decoded[0]]


def decode_revert_reason(return_data: bytes) -> str:
    """Try to decode a revert reason from return data."""
    if len(return_data) < 4:
        return "unknown revert"
    # Error(string) selector = 0x08c379a0
    if return_data[:4] == bytes.fromhex("08c379a0"):
        try:
            from eth_abi import decode as abi_decode

            (reason,) = abi_decode(["string"], return_data[4:])
            return reason
        except Exception:
            pass
    return f"revert (0x{return_data[:4].hex()})"


# ---------------------------------------------------------------------------
# Batch splitting
# ---------------------------------------------------------------------------


def split_into_sub_batches(
    auths: list[AuthorizationItem],
    max_per_tx: int,
) -> list[list[AuthorizationItem]]:
    """Split authorization list into sub-batches that fit within tx gas limit."""
    return [auths[i : i + max_per_tx] for i in range(0, len(auths), max_per_tx)]


# ---------------------------------------------------------------------------
# Receipt decoding
# ---------------------------------------------------------------------------


def _bytes(val: Any) -> bytes:
    if isinstance(val, (bytes, bytearray)):
        return bytes(val)
    return bytes.fromhex(str(val).removeprefix("0x"))


def _hex(val: Any) -> str:
    return "0x" + _bytes(val).hex()


def _addr_topic(addr: str) -> bytes:
    return bytes.fromhex(addr.removeprefix("0x")).rjust(32, b"\x00")


def match_authorizations(
    logs: list[Any],
    token_contract: str,
    auths: list[AuthorizationItem],
) -> list[bool]:
    """Per-auth success derived from the token's own logs in the receipt.

    An auth succeeded iff the token emitted AuthorizationUsed(from, nonce) and a
    matching Transfer(from, to, value) in this transaction.
    """
    token = token_contract.lower()
    used: set[tuple[bytes, bytes]] = set()
    transfers: Counter[tuple[bytes, bytes, int]] = Counter()
    for log in logs:
        if str(log["address"]).lower() != token:
            continue
        topics = [_bytes(t) for t in log["topics"]]
        if not topics:
            continue
        if topics[0] == AUTHORIZATION_USED_TOPIC and len(topics) == 3:
            used.add((topics[1], topics[2]))
        elif topics[0] == TRANSFER_TOPIC and len(topics) == 3:
            value = int.from_bytes(_bytes(log["data"]), "big")
            transfers[(topics[1], topics[2], value)] += 1

    outcomes = []
    for item in auths:
        a = item.authorization
        frm = _addr_topic(a.from_)
        tkey = (frm, _addr_topic(a.to), a.value)
        ok = (frm, _to_bytes32(a.nonce)) in used and transfers[tkey] > 0
        if ok:
            transfers[tkey] -= 1
        outcomes.append(ok)
    return outcomes


# ---------------------------------------------------------------------------
# Core settlement
# ---------------------------------------------------------------------------

_NOT_EXECUTED = "authorization not executed (reverted or already used)"


def _results(
    sub_batch: list[AuthorizationItem],
    start: int,
    *,
    status: str,
    error: str | None,
    tx_hash: str | None = None,
) -> list[AuthorizationResult]:
    return [
        AuthorizationResult(
            index=start + i,
            nonce=str(a.authorization.nonce),
            success=status == "success",
            status=status,
            error=error,
            tx_hash=tx_hash,
        )
        for i, a in enumerate(sub_batch)
    ]


def _unresolved(
    sub_batch: list[AuthorizationItem],
    start: int,
    status: str,
    tx_hash: str,
    error: str,
) -> tuple[list[AuthorizationResult], SubBatchResult]:
    sb = SubBatchResult(
        tx_hash=tx_hash,
        gas_used=0,
        gas_price_gwei=0.0,
        status=status,
        call_count=len(sub_batch),
        success_count=0,
        failure_count=0,
    )
    return _results(sub_batch, start, status=status, error=error, tx_hash=tx_hash), sb


async def settle_batch(
    w3: AsyncWeb3,
    settler_account: LocalAccount,
    chain_id: int,
    token_contract: str,
    auths: list[AuthorizationItem],
    max_calls_per_tx: int = 400,
    gas_multiplier: float = 1.1,
    fee_auth: AuthorizationItem | None = None,
    max_fee_per_gas_wei: int | None = None,
) -> SettleBatchResponse:
    """Submit a batch of EIP-3009 authorizations via Multicall3.

    Uses aggregate3 with allowFailure=true so individual auth failures
    don't revert the entire transaction. If fee_auth is provided, it is
    prepended to the first sub-batch with allowFailure=false.
    """
    async with _settlement_lock:
        return await _settle_batch_inner(
            w3,
            settler_account,
            chain_id,
            token_contract,
            auths,
            max_calls_per_tx,
            gas_multiplier,
            fee_auth,
            max_fee_per_gas_wei,
        )


async def _run_sub_batch(
    w3: AsyncWeb3,
    settler_account: LocalAccount,
    chain_id: int,
    token_contract: str,
    sub_batch: list[AuthorizationItem],
    start: int,
    gas_multiplier: float,
    fee_auth: AuthorizationItem | None,
    max_fee_per_gas_wei: int | None,
) -> tuple[list[AuthorizationResult], SubBatchResult | None, bool, bool]:
    """Returns (results, sub_batch_result, fee_collected, halt).

    halt means later sub-batches must not be sent (fee not collected, or the
    outcome of this transaction is not known yet).
    """
    has_fee = fee_auth is not None
    calls: list[tuple[str, bool, bytes]] = []
    if fee_auth is not None:
        fv, fr, fs = split_signature(fee_auth.signature)
        fee_cd = encode_transfer_with_authorization(
            fee_auth.authorization.as_call_dict(), fv, fr, fs
        )
        calls.append((token_contract, False, fee_cd))
    for item in sub_batch:
        v, r, s = split_signature(item.signature)
        cd = encode_transfer_with_authorization(item.authorization.as_call_dict(), v, r, s)
        calls.append((token_contract, True, cd))
    multicall_data = encode_aggregate3(calls)

    def not_sent(error: str):
        return _results(sub_batch, start, status="failed", error=error), None, False, has_fee

    try:
        gas_estimate = await w3.eth.estimate_gas(
            {"from": settler_account.address, "to": MULTICALL3_ADDRESS, "data": multicall_data}
        )
        fee_history = await w3.eth.fee_history(1, "latest", [50])
        base_fee = fee_history["baseFeePerGas"][-1]
        max_priority = max(
            fee_history["reward"][0][0] if fee_history["reward"] else 100_000_000,
            100_000_000,
        )
        max_fee = base_fee * 2 + max_priority
        nonce = await w3.eth.get_transaction_count(settler_account.address, "pending")
    except Exception as exc:
        logger.error("Pre-send RPC calls failed for sub-batch of %d: %s", len(sub_batch), exc)
        return not_sent("gas estimation or RPC failure")

    if max_fee_per_gas_wei is not None and max_fee > max_fee_per_gas_wei:
        logger.error("Gas price %d wei above ceiling %d", max_fee, max_fee_per_gas_wei)
        return not_sent("gas price above ceiling")

    tx = {
        "to": MULTICALL3_ADDRESS,
        "data": multicall_data,
        "gas": int(gas_estimate * gas_multiplier),
        "nonce": nonce,
        "chainId": chain_id,
        "type": 2,
        "maxFeePerGas": max_fee,
        "maxPriorityFeePerGas": max_priority,
    }
    signed = Account.sign_transaction(tx, settler_account.key)
    local_hash = _hex(signed.hash)

    try:
        tx_hash = await w3.eth.send_raw_transaction(signed.raw_transaction)
    except Exception as exc:
        # The node may have accepted the tx before the error; do not claim it failed
        logger.error("send_raw_transaction errored for %s: %s", local_hash, exc)
        results, sb = _unresolved(
            sub_batch, start, "unknown", local_hash, "submission outcome unknown; check tx_hash"
        )
        return results, sb, False, True

    try:
        receipt = await w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    except Exception as exc:
        logger.warning("No receipt for %s yet: %s", _hex(tx_hash), exc)
        results, sb = _unresolved(
            sub_batch, start, "pending", _hex(tx_hash), "receipt not yet available; check tx_hash"
        )
        return results, sb, False, True

    gas_used = receipt["gasUsed"]
    price = receipt.get("effectiveGasPrice", 0)
    tx_hash_hex = _hex(receipt["transactionHash"])
    fee_collected = False
    sb_status = "confirmed"
    halt = False

    logs = receipt.get("logs")
    if receipt["status"] != 1:
        outcomes: list[bool] | None = [False] * len(calls)
    elif isinstance(logs, (list, tuple)):
        outcomes = match_authorizations(
            list(logs), token_contract, ([fee_auth] if fee_auth else []) + sub_batch
        )
    else:
        outcomes = None

    if outcomes is None:
        logger.error("Receipt for %s has no logs; cannot determine outcomes", tx_hash_hex)
        results = _results(
            sub_batch, start, status="unknown", error="outcome undeterminable", tx_hash=tx_hash_hex
        )
        sb_status, halt = "unknown", True
        success_count = failure_count = 0
    else:
        if has_fee:
            fee_collected = outcomes[0]
            outcomes = outcomes[1:]
        results = []
        for i, (item, ok) in enumerate(zip(sub_batch, outcomes)):
            results.append(
                AuthorizationResult(
                    index=start + i,
                    nonce=str(item.authorization.nonce),
                    success=ok,
                    status="success" if ok else "failed",
                    error=None if ok else _NOT_EXECUTED,
                    tx_hash=tx_hash_hex,
                )
            )
        success_count = sum(outcomes)
        failure_count = len(outcomes) - success_count
        halt = has_fee and not fee_collected

    sb = SubBatchResult(
        tx_hash=tx_hash_hex,
        gas_used=gas_used,
        gas_price_gwei=price / 1e9 if price else 0.0,
        gas_cost_wei=gas_used * price,
        status=sb_status,
        call_count=len(sub_batch),
        success_count=success_count,
        failure_count=failure_count,
    )
    return results, sb, fee_collected, halt


async def _settle_batch_inner(
    w3: AsyncWeb3,
    settler_account: LocalAccount,
    chain_id: int,
    token_contract: str,
    auths: list[AuthorizationItem],
    max_calls_per_tx: int,
    gas_multiplier: float,
    fee_auth: AuthorizationItem | None = None,
    max_fee_per_gas_wei: int | None = None,
) -> SettleBatchResponse:
    all_results: list[AuthorizationResult] = []
    sub_batch_results: list[SubBatchResult] = []
    fee_collected = False
    halted = False

    for batch_index, sub_batch in enumerate(split_into_sub_batches(auths, max_calls_per_tx)):
        start = len(all_results)
        if halted:
            all_results += _results(
                sub_batch,
                start,
                status="failed",
                error="not submitted: earlier sub-batch halted the run",
            )
            continue

        results, sb, batch_fee, halt = await _run_sub_batch(
            w3,
            settler_account,
            chain_id,
            token_contract,
            sub_batch,
            start,
            gas_multiplier,
            fee_auth if batch_index == 0 else None,
            max_fee_per_gas_wei,
        )
        all_results += results
        halted = halt
        if batch_index == 0:
            fee_collected = batch_fee
        if sb is not None:
            sub_batch_results.append(sb)
            logger.info(
                "Sub-batch %s: %d/%d succeeded, tx=%s, gas=%d",
                sb.status,
                sb.success_count,
                sb.call_count,
                sb.tx_hash[:10],
                sb.gas_used,
            )

    total_succeeded = sum(r.status == "success" for r in all_results)
    total_failed = sum(r.status == "failed" for r in all_results)
    total_pending = len(all_results) - total_succeeded - total_failed
    total_gas_cost_wei = sum(sb.gas_cost_wei for sb in sub_batch_results)

    if total_pending:
        status = "pending"
    elif total_failed == 0:
        status = "settled"
    elif total_succeeded > 0:
        status = "partial"
    else:
        status = "failed"

    return SettleBatchResponse(
        status=status,
        total_submitted=len(auths),
        total_succeeded=total_succeeded,
        total_failed=total_failed,
        total_pending=total_pending,
        total_gas_used=sum(sb.gas_used for sb in sub_batch_results),
        total_gas_cost_wei=total_gas_cost_wei,
        total_gas_cost_eth=f"{Decimal(total_gas_cost_wei) / Decimal(10**18):.8f}",
        sub_batches=sub_batch_results,
        results=all_results,
        fee_collected=fee_collected,
    )
