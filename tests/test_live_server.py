"""Smoke tests against a real uvicorn server.

These exist because TestClient is not a faithful stand-in. It drives the app
through a single portal thread, so it happily passed a build where every
request 500'd in production: FastAPI runs sync generator dependencies on its
threadpool, and the code after `yield` can resume on a *different* worker
thread than the one that opened the SQLite connection.

Anything that only breaks under real threading, real cookies or real HTTP
belongs here.

Run: python tests/test_live_server.py
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="billsplit-live-")
os.environ["BILLSPLIT_DATA_DIR"] = _TMP
os.environ["BILLSPLIT_SECRET_KEY"] = "live-test-secret"
os.environ["BILLSPLIT_SKIP_BACKGROUND"] = "1"

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app import config as app_config  # noqa: E402
from app import db, security  # noqa: E402
from app.main import app  # noqa: E402

security.SCRYPT_N = 2 ** 8


def _wipe_data_dir() -> None:
    for name in os.listdir(_TMP):
        path = os.path.join(_TMP, name)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                os.remove(path)
            except OSError:
                pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, trusted_proxies: str | None = None) -> None:
        self.port = free_port()
        # One worker thread would hide the very bug this file exists to catch;
        # several make the threadpool actually hand work around.
        #
        # proxy_headers/forwarded_allow_ips have to be passed exactly as
        # app.main.main() passes them. Leaving them out made an earlier version
        # of the proxy test pass on uvicorn's own default rather than on our
        # configuration, which is not the same thing at all.
        config = uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="warning",
            proxy_headers=True,
            forwarded_allow_ips=(
                trusted_proxies if trusted_proxies is not None else app_config.TRUSTED_PROXIES
            ),
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> str:
        _wipe_data_dir()  # each test starts from a fresh install
        db.init_db()
        self.thread.start()
        base = f"http://127.0.0.1:{self.port}"
        deadline = time.time() + 25
        while time.time() < deadline:
            try:
                if httpx.get(f"{base}/api/health", timeout=1).status_code == 200:
                    return base
            except httpx.HTTPError:
                time.sleep(0.15)
        raise RuntimeError("server did not start")

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=15)


def test_full_flow_over_real_http():
    with Server() as base, httpx.Client(base_url=base, timeout=20) as client:
        assert client.get("/api/bootstrap").json()["needs_setup"] is True

        r = client.post("/api/auth/register", json={"username": "jon", "password": "livetestpass1"})
        assert r.status_code == 200, r.text
        assert r.json()["is_first_admin"] is True

        # The threading bug showed up on any endpoint using the DB dependency,
        # so hit a spread of them - and hit them repeatedly, since the failure
        # depends on which worker thread the generator resumes on.
        for _ in range(6):
            for path in ("/api/me", "/api/dashboard", "/api/groups", "/api/directory",
                         "/api/categories", "/api/admin/users", "/api/admin/settings",
                         "/api/admin/audit", "/api/system/update"):
                response = client.get(path)
                assert response.status_code == 200, f"{path} -> {response.status_code} {response.text[:300]}"

        me = client.get("/api/me").json()["user"]
        jon = f"u:{me['id']}"

        group_id = client.post(
            "/api/groups", json={"name": "Live Test", "guest_names": ["Dana"]}
        ).json()["group"]["id"]
        dana = next(
            p["party"] for p in client.get(f"/api/groups/{group_id}").json()["parties"]
            if p["name"] == "Dana"
        )

        r = client.post(
            f"/api/groups/{group_id}/bills",
            json={
                "title": "Dinner", "split_mode": "even", "subtotal": "80.00",
                "tax": {"mode": "percent", "percent": 8.875},
                "tip": {"mode": "percent", "percent": 20},
                "participants": [{"party": jon}, {"party": dana}],
                "payments": [{"party": jon, "amount": "103.10"}],
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["totals"]["total_cents"] == 8000 + 710 + 1600

        group = client.get(f"/api/groups/{group_id}").json()
        assert group["outstanding_cents"] == r.json()["totals"]["total_cents"] // 2

        assert client.get(f"/api/groups/{group_id}/export.csv").status_code == 200


def test_concurrent_requests_do_not_break_the_db():
    """Several browsers at once is the normal case for a shared bill splitter."""
    with Server() as base, httpx.Client(base_url=base, timeout=25) as client:
        client.post("/api/auth/register", json={"username": "jon", "password": "livetestpass1"})
        me = client.get("/api/me").json()["user"]
        jon = f"u:{me['id']}"
        group_id = client.post("/api/groups", json={"name": "Concurrent"}).json()["group"]["id"]

        for i in range(4):
            client.post(
                f"/api/groups/{group_id}/bills",
                json={"title": f"Bill {i}", "split_mode": "even", "subtotal": "10.00",
                      "participants": [{"party": jon}]},
            )

        cookies = dict(client.cookies)
        errors: list[str] = []

        def hammer() -> None:
            with httpx.Client(base_url=base, cookies=cookies, timeout=25) as c:
                for _ in range(8):
                    for path in ("/api/dashboard", f"/api/groups/{group_id}",
                                 f"/api/groups/{group_id}/bills", "/api/admin/users"):
                        try:
                            response = c.get(path)
                            if response.status_code != 200:
                                errors.append(f"{path} -> {response.status_code} {response.text[:200]}")
                        except httpx.HTTPError as exc:
                            errors.append(f"{path} -> {exc}")

        threads = [threading.Thread(target=hammer) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=90)

        assert not errors, "\n".join(errors[:10])


def test_static_assets_are_served():
    with Server() as base, httpx.Client(base_url=base, timeout=15) as client:
        index = client.get("/")
        assert index.status_code == 200
        assert "Bill Splitter" in index.text

        for asset in ("/css/styles.css", "/js/app.js", "/js/util.js", "/js/api.js",
                      "/js/views/bill.js", "/js/views/admin.js", "/js/views/groups.js",
                      "/js/views/dashboard.js", "/js/views/auth.js", "/js/views/account.js",
                      "/icon.svg", "/manifest.webmanifest"):
            response = client.get(asset)
            assert response.status_code == 200, f"{asset} missing"

        # CSP and friends should be on every response.
        assert "default-src 'self'" in index.headers.get("content-security-policy", "")
        assert index.headers.get("x-content-type-options") == "nosniff"


def test_the_frontend_is_revalidated_so_updates_actually_show_up():
    """The app updates itself, so a browser must never reuse the old frontend
    without asking. Without Cache-Control, browsers invent their own freshness
    window and quietly keep serving a stale app.js after an update - which looks
    exactly like the update having done nothing."""
    with Server() as base, httpx.Client(base_url=base, timeout=15) as client:
        for path in ("/", "/js/app.js", "/js/share.js", "/js/views/bill.js",
                     "/css/styles.css", "/manifest.webmanifest"):
            response = client.get(path)
            assert response.status_code == 200, path
            assert response.headers.get("cache-control") == "no-cache", (
                f"{path} must be revalidated, got "
                f"{response.headers.get('cache-control')!r}"
            )
            assert response.headers.get("etag"), f"{path} needs an ETag to revalidate against"

        # The share page is served by its own route; it needs the same treatment.
        share = client.get("/s/whatever")
        assert share.status_code == 200
        assert share.headers.get("cache-control") == "no-cache"

        # A CDN in front (Cloudflare tunnel, say) must not cache any of it
        # either: an edge-cached app.js survives a hard refresh AND a private
        # window, since the stale copy is not in the browser at all.
        for path in ("/", "/js/app.js", "/css/styles.css", "/api/health", "/s/whatever"):
            headers = client.get(path).headers
            assert headers.get("cdn-cache-control") == "no-store", path
            assert headers.get("cloudflare-cdn-cache-control") == "no-store", path

        # An unchanged file should then cost a 304 rather than a fresh body.
        first = client.get("/js/app.js")
        again = client.get("/js/app.js", headers={"If-None-Match": first.headers["etag"]})
        assert again.status_code == 304, "revalidation should be cheap, not a full re-download"


def test_the_ui_version_matches_the_shipped_version():
    """app.js carries its own version so a stale cached frontend can announce
    itself. If it drifts from VERSION the warning fires on every load (or never
    fires at all), so the two are pinned together here rather than by memory."""
    version = (open(os.path.join(ROOT, "VERSION"), encoding="utf-8").read().strip())
    app_js = open(os.path.join(ROOT, "static", "js", "app.js"), encoding="utf-8").read()
    expected = f"UI_VERSION = '{version}'"
    assert expected in app_js, (
        f"static/js/app.js should declare {expected} to match the VERSION file. "
        "Bump both together."
    )

    with Server() as base, httpx.Client(base_url=base, timeout=15) as client:
        assert client.get("/api/health").json()["version"] == version


def test_health_reports_the_client_address_it_sees():
    """The only way to check BILLSPLIT_TRUSTED_PROXIES took effect. An untrusted
    caller's X-Forwarded-For must be ignored, or the header would be a free pass
    around the per-IP sign-in rate limit."""
    with Server() as base, httpx.Client(base_url=base, timeout=15) as client:
        seen = client.get("/api/health").json()["client_ip"]
        assert seen == "127.0.0.1", seen

        # The shipped default trusts 127.0.0.1, and this request *is* from
        # 127.0.0.1, so the forwarded header is honoured - the tunnel-on-the-
        # same-box case, where that header is the only source of the real IP.
        forwarded = client.get(
            "/api/health", headers={"X-Forwarded-For": "203.0.113.9"}
        ).json()["client_ip"]
        assert forwarded == "203.0.113.9", "a trusted proxy's X-Forwarded-For should be used"

    # And the direction that matters for security: an untrusted caller saying
    # "I am someone else" must be ignored, or the header is a free pass around
    # the per-IP sign-in rate limit.
    with Server(trusted_proxies="192.168.10.236") as base, \
            httpx.Client(base_url=base, timeout=15) as client:
        ignored = client.get(
            "/api/health", headers={"X-Forwarded-For": "203.0.113.9"}
        ).json()["client_ip"]
        assert ignored == "127.0.0.1", (
            f"X-Forwarded-For from an untrusted peer must be ignored, got {ignored}"
        )


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
