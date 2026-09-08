"""FastAPI dependencies: who is calling, and are they allowed to."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import Depends, HTTPException, Request, status

from . import config, db, security


def optional_user(request: Request, conn: sqlite3.Connection = Depends(db.get_db)) -> Any | None:
    token = request.cookies.get(config.SESSION_COOKIE, "")
    row = security.lookup_session(conn, token)
    if row is None:
        return None
    if row["status"] != "active":
        # Suspended or deleted mid-session: kill the session rather than serve it.
        security.destroy_user_sessions(conn, row["id"])
        return None
    return row


def current_user(user: Any | None = Depends(optional_user)) -> Any:
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in to continue.")
    return user


def active_user(user: Any = Depends(current_user)) -> Any:
    """A user who must not be forced through a password change first.

    Everything except /api/me and the change-password endpoint uses this, so a
    reset account cannot poke at data until it picks a new password.
    """
    if user["must_change_password"]:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You must set a new password before continuing.",
        )
    return user


def admin_user(user: Any = Depends(active_user)) -> Any:
    if not user["is_admin"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admins only.")
    return user


def require_group_access(
    conn: sqlite3.Connection, group_id: int, user: Any, *, owner_only: bool = False
) -> sqlite3.Row:
    """Group must exist and the user must be a member (admins can see everything)."""
    group = conn.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
    if group is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Group not found.")

    membership = conn.execute(
        "SELECT role FROM group_members WHERE group_id=? AND user_id=?", (group_id, user["id"])
    ).fetchone()

    if membership is None and not user["is_admin"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You are not in this group.")
    if owner_only:
        is_owner = membership is not None and membership["role"] == "owner"
        if not is_owner and not user["is_admin"]:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "Only the group owner or an admin can do that."
            )
    return group
