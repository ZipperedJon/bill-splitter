"""Share links: send a bill to people so they can tick off what they had.

The owner creates a link for a bill. Anyone who opens it says who they are -
picking from the people already on the bill, or adding their own name - and
then selects their items. No account needed.

The token is the entire credential, so these endpoints are deliberately narrow:

  * They never reveal anything outside the one bill. No usernames, no emails,
    no other bills, no group balances - only what is printed on this receipt
    and the display names of the people splitting it.
  * A visitor can only change *their own* claims. Every write is scoped to one
    party and to items belonging to this bill.
  * Claims are surgical inserts and deletes, not a whole-bill replace, so two
    people ticking items at the same table cannot overwrite each other.

Anyone with the link can claim to be anyone on the bill; that is inherent to a
link you paste into a group chat. The UI warns before you take over a name that
has already picked, and the owner can close or rotate the link at any time.
"""

from __future__ import annotations

import secrets
import sqlite3
import time
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from .. import db, deps, ledger, splitter

router = APIRouter(tags=["share"])

TOKEN_BYTES = 24  # ~32 url-safe characters; not guessable

# Slow down anyone walking the token space. Generous enough that a real group
# passing one link around never notices.
_MISS_WINDOW = 300
_MISS_LIMIT = 60
_misses: dict[str, list[float]] = defaultdict(list)


def _too_many_misses(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _misses[ip] if now - t < _MISS_WINDOW]
    _misses[ip] = recent
    return len(recent) >= _MISS_LIMIT


def _record_miss(ip: str) -> None:
    _misses[ip].append(time.time())


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


# --- owner side --------------------------------------------------------------

class ShareIn(BaseModel):
    allow_join: bool = True
    expires_days: int | None = Field(default=None, ge=1, le=365)


class SharePatch(BaseModel):
    allow_join: bool | None = None
    closed: bool | None = None


def _bill_for_owner(conn: sqlite3.Connection, bill_id: int, user: Any) -> sqlite3.Row:
    bill = conn.execute("SELECT * FROM bills WHERE id=?", (bill_id,)).fetchone()
    if bill is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bill not found.")
    deps.require_group_access(conn, bill["group_id"], user)
    return bill


def _share_status(conn: sqlite3.Connection, bill_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM bill_shares WHERE bill_id=?", (bill_id,)).fetchone()
    if row is None:
        return {"exists": False}

    parties = ledger.group_parties(
        conn,
        conn.execute("SELECT group_id FROM bills WHERE id=?", (bill_id,)).fetchone()["group_id"],
    )
    claimed: dict[str, int] = defaultdict(int)
    for share in conn.execute(
        """SELECT s.party FROM bill_item_shares s
             JOIN bill_items i ON i.id = s.item_id WHERE i.bill_id=?""",
        (bill_id,),
    ):
        claimed[share["party"]] += 1

    people = [
        {
            "party": p["party"],
            "name": ledger.party_label(parties, p["party"]),
            "claimed_items": claimed.get(p["party"], 0),
        }
        for p in conn.execute(
            "SELECT party FROM bill_participants WHERE bill_id=? ORDER BY id", (bill_id,)
        )
    ]
    item_count = conn.execute(
        "SELECT COUNT(*) AS n FROM bill_items WHERE bill_id=?", (bill_id,)
    ).fetchone()["n"]

    # The browser builds the link from its own origin, which is right whenever
    # the admin is already on the address guests will use. It is wrong when they
    # are on the LAN address and the guests are not, so a configured public
    # address wins when there is one.
    public_base = db.get_setting(conn, "public_base_url", "").rstrip("/")
    path = f"/s/{row['token']}"

    return {
        "exists": True,
        "token": row["token"],
        "path": path,
        "public_url": f"{public_base}{path}" if public_base else "",
        "public_base_url": public_base,
        "allow_join": bool(row["allow_join"]),
        "closed": bool(row["closed"]),
        "expires_at": row["expires_at"],
        "expired": bool(row["expires_at"] and row["expires_at"] < int(time.time())),
        "views": row["views"],
        "last_seen_at": row["last_seen_at"],
        "created_at": row["created_at"],
        "people": people,
        "item_count": item_count,
        "people_who_picked": sum(1 for p in people if p["claimed_items"]),
    }


@router.get("/api/bills/{bill_id}/share")
def get_share(
    bill_id: int,
    user: Any = Depends(deps.active_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    _bill_for_owner(conn, bill_id, user)
    return {"share": _share_status(conn, bill_id)}


@router.post("/api/bills/{bill_id}/share")
def create_share(
    bill_id: int,
    payload: ShareIn,
    user: Any = Depends(deps.active_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    """Create the link, or hand back the existing one (so the button is safe to
    press twice without silently invalidating a link already sent out)."""
    _bill_for_owner(conn, bill_id, user)

    existing = conn.execute("SELECT * FROM bill_shares WHERE bill_id=?", (bill_id,)).fetchone()
    if existing is not None:
        return {"share": _share_status(conn, bill_id), "created": False}

    expires_at = (
        int(time.time()) + payload.expires_days * 86400 if payload.expires_days else None
    )
    conn.execute(
        """INSERT INTO bill_shares(bill_id, token, allow_join, expires_at, created_by, created_at)
           VALUES (?,?,?,?,?,?)""",
        (bill_id, secrets.token_urlsafe(TOKEN_BYTES), 1 if payload.allow_join else 0,
         expires_at, user["id"], int(time.time())),
    )
    db.audit(conn, user, "share.created", f"bill:{bill_id}")
    return {"share": _share_status(conn, bill_id), "created": True}


@router.patch("/api/bills/{bill_id}/share")
def update_share(
    bill_id: int,
    payload: SharePatch,
    user: Any = Depends(deps.active_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    _bill_for_owner(conn, bill_id, user)
    if conn.execute("SELECT 1 FROM bill_shares WHERE bill_id=?", (bill_id,)).fetchone() is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This bill has no share link.")

    fields = payload.model_dump(exclude_none=True)
    for key, value in fields.items():
        conn.execute(
            f"UPDATE bill_shares SET {key}=? WHERE bill_id=?", (1 if value else 0, bill_id)
        )
    if fields:
        db.audit(conn, user, "share.updated", f"bill:{bill_id}", ", ".join(sorted(fields)))
    return {"share": _share_status(conn, bill_id)}


@router.post("/api/bills/{bill_id}/share/rotate")
def rotate_share(
    bill_id: int,
    user: Any = Depends(deps.active_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    """New token, old link dead. For when a link went somewhere it shouldn't."""
    _bill_for_owner(conn, bill_id, user)
    if conn.execute("SELECT 1 FROM bill_shares WHERE bill_id=?", (bill_id,)).fetchone() is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This bill has no share link.")
    conn.execute(
        "UPDATE bill_shares SET token=?, views=0, last_seen_at=NULL WHERE bill_id=?",
        (secrets.token_urlsafe(TOKEN_BYTES), bill_id),
    )
    db.audit(conn, user, "share.rotated", f"bill:{bill_id}")
    return {"share": _share_status(conn, bill_id)}


@router.delete("/api/bills/{bill_id}/share")
def revoke_share(
    bill_id: int,
    user: Any = Depends(deps.active_user),
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    _bill_for_owner(conn, bill_id, user)
    conn.execute("DELETE FROM bill_shares WHERE bill_id=?", (bill_id,))
    db.audit(conn, user, "share.revoked", f"bill:{bill_id}")
    return {"share": {"exists": False}}


# --- public side -------------------------------------------------------------

def _load_share(conn: sqlite3.Connection, token: str, request: Request) -> sqlite3.Row:
    ip = _client_ip(request)
    if _too_many_misses(ip):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts. Try again in a few minutes."
        )
    row = conn.execute("SELECT * FROM bill_shares WHERE token=?", (token,)).fetchone()
    if row is None:
        _record_miss(ip)
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "This link is not valid. It may have been revoked or replaced - ask whoever sent it.",
        )
    if row["expires_at"] and row["expires_at"] < int(time.time()):
        raise HTTPException(status.HTTP_410_GONE, "This link has expired.")
    return row


def _require_open(share: sqlite3.Row) -> None:
    if share["closed"]:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This bill has been closed for changes. Ask whoever sent the link to reopen it.",
        )


def _public_payload(conn: sqlite3.Connection, share: sqlite3.Row) -> dict[str, Any]:
    """Everything the share page needs, and nothing else."""
    bill_id = share["bill_id"]
    raw = ledger.load_bill(conn, bill_id)
    if raw is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That bill no longer exists.")

    bill = raw["bill"]
    result = ledger.compute(raw)
    parties = ledger.group_parties(conn, bill["group_id"])
    group = conn.execute("SELECT name FROM groups WHERE id=?", (bill["group_id"],)).fetchone()

    on_bill = [p["party"] for p in raw["participants"]]
    # Only top-level lines are claimable; sub-items follow their parent.
    claims_by_item = {
        item["id"]: {k: float(v) for k, v in item["shares"].items() if k in on_bill}
        for item in raw["tree"]
    }
    claimed_counts: dict[str, int] = defaultdict(int)
    for claims in claims_by_item.values():
        for key in claims:
            claimed_counts[key] += 1

    people = [
        {
            "party": key,
            "name": ledger.party_label(parties, key),
            "claimed_items": claimed_counts.get(key, 0),
            "owed_cents": result["per_party"].get(key, {}).get("owed_cents", 0),
            "paid_cents": result["per_party"].get(key, {}).get("paid_cents", 0),
        }
        for key in on_bill
    ]
    people.sort(key=lambda p: p["name"].lower())

    charges = []
    if result["discount_cents"]:
        charges.append({
            "label": "Discount"
            + (f" ({_pct(bill['discount_percent'])})" if bill["discount_mode"] == "percent" else ""),
            "cents": -result["discount_cents"],
        })
    if result["tax_cents"]:
        charges.append({
            "label": "Tax"
            + (f" ({_pct(bill['tax_percent'])})" if bill["tax_mode"] == "percent" else ""),
            "cents": result["tax_cents"],
        })
    if result["tip_cents"]:
        charges.append({
            "label": "Tip"
            + (f" ({_pct(bill['tip_percent'])})" if bill["tip_mode"] == "percent" else ""),
            "cents": result["tip_cents"],
        })
    for extra in result["extras"]:
        if extra["cents"]:
            charges.append({
                "label": extra["label"]
                + (f" ({_pct(extra['percent'])})" if extra["mode"] == "percent" else ""),
                "cents": extra["cents"],
            })

    return {
        "bill": {
            "title": bill["title"],
            "bill_date": bill["bill_date"],
            "currency": bill["currency"],
            "split_mode": bill["split_mode"],
            "notes": bill["notes"],
            "category_name": bill["category_name"],
        },
        "group_name": group["name"] if group else "",
        "itemized": bill["split_mode"] == "itemized",
        "items": [
            {
                "id": item["id"],
                "label": item["label"] or "Item",
                "amount_cents": item["amount_cents"],
                "portions": item["portions"],
                # Line price including its modifications, which is what the
                # person ticking it actually takes on.
                "line_total_cents": item["amount_cents"] + item["sub_total_cents"],
                "sub_items": [
                    {"label": sub["label"] or "Extra", "amount_cents": sub["amount_cents"]}
                    for sub in item["sub_items"]
                ],
                "claimed_by": list(claims_by_item.get(item["id"], {}).keys()),
                "claims": claims_by_item.get(item["id"], {}),
            }
            for item in raw["tree"]
        ],
        "people": people,
        "charges": charges,
        "totals": {
            "subtotal_cents": result["subtotal_cents"],
            "total_cents": result["total_cents"],
        },
        "warnings": result["warnings"],
        "share": {
            "closed": bool(share["closed"]),
            "allow_join": bool(share["allow_join"]),
            "expires_at": share["expires_at"],
        },
    }


def _pct(value: Any) -> str:
    number = float(value or 0)
    trimmed = int(number) if number == int(number) else round(number, 4)
    return f"{trimmed}%"


@router.get("/api/share/{token}")
def view_share(
    token: str,
    request: Request,
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    share = _load_share(conn, token, request)
    conn.execute(
        "UPDATE bill_shares SET views = views + 1, last_seen_at=? WHERE id=?",
        (int(time.time()), share["id"]),
    )
    return _public_payload(conn, share)


class JoinIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)


@router.post("/api/share/{token}/join")
def join_bill(
    token: str,
    payload: JoinIn,
    request: Request,
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    """"I'm not on the list" - add yourself by name.

    Creates a guest in the bill's group and puts them on this bill. Reuses an
    existing guest of the same name rather than making a second "Dana", since
    two people at one table typing the same name mean the same person.
    """
    share = _load_share(conn, token, request)
    _require_open(share)
    if not share["allow_join"]:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Whoever sent this link turned off adding new people. Ask them to add you.",
        )

    bill = conn.execute("SELECT * FROM bills WHERE id=?", (share["bill_id"],)).fetchone()
    if bill is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That bill no longer exists.")

    name = payload.name.strip()
    group_id = bill["group_id"]

    # Don't let someone "join" as a name already on the bill - that is the
    # "I'm on the list" path, and quietly creating a duplicate would split the
    # same person's items across two entries.
    parties = ledger.group_parties(conn, group_id)
    on_bill = {
        p["party"] for p in conn.execute(
            "SELECT party FROM bill_participants WHERE bill_id=?", (share["bill_id"],)
        )
    }
    for key in on_bill:
        if ledger.party_label(parties, key).strip().lower() == name.lower():
            return {"party": key, "name": ledger.party_label(parties, key), "existing": True}

    guest = conn.execute(
        "SELECT id FROM group_guests WHERE group_id=? AND name=? COLLATE NOCASE",
        (group_id, name),
    ).fetchone()
    if guest is None:
        cur = conn.execute(
            "INSERT INTO group_guests(group_id, name, created_at) VALUES (?,?,?)",
            (group_id, name, int(time.time())),
        )
        guest_id = int(cur.lastrowid)
    else:
        guest_id = guest["id"]

    party = f"g:{guest_id}"
    conn.execute(
        "INSERT OR IGNORE INTO bill_participants(bill_id, party, weight) VALUES (?,?,1)",
        (share["bill_id"], party),
    )
    _touch_bill(conn, share["bill_id"])
    db.audit(conn, None, "share.joined", f"bill:{share['bill_id']}", name)
    return {"party": party, "name": name, "existing": False}


class ClaimsIn(BaseModel):
    party: str = Field(min_length=3, max_length=32)
    # Whole lines taken, one portion each.
    item_ids: list[int] = Field(default_factory=list)
    # item id -> how many portions of a divided line ("2 of the 3 beers").
    # Takes precedence over item_ids for the same id.
    portions: dict[str, float] = Field(default_factory=dict)


@router.post("/api/share/{token}/claims")
def set_claims(
    token: str,
    payload: ClaimsIn,
    request: Request,
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    """Replace this one person's item selections. Never touches anyone else's."""
    share = _load_share(conn, token, request)
    _require_open(share)
    bill_id = share["bill_id"]

    on_bill = {
        p["party"] for p in conn.execute(
            "SELECT party FROM bill_participants WHERE bill_id=?", (bill_id,)
        )
    }
    if payload.party not in on_bill:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "You are not on this bill any more - reload the page and pick again.",
        )

    # Only top-level lines can be claimed; a modification belongs to its dish.
    claimable = {
        r["id"]: r["portions"] for r in conn.execute(
            "SELECT id, portions FROM bill_items WHERE bill_id=? AND parent_id IS NULL",
            (bill_id,),
        )
    }

    wanted: dict[int, float] = {item_id: 1.0 for item_id in payload.item_ids}
    for raw_id, units in payload.portions.items():
        try:
            wanted[int(raw_id)] = float(units)
        except (TypeError, ValueError):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Bad portion count.")

    for item_id, units in wanted.items():
        if item_id not in claimable:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Some of those items are not on this bill, or are extras that "
                "come with another item.",
            )
        if units > claimable[item_id]:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"That is more than the {claimable[item_id]} portion"
                f"{'s' if claimable[item_id] != 1 else ''} on that line.",
            )

    # Scoped to this party and this bill's items, so simultaneous claims by
    # different people at the same table do not collide.
    conn.execute(
        """DELETE FROM bill_item_shares
            WHERE party=? AND item_id IN (SELECT id FROM bill_items WHERE bill_id=?)""",
        (payload.party, bill_id),
    )
    for item_id, units in wanted.items():
        if units > 0:
            conn.execute(
                "INSERT INTO bill_item_shares(item_id, party, weight) VALUES (?,?,?)",
                (item_id, payload.party, units),
            )
    _touch_bill(conn, bill_id)

    return _public_payload(conn, share)


def _touch_bill(conn: sqlite3.Connection, bill_id: int) -> None:
    """Bump the revision so the owner's editor notices and cannot silently
    overwrite what people picked (see the version check on PUT /api/bills)."""
    conn.execute(
        "UPDATE bills SET updated_at=?, revision=revision+1 WHERE id=?",
        (int(time.time()), bill_id),
    )


@router.get("/api/share/{token}/mine")
def my_share(
    token: str,
    party: str,
    request: Request,
    conn: sqlite3.Connection = Depends(db.get_db),
) -> dict[str, Any]:
    """What this one person currently owes - for the 'you owe' line."""
    share = _load_share(conn, token, request)
    raw = ledger.load_bill(conn, share["bill_id"])
    if raw is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That bill no longer exists.")
    result = ledger.compute(raw)
    line = result["per_party"].get(party)
    if line is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not on this bill.")
    parties = ledger.group_parties(conn, raw["bill"]["group_id"])
    return {
        "party": party,
        "name": ledger.party_label(parties, party),
        "currency": raw["bill"]["currency"],
        "formatted": splitter.money(line["owed_cents"]),
        **line,
    }
