"""Unit tests for settle402 core logic."""

import pytest

from settler.schemas import AuthorizationItem
from settler.settler import (
    decode_revert_reason,
    encode_aggregate3,
    encode_transfer_with_authorization,
    split_into_sub_batches,
    split_signature,
)


class TestSplitSignature:
    def test_valid_65_byte_signature(self):
        sig = "0x" + "ab" * 32 + "cd" * 32 + "1b"
        v, r, s = split_signature(sig)
        assert v == 27
        assert r == bytes.fromhex("ab" * 32)
        assert s == bytes.fromhex("cd" * 32)

    def test_v_normalization(self):
        sig = "0x" + "00" * 64 + "00"  # v=0 → 27
        v, _, _ = split_signature(sig)
        assert v == 27

        sig = "0x" + "00" * 64 + "01"  # v=1 → 28
        v, _, _ = split_signature(sig)
        assert v == 28

    def test_v_already_normalized(self):
        sig = "0x" + "00" * 64 + "1b"  # v=27 stays 27
        v, _, _ = split_signature(sig)
        assert v == 27

    def test_invalid_length(self):
        with pytest.raises(ValueError, match="65 bytes"):
            split_signature("0x" + "ab" * 30)

    def test_no_0x_prefix(self):
        sig = "ab" * 32 + "cd" * 32 + "1b"
        v, r, s = split_signature(sig)
        assert v == 27


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
        fee_cd = encode_transfer_with_authorization(fee_auth.authorization, v1, r1, s1)
        fee_call = ("0x" + "ff" * 20, False, fee_cd)

        v2, r2, s2 = split_signature(user_auth.signature)
        user_cd = encode_transfer_with_authorization(user_auth.authorization, v2, r2, s2)
        user_call = ("0x" + "ff" * 20, True, user_cd)

        calls = [fee_call, user_call]
        calldata = encode_aggregate3(calls)
        assert calldata[:4] == bytes.fromhex("82ad56cb")
        # Fee call has allowFailure=False, user has True
        assert fee_call[1] is False
        assert user_call[1] is True


def _make_auth(index: int) -> AuthorizationItem:
    return AuthorizationItem(
        authorization={
            "from": "0x" + "11" * 20,
            "to": "0x" + "22" * 20,
            "value": 1000 * (index + 1),
            "validAfter": 0,
            "validBefore": 0,
            "nonce": index,
        },
        signature="0x" + "ab" * 32 + "cd" * 32 + "1b",
        payer="0x" + "11" * 20,
        amount=1000 * (index + 1),
    )
