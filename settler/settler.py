"""Core batch settlement logic — Multicall3 + EIP-3009."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from eth_abi import encode
from eth_account import Account
from eth_account.signers.local import LocalAccount
from web3 import AsyncWeb3
from web3.types import TxReceipt

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

# Serialization lock — prevents nonce conflicts when single replica
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
    return v, r, s


def _to_bytes32(val: Any) -> bytes:
    """Convert a nonce value to bytes32."""
    if isinstance(val, bytes):
        return val.ljust(32, b"\x00")[:32]
    if isinstance(val, str):
        if val.startswith("0x"):
            return bytes.fromhex(val[2:]).rjust(32, b"\x00")[:32]
        return bytes.fromhex(val).rjust(32, b"\x00")[:32]
    if isinstance(val, int):
        return val.to_bytes(32, "big")
    raise ValueError(f"Cannot convert {type(val)} to bytes32")


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
# Core settlement
# ---------------------------------------------------------------------------


async def settle_batch(
    w3: AsyncWeb3,
    settler_account: LocalAccount,
    chain_id: int,
    token_contract: str,
    auths: list[AuthorizationItem],
    max_calls_per_tx: int = 400,
    gas_multiplier: float = 1.1,
) -> SettleBatchResponse:
    """Submit a batch of EIP-3009 authorizations via Multicall3.

    Uses aggregate3 with allowFailure=true so individual auth failures
    don't revert the entire transaction.
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
        )


async def _settle_batch_inner(
    w3: AsyncWeb3,
    settler_account: LocalAccount,
    chain_id: int,
    token_contract: str,
    auths: list[AuthorizationItem],
    max_calls_per_tx: int,
    gas_multiplier: float,
) -> SettleBatchResponse:
    sub_batches = split_into_sub_batches(auths, max_calls_per_tx)
    all_results: list[AuthorizationResult] = []
    sub_batch_results: list[SubBatchResult] = []
    global_index = 0
    total_gas_used = 0

    for sub_batch in sub_batches:
        # Build Multicall3 calls
        calls: list[tuple[str, bool, bytes]] = []
        for auth_item in sub_batch:
            v, r, s = split_signature(auth_item.signature)
            calldata = encode_transfer_with_authorization(auth_item.authorization, v, r, s)
            calls.append((token_contract, True, calldata))  # allowFailure=True

        multicall_data = encode_aggregate3(calls)

        # Estimate gas
        try:
            gas_estimate = await w3.eth.estimate_gas(
                {
                    "from": settler_account.address,
                    "to": MULTICALL3_ADDRESS,
                    "data": multicall_data,
                }
            )
        except Exception as exc:
            logger.error("Gas estimation failed for sub-batch of %d: %s", len(sub_batch), exc)
            for i, auth_item in enumerate(sub_batch):
                all_results.append(
                    AuthorizationResult(
                        index=global_index + i,
                        nonce=str(auth_item.authorization.get("nonce", "")),
                        success=False,
                        error=f"gas estimation failed: {exc}",
                    )
                )
            global_index += len(sub_batch)
            continue

        # Build EIP-1559 transaction
        fee_history = await w3.eth.fee_history(1, "latest", [50])
        base_fee = fee_history["baseFeePerGas"][-1]
        max_priority = max(
            fee_history["reward"][0][0] if fee_history["reward"] else 100_000_000,
            100_000_000,
        )
        max_fee = int(base_fee * 2) + max_priority

        nonce = await w3.eth.get_transaction_count(settler_account.address, "pending")
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

        # Send and wait
        try:
            tx_hash = await w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt: TxReceipt = await w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        except Exception as exc:
            logger.error("Transaction failed for sub-batch of %d: %s", len(sub_batch), exc)
            for i, auth_item in enumerate(sub_batch):
                all_results.append(
                    AuthorizationResult(
                        index=global_index + i,
                        nonce=str(auth_item.authorization.get("nonce", "")),
                        success=False,
                        error=f"tx failed: {exc}",
                    )
                )
            global_index += len(sub_batch)
            continue

        gas_used = receipt["gasUsed"]
        effective_gas_price = receipt.get("effectiveGasPrice", 0)
        total_gas_used += gas_used
        tx_hash_hex = receipt["transactionHash"].hex()

        # Decode Multicall3 results from the tx output
        try:
            raw_output = await w3.eth.call(
                {
                    "from": settler_account.address,
                    "to": MULTICALL3_ADDRESS,
                    "data": multicall_data,
                },
                receipt["blockNumber"],
            )
            decoded = decode_aggregate3_results(raw_output)
        except Exception:
            # Fallback: if we can't decode, assume all succeeded if tx succeeded
            decoded = [(receipt["status"] == 1, b"")] * len(sub_batch)

        success_count = 0
        failure_count = 0
        for i, (auth_item, (success, return_data)) in enumerate(zip(sub_batch, decoded)):
            error_msg = None
            if not success:
                error_msg = decode_revert_reason(return_data)
                failure_count += 1
            else:
                success_count += 1
            all_results.append(
                AuthorizationResult(
                    index=global_index + i,
                    nonce=str(auth_item.authorization.get("nonce", "")),
                    success=success,
                    error=error_msg,
                    tx_hash=tx_hash_hex,
                )
            )

        sub_batch_results.append(
            SubBatchResult(
                tx_hash=tx_hash_hex,
                gas_used=gas_used,
                gas_price_gwei=effective_gas_price / 1e9 if effective_gas_price else 0.0,
                call_count=len(sub_batch),
                success_count=success_count,
                failure_count=failure_count,
            )
        )
        global_index += len(sub_batch)

        logger.info(
            "Sub-batch settled: %d/%d succeeded, tx=%s, gas=%d",
            success_count,
            len(sub_batch),
            tx_hash_hex[:10],
            gas_used,
        )

    # Build final response
    total_succeeded = sum(r.success for r in all_results)
    total_failed = sum(not r.success for r in all_results)
    total_gas_cost_wei = sum(sb.gas_used * int(sb.gas_price_gwei * 1e9) for sb in sub_batch_results)

    if total_failed == 0:
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
        total_gas_used=total_gas_used,
        total_gas_cost_eth=f"{total_gas_cost_wei / 1e18:.8f}",
        sub_batches=sub_batch_results,
        results=all_results,
    )
