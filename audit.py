"""Audit & perf event store.

Records what happened to every request and every upstream interaction in
per-day JSONL files under ``<db dir>/audit/``, so questions that are
unanswerable from the token status column alone become queryable:

    - Which upstream signal rate-limited token #3 - an HTTP 429 on the
      completion POST, or a `rate_limit_reached` event inside a 200 SSE body?
    - How many times was each token limited today, and how long did its
      cooldown actually last?
    - Which requests triggered a failover?

Storage: one ``audit-YYYY-MM-DD.jsonl`` per UTC day, one JSON object per line,
oldest first. The filename date is UTC because `ts` is epoch seconds - a local
date would file cross-midnight rows in the wrong bucket. An append is a single
locked ``write()`` with no fsync (record() already runs off the event loop,
and the lock is what keeps a >PIPE_BUF detail field from interleaving).

Design constraints (deliberate, see CLAUDE.md):

* Standalone. This module imports NOTHING from the rest of the project.
  functions.py imports it, so any import back into functions would create a
  cycle. It resolves the data directory exactly like functions.py does -
  keep the two resolutions in sync if either changes.
* Best-effort WRITES. Auditing must never break a request. record() swallows
  every exception and degrades to a single stderr line. A missing or
  read-only store costs you audit rows, never a chat response.
* Deliberately raising READS. query_events() / token_rate_limit_summary() /
  perf_summary() do NOT swallow filesystem failures: /v1/audit/* turns them
  into a 503, which an operator must be able to tell apart from "no events in
  this window". A single unparseable LINE is a different failure mode and is
  skipped instead - an append interrupted mid-write must not blank the whole
  console.
* Off the event loop. arecord() is the async entry point and runs the blocking
  append via asyncio.to_thread, mirroring the _db() pattern in app.py. Sync
  call sites (functions.py helpers) use record() directly.
"""

import contextlib
import contextvars
import json
import os
import re
import sqlite3
import sys
import threading
import time

# Raw bodies live in their own module so this one stays standard-library-only
# apart from this single import: audit_raw itself imports nothing from the
# project, so there is no cycle (functions.py imports both).
import audit_raw

# Resolved exactly like functions.data_dir() so both modules address one
# location. DB_PATH is honored; the fallback is the package's data/
# subdirectory. functions.py recomputes these same paths on its own (it
# imports this module, so importing back would cycle): change one and you
# must change the other.
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_BASE_DIR, "data")
_DB_PATH = os.getenv("DB_PATH") or os.path.join(_DATA_DIR, "deeperseeker.db")
# Audit events live beside the database, so they ride the same volume mount
# (docker-compose.yml mounts the DB's directory at /app/data).
_AUDIT_DIR = os.path.join(os.path.dirname(os.path.abspath(_DB_PATH)), "audit")

_FILE_RE = re.compile(r"^audit-(\d{4}-\d{2}-\d{2})\.jsonl$")

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
# Emitted in production today:
#
#   request       one client request, terminal outcome + latency
#   rate_limit    a token was marked RATE_LIMITED (the one that matters most)
#   rate_recover  a cooling token was auto-recovered back to ACTIVE
#   failover      a POST moved to the fallback upstream base
#
# Declared but never emitted - the dashboard's counters/rollover panels reserve
# a slot for them, and _COUNTER_KINDS keeps them counted if they ever land:
#
#   rollover      accumulated context triggered a summarize-and-rollover
#   retry         a generic upstream failure triggered a bounded retry
# ==============================================================================

# Fields of one audit row, in write order. record() accepts any subset via
# kwargs; unknown keys are ignored (they never reach the file).
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
    # Index into the compressed raw-body store (see audit_raw): the full
    # prompt/response text for this exchange, redacted, lives there. A dict
    # when written, a JSON string when read back - _coerce_raw handles both.
    "raw",
)

# Kinds counted in perf_summary()["counters"]. A whitelist, not a filter on
# "anything but request": without it the roll-up would grow a bucket for every
# new event kind.
_COUNTER_KINDS = ("rate_limit", "rate_recover", "failover", "rollover", "retry")

_MAX_DETAIL = 2000

# Insertion order used to come from SQLite's AUTOINCREMENT id. JSONL has no
# such thing, so every row carries a seq: process-wide monotonic, seeded from
# the highest seq already on disk so it stays monotonic across restarts.
# prune_audit() leaves holes in it, which is fine - only the ordering and the
# maximum matter.
_seq_lock = threading.Lock()
_seq_state = {"next": None}

# Serializes appends. detail can reach 2000 chars, so a whole line can exceed
# PIPE_BUF and O_APPEND alone would not keep concurrent writers from
# interleaving. record() is synchronous by design, so the lock costs nothing.
_write_lock = threading.Lock()


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


def _coerce_raw(value):
    """The `raw` column: the raw-body index dict from audit_raw.record_raw().

    Same shape handling as detail (a dict is JSON-encoded before it reaches
    the line), but a separate name so the intent stays readable. The index is
    roughly 200 characters, so the cap is a guard, not a real limit."""
    return _coerce_detail(value)


# ==============================================================================
# Files
# ==============================================================================


def _date_from_ts(ts):
    """UTC calendar day of an epoch-seconds timestamp, as YYYY-MM-DD."""
    return time.strftime("%Y-%m-%d", time.gmtime(float(ts)))


def _file_for_ts(ts):
    return os.path.join(_AUDIT_DIR, f"audit-{_date_from_ts(ts)}.jsonl")


def _file_date(path):
    m = _FILE_RE.match(os.path.basename(path))
    return m.group(1) if m else ""


def _audit_files(since=None):
    """Existing audit files, oldest first.

    A missing directory reads as "no events" (fresh install), not as a
    failure. Anything else - the path is a file, permissions, I/O - raises, so
    the /v1/audit/* endpoints can answer 503 instead of a misleading empty
    window. `since` skips whole files whose UTC day cannot contain a matching
    row, with a one-day margin for clock skew.
    """
    try:
        names = os.listdir(_AUDIT_DIR)
    except FileNotFoundError:
        return []
    dated = []
    for name in names:
        m = _FILE_RE.match(name)
        if m:
            dated.append((m.group(1), os.path.join(_AUDIT_DIR, name)))
    dated.sort()
    if since is not None:
        cutoff = _date_from_ts(float(since) - 86400)
        dated = [(d, p) for d, p in dated if d >= cutoff]
    return [p for _, p in dated]


def _decode(raw):
    """One JSONL line -> dict, or None if it is not a JSON object.

    A None here means a torn line (an append interrupted mid-write) or
    foreign content; callers skip it rather than failing the whole read.
    """
    try:
        row = json.loads(raw)
    except ValueError:
        return None
    return row if isinstance(row, dict) else None


def _iter_events(files):
    """Yield every decodable event in file order (oldest first).

    Opening/reading a file propagates OSError - that is the 503 contract.
    A single unparseable line is skipped instead.
    """
    for path in files:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = _decode(line)
                if row is not None:
                    yield row


def _read_lines(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return [ln for ln in fh.read().splitlines() if ln.strip()]


def _rewrite(path, lines):
    """Replace a file atomically, or delete it when nothing is left.

    No fsync for the same reason the append path has none: a lost tail after a
    crash is an audit row, not a corrupt database.
    """
    if not lines:
        os.remove(path)
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("".join(ln + "\n" for ln in lines))
    os.replace(tmp, path)


def _order_key(row):
    seq = row.get("seq")
    if not isinstance(seq, (int, float)) or isinstance(seq, bool):
        return 0.0
    return float(seq)


# ==============================================================================
# Writing
# ==============================================================================


def _max_seq_on_disk():
    """Highest seq already persisted. Best-effort: any failure means 0.

    Only the tail of each file is read - within a file seq is written in
    increasing order, so the last decodable line carries its maximum.
    """
    best = 0
    try:
        for path in _audit_files():
            try:
                with open(path, "rb") as fh:
                    fh.seek(0, os.SEEK_END)
                    size = fh.tell()
                    fh.seek(max(0, size - 65536))
                    tail = fh.read().decode("utf-8", "replace")
            except OSError:
                continue
            for line in reversed(tail.splitlines()):
                row = _decode(line.strip())
                if row is None:
                    continue
                seq = row.get("seq")
                if isinstance(seq, (int, float)) and not isinstance(seq, bool):
                    best = max(best, int(seq))
                break
    except Exception:  # noqa: BLE001 - seeding must never break a write
        return 0
    return best


def _next_seq():
    with _seq_lock:
        nxt = _seq_state["next"]
        if nxt is None:
            nxt = _max_seq_on_disk() + 1
        _seq_state["next"] = nxt + 1
        return nxt


def _append(row):
    """Append one row to its day's file. Raises on any I/O failure."""
    line = json.dumps(row, ensure_ascii=False, default=str) + "\n"
    path = _file_for_ts(row["ts"])
    with _write_lock:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)


def record(kind, **fields):
    """Append one audit event. Never raises.

    Returns the row's seq on success, None when auditing is disabled or the
    write failed - callers are free to ignore the return value entirely.
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
    row["raw"] = _coerce_raw(row.get("raw"))
    if row["stream"] is not None:
        row["stream"] = 1 if row["stream"] else 0
    for int_col in ("token_id", "upstream_status", "attempts"):
        if row[int_col] is not None:
            with contextlib.suppress(TypeError, ValueError):
                row[int_col] = int(row[int_col])
    try:
        row["seq"] = _next_seq()
        _append(row)
        return row["seq"]
    except Exception as e:  # noqa: BLE001 - auditing must never break a request
        _warn(f"record({kind}) failed: {e}")
        return None


async def arecord(kind, **fields):
    """Async entry point: run record() off the event loop.

    Mirrors app._db(): the append is synchronous by design (shared with CLI
    paths and tests) but must not stall the loop under concurrent streams.
    """
    if not enabled():
        return None
    import asyncio

    return await asyncio.to_thread(lambda: record(kind, **fields))


# ==============================================================================
# Setup + one-shot migration off the old audit_events table
# ==============================================================================


def _legacy_rows(conn):
    """Rows of the retired audit_events table, oldest first, or None."""
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='audit_events'"
    )
    if cur.fetchone() is None:
        return None
    cur = conn.execute("SELECT * FROM audit_events ORDER BY id ASC")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]


def _migrate_legacy_table(conn=None):
    """Move rows out of the old SQLite table into per-day JSONL files.

    Runs once: after the files are safely written the table is dropped, so a
    second call is a no-op. The files are written BEFORE the drop - a crash in
    between leaves the table in place and re-runs, which is why the row count
    only ever goes down on success.
    """
    own = conn is None
    if own:
        if not os.path.exists(_DB_PATH):
            return
        conn = sqlite3.connect(_DB_PATH, timeout=30)
    try:
        rows = _legacy_rows(conn)
        if rows is None:
            return
        buckets = {}
        for old in rows:
            row = {col: old.get(col) for col in _COLUMNS}
            row["kind"] = str(old.get("kind") or "")
            seq = old.get("id")
            row["seq"] = seq if isinstance(seq, int) else len(buckets) + 1
            ts = old.get("ts")
            buckets.setdefault(_file_for_ts(ts if ts is not None else 0), []).append(row)
        os.makedirs(_AUDIT_DIR, exist_ok=True)
        for path, events in buckets.items():
            with open(path, "a", encoding="utf-8") as fh:
                for row in events:
                    fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        conn.execute("DROP TABLE IF EXISTS audit_events")
        conn.commit()
        # The migrated rows carry the old ids as their seq; re-seed lazily so
        # new events continue above them.
        _seq_state["next"] = None
        _warn(f"migrated {len(rows)} audit rows out of SQLite into {_AUDIT_DIR}")
    finally:
        if own:
            conn.close()


def init_audit_db(conn=None):
    """Ensure the audit directory exists and retire the legacy table.

    No schema is created any more - events live in JSONL. Kept as the setup
    hook (and the conn parameter) for functions.init_db. Never raises: the
    caller treats setup as non-fatal and a missing audit directory only costs
    audit rows.
    """
    try:
        os.makedirs(_AUDIT_DIR, exist_ok=True)
    except Exception as e:  # noqa: BLE001 - setup is best-effort
        _warn(f"could not create {_AUDIT_DIR}: {e}")
        return
    try:
        _migrate_legacy_table(conn)
    except Exception as e:  # noqa: BLE001 - setup is best-effort
        _warn(f"legacy audit migration failed: {e}")


# ==============================================================================
# Reading
# ==============================================================================


def query_events(kind=None, token_id=None, since=None, limit=100, before_seq=None):
    """Most-recent-first events. since is an epoch-seconds lower bound;
    kind and token_id are exact filters.

    before_seq is a cursor for paging: only rows with seq < before_seq are
    returned, so a caller passes the last seq it saw to get the next page.
    None (the default) means no cursor and preserves the old behaviour."""
    limit = max(1, min(int(limit or 100), 1000))
    tid = None if token_id is None else token_id
    cursor = None if before_seq is None else float(before_seq)
    matched = []
    for row in _iter_events(_audit_files(since)):
        if kind is not None and row.get("kind") != kind:
            continue
        if tid is not None and row.get("token_id") != tid:
            continue
        if cursor is not None and _order_key(row) >= cursor:
            continue
        if since is not None:
            ts = row.get("ts")
            if not isinstance(ts, (int, float)) or ts < float(since):
                continue
        matched.append(row)
    matched.sort(key=_order_key, reverse=True)
    return matched[:limit]


def _raw_index(row):
    """Decode an event's `raw` column into the index dict, or None.

    The column is a JSON string on disk (a dict at write time, encoded by
    _coerce_raw); events with no captured body have no index at all."""
    value = row.get("raw")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def raw_events(since=None, limit=50, before_seq=None):
    """Events that carry a captured raw body, most recent first.

    Metadata only - the index says which sides exist and where, but the text
    itself stays in the compressed store until a caller asks for one body by
    request_id. Same cursor contract as query_events: before_seq returns rows
    with seq < before_seq."""
    limit = max(1, min(int(limit or 50), 500))
    cursor = None if before_seq is None else float(before_seq)
    found = []
    for row in _iter_events(_audit_files(since)):
        index = _raw_index(row)
        if not index:
            continue
        if cursor is not None and _order_key(row) >= cursor:
            continue
        ts = row.get("ts")
        if since is not None and (
            not isinstance(ts, (int, float)) or ts < float(since)
        ):
            continue
        found.append(
            {
                "seq": row.get("seq"),
                "ts": ts,
                "request_id": row.get("request_id"),
                "model": row.get("model"),
                "token_id": row.get("token_id"),
                "endpoint": row.get("endpoint"),
                "sides": sorted(index),
            }
        )
    found.sort(key=lambda item: float(item.get("seq") or 0), reverse=True)
    return found[:limit]


def raw_body_for(request_id, direction, since=None):
    """The stored text for one side of one exchange, or None.

    The newest matching event wins, so a request_id that somehow got recorded
    twice serves the later capture. Directory I/O errors propagate, which is
    the same 503 contract the other readers keep."""
    newest = None
    for row in _iter_events(_audit_files(since)):
        if row.get("request_id") != request_id:
            continue
        index = _raw_index(row)
        if not index:
            continue
        if newest is None or _order_key(row) > _order_key(newest):
            newest = row
    if newest is None:
        return None
    return audit_raw.read_body(_raw_index(newest), direction)


def token_rate_limit_summary(since=None):
    """The rate-limit audit view: per-token limit counts, the most recent
    trigger and the reason breakdown.

    One row per token that has been limited inside the window."""
    if since is None:
        since = time.time() - 24 * 3600
    since = float(since)

    limits = {}
    recovers = {}
    for row in _iter_events(_audit_files(since)):
        ts = row.get("ts")
        if not isinstance(ts, (int, float)) or ts < since:
            continue
        tid = row.get("token_id")
        if tid is None:
            continue
        kind = row.get("kind")
        if kind == "rate_limit":
            entry = limits.setdefault(
                tid,
                {
                    "limit_count": 0,
                    "last_limited_at": 0.0,
                    "http_429": 0,
                    "sse_rate_limit": 0,
                    "http_other": 0,
                },
            )
            entry["limit_count"] += 1
            entry["last_limited_at"] = max(entry["last_limited_at"], ts)
            reason = row.get("reason")
            if reason == "http_429":
                entry["http_429"] += 1
            elif reason == "sse_rate_limit":
                entry["sse_rate_limit"] += 1
            elif isinstance(reason, str) and reason.lower().startswith("http_4"):
                entry["http_other"] += 1
        elif kind == "rate_recover":
            recovers[tid] = recovers.get(tid, 0) + 1

    # last_reason is deliberately NOT windowed (it never was): the newest
    # trigger for a token is the same fact inside and outside the window.
    last_reason = {}
    last_key = {}
    for row in _iter_events(_audit_files()):
        if row.get("kind") != "rate_limit":
            continue
        tid = row.get("token_id")
        if tid is None:
            continue
        key = _order_key(row)
        if tid not in last_key or key > last_key[tid]:
            last_key[tid] = key
            last_reason[tid] = row.get("reason")

    out = [
        {
            "token_id": tid,
            "limit_count": entry["limit_count"],
            "last_limited_at": entry["last_limited_at"],
            "last_reason": last_reason.get(tid),
            "http_429": entry["http_429"],
            "sse_rate_limit": entry["sse_rate_limit"],
            "http_other": entry["http_other"],
            "recover_count": recovers.get(tid, 0),
        }
        for tid, entry in limits.items()
    ]
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

    requests = 0
    failures = 0
    streams = 0
    latencies = []
    counters = {}
    reasons = {}
    for row in _iter_events(_audit_files(since)):
        ts = row.get("ts")
        if not isinstance(ts, (int, float)) or ts < since:
            continue
        kind = row.get("kind")
        if kind == "request":
            requests += 1
            status = row.get("upstream_status")
            if status is None or (
                isinstance(status, (int, float))
                and not isinstance(status, bool)
                and status >= 400
            ):
                failures += 1
            if row.get("stream") == 1:
                streams += 1
            latency = row.get("latency_ms")
            if isinstance(latency, (int, float)) and not isinstance(latency, bool):
                latencies.append(float(latency))
        if kind in _COUNTER_KINDS:
            counters[kind] = counters.get(kind, 0) + 1
        if kind == "rate_limit":
            reason = row.get("reason")
            reason = "unknown" if reason is None else str(reason)
            reasons[reason] = reasons.get(reason, 0) + 1

    latencies.sort()
    return {
        "window_seconds": window,
        "requests": requests,
        "failures": failures,
        "error_rate": (failures / requests) if requests else 0.0,
        "streams": streams,
        "latency_ms": {
            "avg": (sum(latencies) / len(latencies)) if latencies else None,
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            # p99 is computed off the same sorted list the other percentiles
            # use - the audit page surfaces it, and a separate pass for one
            # more index would double the scan for no gain.
            "p99": _percentile(latencies, 0.99),
            "max": latencies[-1] if latencies else None,
        },
        "counters": counters,
        "rate_limit_reasons": reasons,
    }


# ==============================================================================
# Retention
# ==============================================================================


def _filter_old_lines(path, cutoff):
    """Drop rows older than cutoff from one file. Returns rows removed.

    A file can span the cutoff (its name is a UTC day, but rows land in the
    file of their own timestamp), so the boundary day is filtered row by row.
    Unparseable lines are kept - they cannot be dated, and dropping them on a
    transient torn append would destroy a real row.
    """
    lines = _read_lines(path)
    kept = []
    removed = 0
    for raw in lines:
        row = _decode(raw)
        if row is not None:
            ts = row.get("ts")
            if (
                isinstance(ts, (int, float))
                and not isinstance(ts, bool)
                and ts < cutoff
            ):
                removed += 1
                continue
        kept.append(raw)
    if removed:
        _rewrite(path, kept)
    return removed


def _ordered_entries(files, contents):
    """(order key, path, line index) for every non-empty line, oldest first."""
    entries = []
    last = 0.0
    for path in files:
        for idx, raw in enumerate(contents[path]):
            row = _decode(raw)
            seq = _order_key(row) if row is not None else 0.0
            if seq <= 0.0:
                # No usable seq (torn line): keep it in place rather than
                # letting it jump to either end of the ordering.
                last += 0.5
            else:
                last = seq
            entries.append((last, path, idx))
    return entries


def prune_audit():
    """Drop events past the retention window and over the row cap.

    Called from the existing session-prune trigger (PRUNE_EVERY saves) - no
    new timer. Returns the number of deleted rows. Never raises."""
    if not enabled():
        return 0
    days, cap = retention_days(), max_rows()
    deleted = 0
    try:
        if days > 0:
            cutoff = time.time() - days * 86400
            cutoff_date = _date_from_ts(cutoff)
            for path in _audit_files():
                # Whole files whose UTC day is entirely before the cutoff go in
                # one step - that is the common case, and the reason the store
                # is split by day.
                if _file_date(path) < cutoff_date:
                    try:
                        lines = _read_lines(path)
                        os.remove(path)
                        deleted += len(lines)
                    except OSError as e:
                        _warn(f"prune could not drop {path}: {e}")
                    continue
                deleted += _filter_old_lines(path, cutoff)
        if cap > 0:
            files = _audit_files()
            contents = {p: _read_lines(p) for p in files}
            entries = _ordered_entries(files, contents)
            to_drop = len(entries) - cap
            if to_drop > 0:
                victims = {}
                for _, path, idx in entries[:to_drop]:
                    victims.setdefault(path, set()).add(idx)
                for path, idxs in victims.items():
                    lines = contents[path]
                    kept = [ln for i, ln in enumerate(lines) if i not in idxs]
                    deleted += len(lines) - len(kept)
                    _rewrite(path, kept)
    except Exception as e:  # noqa: BLE001 - pruning is opportunistic
        _warn(f"prune failed: {e}")
        return deleted
    # Raw bodies are pruned on their own schedule (retention days + byte cap)
    # and their own failure domain: a broken raw directory must not roll back
    # or mask the event prune that just succeeded.
    try:
        audit_raw.prune_raw()
    except Exception as e:  # noqa: BLE001 - opportunistic, never raises out
        _warn(f"raw prune failed: {e}")
    return deleted
