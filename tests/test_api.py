"""End-to-end API tests against a throwaway database.

Run: python tests/test_api.py     (or python -m pytest tests -q)
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Must be set before app.config is imported, and keeps the real data/ untouched.
_TMP = tempfile.mkdtemp(prefix="billsplit-test-")
os.environ["BILLSPLIT_DATA_DIR"] = _TMP
os.environ["BILLSPLIT_SECRET_KEY"] = "test-secret"
os.environ["BILLSPLIT_SKIP_BACKGROUND"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from app import db, security  # noqa: E402
from app.main import app  # noqa: E402

# Tests do dozens of password hashes; drop the scrypt cost so they run in
# seconds instead of a minute. Production keeps the real cost.
security.SCRYPT_N = 2 ** 8


def fresh_client() -> TestClient:
    for name in os.listdir(_TMP):
        path = os.path.join(_TMP, name)
        shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) else os.remove(path)
    db.init_db()
    return TestClient(app)


def signup_admin(client: TestClient, username: str = "jon", password: str = "supersecret1") -> None:
    r = client.post("/api/auth/register", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_first_admin"] is True
    assert body["user"]["is_admin"] is True
    assert body["user"]["status"] == "active"


def test_bootstrap_reports_fresh_install():
    client = fresh_client()
    assert client.get("/api/bootstrap").json()["needs_setup"] is True
    signup_admin(client)
    assert client.get("/api/bootstrap").json()["needs_setup"] is False


def test_first_user_is_admin_second_is_pending():
    client = fresh_client()
    signup_admin(client)

    r = client.post("/api/auth/register", json={"username": "sam", "password": "hunter2hunter"})
    assert r.status_code == 200
    assert r.json()["status"] == "pending"
    assert r.json()["is_first_admin"] is False

    # Pending accounts cannot sign in.
    fresh = TestClient(app)
    r = fresh.post("/api/auth/login", json={"username": "sam", "password": "hunter2hunter"})
    assert r.status_code == 403
    assert "approval" in r.json()["detail"].lower()


def test_admin_approves_then_user_can_sign_in():
    client = fresh_client()
    signup_admin(client)
    client.post("/api/auth/register", json={"username": "sam", "password": "hunter2hunter"})

    users = client.get("/api/admin/users").json()
    assert users["pending_count"] == 1
    sam = next(u for u in users["users"] if u["username"] == "sam")

    r = client.post(f"/api/admin/users/{sam['id']}/approve", json={})
    assert r.status_code == 200
    assert r.json()["user"]["status"] == "active"

    sam_client = TestClient(app)
    r = sam_client.post("/api/auth/login", json={"username": "sam", "password": "hunter2hunter"})
    assert r.status_code == 200
    assert r.json()["user"]["username"] == "sam"
    assert r.json()["user"]["is_admin"] is False

    # ...and cannot reach admin endpoints.
    assert sam_client.get("/api/admin/users").status_code == 403


def test_rejected_and_suspended_cannot_sign_in():
    client = fresh_client()
    signup_admin(client)

    client.post("/api/auth/register", json={"username": "spam", "password": "spamspam123"})
    spam = next(u for u in client.get("/api/admin/users").json()["users"] if u["username"] == "spam")
    client.post(f"/api/admin/users/{spam['id']}/reject", json={"note": "not one of us"})
    r = TestClient(app).post("/api/auth/login", json={"username": "spam", "password": "spamspam123"})
    assert r.status_code == 403

    client.post("/api/auth/register", json={"username": "sam", "password": "hunter2hunter"})
    sam = next(u for u in client.get("/api/admin/users").json()["users"] if u["username"] == "sam")
    client.post(f"/api/admin/users/{sam['id']}/approve", json={})
    sam_client = TestClient(app)
    assert sam_client.post(
        "/api/auth/login", json={"username": "sam", "password": "hunter2hunter"}
    ).status_code == 200

    client.post(f"/api/admin/users/{sam['id']}/suspend", json={})
    # The live session dies with the suspension.
    assert sam_client.get("/api/dashboard").status_code == 401
    assert TestClient(app).post(
        "/api/auth/login", json={"username": "sam", "password": "hunter2hunter"}
    ).status_code == 403


def test_password_reset_forces_a_change_before_anything_else():
    client = fresh_client()
    signup_admin(client)
    client.post("/api/auth/register", json={"username": "sam", "password": "hunter2hunter"})
    sam = next(u for u in client.get("/api/admin/users").json()["users"] if u["username"] == "sam")
    client.post(f"/api/admin/users/{sam['id']}/approve", json={})

    r = client.post(f"/api/admin/users/{sam['id']}/reset-password")
    temp = r.json()["temporary_password"]
    assert len(temp) >= 10

    sam_client = TestClient(app)
    assert sam_client.post(
        "/api/auth/login", json={"username": "sam", "password": "hunter2hunter"}
    ).status_code == 401
    r = sam_client.post("/api/auth/login", json={"username": "sam", "password": temp})
    assert r.status_code == 200
    assert r.json()["user"]["must_change_password"] is True

    # Locked out of everything until a new password is chosen.
    assert sam_client.get("/api/dashboard").status_code == 403
    assert sam_client.get("/api/me").status_code == 200

    r = sam_client.post(
        "/api/auth/change-password",
        json={"current_password": temp, "new_password": "brandnewpass9"},
    )
    assert r.status_code == 200
    assert sam_client.get("/api/dashboard").status_code == 200


def test_cannot_remove_the_last_admin():
    client = fresh_client()
    signup_admin(client)
    me = client.get("/api/me").json()["user"]
    r = client.post(f"/api/admin/users/{me['id']}/admin", json={"is_admin": False})
    assert r.status_code == 400
    assert "last admin" in r.json()["detail"].lower()
    assert client.delete(f"/api/admin/users/{me['id']}").status_code == 400


def test_registration_can_be_closed():
    client = fresh_client()
    signup_admin(client)
    client.post("/api/admin/settings", json={"registration_open": False})
    r = client.post("/api/auth/register", json={"username": "nope", "password": "password123"})
    assert r.status_code == 403


def test_login_rate_limit():
    client = fresh_client()
    signup_admin(client)
    codes = [
        client.post("/api/auth/login", json={"username": "jon", "password": "wrong"}).status_code
        for _ in range(12)
    ]
    assert 429 in codes
    from app.routers import auth as auth_router

    auth_router._attempts.clear()


# --- groups and bills --------------------------------------------------------

def _two_user_group(client: TestClient) -> tuple[int, str, str]:
    """Admin 'jon' plus approved 'sam' in one group. Returns (group_id, jon_party, sam_party)."""
    client.post("/api/auth/register", json={"username": "sam", "password": "hunter2hunter"})
    sam = next(u for u in client.get("/api/admin/users").json()["users"] if u["username"] == "sam")
    client.post(f"/api/admin/users/{sam['id']}/approve", json={})
    me = client.get("/api/me").json()["user"]

    r = client.post(
        "/api/groups",
        json={"name": "Vegas 2026", "currency": "USD", "member_ids": [sam["id"]]},
    )
    assert r.status_code == 200, r.text
    return r.json()["group"]["id"], f"u:{me['id']}", f"u:{sam['id']}"


def test_create_group_with_members_and_guests():
    client = fresh_client()
    signup_admin(client)
    group_id, jon, sam = _two_user_group(client)

    r = client.post(f"/api/groups/{group_id}/guests", json={"name": "Dana"})
    assert r.status_code == 200
    parties = r.json()["parties"]
    assert len(parties) == 3
    assert any(p["kind"] == "guest" and p["name"] == "Dana" for p in parties)


def test_non_member_cannot_see_a_group():
    client = fresh_client()
    signup_admin(client)
    group_id, _, _ = _two_user_group(client)

    client.post("/api/auth/register", json={"username": "eve", "password": "evepassword1"})
    eve = next(u for u in client.get("/api/admin/users").json()["users"] if u["username"] == "eve")
    client.post(f"/api/admin/users/{eve['id']}/approve", json={})

    eve_client = TestClient(app)
    eve_client.post("/api/auth/login", json={"username": "eve", "password": "evepassword1"})
    assert eve_client.get(f"/api/groups/{group_id}").status_code == 403
    assert eve_client.get("/api/groups").json()["groups"] == []


def test_even_bill_with_percent_tax_and_tip_end_to_end():
    client = fresh_client()
    signup_admin(client)
    group_id, jon, sam = _two_user_group(client)

    r = client.post(
        f"/api/groups/{group_id}/bills",
        json={
            "title": "Dinner at Lotus",
            "bill_date": "2026-09-05",
            "split_mode": "even",
            "subtotal": "100.00",
            "tax": {"mode": "percent", "percent": 8.875},
            "tip": {"mode": "percent", "percent": 20, "base": "pre_tax"},
            "participants": [{"party": jon}, {"party": sam}],
            "payments": [{"party": jon, "amount": "128.88"}],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["totals"]["tax_cents"] == 888
    assert body["totals"]["tip_cents"] == 2000
    assert body["totals"]["total_cents"] == 12888
    assert body["totals"]["unpaid_cents"] == 0

    lines = {b["party"]: b for b in body["breakdown"]}
    assert lines[jon]["owed_cents"] == 6444
    assert lines[sam]["owed_cents"] == 6444
    assert lines[jon]["balance_cents"] == 6444     # jon paid, so sam owes him
    assert lines[sam]["balance_cents"] == -6444

    group = client.get(f"/api/groups/{group_id}").json()
    assert len(group["transfers"]) == 1
    assert group["transfers"][0]["from_party"] == sam
    assert group["transfers"][0]["to_party"] == jon
    assert group["transfers"][0]["amount_cents"] == 6444
    assert group["outstanding_cents"] == 6444


def test_manual_tax_and_tip_amounts_end_to_end():
    client = fresh_client()
    signup_admin(client)
    group_id, jon, sam = _two_user_group(client)

    r = client.post(
        f"/api/groups/{group_id}/bills",
        json={
            "title": "Takeout",
            "split_mode": "even",
            "subtotal": "40.00",
            "tax": {"mode": "amount", "amount": "3.50"},
            "tip": {"mode": "amount", "amount": "6.00"},
            "extras": [
                {"label": "Delivery", "mode": "amount", "amount": "4.99", "split": "even"}
            ],
            "participants": [{"party": jon}, {"party": sam}],
        },
    )
    assert r.status_code == 200, r.text
    totals = r.json()["totals"]
    assert totals["tax_cents"] == 350
    assert totals["tip_cents"] == 600
    assert totals["extras_total_cents"] == 499
    assert totals["total_cents"] == 4000 + 350 + 600 + 499


def test_itemized_bill_end_to_end():
    client = fresh_client()
    signup_admin(client)
    group_id, jon, sam = _two_user_group(client)
    client.post(f"/api/groups/{group_id}/guests", json={"name": "Dana"})
    parties = client.get(f"/api/groups/{group_id}").json()["parties"]
    dana = next(p["party"] for p in parties if p["name"] == "Dana")

    r = client.post(
        f"/api/groups/{group_id}/bills",
        json={
            "title": "Steakhouse",
            "split_mode": "itemized",
            "items": [
                {"label": "Ribeye", "amount": "48.00", "shares": {jon: 1}},
                {"label": "Salmon", "amount": "32.00", "shares": {sam: 1}},
                {"label": "Salad", "amount": "12.00", "shares": {dana: 1}},
                {"label": "Bottle of red", "amount": "60.00", "shares": {jon: 1, sam: 1, dana: 1}},
            ],
            "tax": {"mode": "percent", "percent": 10},
            "tip": {"mode": "percent", "percent": 20, "base": "post_tax"},
            "participants": [{"party": jon}, {"party": sam}, {"party": dana}],
            "payments": [{"party": jon, "amount": "200.00"}],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["totals"]["subtotal_cents"] == 15200
    assert body["totals"]["tax_cents"] == 1520
    assert body["totals"]["tip_cents"] == 3344       # 20% of 167.20
    total = body["totals"]["total_cents"]
    assert total == 15200 + 1520 + 3344
    assert sum(b["owed_cents"] for b in body["breakdown"]) == total

    lines = {b["party"]: b for b in body["breakdown"]}
    assert lines[jon]["base_cents"] == 4800 + 2000
    assert lines[sam]["base_cents"] == 3200 + 2000
    assert lines[dana]["base_cents"] == 1200 + 2000
    # the ribeye eater carries more tax than the salad eater
    assert lines[jon]["tax_cents"] > lines[dana]["tax_cents"]


def test_sub_items_round_trip_and_follow_the_parent():
    client = fresh_client()
    signup_admin(client)
    group_id, jon, sam = _two_user_group(client)

    r = client.post(
        f"/api/groups/{group_id}/bills",
        json={
            "title": "Burgers", "split_mode": "itemized",
            "items": [
                {"label": "Burger", "amount": "12.00", "shares": {jon: 1}, "sub_items": [
                    {"label": "Add bacon", "amount": "2.00"},
                    {"label": "No onions", "amount": "0"},
                ]},
                {"label": "Salad", "amount": "9.00", "shares": {sam: 1}},
            ],
            "participants": [{"party": jon}, {"party": sam}],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["totals"]["subtotal_cents"] == 2300
    lines = {b["party"]: b for b in body["breakdown"]}
    assert lines[jon]["base_cents"] == 1400, "burger carries its bacon"
    assert lines[sam]["base_cents"] == 900

    # Read back: sub-items are nested under their parent, not loose lines.
    detail = client.get(f"/api/bills/{body['bill_id']}").json()
    assert len(detail["items"]) == 2
    burger = next(i for i in detail["items"] if i["label"] == "Burger")
    assert [s["label"] for s in burger["sub_items"]] == ["Add bacon", "No onions"]
    assert burger["sub_total_cents"] == 200
    assert next(i for i in detail["items"] if i["label"] == "Salad")["sub_items"] == []

    # And editing keeps them.
    r = client.put(
        f"/api/bills/{body['bill_id']}",
        json={
            "title": "Burgers", "split_mode": "itemized",
            "items": [{"label": "Burger", "amount": "12.00", "shares": {jon: 1},
                       "sub_items": [{"label": "Add bacon", "amount": "2.50"}]}],
            "participants": [{"party": jon}, {"party": sam}],
        },
    )
    assert r.status_code == 200
    assert r.json()["items"][0]["sub_items"][0]["amount_cents"] == 250


def test_divided_items_round_trip():
    client = fresh_client()
    signup_admin(client)
    group_id, jon, sam = _two_user_group(client)

    r = client.post(
        f"/api/groups/{group_id}/bills",
        json={
            "title": "Round of beers", "split_mode": "itemized",
            "items": [{"label": "Beer", "amount": "18.00", "portions": 3,
                       "shares": {jon: 2, sam: 1}}],
            "participants": [{"party": jon}, {"party": sam}],
        },
    )
    assert r.status_code == 200, r.text
    lines = {b["party"]: b for b in r.json()["breakdown"]}
    assert lines[jon]["base_cents"] == 1200
    assert lines[sam]["base_cents"] == 600

    detail = client.get(f"/api/bills/{r.json()['bill_id']}").json()
    assert detail["items"][0]["portions"] == 3
    assert detail["items"][0]["shares"] == {jon: 2, sam: 1}


def test_cannot_take_more_portions_than_exist():
    client = fresh_client()
    signup_admin(client)
    group_id, jon, sam = _two_user_group(client)
    r = client.post(
        f"/api/groups/{group_id}/bills",
        json={
            "title": "Beers", "split_mode": "itemized",
            "items": [{"label": "Beer", "amount": "18.00", "portions": 2, "shares": {jon: 5}}],
            "participants": [{"party": jon}, {"party": sam}],
        },
    )
    assert r.status_code == 400
    assert "more than the 2 portions" in r.json()["detail"]


def test_targeted_discount_round_trip():
    client = fresh_client()
    signup_admin(client)
    group_id, jon, sam = _two_user_group(client)

    r = client.post(
        f"/api/groups/{group_id}/bills",
        json={
            "title": "Dinner with a coupon", "split_mode": "even", "subtotal": "60.00",
            "discount": {"mode": "amount", "amount": "10.00", "target": sam},
            "tax": {"mode": "percent", "percent": 10},
            "participants": [{"party": jon}, {"party": sam}],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bill"]["discount_target"] == sam
    lines = {b["party"]: b for b in body["breakdown"]}
    assert lines[sam]["discount_cents"] == -1000
    assert lines[jon]["discount_cents"] == 0
    assert lines[jon]["owed_cents"] == 3300      # 30 + 10% tax
    assert lines[sam]["owed_cents"] == 2200      # 20 + 10% tax
    assert sum(b["owed_cents"] for b in body["breakdown"]) == body["totals"]["total_cents"]

    # Reloading keeps the target, so the editor shows the right person.
    detail = client.get(f"/api/bills/{body['bill_id']}").json()
    assert detail["bill"]["discount_target"] == sam


def test_a_discount_aimed_at_someone_off_the_bill_is_refused():
    client = fresh_client()
    signup_admin(client)
    group_id, jon, sam = _two_user_group(client)
    r = client.post(
        f"/api/groups/{group_id}/bills",
        json={
            "title": "x", "split_mode": "even", "subtotal": "10.00",
            "discount": {"mode": "amount", "amount": "1.00", "target": sam},
            "participants": [{"party": jon}],
        },
    )
    assert r.status_code == 400
    assert "not on this bill" in r.json()["detail"]


def test_preview_matches_the_saved_bill_for_sub_items_and_portions():
    """The editor previews before saving; the two must not disagree."""
    client = fresh_client()
    signup_admin(client)
    group_id, jon, sam = _two_user_group(client)

    payload = {
        "title": "Mixed", "split_mode": "itemized",
        "items": [
            {"label": "Burger", "amount": "12.00", "shares": {jon: 1},
             "sub_items": [{"label": "Bacon", "amount": "2.00"}]},
            {"label": "Beer", "amount": "18.00", "portions": 3, "shares": {jon: 1, sam: 2}},
        ],
        "discount": {"mode": "percent", "percent": 10, "target": sam},
        "tax": {"mode": "percent", "percent": 8.875},
        "tip": {"mode": "percent", "percent": 20, "base": "post_tax"},
        "participants": [{"party": jon}, {"party": sam}],
    }

    preview = client.post("/api/bills/preview", json={**payload, "group_id": group_id}).json()
    saved = client.post(f"/api/groups/{group_id}/bills", json=payload).json()

    assert preview["totals"]["total_cents"] == saved["totals"]["total_cents"]
    assert {b["party"]: b["owed_cents"] for b in preview["breakdown"]} \
        == {b["party"]: b["owed_cents"] for b in saved["breakdown"]}


def test_shares_mode_for_a_couple_sharing_a_room():
    client = fresh_client()
    signup_admin(client)
    group_id, jon, sam = _two_user_group(client)

    r = client.post(
        f"/api/groups/{group_id}/bills",
        json={
            "title": "Airbnb - 3 nights",
            "split_mode": "shares",
            "subtotal": "900.00",
            "participants": [{"party": jon, "weight": 2}, {"party": sam, "weight": 1}],
        },
    )
    lines = {b["party"]: b for b in r.json()["breakdown"]}
    assert lines[jon]["owed_cents"] == 60000
    assert lines[sam]["owed_cents"] == 30000


def test_preview_saves_nothing():
    client = fresh_client()
    signup_admin(client)
    group_id, jon, sam = _two_user_group(client)

    r = client.post(
        "/api/bills/preview",
        json={
            "group_id": group_id,
            "title": "scratch",
            "split_mode": "even",
            "subtotal": "50.00",
            "tip": {"mode": "percent", "percent": 18},
            "participants": [{"party": jon}, {"party": sam}],
        },
    )
    assert r.status_code == 200
    assert r.json()["totals"]["total_cents"] == 5900
    assert client.get(f"/api/groups/{group_id}/bills").json()["bills"] == []


def test_preview_works_on_a_half_typed_bill():
    """The editor previews while you type, so a blank title or no people yet
    must come back as a friendly zero rather than a validation error."""
    client = fresh_client()
    signup_admin(client)
    group_id, jon, sam = _two_user_group(client)

    r = client.post(
        "/api/bills/preview",
        json={"group_id": group_id, "split_mode": "even", "subtotal": "",
              "participants": [{"party": jon}, {"party": sam}]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["totals"]["total_cents"] == 0

    r = client.post("/api/bills/preview", json={"group_id": group_id, "participants": []})
    assert r.status_code == 200
    assert r.json()["breakdown"] == []
    assert r.json()["totals"]["warnings"]


def test_bill_rejects_outsiders_and_empty_participants():
    client = fresh_client()
    signup_admin(client)
    group_id, jon, _ = _two_user_group(client)

    r = client.post(
        f"/api/groups/{group_id}/bills",
        json={"title": "x", "subtotal": "10", "participants": []},
    )
    assert r.status_code == 400

    r = client.post(
        f"/api/groups/{group_id}/bills",
        json={"title": "x", "subtotal": "10", "participants": [{"party": "u:9999"}]},
    )
    assert r.status_code == 400
    assert "not in this group" in r.json()["detail"]


def test_bad_amount_is_a_clear_error():
    client = fresh_client()
    signup_admin(client)
    group_id, jon, _ = _two_user_group(client)
    r = client.post(
        f"/api/groups/{group_id}/bills",
        json={"title": "x", "subtotal": "twelve dollars", "participants": [{"party": jon}]},
    )
    assert r.status_code == 400
    assert "valid amount" in r.json()["detail"]


def test_edit_and_delete_a_bill():
    client = fresh_client()
    signup_admin(client)
    group_id, jon, sam = _two_user_group(client)
    bill_id = client.post(
        f"/api/groups/{group_id}/bills",
        json={
            "title": "Lunch", "split_mode": "even", "subtotal": "20.00",
            "participants": [{"party": jon}, {"party": sam}],
        },
    ).json()["bill_id"]

    r = client.put(
        f"/api/bills/{bill_id}",
        json={
            "title": "Lunch (corrected)", "split_mode": "even", "subtotal": "30.00",
            "tax": {"mode": "percent", "percent": 10},
            "participants": [{"party": jon}, {"party": sam}],
        },
    )
    assert r.status_code == 200
    assert r.json()["bill"]["title"] == "Lunch (corrected)"
    assert r.json()["totals"]["total_cents"] == 3300
    # Replacing the bill leaves no orphaned child rows behind.
    assert len(client.get(f"/api/bills/{bill_id}").json()["participants"]) == 2

    assert client.delete(f"/api/bills/{bill_id}").status_code == 200
    assert client.get(f"/api/bills/{bill_id}").status_code == 404


def test_settlement_zeroes_the_balance():
    client = fresh_client()
    signup_admin(client)
    group_id, jon, sam = _two_user_group(client)
    client.post(
        f"/api/groups/{group_id}/bills",
        json={
            "title": "Dinner", "split_mode": "even", "subtotal": "100.00",
            "participants": [{"party": jon}, {"party": sam}],
            "payments": [{"party": jon, "amount": "100.00"}],
        },
    )
    group = client.get(f"/api/groups/{group_id}").json()
    assert group["outstanding_cents"] == 5000

    r = client.post(
        f"/api/groups/{group_id}/settlements",
        json={"from_party": sam, "to_party": jon, "amount": "50.00", "note": "Venmo"},
    )
    assert r.status_code == 200
    assert r.json()["outstanding_cents"] == 0
    assert all(b["balance_cents"] == 0 for b in r.json()["balances"])

    settlement_id = r.json()["settlements"][0]["id"]
    r = client.delete(f"/api/groups/{group_id}/settlements/{settlement_id}")
    assert r.json()["outstanding_cents"] == 5000


def test_cannot_remove_someone_who_is_on_a_bill():
    client = fresh_client()
    signup_admin(client)
    group_id, jon, sam = _two_user_group(client)
    client.post(
        f"/api/groups/{group_id}/bills",
        json={
            "title": "Dinner", "split_mode": "even", "subtotal": "20.00",
            "participants": [{"party": jon}, {"party": sam}],
        },
    )
    sam_id = int(sam.split(":")[1])
    r = client.delete(f"/api/groups/{group_id}/members/{sam_id}")
    assert r.status_code == 409
    assert "on 1 bill" in r.json()["detail"]


def test_deleting_a_user_keeps_the_group_maths_intact():
    client = fresh_client()
    signup_admin(client)
    group_id, jon, sam = _two_user_group(client)
    client.post(
        f"/api/groups/{group_id}/bills",
        json={
            "title": "Dinner", "split_mode": "even", "subtotal": "100.00",
            "participants": [{"party": jon}, {"party": sam}],
            "payments": [{"party": jon, "amount": "100.00"}],
        },
    )
    before = client.get(f"/api/groups/{group_id}").json()
    assert before["outstanding_cents"] == 5000

    sam_id = int(sam.split(":")[1])
    r = client.delete(f"/api/admin/users/{sam_id}")
    assert r.status_code == 200, r.text
    assert r.json()["history_preserved_in_groups"] == 1

    after = client.get(f"/api/groups/{group_id}").json()
    assert after["total_spend_cents"] == before["total_spend_cents"]
    assert after["outstanding_cents"] == 5000
    ghost = next(b for b in after["balances"] if b["balance_cents"] == -5000)
    assert ghost["name"].endswith("(removed)")
    assert ghost["kind"] == "guest"
    # The bill itself still adds up.
    bill_id = after["bills"][0]["id"]
    detail = client.get(f"/api/bills/{bill_id}").json()
    assert sum(b["owed_cents"] for b in detail["breakdown"]) == detail["totals"]["total_cents"]


def test_deleting_a_user_without_keeping_history():
    client = fresh_client()
    signup_admin(client)
    group_id, jon, sam = _two_user_group(client)
    client.post(
        f"/api/groups/{group_id}/bills",
        json={
            "title": "Dinner", "split_mode": "even", "subtotal": "100.00",
            "participants": [{"party": jon}, {"party": sam}],
        },
    )
    sam_id = int(sam.split(":")[1])
    r = client.delete(f"/api/admin/users/{sam_id}?keep_history=false")
    assert r.status_code == 200
    assert r.json()["history_preserved_in_groups"] == 0
    # The orphaned party is dropped from the bill but nothing 500s.
    after = client.get(f"/api/groups/{group_id}").json()
    bill_id = after["bills"][0]["id"]
    assert client.get(f"/api/bills/{bill_id}").status_code == 200


def test_dashboard_totals():
    client = fresh_client()
    signup_admin(client)
    group_id, jon, sam = _two_user_group(client)
    client.post(
        f"/api/groups/{group_id}/bills",
        json={
            "title": "Dinner", "split_mode": "even", "subtotal": "100.00",
            "participants": [{"party": jon}, {"party": sam}],
            "payments": [{"party": jon, "amount": "100.00"}],
        },
    )
    dash = client.get("/api/dashboard").json()
    assert dash["owed_to_me_cents"] == 5000
    assert dash["i_owe_cents"] == 0
    assert dash["net_cents"] == 5000
    assert len(dash["recent_bills"]) == 1
    assert dash["groups"][0]["my_transfers"][0]["amount_cents"] == 5000


def test_csv_export():
    client = fresh_client()
    signup_admin(client)
    group_id, jon, sam = _two_user_group(client)
    client.post(
        f"/api/groups/{group_id}/bills",
        json={
            "title": "Dinner", "split_mode": "even", "subtotal": "100.00",
            "tip": {"mode": "percent", "percent": 20},
            "participants": [{"party": jon}, {"party": sam}],
            "payments": [{"party": jon, "amount": "120.00"}],
        },
    )
    r = client.get(f"/api/groups/{group_id}/export.csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    body = r.text
    assert "Dinner" in body and "Suggested transfers" in body
    assert "60.00" in body


def test_categories_and_filtering():
    client = fresh_client()
    signup_admin(client)
    group_id, jon, sam = _two_user_group(client)
    cats = client.get("/api/categories").json()["categories"]
    assert any(c["name"] == "Food & Restaurant" for c in cats)
    food = next(c for c in cats if c["name"] == "Food & Restaurant")
    lodging = next(c for c in cats if c["name"] == "Airbnb / Hotel")

    for title, cat in (("Dinner", food["id"]), ("Cabin", lodging["id"])):
        client.post(
            f"/api/groups/{group_id}/bills",
            json={
                "title": title, "split_mode": "even", "subtotal": "50.00",
                "category_id": cat, "participants": [{"party": jon}, {"party": sam}],
            },
        )

    bills = client.get(f"/api/groups/{group_id}/bills?category_id={food['id']}").json()["bills"]
    assert [b["title"] for b in bills] == ["Dinner"]

    found = client.get(f"/api/groups/{group_id}/bills?search=cab").json()["bills"]
    assert [b["title"] for b in found] == ["Cabin"]

    group = client.get(f"/api/groups/{group_id}").json()
    assert {c["category"] for c in group["spend_by_category"]} == {"Food & Restaurant", "Airbnb / Hotel"}

    r = client.post("/api/categories", json={"name": "Ski passes", "icon": "activities"})
    assert r.status_code == 200
    assert r.json()["category"]["name"] == "Ski passes"


def test_admin_creates_an_account_directly():
    client = fresh_client()
    signup_admin(client)
    r = client.post("/api/admin/users", json={"username": "pat", "display_name": "Pat"})
    assert r.status_code == 200
    temp = r.json()["temporary_password"]
    assert r.json()["user"]["status"] == "active"

    pat = TestClient(app)
    r = pat.post("/api/auth/login", json={"username": "pat", "password": temp})
    assert r.status_code == 200
    assert r.json()["user"]["must_change_password"] is True


def test_settings_validation_and_repo_normalisation():
    client = fresh_client()
    signup_admin(client)
    r = client.post(
        "/api/admin/settings",
        json={"update_repo": "https://github.com/ZipperedJon/bill-splitter.git"},
    )
    assert r.status_code == 200
    assert r.json()["settings"]["update_repo"] == "ZipperedJon/bill-splitter"

    assert client.post("/api/admin/settings", json={"update_repo": "not a repo"}).status_code == 400
    assert client.post("/api/admin/settings", json={"tip_base": "sideways"}).status_code == 400
    assert client.post("/api/admin/settings", json={"nonsense": "1"}).status_code == 400

    r = client.post("/api/admin/settings", json={"update_check_interval_minutes": 1})
    assert r.json()["settings"]["update_check_interval_minutes"] == "5"  # clamped


def test_public_base_url_is_normalised_and_used_for_share_links():
    client = fresh_client()
    signup_admin(client)

    # A bare hostname is accepted and assumed to be https.
    r = client.post("/api/admin/settings", json={"public_base_url": "bills.example.com"})
    assert r.status_code == 200, r.text
    assert r.json()["settings"]["public_base_url"] == "https://bills.example.com"

    # A trailing slash is trimmed, since '/s/<token>' gets appended.
    r = client.post("/api/admin/settings", json={"public_base_url": "https://bills.example.com/"})
    assert r.json()["settings"]["public_base_url"] == "https://bills.example.com"

    # A path would make the share link 404, so it is refused rather than eaten.
    r = client.post("/api/admin/settings", json={"public_base_url": "https://example.com/bills"})
    assert r.status_code == 400
    assert "no path" in r.json()["detail"]

    for bad in ("ftp://example.com", "https://", "::::"):
        assert client.post(
            "/api/admin/settings", json={"public_base_url": bad}
        ).status_code == 400, bad

    # And it flows through to the share link the editor offers.
    client.post("/api/admin/settings", json={"public_base_url": "https://bills.example.com"})
    group_id, jon, sam = _two_user_group(client)
    bill_id = client.post(
        f"/api/groups/{group_id}/bills",
        json={"title": "Dinner", "split_mode": "even", "subtotal": "10.00",
              "participants": [{"party": jon}]},
    ).json()["bill_id"]
    share = client.post(f"/api/bills/{bill_id}/share", json={}).json()["share"]
    assert share["public_url"] == f"https://bills.example.com/s/{share['token']}"

    # Cleared again, the editor falls back to whatever address it is browsing.
    client.post("/api/admin/settings", json={"public_base_url": ""})
    share = client.get(f"/api/bills/{bill_id}/share").json()["share"]
    assert share["public_url"] == ""
    assert share["path"] == f"/s/{share['token']}"


def test_update_token_is_write_only():
    client = fresh_client()
    signup_admin(client)
    assert client.get("/api/admin/settings").json()["settings"]["has_update_token"] == "0"
    client.post("/api/system/update/token", json={"token": "ghp_secretvalue"})
    settings = client.get("/api/admin/settings").json()["settings"]
    assert settings["has_update_token"] == "1"
    assert "update_token" not in settings
    assert "ghp_secretvalue" not in str(settings)


def test_update_status_is_admin_only_and_reports_no_repo():
    client = fresh_client()
    signup_admin(client)
    r = client.get("/api/system/update")
    assert r.status_code == 200
    assert r.json()["status"]["repo"] == ""
    assert r.json()["status"]["update_available"] is False

    r = client.post("/api/system/update/check")
    assert r.status_code == 502
    assert "repository" in r.json()["detail"].lower()

    client.post("/api/auth/register", json={"username": "sam", "password": "hunter2hunter"})
    sam = next(u for u in client.get("/api/admin/users").json()["users"] if u["username"] == "sam")
    client.post(f"/api/admin/users/{sam['id']}/approve", json={})
    sam_client = TestClient(app)
    sam_client.post("/api/auth/login", json={"username": "sam", "password": "hunter2hunter"})
    assert sam_client.get("/api/system/update").status_code == 403
    assert sam_client.post("/api/system/update/apply", json={}).status_code == 403


def test_audit_log_records_admin_actions():
    client = fresh_client()
    signup_admin(client)
    client.post("/api/auth/register", json={"username": "sam", "password": "hunter2hunter"})
    sam = next(u for u in client.get("/api/admin/users").json()["users"] if u["username"] == "sam")
    client.post(f"/api/admin/users/{sam['id']}/approve", json={})
    actions = [e["action"] for e in client.get("/api/admin/audit").json()["entries"]]
    assert "user.approved" in actions
    assert "user.registered" in actions
    assert "install.first_admin" in actions


def test_logout_clears_the_session():
    client = fresh_client()
    signup_admin(client)
    assert client.get("/api/dashboard").status_code == 200
    client.post("/api/auth/logout")
    assert client.get("/api/dashboard").status_code == 401
    assert client.get("/api/me").json()["user"] is None


def test_static_frontend_is_served():
    client = fresh_client()
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


if __name__ == "__main__":
    failures = 0
    names = [n for n in globals() if n.startswith("test_")]
    for name in sorted(names):
        fn = globals()[name]
        try:
            fn()
            print(f"  ok   {name}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            import traceback

            print(f"  FAIL {name}: {exc.__class__.__name__}: {exc}")
            traceback.print_exc(limit=3)
    shutil.rmtree(_TMP, ignore_errors=True)
    print("\nall passed" if not failures else f"\n{failures} failed")
    sys.exit(1 if failures else 0)
