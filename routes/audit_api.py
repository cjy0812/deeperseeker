"""Read-only audit API routes (perf roll-up + token rate-limit breakdown).

Extracted from app.py in the post-merge module split. These are the only two
routes in the app that the test suite exercises purely over HTTP
(tests/test_admin_audit_gate.py) with no module-attribute patching, which is
what makes them safe to move: the patching contract elsewhere in app.py
depends on those functions living in app's own globals.

Dependencies are injected instead of imported -- app.py imports this module,
so ``from app import ...`` would be circular.
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

import audit


def build_router(*, admin_or_key, get_current_admin, run_db, logger):
    """Return the audit APIRouter bound to the host app's helpers.

    admin_or_key      app._admin_or_key  -- API key OR live admin session
    get_current_admin session gate       -- raises HTTPException
    run_db            app._db            -- run a sync store helper off-loop
    logger            uvicorn.error      -- for .exception()
    """
    router = APIRouter()

    @router.get("/v1/audit/perf")
    async def audit_perf(request: Request, window: int = 3600):
        """Perf status: request volume, error rate, latency percentiles and the
        recovery counters over the trailing `window` seconds.

        Gated by EITHER the API key (so an operator can scrape it from the same
        client that talks to the proxy) OR a live admin session (so the /audit
        page can render the same roll-up without embedding the API key in the
        browser). Read-only, best-effort: an audit-store failure is a 503, never
        a 500, since the data is diagnostic."""
        if not admin_or_key(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        try:
            summary = await run_db(audit.perf_summary, window)
        except Exception:
            logger.exception("audit perf summary failed")
            return JSONResponse({"error": "audit store unavailable"}, status_code=503)
        summary["enabled"] = audit.enabled()
        return JSONResponse(summary)

    @router.get("/v1/audit/rate-limits")
    async def audit_rate_limits(request: Request, since: float = None):
        """The upstream rate-limit audit view: one row per token limited inside
        the window, with the reason breakdown (http_429 / sse_rate_limit /
        http_other) and how many times it auto-recovered.

        Admin-gated: token ids and per-token limit counts are operational detail.
        The dashboard / admin session is the intended caller."""
        try:
            # get_current_admin is synchronous (it returns "admin"): awaiting its
            # return value raised TypeError, which the HTTPException handler below
            # could not catch, so the admin-session path was a 500 and only the
            # API-key path (which this endpoint does not accept) ever worked.
            get_current_admin(request)
        except HTTPException:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        try:
            rows = await run_db(audit.token_rate_limit_summary, since)
        except Exception:
            logger.exception("audit rate-limit summary failed")
            return JSONResponse({"error": "audit store unavailable"}, status_code=503)
        return JSONResponse({"tokens": rows, "enabled": audit.enabled()})

    return router