"""Auth & secret-encryption primitives for multi-user mode.

Three responsibilities, all keyed off the single ``SECRET_KEY`` env var:

- Password hashing (PBKDF2-HMAC-SHA256, from the stdlib ``hashlib`` — no extra
  native deps, works everywhere). Stored as ``pbkdf2_sha256$iterations$salt$hash``.
- JWT access tokens (HS256) so the frontend can authenticate every request.
- Fernet symmetric encryption for users' Binance/AI API keys at rest. The
  Fernet key is DERIVED from ``SECRET_KEY`` so we never persist the key itself.

Why fail-safe on a missing SECRET_KEY: we refuse to store a stranger's exchange
secret we cannot encrypt. ``secrets_enabled()`` reports whether key storage is
available; the API disables the key-entry endpoints when it is False.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import secrets as _secrets
from typing import Any, Optional

from cryptography.fernet import Fernet, InvalidToken

# ---- password hashing (stdlib PBKDF2) --------------------------------

_PBKDF2_ROUNDS = 240_000


def hash_password(password: str) -> str:
    if not password or len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    salt = _secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    return "pbkdf2_sha256${}${}${}".format(
        _PBKDF2_ROUNDS,
        base64.b64encode(salt).decode(),
        base64.b64encode(dk).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, rounds_s, salt_b64, hash_b64 = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        rounds = int(rounds_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except (ValueError, TypeError):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, rounds)
    return hmac.compare_digest(dk, expected)


# A throwaway hash of a value no one logs in with, computed once at import. The
# login path verifies against THIS when no account matches, so "no such user" and
# "user exists, wrong password" take the same PBKDF2 time — closing the timing
# side-channel an attacker could otherwise use to enumerate valid accounts.
_DUMMY_HASH = hash_password("timing-equalizer-not-a-real-account-secret")


def dummy_verify(password: str) -> None:
    """Burn the same work :func:`verify_password` does, then discard it.

    Call on the no-such-user branch of login so both branches take ~equal time.
    """
    verify_password(password, _DUMMY_HASH)


# ---- JWT (HS256, minimal, no external dep) ---------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def create_access_token(secret_key: str, *, user_id: int, email: str,
                        role: str, ttl_minutes: int) -> str:
    if not secret_key:
        raise ValueError("SECRET_KEY is required to issue tokens")
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "sub": str(user_id),
        "email": email,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + dt.timedelta(minutes=ttl_minutes)).timestamp()),
    }
    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode())
    )
    sig = hmac.new(secret_key.encode(), signing_input.encode(), hashlib.sha256).digest()
    return signing_input + "." + _b64url(sig)


def decode_access_token(secret_key: str, token: str) -> Optional[dict[str, Any]]:
    """Return the payload if the token is valid and unexpired, else None."""
    if not secret_key or not token:
        return None
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
    except ValueError:
        return None
    signing_input = f"{header_b64}.{payload_b64}"
    expected = hmac.new(
        secret_key.encode(), signing_input.encode(), hashlib.sha256
    ).digest()
    try:
        given = _b64url_decode(sig_b64)
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(expected, given):
        return None
    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except (ValueError, TypeError):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        return None
    if dt.datetime.now(dt.timezone.utc).timestamp() >= exp:
        return None
    return payload


# ---- secret encryption (Fernet, key derived from SECRET_KEY) ---------


def secrets_enabled(secret_key: str) -> bool:
    """True when we can encrypt/decrypt user API keys (SECRET_KEY set)."""
    return bool(secret_key)


def _fernet(secret_key: str) -> Fernet:
    if not secret_key:
        raise RuntimeError("SECRET_KEY is not set; cannot encrypt/decrypt secrets")
    # Derive a stable 32-byte Fernet key from SECRET_KEY.
    digest = hashlib.sha256(secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(secret_key: str, plaintext: str) -> str:
    return _fernet(secret_key).encrypt(plaintext.encode()).decode()


def decrypt_secret(secret_key: str, ciphertext: str) -> Optional[str]:
    if not ciphertext:
        return None
    try:
        return _fernet(secret_key).decrypt(ciphertext.encode()).decode()
    except (InvalidToken, RuntimeError, ValueError):
        return None


def new_webhook_token() -> str:
    """Unguessable per-user TradingView webhook token."""
    return _secrets.token_urlsafe(24)


def new_license_key() -> str:
    """A fresh, unguessable licence key, shown to the admin exactly once.

    Only its :func:`hash_license_key` digest is persisted — never the plaintext.
    """
    return "TT-" + _secrets.token_urlsafe(24)


def hash_license_key(key: str) -> str:
    """Stable SHA-256 of a licence key for at-rest storage and lookup.

    We store/compare the hash so a database leak can't expose usable keys.
    """
    return hashlib.sha256(key.strip().encode()).hexdigest()
