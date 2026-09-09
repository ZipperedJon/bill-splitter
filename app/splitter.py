"""The split engine. Pure functions, no database, no I/O - so it is testable.

Rules of the house:
  * All money is integer cents. Percentages go through Decimal, never float
    arithmetic, so 8.875% of $41.30 lands where a human says it should.
  * Every distribution uses the largest-remainder method, so the sum of what
    each person owes is *exactly* the bill total. No orphaned pennies, and no
    bill where four people each owe $10.00 of a $39.99 tab.

Order of operations (the same order a restaurant receipt uses):
    items / entered subtotal
      - discount           (percent of subtotal, or a flat amount)
      = net subtotal
      + tax                (percent of net subtotal, or a flat amount)
      + tip                (percent of net subtotal or of net+tax, or flat)
      + extras             (delivery, resort fee, service charge, corkage...)
      = total

Tax, tip and proportional extras are apportioned by each person's share of the
net subtotal, which is the fair answer when one person ordered the steak and
another had a salad.
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Iterable, Sequence

WEIGHT_SCALE = 10_000  # weights become integers, keeping allocation exact


def round_half_up(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def percent_of(base_cents: int, percent: float | Decimal | str) -> int:
    """`percent`% of base_cents, rounded half-up to the cent."""
    return round_half_up(Decimal(int(base_cents)) * Decimal(str(percent)) / Decimal(100))


def allocate(total_cents: int, weights: Sequence[float]) -> list[int]:
    """Split `total_cents` across `weights`; the result always sums to the input.

    Largest-remainder: everyone gets the floor of their exact share, then the
    leftover cents go one each to the largest remainders. Deterministic - ties
    break on position, so recomputing a bill never reshuffles who ate the cent.
    """
    n = len(weights)
    if n == 0:
        return []

    sign = -1 if total_cents < 0 else 1
    total = abs(int(total_cents))

    iw = [max(0, int(round(float(w) * WEIGHT_SCALE))) for w in weights]
    if sum(iw) <= 0:
        iw = [1] * n  # nobody has a weight -> straight even split
    weight_sum = sum(iw)

    base = [total * w // weight_sum for w in iw]
    remainders = [(total * w) % weight_sum for w in iw]
    leftover = total - sum(base)

    order = sorted(range(n), key=lambda i: (-remainders[i], i))
    for k in range(leftover):
        base[order[k]] += 1

    return [sign * amount for amount in base]


def _amount_from(mode: str, percent: float, cents: int, base_cents: int) -> int:
    if mode == "percent":
        return percent_of(base_cents, percent)
    if mode == "amount":
        return int(cents)
    return 0


def portion_price(amount_cents: int, portions: int) -> int:
    """The headline price of one portion, to the nearest cent.

    The real split (see compute_bill) hands out largest-remainder portions so
    the line still totals exactly, which can leave some portions a cent apart.
    This is the number to *show* people; `portions_divide_evenly` says whether
    it is the whole truth.
    """
    if portions <= 1:
        return int(amount_cents)
    return round_half_up(Decimal(int(amount_cents)) / Decimal(int(portions)))


def portions_divide_evenly(amount_cents: int, portions: int) -> bool:
    return portions <= 1 or int(amount_cents) % int(portions) == 0


def _effective_shares(
    item: dict[str, Any], by_id: dict[Any, dict[str, Any]], _depth: int = 0
) -> dict[str, Any]:
    """Who is on this line.

    A sub-item has no claims of its own - a modification belongs to the dish it
    modifies, so its cost follows whoever claimed the parent. That keeps
    "tick the burger, get its bacon too" true by construction rather than by the
    UI remembering to mirror the selection.

    The depth guard is belt-and-braces: sub-items are one level deep by
    construction, but a malformed parent chain must not recurse forever.
    """
    parent_id = item.get("parent_id")
    if parent_id is not None and parent_id in by_id and _depth < 4:
        parent = by_id[parent_id]
        if parent is not item:
            return _effective_shares(parent, by_id, _depth + 1)
    return item.get("shares") or {}


def _prop_weights(
    nets: dict[str, int], parties: Sequence[str], fallback: dict[str, float]
) -> list[float]:
    """Weights for apportioning tax/tip: each person's net subtotal share.

    If the net subtotal is zero for everyone (a bill that is nothing but a
    flat tip, say) fall back to the participants' own weights.
    """
    if any(nets.get(p, 0) for p in parties):
        return [float(max(0, nets.get(p, 0))) for p in parties]
    return [float(fallback.get(p, 1) or 0) for p in parties]


def compute_bill(
    *,
    split_mode: str = "even",
    subtotal_cents: int = 0,
    participants: Iterable[dict[str, Any]] = (),
    items: Iterable[dict[str, Any]] = (),
    discount: dict[str, Any] | None = None,
    tax: dict[str, Any] | None = None,
    tip: dict[str, Any] | None = None,
    extras: Iterable[dict[str, Any]] = (),
    payments: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Compute a full bill breakdown.

    participants: [{"party": "u:1", "weight": 1}]
    items:        [{"id": 1, "label": "Steak", "amount_cents": 3200, "portions": 1,
                    "parent_id": None, "shares": {"u:1": 1}}]
                  An item with no shares is split evenly across all participants.
                  A sub-item (parent_id set) carries no shares of its own and
                  follows whoever claimed its parent.
                  `portions` divides a line into individually claimable parts;
                  a share's weight is how many of them that person took.
    discount:     {"mode": "none|percent|amount", "percent": 8.875, "cents": 350,
                   "target": "all" | "<party>"} - a party takes the whole
                  discount, and a percentage is then of their share.
    tax:          {"mode": "none|percent|amount", "percent": 8.875, "cents": 350}
    tip:          same, plus {"base": "pre_tax"|"post_tax"}
    extras:       [{"label": "Delivery", "mode": "amount", "cents": 599,
                    "percent": 0, "split": "even"|"proportional"}]
    payments:     [{"party": "u:1", "amount_cents": 8000}]
    """
    parts = list(participants)
    parties = [str(p["party"]) for p in parts]
    weights = {str(p["party"]): float(p.get("weight", 1) or 0) for p in parts}
    warnings: list[str] = []

    if not parties:
        return _empty_result(warnings=["No one is on this bill yet."])

    item_list = list(items)
    discount = discount or {}
    tax = tax or {}
    tip = tip or {}
    extra_list = list(extras)

    # --- 1. base shares ------------------------------------------------------
    base: dict[str, int] = {p: 0 for p in parties}

    if split_mode == "itemized":
        subtotal = sum(int(it.get("amount_cents", 0)) for it in item_list)
        by_id = {it["id"]: it for it in item_list if it.get("id") is not None}
        unassigned_lines = 0

        for item in item_list:
            amount = int(item.get("amount_cents", 0))
            label = item.get("label") or "An item"
            portions = max(1, int(item.get("portions") or 1))
            shares: dict[str, float] = {
                str(k): float(v) for k, v in _effective_shares(item, by_id).items()
                if str(k) in base and float(v or 0) > 0
            }

            if not shares:
                # Nobody picked this line. Sub-items follow their parent, so only
                # count the top-level lines or the message double-counts.
                if item.get("parent_id") is None:
                    unassigned_lines += 1
                for party, cents in zip(parties, allocate(amount, [1.0] * len(parties))):
                    base[party] += cents
                continue

            keys = [p for p in parties if p in shares]  # stable order

            if portions <= 1:
                # An ordinary line shared by whoever is on it.
                for party, cents in zip(keys, allocate(amount, [shares[p] for p in keys])):
                    base[party] += cents
                continue

            # A divided line has a *price per portion*, not a proportion of the
            # line. Three beers at 18.00 means a beer costs 6.00, so taking one
            # costs 6.00 - it does not mean the only person who spoke up buys
            # the round. Portions are priced with the same largest-remainder
            # split as everything else, so they still add back to the line total
            # exactly even when it does not divide evenly.
            taken = {p: int(round(shares[p])) for p in keys}
            total_taken = sum(taken.values())

            if total_taken > portions:
                # More claimed than exist. Sharing them out in those proportions
                # is the only reading that still adds up; say so rather than
                # silently inventing portions.
                warnings.append(
                    f"{label}: {total_taken} of {portions} portions claimed - more than "
                    "exist, so the line is being split in those proportions instead."
                )
                for party, cents in zip(keys, allocate(amount, [shares[p] for p in keys])):
                    base[party] += cents
                continue

            prices = allocate(amount, [1] * portions)
            cursor = 0
            for party in keys:
                count = taken[party]
                if count <= 0:
                    continue
                base[party] += sum(prices[cursor:cursor + count])
                cursor += count

            unclaimed = prices[cursor:]
            if unclaimed:
                # Portions nobody put their hand up for are treated like an
                # unclaimed line: shared by everyone, same rule as elsewhere.
                left = sum(unclaimed)
                for party, cents in zip(parties, allocate(left, [1.0] * len(parties))):
                    base[party] += cents
                warnings.append(
                    f"{label}: {len(unclaimed)} of {portions} portions "
                    f"({money(left)}) not taken by anyone - split evenly across everyone."
                )

        if unassigned_lines:
            warnings.append(
                f"{unassigned_lines} item{'s' if unassigned_lines != 1 else ''} "
                "not assigned to anyone - split evenly across everyone."
            )
        if not item_list:
            warnings.append("Itemized bill with no items yet.")
    else:
        subtotal = int(subtotal_cents)
        # "even" means even: ignore weights. "shares" honours them.
        w = [1.0] * len(parties) if split_mode == "even" else [weights[p] for p in parties]
        for party, cents in zip(parties, allocate(subtotal, w)):
            base[party] = cents

    # --- 2. discount ---------------------------------------------------------
    # A discount aimed at one person (their coupon, their comped dish) comes off
    # their share alone, and a percentage then means a percentage *of their
    # share* - which is what "20% off my meal" means to a human.
    target = str(discount.get("target") or "all")
    targeted = target in base
    discount_basis = base[target] if targeted else subtotal

    discount_cents = _amount_from(
        discount.get("mode", "none"),
        discount.get("percent", 0) or 0,
        discount.get("cents", 0) or 0,
        discount_basis,
    )
    discount_cents = max(0, discount_cents)
    if discount_cents > discount_basis and discount_basis >= 0:
        warnings.append(
            f"Discount was larger than {'their' if targeted else 'the'} share of the "
            "bill - capped so nobody ends up owing less than nothing."
            if targeted else
            "Discount was larger than the subtotal - capped at the subtotal."
        )
        discount_cents = discount_basis

    discount_share: dict[str, int] = dict.fromkeys(parties, 0)
    if discount_cents:
        if targeted:
            discount_share[target] = -discount_cents
        else:
            w = _prop_weights(base, parties, weights)
            for party, cents in zip(parties, allocate(-discount_cents, w)):
                discount_share[party] = cents

    net_subtotal = subtotal - discount_cents
    net: dict[str, int] = {p: base[p] + discount_share[p] for p in parties}

    # --- 3. tax --------------------------------------------------------------
    tax_cents = _amount_from(
        tax.get("mode", "none"), tax.get("percent", 0) or 0, tax.get("cents", 0) or 0, net_subtotal
    )
    tax_share: dict[str, int] = dict.fromkeys(parties, 0)
    if tax_cents:
        w = _prop_weights(net, parties, weights)
        for party, cents in zip(parties, allocate(tax_cents, w)):
            tax_share[party] = cents

    # --- 4. tip --------------------------------------------------------------
    tip_base_mode = tip.get("base", "pre_tax")
    tip_base_cents = net_subtotal + (tax_cents if tip_base_mode == "post_tax" else 0)
    tip_cents = _amount_from(
        tip.get("mode", "none"), tip.get("percent", 0) or 0, tip.get("cents", 0) or 0, tip_base_cents
    )
    tip_share: dict[str, int] = dict.fromkeys(parties, 0)
    if tip_cents:
        w = _prop_weights(net, parties, weights)
        for party, cents in zip(parties, allocate(tip_cents, w)):
            tip_share[party] = cents

    # --- 5. extras -----------------------------------------------------------
    extras_share: dict[str, int] = dict.fromkeys(parties, 0)
    extras_out: list[dict[str, Any]] = []
    for extra in extra_list:
        cents = _amount_from(
            extra.get("mode", "amount"),
            extra.get("percent", 0) or 0,
            extra.get("cents", 0) or 0,
            net_subtotal,
        )
        split = extra.get("split", "even")
        w = [1.0] * len(parties) if split == "even" else _prop_weights(net, parties, weights)
        per_party: dict[str, int] = {}
        for party, amount in zip(parties, allocate(cents, w)):
            extras_share[party] += amount
            per_party[party] = amount
        extras_out.append(
            {
                "id": extra.get("id"),
                "label": extra.get("label") or "Extra",
                "mode": extra.get("mode", "amount"),
                "percent": float(extra.get("percent", 0) or 0),
                "split": split,
                "cents": cents,
                "per_party": per_party,
            }
        )
    extras_total = sum(e["cents"] for e in extras_out)

    # --- 6. totals -----------------------------------------------------------
    total = net_subtotal + tax_cents + tip_cents + extras_total

    paid: dict[str, int] = dict.fromkeys(parties, 0)
    unknown_payers = 0
    for payment in payments:
        party = str(payment.get("party"))
        amount = int(payment.get("amount_cents", 0))
        if party in paid:
            paid[party] += amount
        else:
            unknown_payers += amount
    if unknown_payers:
        warnings.append(
            "A payment is recorded for someone who is no longer on this bill; "
            "it is not counted."
        )
    paid_total = sum(paid.values())

    per_party: dict[str, dict[str, int]] = {}
    for party in parties:
        owed = base[party] + discount_share[party] + tax_share[party] + tip_share[party] + extras_share[party]
        per_party[party] = {
            "base_cents": base[party],
            "discount_cents": discount_share[party],
            "net_cents": net[party],
            "tax_cents": tax_share[party],
            "tip_cents": tip_share[party],
            "extras_cents": extras_share[party],
            "owed_cents": owed,
            "paid_cents": paid[party],
            "balance_cents": paid[party] - owed,  # >0 they are owed money
        }

    # Belt-and-braces: allocation guarantees this, but a bug here is silent money loss.
    assert sum(v["owed_cents"] for v in per_party.values()) == total, "split does not sum to total"

    return {
        "subtotal_cents": subtotal,
        "discount_cents": discount_cents,
        "net_subtotal_cents": net_subtotal,
        "tax_cents": tax_cents,
        "tip_cents": tip_cents,
        "tip_base_cents": tip_base_cents,
        "extras": extras_out,
        "extras_total_cents": extras_total,
        "total_cents": total,
        "paid_total_cents": paid_total,
        "unpaid_cents": total - paid_total,
        "per_party": per_party,
        "warnings": warnings,
    }


def _empty_result(warnings: list[str] | None = None) -> dict[str, Any]:
    return {
        "subtotal_cents": 0,
        "discount_cents": 0,
        "net_subtotal_cents": 0,
        "tax_cents": 0,
        "tip_cents": 0,
        "tip_base_cents": 0,
        "extras": [],
        "extras_total_cents": 0,
        "total_cents": 0,
        "paid_total_cents": 0,
        "unpaid_cents": 0,
        "per_party": {},
        "warnings": warnings or [],
    }


# --- settling up -------------------------------------------------------------

def settle(balances: dict[str, int]) -> list[dict[str, Any]]:
    """Turn a set of balances into the fewest sensible transfers.

    balance > 0 means the party is owed money; < 0 means they owe.
    Greedy largest-debtor-pays-largest-creditor, which needs at most n-1
    transfers and is what a human would suggest anyway.
    """
    creditors = sorted(
        ((p, b) for p, b in balances.items() if b > 0), key=lambda kv: (-kv[1], kv[0])
    )
    debtors = sorted(
        ((p, -b) for p, b in balances.items() if b < 0), key=lambda kv: (-kv[1], kv[0])
    )

    transfers: list[dict[str, Any]] = []
    i = j = 0
    creditors = [list(c) for c in creditors]
    debtors = [list(d) for d in debtors]

    while i < len(debtors) and j < len(creditors):
        amount = min(debtors[i][1], creditors[j][1])
        if amount > 0:
            transfers.append(
                {"from_party": debtors[i][0], "to_party": creditors[j][0], "amount_cents": amount}
            )
        debtors[i][1] -= amount
        creditors[j][1] -= amount
        if debtors[i][1] == 0:
            i += 1
        if creditors[j][1] == 0:
            j += 1

    return transfers


def money(cents: int) -> str:
    """Format for logs and CSV; the UI does its own locale-aware formatting."""
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def parse_money(value: Any) -> int:
    """Accept '12.34', '$1,234.56', 12.34, 1234 -> integer cents.

    Raises ValueError on anything that is not a number, so a typo in the UI
    becomes a validation error rather than a silent zero.
    """
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        raise ValueError("not a valid amount")
    if isinstance(value, int):
        return value * 100
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError("not a valid amount")
        return round_half_up(Decimal(str(value)) * 100)
    text = str(value).strip().replace(",", "").replace("$", "")
    if not text:
        return 0
    try:
        return round_half_up(Decimal(text) * 100)
    except InvalidOperation as exc:
        raise ValueError(f"{value!r} is not a valid amount") from exc
