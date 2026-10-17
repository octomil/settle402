"""Unit tests for API key auth and rate limiting."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from settler import auth
from settler.auth import hash_key, verify_admin, verify_api_key


def _req(token=None, keys=(), admin_token="", header="x-settler-token"):
    headers = {header: token} if token is not None else {}
    state = SimpleNamespace(api_keys=list(keys), rate_limit=100, admin_token=admin_token)
    return SimpleNamespace(headers=headers, app=SimpleNamespace(state=state))


@pytest.fixture(autouse=True)
def _clear_buckets():
    auth._rate_buckets.clear()
    yield
    auth._rate_buckets.clear()


class TestVerifyApiKey:
    def test_fails_closed_without_keys(self):
        with pytest.raises(HTTPException) as e:
            verify_api_key(_req("anything"))
        assert e.value.status_code == 503

    def test_valid_key_accepted(self):
        verify_api_key(_req("good", keys=[hash_key("good")]))

    def test_raw_stored_key_not_accepted(self):
        with pytest.raises(HTTPException) as e:
            verify_api_key(_req("good", keys=["good"]))
        assert e.value.status_code == 401

    def test_missing_token_rejected(self):
        with pytest.raises(HTTPException) as e:
            verify_api_key(_req(None, keys=[hash_key("good")]))
        assert e.value.status_code == 401

    def test_buckets_keyed_by_digest_not_token(self):
        verify_api_key(_req("good", keys=[hash_key("good")]))
        assert "good" not in auth._rate_buckets
        assert hash_key("good") in auth._rate_buckets


class TestVerifyAdmin:
    def test_disabled_without_token(self):
        with pytest.raises(HTTPException) as e:
            verify_admin(_req("x", header="x-admin-token"))
        assert e.value.status_code == 503

    def test_wrong_token(self):
        with pytest.raises(HTTPException) as e:
            verify_admin(_req("bad", admin_token="adm", header="x-admin-token"))
        assert e.value.status_code == 401

    def test_right_token(self):
        verify_admin(_req("adm", admin_token="adm", header="x-admin-token"))


class TestRateLimit:
    def test_limit_enforced(self):
        assert auth._check_rate_limit("k", 2)
        assert auth._check_rate_limit("k", 2)
        assert not auth._check_rate_limit("k", 2)

    def test_stale_buckets_evicted(self, monkeypatch):
        monkeypatch.setattr(auth, "_SWEEP_THRESHOLD", 5)
        for i in range(10):
            auth._rate_buckets[f"old{i}"] = [-1e9]
        assert auth._check_rate_limit("fresh", 1)
        assert set(auth._rate_buckets) == {"fresh"}
