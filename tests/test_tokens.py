"""What a token verification failure is allowed to say.

The desktop client acts on the status: a 401 during its silent session restore
is a verdict on the credential and ends the session, so this service must only
answer 401 when it has actually judged the token. Anything that stops it from
judging - notably not being able to reach the JWKS endpoint - is a 503.

No database or Redis: these call `decode_supabase_token` directly.
"""

import base64
import json
import threading
import time

import jwt
import pytest
from fastapi import HTTPException

from src.auth import tokens
from src.config import settings

_HMAC_SECRET = "a-test-secret-long-enough-for-sha256-hmac"
_KID = "0b7d2a1e-0000-4000-8000-000000000000"


def _unsigned(alg: str, claims: dict | None = None) -> str:
    """A token with a real header and a nonsense signature.

    Enough for the paths under test: key selection reads only the unverified
    header, and every case here fails before the signature is checked.
    """

    def seg(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    header = {"alg": alg, "typ": "JWT", "kid": _KID}
    return f"{seg(header)}.{seg(claims or {'sub': 'u'})}.c2ln"


@pytest.fixture
def asymmetric_project(monkeypatch):
    """A project on asymmetric signing keys, as every new Supabase project is."""
    monkeypatch.setattr(settings, "supabase_url", "https://example.supabase.co")
    # The client is process-wide and keyed on nothing, so a real one built by an
    # earlier test would outlive its settings.
    tokens._jwks_client.cache_clear()
    tokens._reset_refresh_cooldown()
    yield
    tokens._reset_refresh_cooldown()


class _Key:
    def __init__(self, kid: str):
        self.key_id = kid
        self.key = f"key-for-{kid}"


class _Client:
    """Stands in for PyJWKClient: raises `error`, or serves keys for `kids`."""

    def __init__(self, error: Exception | None = None, kids: tuple[str, ...] = ()):
        self._error = error
        self._kids = kids
        self.refreshes = 0

    def get_signing_keys(self, refresh: bool = False):
        if refresh:
            self.refreshes += 1
        if self._error is not None:
            raise self._error
        return [_Key(k) for k in self._kids]


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
    monkeypatch.setattr(tokens, "_jwks_client", lambda: _Client(kids=("some-other-kid",)))
    with pytest.raises(HTTPException) as caught:
        tokens.decode_supabase_token(_unsigned("ES256"))
    assert caught.value.status_code == 401


def test_an_empty_key_set_is_401(asymmetric_project, monkeypatch):
    monkeypatch.setattr(tokens, "_jwks_client", lambda: _Client(jwt.PyJWKClientError("no signing keys")))
    with pytest.raises(HTTPException) as caught:
        tokens.decode_supabase_token(_unsigned("ES256"))
    assert caught.value.status_code == 401


def test_unknown_kids_force_at_most_one_refresh_per_cooldown(asymmetric_project, monkeypatch):
    """A stream of random `kid`s must not turn into a stream of JWKS fetches."""
    client = _Client(kids=("some-other-kid",))
    monkeypatch.setattr(tokens, "_jwks_client", lambda: client)
    for _ in range(5):
        with pytest.raises(HTTPException) as caught:
            tokens.decode_supabase_token(_unsigned("ES256"))
        assert caught.value.status_code == 401
    assert client.refreshes == 1

    # Once the window has passed, a rotated key can be picked up again.
    monkeypatch.setattr(
        tokens,
        "_last_forced_refresh",
        tokens._last_forced_refresh - tokens._JWKS_REFRESH_COOLDOWN_SECONDS - 1,
    )
    with pytest.raises(HTTPException):
        tokens.decode_supabase_token(_unsigned("ES256"))
    assert client.refreshes == 2


def test_a_known_kid_needs_no_refresh(asymmetric_project, monkeypatch):
    client = _Client(kids=(_KID,))
    monkeypatch.setattr(tokens, "_jwks_client", lambda: client)
    assert tokens._asymmetric_key(_unsigned("ES256")) == f"key-for-{_KID}"
    assert client.refreshes == 0


async def test_the_jwks_lookup_runs_off_the_event_loop(asymmetric_project, monkeypatch):
    """`verify_supabase_token` hands the blocking lookup to the threadpool."""
    seen: list[threading.Thread] = []

    def lookup(token: str):
        seen.append(threading.current_thread())
        raise tokens._unauthorized()

    monkeypatch.setattr(tokens, "_asymmetric_key", lookup)
    with pytest.raises(HTTPException):
        await tokens.verify_supabase_token(_unsigned("ES256"))
    assert seen and seen[0] is not threading.main_thread()


def test_an_algorithm_we_do_not_accept_is_401(asymmetric_project):
    with pytest.raises(HTTPException) as caught:
        tokens.decode_supabase_token(_unsigned("none"))
    assert caught.value.status_code == 401


def test_hs256_without_a_secret_is_401_not_500(monkeypatch):
    monkeypatch.setattr(settings, "supabase_jwt_secret", "")
    with pytest.raises(HTTPException) as caught:
        tokens.decode_supabase_token(_unsigned("HS256"))
    assert caught.value.status_code == 401


def _hs256(claims: dict) -> str:
    now = int(time.time())
    base = {"sub": "u", "aud": "authenticated", "iat": now, "exp": now + 3600}
    return jwt.encode({**base, **claims}, _HMAC_SECRET, algorithm="HS256")


def test_the_issuer_must_be_this_project(monkeypatch):
    """A token from another project that shares the HS256 secret is refused, and
    so is one with no issuer at all."""
    monkeypatch.setattr(settings, "supabase_jwt_secret", _HMAC_SECRET)
    monkeypatch.setattr(settings, "supabase_jwt_audience", "authenticated")
    monkeypatch.setattr(settings, "supabase_url", "https://example.supabase.co")

    ok = _hs256({"iss": "https://example.supabase.co/auth/v1"})
    assert tokens.decode_supabase_token(ok)["sub"] == "u"

    for bad in (_hs256({"iss": "https://other.supabase.co/auth/v1"}), _hs256({})):
        with pytest.raises(HTTPException) as caught:
            tokens.decode_supabase_token(bad)
        assert caught.value.status_code == 401


def test_a_token_minted_a_moment_ahead_of_our_clock_is_accepted(monkeypatch):
    """Session restore presents a token that is milliseconds old, so a second of
    drift between Supabase and this host must not read as a bad credential."""
    monkeypatch.setattr(settings, "supabase_url", "")
    monkeypatch.setattr(settings, "supabase_jwt_secret", _HMAC_SECRET)
    monkeypatch.setattr(settings, "supabase_jwt_audience", "authenticated")
    now = int(time.time())
    token = jwt.encode(
        {"sub": "u", "aud": "authenticated", "iat": now + 5, "exp": now + 3600},
        _HMAC_SECRET,
        algorithm="HS256",
    )
    assert tokens.decode_supabase_token(token)["sub"] == "u"
