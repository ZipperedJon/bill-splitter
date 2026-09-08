"""Updater tests against a throwaway git repo.

The rollback path is the one piece of this app that, if it misbehaves, takes
the whole thing offline on a Pi that may be in another room. So it gets tested
for real: a local "origin", a genuinely broken commit pushed to it, and an
assertion that the app ends up back on the working version.

Nothing here touches GitHub or the real checkout.

Run: python tests/test_updater.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

REAL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REAL_ROOT)

_TMP = tempfile.mkdtemp(prefix="billsplit-upd-")
os.environ["BILLSPLIT_DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["BILLSPLIT_SECRET_KEY"] = "updater-test"
os.environ["BILLSPLIT_SKIP_BACKGROUND"] = "1"

from app import config, db, updater  # noqa: E402


def git(cwd: str, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def build_sandbox() -> tuple[str, str]:
    """A working checkout plus a bare 'origin' it can fetch from."""
    root = tempfile.mkdtemp(prefix="billsplit-checkout-", dir=_TMP)
    origin = tempfile.mkdtemp(prefix="billsplit-origin-", dir=_TMP)

    for name in ("app", "static", "requirements.txt", "VERSION"):
        src = os.path.join(REAL_ROOT, name)
        dst = os.path.join(root, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(src, dst)

    git(origin, "init", "--bare", "-q", "-b", "main")
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "baseline")
    git(root, "remote", "add", "origin", origin)
    git(root, "push", "-q", "origin", "main")
    return root, origin


def push_commit(origin: str, message: str, changes: dict[str, str]) -> str:
    """Clone origin, apply file changes, commit, push. Returns the new sha."""
    work = tempfile.mkdtemp(prefix="billsplit-push-", dir=_TMP)
    git(_TMP, "clone", "-q", origin, work)
    git(work, "config", "user.email", "test@example.com")
    git(work, "config", "user.name", "Test")
    git(work, "config", "commit.gpgsign", "false")
    for relative, content in changes.items():
        path = os.path.join(work, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", message)
    git(work, "push", "-q", "origin", "main")
    return git(work, "rev-parse", "HEAD")


class Sandbox:
    """Points app.config.ROOT at a temp checkout for the duration of a test."""

    def __enter__(self):
        shutil.rmtree(os.environ["BILLSPLIT_DATA_DIR"], ignore_errors=True)
        self.root, self.origin = build_sandbox()
        self._saved_root = config.ROOT
        config.ROOT = __import__("pathlib").Path(self.root)
        db.init_db()
        with db.cursor() as conn:
            db.set_setting(conn, "update_repo", "example/repo")  # only git is used here
            db.set_setting(conn, "update_branch", "main")
        return self

    def __exit__(self, *exc):
        config.ROOT = self._saved_root

    def head(self) -> str:
        return git(self.root, "rev-parse", "HEAD")

    def version(self) -> str:
        with open(os.path.join(self.root, "VERSION"), encoding="utf-8") as handle:
            return handle.read().strip()


def test_no_op_when_already_current():
    with Sandbox() as box, db.cursor() as conn:
        before = box.head()
        result = updater.apply_update(conn, trigger="manual", actor="test")
        assert result["status"] == "no-op", result
        assert box.head() == before


def test_applies_a_good_update():
    with Sandbox() as box:
        target = push_commit(box.origin, "bump", {"VERSION": "9.9.9\n"})
        with db.cursor() as conn:
            result = updater.apply_update(conn, trigger="manual", actor="test")
        assert result["status"] == "success", result
        assert result["to_commit"] == target
        assert box.head() == target
        assert box.version() == "9.9.9"

        # Having just installed it, the app must not still advertise it.
        with db.cursor() as conn:
            assert db.get_setting(conn, "latest_known_commit") == target
            assert updater.status(conn)["update_available"] is False


def test_broken_update_is_rolled_back_and_the_app_stays_up():
    """The safety net: if the new code will not import, go back and stay alive."""
    with Sandbox() as box:
        good = box.head()
        push_commit(
            box.origin,
            "ship a syntax error",
            {
                "VERSION": "6.6.6\n",
                # Valid file, invalid Python - exactly what a bad push looks like.
                "app/splitter.py": "def compute_bill(  <<< this is not python\n",
            },
        )

        with db.cursor() as conn:
            try:
                updater.apply_update(conn, trigger="manual", actor="test")
                raise AssertionError("a broken update should not report success")
            except updater.UpdateError as exc:
                assert "rolled back" in str(exc).lower(), exc

        assert box.head() == good, "should be back on the previous commit"
        assert box.version() != "6.6.6", "VERSION should have been reverted too"

        # And the reverted checkout still imports, i.e. the Pi is serving fine.
        proc = subprocess.run(
            [sys.executable, "-c", "import app.main"],
            cwd=box.root, capture_output=True, text=True,
            env={**os.environ, "BILLSPLIT_SKIP_BACKGROUND": "1"},
        )
        assert proc.returncode == 0, proc.stderr[-500:]

        with db.cursor() as conn:
            last = updater.history(conn)[0]
        assert last["status"] == "rolled-back", last
        assert good.startswith(last["to_commit"][:8]) or last["to_commit"] == good


def test_local_edits_block_an_update_unless_forced():
    with Sandbox() as box:
        target = push_commit(box.origin, "bump", {"VERSION": "2.0.0\n"})

        # Someone hand-patched a file on the Pi.
        with open(os.path.join(box.root, "VERSION"), "w", encoding="utf-8") as handle:
            handle.write("1.0.0-local\n")

        assert updater.working_tree_dirty(), "the dirty check should see the edit"

        with db.cursor() as conn:
            try:
                updater.apply_update(conn, trigger="manual", actor="test")
                raise AssertionError("a dirty tree should refuse to update")
            except updater.UpdateError as exc:
                assert "local changes" in str(exc).lower(), exc

        assert box.version() == "1.0.0-local", "the local edit must survive a refusal"

        # force=true is the explicit override.
        with db.cursor() as conn:
            result = updater.apply_update(conn, trigger="manual", actor="test", force=True)
        assert result["status"] == "success", result
        assert box.head() == target
        assert box.version() == "2.0.0"


def test_a_backup_is_taken_before_updating():
    with Sandbox() as box:
        push_commit(box.origin, "bump", {"VERSION": "3.0.0\n"})
        with db.cursor() as conn:
            updater.apply_update(conn, trigger="manual", actor="test")
        backups = os.listdir(config.BACKUP_DIR)
        assert any("pre-update" in name for name in backups), backups


def test_status_reports_a_non_git_install():
    """A tarball install must say so rather than pretending it can update."""
    plain = tempfile.mkdtemp(prefix="billsplit-plain-", dir=_TMP)
    saved = config.ROOT
    config.ROOT = __import__("pathlib").Path(plain)
    try:
        assert updater.is_git_repo() is False
        with db.cursor() as conn:
            try:
                updater.apply_update(conn, trigger="manual", actor="test")
                raise AssertionError("should refuse without git")
            except updater.UpdateError as exc:
                assert "not a git checkout" in str(exc)
    finally:
        config.ROOT = saved


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
            traceback.print_exc(limit=4)
    shutil.rmtree(_TMP, ignore_errors=True)
    print("\nall passed" if not failures else f"\n{failures} failed")
    sys.exit(1 if failures else 0)
