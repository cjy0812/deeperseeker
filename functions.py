import asyncio
import base64
import contextlib
import contextvars
import json
import logging
import mimetypes
import os
import re
import shutil
import sqlite3
import sys
import time
from datetime import UTC, datetime

import aiohttp
import deepseek_tokenizer
import wasmtime

import audit
import audit_raw

try:
    from playwright.async_api import async_playwright
except ImportError:
    async_playwright = None

logger = logging.getLogger("deeperseeker.functions")

# B12 (Stage 1 audit): all relative runtime paths are anchored to the package
# directory — the old global os.chdir(BASE_DIR) at import mutated process-wide
# state (hostile to embedding, testing and PyPI packaging).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

wasm_path = os.path.join(BASE_DIR, "wasm", "deepseek_pow_solver.wasm")
_session = None
# DB_PATH (bare name, no DEEPSEEKER_ prefix) remains the single override.
# The default used to be the package directory itself, which littered a source
# checkout with runtime state; everything mutable now lives in data/.
_db = os.getenv("DB_PATH") or os.path.join(BASE_DIR, "data", "deeperseeker.db")


# Runtime artifact directory. audit.py deliberately imports nothing from this
# module (functions imports audit — importing back would cycle), so it
# recomputes the same path on its own: change one and you must change the other.
DATA_DIR = os.path.join(BASE_DIR, "data")


def data_dir():
    """Directory holding the SQLite database and the runtime files stored next
    to it (generated API key, DeepSeek cookie jar). Honors DB_PATH; defaults to
    the package's data/ subdirectory so a source checkout stays clean."""
    return os.path.dirname(os.path.abspath(_db))


def _migrate_legacy_db():
    """One-shot move of a pre-data/ checkout database into DATA_DIR.

    Runs at import, before anything connects: a source checkout that predates
    the data/ move keeps its token pool instead of quietly starting empty.
    An explicit DB_PATH disables it — that caller owns the location.
    """
    if os.getenv("DB_PATH"):
        return
    old = os.path.join(BASE_DIR, "deeperseeker.db")
    new = os.path.join(DATA_DIR, "deeperseeker.db")
    if os.path.exists(new) or not os.path.exists(old):
        return
    # WAL sidecars hold committed pages that are not in the main file yet;
    # moving the main file alone would silently drop the most recent writes.
    for suffix in ("", "-wal", "-shm"):
        src, dst = old + suffix, new + suffix
        if not os.path.exists(src):
            continue
        try:
            try:
                os.replace(src, dst)
            except OSError:
                # EXDEV: data/ may live on another device (bind mount, tmpfs).
                shutil.move(src, dst)
        except Exception as e:
            print(
                f"deeperseeker: could not move {src} to {dst}: {e} "
                "(continuing with a fresh database)",
                file=sys.stderr,
            )
            return


# The data/ directory must exist before the first sqlite3.connect(): every
# connection helper used to assume it was already there.
try:
    os.makedirs(data_dir(), exist_ok=True)
except OSError as e:
    print(f"deeperseeker: could not create {data_dir()}: {e}", file=sys.stderr)
_migrate_legacy_db()


def cookie_file_path():
    """Resolve where the DeepSeek cookie file lives.

    Order: DEEPSEEKER_COOKIE_PATH env > the target of a legacy Docker symlink
    > next to the real DB file (which honors DB_PATH, so it honors data/).
    Writing goes to the RESOLVED path so os.replace() can never destroy a
    symlink that bridges the file into the persistent data volume.

    The old cwd-guessing branch is gone: where the cookie landed used to depend
    on the working directory the process happened to be started from.
    """
    p = os.getenv("DEEPSEEKER_COOKIE_PATH")
    if p:
        return p
    link = os.path.join(BASE_DIR, "aws_cookies_deepseek.json")
    try:
        if os.path.islink(link):
            target = os.path.realpath(link)
            if target:
                return target
    except Exception:
        pass
    return os.path.join(data_dir(), "aws_cookies_deepseek.json")


try:
    _TZ_OFFSET = str(int(datetime.now().astimezone().utcoffset().total_seconds()))
except Exception:
    _TZ_OFFSET = "19800"


def get_db():
    conn = sqlite3.connect(_db, timeout=30)
    conn.row_factory = sqlite3.Row
    with contextlib.suppress(Exception):
        conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alias TEXT,
            token TEXT,
            status TEXT DEFAULT 'ACTIVE'
        );
        CREATE TABLE IF NOT EXISTS sessions (
            signature TEXT PRIMARY KEY,
            token_id INTEGER,
            deepseek_session_id TEXT,
            parent_message_id INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS session_map (
            old_session TEXT PRIMARY KEY,
            new_session TEXT,
            token_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS files (
            file_id TEXT PRIMARY KEY,
            token_id INTEGER,
            created_at REAL
        );
    """)
    # B3 (Stage 1 audit) migration: pool cooldown + usage tracking. Older
    # databases miss the columns; the ALTERs are idempotent behind the
    # pragma check.
    existing_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(tokens)").fetchall()
    }
    if "rate_limited_until" not in existing_cols:
        conn.execute("ALTER TABLE tokens ADD COLUMN rate_limited_until REAL")
    if "last_used" not in existing_cols:
        conn.execute("ALTER TABLE tokens ADD COLUMN last_used REAL")
    # P0-3 migration: how many 403s this token has seen with no successful
    # turn in between. Reset by mark_active() and by restore_token().
    if "consecutive_403" not in existing_cols:
        conn.execute(
            "ALTER TABLE tokens ADD COLUMN consecutive_403 INTEGER NOT NULL DEFAULT 0"
        )
    existing_session_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
    }
    if "last_used" not in existing_session_cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN last_used REAL")
    # Audit & perf store: no longer a table here. Events live in per-day
    # JSONL files under data/audit/; this ensures that directory exists and
    # performs the one-shot migration off the retired audit_events table.
    # The connection is still passed so the migration can read/drop it.
    try:
        audit.init_audit_db(conn)
    except Exception:
        logger.exception("Audit store init failed (non-fatal)")
    # P0-6 (Day 29): usage stats — one row per COMPLETED turn (client-visible
    # success), aggregated per day x per masked key at read time. Keeping raw
    # rows instead of pre-aggregated counters makes the numbers auditable and
    # the "streamed retries count once" rule structural: a failed attempt has
    # nothing to record.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            day TEXT NOT NULL,
            key_alias TEXT NOT NULL,
            surface TEXT NOT NULL,
            model TEXT,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0
        );
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_usage_day_key ON usage(day, key_alias)"
    )
    conn.commit()
    conn.close()
    try:
        prune_sessions()
    except Exception:
        logger.exception("Startup session pruning failed (non-fatal)")


_session_lock = asyncio.Lock()


async def get_session():
    global _session
    if _session is None or _session.closed:
        async with _session_lock:
            if _session is None or _session.closed:
                logger.info("Opening shared aiohttp ClientSession")
                _session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=None, connect=15, sock_read=600)
                )
    return _session


async def close_session():
    """Close the shared aiohttp ClientSession (Stage 1 minor list).

    Called from app lifespan shutdown: without it, every uvicorn reload/exit
    leaked the session's connector sockets and logged 'Unclosed client
    session' warnings."""
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


# ==============================================================================
# Stage 0.4 — Dual-endpoint failover
#
# Every upstream POST goes through post_with_failover(): if the primary host
# fails at the connection level (ClientError / timeout) or answers HTTP 5xx,
# the identical request is replayed against the fallback endpoint. 4xx
# responses are returned as-is — a bad token or permission error will not
# improve on a different endpoint, so we fail fast instead of retrying.
#
# Endpoints (env, both optional to override):
#   DEEPSEEKER_UPSTREAM_BASE     primary   (default https://chat.deepseek.com)
#   DEEPSEEKER_UPSTREAM_FALLBACK fallback  (default: none; single-endpoint mode)
# ==============================================================================


def _build_upstream_bases():
    primary = (
        (os.getenv("DEEPSEEKER_UPSTREAM_BASE") or "https://chat.deepseek.com")
        .strip()
        .rstrip("/")
    )
    fallback = (os.getenv("DEEPSEEKER_UPSTREAM_FALLBACK") or "").strip().rstrip("/")
    return [primary] + ([fallback] if fallback else [])


UPSTREAM_BASES = _build_upstream_bases()


async def post_with_failover(path, *, headers, session=None, **kwargs):
    """POST to an upstream endpoint with automatic failover across
    UPSTREAM_BASES. This is a plain coroutine that RETURNS an owned response —
    it is not an async context manager:

        resp = await post_with_failover(...)
        async with resp:
            ...

    The caller owns and closes the returned response (async with). Raises the
    last connection error if every endpoint is unreachable, or returns the
    final 4xx/5xx response when the last endpoint answers with one (callers
    keep their existing error paths). `session` is injectable for tests;
    defaults to the shared ClientSession.
    """
    if session is None:
        session = await get_session()
    last_exc = None
    for attempt, base in enumerate(UPSTREAM_BASES):
        is_last = attempt == len(UPSTREAM_BASES) - 1
        try:
            resp = await session.post(base + path, headers=headers, **kwargs)
        except (TimeoutError, aiohttp.ClientError) as e:
            logger.warning("Upstream POST %s failed on %s: %s", path, base, e)
            last_exc = e
            if is_last:
                raise
            continue
        if resp.status >= 500 and not is_last:
            try:
                body = await resp.text()
            except (TimeoutError, aiohttp.ClientError):
                body = ""
            # PR #26 review fix (Medium): hand the failed connection back to
            # the pool. Without this, repeated failovers leak responses and
            # eventually exhaust the connector.
            resp.release()
            logger.warning(
                "Upstream POST %s -> HTTP %d on %s; failing over to fallback endpoint",
                path,
                resp.status,
                base,
            )
            audit.record(
                "failover",
                endpoint=path,
                upstream_status=resp.status,
                reason="upstream_5xx",
            )
            last_exc = UpstreamError(resp.status, body[:200])
            continue
        if attempt > 0:
            logger.info(
                "Upstream POST %s succeeded on fallback endpoint %s", path, base
            )
        return resp
    raise (
        last_exc
        if last_exc is not None
        else RuntimeError("post_with_failover: no endpoints configured")
    )


# B11 (Stage 1 audit): the Android client identity used to be hardcoded to one
# version for ALL tokens — when DeepSeek's app moves, a single stale string
# degrades every account simultaneously, and identical fingerprints correlate
# bans. The version is env-configurable now; per-token identity profiles
# (rotating UA/version/locale) remain a P1.
DEEPSEEKER_CLIENT_VERSION = os.getenv("DEEPSEEKER_CLIENT_VERSION", "2.4.5")


def get_headers(auth_token, pow=None):
    headers = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": "https://chat.deepseek.com",
        "referer": "https://chat.deepseek.com/",
        "user-agent": "Dalvik/2.1.0 (Linux; U; Android 14; Pixel 7)",
        "x-client-platform": "android",
        "x-client-version": DEEPSEEKER_CLIENT_VERSION,
        "x-client-locale": "en_US",
        "x-client-bundle-id": "com.deepseek.chat",
        "x-client-timezone-offset": _TZ_OFFSET,
    }
    if auth_token:
        headers["authorization"] = f"Bearer {auth_token}"
    if pow:
        headers["x-ds-pow-response"] = pow
    return headers


# ==============================================================================
# BACKUP WAF COOKIE GENERATION (DEPRECATED IN FAVOR OF ANDROID CLIENT HEADERS)
#
# DeepSeek's backend does not enforce AWS WAF on requests using Android client
# headers. The Playwright/Chromium cookie generation below is retained as a backup
# so that if DeepSeek ever tightens WAF rules in the future, it can easily be
# re-enabled simply by uncommenting cookies=cookie in API calls.
# ==============================================================================

_cookie_lock = asyncio.Lock()

COOKIE_REGEN_ATTEMPTS = int(os.getenv("DEEPSEEKER_COOKIE_ATTEMPTS", "2"))
COOKIE_REGEN_TIMEOUT = float(os.getenv("DEEPSEEKER_COOKIE_TIMEOUT", "120"))
COOKIE_FAIL_COOLDOWN = float(os.getenv("DEEPSEEKER_COOKIE_COOLDOWN", "20"))

_cookie_fail = {"until": 0.0, "error": ""}


class CookieGenerationError(Exception):
    """Raised when the DeepSeek WAF cookie file cannot be produced."""


class UpstreamError(Exception):
    """Upstream DeepSeek failure carrying the HTTP status and body (B6).

    Replaces the old Exception(f"HTTP {status}: ...") string protocol whose
    'HTTP (\\d{3}):' regex parsing in app.py broke token rotation and error
    mapping whenever a wording changed. Callers read .status directly; str()
    keeps the familiar 'HTTP <status>: <body>' shape for logs."""

    def __init__(self, status, body="", origin="http"):
        self.status = int(status)
        self.body = str(body)
        # Which signal produced this failure: "http" (the completion POST came
        # back non-200) or "sse" (an error event inside an otherwise-200 SSE
        # body). The rate-limit audit keys its reason breakdown on this
        # distinction: an HTTP 429 and a `rate_limit_reached` event both reach
        # app.py as status 429, so the origin is the only thing that can tell
        # them apart when the token is marked RATE_LIMITED.
        self.origin = origin
        super().__init__(f"HTTP {self.status}: {self.body}")


def _read_cookie_file():
    """Return valid (unexpired) cookies, or None."""
    try:
        with open(cookie_file_path()) as f:
            c = json.load(f)
        if c.get("expiry") is not None and c["expiry"] > time.time():
            return c["cookie"]
    except Exception:
        pass
    return None


def _read_stale_cookie_file():
    """Return cookies even if expired (last-resort fallback)."""
    try:
        with open(cookie_file_path()) as f:
            c = json.load(f)
        return c.get("cookie") or None
    except Exception:
        return None


async def get_cookies():
    cookies = _read_cookie_file()
    if cookies:
        return cookies

    def _cooldown_error():
        return CookieGenerationError(
            _cookie_fail["error"]
            + " (cooling down; will retry automatically — try again shortly)"
        )

    if time.time() < _cookie_fail["until"]:
        stale = _read_stale_cookie_file()
        if stale:
            return stale
        raise _cooldown_error()
    async with _cookie_lock:
        cookies = _read_cookie_file()
        if cookies:
            return cookies
        if time.time() < _cookie_fail["until"]:
            stale = _read_stale_cookie_file()
            if stale:
                return stale
            raise _cooldown_error()
        last_err = None
        for attempt in range(1, COOKIE_REGEN_ATTEMPTS + 1):
            try:
                logger.info(
                    "Generating DeepSeek cookies (attempt %d/%d)...",
                    attempt,
                    COOKIE_REGEN_ATTEMPTS,
                )
                await asyncio.wait_for(
                    _generate_cookies(), timeout=COOKIE_REGEN_TIMEOUT
                )
                cookies = _read_cookie_file()
                if cookies:
                    _cookie_fail["until"] = 0.0
                    _cookie_fail["error"] = ""
                    return cookies
                last_err = CookieGenerationError(
                    "cookie file missing/invalid after generation"
                )
            except Exception as e:
                last_err = e
                logger.warning(
                    "DeepSeek cookie generation attempt %d/%d failed: %s",
                    attempt,
                    COOKIE_REGEN_ATTEMPTS,
                    e,
                )
            if attempt < COOKIE_REGEN_ATTEMPTS:
                await asyncio.sleep(min(5 * attempt, 10))
        _cookie_fail["until"] = time.time() + COOKIE_FAIL_COOLDOWN
        _cookie_fail["error"] = f"Could not generate DeepSeek cookies: {last_err}"
        logger.error("%s", _cookie_fail["error"])
        stale = _read_stale_cookie_file()
        if stale:
            logger.warning(
                "Serving STALE DeepSeek cookies after generation failure (upstream may reject them)"
            )
            return stale
        raise CookieGenerationError(_cookie_fail["error"])


async def _generate_cookies():
    if async_playwright is None:
        raise CookieGenerationError("playwright is not installed")
    launch_kwargs = {"headless": False}
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        launch_kwargs["args"] = ["--no-sandbox"]
    async with async_playwright() as p:
        browser = await p.chromium.launch(**launch_kwargs)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(
                "https://chat.deepseek.com/",
                wait_until="domcontentloaded",
                timeout=45000,
            )
            await page.wait_for_selector("body", timeout=30000)
            with contextlib.suppress(Exception):
                await page.wait_for_url("**/sign_in*", timeout=30000)
            cookies = await context.cookies()
        finally:
            await browser.close()
    final_cookies = {}
    expiry = None
    for i in cookies:
        if i.get("name") == "aws-waf-token":
            expiry = i.get("expires")
        final_cookies[i["name"]] = i["value"]
    final_cookies["ds_cookie_preference"] = (
        "%257B%2522level%2522%253A%2522all%2522%257D"
    )
    if not expiry or expiry < 0:
        expiry = time.time() + 1800
    target = cookie_file_path()
    target_dir = os.path.dirname(os.path.abspath(target))
    os.makedirs(target_dir, exist_ok=True)
    tmp_path = os.path.join(target_dir, os.path.basename(target) + ".tmp")
    with open(tmp_path, "w") as f:
        f.write(json.dumps({"cookie": final_cookies, "expiry": expiry}))
    os.replace(tmp_path, target)
    logger.info(
        "DeepSeek cookies saved to %s (expires %s)",
        target,
        datetime.fromtimestamp(expiry) if expiry else "n/a",
    )


def get_auth_token():
    conn = get_db()
    row = conn.execute("SELECT token FROM tokens LIMIT 1").fetchone()
    conn.close()
    if row:
        return row[0]
    return None


def add_token(token, alias=None):
    conn = get_db()
    if not conn.execute("SELECT 1 FROM tokens WHERE id = 1").fetchone():
        next_id = 1
    else:
        row = conn.execute("""
            SELECT min(t1.id + 1)
            FROM tokens t1
            LEFT JOIN tokens t2 ON t1.id + 1 = t2.id
            WHERE t2.id IS NULL
        """).fetchone()
        next_id = row[0] if row and row[0] else 1
    conn.execute(
        "INSERT INTO tokens (id, alias, token, status) VALUES (?, ?, ?, 'ACTIVE')",
        (next_id, alias, token),
    )
    conn.commit()
    conn.close()


def get_tokens():
    conn = get_db()
    rows = conn.execute("SELECT id, alias, token, status FROM tokens").fetchall()
    conn.close()
    return [{"id": r[0], "alias": r[1], "token": r[2], "status": r[3]} for r in rows]


def get_token(token_id):
    conn = get_db()
    row = conn.execute(
        "SELECT id, alias, token, status FROM tokens WHERE id = ?", (token_id,)
    ).fetchone()
    conn.close()
    if row:
        return {"id": row[0], "alias": row[1], "token": row[2], "status": row[3]}
    return None


def delete_token(token_id):
    conn = get_db()
    conn.execute("DELETE FROM tokens WHERE id = ?", (token_id,))
    conn.commit()
    conn.close()


# ==============================================================================
# B3 (Stage 1 audit) — token pool scheduler v2
#
# The old pool selected ACTIVE tokens with ORDER BY RANDOM() and, when every
# token was RATE_LIMITED, fell back to the FIRST token by id. Consequences
# (root cause of issue #32): a limited token never recovered while any other
# token stayed ACTIVE, a single-token pool had no backoff at all, and
# concurrent requests stampeded onto one account — uneven wear and a
# ban-ladder accelerator. The scheduler now provides:
#   * a rate_limited_until cooldown with automatic RATE_LIMITED -> ACTIVE
#     recovery once the cooldown expires (flipped back on the next pick),
#   * least-in-flight selection (spreads load instead of RANDOM()),
#   * an idle-oldest tie-break (the never/least-recently-used token leads),
#   * a soft per-token concurrency cap: capped tokens are only used when
#     nothing else is free,
#   * a soonest-to-recover fallback when every token is still cooling down.
#
# P0-3 adds the third state on top of this scheduler: QUARANTINED. Selection
# logic below is untouched — a quarantined token is simply never a candidate.
# ==============================================================================

TOKEN_COOLDOWN_SECONDS = max(1, int(os.getenv("DEEPSEEKER_RATE_LIMIT_COOLDOWN", "60")))
TOKEN_CONCURRENCY_CAP = max(1, int(os.getenv("DEEPSEEKER_TOKEN_CONCURRENCY", "8")))

# P0-3: a 403 seen this many times in a row (no successful turn in between) is
# not a transient refusal — it is the ban ladder, so the token leaves rotation
# until an operator restores it. 0 disables escalation and keeps today's
# cooldown-everything behavior; a 401 quarantines unconditionally.
QUARANTINE_AFTER_403 = max(0, int(os.getenv("DEEPSEEKER_QUARANTINE_AFTER_403", "3")))

# Per-token in-flight refcounts (per process; single event-loop worker). Every
# send reserves one slot via acquire_token_slot() and releases it when the
# request ends — success, upstream failure or client abort — mirroring the
# ownership-transfer pattern of the per-chat locks.
_in_flight = {}


def _bump_in_flight(token_id, delta):
    n = _in_flight.get(token_id, 0) + delta
    if n > 0:
        _in_flight[token_id] = n
    else:
        _in_flight.pop(token_id, None)


class _TokenSlot:
    """One in-flight reservation against a token. release() is once-only, so
    transferring the slot to a streaming generator (exactly like the per-chat
    lock owner) is safe by construction."""

    __slots__ = ("token_id", "_held")

    def __init__(self, token_id):
        self.token_id = token_id
        self._held = True
        _bump_in_flight(token_id, +1)

    def release(self):
        if not self._held:
            return
        self._held = False
        _bump_in_flight(self.token_id, -1)


def acquire_token_slot(token_id):
    """Reserve one in-flight slot for token_id; pair it with slot.release()."""
    return _TokenSlot(token_id)


def token_in_flight(token_id):
    """Current in-flight request count for a token (selection signal)."""
    return _in_flight.get(token_id, 0)


def pick_token(exclude=None):
    """Select the best available token (scheduler v2 — block comment above).

    `exclude` (optional, B10) lists token ids the caller just saw fail: they
    are avoided while ANY other token is available, so an empty-SSE retry
    cannot land on the same poisoned token.

    QUARANTINED tokens (P0-3) are never a candidate here, not even through the
    all-cooling fallback: a token rejected on auth would otherwise be handed
    straight back to the caller as the "soonest to recover" one.
    """
    now = time.time()
    conn = get_db()
    rows = conn.execute(
        "SELECT id, status, rate_limited_until, last_used FROM tokens"
    ).fetchall()
    conn.close()
    if not rows:
        return None

    excluded = set(exclude or ())
    available = []
    recovered = []
    for tid, status, until, last_used in rows:
        if status == "QUARANTINED":
            continue  # terminal: only restore_token() puts it back (P0-3)
        if status == "RATE_LIMITED" and until is not None and until > now:
            continue  # still cooling down
        if status == "RATE_LIMITED":
            recovered.append(tid)  # cooldown expired -> auto-recover below
        available.append((tid, last_used or 0.0))

    pickable = [entry for entry in available if entry[0] not in excluded] or available
    if pickable:
        if recovered:
            conn = get_db()
            conn.executemany(
                "UPDATE tokens SET status = 'ACTIVE', rate_limited_until = NULL WHERE id = ?",
                [(tid,) for tid in recovered],
            )
            conn.commit()
            conn.close()
            logger.info(
                "Token pool: cooldown expired, auto-recovered token(s) %s", recovered
            )
            for tid in recovered:
                audit.record("rate_recover", token_id=tid, reason="cooldown_expired")

        def _sort_key(entry):
            tid, last_used = entry
            in_flight = _in_flight.get(tid, 0)
            # Soft cap: over-cap tokens sort behind every under-cap one.
            over_cap = 1 if in_flight >= TOKEN_CONCURRENCY_CAP else 0
            return (over_cap, in_flight, last_used, tid)

        return min(pickable, key=_sort_key)[0]

    # Every token is rate-limited and still cooling: pick the one that
    # recovers first (bounded wait) instead of stampeding onto the lowest id
    # with no backoff at all. Quarantined rows stay out of the race — with a
    # pool of nothing but dead credentials this returns None, and the callers
    # already treat "no token" as the not-configured case.
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM tokens WHERE status != 'QUARANTINED' "
        "ORDER BY COALESCE(rate_limited_until, 0) ASC, id ASC LIMIT 1"
    ).fetchone()
    conn.close()
    return row[0] if row else None


def mark_quarantined(token_id, reason):
    """Take a token out of rotation until an operator restores it (P0-3).

    Unlike RATE_LIMITED there is no timer on this state: rate_limited_until is
    cleared so pick_token()'s auto-recovery can never mistake a quarantined row
    for a cooled-down one. The reason is logged for whoever is watching the
    pool.
    """
    logger.warning(
        "Token #%d QUARANTINED (manual return required): %s", token_id, reason
    )
    conn = get_db()
    conn.execute(
        "UPDATE tokens SET status = ?, rate_limited_until = NULL WHERE id = ?",
        ("QUARANTINED", token_id),
    )
    conn.commit()
    conn.close()


def restore_token(token_id):
    """The only exit from quarantine (P0-3): an explicit operator action.

    last_used is deliberately left alone, so a restored token carries the
    oldest usage stamp and the landed idle-oldest tie-break eases it back into
    rotation instead of dumping a burst onto it.
    """
    conn = get_db()
    conn.execute(
        "UPDATE tokens SET status = ?, rate_limited_until = NULL, "
        "consecutive_403 = 0 WHERE id = ?",
        ("ACTIVE", token_id),
    )
    conn.commit()
    conn.close()
    logger.info("Token #%d restored to ACTIVE by operator", token_id)


def _bump_403_streak(token_id):
    """Increment and return this token's consecutive-403 counter."""
    conn = get_db()
    conn.execute(
        "UPDATE tokens SET consecutive_403 = consecutive_403 + 1 WHERE id = ?",
        (token_id,),
    )
    row = conn.execute(
        "SELECT consecutive_403 FROM tokens WHERE id = ?", (token_id,)
    ).fetchone()
    conn.commit()
    conn.close()
    return row[0] if row else 0


def mark_limited(token_id, code=None, reason=None, **audit_fields):
    """Record an upstream failure against a token — the pool's one door.

    Without `code` this is exactly the landed B3 behavior: a RATE_LIMITED
    cooldown that recovers by itself. With the upstream status (P0-3):

      * 401 -> QUARANTINED immediately. A rejected credential is never
        transient, and cooling it for 60s just re-arms the same dead account.
      * 403 -> cooldown as today, plus QUARANTINED once QUARANTINE_AFTER_403
        consecutive 403s pile up with no successful turn in between.
      * anything else (429) -> cooldown, as landed.
    """
    if code == 401:
        mark_quarantined(token_id, "upstream 401 (auth rejected)")
        return
    if code == 403:
        streak = _bump_403_streak(token_id)
        threshold = QUARANTINE_AFTER_403
        if threshold and streak >= threshold:
            mark_quarantined(
                token_id,
                f"{streak} consecutive upstream 403s (threshold {threshold})",
            )
            return
    until = time.time() + TOKEN_COOLDOWN_SECONDS
    logger.warning(
        "Token #%d marked RATE_LIMITED for %ds (recovers automatically)",
        token_id,
        TOKEN_COOLDOWN_SECONDS,
    )
    conn = get_db()
    conn.execute(
        "UPDATE tokens SET status = ?, rate_limited_until = ? WHERE id = ?",
        ("RATE_LIMITED", until, token_id),
    )
    conn.commit()
    conn.close()
    audit.record("rate_limit", token_id=token_id, reason=reason, **audit_fields)


def mark_active(token_id):
    """Stamp a successful turn: back in rotation, streak cleared.

    A QUARANTINED row is deliberately left alone (P0-3). Requests overlap, so a
    send that started before the pull can land its success afterwards; letting
    that flip the status would let a stale response launder an operator-
    controlled state, and only restore_token() ends a quarantine.
    """
    conn = get_db()
    conn.execute(
        "UPDATE tokens SET status = ?, rate_limited_until = NULL, last_used = ?, "
        "consecutive_403 = 0 WHERE id = ? AND status != 'QUARANTINED'",
        ("ACTIVE", time.time(), token_id),
    )
    conn.commit()
    conn.close()


def _format_span(seconds):
    """Compact elapsed time for the pool view: 42s, 7m, 3h, 2d."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


# ==============================================================================
# P0-6 (Days 28-30) — usage stats
#
# One row per COMPLETED turn (the client saw a real answer). Failed attempts
# record nothing, so a turn retried across the pool lands exactly once — the
# counting rule is structural, not a dedup heuristic. Tokens are billed on the
# CLEANED completion (B7 semantics) by the callers, matching the usage chunk
# clients already receive. Cost is derived from the flat tariff table and is
# labeled as such everywhere it renders: it is an estimate against the shipped
# rates, not upstream metering.
# ==============================================================================


def mask_api_key(key):
    """Masked key alias for display: last 4 characters only.

    The dashboard and every aggregate render this — a screenshot of the page
    must not carry usable key material. Short keys (<= 8 chars, e.g. someone's
    one-word key) are fully masked."""
    key = str(key or "").strip()
    if not key:
        return "unknown"
    if len(key) <= 8:
        return "…"
    return "…" + key[-4:]


def record_usage(key_alias, surface, model, in_tokens, out_tokens, ts=None):
    """Record one completed turn. `key_alias` is expected PRE-MASKED — pass
    mask_api_key(raw_key) at the call site; this function never stores raw
    key material as a defense against future callers forgetting."""
    alias = (
        mask_api_key(key_alias)
        if key_alias and not str(key_alias).startswith("…")
        else str(key_alias or "unknown")
    )
    when = time.time() if ts is None else float(ts)
    day = time.strftime("%Y-%m-%d", time.gmtime(when))
    conn = get_db()
    conn.execute(
        "INSERT INTO usage (ts, day, key_alias, surface, model, input_tokens, output_tokens) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            when,
            day,
            alias,
            str(surface or "unknown"),
            str(model or ""),
            max(0, int(in_tokens or 0)),
            max(0, int(out_tokens or 0)),
        ),
    )
    conn.commit()
    conn.close()


def usage_by_day_key(since_ts=0.0):
    """Aggregate usage per day x per masked key alias.

    Returns ready-to-render rows (day, key, surfaces, requests, input_tokens,
    output_tokens, cost, pricing_source) — the template computes nothing, the
    same rule the pool view follows. An empty store is an empty list, never an
    error; an unused key simply has no rows for that day. Performance: the
    (day, key_alias) index covers the GROUP BY, so a month of data stays a
    few milliseconds of SQLite work — no pre-aggregation table needed."""
    conn = get_db()
    rows = conn.execute(
        "SELECT day, key_alias, "
        "GROUP_CONCAT(DISTINCT surface), COUNT(*), "
        "SUM(input_tokens), SUM(output_tokens) "
        "FROM usage WHERE ts >= ? "
        "GROUP BY day, key_alias ORDER BY day DESC, key_alias ASC",
        (float(since_ts or 0.0),),
    ).fetchall()
    conn.close()

    tariff = DEEPSEEK_TARIFFS["deepseek-v4.1-flash"]
    out = []
    for day, alias, surfaces, requests, in_tok, out_tok in rows:
        in_tok = in_tok or 0
        out_tok = out_tok or 0
        cost = (in_tok / 1_000_000 * tariff["cache_miss_input"]) + (
            out_tok / 1_000_000 * tariff["output_generation"]
        )
        out.append(
            {
                "day": day,
                "key": alias,
                "surfaces": surfaces or "unknown",
                "requests": requests,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "cost": round(cost, 6),
                "pricing_source": "flat tariff table (not metered upstream usage)",
            }
        )
    return out


def get_pool_view():
    """Per-token operator view of the pool (P0-3).

    State, load and timers as ready-to-render strings, and no credential field
    at all: the token column is deliberately absent, because this output goes
    straight into HTML a browser caches and a screenshot carries away.
    Formatting happens here so templates/dashboard.html renders state without
    computing any — a template that computes is one that can grow a secret.
    """
    now = time.time()
    conn = get_db()
    rows = conn.execute(
        "SELECT id, alias, status, rate_limited_until, last_used, consecutive_403 "
        "FROM tokens ORDER BY id"
    ).fetchall()
    conn.close()

    view = []
    for tid, alias, status, until, last_used, streak in rows:
        streak = streak or 0
        if status == "QUARANTINED":
            state_class, cooldown = "err", "—"
            note = "quarantined — manual return required"
        elif status == "RATE_LIMITED" and until and until > now:
            state_class = "warn"
            cooldown = f"{_format_span(until - now)} left"
            note = "cooling down, recovers automatically"
        elif status == "RATE_LIMITED":
            state_class, cooldown = "warn", "expired"
            note = "rejoins rotation on the next pick"
        else:
            state_class, cooldown = "ok", "—"
            note = "in rotation"
        if streak and status != "QUARANTINED":
            note = f"{note}, 403 streak {streak}"
        age = "never" if not last_used else f"{_format_span(now - last_used)} ago"
        view.append(
            {
                "id": tid,
                "label": alias if alias else f"untagged #{tid}",
                "status": status,
                "state_class": state_class,
                "state_note": note,
                "in_flight": _in_flight.get(tid, 0),
                "last_used": age,
                "cooldown": cooldown,
                "can_restore": status == "QUARANTINED",
            }
        )
    return view


def find_session(sig):
    conn = get_db()
    row = conn.execute(
        "SELECT token_id, deepseek_session_id, parent_message_id FROM sessions WHERE signature = ?",
        (sig,),
    ).fetchone()
    if row:
        # B13: touch on read so pruning evicts by real recency of USE, not by
        # insertion order — long-running chats get older rows every turn, and
        # they are precisely the sessions pruning must protect.
        conn.execute(
            "UPDATE sessions SET last_used = ? WHERE signature = ?", (time.time(), sig)
        )
        conn.commit()
    conn.close()
    if row:
        return {"token_id": row[0], "session_id": row[1], "parent_message_id": row[2]}
    return None


# Cap on stored session signatures. Every request stores 2 rows and nothing
# ever removed them, so after many chats the SQLite file (and its WAL) grew
# unbounded — on volume-limited deployments a full disk freezes ALL requests,
# including brand-new chats. PRUNE_EVERY saves trigger a prune that keeps the
# MAX_SESSIONS MOST-RECENTLY-USED rows (B13: by last_used, not insertion
# order — the old rowid policy evicted long-running chats, the most valuable
# sessions, exactly because they hold the oldest rows).
MAX_SESSIONS = int(os.getenv("DEEPSEEKER_MAX_SESSIONS", "20000"))
PRUNE_EVERY = int(os.getenv("DEEPSEEKER_PRUNE_EVERY", "500"))
_save_counter = {"n": 0}


def prune_sessions():
    conn = get_db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        deleted = 0
        if total > MAX_SESSIONS:
            # B13: evict LEAST-RECENTLY-USED. NULL last_used (never re-hit)
            # sorts as 0, i.e. evicted first; rowid breaks ties deterministically.
            deleted = conn.execute(
                "DELETE FROM sessions WHERE rowid IN "
                "(SELECT rowid FROM sessions ORDER BY COALESCE(last_used, 0) ASC, rowid ASC LIMIT ?)",
                (total - MAX_SESSIONS,),
            ).rowcount
        deleted_map = conn.execute(
            "DELETE FROM session_map WHERE created_at < datetime('now', '-7 days')"
        ).rowcount
        conn.commit()
        if deleted or deleted_map:
            logger.info(
                "Pruned %d session signature(s) and %d stale session_map row(s)",
                deleted,
                deleted_map,
            )
    finally:
        conn.close()


def save_session(sig, token_id, session_id, parent_message_id=0):
    conn = get_db()
    conn.execute(
        """INSERT OR REPLACE INTO sessions (signature, token_id, deepseek_session_id, parent_message_id)
           VALUES (?, ?, ?, ?)""",
        (sig, token_id, session_id, parent_message_id),
    )
    conn.commit()
    conn.close()
    _save_counter["n"] += 1
    if _save_counter["n"] >= PRUNE_EVERY:
        _save_counter["n"] = 0
        try:
            prune_sessions()
        except Exception:
            logger.exception("Session pruning failed (non-fatal)")
        # Audit retention rides the existing prune cadence - no new timer.
        try:
            audit.prune_audit()
        except Exception:
            logger.exception("Audit pruning failed (non-fatal)")


def delete_session(sig):
    conn = get_db()
    conn.execute("DELETE FROM sessions WHERE signature = ?", (sig,))
    conn.commit()
    conn.close()


def next_parent(parent_message_id):
    """Compute the parent_message_id to use for the next turn on a chat session.

    Invariant: each successful /chat/completion request appends exactly one user
    message and one assistant message to the DeepSeek chat session, so if the
    request was sent with parent_message_id P, the last message id afterwards is
    P + 1 and the next request must use P + 2.

    DeepSeek's web API exposes no endpoint to list a session's messages, so this
    increment cannot be verified against the server; it is centralized here so
    every save_session() call site (token-rotation branch, non-stream and both
    streaming paths) stays consistent if the invariant ever changes.
    """
    return parent_message_id + 2


def delete_sessions_for_chat(token_id, session_id):
    conn = get_db()
    conn.execute(
        "DELETE FROM sessions WHERE token_id = ? AND deepseek_session_id = ?",
        (token_id, session_id),
    )
    conn.commit()
    conn.close()


# ==============================================================================
# B4 (Stage 1 audit) — file ownership registry
#
# /v1/files uploads picked a RANDOM token and upstream files are
# account-scoped, so a later /v1/chat/completions referencing that file_id
# could pick a different token and get "file not found" — the OpenAI-style
# upload->reference flow (Claude Code / Cline file flows) was broken by
# design. Uploads are now pinned to their token; chat handlers prefer the
# file-owner token on the first turn and re-home foreign references onto the
# chat's own token on later turns.
# ==============================================================================


def record_file(file_id, token_id):
    """Pin an upstream file_id to the token (account) that owns it.
    First owner wins: INSERT OR IGNORE keeps the original mapping stable."""
    conn = get_db()
    conn.execute(
        "INSERT OR IGNORE INTO files (file_id, token_id, created_at) VALUES (?, ?, ?)",
        (file_id, token_id, time.time()),
    )
    conn.commit()
    conn.close()


def get_file_token(file_id):
    """Return the token id that owns an uploaded file_id, or None."""
    conn = get_db()
    row = conn.execute(
        "SELECT token_id FROM files WHERE file_id = ?", (file_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


# DeepSeek now serves a single model (v4.1flash) as the website default. The
# web API uses the website default when model_type is null, so no explicit
# value is sent. If DeepSeek ever exposes an explicit model_type enum value
# for v4.1flash, this constant is the single place to set it.
DEFAULT_MODEL_TYPE = None

# Flat peak-hour rates (per 1M tokens) for the single default model,
# per https://api-docs.deepseek.com/quick_start/pricing
DEEPSEEK_TARIFFS = {
    "deepseek-v4.1-flash": {
        "cache_miss_input": 0.44,
        "output_generation": 1.32,
    },
}


def count_tokens(text, model="deepseek-v4.1-flash"):
    return len(deepseek_tokenizer.ds_token.encode(text))



# The request's reasoning effort ("high", "max", ...) when thinking is on, set by
# the chat endpoints; build_prompt turns it into a think-harder instruction.
request_effort = contextvars.ContextVar("request_effort", default=None)


def effort_level(body):
    """The client's effort string, wherever the dialect puts it."""
    cands = [body.get("reasoning_effort"), body.get("effort")]
    for key in ("reasoning", "output_config", "thinking"):
        v = body.get(key)
        if isinstance(v, dict):
            cands += [v.get("effort"), v.get("reasoning_effort"), v.get("level")]
        elif key == "thinking" and isinstance(v, str):
            cands.append(v)
    for c in cands:
        if isinstance(c, str) and c.strip():
            return c.strip().lower()
    return None


# The tool-call dialect parser lives in dsml.py -- it was ~900 lines of regex
# interleaved with the transport code in this module. Re-exported because app.py
# and the tests import these names from ``functions``.
from dsml import (  # noqa: F401
    StreamToolParser,
    _is_plausible_stream_closer_prefix,
    _is_plausible_stream_entry_prefix,
    normalize_tool_call,
    parse_tools,
    request_tools,
    tool_block_is_truncated,
)


def summarize_messages(messages, max_tokens=500):
    recent = messages[-10:] if len(messages) > 10 else messages
    parts = []
    for msg in recent:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                c.get("text", "") for c in content if c.get("type") == "text"
            )
        if content:
            parts.append(f"{role}: {content[:200]}")
    summary = "\n".join(parts)
    tokens = count_tokens(summary)
    while tokens > max_tokens and len(parts) > 1:
        parts = parts[1:]
        summary = "\n".join(parts)
        tokens = count_tokens(summary)
    return summary


_pow_setup = None


def _get_pow_setup():
    global _pow_setup
    if _pow_setup is None:
        engine = wasmtime.Engine()
        with open(wasm_path, "rb") as f:
            module = wasmtime.Module(engine, f.read())
        _pow_setup = (engine, module, wasmtime.Linker(engine))
    return _pow_setup


def _find_pow_answer_blocking(challange_data):
    engine, module, linker = _get_pow_setup()
    store = wasmtime.Store(engine)
    instance = linker.instantiate(store, module)
    memory = instance.exports(store)["memory"]
    alloc_func = instance.exports(store)["alloc"]
    solve_func = instance.exports(store)["solve_pow"]
    ch_ptr, ch_len = write_string_pow(
        challange_data["challenge"], alloc_func, memory, store
    )
    salt_ptr, salt_len = write_string_pow(
        challange_data["salt"], alloc_func, memory, store
    )
    result = solve_func(
        store,
        ch_ptr,
        ch_len,
        salt_ptr,
        salt_len,
        challange_data["expire_at"],
        challange_data["difficulty"],
    )
    if result < 0:
        result = result + 0x10000000000000000
    return result if result != 0xFFFFFFFFFFFFFFFF else None


async def create_challange_pow(target_path, auth_token):
    headers = get_headers(auth_token)
    # Backup: WAF cookies not required with Android headers. Kept as fallback:
    # cookie = await get_cookies()
    response = await post_with_failover(
        "/api/v0/chat/create_pow_challenge",
        headers=headers,
        json={"target_path": target_path},
        # cookies=cookie,  # Backup WAF fallback
        timeout=aiohttp.ClientTimeout(total=20),
    )
    async with response:
        data = await response.json()
    return data["data"]["biz_data"]["challenge"]


def write_string_pow(text, alloc_func, memory, store):
    data = text.encode("utf-8")
    ptr = alloc_func(store, len(data))
    mem = memory.data_ptr(store)
    for i in range(len(data)):
        mem[ptr + i] = data[i]
    return ptr, len(data)


async def find_pow_answer(challange_data):
    return await asyncio.to_thread(_find_pow_answer_blocking, challange_data)


async def solve_create_pow(target_path, auth_token):
    pow = await create_challange_pow(target_path, auth_token)
    answer = await find_pow_answer(pow)
    if answer is None:
        raise Exception("PoW solve failed")
    json_data = {
        "algorithm": "DeepSeekHashV1",
        "challenge": pow["challenge"],
        "salt": pow["salt"],
        "answer": answer,
        "signature": pow["signature"],
        "target_path": target_path,
    }
    return base64.b64encode(json.dumps(json_data).encode()).decode()


async def create_new_chat(auth_token):
    headers = get_headers(auth_token)
    # Backup: WAF cookies not required with Android headers. Kept as fallback:
    # cookie = await get_cookies()
    response = await post_with_failover(
        "/api/v0/chat_session/create",
        headers=headers,
        # cookies=cookie,  # Backup WAF fallback
        timeout=aiohttp.ClientTimeout(total=20),
    )
    async with response:
        data = await response.json()
    return data["data"]["biz_data"]["chat_session"]["id"]


_EMPTY_SSE_DEFAULT = (
    "Empty response from DeepSeek (no parseable SSE content): "
    "stream ended with no assistant fragments"
)
_EMPTY_SSE_CONTEXT = (
    "Empty response from DeepSeek (prompt may exceed the session context limit)"
)
_CONTEXT_LIMIT_HINT_RE = re.compile(
    r"maximum.?context"
    r"|context.?(?:length|limit|window)"
    r"|token.?limit"
    r"|(?:prompt|payload|conversation|messages?).{0,24}too.?long"
    r"|exceed(?:ed|s)?[^.\n]{0,30}context",
    re.IGNORECASE,
)
_SSE_RECENT_LINES = 8


def is_context_limit_error(exc):
    """Public classifier (DSH fix): did this failure come from the upstream
    session running out of context?

    Agent harnesses die here: a context-class failure used to be retried
    verbatim on other tokens (the context belongs to the CHAT, not the token,
    so every attempt fails the same way) and finally surfaced to the client as
    a context-limit error. The caller uses this to trigger a summary rollover
    instead. Matches:
      * UpstreamError bodies carrying upstream context wording (HTTP 400
        'maximum context length', 'context limit', 'too long', ...), and
      * the bridge's own empty-SSE-with-context-hint error.
    Deliberately NOT matched: generic 5xx/timeouts — 'deadline exceeded' or
    '502 upstream timeout' are retry-shaped, not context-shaped. The loose
    words only count when anchored to context material: a bare 'exceed' or a
    'too long' with no prompt/message in front of it no longer classifies as
    context (review fix, PR #67 — an upstream body like 'header limit
    exceeded' for a non-context reason used to force a wasteful summary
    rollover), and the plain-exception fallback requires explicit context-
    limit phrasing — a mere 'context' substring ('context deadline
    exceeded') is retry-shaped, not context-shaped."""
    if isinstance(exc, UpstreamError):
        body = exc.body or ""
        return bool(_CONTEXT_LIMIT_HINT_RE.search(body))
    text = str(exc or "")
    return bool(
        re.search(
            r"maximum.?context|context.?(?:length|limit|window)|token.?limit",
            text,
            re.IGNORECASE,
        )
    )


class _SseStreamFinished(Exception):
    pass


def _redact_sse_line(line: str) -> str:
    if len(line) > 500:
        return line[:500] + "..."
    return line


def _remember_sse_line(recent_lines, line: str):
    recent_lines.append(_redact_sse_line(line))
    if len(recent_lines) > _SSE_RECENT_LINES:
        del recent_lines[0]


def _sse_context_limit_hint(recent_lines, parsed_events):
    for obj in parsed_events:
        try:
            blob = json.dumps(obj, ensure_ascii=False)
        except Exception:
            blob = str(obj)
        if _CONTEXT_LIMIT_HINT_RE.search(blob):
            return True
    return any(_CONTEXT_LIMIT_HINT_RE.search(line) for line in recent_lines)


def _scan_recent_sse_errors(parsed_events):
    """Return (http_status, message) if a recent parsed event is an upstream error."""
    for obj in reversed(parsed_events):
        if not isinstance(obj, dict) or obj.get("type") != "error":
            continue
        content = obj.get("content") or obj.get("message") or "Upstream error"
        finish = str(obj.get("finish_reason") or "")
        if finish == "rate_limit_reached" or "rate_limit" in finish.lower():
            return 429, content
        if finish in ("permission_denied", "forbidden"):
            return 403, content
        return 502, content
    return None


def _raise_empty_sse_response(recent_lines, parsed_events):
    err = _scan_recent_sse_errors(parsed_events)
    if err:
        code, msg = err
        logger.warning(
            "DeepSeek SSE error event (not empty stream); last events: %s",
            recent_lines[-_SSE_RECENT_LINES:],
        )
        raise UpstreamError(code, msg, origin="sse")
    if _sse_context_limit_hint(recent_lines, parsed_events):
        msg = _EMPTY_SSE_CONTEXT
    else:
        msg = _EMPTY_SSE_DEFAULT
    logger.warning(
        "DeepSeek SSE ended without assistant output; last events: %s",
        recent_lines[-_SSE_RECENT_LINES:],
    )
    raise UpstreamError(502, msg)


def _raise_sse_error_event(data, recent_lines):
    content = data.get("content") or data.get("message") or "Upstream error"
    finish = str(data.get("finish_reason") or "")
    if finish == "rate_limit_reached" or "rate_limit" in finish.lower():
        logger.warning(
            "DeepSeek SSE error event (not empty stream); last events: %s",
            recent_lines[-_SSE_RECENT_LINES:],
        )
        raise UpstreamError(429, content, origin="sse")
    if finish in ("permission_denied", "forbidden"):
        raise UpstreamError(403, content, origin="sse")
    raise UpstreamError(502, content, origin="sse")


# DeepSeek generates one message per account at a time; a second concurrent send
# fails with "A message is being generated" / "Messages too frequent". Queue sends
# per token instead. Every generation goes through send_message, so this covers all
# callers.
_send_locks = {}


def token_busy(auth_token):
    """True while this account is generating (its send lock is held)."""
    lock = _send_locks.get(auth_token)
    return bool(lock and lock.locked())


# Raw-text capture handoff between send_message's pump() and the streaming
# _send_message_unlocked(). A ContextVar is used rather than an argument so the
# capture hook never changes that function's public signature (it is called
# from app.py in several recovery paths) and so a nested/abandoned stream can
# never write into another request's buffers. The value is a dict:
#     {"prompt": [str], "resp": [bytes], "meta": {}}
# Lists are mutated in place; pump() owns the dict and clears it after writing.
_raw_capture_var = contextvars.ContextVar("deeperseeker_raw_capture", default=None)


def _raw_capture():
    """The capture dict for this task, or None when capture is off."""
    cap = _raw_capture_var.get()
    return cap if isinstance(cap, dict) else None


def _capture_prompt(json_data):
    """Stash the exact prompt + request knobs. Stores a reference only."""
    cap = _raw_capture()
    if not cap:
        return
    cap["prompt"].append(json_data.get("prompt"))
    cap["meta"].update(
        {
            "model_type": json_data.get("model_type"),
            "thinking_enabled": json_data.get("thinking_enabled"),
            "search_enabled": json_data.get("search_enabled"),
            "ref_file_ids": list(json_data.get("ref_file_ids") or []),
        }
    )


def _capture_resp_chunk(raw_line):
    """Append one raw SSE line (bytes, undecoded, unredacted)."""
    cap = _raw_capture()
    if not cap:
        return
    cap["resp"].append(raw_line)


async def _flush_raw_capture(cap, request_id):
    """Write the captured exchange off-loop. Never raises.

    Called from pump()'s finally, so it must not mask the original exception
    or raise a new one: everything here is best-effort auditing.
    """
    if not cap:
        return
    prompt_buf = cap.get("prompt") or []
    resp_buf = cap.get("resp") or []
    meta = cap.get("meta") or {}
    if not prompt_buf and not resp_buf:
        return
    try:
        prompt = None
        if prompt_buf:
            prompt = "".join(str(p) for p in prompt_buf if p is not None)
        response = None
        if resp_buf:
            response = b"\n".join(resp_buf).decode("utf-8", "replace")
        index = await asyncio.to_thread(
            audit_raw.record_raw,
            request_id,
            prompt=prompt,
            response=response,
            meta=meta,
        )
        if index:
            await audit.arecord("raw", raw=index, request_id=request_id)
    except Exception:  # noqa: BLE001 - capture must never break a request
        logger.exception("raw audit capture failed")
    finally:
        cap["prompt"].clear()
        cap["resp"].clear()
        cap["meta"].clear()


def _current_request_id():
    """This task's request id, or None outside a request (tests, CLI)."""
    from middleware import request_id_var

    rid = request_id_var.get()
    if not rid or rid == "-":
        return None
    return rid


async def send_message(chat_id, auth_token, *args, **kwargs):
    # The upstream stream is drained by its own task, so the lock is released when
    # DeepSeek finishes even if the consumer abandons this generator mid-stream
    # (client gone without aclose): holding it across our yields leaked the lock
    # and wedged every later request on that account.
    lock = _send_locks.setdefault(auth_token, asyncio.Lock())
    queue = asyncio.Queue()
    done = object()

    async def pump():
        cap = {"prompt": [], "resp": [], "meta": {}}
        # request_id has to be read here: create_task copies the context, so
        # the middleware's binding is still visible, but pump() itself runs in
        # a task that outlives the route handler's context on cancellation.
        request_id = _current_request_id()
        token = _raw_capture_var.set(cap)
        try:
            async with lock:
                gen = _send_message_unlocked(chat_id, auth_token, *args, **kwargs)
                try:
                    async for chunk in gen:
                        queue.put_nowait(chunk)
                finally:
                    await gen.aclose()
        except Exception as e:
            queue.put_nowait(e)
        finally:
            _raw_capture_var.reset(token)
            await _flush_raw_capture(cap, request_id)
            queue.put_nowait(done)

    task = asyncio.create_task(pump())
    try:
        while (item := await queue.get()) is not done:
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        # A consumer that closes us (disconnect, abort) still cancels the upstream.
        task.cancel()


async def _send_message_unlocked(
    chat_id,
    auth_token,
    message,
    parent_message_id,
    thinking=False,
    search=False,
    file_ids_=None,
):
    # Backup: WAF cookies not required with Android headers. Kept as fallback:
    # cookie = await get_cookies()
    if parent_message_id == 0:
        parent_message_id = None
    file_ids = file_ids_ or []

    headers = get_headers(
        auth_token, await solve_create_pow("/api/v0/chat/completion", auth_token)
    )
    json_data = {
        "chat_session_id": chat_id,
        "parent_message_id": parent_message_id,
        "model_type": DEFAULT_MODEL_TYPE,
        "prompt": message,
        "ref_file_ids": file_ids,
        "thinking_enabled": thinking,
        "search_enabled": search,
        "preempt": False,
        "action": None,
    }
    # Reference-only stash: the prompt is the caller's string, not a copy.
    _capture_prompt(json_data)

    think_open = False
    got_output = False
    # DeepSeek ends a reply it cut short (seen mid-thinking on long contexts) with
    # status INCOMPLETE. With no answer text yet that is a failed turn, not a
    # finished one: an empty "stop" makes agent clients end the turn.
    incomplete = False
    got_response = False
    resp = await post_with_failover(
        "/api/v0/chat/completion",
        headers=headers,
        json=json_data,
        # cookies=cookie,  # Backup WAF fallback
    )
    async with resp:
        if resp.status != 200:
            error_text = await resp.text()
            logger.warning(
                "DeepSeek completion HTTP %d for chat %s: %s",
                resp.status,
                chat_id,
                error_text[:300],
            )
            _capture_resp_chunk(
                f"HTTP {resp.status}\n{error_text}".encode("utf-8", "replace")
            )
            raise UpstreamError(resp.status, error_text)

        recent_lines = []
        parsed_events = []
        line_buf = b""

        def _check_incomplete():
            if incomplete and not got_response:
                raise UpstreamError(
                    502, "DeepSeek stopped the reply before any answer (status INCOMPLETE)"
                )

        async def _emit_from_event(data):
            nonlocal think_open, got_output, incomplete, got_response
            if isinstance(data, dict) and data.get("type") == "error":
                _raise_sse_error_event(data, recent_lines)

            if data.get("o") == "BATCH" and isinstance(data.get("v"), list):
                for op in data["v"]:
                    if not isinstance(op, dict):
                        continue
                    if op.get("p") == "quasi_status":
                        incomplete = incomplete or op.get("v") == "INCOMPLETE"
                        continue  # a status word, never content
                    async for chunk in _emit_from_event(op):
                        yield chunk
                return

            # Any other status field ("response/status", ".../status") carries a
            # status word such as INCOMPLETE or WIP, never content.
            p_name, p_val = str(data.get("p") or ""), data.get("v")
            if (
                p_name.endswith("status")
                and isinstance(p_val, str)
                and p_val.isupper()
                and p_val != "FINISHED"
            ):
                incomplete = incomplete or p_val == "INCOMPLETE"
                return

            if data.get("p") == "response/status" and data.get("v") == "FINISHED":
                if think_open:
                    yield "\n</think>\n\n"
                _check_incomplete()
                if not got_output:
                    _raise_empty_sse_response(recent_lines, parsed_events)
                raise _SseStreamFinished()

            if "v" in data and isinstance(data["v"], dict) and "response" in data["v"]:
                fragments = data["v"]["response"].get("fragments")
                if fragments:
                    for fragment in fragments:
                        if fragment.get("type") == "THINK":
                            if not think_open:
                                yield "<think>\n"
                                think_open = True
                            got_output = True
                            yield fragment.get("content", "")
                        else:
                            if think_open:
                                yield "\n</think>\n\n"
                                think_open = False
                            got_output = True
                            got_response = got_response or bool(fragment.get("content"))
                            yield fragment.get("content", "")
                return

            if data.get("p") == "response/fragments" and data.get("o") == "APPEND":
                fragments = data.get("v")
                if isinstance(fragments, list):
                    for fragment in fragments:
                        if fragment.get("type") == "RESPONSE":
                            if think_open:
                                yield "\n</think>\n\n"
                                think_open = False
                            got_output = True
                            got_response = got_response or bool(fragment.get("content"))
                            yield fragment.get("content", "")
                        elif fragment.get("type") == "THINK":
                            if not think_open:
                                yield "<think>\n"
                                think_open = True
                            got_output = True
                            yield fragment.get("content", "")
                        else:
                            got_output = True
                            yield fragment.get("content", "")
                return

            v = data.get("v")
            if isinstance(v, str) and v:
                got_output = True
                got_response = got_response or not think_open
                yield v

        async def _consume_sse_line(decoded_line: str):
            if not decoded_line.startswith("data: "):
                return
            _remember_sse_line(recent_lines, decoded_line)
            try:
                data = json.loads(decoded_line[6:])
            except Exception:
                return
            if isinstance(data, dict):
                parsed_events.append(data)
                if len(parsed_events) > _SSE_RECENT_LINES:
                    del parsed_events[0]
            try:
                async for chunk in _emit_from_event(data):
                    yield chunk
            except _SseStreamFinished:
                raise

        async for chunk in resp.content.iter_any():
            if not chunk:
                continue
            line_buf += chunk
            while b"\n" in line_buf:
                raw_line, line_buf = line_buf.split(b"\n", 1)
                # Capture before the empty-line skip so the stored stream is
                # byte-faithful: blank separators are part of the wire format
                # and dropping them would make a replay misleading.
                _capture_resp_chunk(raw_line)
                decoded_line = raw_line.decode("utf-8", errors="replace").strip()
                if not decoded_line:
                    continue
                try:
                    async for out in _consume_sse_line(decoded_line):
                        yield out
                except _SseStreamFinished:
                    return

        if line_buf.strip():
            _capture_resp_chunk(line_buf)
            decoded_line = line_buf.decode("utf-8", errors="replace").strip()
            if decoded_line:
                try:
                    async for out in _consume_sse_line(decoded_line):
                        yield out
                except _SseStreamFinished:
                    return

        if think_open:
            yield "\n</think>\n\n"
        _check_incomplete()
        if not got_output:
            _raise_empty_sse_response(recent_lines, parsed_events)


async def upload_file(file_bytes, file_name, file_content_type, auth_token):
    # Backup: WAF cookies not required with Android headers. Kept as fallback:
    # cookie = await get_cookies()
    session = await get_session()
    file_size = len(file_bytes)
    pow_response = await solve_create_pow("/api/v0/file/upload_file", auth_token)
    boundary = b"----WebKitFormBoundaryTB0pXOQR2RL219Hu"
    safe_name = re.sub(r"[^ -~]", "_", file_name).replace('"', "_") or "file.bin"
    body_parts = [
        b"--" + boundary + b"\r\n",
        f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'.encode(),
        f"Content-Type: {file_content_type}\r\n\r\n".encode(),
        file_bytes,
        b"\r\n--" + boundary + b"--\r\n",
    ]
    reconstructed_body = b"".join(body_parts)
    headers = get_headers(auth_token, pow_response)
    headers.update(
        {
            "content-type": f"multipart/form-data; boundary={boundary.decode('utf-8')}",
            "x-file-size": str(file_size),
        }
    )
    response = await post_with_failover(
        "/api/v0/file/upload_file",
        data=reconstructed_body,
        headers=headers,
        # cookies=cookie,  # Backup WAF fallback
        timeout=aiohttp.ClientTimeout(total=120),
    )
    async with response:
        resp_json = await response.json()
    file_id = resp_json["data"]["biz_data"]["id"]
    yield ("uploaded", file_id)
    js_data = resp_json["data"]["biz_data"]
    status = js_data["status"]
    headers = get_headers(auth_token)
    deadline = time.time() + 300
    while status in ["PENDING", "PARSING"] and time.time() < deadline:
        yield ("uploaded", file_id)
        await asyncio.sleep(0.3)
        async with session.get(
            "https://chat.deepseek.com/api/v0/file/fetch_files?file_ids=" + file_id,
            headers=headers,
            # cookies=cookie,  # Backup WAF fallback
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            js_data = (await resp.json())["data"]["biz_data"]["files"][0]
        status = js_data["status"]
    if status == "SUCCESS":
        tp_data = datetime.fromtimestamp(js_data["updated_at"], UTC)
        yield (
            "success",
            {
                "file_id": file_id,
                "openai_timestamp": int(js_data["updated_at"]),
                "size": js_data["file_size"],
                "anthropic_timestamp": tp_data.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
    else:
        yield ("error", file_id)


async def get_file_content(auth_token, file_id):
    # Backup: WAF cookies not required with Android headers. Kept as fallback:
    # cookie = await get_cookies()
    session = await get_session()
    headers = get_headers(auth_token)
    async with session.get(
        "https://chat.deepseek.com/api/v0/file/fetch_files?file_ids=" + file_id,
        headers=headers,
        # cookies=cookie,  # Backup WAF fallback
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        resp_json = await resp.json()
    js_data = resp_json["data"]["biz_data"]["files"][0]
    yield mimetypes.guess_type(js_data["file_name"])[0]
    deadline = time.time() + 60
    while (
        js_data.get("status") in ("PENDING", "PARSING")
        and time.time() < deadline
        and not js_data.get("signed_path")
    ):
        await asyncio.sleep(0.5)
        async with session.get(
            "https://chat.deepseek.com/api/v0/file/fetch_files?file_ids=" + file_id,
            headers=headers,
            # cookies=cookie,  # Backup WAF fallback
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            js_data = (await resp.json())["data"]["biz_data"]["files"][0]
    if not js_data.get("signed_path"):
        return
    file_path = "https://files.deepseeksvc.com/api" + js_data["signed_path"] + "&ty=r"
    async with session.get(file_path) as data:
        async for chunk in data.content.iter_chunked(8192):
            if chunk:
                yield chunk
