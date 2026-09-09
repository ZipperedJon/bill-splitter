"""Share-link tests: the guest flow, and what a link must never expose.

Run: python tests/test_share.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="billsplit-share-")
os.environ["BILLSPLIT_DATA_DIR"] = _TMP
os.environ["BILLSPLIT_SECRET_KEY"] = "share-test"
os.environ["BILLSPLIT_SKIP_BACKGROUND"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from app import db, security  # noqa: E402
from app.main import app  # noqa: E402

security.SCRYPT_N = 2 ** 8


def fresh_client() -> TestClient:
    for name in os.listdir(_TMP):
        path = os.path.join(_TMP, name)
        shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) else os.remove(path)
    db.init_db()
    client = TestClient(app)
    r = client.post("/api/auth/register", json={"username": "jon", "password": "sharetestpass"})
    assert r.status_code == 200, r.text
    return client


def itemized_bill(client: TestClient):
    """A steak-and-salad bill with three people and four items."""
    me = client.get("/api/me").json()["user"]
    jon = f"u:{me['id']}"
    group_id = client.post(
        "/api/groups", json={"name": "Dinner Club", "guest_names": ["Dana", "Marcus"]}
    ).json()["group"]["id"]
    parties = client.get(f"/api/groups/{group_id}").json()["parties"]
    dana = next(p["party"] for p in parties if p["name"] == "Dana")
    marcus = next(p["party"] for p in parties if p["name"] == "Marcus")

    r = client.post(
        f"/api/groups/{group_id}/bills",
        json={
            "title": "Steakhouse", "split_mode": "itemized",
            "items": [
                {"label": "Ribeye", "amount": "48.00", "shares": {}},
                {"label": "Salmon", "amount": "32.00", "shares": {}},
                {"label": "Salad", "amount": "14.00", "shares": {}},
                {"label": "Wine", "amount": "60.00", "shares": {}},
            ],
            "tax": {"mode": "percent", "percent": 10},
            "tip": {"mode": "percent", "percent": 20},
            "participants": [{"party": jon}, {"party": dana}, {"party": marcus}],
        },
    )
    assert r.status_code == 200, r.text
    detail = r.json()
    items = {i["label"]: i["id"] for i in detail["items"]}
    return {
        "group_id": group_id, "bill_id": detail["bill_id"], "items": items,
        "jon": jon, "dana": dana, "marcus": marcus,
    }


def make_link(client: TestClient, bill_id: int, **kwargs) -> str:
    r = client.post(f"/api/bills/{bill_id}/share", json={"allow_join": True, **kwargs})
    assert r.status_code == 200, r.text
    return r.json()["share"]["token"]


def guest() -> TestClient:
    """A visitor with no session at all."""
    return TestClient(app)


# --- creating and managing the link -----------------------------------------

def test_create_is_idempotent_so_the_button_is_safe_to_press_twice():
    client = fresh_client()
    bill = itemized_bill(client)

    first = client.post(f"/api/bills/{bill['bill_id']}/share", json={}).json()
    second = client.post(f"/api/bills/{bill['bill_id']}/share", json={}).json()
    assert first["created"] is True
    assert second["created"] is False
    assert first["share"]["token"] == second["share"]["token"]


def test_rotate_kills_the_old_link_and_revoke_kills_them_all():
    client = fresh_client()
    bill = itemized_bill(client)
    old = make_link(client, bill["bill_id"])

    new = client.post(f"/api/bills/{bill['bill_id']}/share/rotate").json()["share"]["token"]
    assert new != old
    assert guest().get(f"/api/share/{old}").status_code == 404
    assert guest().get(f"/api/share/{new}").status_code == 200

    client.delete(f"/api/bills/{bill['bill_id']}/share")
    assert guest().get(f"/api/share/{new}").status_code == 404
    assert client.get(f"/api/bills/{bill['bill_id']}/share").json()["share"]["exists"] is False


def test_only_group_members_can_manage_the_link():
    client = fresh_client()
    bill = itemized_bill(client)

    client.post("/api/auth/register", json={"username": "eve", "password": "evepassword1"})
    eve_id = next(
        u["id"] for u in client.get("/api/admin/users").json()["users"] if u["username"] == "eve"
    )
    client.post(f"/api/admin/users/{eve_id}/approve", json={})
    eve = TestClient(app)
    eve.post("/api/auth/login", json={"username": "eve", "password": "evepassword1"})

    assert eve.post(f"/api/bills/{bill['bill_id']}/share", json={}).status_code == 403
    assert eve.get(f"/api/bills/{bill['bill_id']}/share").status_code == 403


def test_an_unknown_token_is_a_clean_404():
    fresh_client()
    r = guest().get("/api/share/definitely-not-a-real-token")
    assert r.status_code == 404
    assert "not valid" in r.json()["detail"]


def test_expired_link_reports_gone():
    client = fresh_client()
    bill = itemized_bill(client)
    token = make_link(client, bill["bill_id"])
    # Reach in and age it, rather than sleeping.
    with db.cursor() as conn:
        conn.execute("UPDATE bill_shares SET expires_at=1 WHERE token=?", (token,))
    assert guest().get(f"/api/share/{token}").status_code == 410


# --- what a guest sees -------------------------------------------------------

def test_the_share_page_shows_the_bill_and_hides_everything_else():
    client = fresh_client()
    bill = itemized_bill(client)
    token = make_link(client, bill["bill_id"])

    data = guest().get(f"/api/share/{token}").json()
    assert data["bill"]["title"] == "Steakhouse"
    assert data["group_name"] == "Dinner Club"
    assert data["itemized"] is True
    assert [i["label"] for i in data["items"]] == ["Ribeye", "Salmon", "Salad", "Wine"]
    assert {p["name"] for p in data["people"]} == {"jon", "Dana", "Marcus"}
    assert data["totals"]["subtotal_cents"] == 15400
    assert [c["label"] for c in data["charges"]] == ["Tax (10%)", "Tip (20%)"]

    # No account details, no other bills, no group ledger.
    blob = str(data)
    for leaked in ("password_hash", "email", "username", "balances", "settlements",
                   "is_admin", "update_repo", "audit"):
        assert leaked not in blob, f"share payload leaks {leaked}"


def test_viewing_does_not_require_a_session_and_counts_views():
    client = fresh_client()
    bill = itemized_bill(client)
    token = make_link(client, bill["bill_id"])

    for _ in range(3):
        assert guest().get(f"/api/share/{token}").status_code == 200

    share = client.get(f"/api/bills/{bill['bill_id']}/share").json()["share"]
    assert share["views"] == 3
    assert share["last_seen_at"]


# --- claiming items ----------------------------------------------------------

def test_a_guest_picks_their_items_and_the_split_follows():
    client = fresh_client()
    bill = itemized_bill(client)
    token = make_link(client, bill["bill_id"])
    items = bill["items"]
    visitor = guest()

    # Dana had the salad and a share of the wine.
    r = visitor.post(
        f"/api/share/{token}/claims",
        json={"party": bill["dana"], "item_ids": [items["Salad"], items["Wine"]]},
    )
    assert r.status_code == 200, r.text

    # Marcus had the salmon and a share of the wine.
    r = visitor.post(
        f"/api/share/{token}/claims",
        json={"party": bill["marcus"], "item_ids": [items["Salmon"], items["Wine"]]},
    )
    assert r.status_code == 200

    data = visitor.get(f"/api/share/{token}").json()
    by_name = {p["name"]: p for p in data["people"]}
    assert by_name["Dana"]["claimed_items"] == 2
    assert by_name["Marcus"]["claimed_items"] == 2

    # The owner sees exactly the same numbers.
    detail = client.get(f"/api/bills/{bill['bill_id']}").json()
    lines = {b["name"]: b for b in detail["breakdown"]}
    # Wine is split between Dana and Marcus; the unclaimed ribeye spreads over all.
    assert lines["Dana"]["base_cents"] == 1400 + 3000 + 1600   # salad + wine/2 + ribeye/3
    assert lines["Marcus"]["base_cents"] == 3200 + 3000 + 1600
    assert sum(b["owed_cents"] for b in detail["breakdown"]) == detail["totals"]["total_cents"]


def test_claims_replace_only_that_persons_picks():
    client = fresh_client()
    bill = itemized_bill(client)
    token = make_link(client, bill["bill_id"])
    items = bill["items"]
    visitor = guest()

    visitor.post(f"/api/share/{token}/claims",
                 json={"party": bill["dana"], "item_ids": [items["Salad"]]})
    visitor.post(f"/api/share/{token}/claims",
                 json={"party": bill["marcus"], "item_ids": [items["Salmon"]]})

    # Dana changes her mind entirely.
    data = visitor.post(f"/api/share/{token}/claims",
                        json={"party": bill["dana"], "item_ids": [items["Wine"]]}).json()
    by_name = {p["name"]: p for p in data["people"]}
    assert by_name["Dana"]["claimed_items"] == 1
    assert by_name["Marcus"]["claimed_items"] == 1, "Marcus's pick must survive Dana's edit"

    claims = {i["label"]: set(i["claimed_by"]) for i in data["items"]}
    assert claims["Wine"] == {bill["dana"]}
    assert claims["Salmon"] == {bill["marcus"]}
    assert claims["Salad"] == set()


def test_clearing_picks_is_allowed():
    client = fresh_client()
    bill = itemized_bill(client)
    token = make_link(client, bill["bill_id"])
    visitor = guest()
    visitor.post(f"/api/share/{token}/claims",
                 json={"party": bill["dana"], "item_ids": [bill["items"]["Salad"]]})
    data = visitor.post(f"/api/share/{token}/claims",
                        json={"party": bill["dana"], "item_ids": []}).json()
    assert next(p for p in data["people"] if p["name"] == "Dana")["claimed_items"] == 0


def test_cannot_claim_for_someone_not_on_the_bill_or_a_foreign_item():
    client = fresh_client()
    bill = itemized_bill(client)
    token = make_link(client, bill["bill_id"])
    visitor = guest()

    r = visitor.post(f"/api/share/{token}/claims",
                     json={"party": "g:99999", "item_ids": []})
    assert r.status_code == 400
    assert "not on this bill" in r.json()["detail"]

    r = visitor.post(f"/api/share/{token}/claims",
                     json={"party": bill["dana"], "item_ids": [999999]})
    assert r.status_code == 400
    assert "not on this bill" in r.json()["detail"]


# --- sub-items and divided items through a link ------------------------------

def mixed_bill(client: TestClient):
    """A burger with extras, and a round of three beers on one line."""
    me = client.get("/api/me").json()["user"]
    jon = f"u:{me['id']}"
    group_id = client.post(
        "/api/groups", json={"name": "Pub", "guest_names": ["Dana"]}
    ).json()["group"]["id"]
    dana = next(
        p["party"] for p in client.get(f"/api/groups/{group_id}").json()["parties"]
        if p["name"] == "Dana"
    )
    detail = client.post(
        f"/api/groups/{group_id}/bills",
        json={
            "title": "Pub night", "split_mode": "itemized",
            "items": [
                {"label": "Burger", "amount": "12.00", "sub_items": [
                    {"label": "Add bacon", "amount": "2.00"},
                    {"label": "No onions", "amount": "0"},
                ]},
                {"label": "Beer", "amount": "18.00", "portions": 3},
            ],
            "participants": [{"party": jon}, {"party": dana}],
        },
    ).json()
    ids = {i["label"]: i["id"] for i in detail["items"]}
    return {"group_id": group_id, "bill_id": detail["bill_id"], "ids": ids,
            "jon": jon, "dana": dana}


def test_the_share_page_nests_sub_items_and_prices_the_whole_line():
    client = fresh_client()
    bill = mixed_bill(client)
    token = make_link(client, bill["bill_id"])

    data = guest().get(f"/api/share/{token}").json()
    assert [i["label"] for i in data["items"]] == ["Burger", "Beer"], \
        "sub-items must not appear as their own tappable lines"

    burger = data["items"][0]
    assert [s["label"] for s in burger["sub_items"]] == ["Add bacon", "No onions"]
    assert burger["amount_cents"] == 1200
    assert burger["line_total_cents"] == 1400, "what you take on by ticking it"
    assert data["items"][1]["portions"] == 3


def test_claiming_a_parent_through_the_link_picks_up_its_sub_items():
    client = fresh_client()
    bill = mixed_bill(client)
    token = make_link(client, bill["bill_id"])

    r = guest().post(
        f"/api/share/{token}/claims",
        json={"party": bill["dana"], "item_ids": [bill["ids"]["Burger"]]},
    )
    assert r.status_code == 200, r.text

    detail = client.get(f"/api/bills/{bill['bill_id']}").json()
    lines = {b["name"]: b for b in detail["breakdown"]}
    # Burger + bacon land on Dana; the unclaimed beer line splits evenly.
    assert lines["Dana"]["base_cents"] == 1400 + 900
    assert sum(b["owed_cents"] for b in detail["breakdown"]) == detail["totals"]["total_cents"]


def test_a_guest_takes_some_portions_of_a_divided_line():
    client = fresh_client()
    bill = mixed_bill(client)
    token = make_link(client, bill["bill_id"])
    visitor = guest()
    beer = str(bill["ids"]["Beer"])

    visitor.post(f"/api/share/{token}/claims",
                 json={"party": bill["dana"], "portions": {beer: 2}})
    r = visitor.post(f"/api/share/{token}/claims",
                     json={"party": bill["jon"], "portions": {beer: 1}})
    assert r.status_code == 200, r.text

    data = r.json()
    beer_line = next(i for i in data["items"] if i["label"] == "Beer")
    assert beer_line["claims"] == {bill["dana"]: 2, bill["jon"]: 1}

    detail = client.get(f"/api/bills/{bill['bill_id']}").json()
    lines = {b["name"]: b for b in detail["breakdown"]}
    # Two of three beers plus half the unclaimed burger line.
    assert lines["Dana"]["base_cents"] == 1200 + 700
    assert lines["jon"]["base_cents"] == 600 + 700


def test_a_portion_costs_a_portion_through_the_share_link():
    """The behaviour that made dividing confusing: taking one of three beers
    used to bill you for all three. It should cost one beer."""
    client = fresh_client()
    bill = mixed_bill(client)
    token = make_link(client, bill["bill_id"])
    beer = str(bill["ids"]["Beer"])          # 18.00 over 3 portions

    r = guest().post(f"/api/share/{token}/claims",
                     json={"party": bill["dana"], "portions": {beer: 1}})
    assert r.status_code == 200, r.text

    detail = client.get(f"/api/bills/{bill['bill_id']}").json()
    lines = {b["name"]: b for b in detail["breakdown"]}
    # 6.00 for her beer, plus her half of the two nobody took (12.00) and half
    # of the unclaimed burger line (14.00).
    assert lines["Dana"]["base_cents"] == 600 + 600 + 700
    assert lines["jon"]["base_cents"] == 600 + 700
    assert sum(b["owed_cents"] for b in detail["breakdown"]) == detail["totals"]["total_cents"]


def test_a_guest_cannot_take_more_portions_than_the_line_has():
    client = fresh_client()
    bill = mixed_bill(client)
    token = make_link(client, bill["bill_id"])
    r = guest().post(
        f"/api/share/{token}/claims",
        json={"party": bill["dana"], "portions": {str(bill["ids"]["Beer"]): 9}},
    )
    assert r.status_code == 400
    assert "more than the 3 portions" in r.json()["detail"]


def test_a_guest_cannot_claim_a_sub_item_directly():
    """Modifications belong to their dish; they are not separately claimable."""
    client = fresh_client()
    bill = mixed_bill(client)
    token = make_link(client, bill["bill_id"])

    with db.cursor() as conn:
        bacon_id = conn.execute(
            "SELECT id FROM bill_items WHERE bill_id=? AND label='Add bacon'",
            (bill["bill_id"],),
        ).fetchone()["id"]

    r = guest().post(f"/api/share/{token}/claims",
                     json={"party": bill["dana"], "item_ids": [bacon_id]})
    assert r.status_code == 400
    assert "extras that come with another item" in r.json()["detail"]


# --- joining -----------------------------------------------------------------

def test_a_newcomer_adds_their_own_name_and_picks():
    client = fresh_client()
    bill = itemized_bill(client)
    token = make_link(client, bill["bill_id"])
    visitor = guest()

    r = visitor.post(f"/api/share/{token}/join", json={"name": "Priya"})
    assert r.status_code == 200, r.text
    priya = r.json()["party"]
    assert r.json()["existing"] is False
    assert priya.startswith("g:")

    r = visitor.post(f"/api/share/{token}/claims",
                     json={"party": priya, "item_ids": [bill["items"]["Ribeye"]]})
    assert r.status_code == 200

    data = visitor.get(f"/api/share/{token}").json()
    priya_row = next(p for p in data["people"] if p["name"] == "Priya")
    assert priya_row["claimed_items"] == 1

    # And the owner sees her on the bill, in the group's guest list.
    detail = client.get(f"/api/bills/{bill['bill_id']}").json()
    assert "Priya" in {b["name"] for b in detail["breakdown"]}
    group = client.get(f"/api/groups/{bill['group_id']}").json()
    assert "Priya" in {p["name"] for p in group["parties"] if p["kind"] == "guest"}


def test_joining_with_a_name_already_on_the_bill_returns_that_person():
    """Two people typing "Dana" mean one Dana, not a duplicate that splits her items."""
    client = fresh_client()
    bill = itemized_bill(client)
    token = make_link(client, bill["bill_id"])

    r = guest().post(f"/api/share/{token}/join", json={"name": "dana"})
    assert r.status_code == 200
    assert r.json()["existing"] is True
    assert r.json()["party"] == bill["dana"]

    data = guest().get(f"/api/share/{token}").json()
    assert sum(1 for p in data["people"] if p["name"].lower() == "dana") == 1


def test_join_can_be_turned_off():
    client = fresh_client()
    bill = itemized_bill(client)
    token = make_link(client, bill["bill_id"], allow_join=False)

    r = guest().post(f"/api/share/{token}/join", json={"name": "Gatecrasher"})
    assert r.status_code == 403
    assert "turned off" in r.json()["detail"]

    client.patch(f"/api/bills/{bill['bill_id']}/share", json={"allow_join": True})
    assert guest().post(f"/api/share/{token}/join", json={"name": "Priya"}).status_code == 200


# --- closing -----------------------------------------------------------------

def test_closing_makes_it_read_only_and_reopening_restores_it():
    client = fresh_client()
    bill = itemized_bill(client)
    token = make_link(client, bill["bill_id"])
    visitor = guest()

    client.patch(f"/api/bills/{bill['bill_id']}/share", json={"closed": True})

    assert visitor.get(f"/api/share/{token}").status_code == 200, "closed still viewable"
    assert visitor.get(f"/api/share/{token}").json()["share"]["closed"] is True

    r = visitor.post(f"/api/share/{token}/claims",
                     json={"party": bill["dana"], "item_ids": [bill["items"]["Salad"]]})
    assert r.status_code == 409
    assert "closed" in r.json()["detail"]
    assert visitor.post(f"/api/share/{token}/join", json={"name": "Late"}).status_code == 409

    client.patch(f"/api/bills/{bill['bill_id']}/share", json={"closed": False})
    assert visitor.post(
        f"/api/share/{token}/claims",
        json={"party": bill["dana"], "item_ids": [bill["items"]["Salad"]]},
    ).status_code == 200


# --- the lost-update guard ---------------------------------------------------

def test_the_owner_cannot_silently_overwrite_picks_made_via_the_link():
    client = fresh_client()
    bill = itemized_bill(client)
    token = make_link(client, bill["bill_id"])

    # Owner opens the editor.
    loaded = client.get(f"/api/bills/{bill['bill_id']}").json()
    stale_revision = loaded["bill"]["revision"]

    # Meanwhile Dana picks the salad.
    guest().post(f"/api/share/{token}/claims",
                 json={"party": bill["dana"], "item_ids": [bill["items"]["Salad"]]})

    # Owner saves the version they loaded, which has no claims on it.
    payload = {
        "title": "Steakhouse", "split_mode": "itemized",
        "items": [{"label": i["label"], "amount": str(i["amount_cents"] / 100), "shares": {}}
                  for i in loaded["items"]],
        "participants": [{"party": p["party"]} for p in loaded["participants"]],
        "expected_revision": stale_revision,
    }
    r = client.put(f"/api/bills/{bill['bill_id']}", json=payload)
    assert r.status_code == 409, "a stale save must be refused"
    assert "changed while you had it open" in r.json()["detail"]

    # Dana's pick is untouched.
    data = guest().get(f"/api/share/{token}").json()
    assert next(i for i in data["items"] if i["label"] == "Salad")["claimed_by"] == [bill["dana"]]

    # Reloading and saving again works.
    fresh = client.get(f"/api/bills/{bill['bill_id']}").json()
    payload["expected_revision"] = fresh["bill"]["revision"]
    payload["items"] = [
        {"label": i["label"], "amount": str(i["amount_cents"] / 100), "shares": i["shares"]}
        for i in fresh["items"]
    ]
    assert client.put(f"/api/bills/{bill['bill_id']}", json=payload).status_code == 200


def test_a_save_without_the_version_field_still_works():
    """Omitting expected_revision keeps the old behaviour, so nothing else breaks."""
    client = fresh_client()
    bill = itemized_bill(client)
    r = client.put(
        f"/api/bills/{bill['bill_id']}",
        json={"title": "Renamed", "split_mode": "even", "subtotal": "10.00",
              "participants": [{"party": bill["jon"]}]},
    )
    assert r.status_code == 200
    assert r.json()["bill"]["title"] == "Renamed"


# --- non-itemized bills ------------------------------------------------------

def test_an_even_split_bill_still_lets_people_join_and_see_their_share():
    client = fresh_client()
    me = client.get("/api/me").json()["user"]
    jon = f"u:{me['id']}"
    group_id = client.post("/api/groups", json={"name": "Cabin"}).json()["group"]["id"]
    bill_id = client.post(
        f"/api/groups/{group_id}/bills",
        json={"title": "Cabin night", "split_mode": "even", "subtotal": "120.00",
              "participants": [{"party": jon}]},
    ).json()["bill_id"]
    token = make_link(client, bill_id)
    visitor = guest()

    data = visitor.get(f"/api/share/{token}").json()
    assert data["itemized"] is False
    assert data["items"] == []

    visitor.post(f"/api/share/{token}/join", json={"name": "Dana"})
    visitor.post(f"/api/share/{token}/join", json={"name": "Marcus"})

    data = visitor.get(f"/api/share/{token}").json()
    assert len(data["people"]) == 3
    assert all(p["owed_cents"] == 4000 for p in data["people"]), "even three-way split"


# --- pages -------------------------------------------------------------------

def test_the_share_url_serves_the_guest_page():
    client = fresh_client()
    bill = itemized_bill(client)
    token = make_link(client, bill["bill_id"])
    r = guest().get(f"/s/{token}")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "share.js" in r.text
    # The page itself is a static shell; the token is validated by the API.
    assert guest().get("/s/anything-at-all").status_code == 200
    assert guest().get("/js/share.js").status_code == 200


if __name__ == "__main__":
    failures = 0
    for name in sorted(n for n in list(globals()) if n.startswith("test_")):
        try:
            globals()[name]()
            print(f"  ok   {name}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            import traceback

            print(f"  FAIL {name}: {exc.__class__.__name__}: {exc}")
            traceback.print_exc(limit=3)
    shutil.rmtree(_TMP, ignore_errors=True)
    print("\nall passed" if not failures else f"\n{failures} failed")
    sys.exit(1 if failures else 0)
