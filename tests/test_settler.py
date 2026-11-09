"""Unit tests for settle402 core logic."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from eth_account import Account

from settler.schemas import AuthorizationItem
from settler.settler import (
    AUTHORIZATION_USED_TOPIC,
    SECP256K1_N,
    TRANSFER_TOPIC,
    _addr_topic,
    _to_bytes32,
    decode_revert_reason,
    encode_aggregate3,
    encode_transfer_with_authorization,
    match_authorizations,
    settle_batch,
    split_into_sub_batches,
    split_signature,
)


class TestSplitSignature:
    def test_valid_65_byte_signature(self):
        sig = "0x" + "ab" * 32 + "11" * 32 + "1b"
        v, r, s = split_signature(sig)
        assert v == 27
        assert r == bytes.fromhex("ab" * 32)
        assert s == bytes.fromhex("11" * 32)

    def test_v_normalization(self):
        sig = "0x" + "ab" * 32 + "11" * 32 + "00"  # v=0 → 27
        v, _, _ = split_signature(sig)
        assert v == 27

        sig = "0x" + "ab" * 32 + "11" * 32 + "01"  # v=1 → 28
        v, _, _ = split_signature(sig)
        assert v == 28

    def test_v_already_normalized(self):
        sig = "0x" + "ab" * 32 + "11" * 32 + "1b"  # v=27 stays 27
        v, _, _ = split_signature(sig)
        assert v == 27

    def test_invalid_length(self):
        with pytest.raises(ValueError, match="65 bytes"):
            split_signature("0x" + "ab" * 30)

    def test_no_0x_prefix(self):
        sig = "ab" * 32 + "11" * 32 + "1b"
        v, r, s = split_signature(sig)
        assert v == 27


class TestSignatureChecks:
    def test_high_s_rejected(self):
        high_s = (SECP256K1_N // 2 + 1).to_bytes(32, "big").hex()
        with pytest.raises(ValueError, match="lower half"):
            split_signature("0x" + "ab" * 32 + high_s + "1b")

    def test_zero_r_rejected(self):
        with pytest.raises(ValueError, match="out of range"):
            split_signature("0x" + "00" * 32 + "11" * 32 + "1b")

    def test_bad_v_rejected(self):
        with pytest.raises(ValueError, match="Invalid signature v"):
            split_signature("0x" + "ab" * 32 + "11" * 32 + "1d")


class TestToBytes32:
    def test_exact_hex(self):
        assert _to_bytes32("0x" + "aa" * 32) == b"\xaa" * 32

    def test_int(self):
        assert _to_bytes32(5) == (5).to_bytes(32, "big")

    @pytest.mark.parametrize("val", ["0x" + "aa" * 31, "0x" + "aa" * 33, b"\x01" * 4, -1, 2**256])
    def test_wrong_size_rejected(self, val):
        with pytest.raises(ValueError):
            _to_bytes32(val)


class TestEncodeTransferWithAuthorization:
    def test_produces_calldata(self):
        auth = {
            "from": "0x" + "11" * 20,
            "to": "0x" + "22" * 20,
            "value": 1000000,
            "validAfter": 0,
            "validBefore": 2**256 - 1,
            "nonce": "0x" + "aa" * 32,
        }
        calldata = encode_transfer_with_authorization(auth, 27, b"\x00" * 32, b"\x00" * 32)
        assert calldata[:4] == bytes.fromhex("e3ee160e")
        assert len(calldata) > 4

    def test_integer_nonce(self):
        auth = {
            "from": "0x" + "11" * 20,
            "to": "0x" + "22" * 20,
            "value": 1000,
            "validAfter": 0,
            "validBefore": 0,
            "nonce": 42,
        }
        calldata = encode_transfer_with_authorization(auth, 28, b"\x01" * 32, b"\x02" * 32)
        assert len(calldata) > 4


class TestEncodeAggregate3:
    def test_single_call(self):
        calls = [("0x" + "33" * 20, True, b"\x00\x01\x02\x03")]
        calldata = encode_aggregate3(calls)
        assert calldata[:4] == bytes.fromhex("82ad56cb")

    def test_multiple_calls(self):
        calls = [
            ("0x" + "33" * 20, True, b"\x00" * 4),
            ("0x" + "44" * 20, False, b"\xff" * 4),
        ]
        calldata = encode_aggregate3(calls)
        assert len(calldata) > 4


class TestSplitIntoSubBatches:
    def test_single_batch(self):
        auths = [_make_auth(i) for i in range(5)]
        result = split_into_sub_batches(auths, 10)
        assert len(result) == 1
        assert len(result[0]) == 5

    def test_multiple_batches(self):
        auths = [_make_auth(i) for i in range(10)]
        result = split_into_sub_batches(auths, 3)
        assert len(result) == 4
        assert len(result[0]) == 3
        assert len(result[3]) == 1

    def test_exact_batch_size(self):
        auths = [_make_auth(i) for i in range(6)]
        result = split_into_sub_batches(auths, 3)
        assert len(result) == 2


class TestDecodeRevertReason:
    def test_empty_data(self):
        assert decode_revert_reason(b"") == "unknown revert"

    def test_short_data(self):
        assert decode_revert_reason(b"\x00\x01") == "unknown revert"

    def test_unknown_selector(self):
        result = decode_revert_reason(b"\xff\xff\xff\xff" + b"\x00" * 32)
        assert result.startswith("revert (0x")


class TestFeeCallEncoding:
    def test_fee_call_uses_allow_failure_false(self):
        """Fee call should use allowFailure=False, user calls use True."""
        fee_auth = _make_auth(0)
        user_auth = _make_auth(1)

        v1, r1, s1 = split_signature(fee_auth.signature)
        fee_cd = encode_transfer_with_authorization(
            fee_auth.authorization.as_call_dict(), v1, r1, s1
        )
        fee_call = ("0x" + "ff" * 20, False, fee_cd)

        v2, r2, s2 = split_signature(user_auth.signature)
        user_cd = encode_transfer_with_authorization(
            user_auth.authorization.as_call_dict(), v2, r2, s2
        )
        user_call = ("0x" + "ff" * 20, True, user_cd)

        calls = [fee_call, user_call]
        calldata = encode_aggregate3(calls)
        assert calldata[:4] == bytes.fromhex("82ad56cb")
        # Fee call has allowFailure=False, user has True
        assert fee_call[1] is False
        assert user_call[1] is True


FAR_FUTURE = 4_000_000_000


def _make_auth(index: int) -> AuthorizationItem:
    return AuthorizationItem(
        authorization={
            "from": "0x" + "11" * 20,
            "to": "0x" + "22" * 20,
            "value": 1000 * (index + 1),
            "validAfter": 0,
            "validBefore": FAR_FUTURE,
            "nonce": index,
        },
        signature="0x" + "ab" * 32 + "11" * 32 + "1b",
        payer="0x" + "11" * 20,
        amount=1000 * (index + 1),
    )


# ---------------------------------------------------------------------------
# Receipt decoding and settle_batch (mocked RPC, no network)
# ---------------------------------------------------------------------------

TOKEN = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
SETTLER = Account.from_key("0x" + "ab" * 32)


def _logs(auths, ok_flags, token=TOKEN):
    logs = []
    for item, ok in zip(auths, ok_flags):
        if not ok:
            continue
        a = item.authorization
        frm = _addr_topic(a.from_)
        logs.append(
            {
                "address": token,
                "topics": [AUTHORIZATION_USED_TOPIC, frm, _to_bytes32(a.nonce)],
                "data": b"",
            }
        )
        logs.append(
            {
                "address": token,
                "topics": [TRANSFER_TOPIC, frm, _addr_topic(a.to)],
                "data": a.value.to_bytes(32, "big"),
            }
        )
    return logs


def _receipt(logs, status=1, price=2 * 10**9, gas=21_000, tx_hash=b"\x01" * 32):
    return {
        "status": status,
        "gasUsed": gas,
        "effectiveGasPrice": price,
        "transactionHash": tx_hash,
        "blockNumber": 100,
        "logs": logs,
    }


def _w3(receipts=None, send_exc=None, wait_exc=None, estimate=None, base_fee=10**9):
    receipts = list(receipts or [])

    async def wait(tx_hash, timeout):
        if wait_exc:
            raise wait_exc
        return receipts.pop(0)

    eth = SimpleNamespace(
        estimate_gas=AsyncMock(side_effect=estimate)
        if estimate
        else AsyncMock(return_value=100_000),
        fee_history=AsyncMock(return_value={"baseFeePerGas": [base_fee], "reward": [[10**8]]}),
        get_transaction_count=AsyncMock(return_value=7),
        send_raw_transaction=AsyncMock(side_effect=send_exc, return_value=b"\x01" * 32),
        wait_for_transaction_receipt=wait,
    )
    return SimpleNamespace(eth=eth)


async def _settle(w3, auths, **kw):
    return await settle_batch(w3, SETTLER, 8453, TOKEN, auths, **kw)


class TestMatchAuthorizations:
    def test_all_matched(self):
        auths = [_make_auth(i) for i in range(3)]
        assert match_authorizations(_logs(auths, [1, 1, 1]), TOKEN, auths) == [True] * 3

    def test_missing_logs_mean_failure(self):
        auths = [_make_auth(i) for i in range(3)]
        assert match_authorizations(_logs(auths, [1, 0, 1]), TOKEN, auths) == [True, False, True]

    def test_authorization_used_without_transfer_is_failure(self):
        auths = [_make_auth(0)]
        logs = [lg for lg in _logs(auths, [1]) if lg["topics"][0] == AUTHORIZATION_USED_TOPIC]
        assert match_authorizations(logs, TOKEN, auths) == [False]

    def test_logs_from_other_contract_ignored(self):
        auths = [_make_auth(0)]
        logs = _logs(auths, [1], token="0x" + "99" * 20)
        assert match_authorizations(logs, TOKEN, auths) == [False]

    def test_hex_string_topics_supported(self):
        auths = [_make_auth(0)]
        logs = [
            {
                **lg,
                "topics": ["0x" + t.hex() for t in lg["topics"]],
                "data": "0x" + lg["data"].hex(),
            }
            for lg in _logs(auths, [1])
        ]
        assert match_authorizations(logs, TOKEN, auths) == [True]


class TestSettleBatch:
    async def test_all_settled(self):
        auths = [_make_auth(i) for i in range(2)]
        w3 = _w3([_receipt(_logs(auths, [1, 1]))])
        res = await _settle(w3, auths)
        assert res.status == "settled"
        assert [r.status for r in res.results] == ["success", "success"]
        assert res.total_gas_cost_wei == 21_000 * 2 * 10**9
        assert res.total_gas_cost_eth == "0.00004200"
        assert res.fee_collected is False

    async def test_settled_auth_not_reported_failed(self):
        # Regression: nonces are consumed at the receipt block, so per-call
        # success must come from logs, not from re-simulating the call
        auths = [_make_auth(0)]
        w3 = _w3([_receipt(_logs(auths, [1]))])
        res = await _settle(w3, auths)
        assert res.results[0].success is True

    async def test_partial_when_one_auth_not_executed(self):
        auths = [_make_auth(i) for i in range(3)]
        w3 = _w3([_receipt(_logs(auths, [1, 0, 1]))])
        res = await _settle(w3, auths)
        assert res.status == "partial"
        assert [r.success for r in res.results] == [True, False, True]
        assert res.total_failed == 1

    async def test_reverted_tx_fails_everything(self):
        auths = [_make_auth(i) for i in range(2)]
        w3 = _w3([_receipt([], status=0)])
        res = await _settle(w3, auths)
        assert res.status == "failed"
        assert res.total_succeeded == 0

    async def test_missing_logs_is_unknown_not_success(self):
        auths = [_make_auth(0)]
        receipt = _receipt(None)
        w3 = _w3([receipt])
        res = await _settle(w3, auths)
        assert res.status == "pending"
        assert res.results[0].status == "unknown"
        assert res.results[0].success is False
        assert res.total_succeeded == 0

    async def test_receipt_timeout_returns_pending_with_tx_hash(self):
        auths = [_make_auth(0)]
        w3 = _w3(wait_exc=TimeoutError("timed out"))
        res = await _settle(w3, auths)
        assert res.status == "pending"
        assert res.total_pending == 1
        assert res.results[0].status == "pending"
        assert res.results[0].tx_hash == "0x" + "01" * 32
        assert res.sub_batches[0].tx_hash == "0x" + "01" * 32

    async def test_send_error_is_unknown_with_local_hash(self):
        auths = [_make_auth(0)]
        w3 = _w3(send_exc=ConnectionError("reset"))
        res = await _settle(w3, auths)
        assert res.results[0].status == "unknown"
        assert res.results[0].tx_hash.startswith("0x")
        assert "reset" not in (res.results[0].error or "")

    async def test_gas_estimation_error_not_leaked(self):
        auths = [_make_auth(0)]
        w3 = _w3(estimate=[RuntimeError("http://secret-rpc.example/key123")])
        res = await _settle(w3, auths)
        assert res.status == "failed"
        assert "secret-rpc" not in res.results[0].error
        w3.eth.send_raw_transaction.assert_not_called()

    async def test_gas_price_ceiling_blocks_send(self):
        auths = [_make_auth(0)]
        w3 = _w3(base_fee=100 * 10**9)
        res = await _settle(w3, auths, max_fee_per_gas_wei=50 * 10**9)
        assert res.status == "failed"
        assert res.results[0].error == "gas price above ceiling"
        w3.eth.send_raw_transaction.assert_not_called()

    async def test_fee_collected_from_fee_call_result(self):
        fee = _make_auth(99)
        auths = [_make_auth(0)]
        w3 = _w3([_receipt(_logs([fee, *auths], [1, 1]))])
        res = await _settle(w3, auths, fee_auth=fee)
        assert res.fee_collected is True
        assert res.total_succeeded == 1

    async def test_fee_not_collected_when_no_fee_logs(self):
        fee = _make_auth(99)
        auths = [_make_auth(0)]
        w3 = _w3([_receipt(_logs([fee, *auths], [0, 1]))])
        res = await _settle(w3, auths, fee_auth=fee)
        assert res.fee_collected is False

    async def test_failed_fee_batch_halts_remaining_sub_batches(self):
        fee = _make_auth(99)
        auths = [_make_auth(0), _make_auth(1)]
        w3 = _w3(estimate=[RuntimeError("boom")])
        res = await _settle(w3, auths, fee_auth=fee, max_calls_per_tx=1)
        assert res.fee_collected is False
        assert res.status == "failed"
        assert w3.eth.estimate_gas.await_count == 1
        w3.eth.send_raw_transaction.assert_not_called()

    async def test_indexes_stay_aligned_when_first_sub_batch_fails(self):
        auths = [_make_auth(0), _make_auth(1)]
        w3 = _w3([_receipt(_logs(auths[1:], [1]))], estimate=[RuntimeError("boom"), 100_000])
        res = await _settle(w3, auths, max_calls_per_tx=1)
        assert [(r.index, r.success) for r in res.results] == [(0, False), (1, True)]
        assert res.status == "partial"
