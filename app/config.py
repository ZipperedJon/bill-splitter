"""Runtime configuration.

Two layers:
  * Process config (this file) comes from environment / .env - port, paths, secret.
    Changing it needs a restart, so it is the stuff that cannot change at runtime.
  * App settings (see app.db.get_setting) live in SQLite and are editable by an admin
    from the web UI - update repo, auto-update toggle, default currency, etc.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

# Repo root: .../bill-splitter (this file is <root>/app/config.py)
ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader. Existing environment variables always win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(ROOT / ".env")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip())
    except (TypeError, ValueError):
        return default


# --- Network -----------------------------------------------------------------
# 9100 by default: port 9000 on this Pi belongs to another project.
HOST: str = os.environ.get("BILLSPLIT_HOST", "0.0.0.0")
PORT: int = _env_int("BILLSPLIT_PORT", 9100)

# --- Storage -----------------------------------------------------------------
DATA_DIR: Path = Path(os.environ.get("BILLSPLIT_DATA_DIR", str(ROOT / "data"))).resolve()
DB_PATH: Path = DATA_DIR / "billsplit.db"
BACKUP_DIR: Path = DATA_DIR / "backups"
UPDATE_STATE_PATH: Path = DATA_DIR / "update-state.json"

# --- Security ----------------------------------------------------------------
SESSION_COOKIE: str = "billsplit_session"
SESSION_DAYS: int = _env_int("BILLSPLIT_SESSION_DAYS", 30)
# Only set the Secure cookie flag when actually served over TLS; a Pi on a LAN
# is usually plain http and a Secure cookie would silently never be stored.
COOKIE_SECURE: bool = _env_bool("BILLSPLIT_COOKIE_SECURE", False)

# --- Updater -----------------------------------------------------------------
# Defaults; an admin can override the repo/branch/toggle in the UI (stored in DB).
DEFAULT_UPDATE_REPO: str = os.environ.get("BILLSPLIT_UPDATE_REPO", "")
DEFAULT_UPDATE_BRANCH: str = os.environ.get("BILLSPLIT_UPDATE_BRANCH", "main")
# systemd unit name, used to restart cleanly after an update.
SERVICE_NAME: str = os.environ.get("BILLSPLIT_SERVICE", "bill-splitter")

VERSION: str = (ROOT / "VERSION").read_text(encoding="utf-8").strip() if (ROOT / "VERSION").is_file() else "0.0.0"


def secret_key() -> str:
    """Signing secret. Generated once into data/secret.key if not supplied by env."""
    env = os.environ.get("BILLSPLIT_SECRET_KEY")
    if env:
        return env
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / "secret.key"
    if not path.is_file():
        path.write_text(secrets.token_urlsafe(48), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass  # non-POSIX filesystem
    return path.read_text(encoding="utf-8").strip()


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
