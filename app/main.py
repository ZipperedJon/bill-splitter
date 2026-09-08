"""Application entrypoint.

Run directly (`python -m app.main`) or under the systemd unit that install.sh
writes. Serves the JSON API under /api and the single-page frontend from
static/.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, updater
from .routers import admin, auth, bills, groups, share, system

log = logging.getLogger("billsplit")

STATIC_DIR = config.ROOT / "static"


async def _update_scheduler() -> None:
    """Check GitHub on a timer; apply automatically when the admin allows it."""
    await asyncio.sleep(30)  # let the app finish booting before touching the network
    while True:
        interval_minutes = 60
        try:
            with db.cursor() as conn:
                settings = db.get_settings(conn)
                interval_minutes = max(5, int(settings.get("update_check_interval_minutes") or 60))
                repo = settings.get("update_repo", "")
                auto = settings.get("auto_update_enabled") == "1"

            if repo:
                loop = asyncio.get_running_loop()
                info = await loop.run_in_executor(None, _check_sync)
                if auto and info.get("update_available"):
                    log.info("auto-update: applying %s", info.get("latest_known_commit_short"))
                    await loop.run_in_executor(None, _apply_sync)
        except Exception as exc:  # noqa: BLE001 - a bad check must never kill the loop
            log.warning("update check failed: %s", exc)

        await asyncio.sleep(interval_minutes * 60)


def _check_sync() -> dict[str, Any]:
    with db.cursor() as conn:
        try:
            updater.do_check(conn)
        except updater.UpdateError as exc:
            log.warning("update check: %s", exc)
            return {}
        return updater.status(conn)


def _apply_sync() -> None:
    with db.cursor() as conn:
        try:
            result = updater.apply_update(conn, trigger="scheduled", actor="auto-update")
            log.info("auto-update: %s", result.get("message"))
        except updater.UpdateError as exc:
            log.error("auto-update failed: %s", exc)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    with db.cursor() as conn:
        from . import security

        security.purge_expired_sessions(conn)
        users = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]

    # The commit goes in the boot line so `journalctl -u bill-splitter` answers
    # "which version is actually running" without any guesswork.
    log.info(
        "Bill Splitter v%s (%s) on http://%s:%s (%s)",
        config.VERSION, updater.local_commit_short() or "no git",
        config.HOST, config.PORT,
        "fresh install - first sign-up becomes admin" if users == 0 else f"{users} account(s)",
    )

    task: asyncio.Task | None = None
    if not os.environ.get("BILLSPLIT_SKIP_BACKGROUND"):
        task = asyncio.create_task(_update_scheduler())

    try:
        yield
    finally:
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


app = FastAPI(
    title="Bill Splitter",
    version=config.VERSION,
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)

app.include_router(system.router)
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(groups.router)
app.include_router(bills.router)
app.include_router(share.router)


# --- versioned assets --------------------------------------------------------
#
# Every asset URL carries this tag, and it changes whenever the code does. That
# is what makes an update land no matter what is caching in between: a browser,
# a CDN or a corporate proxy cannot serve a stale copy for a URL it has never
# seen. Headers only *ask* an intermediary to revalidate; a new URL does not
# have to be asked. The commit is in the tag as well as the version because two
# builds can share a version number while the code differs.
def _asset_tag() -> str:
    commit = updater.local_commit_short()
    return f"{config.VERSION}-{commit}" if commit else config.VERSION


ASSET_TAG = _asset_tag()
NO_CACHE = {"Cache-Control": "no-cache"}


def _page(filename: str) -> HTMLResponse:
    """Serve an HTML shell with the asset tag stitched into its URLs.

    Relative imports do the rest: `/a/<tag>/js/app.js` importing './api.js'
    resolves to `/a/<tag>/js/api.js` on its own, so one substitution in the
    shell versions the whole module graph with no build step.
    """
    page = STATIC_DIR / filename
    if not page.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{filename} is missing.")
    html = page.read_text(encoding="utf-8").replace("__ASSETS__", ASSET_TAG)
    return HTMLResponse(html, headers=NO_CACHE)


@app.get("/", include_in_schema=False)
async def index() -> HTMLResponse:
    return _page("index.html")


@app.get("/s/{token}", include_in_schema=False)
async def share_page(token: str) -> HTMLResponse:
    """The guest-facing share page.

    A real path rather than a #hash route, so the link you paste into a group
    chat looks like a link. The token is not read here - share.js pulls it from
    the URL and calls /api/share/{token}, which is where it is validated.
    """
    return _page("share.html")


@app.get("/a/{tag}/{asset_path:path}", include_in_schema=False)
async def versioned_asset(tag: str, asset_path: str) -> FileResponse:
    """Serve a static file under any tag. The tag is only a cache key - it is
    not checked, so an old page requesting an old tag still works rather than
    breaking until the person reloads."""
    root = STATIC_DIR.resolve()
    target = (root / asset_path).resolve()
    # Containment check before touching the filesystem: '..' in the path must
    # not reach outside static/.
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")
    return FileResponse(target, headers=NO_CACHE)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)

    # Nothing this app serves should ever sit in a CDN cache. Put it behind a
    # Cloudflare tunnel and the edge will happily cache .js and .css by
    # extension, then keep serving the previous version after a self-update -
    # invisible to a hard refresh or a private window, because the stale copy
    # is not in the browser at all. These two headers are the standard and the
    # Cloudflare-specific way to say "edge: do not store", and they are set
    # separately from Cache-Control so the browser still gets cheap ETag
    # revalidation rather than no caching at all.
    response.headers.setdefault("CDN-Cache-Control", "no-store")
    response.headers.setdefault("Cloudflare-CDN-Cache-Control", "no-store")

    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    # Everything is served from this origin; no external scripts, fonts or images.
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; "
        "frame-ancestors 'none'",
    )
    return response


@app.exception_handler(500)
async def internal_error(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse({"detail": "Something went wrong on the server."}, status_code=500)


class AppStatic(StaticFiles):
    """StaticFiles that makes browsers revalidate the frontend.

    Starlette sends ETag and Last-Modified but no Cache-Control, which leaves
    browsers to guess how long a file stays fresh - typically a tenth of its
    age - and reuse it without asking. In an app that updates itself that is a
    trap: the new code lands on disk, the browser keeps running the old app.js,
    and the update looks like it did nothing.

    `no-cache` does not mean "do not cache", it means "ask before reusing". The
    ETag is already there, so an unchanged file costs a 304 with no body - on a
    LAN that is nearly free, and the page is never stale after an update.

    Applied to everything rather than a list of extensions: static/ holds only
    frontend code that has to move in step with the app, and matching on paths
    is needlessly brittle (a request for "/" arrives here as ".").
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


if STATIC_DIR.is_dir():
    # Mounted last so /api/* always wins.
    app.mount("/", AppStatic(directory=str(STATIC_DIR), html=True), name="static")


def main() -> None:
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.Formatter.converter = time.localtime
    uvicorn.run(
        "app.main:app",
        host=config.HOST,
        port=config.PORT,
        log_level="info",
        proxy_headers=True,
        forwarded_allow_ips=config.TRUSTED_PROXIES,
    )


if __name__ == "__main__":
    main()
