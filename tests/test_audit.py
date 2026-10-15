"""Regression tests for the audit & perf store (audit.py + its call sites).

The audit layer answers questions the token status column cannot: WHICH
upstream signal rate-limited a token (a real HTTP 429, or a
`rate_limit_reached` event inside an otherwise-200 SSE body?), how often each
token was limited, and which requests rolled over / retried / failed over.
Three invariants are load-bearing and guarded here:

* record() NEVER raises. Auditing is best-effort - a broken or read-only
  database must cost audit rows, never a chat response.
* The rate-limit reason vocabulary stays in sync with the SQL in
  token_rate_limit_summary(): `http_429` / `sse_rate_limit` / `http_4%`.
  app._rate_limit_reason() is the only producer of those strings, and a
  regression there silently flattens the breakdown to all-http_other.
* Ambient request fields bound by bind_request() are inherited by every
  event, but an explicit kwarg always wins.

Run:  python tests/test_audit.py   (pytest-compatible)
"""

import os
import sqlite3
import sys
import tempfile
import time
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audit  # noqa: E402

# ==============================================================================
# Fixture
# ==============================================================================


def _fresh_db():
    """Point audit at a throwaway DB and create the schema there."""
    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, "audit.db")
    patcher = mock.patch.object(audit, "_DB_PATH", path)
    patcher.start()
    audit.init_audit_db()
    # Ambient context leaks across tests through the ContextVar; reset it.
    audit._ctx.set(None)
    return patcher


def _run_with_db(fn):
    patcher = _fresh_db()
    try:
        return fn()
    finally:
        patcher.stop()


# ==============================================================================
# record / query_events
# ==============================================================================


def test_record_then_query_roundtrip():
    def body():
        audit.record(
            "rate_limit", token_id=7, reason="http_429", endpoint="/v1/messages"
        )
        rows = audit.query_events(kind="rate_limit")
        assert len(rows) == 1, rows
        row = rows[0]
        assert row["kind"] == "rate_limit"
        assert row["token_id"] == 7
        assert row["reason"] == "http_429"
        assert row["endpoint"] == "/v1/messages"
        assert row["ts"] > 0

    _run_with_db(body)


def test_query_events_filters_and_order():
    def body():
        audit.record("rate_limit", token_id=1, reason="http_429")
        audit.record("rate_limit", token_id=2, reason="sse_rate_limit")
        audit.record("failover", token_id=1, reason="upstream_5xx")

        only_kind = audit.query_events(kind="rate_limit")
        assert [r["token_id"] for r in only_kind] == [2, 1], "newest first"

        only_token = audit.query_events(token_id=1)
        assert {r["kind"] for r in only_token} == {"rate_limit", "failover"}

        assert audit.query_events(since=time.time() + 60) == []
        assert len(audit.query_events(limit=2)) == 2

    _run_with_db(body)


def test_record_never_raises_on_broken_db():
    def body():
        # A directory where the file should be: sqlite3.connect succeeds but
        # every statement fails. record() must swallow it.
        #
        # Only the WRITE path is best-effort. The readers deliberately raise:
        # /v1/audit/* turns that into a 503 "audit store unavailable", which
        # an operator must be able to tell apart from "no events in window".
        bad = tempfile.mkdtemp()
        with mock.patch.object(audit, "_DB_PATH", bad):
            assert audit.record("request", endpoint="/x") is None
            try:
                audit.query_events()
            except sqlite3.Error:
                pass
            else:  # pragma: no cover - the raise is the contract
                raise AssertionError("readers must surface a broken store")

    _run_with_db(body)


def test_record_disabled_returns_none_and_writes_nothing():
    def body():
        with mock.patch.dict(os.environ, {"DEEPSEEKER_AUDIT_ENABLED": "0"}):
            assert audit.enabled() is False
            assert audit.record("request") is None
        assert audit.query_events() == [], "disabled auditing must write no rows"

    _run_with_db(body)


def test_stream_flag_is_normalized_to_int():
    def body():
        audit.record("request", stream=True)
        audit.record("request", stream=False)
        assert audit.query_events()[0]["stream"] == 0
        assert audit.query_events()[1]["stream"] == 1

    _run_with_db(body)


# ==============================================================================
# Ambient request context
# ==============================================================================


def test_bind_request_fields_are_inherited_but_kwargs_win():
    def body():
        audit.bind_request(request_id="req-1", endpoint="/v1/chat/completions")
        audit.record("rate_limit", token_id=3, reason="http_429")
        row = audit.query_events()[0]
        assert row["request_id"] == "req-1", "ambient field must be inherited"
        assert row["endpoint"] == "/v1/chat/completions"

        # An explicit value at the call site overrides the ambient one.
        audit.record("failover", endpoint="/v1/messages")
        assert audit.query_events()[0]["endpoint"] == "/v1/messages"

    _run_with_db(body)


# ==============================================================================
# token_rate_limit_summary - the upstream rate-limit audit view
# ==============================================================================


def test_rate_limit_summary_breaks_down_by_reason():
    def body():
        audit.record("rate_limit", token_id=1, reason="http_429")
        audit.record("rate_limit", token_id=1, reason="http_429")
        audit.record("rate_limit", token_id=1, reason="sse_rate_limit")
        audit.record("rate_limit", token_id=1, reason="http_403")
        audit.record("rate_limit", token_id=2, reason="http_429")
        # A recover and a non-limit event must not leak into the counts.
        audit.record("rate_recover", token_id=1, reason="cooldown_expired")
        audit.record("failover", token_id=1, reason="upstream_5xx")

        rows = audit.token_rate_limit_summary()
        assert [r["token_id"] for r in rows] == [1, 2], "sorted by limit_count desc"
        top = rows[0]
        assert top["limit_count"] == 4, top
        assert top["http_429"] == 2, top
        assert top["sse_rate_limit"] == 1, top
        assert top["http_other"] == 1, "http_403 is neither 429 nor sse"
        assert top["recover_count"] == 1, top
        assert top["last_reason"] == "http_403", "newest reason wins"
        assert top["last_limited_at"] > 0

    _run_with_db(body)


def test_rate_limit_summary_respects_since_window():
    def body():
        audit.record("rate_limit", token_id=1, reason="http_429")
        rows = audit.token_rate_limit_summary(since=time.time() + 60)
        assert rows == [], "events outside the window must not be counted"

    _run_with_db(body)


# ==============================================================================
# perf_summary
# ==============================================================================


def test_perf_summary_counts_latency_and_counters():
    def body():
        for latency, status in ((100.0, 200), (200.0, 200), (300.0, 500), (400.0, None)):
            audit.record(
                "request",
                endpoint="/v1/chat/completions",
                upstream_status=status,
                latency_ms=latency,
            )
        audit.record("request", endpoint="/v1/messages", latency_ms=500.0,
                     upstream_status=200, stream=True)
        audit.record("rate_limit", token_id=1, reason="http_429")
        audit.record("rate_recover", token_id=1, reason="cooldown_expired")
        audit.record("rollover", session_id="s-1")

        s = audit.perf_summary(window=3600)
        assert s["requests"] == 5, s
        # A null upstream_status counts as a failure alongside >=400.
        assert s["failures"] == 2, s
        assert abs(s["error_rate"] - 0.4) < 1e-9, s
        assert s["streams"] == 1, s
        lat = s["latency_ms"]
        assert lat["avg"] == 300.0, lat
        assert lat["max"] == 500.0, lat
        assert lat["p50"] == 300.0, lat
        assert s["counters"] == {"rate_limit": 1, "rate_recover": 1, "rollover": 1}, s
        assert s["rate_limit_reasons"] == {"http_429": 1}, s

    _run_with_db(body)


def test_perf_summary_window_is_clamped():
    def body():
        assert audit.perf_summary(window=1)["window_seconds"] == 60
        assert audit.perf_summary(window=10**9)["window_seconds"] == 30 * 24 * 3600

    _run_with_db(body)


# ==============================================================================
# prune_audit
# ==============================================================================


def test_prune_drops_rows_past_retention():
    def body():
        audit.record("request")
        conn = audit.sqlite3.connect(audit._DB_PATH)
        conn.execute("UPDATE audit_events SET ts = ?", (time.time() - 30 * 86400,))
        conn.commit()
        conn.close()

        with mock.patch.dict(os.environ, {"DEEPSEEKER_AUDIT_RETENTION_DAYS": "7"}):
            deleted = audit.prune_audit()
        assert deleted == 1, deleted
        assert audit.query_events() == []

    _run_with_db(body)


def test_prune_enforces_row_cap_oldest_first():
    def body():
        for i in range(5):
            audit.record("request", detail=f"row-{i}")

        env = {
            "DEEPSEEKER_AUDIT_MAX_ROWS": "2",
            "DEEPSEEKER_AUDIT_RETENTION_DAYS": "365",
        }
        with mock.patch.dict(os.environ, env):
            audit.prune_audit()
        remaining = audit.query_events()
        assert len(remaining) == 2, remaining
        assert {r["detail"] for r in remaining} == {"row-3", "row-4"}, remaining

    _run_with_db(body)


def test_prune_never_raises():
    def body():
        with mock.patch.object(audit, "_DB_PATH", tempfile.mkdtemp()):
            assert audit.prune_audit() == 0

    _run_with_db(body)


# ==============================================================================
# app._rate_limit_reason - the producer of the reason vocabulary
# ==============================================================================


def test_rate_limit_reason_separates_sse_from_http_429():
    import app

    class UpstreamError(Exception):
        def __init__(self, status, origin="http"):
            super().__init__(f"HTTP {status}")
            self.status = status
            self.origin = origin

    # A 200 SSE body carrying rate_limit_reached reaches callers as 429 too;
    # only .origin separates it from a real HTTP 429 - and the audit view's
    # http_429 / sse_rate_limit split keys on exactly this distinction.
    assert app._rate_limit_reason(UpstreamError(429, origin="sse")) == "sse_rate_limit"
    assert app._rate_limit_reason(UpstreamError(429)) == "http_429"
    assert app._rate_limit_reason(UpstreamError(403, origin="sse")) == "http_403"
    assert app._rate_limit_reason(UpstreamError(401)) == "http_401"
    # Connection-level failures carry no status and must not invent a reason.
    assert app._rate_limit_reason(Exception("boom")) is None


def test_sse_origin_matches_the_summary_sql():
    """End-to-end: app's reason vocabulary feeds audit's breakdown."""
    import app

    class UpstreamError(Exception):
        def __init__(self, status, origin="http"):
            super().__init__(f"HTTP {status}")
            self.status = status
            self.origin = origin

    def body():
        audit.record(
            "rate_limit",
            token_id=5,
            reason=app._rate_limit_reason(UpstreamError(429, origin="sse")),
        )
        row = audit.token_rate_limit_summary()[0]
        assert row["sse_rate_limit"] == 1, row
        assert row["http_429"] == 0, row
        assert row["http_other"] == 0, row

    _run_with_db(body)


def main():
    tests = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    if failed:
        sys.exit(1)
    print(f"{len(tests)} tests passed")


if __name__ == "__main__":
    main()
