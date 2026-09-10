"""Tests for the split engine. Run: python -m pytest -q   (or python tests/test_splitter.py)

The invariant that matters most: whatever the bill looks like, the sum of what
each person owes equals the bill total, to the cent.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.splitter import (  # noqa: E402
    allocate, compute_bill, money, parse_money, percent_of, portion_price,
    portions_divide_evenly, settle,
)


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


def test_an_item_nobody_picked_is_left_unassigned():
    """The default. Charging everyone for a line nobody ticked is a guess, and
    an invisible one - people's totals move without anything saying why."""
    r = compute_bill(
        split_mode="itemized",
        participants=P("u:1", "u:2"),
        items=[{"label": "Bottle of wine", "amount_cents": 3000, "shares": {}}],
    )
    assert r["per_party"]["u:1"]["base_cents"] == 0
    assert r["per_party"]["u:2"]["base_cents"] == 0
    assert r["unassigned_cents"] == 3000
    assert r["unassigned_items"] == 1
    assert r["total_cents"] == 3000, "the bill still totals what the receipt says"
    assert not r["warnings"], (
        "leaving it unassigned is a reported fact, not something to warn about - "
        "every screen shows unassigned_cents plainly"
    )


def test_unclaimed_can_still_be_split_evenly_on_request():
    r = compute_bill(
        split_mode="itemized",
        participants=P("u:1", "u:2"),
        items=[{"label": "Bottle of wine", "amount_cents": 3000, "shares": {}}],
        unclaimed_mode="even",
    )
    assert r["per_party"]["u:1"]["base_cents"] == 1500
    assert r["per_party"]["u:2"]["base_cents"] == 1500
    assert r["unassigned_cents"] == 0
    assert r["unassigned_items"] == 0, "nothing is unassigned when it was shared out"
    assert any("not assigned" in w for w in r["warnings"])


def test_unassigned_carries_its_own_share_of_tax_and_tip():
    """The unclaimed money is not tax-free: it takes its slice like anyone
    else, so what the people owe plus what is unassigned is exactly the total."""
    r = compute_bill(
        split_mode="itemized",
        participants=P("u:1", "u:2"),
        items=[
            {"label": "Steak", "amount_cents": 3000, "shares": {"u:1": 1}},
            {"label": "Wine", "amount_cents": 1000, "shares": {}},
        ],
        tax={"mode": "percent", "percent": 10},
    )
    assert r["per_party"]["u:1"]["tax_cents"] == 300
    assert r["per_party"]["u:2"]["tax_cents"] == 0
    assert r["unassigned_cents"] == 1100          # 1000 + its 10% tax
    assert sum(v["owed_cents"] for v in r["per_party"].values()) == 3300
    assert r["total_cents"] == 4400


def test_an_even_split_fee_stays_with_the_people():
    """A delivery fee is split between the people at the table - the unclaimed
    bucket is not a person and does not chip in for it."""
    r = compute_bill(
        split_mode="itemized",
        participants=P("u:1", "u:2"),
        items=[{"label": "Wine", "amount_cents": 1000, "shares": {}}],
        extras=[{"label": "Delivery", "mode": "amount", "cents": 600, "split": "even"}],
    )
    assert r["per_party"]["u:1"]["owed_cents"] == 300
    assert r["per_party"]["u:2"]["owed_cents"] == 300
    assert r["unassigned_cents"] == 1000


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
    owed = sum(v["owed_cents"] for v in r["per_party"].values())
    assert owed + r["unassigned_cents"] == r["total_cents"]
    assert sum(v["balance_cents"] for v in r["per_party"].values()) == r["paid_total_cents"] - owed


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
            unclaimed_mode=rng.choice(["unassigned", "even"]),
        )
        assert (
            sum(v["owed_cents"] for v in r["per_party"].values()) + r["unassigned_cents"]
            == r["total_cents"]
        )


# --- sub-items, divided items, targeted discounts ----------------------------

def test_sub_items_follow_whoever_claimed_the_parent():
    # Ticking the burger has to pick up its bacon: a modification belongs to
    # the dish, not to the table.
    r = compute_bill(
        split_mode="itemized",
        participants=P("u:1", "u:2", "u:3"),
        items=[
            {"id": 1, "parent_id": None, "label": "Burger", "amount_cents": 1200, "shares": {"u:1": 1}},
            {"id": 2, "parent_id": 1, "label": "Add bacon", "amount_cents": 200, "shares": {}},
            {"id": 3, "parent_id": 1, "label": "No onions", "amount_cents": 0, "shares": {}},
            {"id": 4, "parent_id": None, "label": "Salad", "amount_cents": 900, "shares": {"u:2": 1}},
        ],
    )
    assert r["subtotal_cents"] == 2300
    assert r["per_party"]["u:1"]["base_cents"] == 1400   # burger + bacon
    assert r["per_party"]["u:2"]["base_cents"] == 900
    assert r["per_party"]["u:3"]["base_cents"] == 0
    assert not r["warnings"]


def test_sub_items_of_a_shared_parent_are_shared_too():
    r = compute_bill(
        split_mode="itemized",
        participants=P("u:1", "u:2"),
        items=[
            {"id": 1, "parent_id": None, "amount_cents": 2000, "shares": {"u:1": 1, "u:2": 1}},
            {"id": 2, "parent_id": 1, "amount_cents": 400, "shares": {}},
        ],
    )
    assert r["per_party"]["u:1"]["base_cents"] == 1200
    assert r["per_party"]["u:2"]["base_cents"] == 1200


def test_an_unclaimed_parent_takes_its_sub_items_with_it_and_counts_once():
    # Two rows are unassigned, but it is one line as far as a human is concerned.
    items = [
        {"id": 1, "parent_id": None, "label": "Burger", "amount_cents": 1000, "shares": {}},
        {"id": 2, "parent_id": 1, "label": "Add cheese", "amount_cents": 200, "shares": {}},
    ]
    r = compute_bill(split_mode="itemized", participants=P("u:1", "u:2"), items=items)
    assert r["per_party"]["u:1"]["base_cents"] == 0
    assert r["per_party"]["u:2"]["base_cents"] == 0
    assert r["unassigned_cents"] == 1200, "the cheese goes unassigned with its burger"
    assert r["unassigned_items"] == 1, "a modification is not an item of its own"

    r = compute_bill(split_mode="itemized", participants=P("u:1", "u:2"), items=items,
                     unclaimed_mode="even")
    assert r["per_party"]["u:1"]["base_cents"] == 600
    assert sum("not assigned" in w for w in r["warnings"]) == 1
    assert "1 item" in " ".join(r["warnings"])


def test_a_divided_item_splits_by_portions_taken():
    # Three beers on one line: two for one person, one for another.
    r = compute_bill(
        split_mode="itemized",
        participants=P("u:1", "u:2", "u:3"),
        items=[{"id": 1, "parent_id": None, "label": "Beer", "amount_cents": 1800,
                "portions": 3, "shares": {"u:1": 2, "u:2": 1}}],
    )
    assert r["per_party"]["u:1"]["base_cents"] == 1200
    assert r["per_party"]["u:2"]["base_cents"] == 600
    assert r["per_party"]["u:3"]["base_cents"] == 0
    assert not r["warnings"]


def test_a_portion_costs_a_portion_not_the_whole_line():
    """Dividing sets a price per portion. Taking one of three beers at 18.00
    costs 6.00 - speaking up first must not buy you the whole round. The
    portions nobody claimed fall back to the usual unclaimed rule."""
    line = {"id": 1, "parent_id": None, "label": "Beer", "amount_cents": 1800,
            "portions": 3, "shares": {"u:1": 1}}

    r = compute_bill(split_mode="itemized", participants=P("u:1", "u:2"), items=[line])
    # 6.00 for the beer they took; the two nobody claimed are left unassigned.
    assert r["per_party"]["u:1"]["base_cents"] == 600
    assert r["per_party"]["u:2"]["base_cents"] == 0
    assert r["unassigned_cents"] == 1200
    assert any("2 of 3 portions" in w and "not taken" in w for w in r["warnings"])

    r = compute_bill(split_mode="itemized", participants=P("u:1", "u:2"), items=[line],
                     unclaimed_mode="even")
    # Same beer, but now the spare two are shared out.
    assert r["per_party"]["u:1"]["base_cents"] == 600 + 600
    assert r["per_party"]["u:2"]["base_cents"] == 600
    assert sum(v["owed_cents"] for v in r["per_party"].values()) == r["total_cents"]


def test_portions_are_exact_to_the_cent_when_they_do_not_divide_evenly():
    # 10.00 into 3 is 3.33 and a third. Someone has to carry the odd cent, and
    # the line still has to come to exactly 10.00.
    r = compute_bill(
        split_mode="itemized",
        participants=P("u:1", "u:2", "u:3"),
        items=[{"id": 1, "parent_id": None, "amount_cents": 1000, "portions": 3,
                "shares": {"u:1": 1, "u:2": 1, "u:3": 1}}],
    )
    shares = sorted(v["base_cents"] for v in r["per_party"].values())
    assert shares == [333, 333, 334]
    assert sum(shares) == 1000
    assert not r["warnings"], "every portion was taken, so nothing to warn about"

    assert portion_price(1000, 3) == 333        # the number to show people
    assert portion_price(1800, 3) == 600
    assert portions_divide_evenly(1800, 3) is True
    assert portions_divide_evenly(1000, 3) is False


def test_two_people_sharing_one_portion_of_a_divided_line():
    """Eight slices, and two people had three each - the rest went spare."""
    r = compute_bill(
        split_mode="itemized",
        participants=P("u:1", "u:2", "u:3"),
        items=[{"id": 1, "parent_id": None, "label": "Pizza", "amount_cents": 2400,
                "portions": 8, "shares": {"u:1": 3, "u:2": 3}}],
        unclaimed_mode="even",
    )
    # 300 a slice: three slices each, and the two spare split three ways.
    assert r["per_party"]["u:1"]["base_cents"] == 900 + 200
    assert r["per_party"]["u:2"]["base_cents"] == 900 + 200
    assert r["per_party"]["u:3"]["base_cents"] == 200
    assert sum(v["owed_cents"] for v in r["per_party"].values()) == r["total_cents"]


def test_over_claiming_portions_warns_but_still_balances():
    r = compute_bill(
        split_mode="itemized",
        participants=P("u:1", "u:2"),
        items=[{"id": 1, "parent_id": None, "label": "Beer", "amount_cents": 1800,
                "portions": 3, "shares": {"u:1": 2, "u:2": 2}}],
    )
    assert any("more than exist" in w for w in r["warnings"])
    assert r["per_party"]["u:1"]["base_cents"] == 900
    assert sum(v["owed_cents"] for v in r["per_party"].values()) == r["total_cents"]


def test_divided_item_with_sub_items():
    """A pizza divided four ways, with a topping charge that rides along."""
    r = compute_bill(
        split_mode="itemized",
        participants=P("u:1", "u:2", "u:3"),
        items=[
            {"id": 1, "parent_id": None, "label": "Pizza", "amount_cents": 2000,
             "portions": 4, "shares": {"u:1": 3, "u:2": 1}},
            {"id": 2, "parent_id": 1, "label": "Extra cheese", "amount_cents": 400, "shares": {}},
        ],
    )
    # Both rows follow the same 3:1 split.
    assert r["per_party"]["u:1"]["base_cents"] == 1500 + 300
    assert r["per_party"]["u:2"]["base_cents"] == 500 + 100
    assert r["per_party"]["u:3"]["base_cents"] == 0


def test_targeted_discount_comes_off_one_person():
    r = compute_bill(
        split_mode="even", subtotal_cents=9000, participants=P("u:1", "u:2", "u:3"),
        discount={"mode": "amount", "cents": 1500, "target": "u:2"},
        tax={"mode": "percent", "percent": 10},
    )
    assert r["discount_cents"] == 1500
    assert r["per_party"]["u:2"]["discount_cents"] == -1500
    assert r["per_party"]["u:1"]["discount_cents"] == 0
    assert r["per_party"]["u:3"]["discount_cents"] == 0
    # Their tax drops with their share; the others' does not.
    assert r["per_party"]["u:2"]["owed_cents"] == 1650
    assert r["per_party"]["u:1"]["owed_cents"] == 3300
    assert sum(v["owed_cents"] for v in r["per_party"].values()) == r["total_cents"]


def test_targeted_percent_discount_is_a_percent_of_their_share():
    r = compute_bill(
        split_mode="even", subtotal_cents=9000, participants=P("u:1", "u:2", "u:3"),
        discount={"mode": "percent", "percent": 50, "target": "u:1"},
    )
    assert r["discount_cents"] == 1500      # half of their 30.00, not of 90.00
    assert r["per_party"]["u:1"]["owed_cents"] == 1500
    assert r["per_party"]["u:2"]["owed_cents"] == 3000


def test_targeted_discount_is_capped_at_that_persons_share():
    r = compute_bill(
        split_mode="even", subtotal_cents=3000, participants=P("u:1", "u:2", "u:3"),
        discount={"mode": "amount", "cents": 5000, "target": "u:1"},
    )
    assert r["per_party"]["u:1"]["owed_cents"] == 0, "never below zero"
    assert r["per_party"]["u:2"]["owed_cents"] == 1000, "others are unaffected"
    assert any("capped" in w for w in r["warnings"])
    assert sum(v["owed_cents"] for v in r["per_party"].values()) == r["total_cents"]


def test_targeted_discount_on_an_itemized_bill():
    """The classic: one person had a coupon for their own dish."""
    r = compute_bill(
        split_mode="itemized",
        participants=P("u:1", "u:2"),
        items=[
            {"id": 1, "parent_id": None, "amount_cents": 4000, "shares": {"u:1": 1}},
            {"id": 2, "parent_id": None, "amount_cents": 2000, "shares": {"u:2": 1}},
        ],
        discount={"mode": "amount", "cents": 1000, "target": "u:1"},
        tax={"mode": "percent", "percent": 10},
    )
    assert r["net_subtotal_cents"] == 5000
    assert r["per_party"]["u:1"]["net_cents"] == 3000
    assert r["per_party"]["u:2"]["net_cents"] == 2000
    assert r["per_party"]["u:1"]["tax_cents"] == 300
    assert r["per_party"]["u:2"]["tax_cents"] == 200
    assert sum(v["owed_cents"] for v in r["per_party"].values()) == r["total_cents"]


def test_an_unknown_discount_target_falls_back_to_everyone():
    r = compute_bill(
        split_mode="even", subtotal_cents=3000, participants=P("u:1", "u:2", "u:3"),
        discount={"mode": "amount", "cents": 300, "target": "u:999"},
    )
    assert all(v["discount_cents"] == -100 for v in r["per_party"].values())


def test_nothing_lost_with_sub_items_portions_and_targeted_discounts():
    import random

    rng = random.Random(99)
    for _ in range(400):
        n = rng.randint(1, 5)
        parties = [f"u:{i}" for i in range(n)]
        items = []
        next_id = 1
        for _ in range(rng.randint(0, 5)):
            parent_id = next_id
            next_id += 1
            portions = rng.choice([1, 1, 2, 3, 4])
            takers = [p for p in parties if rng.random() < 0.6]
            items.append({
                "id": parent_id, "parent_id": None,
                "amount_cents": rng.randint(1, 9999), "portions": portions,
                "shares": {p: rng.randint(1, portions) for p in takers},
            })
            for _ in range(rng.randint(0, 2)):
                items.append({
                    "id": next_id, "parent_id": parent_id,
                    "amount_cents": rng.randint(0, 800), "portions": 1, "shares": {},
                })
                next_id += 1

        r = compute_bill(
            split_mode="itemized",
            participants=[
                {"party": p, "weight": 1, "exempt": rng.random() < 0.2} for p in parties
            ],
            items=items,
            discount={
                "mode": rng.choice(["none", "percent", "amount"]),
                "percent": rng.uniform(0, 60), "cents": rng.randint(0, 4000),
                "target": rng.choice(["all"] + parties),
            },
            tax={"mode": "percent", "percent": rng.uniform(0, 15)},
            tip={"mode": "percent", "percent": rng.uniform(0, 25),
                 "base": rng.choice(["pre_tax", "post_tax"])},
            extras=[{"label": "x", "mode": "amount", "cents": rng.randint(0, 2000),
                     "split": rng.choice(["even", "proportional"])}],
            unclaimed_mode=rng.choice(["unassigned", "even"]),
        )
        assert (
            sum(v["owed_cents"] for v in r["per_party"].values()) + r["unassigned_cents"]
            == r["total_cents"]
        )
        assert all(v["owed_cents"] >= 0 for v in r["per_party"].values()), (
            "a discount must never push somebody below zero"
        )
        assert all(
            r["per_party"][p]["owed_cents"] == 0 for p in r["exempt_parties"]
        ), "somebody who is not paying must never owe a cent"


# --- what one person's figure is made of -------------------------------------

def test_each_person_gets_the_lines_behind_their_share():
    r = compute_bill(
        split_mode="itemized",
        participants=P("u:1", "u:2", "u:3"),
        items=[
            {"id": 1, "parent_id": None, "label": "Burger", "amount_cents": 1200,
             "shares": {"u:1": 1}},
            {"id": 2, "parent_id": 1, "label": "Add bacon", "amount_cents": 200, "shares": {}},
            {"id": 3, "parent_id": None, "label": "Nachos", "amount_cents": 1000,
             "shares": {"u:1": 1, "u:2": 1}},
            {"id": 4, "parent_id": None, "label": "Wine", "amount_cents": 3000, "shares": {}},
        ],
        tax={"mode": "percent", "percent": 10},
    )

    jon = r["per_party"]["u:1"]["lines"]
    assert [(x["label"], x["cents"]) for x in jon] == [
        ("Burger", 1400),   # the bacon is folded into the burger, not its own line
        ("Nachos", 500),
    ]
    assert next(x for x in jon if x["label"] == "Nachos")["sharers"] == 2
    assert sum(x["cents"] for x in jon) == r["per_party"]["u:1"]["base_cents"]

    assert r["per_party"]["u:3"]["lines"] == [], "he had nothing"

    # And the unclaimed bucket lists what is still going spare.
    assert [(x["label"], x["cents"], x["kind"]) for x in r["unassigned_lines"]] == [
        ("Wine", 3000, "unclaimed"),
    ]


def test_covering_somebody_is_its_own_line_not_a_mystery_item():
    r = compute_bill(
        split_mode="itemized",
        participants=[
            {"party": "u:1", "weight": 1, "exempt": True},
            {"party": "u:2", "weight": 1},
        ],
        items=[
            {"id": 1, "parent_id": None, "label": "Cake", "amount_cents": 2000,
             "shares": {"u:1": 1}},
            {"id": 2, "parent_id": None, "label": "Tea", "amount_cents": 400,
             "shares": {"u:2": 1}},
        ],
    )
    sam = r["per_party"]["u:2"]["lines"]
    assert [(x["kind"], x["cents"]) for x in sam] == [("item", 400), ("covering", 2000)]
    assert sam[1]["parties"] == ["u:1"]

    # The birthday person keeps the record of what they had, even though it is
    # on everybody else's tab - that is the useful thing to be able to open.
    assert [(x["label"], x["cents"]) for x in r["per_party"]["u:1"]["lines"]] == [("Cake", 2000)]
    assert r["per_party"]["u:1"]["owed_cents"] == 0


# --- the birthday rule -------------------------------------------------------

def test_the_birthday_person_pays_nothing_and_everyone_else_covers_it():
    r = compute_bill(
        split_mode="itemized",
        participants=[
            {"party": "u:1", "weight": 1, "exempt": True},   # it's their birthday
            {"party": "u:2", "weight": 1},
            {"party": "u:3", "weight": 1},
        ],
        items=[
            {"label": "Steak", "amount_cents": 3000, "shares": {"u:1": 1}},
            {"label": "Salad", "amount_cents": 1000, "shares": {"u:2": 1}},
            {"label": "Pasta", "amount_cents": 2000, "shares": {"u:3": 1}},
        ],
    )
    assert r["per_party"]["u:1"]["owed_cents"] == 0
    # Their 30.00 steak is split between the other two, on top of their own.
    assert r["per_party"]["u:2"]["owed_cents"] == 1000 + 1500
    assert r["per_party"]["u:3"]["owed_cents"] == 2000 + 1500
    assert r["total_cents"] == 6000
    assert r["exempt_parties"] == ["u:1"]


def test_the_birthday_person_pays_no_tax_tip_or_fees():
    r = compute_bill(
        split_mode="itemized",
        participants=[
            {"party": "u:1", "weight": 1, "exempt": True},
            {"party": "u:2", "weight": 1},
        ],
        items=[{"label": "Cake", "amount_cents": 2000, "shares": {"u:1": 1}}],
        tax={"mode": "percent", "percent": 10},
        tip={"mode": "percent", "percent": 20},
        extras=[{"label": "Corkage", "mode": "amount", "cents": 500, "split": "even"}],
    )
    assert r["per_party"]["u:1"]["owed_cents"] == 0
    assert r["per_party"]["u:2"]["owed_cents"] == r["total_cents"] == 2000 + 200 + 400 + 500


def test_the_birthday_person_can_still_be_the_one_who_paid():
    """They put their card down and get all of it back."""
    r = compute_bill(
        split_mode="even",
        subtotal_cents=6000,
        participants=[
            {"party": "u:1", "weight": 1, "exempt": True},
            {"party": "u:2", "weight": 1},
            {"party": "u:3", "weight": 1},
        ],
        payments=[{"party": "u:1", "amount_cents": 6000}],
    )
    assert r["per_party"]["u:1"]["balance_cents"] == 6000
    assert r["per_party"]["u:2"]["owed_cents"] == 3000
    assert r["per_party"]["u:3"]["owed_cents"] == 3000


def test_exempting_everybody_is_ignored_rather_than_losing_the_money():
    r = compute_bill(
        split_mode="even",
        subtotal_cents=5000,
        participants=[
            {"party": "u:1", "weight": 1, "exempt": True},
            {"party": "u:2", "weight": 1, "exempt": True},
        ],
    )
    assert r["per_party"]["u:1"]["owed_cents"] == 2500
    assert r["per_party"]["u:2"]["owed_cents"] == 2500
    assert any("somebody has to cover it" in w for w in r["warnings"])


def test_a_discount_aimed_at_somebody_not_paying_says_so():
    r = compute_bill(
        split_mode="itemized",
        participants=[
            {"party": "u:1", "weight": 1, "exempt": True},
            {"party": "u:2", "weight": 1},
        ],
        items=[{"label": "Steak", "amount_cents": 3000, "shares": {"u:1": 1}}],
        discount={"mode": "amount", "cents": 500, "target": "u:1"},
    )
    assert r["discount_cents"] == 0
    assert r["per_party"]["u:2"]["owed_cents"] == 3000
    assert any("not paying" in w for w in r["warnings"])


def test_the_birthday_share_follows_the_weights_in_shares_mode():
    r = compute_bill(
        split_mode="shares",
        subtotal_cents=12000,
        participants=[
            {"party": "u:1", "weight": 1, "exempt": True},
            {"party": "u:2", "weight": 2},   # a couple
            {"party": "u:3", "weight": 1},
        ],
    )
    # 3000 of theirs to cover, split 2:1 like the rest of the bill.
    assert r["per_party"]["u:1"]["owed_cents"] == 0
    assert r["per_party"]["u:2"]["owed_cents"] == 6000 + 2000
    assert r["per_party"]["u:3"]["owed_cents"] == 3000 + 1000


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
