"""Passwords and sessions - stdlib only, no bcrypt/argon2 wheel to compile.

Password hashes use scrypt (RFC 7914) via hashlib, stored as
    scrypt$n$r$p$<salt_b64>$<hash_b64>
so cost parameters travel with the hash and can be raised later without
invalidating existing hashes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from typing import Any

# ~64 MiB, ~0.1s on a Pi 4. Raise N (power of two) as hardware improves.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_MAXMEM = 132 * 1024 * 1024

USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,31}$")
MIN_PASSWORD_LEN = 8

_B64 = base64.urlsafe_b64encode
_UNB64 = base64.urlsafe_b64decode


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
        maxmem=SCRYPT_MAXMEM,
    )
    return "scrypt${}${}${}${}${}".format(
        SCRYPT_N, SCRYPT_R, SCRYPT_P,
        _B64(salt).decode(), _B64(dk).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = _UNB64(hash_b64.encode())
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_UNB64(salt_b64.encode()),
            n=int(n), r=int(r), p=int(p),
            dklen=len(expected),
            maxmem=SCRYPT_MAXMEM,
        )
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(expected, actual)


def validate_username(username: str) -> str | None:
    """Returns an error message, or None when valid."""
    if not USERNAME_RE.match(username or ""):
        return (
            "Username must be 2-32 characters: letters, numbers, dot, dash or "
            "underscore, starting with a letter or number."
        )
    return None


def validate_password(password: str) -> str | None:
    if len(password or "") < MIN_PASSWORD_LEN:
        return f"Password must be at least {MIN_PASSWORD_LEN} characters."
    return None


# Ambiguity-free alphabet: no 0/O/1/l/I, since admins read these out loud.
_TEMP_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"


def generate_temp_password(length: int = 12) -> str:
    return "".join(secrets.choice(_TEMP_ALPHABET) for _ in range(length))


# --- sessions ----------------------------------------------------------------

def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def create_session(conn: sqlite3.Connection, user_id: int, days: int, user_agent: str = "") -> str:
    token = new_session_token()
    now = int(time.time())
    conn.execute(
        "INSERT INTO sessions(token, user_id, created_at, expires_at, user_agent) VALUES (?,?,?,?,?)",
        (token, user_id, now, now + days * 86400, user_agent[:200]),
    )
    return token


def lookup_session(conn: sqlite3.Connection, token: str) -> Any | None:
    if not token:
        return None
    row = conn.execute(
        """SELECT u.*, s.token AS session_token, s.expires_at
             FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token = ? AND s.expires_at > ?""",
        (token, int(time.time())),
    ).fetchone()
    return row


def destroy_session(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token=?", (token,))


def destroy_user_sessions(conn: sqlite3.Connection, user_id: int) -> None:
    """Used when an admin resets a password, suspends or deletes an account -
    a revoked account must not keep a live session."""
    conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))


def purge_expired_sessions(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (int(time.time()),))
