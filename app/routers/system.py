"""Bootstrap, session info, and the update controls."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from .. import config, db, deps, updater
from .auth import public_user

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/bootstrap")
def bootstrap(conn: sqlite3.Connection = Depends(db.get_db)) -> dict[str, Any]:
    """Public. Tells the login screen whether this is a fresh install."""
    user_count = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    return {
        "app": "Bill Splitter",
        "version": config.VERSION,
        "needs_setup": user_count == 0,
        "registration_open": db.get_setting(conn, "registration_open", "1") == "1",
    }


@router.get("/me")
def me(
    user: Any | None = Depends(deps.optional_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    if user is None:
        return {"user": None}
    settings = db.get_settings(conn)
    pending = 0
    update_available = False
    if user["is_admin"]:
        pending = conn.execute("SELECT COUNT(*) AS n FROM users WHERE status='pending'").fetchone()["n"]
        latest = settings.get("latest_known_commit", "")
        current = updater.local_commit()
        update_available = bool(latest and current and latest != current)
    return {
        "user": public_user(user),
        "defaults": {
            "currency": settings.get("default_currency", "USD"),
            "tax_percent": settings.get("default_tax_percent", "0"),
            "tip_percent": settings.get("default_tip_percent", "18"),
            "tip_base": settings.get("tip_base", "pre_tax"),
        },
        "version": config.VERSION,
        "pending_approvals": pending,
        "update_available": update_available,
    }


@router.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "version": config.VERSION}


# --- updates -----------------------------------------------------------------

@router.get("/system/update")
def update_status(
    _: Any = Depends(deps.admin_user), conn: sqlite3.Connection = Depends(db.get_db)
) -> dict[str, Any]:
    return {"status": updater.status(conn), "history": updater.history(conn)}


@router.post("/system/update/check")
def update_check(
    actor: Any = Depends(deps.admin_user), conn: sqlite3.Connection = Depends(db.get_db)
) -> dict[str, Any]:
    try:
        info = updater.do_check(conn)
    except updater.UpdateError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    db.audit(conn, actor, "update.checked", detail=info.get("latest_commit_short", ""))
    return {"status": updater.status(conn), "check": info, "history": updater.history(conn)}


class ApplyIn(BaseModel):
    force: bool = False


@router.post("/system/update/apply")
def update_apply(
    payload: ApplyIn,
    actor: Any = Depends(deps.admin_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    try:
        result = updater.apply_update(
            conn, trigger="manual", actor=actor["username"], force=payload.force
        )
    except updater.UpdateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return {"result": result, "history": updater.history(conn)}


class TokenIn(BaseModel):
    token: str = Field(default="", max_length=200)


@router.post("/system/update/token")
def set_update_token(
    payload: TokenIn,
    actor: Any = Depends(deps.admin_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    """Store (or clear, with an empty string) a GitHub token for a private repo."""
    db.set_setting(conn, "update_token", payload.token.strip())
    db.audit(
        conn, actor, "update.token_set" if payload.token.strip() else "update.token_cleared"
    )
    return {"has_token": bool(payload.token.strip())}
