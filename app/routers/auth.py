"""Sign-up, sign-in, sessions.

The very first account created on a fresh install becomes the admin and is
active immediately. Every account after that lands in `pending` and cannot log
in until an admin approves it.
"""

from __future__ import annotations

import sqlite3
import time
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from .. import config, db, deps, security

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Crude but effective brute-force brake: per (ip, username) attempts in a window.
_ATTEMPT_WINDOW = 300      # seconds
_ATTEMPT_LIMIT = 10
_attempts: dict[tuple[str, str], list[float]] = defaultdict(list)


def _rate_limited(ip: str, username: str) -> bool:
    key = (ip, username.lower())
    now = time.time()
    recent = [t for t in _attempts[key] if now - t < _ATTEMPT_WINDOW]
    _attempts[key] = recent
    return len(recent) >= _ATTEMPT_LIMIT


def _record_attempt(ip: str, username: str) -> None:
    _attempts[(ip, username.lower())].append(time.time())


def _clear_attempts(ip: str, username: str) -> None:
    _attempts.pop((ip, username.lower()), None)


class RegisterIn(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    password: str = Field(min_length=1, max_length=256)
    display_name: str = Field(default="", max_length=64)
    email: str = Field(default="", max_length=200)


class LoginIn(BaseModel):
    username: str
    password: str


class ChangePasswordIn(BaseModel):
    current_password: str = ""
    new_password: str = Field(min_length=1, max_length=256)


def public_user(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"] or row["username"],
        "email": row["email"],
        "status": row["status"],
        "is_admin": bool(row["is_admin"]),
        "must_change_password": bool(row["must_change_password"]),
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
    }


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        config.SESSION_COOKIE,
        token,
        max_age=config.SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=config.COOKIE_SECURE,
        path="/",
    )


@router.post("/register")
def register(
    payload: RegisterIn,
    request: Request,
    response: Response,
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    username = payload.username.strip()
    if err := security.validate_username(username):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, err)
    if err := security.validate_password(payload.password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, err)

    user_count = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    is_first = user_count == 0

    if not is_first and db.get_setting(conn, "registration_open", "1") != "1":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "New sign-ups are closed. Ask an admin to create your account.",
        )

    if conn.execute(
        "SELECT 1 FROM users WHERE username = ? COLLATE NOCASE", (username,)
    ).fetchone():
        raise HTTPException(status.HTTP_409_CONFLICT, "That username is already taken.")

    now = int(time.time())
    cur = conn.execute(
        """INSERT INTO users(username, display_name, email, password_hash, status,
                             is_admin, created_at, decided_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            username,
            payload.display_name.strip() or username,
            payload.email.strip(),
            security.hash_password(payload.password),
            "active" if is_first else "pending",
            1 if is_first else 0,
            now,
            now if is_first else None,
        ),
    )
    user_id = int(cur.lastrowid)

    if is_first:
        db.audit(conn, None, "install.first_admin", f"user:{user_id}", username)
        token = security.create_session(
            conn, user_id, config.SESSION_DAYS, request.headers.get("user-agent", "")
        )
        conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (now, user_id))
        _set_session_cookie(response, token)
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return {"status": "active", "is_first_admin": True, "user": public_user(row)}

    db.audit(conn, None, "user.registered", f"user:{user_id}", username)
    return {
        "status": "pending",
        "is_first_admin": False,
        "message": "Account requested. An admin needs to approve it before you can sign in.",
    }


@router.post("/login")
def login(
    payload: LoginIn,
    request: Request,
    response: Response,
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    ip = request.client.host if request.client else "?"
    username = payload.username.strip()

    if _rate_limited(ip, username):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many failed attempts. Wait a few minutes and try again.",
        )

    row = conn.execute(
        "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
    ).fetchone()

    # Same generic error whether the user exists or not, and always spend the
    # time on a hash so timing does not leak which usernames are real.
    dummy = "scrypt$16384$8$1$AAAAAAAAAAAAAAAAAAAAAA$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    ok = security.verify_password(payload.password, row["password_hash"] if row else dummy)

    if row is None or not ok:
        _record_attempt(ip, username)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect username or password.")

    if row["status"] == "pending":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Your account is still waiting for admin approval.",
        )
    if row["status"] == "rejected":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Your account request was declined.")
    if row["status"] == "suspended":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "This account is suspended. Contact an admin."
        )

    _clear_attempts(ip, username)
    security.purge_expired_sessions(conn)
    token = security.create_session(
        conn, row["id"], config.SESSION_DAYS, request.headers.get("user-agent", "")
    )
    conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (int(time.time()), row["id"]))
    _set_session_cookie(response, token)

    row = conn.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone()
    return {"user": public_user(row)}


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, bool]:
    token = request.cookies.get(config.SESSION_COOKIE, "")
    if token:
        security.destroy_session(conn, token)
    response.delete_cookie(config.SESSION_COOKIE, path="/")
    return {"ok": True}


@router.post("/logout-everywhere")
def logout_everywhere(
    response: Response,
    user: Any = Depends(deps.current_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, bool]:
    security.destroy_user_sessions(conn, user["id"])
    response.delete_cookie(config.SESSION_COOKIE, path="/")
    return {"ok": True}


@router.post("/change-password")
def change_password(
    payload: ChangePasswordIn,
    request: Request,
    response: Response,
    user: Any = Depends(deps.current_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, bool]:
    if err := security.validate_password(payload.new_password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, err)
    if not security.verify_password(payload.current_password, user["password_hash"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Current password is incorrect.")

    conn.execute(
        "UPDATE users SET password_hash=?, must_change_password=0 WHERE id=?",
        (security.hash_password(payload.new_password), user["id"]),
    )
    # Drop every other session, then re-issue one for this browser.
    security.destroy_user_sessions(conn, user["id"])
    token = security.create_session(
        conn, user["id"], config.SESSION_DAYS, request.headers.get("user-agent", "")
    )
    _set_session_cookie(response, token)
    db.audit(conn, user, "user.password_changed", f"user:{user['id']}")
    return {"ok": True}


class ProfileIn(BaseModel):
    display_name: str = Field(default="", max_length=64)
    email: str = Field(default="", max_length=200)


@router.post("/profile")
def update_profile(
    payload: ProfileIn,
    user: Any = Depends(deps.active_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    conn.execute(
        "UPDATE users SET display_name=?, email=? WHERE id=?",
        (payload.display_name.strip() or user["username"], payload.email.strip(), user["id"]),
    )
    row = conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
    return {"user": public_user(row)}
