"""Schema migration tests.

Existing installs reach a new schema by way of auto-update: it pulls, then
calls init_db() before the new code serves anything. So the upgrade path has to
work on a database with real rows in it, not just on an empty one.

Run: python tests/test_migrations.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="billsplit-migrate-")
os.environ["BILLSPLIT_DATA_DIR"] = _TMP
os.environ["BILLSPLIT_SECRET_KEY"] = "migrate-test"
os.environ["BILLSPLIT_SKIP_BACKGROUND"] = "1"

from app import db  # noqa: E402


def wipe() -> None:
    for name in os.listdir(_TMP):
        path = os.path.join(_TMP, name)
        shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) else os.remove(path)


# The bills table exactly as schema v1 declared it - no `revision`. Rebuilt by
# hand rather than with ALTER TABLE ... DROP COLUMN, both because that is what a
# real v1 database contains and because DROP COLUMN makes SQLite re-parse the
# stored CREATE statement, which trips over the comments in our schema.
V1_BILLS = """
CREATE TABLE bills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id        INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    title           TEXT NOT NULL,
    category_id     INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    notes           TEXT NOT NULL DEFAULT '',
    bill_date       TEXT NOT NULL,
    currency        TEXT NOT NULL DEFAULT 'USD',
    split_mode      TEXT NOT NULL DEFAULT 'even'
                    CHECK (split_mode IN ('even','shares','itemized')),
    subtotal_cents  INTEGER NOT NULL DEFAULT 0,
    discount_mode   TEXT NOT NULL DEFAULT 'none' CHECK (discount_mode IN ('none','percent','amount')),
    discount_percent REAL NOT NULL DEFAULT 0,
    discount_cents  INTEGER NOT NULL DEFAULT 0,
    tax_mode        TEXT NOT NULL DEFAULT 'none' CHECK (tax_mode IN ('none','percent','amount')),
    tax_percent     REAL NOT NULL DEFAULT 0,
    tax_cents       INTEGER NOT NULL DEFAULT 0,
    tip_mode        TEXT NOT NULL DEFAULT 'none' CHECK (tip_mode IN ('none','percent','amount')),
    tip_percent     REAL NOT NULL DEFAULT 0,
    tip_cents       INTEGER NOT NULL DEFAULT 0,
    tip_base        TEXT NOT NULL DEFAULT 'pre_tax' CHECK (tip_base IN ('pre_tax','post_tax')),
    created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
)
"""


def make_v1_database() -> None:
    """A database as it looked at schema v1: no bill_shares, no bills.revision."""
    wipe()
    db.init_db()
    with db.cursor() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DROP TABLE bill_shares")
        conn.execute("DROP TABLE bills")
        conn.execute(V1_BILLS)
        conn.execute("CREATE INDEX idx_bills_group ON bills(group_id, bill_date DESC)")
        conn.execute("UPDATE meta SET value='1' WHERE key='schema_version'")

        # Real data, so we can prove the upgrade does not lose any of it.
        conn.execute(
            """INSERT INTO users(username, display_name, password_hash, status, is_admin, created_at)
               VALUES ('jon','Jon','scrypt$1$1$1$AA$AA','active',1,1)"""
        )
        conn.execute(
            "INSERT INTO groups(name, currency, created_by, created_at) VALUES ('Trip','USD',1,1)"
        )
        conn.execute(
            "INSERT INTO group_members(group_id, user_id, role, joined_at) VALUES (1,1,'owner',1)"
        )
        conn.execute(
            """INSERT INTO bills(group_id, title, bill_date, currency, split_mode,
                                 subtotal_cents, created_by, created_at, updated_at)
               VALUES (1,'Old dinner','2026-01-01','USD','even',5000,1,1,1)"""
        )
        conn.execute("INSERT INTO bill_participants(bill_id, party, weight) VALUES (1,'u:1',1)")


def version() -> int:
    with db.cursor() as conn:
        return int(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"])


def columns(table: str) -> set[str]:
    with db.cursor() as conn:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def tables() -> set[str]:
    with db.cursor() as conn:
        return {
            row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }


def test_a_v1_database_upgrades_and_keeps_its_data():
    make_v1_database()
    assert version() == 1
    assert "bill_shares" not in tables()
    assert "revision" not in columns("bills")

    db.init_db()  # what the updater calls after pulling new code

    assert version() == db.SCHEMA_VERSION
    assert "bill_shares" in tables(), "the new table should have been created"
    assert "revision" in columns("bills"), "the new column should have been added"

    with db.cursor() as conn:
        bill = conn.execute("SELECT * FROM bills WHERE id=1").fetchone()
        assert bill["title"] == "Old dinner"
        assert bill["subtotal_cents"] == 5000
        assert bill["revision"] == 0, "existing rows start at revision 0"
        assert conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 1
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM bill_participants"
        ).fetchone()["n"] == 1


def test_the_upgraded_database_actually_works():
    """Not just the right shape - the app can use it."""
    make_v1_database()
    db.init_db()

    from fastapi.testclient import TestClient

    from app import security
    from app.main import app

    security.SCRYPT_N = 2 ** 8
    client = TestClient(app)

    # The seeded admin's hash is fake, so give it a real one and sign in.
    with db.cursor() as conn:
        conn.execute(
            "UPDATE users SET password_hash=? WHERE id=1",
            (security.hash_password("migratedpass1"),),
        )
    assert client.post(
        "/api/auth/login", json={"username": "jon", "password": "migratedpass1"}
    ).status_code == 200

    # The old bill still computes, and the new share feature works on it.
    detail = client.get("/api/bills/1").json()
    assert detail["bill"]["title"] == "Old dinner"
    assert detail["totals"]["total_cents"] == 5000

    r = client.post("/api/bills/1/share", json={})
    assert r.status_code == 200, r.text
    token = r.json()["share"]["token"]
    assert TestClient(app).get(f"/api/share/{token}").status_code == 200


def test_init_is_idempotent():
    """Boot runs init_db() every time; it must not churn or re-migrate."""
    wipe()
    db.init_db()
    first = version()
    for _ in range(3):
        db.init_db()
    assert version() == first == db.SCHEMA_VERSION

    with db.cursor() as conn:
        # Defaults are seeded once, not duplicated.
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM categories WHERE name='Other'"
        ).fetchone()["n"] == 1


def test_a_fresh_database_starts_at_the_current_version():
    wipe()
    db.init_db()
    assert version() == db.SCHEMA_VERSION
    assert "bill_shares" in tables()
    assert "revision" in columns("bills")


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
