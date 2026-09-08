"""Groups: a trip, a household, a dinner club. Bills live inside a group.

A group holds members (app users) and guests (people with no account, so you
can still split with someone's partner or a friend who never signed up).
"""

from __future__ import annotations

import csv
import io
import sqlite3
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from .. import db, deps, ledger, splitter

router = APIRouter(prefix="/api", tags=["groups"])


class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    currency: str = Field(default="USD", max_length=8)
    member_ids: list[int] = Field(default_factory=list)
    guest_names: list[str] = Field(default_factory=list)


class GroupPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    currency: str | None = Field(default=None, max_length=8)
    archived: bool | None = None


@router.get("/directory")
def directory(
    user: Any = Depends(deps.active_user), conn: sqlite3.Connection = Depends(db.get_db)
) -> dict[str, Any]:
    """Active users, for picking group members."""
    rows = conn.execute(
        """SELECT id, username, display_name FROM users
            WHERE status='active'
            ORDER BY LOWER(COALESCE(NULLIF(display_name,''), username))"""
    ).fetchall()
    return {
        "users": [
            {
                "id": r["id"],
                "username": r["username"],
                "name": r["display_name"] or r["username"],
                "is_me": r["id"] == user["id"],
            }
            for r in rows
        ]
    }


@router.get("/categories")
def categories(
    _: Any = Depends(deps.active_user), conn: sqlite3.Connection = Depends(db.get_db)
) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT * FROM categories WHERE archived=0 ORDER BY sort_order, LOWER(name)"
    ).fetchall()
    return {"categories": [dict(r) for r in rows]}


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    icon: str = Field(default="other", max_length=30)


@router.post("/categories")
def create_category(
    payload: CategoryIn,
    user: Any = Depends(deps.active_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    name = payload.name.strip()
    existing = conn.execute(
        "SELECT * FROM categories WHERE name=? COLLATE NOCASE", (name,)
    ).fetchone()
    if existing:
        if existing["archived"]:
            conn.execute("UPDATE categories SET archived=0 WHERE id=?", (existing["id"],))
            return {"category": dict(conn.execute(
                "SELECT * FROM categories WHERE id=?", (existing["id"],)).fetchone())}
        raise HTTPException(status.HTTP_409_CONFLICT, "That category already exists.")
    cur = conn.execute(
        "INSERT INTO categories(name, icon, sort_order) VALUES (?,?,500)", (name, payload.icon)
    )
    db.audit(conn, user, "category.created", f"category:{cur.lastrowid}", name)
    row = conn.execute("SELECT * FROM categories WHERE id=?", (cur.lastrowid,)).fetchone()
    return {"category": dict(row)}


@router.delete("/categories/{category_id}")
def archive_category(
    category_id: int,
    user: Any = Depends(deps.admin_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, bool]:
    """Archive rather than delete - existing bills keep their label."""
    conn.execute("UPDATE categories SET archived=1 WHERE id=?", (category_id,))
    db.audit(conn, user, "category.archived", f"category:{category_id}")
    return {"ok": True}


# --- groups ------------------------------------------------------------------

@router.get("/groups")
def list_groups(
    user: Any = Depends(deps.active_user),
    conn: sqlite3.Connection = Depends(db.get_db),
    include_archived: bool = Query(False),
) -> dict[str, Any]:
    sql = """SELECT g.*, gm.role AS my_role,
                    (SELECT COUNT(*) FROM group_members m WHERE m.group_id=g.id) AS member_count,
                    (SELECT COUNT(*) FROM group_guests gu WHERE gu.group_id=g.id) AS guest_count,
                    (SELECT COUNT(*) FROM bills b WHERE b.group_id=g.id) AS bill_count,
                    (SELECT MAX(b.bill_date) FROM bills b WHERE b.group_id=g.id) AS last_bill_date
               FROM groups g JOIN group_members gm ON gm.group_id=g.id AND gm.user_id=?
              WHERE (? OR g.archived=0)
              ORDER BY g.archived, COALESCE(last_bill_date,'') DESC, g.created_at DESC"""
    rows = conn.execute(sql, (user["id"], 1 if include_archived else 0)).fetchall()

    groups = []
    my_party = f"u:{user['id']}"
    for row in rows:
        item = dict(row)
        led = ledger.group_ledger(conn, row["id"])
        item["total_spend_cents"] = led["total_spend_cents"]
        item["outstanding_cents"] = led["outstanding_cents"]
        item["my_balance_cents"] = next(
            (b["balance_cents"] for b in led["balances"] if b["party"] == my_party), 0
        )
        groups.append(item)

    return {"groups": groups}


@router.post("/groups")
def create_group(
    payload: GroupIn,
    user: Any = Depends(deps.active_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    now = int(time.time())
    cur = conn.execute(
        "INSERT INTO groups(name, description, currency, created_by, created_at) VALUES (?,?,?,?,?)",
        (payload.name.strip(), payload.description.strip(),
         payload.currency.strip().upper() or "USD", user["id"], now),
    )
    group_id = int(cur.lastrowid)

    conn.execute(
        "INSERT INTO group_members(group_id, user_id, role, joined_at) VALUES (?,?,'owner',?)",
        (group_id, user["id"], now),
    )
    for member_id in dict.fromkeys(payload.member_ids):
        if member_id == user["id"]:
            continue
        exists = conn.execute(
            "SELECT 1 FROM users WHERE id=? AND status='active'", (member_id,)
        ).fetchone()
        if exists:
            conn.execute(
                "INSERT OR IGNORE INTO group_members(group_id, user_id, role, joined_at) "
                "VALUES (?,?,'member',?)",
                (group_id, member_id, now),
            )
    for name in payload.guest_names:
        if name.strip():
            conn.execute(
                "INSERT INTO group_guests(group_id, name, created_at) VALUES (?,?,?)",
                (group_id, name.strip()[:80], now),
            )

    db.audit(conn, user, "group.created", f"group:{group_id}", payload.name)
    return get_group(group_id, user, conn)


@router.get("/groups/{group_id}")
def get_group(
    group_id: int,
    user: Any = Depends(deps.active_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    group = deps.require_group_access(conn, group_id, user)
    led = ledger.group_ledger(conn, group_id)
    membership = conn.execute(
        "SELECT role FROM group_members WHERE group_id=? AND user_id=?", (group_id, user["id"])
    ).fetchone()
    return {
        "group": dict(group),
        "my_role": membership["role"] if membership else ("admin" if user["is_admin"] else None),
        "my_party": f"u:{user['id']}",
        **led,
    }


@router.patch("/groups/{group_id}")
def update_group(
    group_id: int,
    payload: GroupPatch,
    user: Any = Depends(deps.active_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    deps.require_group_access(conn, group_id, user, owner_only=True)
    fields = payload.model_dump(exclude_none=True)
    if not fields:
        return get_group(group_id, user, conn)
    sets, values = [], []
    for key, value in fields.items():
        sets.append(f"{key}=?")
        if key == "archived":
            values.append(1 if value else 0)
        elif key == "currency":
            values.append(str(value).strip().upper() or "USD")
        else:
            values.append(str(value).strip())
    values.append(group_id)
    conn.execute(f"UPDATE groups SET {', '.join(sets)} WHERE id=?", values)
    db.audit(conn, user, "group.updated", f"group:{group_id}", ", ".join(fields))
    return get_group(group_id, user, conn)


@router.delete("/groups/{group_id}")
def delete_group(
    group_id: int,
    user: Any = Depends(deps.active_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    group = deps.require_group_access(conn, group_id, user, owner_only=True)
    bills = conn.execute(
        "SELECT COUNT(*) AS n FROM bills WHERE group_id=?", (group_id,)
    ).fetchone()["n"]
    db.backup_database(label="pre-group-delete")
    conn.execute("DELETE FROM groups WHERE id=?", (group_id,))
    db.audit(conn, user, "group.deleted", f"group:{group_id}", f"{group['name']} ({bills} bills)")
    return {"ok": True, "deleted_bills": bills}


# --- members and guests ------------------------------------------------------

class MemberIn(BaseModel):
    user_id: int


@router.post("/groups/{group_id}/members")
def add_member(
    group_id: int,
    payload: MemberIn,
    user: Any = Depends(deps.active_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    deps.require_group_access(conn, group_id, user)
    target = conn.execute(
        "SELECT * FROM users WHERE id=? AND status='active'", (payload.user_id,)
    ).fetchone()
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No active user with that id.")
    conn.execute(
        "INSERT OR IGNORE INTO group_members(group_id, user_id, role, joined_at) VALUES (?,?,'member',?)",
        (group_id, payload.user_id, int(time.time())),
    )
    db.audit(conn, user, "group.member_added", f"group:{group_id}", target["username"])
    return get_group(group_id, user, conn)


@router.delete("/groups/{group_id}/members/{member_id}")
def remove_member(
    group_id: int,
    member_id: int,
    user: Any = Depends(deps.active_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    """Removing a member is blocked while they still appear on bills - otherwise
    the group's balances would silently stop adding up."""
    if member_id != user["id"]:
        deps.require_group_access(conn, group_id, user, owner_only=True)
    else:
        deps.require_group_access(conn, group_id, user)

    party = f"u:{member_id}"
    on_bills = conn.execute(
        """SELECT COUNT(*) AS n FROM bill_participants p JOIN bills b ON b.id=p.bill_id
            WHERE b.group_id=? AND p.party=?""",
        (group_id, party),
    ).fetchone()["n"]
    if on_bills:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"They are on {on_bills} bill(s) in this group. Remove them from those "
            "bills first, or archive the group instead.",
        )

    owners = conn.execute(
        "SELECT COUNT(*) AS n FROM group_members WHERE group_id=? AND role='owner'", (group_id,)
    ).fetchone()["n"]
    role = conn.execute(
        "SELECT role FROM group_members WHERE group_id=? AND user_id=?", (group_id, member_id)
    ).fetchone()
    if role and role["role"] == "owner" and owners <= 1:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "That is the group's only owner. Hand ownership over first."
        )

    conn.execute("DELETE FROM group_members WHERE group_id=? AND user_id=?", (group_id, member_id))
    db.audit(conn, user, "group.member_removed", f"group:{group_id}", f"user:{member_id}")
    if member_id == user["id"] and not user["is_admin"]:
        return {"ok": True, "left": True}
    return get_group(group_id, user, conn)


class RoleIn(BaseModel):
    role: str = Field(pattern="^(owner|member)$")


@router.post("/groups/{group_id}/members/{member_id}/role")
def set_member_role(
    group_id: int,
    member_id: int,
    payload: RoleIn,
    user: Any = Depends(deps.active_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    deps.require_group_access(conn, group_id, user, owner_only=True)
    if payload.role == "member":
        owners = conn.execute(
            "SELECT COUNT(*) AS n FROM group_members WHERE group_id=? AND role='owner' AND user_id!=?",
            (group_id, member_id),
        ).fetchone()["n"]
        if owners == 0:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "A group needs at least one owner."
            )
    conn.execute(
        "UPDATE group_members SET role=? WHERE group_id=? AND user_id=?",
        (payload.role, group_id, member_id),
    )
    return get_group(group_id, user, conn)


class GuestIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


@router.post("/groups/{group_id}/guests")
def add_guest(
    group_id: int,
    payload: GuestIn,
    user: Any = Depends(deps.active_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    deps.require_group_access(conn, group_id, user)
    conn.execute(
        "INSERT INTO group_guests(group_id, name, created_at) VALUES (?,?,?)",
        (group_id, payload.name.strip(), int(time.time())),
    )
    db.audit(conn, user, "group.guest_added", f"group:{group_id}", payload.name)
    return get_group(group_id, user, conn)


@router.patch("/groups/{group_id}/guests/{guest_id}")
def rename_guest(
    group_id: int,
    guest_id: int,
    payload: GuestIn,
    user: Any = Depends(deps.active_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    deps.require_group_access(conn, group_id, user)
    conn.execute(
        "UPDATE group_guests SET name=? WHERE id=? AND group_id=?",
        (payload.name.strip(), guest_id, group_id),
    )
    return get_group(group_id, user, conn)


@router.delete("/groups/{group_id}/guests/{guest_id}")
def delete_guest(
    group_id: int,
    guest_id: int,
    user: Any = Depends(deps.active_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    deps.require_group_access(conn, group_id, user)
    party = f"g:{guest_id}"
    on_bills = conn.execute(
        """SELECT COUNT(*) AS n FROM bill_participants p JOIN bills b ON b.id=p.bill_id
            WHERE b.group_id=? AND p.party=?""",
        (group_id, party),
    ).fetchone()["n"]
    if on_bills:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"That guest is on {on_bills} bill(s). Remove them from those bills first.",
        )
    conn.execute("DELETE FROM group_guests WHERE id=? AND group_id=?", (guest_id, group_id))
    db.audit(conn, user, "group.guest_removed", f"group:{group_id}", party)
    return get_group(group_id, user, conn)


# --- settling up -------------------------------------------------------------

class SettlementIn(BaseModel):
    from_party: str = Field(min_length=3, max_length=32)
    to_party: str = Field(min_length=3, max_length=32)
    amount: Any = 0
    note: str = Field(default="", max_length=200)
    paid_on: str = Field(default="", max_length=10)


@router.post("/groups/{group_id}/settlements")
def record_settlement(
    group_id: int,
    payload: SettlementIn,
    user: Any = Depends(deps.active_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    deps.require_group_access(conn, group_id, user)
    parties = ledger.group_parties(conn, group_id)
    for key in (payload.from_party, payload.to_party):
        if key not in parties:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{key} is not in this group.")
    if payload.from_party == payload.to_party:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Payer and payee must differ.")

    try:
        cents = splitter.parse_money(payload.amount)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Amount is not a valid number.")
    if cents <= 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Amount must be greater than zero.")

    conn.execute(
        """INSERT INTO settlements(group_id, from_party, to_party, amount_cents, note,
                                   paid_on, created_by, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (group_id, payload.from_party, payload.to_party, cents, payload.note.strip(),
         payload.paid_on or time.strftime("%Y-%m-%d"), user["id"], int(time.time())),
    )
    db.audit(
        conn, user, "settlement.recorded", f"group:{group_id}",
        f"{parties[payload.from_party]['name']} -> {parties[payload.to_party]['name']} "
        f"{splitter.money(cents)}",
    )
    return get_group(group_id, user, conn)


@router.delete("/groups/{group_id}/settlements/{settlement_id}")
def delete_settlement(
    group_id: int,
    settlement_id: int,
    user: Any = Depends(deps.active_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    deps.require_group_access(conn, group_id, user)
    conn.execute(
        "DELETE FROM settlements WHERE id=? AND group_id=?", (settlement_id, group_id)
    )
    db.audit(conn, user, "settlement.deleted", f"group:{group_id}", f"settlement:{settlement_id}")
    return get_group(group_id, user, conn)


# --- export ------------------------------------------------------------------

@router.get("/groups/{group_id}/export.csv")
def export_csv(
    group_id: int,
    user: Any = Depends(deps.active_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> Response:
    """One row per person per bill - drops straight into a spreadsheet."""
    group = deps.require_group_access(conn, group_id, user)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["Date", "Bill", "Category", "Currency", "Person", "Base", "Discount", "Tax",
         "Tip", "Extras", "Owed", "Paid", "Balance"]
    )

    bill_ids = [
        r["id"] for r in conn.execute(
            "SELECT id FROM bills WHERE group_id=? ORDER BY bill_date, id", (group_id,)
        )
    ]
    for bill_id in bill_ids:
        detail = ledger.bill_detail(conn, bill_id)
        if detail is None:
            continue
        bill = detail["bill"]
        for line in detail["breakdown"]:
            writer.writerow([
                bill["bill_date"], bill["title"], bill["category_name"] or "", bill["currency"],
                line["name"],
                splitter.money(line.get("base_cents", 0)),
                splitter.money(line.get("discount_cents", 0)),
                splitter.money(line.get("tax_cents", 0)),
                splitter.money(line.get("tip_cents", 0)),
                splitter.money(line.get("extras_cents", 0)),
                splitter.money(line.get("owed_cents", 0)),
                splitter.money(line.get("paid_cents", 0)),
                splitter.money(line.get("balance_cents", 0)),
            ])

    led = ledger.group_ledger(conn, group_id)
    writer.writerow([])
    writer.writerow(["Net balances"])
    writer.writerow(["Person", "Balance", "", "(negative = owes the group)"])
    for row in led["balances"]:
        writer.writerow([row["name"], splitter.money(row["balance_cents"])])
    writer.writerow([])
    writer.writerow(["Suggested transfers"])
    for t in led["transfers"]:
        writer.writerow([t["from_name"], "pays", t["to_name"], splitter.money(t["amount_cents"])])

    safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in group["name"]).strip() or "group"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{safe} - bill splitter.csv"'},
    )
