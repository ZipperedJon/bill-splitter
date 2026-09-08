"""SQLite access and schema management.

A fresh connection per request; SQLite opens in microseconds and this keeps
threading rules trivial. WAL mode so a long read never blocks a write.

Money is stored as INTEGER cents everywhere. Percentages are REAL.
Never store money as a float.

A "party" is anyone who can be on a bill, identified by a string key:
    "u:<user_id>"   an app user
    "g:<guest_id>"  a guest who has no account (someone's partner, a friend
                    on the trip who never signed up)
That single key makes bills, balances and settlements uniform.
"""

from __future__ import annotations

import shutil
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import config

SCHEMA_VERSION = 4

DEFAULT_CATEGORIES = [
    ("Food & Restaurant", "restaurant", 10),
    ("Groceries", "groceries", 20),
    ("Airbnb / Hotel", "lodging", 30),
    ("Transport / Gas", "transport", 40),
    ("Flights", "flights", 50),
    ("Activities & Tickets", "activities", 60),
    ("Drinks & Nightlife", "drinks", 70),
    ("Utilities", "utilities", 80),
    ("Rent", "rent", 90),
    ("Shopping", "shopping", 100),
    ("Household", "household", 110),
    ("Other", "other", 999),
]

DEFAULT_SETTINGS = {
    "default_currency": "USD",
    "default_tax_percent": "0",
    "default_tip_percent": "18",
    "tip_base": "pre_tax",  # pre_tax | post_tax
    "registration_open": "1",  # admins can close signups entirely
    # The address people outside the house should use, e.g. a Cloudflare tunnel
    # hostname. Only needed so a share link created while on the LAN address is
    # still openable by a guest who is not on the LAN.
    "public_base_url": "",
    "update_repo": config.DEFAULT_UPDATE_REPO,
    "update_branch": config.DEFAULT_UPDATE_BRANCH,
    # Optional GitHub token, only needed to check a private repo. Set through
    # its own endpoint and never returned to the browser.
    "update_token": "",
    "auto_update_enabled": "0",
    "update_check_interval_minutes": "60",
    "last_update_check": "",
    "latest_known_commit": "",
    "latest_known_message": "",
}


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS users (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    username             TEXT NOT NULL COLLATE NOCASE,
    display_name         TEXT NOT NULL DEFAULT '',
    email                TEXT NOT NULL DEFAULT '',
    password_hash        TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending','active','suspended','rejected')),
    is_admin             INTEGER NOT NULL DEFAULT 0,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    note                 TEXT NOT NULL DEFAULT '',
    created_at           INTEGER NOT NULL,
    decided_at           INTEGER,
    decided_by           INTEGER REFERENCES users(id) ON DELETE SET NULL,
    last_login_at        INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    user_agent TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS categories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    icon       TEXT NOT NULL DEFAULT 'other',
    sort_order INTEGER NOT NULL DEFAULT 500,
    archived   INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_categories_name ON categories(name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    currency    TEXT NOT NULL DEFAULT 'USD',
    created_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at  INTEGER NOT NULL,
    archived    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS group_members (
    group_id  INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role      TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('owner','member')),
    joined_at INTEGER NOT NULL,
    PRIMARY KEY (group_id, user_id)
);

CREATE TABLE IF NOT EXISTS group_guests (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id   INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_guests_group ON group_guests(group_id);

CREATE TABLE IF NOT EXISTS bills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id        INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    title           TEXT NOT NULL,
    category_id     INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    notes           TEXT NOT NULL DEFAULT '',
    bill_date       TEXT NOT NULL,                -- ISO yyyy-mm-dd
    currency        TEXT NOT NULL DEFAULT 'USD',

    split_mode      TEXT NOT NULL DEFAULT 'even'
                    CHECK (split_mode IN ('even','shares','itemized')),
    subtotal_cents  INTEGER NOT NULL DEFAULT 0,   -- ignored when itemized (items are the truth)

    discount_mode   TEXT NOT NULL DEFAULT 'none' CHECK (discount_mode IN ('none','percent','amount')),
    discount_percent REAL NOT NULL DEFAULT 0,
    discount_cents  INTEGER NOT NULL DEFAULT 0,
    -- 'all' spreads the discount over everyone in proportion to their share.
    -- A party key ('u:3' / 'g:7') takes it off that one person - someone's
    -- coupon or comped dish should not quietly subsidise the whole table.
    discount_target TEXT NOT NULL DEFAULT 'all',

    tax_mode        TEXT NOT NULL DEFAULT 'none' CHECK (tax_mode IN ('none','percent','amount')),
    tax_percent     REAL NOT NULL DEFAULT 0,
    tax_cents       INTEGER NOT NULL DEFAULT 0,

    tip_mode        TEXT NOT NULL DEFAULT 'none' CHECK (tip_mode IN ('none','percent','amount')),
    tip_percent     REAL NOT NULL DEFAULT 0,
    tip_cents       INTEGER NOT NULL DEFAULT 0,
    tip_base        TEXT NOT NULL DEFAULT 'pre_tax' CHECK (tip_base IN ('pre_tax','post_tax')),

    created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    -- Bumped on every write. Used for optimistic concurrency when the owner is
    -- editing a bill while people tick items through a share link. updated_at
    -- cannot do this job: it has one-second resolution, so two writes in the
    -- same second look identical and a stale save would slip through.
    revision        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_bills_group ON bills(group_id, bill_date DESC);

CREATE TABLE IF NOT EXISTS bill_participants (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_id INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    party   TEXT NOT NULL,                        -- 'u:<id>' or 'g:<id>'
    weight  REAL NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_bp_bill_party ON bill_participants(bill_id, party);

CREATE TABLE IF NOT EXISTS bill_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_id      INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    -- A modification hanging off another line ("add bacon", "oat milk").
    -- Sub-items are never claimed on their own: their cost follows whoever
    -- claimed the parent, so ticking the burger picks up its extras too.
    -- One level deep only.
    parent_id    INTEGER REFERENCES bill_items(id) ON DELETE CASCADE,
    label        TEXT NOT NULL DEFAULT '',
    -- Line total, always. `portions` divides it into individually claimable
    -- parts (3 beers on one line), and a claim's weight is how many someone
    -- took - which the existing weighted split already handles.
    amount_cents INTEGER NOT NULL DEFAULT 0,
    portions     INTEGER NOT NULL DEFAULT 1,
    sort_order   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_items_bill ON bill_items(bill_id, sort_order);

CREATE TABLE IF NOT EXISTS bill_item_shares (
    item_id INTEGER NOT NULL REFERENCES bill_items(id) ON DELETE CASCADE,
    party   TEXT NOT NULL,
    weight  REAL NOT NULL DEFAULT 1,
    PRIMARY KEY (item_id, party)
);

CREATE TABLE IF NOT EXISTS bill_extras (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_id    INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    label      TEXT NOT NULL DEFAULT 'Fee',
    mode       TEXT NOT NULL DEFAULT 'amount' CHECK (mode IN ('percent','amount')),
    percent    REAL NOT NULL DEFAULT 0,
    cents      INTEGER NOT NULL DEFAULT 0,
    split      TEXT NOT NULL DEFAULT 'even' CHECK (split IN ('even','proportional')),
    sort_order INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_extras_bill ON bill_extras(bill_id, sort_order);

CREATE TABLE IF NOT EXISTS bill_payments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_id      INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    party        TEXT NOT NULL,
    amount_cents INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_payments_bill ON bill_payments(bill_id);

-- A share link for one bill. The token is the whole credential: anyone holding
-- it can pick who they are on that bill and tick what they had, without an
-- account. Stored in the clear so the owner can re-copy the link; rotate or
-- revoke to invalidate.
CREATE TABLE IF NOT EXISTS bill_shares (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_id      INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    token        TEXT NOT NULL,
    allow_join   INTEGER NOT NULL DEFAULT 1,   -- may a newcomer add their own name?
    closed       INTEGER NOT NULL DEFAULT 0,   -- read-only but still viewable
    expires_at   INTEGER,                      -- NULL = never
    views        INTEGER NOT NULL DEFAULT 0,
    last_seen_at INTEGER,
    created_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at   INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_shares_token ON bill_shares(token);
-- One live link per bill: rotating replaces it rather than piling up tokens
-- that all still work.
CREATE UNIQUE INDEX IF NOT EXISTS idx_shares_bill ON bill_shares(bill_id);

CREATE TABLE IF NOT EXISTS settlements (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id     INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    from_party   TEXT NOT NULL,
    to_party     TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    note         TEXT NOT NULL DEFAULT '',
    paid_on      TEXT NOT NULL,
    created_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_settlements_group ON settlements(group_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    actor_name TEXT NOT NULL DEFAULT '',
    action     TEXT NOT NULL,
    target     TEXT NOT NULL DEFAULT '',
    detail     TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC);

CREATE TABLE IF NOT EXISTS update_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   INTEGER NOT NULL,
    finished_at  INTEGER,
    status       TEXT NOT NULL,     -- running | success | failed | no-op | rolled-back
    trigger      TEXT NOT NULL,     -- manual | scheduled | startup
    from_commit  TEXT NOT NULL DEFAULT '',
    to_commit    TEXT NOT NULL DEFAULT '',
    from_version TEXT NOT NULL DEFAULT '',
    to_version   TEXT NOT NULL DEFAULT '',
    message      TEXT NOT NULL DEFAULT '',
    actor_name   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_update_started ON update_log(started_at DESC);
"""

# Indexes over columns that arrived after v1. These CANNOT live in SCHEMA:
# init_db runs SCHEMA before migrating, and on an existing database
# CREATE TABLE IF NOT EXISTS leaves the old table alone - so an index naming a
# column the migration has not added yet fails with "no such column" and takes
# the whole upgrade down with it. Created after migration, which covers a fresh
# database and an upgraded one alike.
LATE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_items_parent ON bill_items(parent_id);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    # check_same_thread=False is required, not merely convenient: FastAPI runs a
    # sync generator dependency in its threadpool, and the code after `yield`
    # (our commit/close) can land on a *different* worker thread than the one
    # that opened the connection. SQLite refuses that by default. It stays safe
    # because each request gets its own connection and never shares it - the
    # threads take turns, they do not overlap.
    conn = sqlite3.connect(str(path or config.DB_PATH), timeout=15.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


@contextmanager
def cursor() -> Iterator[sqlite3.Connection]:
    """Transactional connection: commits on clean exit, rolls back on exception."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --- schema ------------------------------------------------------------------

def init_db() -> None:
    config.ensure_dirs()
    with cursor() as conn:
        conn.executescript(SCHEMA)
        current = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if current is None:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
            )
        else:
            _migrate(conn, int(current["value"]))

        # Only safe once migrations have added the columns these name.
        conn.executescript(LATE_INDEXES)

        for key, value in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?,?)", (key, value))

        if conn.execute("SELECT COUNT(*) AS n FROM categories").fetchone()["n"] == 0:
            conn.executemany(
                "INSERT INTO categories(name, icon, sort_order) VALUES (?,?,?)",
                DEFAULT_CATEGORIES,
            )


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row["name"] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def _migrate(conn: sqlite3.Connection, from_version: int) -> None:
    """Apply forward migrations. Each step bumps meta.schema_version.

    init_db() runs the whole SCHEMA first, and every statement in it is
    CREATE ... IF NOT EXISTS - so a release that only *adds* a table or index
    needs nothing here beyond recording the bump. Steps that change an existing
    table (ALTER TABLE, backfills) go here.

    The updater calls init_db() after pulling, so a `git pull` that ships a
    schema change migrates itself before the new code serves a request.
    """
    version = from_version

    if version < 2:
        # v2 added bill_shares; SCHEMA's CREATE TABLE IF NOT EXISTS handled it.
        version = 2

    if version < 3:
        # v3 added bills.revision. A new column on an existing table does need
        # doing by hand - CREATE TABLE IF NOT EXISTS leaves the old table alone.
        if not _has_column(conn, "bills", "revision"):
            conn.execute("ALTER TABLE bills ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
        version = 3

    if version < 4:
        # v4 added targeted discounts, sub-items and divisible items.
        if not _has_column(conn, "bills", "discount_target"):
            conn.execute(
                "ALTER TABLE bills ADD COLUMN discount_target TEXT NOT NULL DEFAULT 'all'"
            )
        if not _has_column(conn, "bill_items", "parent_id"):
            # A column with REFERENCES is allowed by ALTER TABLE as long as it
            # defaults to NULL, which a top-level item's parent is anyway.
            conn.execute(
                "ALTER TABLE bill_items ADD COLUMN parent_id INTEGER REFERENCES bill_items(id)"
            )
        if not _has_column(conn, "bill_items", "portions"):
            conn.execute(
                "ALTER TABLE bill_items ADD COLUMN portions INTEGER NOT NULL DEFAULT 1"
            )
        version = 4

    if version != from_version:
        conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(version),))


def backup_database(label: str = "manual") -> Path | None:
    """Snapshot the DB using SQLite's online backup API (safe while running)."""
    if not config.DB_PATH.is_file():
        return None
    config.ensure_dirs()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = config.BACKUP_DIR / f"billsplit-{stamp}-{label}.db"
    src = connect()
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    _prune_backups(keep=15)
    return dest


def _prune_backups(keep: int) -> None:
    files = sorted(config.BACKUP_DIR.glob("billsplit-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in files[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass


# --- settings ----------------------------------------------------------------

def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def get_settings(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


# --- audit -------------------------------------------------------------------

def audit(
    conn: sqlite3.Connection,
    actor: dict[str, Any] | sqlite3.Row | None,
    action: str,
    target: str = "",
    detail: str = "",
) -> None:
    conn.execute(
        "INSERT INTO audit_log(actor_id, actor_name, action, target, detail, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            actor["id"] if actor else None,
            (actor["username"] if actor else "system"),
            action,
            target,
            detail,
            int(time.time()),
        ),
    )


def free_space_bytes() -> int:
    try:
        return shutil.disk_usage(str(config.DATA_DIR)).free
    except OSError:
        return -1
