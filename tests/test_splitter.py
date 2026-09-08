"""Tests for the split engine. Run: python -m pytest -q   (or python tests/test_splitter.py)

The invariant that matters most: whatever the bill looks like, the sum of what
each person owes equals the bill total, to the cent.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.splitter import allocate, compute_bill, money, parse_money, percent_of, settle  # noqa: E402


def P(*keys, weight=1):
    return [{"party": k, "weight": weight} for k in keys]


# --- allocate ----------------------------------------------------------------

def test_allocate_sums_exactly():
    assert sum(allocate(1000, [1, 1, 1])) == 1000
    assert allocate(1000, [1, 1, 1]) == [334, 333, 333]


def test_allocate_penny_goes_to_largest_remainder():
    # $39.99 across 4 -> 9.99, 10.00, 10.00, 10.00 in some order, summing to 3999
    parts = allocate(3999, [1, 1, 1, 1])
    assert sum(parts) == 3999
    assert sorted(parts) == [999, 1000, 1000, 1000]


def test_allocate_weighted():
    assert allocate(1000, [3, 1]) == [750, 250]
    assert sum(allocate(1001, [3, 1])) == 1001


def test_allocate_zero_weights_falls_back_to_even():
    assert allocate(300, [0, 0, 0]) == [100, 100, 100]


def test_allocate_negative_total():
    out = allocate(-1000, [1, 1, 1])
    assert sum(out) == -1000
    assert all(v <= 0 for v in out)


def test_allocate_edge_cases():
    assert allocate(0, [1, 2]) == [0, 0]
    assert allocate(100, []) == []
    assert sum(allocate(1, [1, 1, 1])) == 1


def test_allocate_fractional_weights():
    out = allocate(10000, [33.33, 33.33, 33.34])
    assert sum(out) == 10000


# --- percent -----------------------------------------------------------------

def test_percent_half_up_and_precision():
    assert percent_of(4130, 8.875) == 367          # 366.5375 -> 367
    assert percent_of(1000, 10) == 100
    assert percent_of(1050, 10) == 105
    assert percent_of(105, 10) == 11               # 10.5 -> 11, not 10 (float would give 10)
    assert percent_of(0, 20) == 0


# --- whole bills -------------------------------------------------------------

def test_even_split_with_percent_tax_and_tip():
    r = compute_bill(
        split_mode="even",
        subtotal_cents=10000,
        participants=P("u:1", "u:2", "u:3"),
        tax={"mode": "percent", "percent": 8.875},
        tip={"mode": "percent", "percent": 20, "base": "pre_tax"},
    )
    assert r["tax_cents"] == 888          # 887.5 -> 888
    assert r["tip_cents"] == 2000
    assert r["total_cents"] == 12888
    assert sum(v["owed_cents"] for v in r["per_party"].values()) == 12888


def test_tip_on_post_tax_base():
    r = compute_bill(
        split_mode="even",
        subtotal_cents=10000,
        participants=P("u:1", "u:2"),
        tax={"mode": "percent", "percent": 10},
        tip={"mode": "percent", "percent": 20, "base": "post_tax"},
    )
    assert r["tax_cents"] == 1000
    assert r["tip_cents"] == 2200         # 20% of 11000
    assert r["total_cents"] == 13200


def test_manual_tax_and_tip_amounts():
    r = compute_bill(
        split_mode="even",
        subtotal_cents=5000,
        participants=P("u:1", "u:2"),
        tax={"mode": "amount", "cents": 437},
        tip={"mode": "amount", "cents": 1000},
    )
    assert r["tax_cents"] == 437
    assert r["tip_cents"] == 1000
    assert r["total_cents"] == 6437
    assert sum(v["owed_cents"] for v in r["per_party"].values()) == 6437


def test_itemized_tax_and_tip_follow_what_you_ordered():
    # Steak eater pays proportionally more tax and tip than the salad eater.
    r = compute_bill(
        split_mode="itemized",
        participants=P("u:1", "u:2"),
        items=[
            {"label": "Steak", "amount_cents": 4000, "shares": {"u:1": 1}},
            {"label": "Salad", "amount_cents": 1000, "shares": {"u:2": 1}},
        ],
        tax={"mode": "percent", "percent": 10},
        tip={"mode": "percent", "percent": 20},
    )
    assert r["subtotal_cents"] == 5000
    assert r["total_cents"] == 5000 + 500 + 1000
    a, b = r["per_party"]["u:1"], r["per_party"]["u:2"]
    assert a["base_cents"] == 4000 and b["base_cents"] == 1000
    assert a["tax_cents"] == 400 and b["tax_cents"] == 100
    assert a["tip_cents"] == 800 and b["tip_cents"] == 200
    assert a["owed_cents"] + b["owed_cents"] == r["total_cents"]


def test_itemized_shared_item():
    r = compute_bill(
        split_mode="itemized",
        participants=P("u:1", "u:2", "u:3"),
        items=[{"label": "Nachos", "amount_cents": 1000, "shares": {"u:1": 1, "u:2": 1}}],
    )
    assert r["per_party"]["u:1"]["base_cents"] == 500
    assert r["per_party"]["u:2"]["base_cents"] == 500
    assert r["per_party"]["u:3"]["base_cents"] == 0


def test_itemized_unassigned_item_is_split_and_warned():
    r = compute_bill(
        split_mode="itemized",
        participants=P("u:1", "u:2"),
        items=[{"label": "Bottle of wine", "amount_cents": 3000, "shares": {}}],
    )
    assert r["per_party"]["u:1"]["base_cents"] == 1500
    assert r["per_party"]["u:2"]["base_cents"] == 1500
    assert any("not assigned" in w for w in r["warnings"])


def test_shares_mode_weights():
    # A couple sharing an Airbnb room counts as 2 shares.
    r = compute_bill(
        split_mode="shares",
        subtotal_cents=90000,
        participants=[
            {"party": "u:1", "weight": 2},
            {"party": "u:2", "weight": 1},
        ],
    )
    assert r["per_party"]["u:1"]["base_cents"] == 60000
    assert r["per_party"]["u:2"]["base_cents"] == 30000


def test_even_mode_ignores_weights():
    r = compute_bill(
        split_mode="even",
        subtotal_cents=1000,
        participants=[{"party": "u:1", "weight": 5}, {"party": "u:2", "weight": 1}],
    )
    assert r["per_party"]["u:1"]["base_cents"] == 500


def test_extras_even_and_proportional():
    r = compute_bill(
        split_mode="itemized",
        participants=P("u:1", "u:2"),
        items=[
            {"amount_cents": 8000, "shares": {"u:1": 1}},
            {"amount_cents": 2000, "shares": {"u:2": 1}},
        ],
        extras=[
            {"label": "Delivery", "mode": "amount", "cents": 500, "split": "even"},
            {"label": "Service", "mode": "percent", "percent": 10, "split": "proportional"},
        ],
    )
    assert r["extras_total_cents"] == 500 + 1000
    # delivery split evenly, service charge follows the order sizes
    assert r["per_party"]["u:1"]["extras_cents"] == 250 + 800
    assert r["per_party"]["u:2"]["extras_cents"] == 250 + 200
    assert r["total_cents"] == 11500


def test_discount_percent_then_tax():
    r = compute_bill(
        split_mode="even",
        subtotal_cents=10000,
        participants=P("u:1", "u:2"),
        discount={"mode": "percent", "percent": 25},
        tax={"mode": "percent", "percent": 10},
    )
    assert r["discount_cents"] == 2500
    assert r["net_subtotal_cents"] == 7500
    assert r["tax_cents"] == 750
    assert r["total_cents"] == 8250
    assert sum(v["owed_cents"] for v in r["per_party"].values()) == 8250


def test_discount_larger_than_subtotal_is_capped():
    r = compute_bill(
        split_mode="even",
        subtotal_cents=1000,
        participants=P("u:1"),
        discount={"mode": "amount", "cents": 5000},
    )
    assert r["discount_cents"] == 1000
    assert r["total_cents"] == 0
    assert any("capped" in w for w in r["warnings"])


def test_flat_tip_with_zero_subtotal_still_distributes():
    r = compute_bill(
        split_mode="even",
        subtotal_cents=0,
        participants=P("u:1", "u:2"),
        tip={"mode": "amount", "cents": 1000},
    )
    assert r["total_cents"] == 1000
    assert r["per_party"]["u:1"]["owed_cents"] == 500


def test_payments_and_balances():
    r = compute_bill(
        split_mode="even",
        subtotal_cents=6000,
        participants=P("u:1", "u:2", "u:3"),
        payments=[{"party": "u:1", "amount_cents": 6000}],
    )
    assert r["paid_total_cents"] == 6000
    assert r["unpaid_cents"] == 0
    assert r["per_party"]["u:1"]["balance_cents"] == 4000    # is owed 40
    assert r["per_party"]["u:2"]["balance_cents"] == -2000   # owes 20


def test_payment_from_someone_not_on_the_bill_is_flagged():
    r = compute_bill(
        split_mode="even",
        subtotal_cents=1000,
        participants=P("u:1"),
        payments=[{"party": "g:9", "amount_cents": 1000}],
    )
    assert r["paid_total_cents"] == 0
    assert any("no longer on this bill" in w for w in r["warnings"])


def test_no_participants_is_not_a_crash():
    r = compute_bill(split_mode="even", subtotal_cents=1000, participants=[])
    assert r["total_cents"] == 0
    assert r["warnings"]


def test_everything_at_once_still_sums():
    r = compute_bill(
        split_mode="itemized",
        participants=[
            {"party": "u:1", "weight": 1},
            {"party": "u:2", "weight": 1},
            {"party": "g:1", "weight": 1},
        ],
        items=[
            {"amount_cents": 1799, "shares": {"u:1": 1}},
            {"amount_cents": 2350, "shares": {"u:2": 1}},
            {"amount_cents": 1425, "shares": {"g:1": 1}},
            {"amount_cents": 1299, "shares": {"u:1": 1, "u:2": 1, "g:1": 1}},
            {"amount_cents": 777, "shares": {}},
        ],
        discount={"mode": "percent", "percent": 7.5},
        tax={"mode": "percent", "percent": 8.875},
        tip={"mode": "percent", "percent": 18, "base": "post_tax"},
        extras=[
            {"label": "Corkage", "mode": "amount", "cents": 1500, "split": "even"},
            {"label": "Service", "mode": "percent", "percent": 3.5, "split": "proportional"},
        ],
        payments=[{"party": "u:1", "amount_cents": 5000}, {"party": "u:2", "amount_cents": 5000}],
    )
    assert sum(v["owed_cents"] for v in r["per_party"].values()) == r["total_cents"]
    assert sum(v["balance_cents"] for v in r["per_party"].values()) == r["paid_total_cents"] - r["total_cents"]


def test_no_pennies_lost_across_many_random_bills():
    import random

    rng = random.Random(1234)
    for _ in range(400):
        n = rng.randint(1, 6)
        parties = [f"u:{i}" for i in range(n)]
        mode = rng.choice(["even", "shares", "itemized"])
        items = [
            {
                "amount_cents": rng.randint(1, 9999),
                "shares": {p: 1 for p in parties if rng.random() < 0.6},
            }
            for _ in range(rng.randint(0, 6))
        ]
        r = compute_bill(
            split_mode=mode,
            subtotal_cents=rng.randint(0, 500000),
            participants=[{"party": p, "weight": rng.choice([1, 1, 2, 0.5, 3])} for p in parties],
            items=items,
            discount={"mode": rng.choice(["none", "percent", "amount"]),
                      "percent": rng.uniform(0, 30), "cents": rng.randint(0, 3000)},
            tax={"mode": rng.choice(["none", "percent", "amount"]),
                 "percent": rng.uniform(0, 15), "cents": rng.randint(0, 3000)},
            tip={"mode": rng.choice(["none", "percent", "amount"]),
                 "percent": rng.uniform(0, 30), "cents": rng.randint(0, 3000),
                 "base": rng.choice(["pre_tax", "post_tax"])},
            extras=[{"label": "x", "mode": rng.choice(["percent", "amount"]),
                     "percent": rng.uniform(0, 10), "cents": rng.randint(0, 2000),
                     "split": rng.choice(["even", "proportional"])}
                    for _ in range(rng.randint(0, 3))],
        )
        assert sum(v["owed_cents"] for v in r["per_party"].values()) == r["total_cents"]


# --- settling up -------------------------------------------------------------

def test_settle_simple():
    out = settle({"u:1": 4000, "u:2": -2000, "u:3": -2000})
    assert len(out) == 2
    assert all(t["to_party"] == "u:1" for t in out)
    assert sum(t["amount_cents"] for t in out) == 4000


def test_settle_minimises_transfers():
    # 4 people, balanced -> at most 3 transfers
    out = settle({"a": 5000, "b": -1000, "c": -1500, "d": -2500})
    assert len(out) <= 3
    assert sum(t["amount_cents"] for t in out) == 5000


def test_settle_all_square():
    assert settle({"a": 0, "b": 0}) == []


def test_settle_is_deterministic():
    balances = {"a": 3000, "b": 3000, "c": -6000}
    assert settle(balances) == settle(balances)


# --- money helpers -----------------------------------------------------------

def test_parse_money():
    assert parse_money("12.34") == 1234
    assert parse_money("$1,234.56") == 123456
    assert parse_money(12.34) == 1234
    assert parse_money(10) == 1000
    assert parse_money("") == 0
    assert parse_money(None) == 0
    assert parse_money("0.005") == 1        # half-up
    assert parse_money("-5.00") == -500


def test_parse_money_rejects_garbage():
    for bad in ["abc", "1.2.3", "twelve", True]:
        try:
            parse_money(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should not parse")


def test_money_format():
    assert money(1234) == "12.34"
    assert money(-50) == "-0.50"
    assert money(0) == "0.00"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok   {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"  FAIL {name}: {exc.__class__.__name__}: {exc}")
    print("\nall passed" if not failures else f"\n{failures} failed")
    sys.exit(1 if failures else 0)
