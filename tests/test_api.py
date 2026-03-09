"""API endpoint tests for settle402."""

import pytest
from fastapi.testclient import TestClient

from settler.main import app


@pytest.fixture
def client():
    app.state.api_keys = []
    return TestClient(app)


class TestHealth:
    def test_health_returns_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestAuth:
    def test_no_keys_allows_access(self, client):
        app.state.api_keys = []
        resp = client.post(
            "/settle",
            json={
                "network": "base",
                "chainId": 8453,
                "tokenContract": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "authorizations": [],
            },
        )
        # Will fail with 503 (not configured) but NOT 401
        assert resp.status_code != 401

    def test_wrong_key_rejected(self, client):
        app.state.api_keys = ["correct-key"]
        resp = client.post(
            "/settle",
            json={
                "network": "base",
                "chainId": 8453,
                "tokenContract": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "authorizations": [],
            },
            headers={"x-settler-token": "wrong-key"},
        )
        assert resp.status_code == 401

    def test_correct_key_accepted(self, client):
        app.state.api_keys = ["correct-key"]
        resp = client.post(
            "/settle",
            json={
                "network": "base",
                "chainId": 8453,
                "tokenContract": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "authorizations": [],
            },
            headers={"x-settler-token": "correct-key"},
        )
        # Will fail with 503 (not configured) but NOT 401
        assert resp.status_code != 401

    def test_rate_limit_enforced(self, client):
        from settler.auth import _rate_buckets

        app.state.api_keys = ["rate-key"]
        app.state.rate_limit = 2  # 2 per minute for test

        # Clear any previous state
        _rate_buckets.clear()

        payload = {
            "network": "base",
            "chainId": 8453,
            "tokenContract": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "authorizations": [],
        }
        headers = {"x-settler-token": "rate-key"}

        # First 2 should pass (503 = not configured, but not 429)
        for _ in range(2):
            resp = client.post("/settle", json=payload, headers=headers)
            assert resp.status_code != 429

        # Third should be rate limited
        resp = client.post("/settle", json=payload, headers=headers)
        assert resp.status_code == 429

        _rate_buckets.clear()


class TestSettleValidation:
    def test_empty_authorizations_rejected(self, client):
        # Patch internals to avoid 503
        import settler.main as m

        m._config = type("C", (), {"chain_id": 8453})()
        m._w3 = True
        m._account = True

        resp = client.post(
            "/settle",
            json={
                "network": "base",
                "chainId": 8453,
                "tokenContract": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "authorizations": [],
            },
        )
        assert resp.status_code == 400

        # Cleanup
        m._config = None
        m._w3 = None
        m._account = None

    def test_batch_over_1000_rejected(self, client):
        import settler.main as m

        m._config = type("C", (), {"chain_id": 8453})()
        m._w3 = True
        m._account = True

        auth = {
            "authorization": {
                "from": "0x" + "11" * 20,
                "to": "0x" + "22" * 20,
                "value": 1000,
                "validAfter": 0,
                "validBefore": 0,
                "nonce": 0,
            },
            "signature": "0x" + "ab" * 32 + "cd" * 32 + "1b",
            "payer": "0x" + "11" * 20,
            "amount": 1000,
        }
        resp = client.post(
            "/settle",
            json={
                "network": "base",
                "chainId": 8453,
                "tokenContract": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "authorizations": [auth] * 1001,
            },
        )
        assert resp.status_code == 400
        assert "1,000" in resp.json()["detail"]

        m._config = None
        m._w3 = None
        m._account = None

    def test_chain_mismatch_rejected(self, client):
        import settler.main as m

        m._config = type("C", (), {"chain_id": 8453})()
        m._w3 = True
        m._account = True

        resp = client.post(
            "/settle",
            json={
                "network": "base-sepolia",
                "chainId": 84532,
                "tokenContract": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
                "authorizations": [
                    {
                        "authorization": {
                            "from": "0x" + "11" * 20,
                            "to": "0x" + "22" * 20,
                            "value": 1000,
                            "validAfter": 0,
                            "validBefore": 0,
                            "nonce": 0,
                        },
                        "signature": "0x" + "ab" * 32 + "cd" * 32 + "1b",
                        "payer": "0x" + "11" * 20,
                        "amount": 1000,
                    }
                ],
            },
        )
        assert resp.status_code == 400

        m._config = None
        m._w3 = None
        m._account = None
