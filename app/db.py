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

SCHEMA_VERSION = 1

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
    label        TEXT NOT NULL DEFAULT '',
    amount_cents INTEGER NOT NULL DEFAULT 0,
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

        for key, value in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?,?)", (key, value))

        if conn.execute("SELECT COUNT(*) AS n FROM categories").fetchone()["n"] == 0:
            conn.executemany(
                "INSERT INTO categories(name, icon, sort_order) VALUES (?,?,?)",
                DEFAULT_CATEGORIES,
            )


def _migrate(conn: sqlite3.Connection, from_version: int) -> None:
    """Apply forward migrations. Each step bumps meta.schema_version.

    Future releases add branches here; the updater calls init_db() on boot so
    a `git pull` that ships a schema change migrates itself.
    """
    version = from_version
    # (no migrations past v1 yet)
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
