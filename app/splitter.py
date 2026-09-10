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

# A bucket, not a person: it holds lines nobody has claimed. It takes its share
# of tax and tip like anybody else, so the bill still adds up exactly, but it is
# reported on its own rather than being quietly charged to the table. Cannot
# collide with a real party key, which is always 'u:<id>' or 'g:<id>'.
UNASSIGNED = "?"


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
    unclaimed_mode: str = "unassigned",
) -> dict[str, Any]:
    """Compute a full bill breakdown.

    participants: [{"party": "u:1", "weight": 1, "exempt": False}]
                  `exempt` is the birthday rule: that person pays nothing and
                  their share is covered by everybody else.
    items:        [{"id": 1, "label": "Steak", "amount_cents": 3200, "portions": 1,
                    "parent_id": None, "shares": {"u:1": 1}}]
                  A sub-item (parent_id set) carries no shares of its own and
                  follows whoever claimed its parent.
                  `portions` divides a line into individually claimable parts;
                  a share's weight is how many of them that person took.
    unclaimed_mode: what happens to a line nobody has ticked.
                  "unassigned" (default) parks it - reported as unassigned_cents
                  and charged to nobody, because guessing that everyone shared
                  the thing is usually wrong and always invisible.
                  "even" is the old behaviour: split it across everyone.
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
    exempt = {str(p["party"]) for p in parts if p.get("exempt")}
    warnings: list[str] = []

    if not parties:
        return _empty_result(warnings=["No one is on this bill yet."])

    if exempt and len(exempt) >= len(parties):
        warnings.append(
            "Everyone on this bill is marked as not paying, so that is being "
            "ignored - somebody has to cover it."
        )
        exempt = set()
    payers = [p for p in parties if p not in exempt]

    item_list = list(items)
    discount = discount or {}
    tax = tax or {}
    tip = tip or {}
    extra_list = list(extras)
    leave_unclaimed = unclaimed_mode != "even"

    # --- 1. base shares ------------------------------------------------------
    base: dict[str, int] = {p: 0 for p in parties}
    unclaimed_cents = 0   # what ends up on the UNASSIGNED bucket
    unassigned_lines = 0  # top-level lines nobody ticked at all

    # Which lines made up each person's base, recorded as the allocation
    # happens rather than worked out again afterwards - so "why do I owe
    # $54.00" is answered by the same arithmetic that produced the $54.00.
    ledger: dict[str, list[dict[str, Any]]] = {}
    entries: dict[tuple[str, Any], dict[str, Any]] = {}
    by_id = {it["id"]: it for it in item_list if it.get("id") is not None}

    def credit(party: str, item: dict[str, Any], key: Any, cents: int, **extra: Any) -> None:
        """Put `cents` of `item` on `party`'s tab, folding sub-items into their
        parent: whoever took the burger took its bacon, and one line is what a
        human sees on the receipt."""
        entry = entries.get((party, key))
        if entry is None:
            parent = by_id.get(item.get("parent_id"))
            entry = {
                "id": key,
                "label": (parent or item).get("label") or "An item",
                "cents": 0,
                "kind": "item",
                **extra,
            }
            entries[(party, key)] = entry
            ledger.setdefault(party, []).append(entry)
        entry["cents"] += cents

    if split_mode == "itemized":
        subtotal = sum(int(it.get("amount_cents", 0)) for it in item_list)

        for position, item in enumerate(item_list):
            # One key per top-level line, so a modification lands on its parent.
            # The position is only a fallback for items with no id at all.
            line_key = item.get("parent_id")
            if line_key is None:
                line_key = item.get("id") if item.get("id") is not None else f"#{position}"
            amount = int(item.get("amount_cents", 0))
            label = item.get("label") or "An item"
            portions = max(1, int(item.get("portions") or 1))
            shares: dict[str, float] = {
                str(k): float(v) for k, v in _effective_shares(item, by_id).items()
                if str(k) in base and float(v or 0) > 0
            }

            if not shares:
                # Nobody picked this line. Sub-items follow their parent, so only
                # count the top-level lines or the message double-counts - but do
                # count their money, since an unclaimed burger takes its cheese
                # with it.
                if item.get("parent_id") is None:
                    unassigned_lines += 1
                if leave_unclaimed:
                    unclaimed_cents += amount
                    credit(UNASSIGNED, item, line_key, amount, kind="unclaimed")
                else:
                    for party, cents in zip(parties, allocate(amount, [1.0] * len(parties))):
                        base[party] += cents
                        credit(party, item, line_key, cents,
                               kind="unclaimed", sharers=len(parties))
                continue

            keys = [p for p in parties if p in shares]  # stable order

            if portions <= 1:
                # An ordinary line shared by whoever is on it.
                for party, cents in zip(keys, allocate(amount, [shares[p] for p in keys])):
                    base[party] += cents
                    credit(party, item, line_key, cents, sharers=len(keys))
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
                    credit(party, item, line_key, cents, sharers=len(keys))
                continue

            prices = allocate(amount, [1] * portions)
            cursor = 0
            for party in keys:
                count = taken[party]
                if count <= 0:
                    continue
                mine = sum(prices[cursor:cursor + count])
                base[party] += mine
                credit(party, item, line_key, mine, sharers=len(keys), units=count)
                cursor += count

            unclaimed = prices[cursor:]
            if unclaimed:
                # Portions nobody put their hand up for follow the same rule as
                # a whole unclaimed line.
                left = sum(unclaimed)
                if leave_unclaimed:
                    # Worth saying out loud even though the amount shows up in
                    # unassigned_cents: the line looks claimed, and only this
                    # says that part of it is not.
                    unclaimed_cents += left
                    credit(UNASSIGNED, item, line_key, left, kind="unclaimed",
                           units=len(unclaimed), of=portions)
                    warnings.append(
                        f"{label}: {len(unclaimed)} of {portions} portions "
                        f"({money(left)}) not taken by anyone - left unassigned."
                    )
                else:
                    for party, cents in zip(parties, allocate(left, [1.0] * len(parties))):
                        base[party] += cents
                        credit(party, item, line_key, cents, kind="unclaimed")
                    warnings.append(
                        f"{label}: {len(unclaimed)} of {portions} portions "
                        f"({money(left)}) not taken by anyone - split evenly across everyone."
                    )

        # Only a warning when the engine has actually done something to
        # people's totals. Leaving it unassigned is reported as a fact -
        # unassigned_cents and unassigned_items - which every screen shows
        # plainly, so saying it twice would just be noise.
        if unassigned_lines and not leave_unclaimed:
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
            if cents:
                ledger.setdefault(party, []).append(
                    {"id": None, "label": "", "cents": cents,
                     "kind": split_mode, "sharers": len(parties)}
                )

    # --- 1b. whoever is not paying -------------------------------------------
    # The birthday rule. Their share moves to the people who *are* paying, which
    # is what "it's on us" means. Everything downstream then follows from their
    # share being zero: tax and tip are apportioned by share, so they pick up
    # none of it and end up owing exactly nothing.
    if exempt:
        covered = sum(base[party] for party in exempt)
        for party in exempt:
            base[party] = 0
        if covered:
            spread = (
                [weights[p] for p in payers] if split_mode == "shares"
                else [1.0] * len(payers)
            )
            for party, cents in zip(payers, allocate(covered, spread)):
                base[party] += cents
                # Its own line in the ledger, so the extra on somebody's tab is
                # explained rather than looking like an item they never had.
                ledger.setdefault(party, []).append(
                    {"id": None, "label": "", "cents": cents, "kind": "covering",
                     "parties": sorted(exempt)}
                )

    # --- 1c. the unassigned bucket -------------------------------------------
    # From here on everything is apportioned across `holders`: the people, plus
    # the bucket when it holds something. The bucket behaves like a participant
    # for tax and tip - so the arithmetic still closes exactly - and is pulled
    # back out at the end to be reported on its own.
    holders = list(parties)
    if unclaimed_cents:
        holders.append(UNASSIGNED)
        base[UNASSIGNED] = unclaimed_cents

    # Weights to fall back on when there is no subtotal to apportion against.
    # Neither the bucket nor somebody who is not paying should collect a share
    # of a bill that is nothing but a flat tip.
    fallback = {p: (0.0 if p in exempt else weights[p]) for p in parties}
    fallback[UNASSIGNED] = 0.0

    # --- 2. discount ---------------------------------------------------------
    # A discount aimed at one person (their coupon, their comped dish) comes off
    # their share alone, and a percentage then means a percentage *of their
    # share* - which is what "20% off my meal" means to a human.
    target = str(discount.get("target") or "all")
    targeted = target in base and target != UNASSIGNED
    if targeted and target in exempt:
        # Their share is already zero, so there is nothing to take off. Say so
        # rather than reporting it as a discount that got capped.
        warnings.append(
            "The discount is aimed at somebody who is not paying, so it has no effect."
        )
        discount = {"mode": "none"}
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

    discount_share: dict[str, int] = dict.fromkeys(holders, 0)
    if discount_cents:
        if targeted:
            discount_share[target] = -discount_cents
        else:
            w = _prop_weights(base, holders, fallback)
            for party, cents in zip(holders, allocate(-discount_cents, w)):
                discount_share[party] = cents

    net_subtotal = subtotal - discount_cents
    net: dict[str, int] = {p: base[p] + discount_share[p] for p in holders}

    # --- 3. tax --------------------------------------------------------------
    tax_cents = _amount_from(
        tax.get("mode", "none"), tax.get("percent", 0) or 0, tax.get("cents", 0) or 0, net_subtotal
    )
    tax_share: dict[str, int] = dict.fromkeys(holders, 0)
    if tax_cents:
        w = _prop_weights(net, holders, fallback)
        for party, cents in zip(holders, allocate(tax_cents, w)):
            tax_share[party] = cents

    # --- 4. tip --------------------------------------------------------------
    tip_base_mode = tip.get("base", "pre_tax")
    tip_base_cents = net_subtotal + (tax_cents if tip_base_mode == "post_tax" else 0)
    tip_cents = _amount_from(
        tip.get("mode", "none"), tip.get("percent", 0) or 0, tip.get("cents", 0) or 0, tip_base_cents
    )
    tip_share: dict[str, int] = dict.fromkeys(holders, 0)
    if tip_cents:
        w = _prop_weights(net, holders, fallback)
        for party, cents in zip(holders, allocate(tip_cents, w)):
            tip_share[party] = cents

    # --- 5. extras -----------------------------------------------------------
    extras_share: dict[str, int] = dict.fromkeys(holders, 0)
    extras_out: list[dict[str, Any]] = []
    for extra in extra_list:
        cents = _amount_from(
            extra.get("mode", "amount"),
            extra.get("percent", 0) or 0,
            extra.get("cents", 0) or 0,
            net_subtotal,
        )
        split = extra.get("split", "even")
        if split == "even":
            # A flat fee belongs to the people at the table - not to the
            # unclaimed bucket, and not to whoever is not paying.
            w = [0.0 if (p in exempt or p == UNASSIGNED) else 1.0 for p in holders]
        else:
            w = _prop_weights(net, holders, fallback)
        per_holder: dict[str, int] = {}
        for party, amount in zip(holders, allocate(cents, w)):
            extras_share[party] += amount
            per_holder[party] = amount
        extras_out.append(
            {
                "id": extra.get("id"),
                "label": extra.get("label") or "Extra",
                "mode": extra.get("mode", "amount"),
                "percent": float(extra.get("percent", 0) or 0),
                "split": split,
                "cents": cents,
                # People only; the bucket's slice is reported as unassigned.
                "per_party": {k: v for k, v in per_holder.items() if k != UNASSIGNED},
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

    def owed_by(party: str) -> int:
        return (
            base[party] + discount_share[party] + tax_share[party]
            + tip_share[party] + extras_share[party]
        )

    per_party: dict[str, dict[str, Any]] = {}
    for party in parties:
        owed = owed_by(party)
        lines = ledger.get(party, [])
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
            "exempt": party in exempt,
            # What made up base_cents, line by line. For somebody who is not
            # paying this is still what they had - it is simply on everyone
            # else's tab, which is what `exempt` says.
            "lines": lines,
        }
        # The ledger is written by the same code that moves the money, so a
        # mismatch means one of the two branches forgot the other.
        assert party in exempt or sum(x["cents"] for x in lines) == base[party], (
            "the per-person line ledger does not add up to their base"
        )

    unassigned = owed_by(UNASSIGNED) if UNASSIGNED in holders else 0

    # Belt-and-braces: allocation guarantees this, but a bug here is silent money loss.
    assert sum(v["owed_cents"] for v in per_party.values()) + unassigned == total, (
        "split does not sum to total"
    )

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
        # Nobody's yet: the unclaimed lines plus their share of tax and tip.
        "unassigned_cents": unassigned,
        # How many lines are in that bucket. Zero when they were shared out
        # instead, so the pair always describes the same thing.
        "unassigned_items": unassigned_lines if leave_unclaimed else 0,
        "unassigned_lines": ledger.get(UNASSIGNED, []),
        "exempt_parties": sorted(exempt),
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
        "unassigned_cents": 0,
        "unassigned_items": 0,
        "unassigned_lines": [],
        "exempt_parties": [],
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
