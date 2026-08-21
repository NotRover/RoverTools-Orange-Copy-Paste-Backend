"""What a token verification failure is allowed to say.

The desktop client acts on the status: a 401 during its silent session restore
is a verdict on the credential and ends the session, so this service must only
answer 401 when it has actually judged the token. Anything that stops it from
judging - notably not being able to reach the JWKS endpoint - is a 503.

No database or Redis: these call `decode_supabase_token` directly.
"""

import base64
import json
import time

import jwt
import pytest
from fastapi import HTTPException

from src.auth import tokens
from src.config import settings

_HMAC_SECRET = "a-test-secret-long-enough-for-sha256-hmac"


def _unsigned(alg: str, claims: dict | None = None) -> str:
    """A token with a real header and a nonsense signature.

    Enough for the paths under test: key selection reads only the unverified
    header, and every case here fails before the signature is checked.
    """

    def seg(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    header = {"alg": alg, "typ": "JWT", "kid": "0b7d2a1e-0000-4000-8000-000000000000"}
    return f"{seg(header)}.{seg(claims or {'sub': 'u'})}.c2ln"


@pytest.fixture
def asymmetric_project(monkeypatch):
    """A project on asymmetric signing keys, as every new Supabase project is."""
    monkeypatch.setattr(settings, "supabase_url", "https://example.supabase.co")
    # The client is process-wide and keyed on nothing, so a real one built by an
    # earlier test would outlive its settings.
    tokens._jwks_client.cache_clear()
    return None


class _Client:
    def __init__(self, error: Exception):
        self._error = error

    def get_signing_key_from_jwt(self, token: str):
        raise self._error


def test_unreachable_jwks_is_503_not_401(asymmetric_project, monkeypatch):
    """The one that signs users out. Nothing was judged, so say nothing about it."""
    monkeypatch.setattr(
        tokens,
        "_jwks_client",
        lambda: _Client(jwt.PyJWKClientConnectionError("dns")),
    )
    with pytest.raises(HTTPException) as caught:
        tokens.decode_supabase_token(_unsigned("ES256"))
    assert caught.value.status_code == 503
    assert (caught.value.headers or {}).get("Retry-After") == "2"


def test_unknown_kid_is_still_401(asymmetric_project, monkeypatch):
    """We reached the key set and this token is not in it. A forged `kid` must
    not be a way to drive 5xx out of the service."""
    monkeypatch.setattr(
        tokens,
        "_jwks_client",
        lambda: _Client(jwt.PyJWKClientError("no key for kid")),
    )
    with pytest.raises(HTTPException) as caught:
        tokens.decode_supabase_token(_unsigned("ES256"))
    assert caught.value.status_code == 401


def test_an_algorithm_we_do_not_accept_is_401(asymmetric_project):
    with pytest.raises(HTTPException) as caught:
        tokens.decode_supabase_token(_unsigned("none"))
    assert caught.value.status_code == 401


def test_a_token_minted_a_moment_ahead_of_our_clock_is_accepted(monkeypatch):
    """Session restore presents a token that is milliseconds old, so a second of
    drift between Supabase and this host must not read as a bad credential."""
    monkeypatch.setattr(settings, "supabase_jwt_secret", _HMAC_SECRET)
    monkeypatch.setattr(settings, "supabase_jwt_audience", "authenticated")
    now = int(time.time())
    token = jwt.encode(
        {"sub": "u", "aud": "authenticated", "iat": now + 5, "exp": now + 3600},
        _HMAC_SECRET,
        algorithm="HS256",
    )
    assert tokens.decode_supabase_token(token)["sub"] == "u"
