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
from fastapi.responses import FileResponse, JSONResponse
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

    log.info(
        "Bill Splitter v%s on http://%s:%s (%s)",
        config.VERSION, config.HOST, config.PORT,
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


@app.get("/s/{token}", include_in_schema=False)
async def share_page(token: str) -> FileResponse:
    """The guest-facing share page.

    A real path rather than a #hash route, so the link you paste into a group
    chat looks like a link. The token is not read here - share.js pulls it from
    the URL and calls /api/share/{token}, which is where it is validated.
    """
    page = STATIC_DIR / "share.html"
    if not page.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Share page missing.")
    return FileResponse(page, media_type="text/html")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
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


if STATIC_DIR.is_dir():
    # Mounted last so /api/* always wins.
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


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
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
