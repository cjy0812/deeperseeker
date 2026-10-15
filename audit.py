"""Audit & perf event store.

Records what happened to every request and every upstream interaction in a
local SQLite table, so questions that are unanswerable from the token status
column alone become queryable:

    - Which upstream signal rate-limited token #3 - an HTTP 429 on the
      completion POST, or a `rate_limit_reached` event inside a 200 SSE body?
    - How many times was each token limited today, and how long did its
      cooldown actually last?
    - Which requests triggered a rollover / a retry / a failover?

Design constraints (deliberate, see CLAUDE.md):

* Standalone. This module imports NOTHING from the rest of the project.
  functions.py imports it, so any import back into functions would create a
  cycle. It resolves the same DB path as functions.py on its own - keep the
  two resolutions in sync if either changes.
* Best-effort. Auditing must never break a request. record() swallows every
  exception and degrades to a single stderr line. A missing or read-only
  database costs you audit rows, never a chat response.
* Off the event loop. arecord() is the async entry point and runs the
  blocking insert via asyncio.to_thread, mirroring the _db() pattern in
  app.py. Sync call sites (functions.py helpers) use record() directly.
"""

import contextlib
import contextvars
import json
import os
import sqlite3
import sys
import time

# Resolved exactly like functions._db so both modules address one database.
# DB_PATH is honored; the fallback is the package directory.
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DB_PATH = os.getenv("DB_PATH") or os.path.join(_BASE_DIR, "deeperseeker.db")

# Per-request ambient fields (request_id, endpoint, scope, model, ...).
#
# The request id is minted in middleware.RequestIDMiddleware and lives on
# scope["state"]; handle_chat / stream_response never receive the Request, so
# they cannot read it from there. Binding it to a ContextVar instead lets the
# middleware set it once and every record()/arecord() call in that task (and in
# the asyncio.to_thread workers it spawns - to_thread copies the context) pick
# it up with no plumbing. ContextVar.set() inside an ASGI middleware call is
# task-local, so concurrent requests never see each other's ids.
_ctx = contextvars.ContextVar("deeperseeker_audit_ctx", default=None)


def bind_request(**fields):
    """Attach ambient request fields for every audit event in this task.

    Returns the ContextVar token so a caller that must restore the previous
    value can, though middleware never needs to (each request is its own task).
    """
    ctx = dict(_ctx.get() or {})
    ctx.update({k: v for k, v in fields.items() if v is not None})
    return _ctx.set(ctx)


def current_context():
    """The ambient request fields bound by bind_request(), or {}."""
    return dict(_ctx.get() or {})


def db_path():
    """Path to the shared SQLite database (honors DB_PATH, like functions.py)."""
    return _DB_PATH


def enabled():
    """Auditing is on by default; DEEPSEEKER_AUDIT_ENABLED=0 disables it."""
    return (os.getenv("DEEPSEEKER_AUDIT_ENABLED", "1") or "").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def retention_days():
    try:
        return max(0, int(os.getenv("DEEPSEEKER_AUDIT_RETENTION_DAYS", "7")))
    except ValueError:
        return 7


def max_rows():
    try:
        return max(0, int(os.getenv("DEEPSEEKER_AUDIT_MAX_ROWS", "50000")))
    except ValueError:
        return 50000


# ==============================================================================
# Event kinds (bare strings so a new kind needs no migration)
#
#   request       one client request, terminal outcome + latency
#   chat          one chat orchestration: session reuse/create, attempts
#   token_pick    a token was selected for a send
#   rate_limit    a token was marked RATE_LIMITED (the one that matters most)
#   rate_recover  a cooling token was auto-recovered back to ACTIVE
#   failover      a POST moved to the fallback upstream base
#   rollover      accumulated context triggered a summarize-and-rollover
#   retry         a generic upstream failure triggered a bounded retry
# ==============================================================================

# Columns of audit_events, in insert order. record() accepts any subset via
# kwargs; unknown keys are ignored (they never reach the INSERT).
_COLUMNS = (
    "ts",
    "kind",
    "request_id",
    "scope",
    "model",
    "endpoint",
    "token_id",
    "session_id",
    "signature",
    "upstream_status",
    "reason",
    "latency_ms",
    "stream",
    "attempts",
    "detail",
)

_MAX_DETAIL = 2000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    request_id TEXT,
    scope TEXT,
    model TEXT,
    endpoint TEXT,
    token_id INTEGER,
    session_id TEXT,
    signature TEXT,
    upstream_status INTEGER,
    reason TEXT,
    latency_ms REAL,
    stream INTEGER,
    attempts INTEGER,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_events(ts);
CREATE INDEX IF NOT EXISTS idx_audit_kind_ts ON audit_events(kind, ts);
CREATE INDEX IF NOT EXISTS idx_audit_token_ts ON audit_events(token_id, ts);
"""


def _warn(msg):
    """Last-resort visibility for audit failures: one stderr line, no logging
    setup (this module must not depend on the app's logging configuration)."""
    with contextlib.suppress(Exception):
        sys.stderr.write(f"deeperseeker.audit: {msg}\n")


def _coerce_detail(value):
    if value is None:
        return None
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            value = str(value)
    return value[:_MAX_DETAIL]


def init_audit_db(conn=None):
    """Create the audit table + indexes. Safe to call repeatedly.

    Accepts an optional open connection so callers holding one (init_db in
    functions.py) can reuse it; otherwise opens its own.
    """
    own = conn is None
    if own:
        conn = sqlite3.connect(_DB_PATH, timeout=30)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        if own:
            conn.close()


def record(kind, **fields):
    """Append one audit event. Never raises.

    Returns the rowid on success, None when auditing is disabled or the write
    failed - callers are free to ignore the return value entirely.
    """
    if not enabled():
        return None
    # Ambient fields bound by bind_request() act as defaults; an explicit
    # non-None kwarg from the call site always wins. This is what lets the
    # middleware stamp request_id/endpoint/scope/model once and every event
    # emitted deeper in the stack inherit them without threading a Request
    # through handle_chat / stream_response.
    ambient = _ctx.get() or {}
    row = {
        col: fields.get(col) if fields.get(col) is not None else ambient.get(col)
        for col in _COLUMNS
    }
    row["ts"] = time.time()
    row["kind"] = str(kind)
    row["detail"] = _coerce_detail(fields.get("detail"))
    if row["stream"] is not None:
        row["stream"] = 1 if row["stream"] else 0
    for int_col in ("token_id", "upstream_status", "attempts"):
        if row[int_col] is not None:
            with contextlib.suppress(TypeError, ValueError):
                row[int_col] = int(row[int_col])
    conn = None
    try:
        conn = sqlite3.connect(_DB_PATH, timeout=5)
        with contextlib.suppress(Exception):
            conn.execute("PRAGMA journal_mode=WAL")
        cols = ", ".join(_COLUMNS)
        placeholders = ", ".join("?" for _ in _COLUMNS)
        cur = conn.execute(
            f"INSERT INTO audit_events ({cols}) VALUES ({placeholders})",
            tuple(row[col] for col in _COLUMNS),
        )
        conn.commit()
        return cur.lastrowid
    except Exception as e:  # noqa: BLE001 - auditing must never break a request
        _warn(f"record({kind}) failed: {e}")
        return None
    finally:
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()


async def arecord(kind, **fields):
    """Async entry point: run record() off the event loop.

    Mirrors app._db(): the insert is synchronous by design (shared with CLI
    paths and tests) but must not stall the loop under concurrent streams.
    """
    if not enabled():
        return None
    import asyncio

    return await asyncio.to_thread(lambda: record(kind, **fields))


# ==============================================================================
# Reading
# ==============================================================================


def _rows_to_dicts(rows):
    return [dict(r) for r in rows]


def query_events(kind=None, token_id=None, since=None, limit=100):
    """Most-recent-first events. since is an epoch-seconds lower bound;
    kind and token_id are exact filters."""
    limit = max(1, min(int(limit or 100), 1000))
    where, args = [], []
    if kind:
        where.append("kind = ?")
        args.append(kind)
    if token_id is not None:
        where.append("token_id = ?")
        args.append(int(token_id))
    if since is not None:
        where.append("ts >= ?")
        args.append(float(since))
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    conn = sqlite3.connect(_DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"SELECT * FROM audit_events{clause} ORDER BY id DESC LIMIT ?",
            (*args, limit),
        ).fetchall()
    finally:
        conn.close()
    return _rows_to_dicts(rows)


def token_rate_limit_summary(since=None):
    """The rate-limit audit view: per-token limit counts, the most recent
    trigger and the reason breakdown.

    One row per token that has been limited inside the window."""
    if since is None:
        since = time.time() - 24 * 3600
    conn = sqlite3.connect(_DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        limits = conn.execute(
            """SELECT token_id,
                      COUNT(*) AS limit_count,
                      MAX(ts) AS last_limited_at,
                      SUM(CASE WHEN reason = ? THEN 1 ELSE 0 END) AS http_429,
                      SUM(CASE WHEN reason = ? THEN 1 ELSE 0 END) AS sse_rate_limit,
                      SUM(CASE WHEN reason LIKE 'http_4%'
                                AND reason != 'http_429'
                               THEN 1 ELSE 0 END) AS http_other
                 FROM audit_events
                WHERE kind = 'rate_limit' AND ts >= ? AND token_id IS NOT NULL
             GROUP BY token_id""",
            ("http_429", "sse_rate_limit", since),
        ).fetchall()
        recovers = {
            r["token_id"]: r["n"]
            for r in conn.execute(
                """SELECT token_id, COUNT(*) AS n
                     FROM audit_events
                    WHERE kind = 'rate_recover' AND ts >= ? AND token_id IS NOT NULL
                 GROUP BY token_id""",
                (since,),
            ).fetchall()
        }
        last_reason = {
            r["token_id"]: r["reason"]
            for r in conn.execute(
                """SELECT token_id, reason FROM audit_events a
                    WHERE kind = 'rate_limit' AND token_id IS NOT NULL
                      AND id = (SELECT MAX(id) FROM audit_events
                                 WHERE kind = 'rate_limit'
                                   AND token_id = a.token_id)"""
            ).fetchall()
        }
    finally:
        conn.close()
    out = []
    for r in limits:
        out.append(
            {
                "token_id": r["token_id"],
                "limit_count": r["limit_count"],
                "last_limited_at": r["last_limited_at"],
                "last_reason": last_reason.get(r["token_id"]),
                "http_429": r["http_429"],
                "sse_rate_limit": r["sse_rate_limit"],
                "http_other": r["http_other"],
                "recover_count": recovers.get(r["token_id"], 0),
            }
        )
    out.sort(key=lambda d: d["limit_count"], reverse=True)
    return out


def _percentile(sorted_values, p):
    if not sorted_values:
        return None
    idx = min(len(sorted_values) - 1, int(round((len(sorted_values) - 1) * p)))
    return sorted_values[idx]


def perf_summary(window=3600):
    """Request-level roll-up over the trailing window seconds: volume, error
    classes, latency percentiles and the recovery counters."""
    window = max(60, min(int(window or 3600), 30 * 24 * 3600))
    since = time.time() - window
    conn = sqlite3.connect(_DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        totals = conn.execute(
            """SELECT COUNT(*) AS requests,
                      SUM(CASE WHEN upstream_status >= 400
                                OR upstream_status IS NULL THEN 1 ELSE 0 END) AS failures,
                      AVG(latency_ms) AS avg_latency_ms,
                      MAX(latency_ms) AS max_latency_ms,
                      SUM(CASE WHEN stream = 1 THEN 1 ELSE 0 END) AS streams
                 FROM audit_events
                WHERE kind = 'request' AND ts >= ?""",
            (since,),
        ).fetchone()
        latencies = [
            r["latency_ms"]
            for r in conn.execute(
                """SELECT latency_ms FROM audit_events
                    WHERE kind = 'request' AND ts >= ? AND latency_ms IS NOT NULL
                 ORDER BY latency_ms ASC""",
                (since,),
            ).fetchall()
        ]
        counters = {
            r["kind"]: r["n"]
            for r in conn.execute(
                """SELECT kind, COUNT(*) AS n FROM audit_events
                    WHERE ts >= ?
                      AND kind IN ('rate_limit', 'rate_recover', 'failover',
                                   'rollover', 'retry')
                 GROUP BY kind""",
                (since,),
            ).fetchall()
        }
        reasons = {
            r["reason"]: r["n"]
            for r in conn.execute(
                """SELECT COALESCE(reason, 'unknown') AS reason, COUNT(*) AS n
                     FROM audit_events
                    WHERE kind = 'rate_limit' AND ts >= ?
                 GROUP BY reason""",
                (since,),
            ).fetchall()
        }
    finally:
        conn.close()

    requests = totals["requests"] or 0
    failures = totals["failures"] or 0
    return {
        "window_seconds": window,
        "requests": requests,
        "failures": failures,
        "error_rate": (failures / requests) if requests else 0.0,
        "streams": totals["streams"] or 0,
        "latency_ms": {
            "avg": totals["avg_latency_ms"],
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            # p99 is computed off the same sorted list the other percentiles
            # use — the audit page surfaces it, and a separate query for one
            # more index would double the scan for no gain.
            "p99": _percentile(latencies, 0.99),
            "max": totals["max_latency_ms"],
        },
        "counters": counters,
        "rate_limit_reasons": reasons,
    }


def prune_audit():
    """Drop events past the retention window and over the row cap.

    Called from the existing session-prune trigger (PRUNE_EVERY saves) - no
    new timer. Returns the number of deleted rows. Never raises."""
    if not enabled():
        return 0
    days, cap = retention_days(), max_rows()
    deleted = 0
    conn = None
    try:
        conn = sqlite3.connect(_DB_PATH, timeout=10)
        if days > 0:
            deleted += conn.execute(
                "DELETE FROM audit_events WHERE ts < ?",
                (time.time() - days * 86400,),
            ).rowcount
        if cap > 0:
            total = conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
            if total > cap:
                deleted += conn.execute(
                    "DELETE FROM audit_events WHERE id IN "
                    "(SELECT id FROM audit_events ORDER BY id ASC LIMIT ?)",
                    (total - cap,),
                ).rowcount
        conn.commit()
    except Exception as e:  # noqa: BLE001 - pruning is opportunistic
        _warn(f"prune failed: {e}")
        return deleted
    finally:
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()
    return deleted
