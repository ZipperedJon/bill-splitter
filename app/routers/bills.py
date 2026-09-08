"""Bills: create, edit, delete, and a live preview that saves nothing.

Editing a bill is a full replace (PUT) rather than a pile of nested PATCH
endpoints. A bill is small, the client already holds the whole thing while you
edit it, and replacing it wholesale means the saved bill can never end up in a
half-updated state.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from .. import db, deps, ledger, splitter

router = APIRouter(prefix="/api", tags=["bills"])

SPLIT_MODES = {"even", "shares", "itemized"}
CHARGE_MODES = {"none", "percent", "amount"}


class ChargeIn(BaseModel):
    mode: str = "none"
    percent: float = 0
    amount: Any = 0            # dollars, as typed; converted to cents server-side


class TipIn(ChargeIn):
    base: str = "pre_tax"


class ExtraIn(BaseModel):
    label: str = Field(default="Extra", max_length=60)
    mode: str = "amount"
    percent: float = 0
    amount: Any = 0
    split: str = "even"        # even | proportional


class ItemIn(BaseModel):
    label: str = Field(default="", max_length=120)
    amount: Any = 0
    shares: dict[str, float] = Field(default_factory=dict)


class ParticipantIn(BaseModel):
    party: str = Field(min_length=3, max_length=32)
    weight: float = 1


class PaymentIn(BaseModel):
    party: str = Field(min_length=3, max_length=32)
    amount: Any = 0


class BillIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    category_id: int | None = None
    notes: str = Field(default="", max_length=2000)
    bill_date: str = Field(default="", max_length=10)
    currency: str = Field(default="", max_length=8)
    split_mode: str = "even"
    subtotal: Any = 0
    discount: ChargeIn = Field(default_factory=ChargeIn)
    tax: ChargeIn = Field(default_factory=ChargeIn)
    tip: TipIn = Field(default_factory=TipIn)
    extras: list[ExtraIn] = Field(default_factory=list)
    items: list[ItemIn] = Field(default_factory=list)
    participants: list[ParticipantIn] = Field(default_factory=list)
    payments: list[PaymentIn] = Field(default_factory=list)
    # Optimistic concurrency. The editor sends the revision it loaded; if the
    # bill has moved since (someone ticked their items through a share link),
    # the save is refused instead of quietly wiping their selections.
    expected_revision: int | None = None


def _money(value: Any, what: str) -> int:
    try:
        return splitter.parse_money(value)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{what} is not a valid amount.")


def _percent(value: Any, what: str) -> float:
    try:
        pct = float(value or 0)
    except (TypeError, ValueError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{what} is not a valid percentage.")
    if not 0 <= pct <= 1000:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{what} must be between 0 and 1000%.")
    return pct


def _validate(payload: BillIn, parties: dict[str, dict[str, Any]]) -> None:
    if payload.split_mode not in SPLIT_MODES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown split mode.")
    for charge, name in ((payload.discount, "Discount"), (payload.tax, "Tax"), (payload.tip, "Tip")):
        if charge.mode not in CHARGE_MODES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{name} mode must be none, percent or amount.")
    if payload.tip.base not in {"pre_tax", "post_tax"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Tip base must be pre_tax or post_tax.")
    for extra in payload.extras:
        if extra.mode not in {"percent", "amount"}:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Extra mode must be percent or amount.")
        if extra.split not in {"even", "proportional"}:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Extra split must be even or proportional.")

    if not payload.participants:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Add at least one person to the bill.")

    seen: set[str] = set()
    for participant in payload.participants:
        if participant.party not in parties:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"{participant.party} is not in this group."
            )
        if participant.party in seen:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Someone is listed twice.")
        seen.add(participant.party)
        if participant.weight < 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Shares cannot be negative.")

    if payload.split_mode == "shares" and not any(p.weight > 0 for p in payload.participants):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "At least one person needs a share above zero."
        )

    for item in payload.items:
        for key in item.shares:
            if key not in seen:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"An item is assigned to {key}, who is not on this bill.",
                )
    for payment in payload.payments:
        if payment.party not in seen:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "A payment is recorded for someone who is not on this bill.",
            )


def _write_bill(
    conn: sqlite3.Connection, bill_id: int, group_id: int, payload: BillIn, currency: str
) -> None:
    """Replace every child row of a bill. Caller owns the transaction."""
    now = int(time.time())
    conn.execute(
        """UPDATE bills SET title=?, category_id=?, notes=?, bill_date=?, currency=?,
                            split_mode=?, subtotal_cents=?,
                            discount_mode=?, discount_percent=?, discount_cents=?,
                            tax_mode=?, tax_percent=?, tax_cents=?,
                            tip_mode=?, tip_percent=?, tip_cents=?, tip_base=?,
                            updated_at=?, revision=revision+1
                      WHERE id=?""",
        (
            payload.title.strip(),
            payload.category_id,
            payload.notes.strip(),
            payload.bill_date or time.strftime("%Y-%m-%d"),
            currency,
            payload.split_mode,
            _money(payload.subtotal, "Subtotal"),
            payload.discount.mode,
            _percent(payload.discount.percent, "Discount"),
            _money(payload.discount.amount, "Discount"),
            payload.tax.mode,
            _percent(payload.tax.percent, "Tax"),
            _money(payload.tax.amount, "Tax"),
            payload.tip.mode,
            _percent(payload.tip.percent, "Tip"),
            _money(payload.tip.amount, "Tip"),
            payload.tip.base,
            now,
            bill_id,
        ),
    )

    conn.execute("DELETE FROM bill_participants WHERE bill_id=?", (bill_id,))
    conn.execute("DELETE FROM bill_items WHERE bill_id=?", (bill_id,))
    conn.execute("DELETE FROM bill_extras WHERE bill_id=?", (bill_id,))
    conn.execute("DELETE FROM bill_payments WHERE bill_id=?", (bill_id,))

    for participant in payload.participants:
        conn.execute(
            "INSERT INTO bill_participants(bill_id, party, weight) VALUES (?,?,?)",
            (bill_id, participant.party, participant.weight),
        )

    for order, item in enumerate(payload.items):
        cur = conn.execute(
            "INSERT INTO bill_items(bill_id, label, amount_cents, sort_order) VALUES (?,?,?,?)",
            (bill_id, item.label.strip(), _money(item.amount, f"Item '{item.label}'"), order),
        )
        item_id = int(cur.lastrowid)
        for party, weight in item.shares.items():
            if float(weight or 0) > 0:
                conn.execute(
                    "INSERT INTO bill_item_shares(item_id, party, weight) VALUES (?,?,?)",
                    (item_id, party, float(weight)),
                )

    for order, extra in enumerate(payload.extras):
        conn.execute(
            """INSERT INTO bill_extras(bill_id, label, mode, percent, cents, split, sort_order)
               VALUES (?,?,?,?,?,?,?)""",
            (
                bill_id,
                extra.label.strip() or "Extra",
                extra.mode,
                _percent(extra.percent, f"Extra '{extra.label}'"),
                _money(extra.amount, f"Extra '{extra.label}'"),
                extra.split,
                order,
            ),
        )

    for payment in payload.payments:
        cents = _money(payment.amount, "Payment")
        if cents:
            conn.execute(
                "INSERT INTO bill_payments(bill_id, party, amount_cents) VALUES (?,?,?)",
                (bill_id, payment.party, cents),
            )


@router.get("/groups/{group_id}/bills")
def list_bills(
    group_id: int,
    user: Any = Depends(deps.active_user),
    conn: sqlite3.Connection = Depends(db.get_db),
    category_id: int | None = Query(None),
    search: str = Query(""),
) -> dict[str, Any]:
    deps.require_group_access(conn, group_id, user)
    led = ledger.group_ledger(conn, group_id)
    bills = led["bills"]
    if category_id is not None:
        keep = {
            r["id"] for r in conn.execute(
                "SELECT id FROM bills WHERE group_id=? AND category_id=?", (group_id, category_id)
            )
        }
        bills = [b for b in bills if b["id"] in keep]
    if search.strip():
        needle = search.strip().lower()
        bills = [b for b in bills if needle in b["title"].lower()]
    return {"bills": bills}


@router.post("/groups/{group_id}/bills")
def create_bill(
    group_id: int,
    payload: BillIn,
    user: Any = Depends(deps.active_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    group = deps.require_group_access(conn, group_id, user)
    parties = ledger.group_parties(conn, group_id)
    _validate(payload, parties)

    now = int(time.time())
    cur = conn.execute(
        """INSERT INTO bills(group_id, title, bill_date, currency, created_by, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?)""",
        (group_id, payload.title.strip(), payload.bill_date or time.strftime("%Y-%m-%d"),
         (payload.currency or group["currency"]).upper(), user["id"], now, now),
    )
    bill_id = int(cur.lastrowid)
    _write_bill(conn, bill_id, group_id, payload, (payload.currency or group["currency"]).upper())
    db.audit(conn, user, "bill.created", f"bill:{bill_id}", payload.title)

    detail = ledger.bill_detail(conn, bill_id)
    return {"bill_id": bill_id, **(detail or {})}


@router.get("/bills/{bill_id}")
def get_bill(
    bill_id: int,
    user: Any = Depends(deps.active_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    row = conn.execute("SELECT group_id FROM bills WHERE id=?", (bill_id,)).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bill not found.")
    deps.require_group_access(conn, row["group_id"], user)
    detail = ledger.bill_detail(conn, bill_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bill not found.")
    return detail


@router.put("/bills/{bill_id}")
def update_bill(
    bill_id: int,
    payload: BillIn,
    user: Any = Depends(deps.active_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM bills WHERE id=?", (bill_id,)).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bill not found.")
    group = deps.require_group_access(conn, row["group_id"], user)

    if payload.expected_revision is not None and payload.expected_revision != row["revision"]:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This bill changed while you had it open - someone picked their items "
            "through the share link. Reload to see their choices, then save again.",
        )

    parties = ledger.group_parties(conn, row["group_id"])
    _validate(payload, parties)

    _write_bill(
        conn, bill_id, row["group_id"], payload,
        (payload.currency or row["currency"] or group["currency"]).upper(),
    )
    db.audit(conn, user, "bill.updated", f"bill:{bill_id}", payload.title)
    detail = ledger.bill_detail(conn, bill_id)
    return {"bill_id": bill_id, **(detail or {})}


@router.delete("/bills/{bill_id}")
def delete_bill(
    bill_id: int,
    user: Any = Depends(deps.active_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM bills WHERE id=?", (bill_id,)).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bill not found.")
    deps.require_group_access(conn, row["group_id"], user)
    conn.execute("DELETE FROM bills WHERE id=?", (bill_id,))
    db.audit(conn, user, "bill.deleted", f"bill:{bill_id}", row["title"])
    return {"ok": True, "group_id": row["group_id"]}


class PreviewIn(BillIn):
    group_id: int
    # A bill being typed has no title yet, and the preview is not saving
    # anything, so relax the one field BillIn insists on.
    title: str = Field(default="", max_length=120)


@router.post("/bills/preview")
def preview_bill(
    payload: PreviewIn,
    user: Any = Depends(deps.active_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    """Compute a bill without saving it - powers the live totals while typing."""
    deps.require_group_access(conn, payload.group_id, user)
    parties = ledger.group_parties(conn, payload.group_id)

    # A half-typed bill should show a friendly note, not a validation error.
    known = [p for p in payload.participants if p.party in parties]
    if not known:
        return {
            "totals": {"total_cents": 0, "warnings": ["Add people to see the split."]},
            "breakdown": [],
        }

    result = splitter.compute_bill(
        split_mode=payload.split_mode if payload.split_mode in SPLIT_MODES else "even",
        subtotal_cents=_money(payload.subtotal, "Subtotal"),
        participants=[{"party": p.party, "weight": p.weight} for p in known],
        items=[
            {
                "label": i.label,
                "amount_cents": _money(i.amount, f"Item '{i.label}'"),
                "shares": {k: v for k, v in i.shares.items() if k in parties},
            }
            for i in payload.items
        ],
        discount={
            "mode": payload.discount.mode,
            "percent": _percent(payload.discount.percent, "Discount"),
            "cents": _money(payload.discount.amount, "Discount"),
        },
        tax={
            "mode": payload.tax.mode,
            "percent": _percent(payload.tax.percent, "Tax"),
            "cents": _money(payload.tax.amount, "Tax"),
        },
        tip={
            "mode": payload.tip.mode,
            "percent": _percent(payload.tip.percent, "Tip"),
            "cents": _money(payload.tip.amount, "Tip"),
            "base": payload.tip.base,
        },
        extras=[
            {
                "label": e.label,
                "mode": e.mode,
                "percent": _percent(e.percent, f"Extra '{e.label}'"),
                "cents": _money(e.amount, f"Extra '{e.label}'"),
                "split": e.split,
            }
            for e in payload.extras
        ],
        payments=[
            {"party": p.party, "amount_cents": _money(p.amount, "Payment")}
            for p in payload.payments
            if p.party in parties
        ],
    )

    breakdown = sorted(
        (
            {
                **line,
                "party": key,
                "name": ledger.party_label(parties, key),
            }
            for key, line in result["per_party"].items()
        ),
        key=lambda x: (-x["owed_cents"], x["name"].lower()),
    )
    return {
        "totals": {k: v for k, v in result.items() if k != "per_party"},
        "breakdown": breakdown,
    }


@router.get("/dashboard")
def dashboard(
    user: Any = Depends(deps.active_user), conn: sqlite3.Connection = Depends(db.get_db)
) -> dict[str, Any]:
    """Home screen: what you owe, what you're owed, and recent activity."""
    my_party = f"u:{user['id']}"
    group_rows = conn.execute(
        """SELECT g.* FROM groups g JOIN group_members gm ON gm.group_id=g.id
            WHERE gm.user_id=? AND g.archived=0""",
        (user["id"],),
    ).fetchall()

    owed_to_me = 0
    i_owe = 0
    per_group: list[dict[str, Any]] = []
    recent: list[dict[str, Any]] = []
    categories: dict[str, int] = {}

    for group in group_rows:
        led = ledger.group_ledger(conn, group["id"])
        balance = next(
            (b["balance_cents"] for b in led["balances"] if b["party"] == my_party), 0
        )
        if balance > 0:
            owed_to_me += balance
        else:
            i_owe += -balance
        per_group.append(
            {
                "id": group["id"],
                "name": group["name"],
                "currency": group["currency"],
                "my_balance_cents": balance,
                "total_spend_cents": led["total_spend_cents"],
                "outstanding_cents": led["outstanding_cents"],
                "bill_count": len(led["bills"]),
                "my_transfers": [
                    t for t in led["transfers"]
                    if t["from_party"] == my_party or t["to_party"] == my_party
                ],
            }
        )
        for bill in led["bills"][:5]:
            recent.append({**bill, "group_id": group["id"], "group_name": group["name"]})
        for cat in led["spend_by_category"]:
            categories[cat["category"]] = categories.get(cat["category"], 0) + cat["total_cents"]

    recent.sort(key=lambda b: (b["bill_date"], b["id"]), reverse=True)

    pending = 0
    if user["is_admin"]:
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE status='pending'"
        ).fetchone()["n"]

    return {
        "owed_to_me_cents": owed_to_me,
        "i_owe_cents": i_owe,
        "net_cents": owed_to_me - i_owe,
        "groups": per_group,
        "recent_bills": recent[:12],
        "spend_by_category": sorted(
            ({"category": k, "total_cents": v} for k, v in categories.items()),
            key=lambda c: -c["total_cents"],
        ),
        "pending_approvals": pending,
    }
