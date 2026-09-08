"""Self-update from GitHub.

An admin can check and apply updates by hand, or leave auto-update on and the
app pulls new commits on a timer.

The update is deliberately paranoid, because the thing being updated is the
thing doing the updating:

  1. Refuse if the working tree has local edits - never clobber someone's
     hand-patched file.
  2. Back up the database first.
  3. `git fetch` then `git reset --hard` to the fetched commit.
  4. `pip install -r requirements.txt` only when that file actually changed.
  5. Smoke-test the new code in a *separate process* (`python -c "import
     app.main"`). If it will not even import, roll straight back to the old
     commit and stay up on the old version.
  6. Only then restart, and only if systemd is around to restart us.

Anything unexpected leaves the running version untouched and writes a row in
update_log that the admin page shows.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from . import config, db

GITHUB_API = "https://api.github.com"
USER_AGENT = f"bill-splitter/{config.VERSION}"
GIT_TIMEOUT = 180
PIP_TIMEOUT = 900

_lock = threading.Lock()
_in_progress = False


class UpdateError(RuntimeError):
    """Something went wrong, but the running app is unharmed."""


# --- local git state ---------------------------------------------------------

def _git(*args: str, check: bool = True, timeout: int = GIT_TIMEOUT) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(config.ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise UpdateError(
            f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()[:500]}"
        )
    return (proc.stdout or "").strip()


def is_git_repo() -> bool:
    if not (config.ROOT / ".git").exists():
        return False
    try:
        return _git("rev-parse", "--is-inside-work-tree", check=False) == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def local_commit() -> str:
    try:
        return _git("rev-parse", "HEAD", check=False)
    except (OSError, subprocess.SubprocessError, UpdateError):
        return ""


def local_commit_short() -> str:
    return local_commit()[:8]


def local_branch() -> str:
    try:
        return _git("rev-parse", "--abbrev-ref", "HEAD", check=False)
    except (OSError, subprocess.SubprocessError, UpdateError):
        return ""


def working_tree_dirty() -> list[str]:
    """Tracked files with local modifications. Untracked files are fine."""
    try:
        out = _git("status", "--porcelain", "--untracked-files=no", check=False)
    except (OSError, subprocess.SubprocessError, UpdateError):
        return []
    return [line[3:] for line in out.splitlines() if line.strip()]


def remote_url() -> str:
    try:
        return _git("config", "--get", "remote.origin.url", check=False)
    except (OSError, subprocess.SubprocessError, UpdateError):
        return ""


def under_systemd() -> bool:
    """systemd sets INVOCATION_ID for services it starts."""
    return bool(os.environ.get("INVOCATION_ID"))


# --- GitHub ------------------------------------------------------------------

def _api(path: str, token: str = "") -> Any:
    request = urllib.request.Request(
        f"{GITHUB_API}{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError(
                "GitHub returned 404. Check the repo name, and add an access "
                "token in settings if the repo is private."
            ) from exc
        if exc.code in (401, 403):
            detail = "rate limit or permissions"
            raise UpdateError(f"GitHub refused the request ({exc.code}: {detail}).") from exc
        raise UpdateError(f"GitHub error {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise UpdateError(f"Could not reach GitHub: {exc.reason}") from exc
    except (TimeoutError, json.JSONDecodeError) as exc:
        raise UpdateError(f"Bad response from GitHub: {exc}") from exc


def check_remote(repo: str, branch: str, token: str = "") -> dict[str, Any]:
    """Latest commit on `branch`, plus how far ahead of us it is."""
    if not repo:
        raise UpdateError("No update repository configured.")

    head = _api(f"/repos/{repo}/commits/{branch}", token)
    latest_sha = head.get("sha", "")
    commit = head.get("commit", {}) or {}
    info: dict[str, Any] = {
        "latest_commit": latest_sha,
        "latest_commit_short": latest_sha[:8],
        "latest_message": (commit.get("message") or "").split("\n")[0][:200],
        "latest_author": ((commit.get("author") or {}).get("name") or "")[:80],
        "latest_date": (commit.get("author") or {}).get("date") or "",
        "commits_behind": 0,
        "changelog": [],
    }

    current = local_commit()
    if current and latest_sha and current != latest_sha:
        try:
            comparison = _api(f"/repos/{repo}/compare/{current}...{latest_sha}", token)
            info["commits_behind"] = int(comparison.get("ahead_by") or 0)
            info["changelog"] = [
                {
                    "sha": (c.get("sha") or "")[:8],
                    "message": ((c.get("commit") or {}).get("message") or "").split("\n")[0][:160],
                    "author": (((c.get("commit") or {}).get("author") or {}).get("name") or "")[:60],
                    "date": ((c.get("commit") or {}).get("author") or {}).get("date") or "",
                }
                for c in (comparison.get("commits") or [])
            ][-30:]
        except UpdateError:
            # The compare endpoint fails if our commit is not on the remote
            # (e.g. installed from a tarball). An update is still possible.
            info["commits_behind"] = 1
    return info


# --- status ------------------------------------------------------------------

def status(conn: sqlite3.Connection) -> dict[str, Any]:
    settings = db.get_settings(conn)
    dirty = working_tree_dirty()
    last_run = conn.execute(
        "SELECT * FROM update_log ORDER BY started_at DESC, id DESC LIMIT 1"
    ).fetchone()

    latest = settings.get("latest_known_commit", "")
    current = local_commit()

    return {
        "version": config.VERSION,
        "current_commit": current,
        "current_commit_short": current[:8],
        "branch": local_branch(),
        "configured_branch": settings.get("update_branch", "main"),
        "repo": settings.get("update_repo", ""),
        "has_token": bool(settings.get("update_token", "")),
        "remote_url": remote_url(),
        "is_git_repo": is_git_repo(),
        "under_systemd": under_systemd(),
        "service_name": config.SERVICE_NAME,
        "auto_update_enabled": settings.get("auto_update_enabled") == "1",
        "check_interval_minutes": int(settings.get("update_check_interval_minutes") or 60),
        "last_check": settings.get("last_update_check", ""),
        "latest_known_commit": latest,
        "latest_known_commit_short": latest[:8],
        "latest_known_message": settings.get("latest_known_message", ""),
        "update_available": bool(latest and current and latest != current),
        "working_tree_dirty": dirty,
        "in_progress": _in_progress,
        "last_run": dict(last_run) if last_run else None,
    }


def history(conn: sqlite3.Connection, limit: int = 25) -> list[dict[str, Any]]:
    return [
        dict(r) for r in conn.execute(
            "SELECT * FROM update_log ORDER BY started_at DESC, id DESC LIMIT ?", (limit,)
        )
    ]


def do_check(conn: sqlite3.Connection) -> dict[str, Any]:
    """Ask GitHub what the latest commit is and remember the answer."""
    settings = db.get_settings(conn)
    repo = settings.get("update_repo", "")
    branch = settings.get("update_branch", "main") or "main"
    token = settings.get("update_token", "")
    if not repo:
        raise UpdateError(
            "No update repository set yet. Add it under Admin -> Updates "
            "(for example: yourname/bill-splitter)."
        )

    info = check_remote(repo, branch, token)
    db.set_setting(conn, "last_update_check", time.strftime("%Y-%m-%d %H:%M:%S"))
    db.set_setting(conn, "latest_known_commit", info["latest_commit"])
    db.set_setting(conn, "latest_known_message", info["latest_message"])
    conn.commit()
    return {**status(conn), **info}


# --- applying ----------------------------------------------------------------

def _log_start(conn: sqlite3.Connection, trigger: str, actor: str, from_commit: str) -> int:
    cur = conn.execute(
        """INSERT INTO update_log(started_at, status, trigger, from_commit, from_version, actor_name)
           VALUES (?, 'running', ?, ?, ?, ?)""",
        (int(time.time()), trigger, from_commit, config.VERSION, actor),
    )
    conn.commit()
    return int(cur.lastrowid)


def _log_finish(
    conn: sqlite3.Connection, log_id: int, status_text: str, message: str,
    to_commit: str = "", to_version: str = "",
) -> None:
    conn.execute(
        """UPDATE update_log SET finished_at=?, status=?, message=?, to_commit=?, to_version=?
            WHERE id=?""",
        (int(time.time()), status_text, message[:2000], to_commit, to_version, log_id),
    )
    conn.commit()


def _read_version() -> str:
    path = config.ROOT / "VERSION"
    return path.read_text(encoding="utf-8").strip() if path.is_file() else config.VERSION


def _pip_install() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
         "-r", str(config.ROOT / "requirements.txt")],
        cwd=str(config.ROOT), capture_output=True, text=True, timeout=PIP_TIMEOUT,
    )
    if proc.returncode != 0:
        raise UpdateError(f"pip install failed: {(proc.stderr or proc.stdout).strip()[-800:]}")


def _smoke_test() -> None:
    """Import the app in a fresh process. Catches syntax errors and bad imports
    before we hand the port to the new code."""
    proc = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=str(config.ROOT), capture_output=True, text=True, timeout=120,
        env={**os.environ, "BILLSPLIT_SKIP_BACKGROUND": "1"},
    )
    if proc.returncode != 0:
        raise UpdateError(f"new version failed to import: {(proc.stderr or '').strip()[-800:]}")


def apply_update(
    conn: sqlite3.Connection, trigger: str = "manual", actor: str = "", force: bool = False
) -> dict[str, Any]:
    global _in_progress

    if not _lock.acquire(blocking=False):
        raise UpdateError("An update is already running.")
    _in_progress = True
    log_id: int | None = None
    from_commit = local_commit()

    try:
        if not is_git_repo():
            raise UpdateError(
                "This install is not a git checkout, so it cannot self-update. "
                "Re-install with install.sh to enable updates."
            )

        settings = db.get_settings(conn)
        repo = settings.get("update_repo", "")
        branch = settings.get("update_branch", "main") or "main"
        if not repo:
            raise UpdateError("No update repository set. Add it under Admin -> Updates.")

        dirty = working_tree_dirty()
        if dirty and not force:
            raise UpdateError(
                "Local changes to " + ", ".join(dirty[:5])
                + (" and others" if len(dirty) > 5 else "")
                + ". Commit or discard them, or use Force update to overwrite."
            )

        log_id = _log_start(conn, trigger, actor or "system", from_commit)
        db.backup_database(label="pre-update")

        _git("fetch", "--prune", "origin", branch)
        target = _git("rev-parse", "FETCH_HEAD")
        if target == from_commit:
            _log_finish(conn, log_id, "no-op", "Already up to date.", target, config.VERSION)
            return {"status": "no-op", "message": "Already up to date.", "commit": target}

        requirements_changed = bool(
            _git("diff", "--name-only", from_commit, target, "--", "requirements.txt", check=False)
        ) if from_commit else True

        _git("reset", "--hard", target)

        try:
            if requirements_changed:
                _pip_install()
            _smoke_test()
        except UpdateError as exc:
            # Put everything back exactly as it was and stay on the old version.
            rollback_note = ""
            try:
                _git("reset", "--hard", from_commit)
                if requirements_changed:
                    _pip_install()
            except UpdateError as rollback_exc:
                rollback_note = f" ROLLBACK ALSO FAILED: {rollback_exc}"
            _log_finish(
                conn, log_id, "rolled-back",
                f"{exc}{rollback_note} Stayed on {from_commit[:8]}.",
                from_commit, config.VERSION,
            )
            db.audit(conn, None, "update.rolled_back", detail=str(exc)[:400])
            conn.commit()
            raise UpdateError(f"Update rolled back: {exc}") from exc

        new_version = _read_version()
        # Pick up any schema changes the new version ships before it boots.
        db.init_db()

        _log_finish(
            conn, log_id, "success",
            f"Updated {from_commit[:8]} -> {target[:8]}.", target, new_version,
        )
        db.audit(
            conn, None, "update.applied",
            detail=f"{from_commit[:8]} -> {target[:8]} (v{config.VERSION} -> v{new_version}) "
                   f"by {actor or trigger}",
        )
        conn.commit()

        restarting = under_systemd()
        if restarting:
            schedule_restart()

        return {
            "status": "success",
            "from_commit": from_commit,
            "to_commit": target,
            "from_version": config.VERSION,
            "to_version": new_version,
            "restarting": restarting,
            "message": (
                f"Updated to {target[:8]} (v{new_version}). Restarting now - "
                "give it a few seconds and reload."
                if restarting else
                f"Updated to {target[:8]} (v{new_version}). Restart the app to load the new code "
                f"(`sudo systemctl restart {config.SERVICE_NAME}`)."
            ),
        }

    except UpdateError as exc:
        if log_id is not None:
            try:
                _log_finish(conn, log_id, "failed", str(exc), "", config.VERSION)
            except sqlite3.Error:
                pass
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        if log_id is not None:
            try:
                _log_finish(conn, log_id, "failed", f"{exc.__class__.__name__}: {exc}", "", config.VERSION)
            except sqlite3.Error:
                pass
        raise UpdateError(f"Update failed: {exc}") from exc
    finally:
        _in_progress = False
        _lock.release()


def schedule_restart(delay: float = 1.5) -> None:
    """Exit shortly, so the HTTP response gets out first; systemd restarts us."""

    def _bye() -> None:
        time.sleep(delay)
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(10)
        os._exit(0)  # graceful shutdown hung; leave anyway so systemd restarts

    threading.Thread(target=_bye, name="restart", daemon=True).start()
