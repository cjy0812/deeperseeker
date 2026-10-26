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

    @router.get("/v1/audit/raw")
    async def audit_raw_list(
        request: Request,
        since: float = None,
        limit: int = 50,
        before_seq: float = None,
    ):
        """List exchanges that carry a captured raw body, newest first.

        Metadata only: each row says which sides exist and when, never the
        text. Fetching a body is a separate, per-request call so a list page
        never decompresses megabytes of prompt to render a row.

        Gated like /v1/audit/perf (API key OR admin session): the metadata is
        operational, the bodies behind it are guarded separately by the same
        gate plus the explicit per-body fetch."""
        denied = await admin_or_key(request)
        if denied:
            return denied
        try:
            events = await run_db(audit.raw_events, since, limit, before_seq)
        except Exception:
            logger.exception("audit raw list failed")
            return JSONResponse(
                {"error": "audit store unavailable"}, status_code=503
            )
        return JSONResponse({"events": events, "enabled": audit.enabled()})

    @router.get("/v1/audit/raw/{request_id}")
    async def audit_raw_body(
        request: Request, request_id: str, direction: str = "req", since: float = None
    ):
        """The captured prompt (`direction=req`) or upstream output (`resp`).

        Gated like the list endpoint (API key OR admin session), per the
        operator's choice: raw capture is an operator-facing diagnostic, and
        the same credential that can scrape the index may read the body. Be
        aware this means a leaked API key exposes full prompts.

        404 means "no such capture" (no index, a pruned file, a corrupt
        frame); 503 means the store itself is unreadable."""
        denied = await admin_or_key(request)
        if denied:
            return denied
        if direction not in ("req", "resp"):
            return JSONResponse(
                {"error": "direction must be req or resp"}, status_code=400
            )
        try:
            text = await run_db(audit.raw_body_for, request_id, direction, since)
        except Exception:
            logger.exception("audit raw body read failed")
            return JSONResponse(
                {"error": "audit store unavailable"}, status_code=503
            )
        if text is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"text": text, "direction": direction})

    return router