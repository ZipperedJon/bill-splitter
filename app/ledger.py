"""Glue between the database and the pure split engine.

Loads a bill out of SQLite, hands it to splitter.compute_bill, and rolls
per-bill results up into group balances and settle-up suggestions.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import splitter


def group_parties(conn: sqlite3.Connection, group_id: int) -> dict[str, dict[str, Any]]:
    """Everyone who can appear on a bill in this group, keyed by party."""
    parties: dict[str, dict[str, Any]] = {}

    for row in conn.execute(
        """SELECT u.id, u.username, u.display_name, u.status, gm.role
             FROM group_members gm JOIN users u ON u.id = gm.user_id
            WHERE gm.group_id = ?
            ORDER BY LOWER(COALESCE(NULLIF(u.display_name,''), u.username))""",
        (group_id,),
    ):
        parties[f"u:{row['id']}"] = {
            "party": f"u:{row['id']}",
            "kind": "user",
            "user_id": row["id"],
            "username": row["username"],
            "name": row["display_name"] or row["username"],
            "role": row["role"],
            "status": row["status"],
        }

    for row in conn.execute(
        "SELECT id, name FROM group_guests WHERE group_id=? ORDER BY LOWER(name)", (group_id,)
    ):
        parties[f"g:{row['id']}"] = {
            "party": f"g:{row['id']}",
            "kind": "guest",
            "guest_id": row["id"],
            "name": row["name"],
            "role": "guest",
            "status": "guest",
        }

    return parties


def party_label(parties: dict[str, dict[str, Any]], key: str) -> str:
    entry = parties.get(key)
    if entry:
        return entry["name"]
    # A party that no longer resolves (deleted group guest, say). Never crash a
    # balance sheet over it.
    return "Unknown"


def load_bill(conn: sqlite3.Connection, bill_id: int) -> dict[str, Any] | None:
    bill = conn.execute(
        """SELECT b.*, c.name AS category_name, c.icon AS category_icon,
                  u.username AS created_by_name
             FROM bills b
             LEFT JOIN categories c ON c.id = b.category_id
             LEFT JOIN users u ON u.id = b.created_by
            WHERE b.id = ?""",
        (bill_id,),
    ).fetchone()
    if bill is None:
        return None

    participants = [
        {"id": r["id"], "party": r["party"], "weight": r["weight"]}
        for r in conn.execute(
            "SELECT id, party, weight FROM bill_participants WHERE bill_id=? ORDER BY id",
            (bill_id,),
        )
    ]

    shares_by_item: dict[int, dict[str, float]] = {}
    for r in conn.execute(
        """SELECT s.item_id, s.party, s.weight FROM bill_item_shares s
             JOIN bill_items i ON i.id = s.item_id WHERE i.bill_id=?""",
        (bill_id,),
    ):
        shares_by_item.setdefault(r["item_id"], {})[r["party"]] = r["weight"]

    # Flat list for the split engine (it resolves parents itself), plus a
    # parent-with-sub_items shape for the UI. Sub-items deliberately carry no
    # shares: they follow their parent.
    items = [
        {
            "id": r["id"],
            "parent_id": r["parent_id"],
            "label": r["label"],
            "amount_cents": r["amount_cents"],
            "portions": r["portions"],
            "sort_order": r["sort_order"],
            "shares": shares_by_item.get(r["id"], {}) if r["parent_id"] is None else {},
        }
        for r in conn.execute(
            "SELECT * FROM bill_items WHERE bill_id=? ORDER BY sort_order, id", (bill_id,)
        )
    ]

    extras = [
        {
            "id": r["id"],
            "label": r["label"],
            "mode": r["mode"],
            "percent": r["percent"],
            "cents": r["cents"],
            "split": r["split"],
        }
        for r in conn.execute(
            "SELECT * FROM bill_extras WHERE bill_id=? ORDER BY sort_order, id", (bill_id,)
        )
    ]

    payments = [
        {"id": r["id"], "party": r["party"], "amount_cents": r["amount_cents"]}
        for r in conn.execute(
            "SELECT * FROM bill_payments WHERE bill_id=? ORDER BY id", (bill_id,)
        )
    ]

    return {
        "bill": dict(bill),
        "participants": participants,
        "items": items,
        "tree": nest_items(items),
        "extras": extras,
        "payments": payments,
    }


def nest_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Top-level items, each with its sub-items attached, for the UI."""
    children: dict[Any, list[dict[str, Any]]] = {}
    for item in items:
        if item.get("parent_id") is not None:
            children.setdefault(item["parent_id"], []).append(item)
    return [
        {
            **item,
            "sub_items": [
                {k: v for k, v in child.items() if k != "shares"}
                for child in children.get(item["id"], [])
            ],
            "sub_total_cents": sum(c["amount_cents"] for c in children.get(item["id"], [])),
        }
        for item in items
        if item.get("parent_id") is None
    ]


def compute(raw: dict[str, Any]) -> dict[str, Any]:
    b = raw["bill"]
    return splitter.compute_bill(
        split_mode=b["split_mode"],
        subtotal_cents=b["subtotal_cents"],
        participants=raw["participants"],
        items=raw["items"],
        discount={
            "mode": b["discount_mode"],
            "percent": b["discount_percent"],
            "cents": b["discount_cents"],
            "target": b["discount_target"],
        },
        tax={"mode": b["tax_mode"], "percent": b["tax_percent"], "cents": b["tax_cents"]},
        tip={
            "mode": b["tip_mode"],
            "percent": b["tip_percent"],
            "cents": b["tip_cents"],
            "base": b["tip_base"],
        },
        extras=raw["extras"],
        payments=raw["payments"],
    )


def bill_detail(conn: sqlite3.Connection, bill_id: int) -> dict[str, Any] | None:
    raw = load_bill(conn, bill_id)
    if raw is None:
        return None
    result = compute(raw)
    parties = group_parties(conn, raw["bill"]["group_id"])

    breakdown = []
    for participant in raw["participants"]:
        key = participant["party"]
        line = dict(result["per_party"].get(key, {}))
        line.update(
            {
                "party": key,
                "name": party_label(parties, key),
                "weight": participant["weight"],
            }
        )
        breakdown.append(line)
    breakdown.sort(key=lambda x: (-x.get("owed_cents", 0), x["name"].lower()))

    return {
        "bill": raw["bill"],
        "participants": raw["participants"],
        "items": raw["tree"],
        "extras": raw["extras"],
        "payments": raw["payments"],
        "totals": {k: v for k, v in result.items() if k != "per_party"},
        "breakdown": breakdown,
        "parties": list(parties.values()),
    }


def group_ledger(conn: sqlite3.Connection, group_id: int) -> dict[str, Any]:
    """Everything you need for a group's Balances tab.

    Balance convention: positive means the group owes them money (they paid
    more than their share); negative means they owe.
    """
    parties = group_parties(conn, group_id)
    balances: dict[str, int] = {key: 0 for key in parties}
    spend_by_category: dict[str, dict[str, Any]] = {}
    total_spend = 0
    bill_summaries: list[dict[str, Any]] = []

    bill_ids = [
        r["id"] for r in conn.execute(
            "SELECT id FROM bills WHERE group_id=? ORDER BY bill_date DESC, id DESC", (group_id,)
        )
    ]

    for bill_id in bill_ids:
        raw = load_bill(conn, bill_id)
        if raw is None:
            continue
        result = compute(raw)
        bill = raw["bill"]
        total_spend += result["total_cents"]

        cat = bill["category_name"] or "Uncategorised"
        bucket = spend_by_category.setdefault(
            cat, {"category": cat, "icon": bill["category_icon"] or "other", "total_cents": 0, "bills": 0}
        )
        bucket["total_cents"] += result["total_cents"]
        bucket["bills"] += 1

        for key, line in result["per_party"].items():
            balances[key] = balances.get(key, 0) + line["balance_cents"]

        bill_summaries.append(
            {
                "id": bill["id"],
                "title": bill["title"],
                "bill_date": bill["bill_date"],
                "category_name": bill["category_name"],
                "category_icon": bill["category_icon"] or "other",
                "split_mode": bill["split_mode"],
                "currency": bill["currency"],
                "total_cents": result["total_cents"],
                "paid_total_cents": result["paid_total_cents"],
                "unpaid_cents": result["unpaid_cents"],
                "participant_count": len(raw["participants"]),
                "created_by_name": bill["created_by_name"],
                "warnings": result["warnings"],
            }
        )

    # Settlements already paid move the needle back toward zero.
    settlements = [
        dict(r) for r in conn.execute(
            "SELECT * FROM settlements WHERE group_id=? ORDER BY paid_on DESC, id DESC", (group_id,)
        )
    ]
    for s in settlements:
        balances[s["from_party"]] = balances.get(s["from_party"], 0) + s["amount_cents"]
        balances[s["to_party"]] = balances.get(s["to_party"], 0) - s["amount_cents"]
        s["from_name"] = party_label(parties, s["from_party"])
        s["to_name"] = party_label(parties, s["to_party"])

    transfers = [
        {
            **t,
            "from_name": party_label(parties, t["from_party"]),
            "to_name": party_label(parties, t["to_party"]),
        }
        for t in splitter.settle(balances)
    ]

    balance_rows = sorted(
        (
            {
                "party": key,
                "name": party_label(parties, key),
                "kind": parties.get(key, {}).get("kind", "unknown"),
                "balance_cents": value,
            }
            for key, value in balances.items()
        ),
        key=lambda r: (-r["balance_cents"], r["name"].lower()),
    )

    return {
        "parties": list(parties.values()),
        "balances": balance_rows,
        "transfers": transfers,
        "settlements": settlements,
        "bills": bill_summaries,
        "spend_by_category": sorted(
            spend_by_category.values(), key=lambda c: -c["total_cents"]
        ),
        "total_spend_cents": total_spend,
        "outstanding_cents": sum(t["amount_cents"] for t in transfers),
    }
