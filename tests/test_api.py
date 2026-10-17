"""API endpoint tests for settle402."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from eth_account import Account
from fastapi.testclient import TestClient

import settler.main as m
from settler import auth
from settler.auth import hash_key
from settler.config import SettlerConfig
from settler.main import app
from settler.schemas import AuthorizationResult, SettleBatchResponse
from tests.test_validation import PAYEE, TOKEN, _item

ACCOUNT = Account.from_key("0x" + "ab" * 32)
ADMIN = "admin-secret"


@pytest.fixture
def client(monkeypatch, tmp_path):
    auth._rate_buckets.clear()
    m._fee_nonces.clear()
    monkeypatch.setattr(m, "_status_cache", None)
    monkeypatch.setattr(m, "_KEYS_FILE", tmp_path / "keys.json")
    monkeypatch.setattr(m, "_stats", m.SettlerStats())
    app.state.api_keys = []
    app.state.rate_limit = 100
    app.state.admin_token = ADMIN
    yield TestClient(app)
    auth._rate_buckets.clear()
    m._fee_nonces.clear()


@pytest.fixture
def configured(monkeypatch):
    def apply(**over):
        cfg = SettlerConfig(chain_id=8453, **over)
        monkeypatch.setattr(m, "_config", cfg)
        monkeypatch.setattr(m, "_account", ACCOUNT)
        w3 = SimpleNamespace(eth=SimpleNamespace(get_balance=AsyncMock(return_value=10**18)))
        monkeypatch.setattr(m, "_w3", w3)
        return cfg, w3

    return apply


def _payload(auths, fee=None, **over):
    body = {
        "network": "base",
        "chainId": 8453,
        "tokenContract": TOKEN,
        "authorizations": [a.model_dump(by_alias=True) for a in auths],
    }
    if fee is not None:
        body["feeAuthorization"] = fee.model_dump(by_alias=True)
    body.update(over)
    return body


def _fee_item(nonce=900, **over):
    return _item(nonce=nonce, to=ACCOUNT.address, value=100_000, **over)


def _ok_response(n=1, **over):
    fields = dict(
        status="settled",
        total_submitted=n,
        total_succeeded=n,
        total_failed=0,
        total_gas_used=21_000,
        total_gas_cost_wei=42,
        total_gas_cost_eth="0.00000000",
        sub_batches=[],
        results=[
            AuthorizationResult(index=i, nonce=str(i), success=True, status="success")
            for i in range(n)
        ],
    )
    fields.update(over)
    return SettleBatchResponse(**fields)


class TestHealth:
    def test_health_returns_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_health_does_not_expose_settler_address(self, client, configured):
        configured()
        assert ACCOUNT.address not in client.get("/health").text


class TestAuth:
    def test_no_keys_fails_closed(self, client):
        resp = client.post("/settle", json=_payload([]))
        assert resp.status_code == 503

    def test_wrong_key_rejected(self, client):
        app.state.api_keys = [hash_key("correct-key")]
        resp = client.post("/settle", json=_payload([]), headers={"x-settler-token": "wrong-key"})
        assert resp.status_code == 401

    def test_correct_key_accepted(self, client):
        app.state.api_keys = [hash_key("correct-key")]
        resp = client.post("/settle", json=_payload([]), headers={"x-settler-token": "correct-key"})
        # 503 (not configured), but NOT 401
        assert resp.status_code != 401

    def test_rate_limit_enforced(self, client):
        app.state.api_keys = [hash_key("rate-key")]
        app.state.rate_limit = 2
        headers = {"x-settler-token": "rate-key"}

        for _ in range(2):
            resp = client.post("/settle", json=_payload([]), headers=headers)
            assert resp.status_code != 429

        resp = client.post("/settle", json=_payload([]), headers=headers)
        assert resp.status_code == 429


class TestKeys:
    def test_requires_admin_token(self, client):
        assert client.post("/keys").status_code == 401
        assert client.post("/keys", headers={"x-admin-token": "nope"}).status_code == 401

    def test_disabled_without_admin_token_configured(self, client):
        app.state.admin_token = ""
        assert client.post("/keys", headers={"x-admin-token": ""}).status_code == 503

    def test_creates_key_stored_hashed(self, client, configured):
        configured()
        resp = client.post("/keys", headers={"x-admin-token": ADMIN})
        assert resp.status_code == 200
        key = resp.json()["key"]
        assert app.state.api_keys == [hash_key(key)]
        on_disk = m._KEYS_FILE.read_text()
        assert key not in on_disk
        assert json.loads(on_disk) == [hash_key(key)]

    def test_new_key_authenticates(self, client, configured):
        configured()
        key = client.post("/keys", headers={"x-admin-token": ADMIN}).json()["key"]
        resp = client.post("/settle", json=_payload([]), headers={"x-settler-token": key})
        assert resp.status_code == 400  # past auth, rejected for empty batch

    def test_key_cap(self, client, configured):
        configured(max_keys=1)
        h = {"x-admin-token": ADMIN}
        assert client.post("/keys", headers=h).status_code == 200
        assert client.post("/keys", headers=h).status_code == 429

    def test_per_ip_limit(self, client, configured):
        configured()
        codes = [
            client.post("/keys", headers={"x-admin-token": "bad"}).status_code for _ in range(6)
        ]
        assert codes[:5] == [401] * 5
        assert codes[5] == 429

    def test_persistence_failure_surfaces_and_rolls_back(self, client, configured, monkeypatch):
        configured()

        def boom():
            raise OSError("disk full")

        monkeypatch.setattr(m, "_save_keys", boom)
        resp = client.post("/keys", headers={"x-admin-token": ADMIN})
        assert resp.status_code == 500
        assert app.state.api_keys == []

    def test_load_hashes_legacy_plaintext_entries(self, client):
        m._KEYS_FILE.write_text(json.dumps(["s402_legacy", hash_key("other")]))
        assert m._load_keys() == [hash_key("s402_legacy"), hash_key("other")]


class TestStatus:
    def test_requires_admin(self, client, configured):
        configured()
        assert client.get("/status").status_code == 401

    def test_balance_cached(self, client, configured):
        _, w3 = configured()
        h = {"x-admin-token": ADMIN}
        assert client.get("/status", headers=h).status_code == 200
        assert client.get("/status", headers=h).status_code == 200
        assert w3.eth.get_balance.await_count == 1

    def test_rpc_error_text_not_returned(self, client, configured):
        _, w3 = configured()
        w3.eth.get_balance.side_effect = RuntimeError("https://rpc.example/secret-key")
        resp = client.get("/status", headers={"x-admin-token": ADMIN})
        assert resp.status_code == 503
        assert "secret-key" not in resp.text


class TestStartup:
    def test_multiple_workers_refused(self, monkeypatch):
        monkeypatch.setenv("WEB_CONCURRENCY", "2")
        with pytest.raises(RuntimeError, match="single process"):
            asyncio.run(m.startup())


class TestFeeValidation:
    @pytest.fixture(autouse=True)
    def _setup(self, client, configured):
        app.state.api_keys = [hash_key("k")]
        self.h = {"x-settler-token": "k"}
        self.client = client
        self.configured = configured

    def _post(self, auths, fee=None):
        return self.client.post("/settle", json=_payload(auths, fee), headers=self.h)

    def test_missing_fee_auth_returns_402(self):
        self.configured(fee_enabled=True)
        assert self._post([_item()]).status_code == 402

    def test_wrong_recipient_returns_402(self):
        self.configured(fee_enabled=True)
        fee = _item(nonce=900, to="0x" + "99" * 20, value=100_000)
        assert self._post([_item()], fee).status_code == 402

    def test_insufficient_value_returns_402(self):
        self.configured(fee_enabled=True)
        fee = _item(nonce=900, to=ACCOUNT.address, value=99_999)
        assert self._post([_item()], fee).status_code == 402

    def test_fee_not_required_when_disabled(self, monkeypatch):
        self.configured(fee_enabled=False)
        monkeypatch.setattr(m, "settle_batch", AsyncMock(return_value=_ok_response()))
        assert self._post([_item()]).status_code == 200

    def test_valid_fee_auth_passes_and_is_forwarded(self, monkeypatch):
        self.configured(fee_enabled=True)
        sb = AsyncMock(return_value=_ok_response(fee_collected=True))
        monkeypatch.setattr(m, "settle_batch", sb)
        resp = self._post([_item()], _fee_item())
        assert resp.status_code == 200
        assert sb.await_args.kwargs["fee_auth"] is not None

    def test_fee_auth_not_reusable_while_collected(self, monkeypatch):
        self.configured(fee_enabled=True)
        monkeypatch.setattr(
            m, "settle_batch", AsyncMock(return_value=_ok_response(fee_collected=True))
        )
        assert self._post([_item(nonce=1)], _fee_item()).status_code == 200
        assert self._post([_item(nonce=2)], _fee_item()).status_code == 409

    def test_fee_auth_released_when_provably_not_collected(self, monkeypatch):
        self.configured(fee_enabled=True)
        monkeypatch.setattr(
            m, "settle_batch", AsyncMock(return_value=_ok_response(fee_collected=False))
        )
        assert self._post([_item(nonce=1)], _fee_item()).status_code == 200
        assert self._post([_item(nonce=2)], _fee_item()).status_code == 200

    def test_fee_auth_held_while_pending(self, monkeypatch):
        self.configured(fee_enabled=True)
        pending = _ok_response(status="pending", total_succeeded=0, total_pending=1, results=[])
        monkeypatch.setattr(m, "settle_batch", AsyncMock(return_value=pending))
        assert self._post([_item(nonce=1)], _fee_item()).status_code == 200
        assert self._post([_item(nonce=2)], _fee_item()).status_code == 409


class TestSettleValidation:
    @pytest.fixture(autouse=True)
    def _setup(self, client, configured):
        app.state.api_keys = [hash_key("k")]
        self.h = {"x-settler-token": "k"}
        self.client = client
        self.cfg, self.w3 = configured()

    def _post(self, body):
        return self.client.post("/settle", json=body, headers=self.h)

    def test_empty_authorizations_rejected(self):
        assert self._post(_payload([])).status_code == 400

    def test_batch_over_1000_rejected(self):
        resp = self._post(_payload([_item()] * 1001))
        assert resp.status_code == 400
        assert "1,000" in resp.json()["detail"]

    def test_chain_mismatch_rejected(self):
        resp = self._post(_payload([_item()], chainId=84532))
        assert resp.status_code == 400

    def test_token_not_in_allowlist_rejected(self):
        resp = self._post(_payload([_item()], tokenContract="0x" + "99" * 20))
        assert resp.status_code == 400
        assert "not allowed" in resp.json()["detail"]

    def test_extra_token_allowlisted_via_config(self, monkeypatch):
        extra = "0x" + "99" * 20
        self.cfg.token_allowlist = [extra]
        monkeypatch.setattr(m, "settle_batch", AsyncMock(return_value=_ok_response()))
        resp = self._post(_payload([_item()], tokenContract=extra))
        assert resp.status_code == 200  # signer recovery skipped for non-default tokens

    def test_default_token_used_when_omitted(self, monkeypatch):
        sb = AsyncMock(return_value=_ok_response())
        monkeypatch.setattr(m, "settle_batch", sb)
        body = _payload([_item()])
        del body["tokenContract"]
        assert self._post(body).status_code == 200
        assert sb.await_args.kwargs["token_contract"] == TOKEN

    def test_malformed_auth_rejected_before_any_send(self, monkeypatch):
        sb = AsyncMock()
        monkeypatch.setattr(m, "settle_batch", sb)
        body = _payload([_item(nonce=1), _item(nonce=2)])
        body["authorizations"][1]["authorization"]["to"] = "not-an-address"
        assert self._post(body).status_code == 422
        sb.assert_not_called()

    def test_payer_mismatch_rejected_with_422(self):
        body = _payload([_item()])
        body["authorizations"][0]["payer"] = "0x" + "99" * 20
        assert self._post(body).status_code == 422

    def test_bad_signature_in_late_item_rejects_whole_batch(self, monkeypatch):
        sb = AsyncMock()
        monkeypatch.setattr(m, "settle_batch", sb)
        stranger = Account.from_key("0x" + "ee" * 32)
        resp = self._post(_payload([_item(nonce=1), _item(nonce=2, signer=stranger)]))
        assert resp.status_code == 400
        assert "authorizations[1]" in resp.json()["detail"]
        sb.assert_not_called()

    def test_expired_auth_rejected(self):
        resp = self._post(_payload([_item(validBefore=1)]))
        assert resp.status_code == 400

    def test_payee_allowlist_enforced(self):
        self.cfg.payee_allowlist = ["0x" + "33" * 20]
        assert self._post(_payload([_item()])).status_code == 400
        self.cfg.payee_allowlist = [PAYEE]

    def test_rpc_error_text_not_returned(self):
        self.w3.eth.get_balance.side_effect = RuntimeError("https://rpc.example/secret-key")
        resp = self._post(_payload([_item()]))
        assert resp.status_code == 503
        assert "secret-key" not in resp.text

    def test_stats_use_signed_value_by_result_index(self, monkeypatch):
        items = [_item(nonce=1, value=1000), _item(nonce=2, value=5000)]
        results = [
            AuthorizationResult(index=0, nonce="1", success=False, status="failed"),
            AuthorizationResult(index=1, nonce="2", success=True, status="success"),
        ]
        resp = _ok_response(2, status="partial", total_succeeded=1, total_failed=1, results=results)
        monkeypatch.setattr(m, "settle_batch", AsyncMock(return_value=resp))
        assert self._post(_payload(items)).status_code == 200
        assert m._stats.total_value_settled == 5000
        assert m._stats.total_gas_spent_wei == 42
