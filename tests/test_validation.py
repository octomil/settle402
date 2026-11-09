"""Tests for typed schemas and up-front authorization validation."""

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data
from pydantic import ValidationError

from settler.schemas import AuthorizationItem
from settler.validation import _TWA_TYPES, validate_batch

TOKEN = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
DOMAIN = ("USD Coin", "2")
NOW = 1_800_000_000
PAYER = Account.from_key("0x" + "cd" * 32)
PAYEE = "0x" + "22" * 20


def _item(nonce=1, signer=PAYER, **over):
    auth = {
        "from": PAYER.address,
        "to": PAYEE,
        "value": 1000,
        "validAfter": 0,
        "validBefore": NOW + 3600,
        "nonce": "0x" + nonce.to_bytes(32, "big").hex(),
    }
    auth.update(over)
    signable = encode_typed_data(
        domain_data={
            "name": DOMAIN[0],
            "version": DOMAIN[1],
            "chainId": 8453,
            "verifyingContract": TOKEN,
        },
        message_types=_TWA_TYPES,
        message_data={**auth, "nonce": bytes.fromhex(auth["nonce"][2:])},
    )
    sig = "0x" + bytes(signer.sign_message(signable).signature).hex()
    return AuthorizationItem(
        authorization=auth, signature=sig, payer=auth["from"], amount=auth["value"]
    )


def _validate(*items, **over):
    kw = dict(chain_id=8453, token_contract=TOKEN, now=NOW, min_value=1, domain=DOMAIN)
    kw.update(over)
    validate_batch([(f"authorizations[{i}]", it) for i, it in enumerate(items)], **kw)


class TestSchemas:
    def test_payer_must_match_from(self):
        item = _item()
        with pytest.raises(ValidationError, match="payer"):
            AuthorizationItem(
                authorization=item.authorization.as_call_dict(),
                signature=item.signature,
                payer="0x" + "99" * 20,
                amount=1000,
            )

    def test_amount_must_match_value(self):
        item = _item()
        with pytest.raises(ValidationError, match="amount"):
            AuthorizationItem(
                authorization=item.authorization.as_call_dict(),
                signature=item.signature,
                payer=item.payer,
                amount=1,
            )

    @pytest.mark.parametrize(
        "over",
        [
            {"to": "0x1234"},
            {"value": True},
            {"value": -1},
            {"value": 1.5},
            {"validBefore": 2**256},
            {"nonce": "0x" + "aa" * 31},
            {"nonce": "zz" * 32},
        ],
    )
    def test_malformed_fields_rejected(self, over):
        base = _item().authorization.as_call_dict()
        base.update(over)
        with pytest.raises(ValidationError):
            AuthorizationItem(
                authorization=base, signature="0x" + "ab" * 65, payer=PAYER.address, amount=1000
            )

    def test_signature_must_be_65_bytes(self):
        item = _item()
        with pytest.raises(ValidationError):
            AuthorizationItem(
                authorization=item.authorization.as_call_dict(),
                signature="0x" + "ab" * 64,
                payer=item.payer,
                amount=1000,
            )


class TestValidateBatch:
    def test_valid_signed_auth_passes(self):
        _validate(_item())

    def test_wrong_signer_rejected(self):
        other = Account.from_key("0x" + "ee" * 32)
        with pytest.raises(ValueError, match=r"authorizations\[0\].*signature does not match"):
            _validate(_item(signer=other))

    def test_wrong_domain_rejected(self):
        with pytest.raises(ValueError, match="signature does not match"):
            _validate(_item(), domain=("Other", "1"))

    def test_recovery_skipped_without_domain(self):
        other = Account.from_key("0x" + "ee" * 32)
        _validate(_item(signer=other), domain=None)

    def test_expired(self):
        with pytest.raises(ValueError, match="expired"):
            _validate(_item(validBefore=NOW + 10))

    def test_not_yet_valid(self):
        with pytest.raises(ValueError, match="not yet valid"):
            _validate(_item(validAfter=NOW + 5))

    def test_below_min_value(self):
        with pytest.raises(ValueError, match="below minimum"):
            _validate(_item(value=999), min_value=1000)

    @pytest.mark.parametrize(
        "to", ["0x" + "00" * 20, TOKEN, "0xcA11bde05977b3631167028862bE2a173976CA11"]
    )
    def test_bad_recipient(self, to):
        with pytest.raises(ValueError, match="recipient"):
            _validate(_item(to=to))

    def test_payee_allowlist(self):
        _validate(_item(), payee_allowlist=[PAYEE])
        with pytest.raises(ValueError, match="allowed recipient"):
            _validate(_item(), payee_allowlist=["0x" + "33" * 20])

    def test_duplicate_nonce_rejected(self):
        with pytest.raises(ValueError, match="duplicate"):
            _validate(_item(nonce=1), _item(nonce=1))

    def test_error_names_the_offending_index(self):
        with pytest.raises(ValueError, match=r"authorizations\[1\]"):
            _validate(_item(nonce=1), _item(nonce=2, value=0))
