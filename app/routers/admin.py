"""Admin control panel: approve sign-ups, reset or delete accounts, settings.

Deleting a user does not shred history. Their share of past bills is rewritten
onto a guest named "<name> (removed)" in each group they took part in, so old
totals and balances stay correct - which matters, because those numbers are
what people actually owe each other.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from .. import db, deps, security
from .auth import public_user

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _admin_count(conn: sqlite3.Connection, excluding: int | None = None) -> int:
    sql = "SELECT COUNT(*) AS n FROM users WHERE is_admin=1 AND status='active'"
    params: tuple[Any, ...] = ()
    if excluding is not None:
        sql += " AND id != ?"
        params = (excluding,)
    return conn.execute(sql, params).fetchone()["n"]


def _get_user(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.")
    return row


@router.get("/users")
def list_users(
    _: Any = Depends(deps.admin_user), conn: sqlite3.Connection = Depends(db.get_db)
) -> dict[str, Any]:
    rows = conn.execute(
        """SELECT u.*, d.username AS decided_by_name,
                  (SELECT COUNT(*) FROM sessions s WHERE s.user_id=u.id AND s.expires_at > ?) AS live_sessions,
                  (SELECT COUNT(*) FROM group_members gm WHERE gm.user_id=u.id) AS group_count,
                  (SELECT COUNT(*) FROM bills b WHERE b.created_by=u.id) AS bills_created
             FROM users u LEFT JOIN users d ON d.id = u.decided_by
            ORDER BY CASE u.status WHEN 'pending' THEN 0 ELSE 1 END,
                     u.created_at DESC""",
        (int(time.time()),),
    ).fetchall()

    users = []
    for row in rows:
        item = public_user(row)
        item.update(
            {
                "note": row["note"],
                "decided_at": row["decided_at"],
                "decided_by_name": row["decided_by_name"],
                "live_sessions": row["live_sessions"],
                "group_count": row["group_count"],
                "bills_created": row["bills_created"],
            }
        )
        users.append(item)

    return {
        "users": users,
        "pending_count": sum(1 for u in users if u["status"] == "pending"),
        "admin_count": _admin_count(conn),
    }


class DecisionIn(BaseModel):
    note: str = Field(default="", max_length=500)
    make_admin: bool = False


@router.post("/users/{user_id}/approve")
def approve_user(
    user_id: int,
    payload: DecisionIn,
    actor: Any = Depends(deps.admin_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    target = _get_user(conn, user_id)
    if target["status"] == "active":
        raise HTTPException(status.HTTP_409_CONFLICT, "That account is already active.")
    conn.execute(
        """UPDATE users SET status='active', is_admin=?, note=?, decided_at=?, decided_by=?
            WHERE id=?""",
        (1 if payload.make_admin else target["is_admin"], payload.note,
         int(time.time()), actor["id"], user_id),
    )
    db.audit(conn, actor, "user.approved", f"user:{user_id}", target["username"])
    return {"user": public_user(_get_user(conn, user_id))}


@router.post("/users/{user_id}/reject")
def reject_user(
    user_id: int,
    payload: DecisionIn,
    actor: Any = Depends(deps.admin_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    target = _get_user(conn, user_id)
    if target["id"] == actor["id"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot reject your own account.")
    conn.execute(
        "UPDATE users SET status='rejected', note=?, decided_at=?, decided_by=? WHERE id=?",
        (payload.note, int(time.time()), actor["id"], user_id),
    )
    security.destroy_user_sessions(conn, user_id)
    db.audit(conn, actor, "user.rejected", f"user:{user_id}", target["username"])
    return {"user": public_user(_get_user(conn, user_id))}


@router.post("/users/{user_id}/suspend")
def suspend_user(
    user_id: int,
    payload: DecisionIn,
    actor: Any = Depends(deps.admin_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    target = _get_user(conn, user_id)
    if target["id"] == actor["id"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot suspend yourself.")
    if target["is_admin"] and _admin_count(conn, excluding=user_id) == 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "That is the only active admin - promote someone else first.",
        )
    conn.execute(
        "UPDATE users SET status='suspended', note=?, decided_at=?, decided_by=? WHERE id=?",
        (payload.note, int(time.time()), actor["id"], user_id),
    )
    security.destroy_user_sessions(conn, user_id)
    db.audit(conn, actor, "user.suspended", f"user:{user_id}", target["username"])
    return {"user": public_user(_get_user(conn, user_id))}


@router.post("/users/{user_id}/reactivate")
def reactivate_user(
    user_id: int,
    actor: Any = Depends(deps.admin_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    target = _get_user(conn, user_id)
    conn.execute(
        "UPDATE users SET status='active', decided_at=?, decided_by=? WHERE id=?",
        (int(time.time()), actor["id"], user_id),
    )
    db.audit(conn, actor, "user.reactivated", f"user:{user_id}", target["username"])
    return {"user": public_user(_get_user(conn, user_id))}


class AdminFlagIn(BaseModel):
    is_admin: bool


@router.post("/users/{user_id}/admin")
def set_admin(
    user_id: int,
    payload: AdminFlagIn,
    actor: Any = Depends(deps.admin_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    target = _get_user(conn, user_id)
    if not payload.is_admin and _admin_count(conn, excluding=user_id) == 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "You would be removing the last admin. Promote someone else first.",
        )
    conn.execute("UPDATE users SET is_admin=? WHERE id=?", (1 if payload.is_admin else 0, user_id))
    db.audit(
        conn, actor,
        "user.promoted" if payload.is_admin else "user.demoted",
        f"user:{user_id}", target["username"],
    )
    return {"user": public_user(_get_user(conn, user_id))}


@router.post("/users/{user_id}/reset-password")
def reset_password(
    user_id: int,
    actor: Any = Depends(deps.admin_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    """Issue a one-time password. Shown once, in the admin's browser only -
    it is never stored in plaintext, so it cannot be retrieved again."""
    target = _get_user(conn, user_id)
    temp = security.generate_temp_password()
    conn.execute(
        "UPDATE users SET password_hash=?, must_change_password=1 WHERE id=?",
        (security.hash_password(temp), user_id),
    )
    security.destroy_user_sessions(conn, user_id)
    db.audit(conn, actor, "user.password_reset", f"user:{user_id}", target["username"])
    return {
        "username": target["username"],
        "temporary_password": temp,
        "message": "Give this to them in person or over a private channel. "
                   "They will be asked to choose a new password at sign-in.",
    }


class CreateUserIn(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    display_name: str = Field(default="", max_length=64)
    email: str = Field(default="", max_length=200)
    is_admin: bool = False


@router.post("/users")
def create_user(
    payload: CreateUserIn,
    actor: Any = Depends(deps.admin_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    """Create an already-approved account with a temporary password."""
    username = payload.username.strip()
    if err := security.validate_username(username):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, err)
    if conn.execute("SELECT 1 FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone():
        raise HTTPException(status.HTTP_409_CONFLICT, "That username is already taken.")

    temp = security.generate_temp_password()
    now = int(time.time())
    cur = conn.execute(
        """INSERT INTO users(username, display_name, email, password_hash, status,
                             is_admin, must_change_password, created_at, decided_at, decided_by)
           VALUES (?,?,?,?, 'active', ?, 1, ?, ?, ?)""",
        (username, payload.display_name.strip() or username, payload.email.strip(),
         security.hash_password(temp), 1 if payload.is_admin else 0, now, now, actor["id"]),
    )
    user_id = int(cur.lastrowid)
    db.audit(conn, actor, "user.created", f"user:{user_id}", username)
    return {
        "user": public_user(_get_user(conn, user_id)),
        "temporary_password": temp,
    }


def _groups_touched_by(conn: sqlite3.Connection, user_id: int) -> set[int]:
    party = f"u:{user_id}"
    ids: set[int] = {
        r["group_id"] for r in conn.execute(
            "SELECT group_id FROM group_members WHERE user_id=?", (user_id,)
        )
    }
    ids |= {
        r["group_id"] for r in conn.execute(
            """SELECT DISTINCT b.group_id FROM bills b
                 JOIN bill_participants p ON p.bill_id=b.id WHERE p.party=?""", (party,)
        )
    }
    ids |= {
        r["group_id"] for r in conn.execute(
            """SELECT DISTINCT b.group_id FROM bills b
                 JOIN bill_payments pay ON pay.bill_id=b.id WHERE pay.party=?""", (party,)
        )
    }
    ids |= {
        r["group_id"] for r in conn.execute(
            "SELECT DISTINCT group_id FROM settlements WHERE from_party=? OR to_party=?",
            (party, party),
        )
    }
    return ids


def _convert_user_to_guests(conn: sqlite3.Connection, user_id: int, display_name: str) -> int:
    """Rewrite every 'u:<id>' reference to a per-group guest so history survives."""
    party = f"u:{user_id}"
    now = int(time.time())
    converted = 0

    for group_id in sorted(_groups_touched_by(conn, user_id)):
        cur = conn.execute(
            "INSERT INTO group_guests(group_id, name, created_at) VALUES (?,?,?)",
            (group_id, f"{display_name} (removed)", now),
        )
        guest_party = f"g:{int(cur.lastrowid)}"

        conn.execute(
            """UPDATE bill_participants SET party=?
                WHERE party=? AND bill_id IN (SELECT id FROM bills WHERE group_id=?)""",
            (guest_party, party, group_id),
        )
        conn.execute(
            """UPDATE bill_payments SET party=?
                WHERE party=? AND bill_id IN (SELECT id FROM bills WHERE group_id=?)""",
            (guest_party, party, group_id),
        )
        conn.execute(
            """UPDATE bill_item_shares SET party=?
                WHERE party=? AND item_id IN (
                    SELECT i.id FROM bill_items i JOIN bills b ON b.id=i.bill_id
                     WHERE b.group_id=?)""",
            (guest_party, party, group_id),
        )
        conn.execute(
            "UPDATE settlements SET from_party=? WHERE from_party=? AND group_id=?",
            (guest_party, party, group_id),
        )
        conn.execute(
            "UPDATE settlements SET to_party=? WHERE to_party=? AND group_id=?",
            (guest_party, party, group_id),
        )
        converted += 1

    return converted


@router.delete("/users/{user_id}")
def delete_user(
    user_id: int,
    actor: Any = Depends(deps.admin_user),
    conn: sqlite3.Connection = Depends(db.get_db),
    keep_history: bool = Query(True, description="Preserve their share of past bills as a guest"),
) -> dict[str, Any]:
    target = _get_user(conn, user_id)
    if target["id"] == actor["id"]:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "You cannot delete your own account. Ask another admin to do it.",
        )
    if target["is_admin"] and _admin_count(conn, excluding=user_id) == 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "That is the only active admin - promote someone else first.",
        )

    name = target["display_name"] or target["username"]
    db.backup_database(label="pre-user-delete")

    groups_touched = 0
    if keep_history:
        groups_touched = _convert_user_to_guests(conn, user_id, name)

    # Cascades take out sessions and group memberships; bills they created keep
    # existing with created_by set to NULL.
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    db.audit(
        conn, actor, "user.deleted", f"user:{user_id}",
        f"{target['username']} (history kept in {groups_touched} group(s))"
        if keep_history else f"{target['username']} (history discarded)",
    )
    return {
        "ok": True,
        "deleted": target["username"],
        "history_preserved_in_groups": groups_touched,
    }


# --- settings ----------------------------------------------------------------

EDITABLE_SETTINGS = {
    "default_currency",
    "default_tax_percent",
    "default_tip_percent",
    "tip_base",
    "registration_open",
    "update_repo",
    "update_branch",
    "auto_update_enabled",
    "update_check_interval_minutes",
}


@router.get("/settings")
def get_settings(
    _: Any = Depends(deps.admin_user), conn: sqlite3.Connection = Depends(db.get_db)
) -> dict[str, Any]:
    settings = db.get_settings(conn)
    # The GitHub token is write-only: report whether one is set, never its value.
    settings["has_update_token"] = "1" if settings.pop("update_token", "") else "0"
    return {"settings": settings}


@router.post("/settings")
def update_settings(
    payload: dict[str, Any],
    actor: Any = Depends(deps.admin_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    unknown = set(payload) - EDITABLE_SETTINGS
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Not editable: {', '.join(sorted(unknown))}"
        )

    for key, value in payload.items():
        if key in {"registration_open", "auto_update_enabled"}:
            value = "1" if str(value).lower() in {"1", "true", "yes", "on"} else "0"
        elif key == "tip_base":
            if value not in {"pre_tax", "post_tax"}:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "tip_base must be pre_tax or post_tax.")
        elif key == "update_check_interval_minutes":
            try:
                value = str(max(5, min(1440, int(value))))
            except (TypeError, ValueError):
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Interval must be a number of minutes.")
        elif key in {"default_tax_percent", "default_tip_percent"}:
            try:
                value = str(max(0.0, min(100.0, float(value))))
            except (TypeError, ValueError):
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{key} must be a percentage.")
        elif key == "update_repo":
            value = str(value).strip()
            if value and not _looks_like_repo(value):
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "Repo must look like owner/name or a github.com URL.",
                )
            value = _normalise_repo(value)
        db.set_setting(conn, key, str(value))

    db.audit(conn, actor, "settings.updated", detail=", ".join(sorted(payload)))
    return {"settings": db.get_settings(conn)}


def _looks_like_repo(value: str) -> bool:
    slug = _normalise_repo(value)
    parts = slug.split("/")
    return len(parts) == 2 and all(p and "/" not in p and " " not in p for p in parts)


def _normalise_repo(value: str) -> str:
    """Accept a full URL or owner/name and store owner/name."""
    v = value.strip().rstrip("/")
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:", "github.com/"):
        if v.lower().startswith(prefix.lower()):
            v = v[len(prefix):]
            break
    if v.endswith(".git"):
        v = v[:-4]
    return v


# --- audit -------------------------------------------------------------------

@router.get("/audit")
def audit_log(
    _: Any = Depends(deps.admin_user),
    conn: sqlite3.Connection = Depends(db.get_db),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT * FROM audit_log ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()
    return {"entries": [dict(r) for r in rows]}


@router.post("/backup")
def make_backup(
    actor: Any = Depends(deps.admin_user), conn: sqlite3.Connection = Depends(db.get_db)
) -> dict[str, Any]:
    path = db.backup_database(label="manual")
    db.audit(conn, actor, "backup.created", detail=path.name if path else "no database yet")
    return {"ok": bool(path), "file": path.name if path else None}
