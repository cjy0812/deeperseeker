"""Regression tests for the admin-session gate on the audit endpoints.

The bug these guard (found while wiring the /audit page):

* `get_current_admin` is SYNCHRONOUS - it returns the string "admin" on
  success. `/v1/audit/rate-limits` called `await get_current_admin(request)`,
  which raised `TypeError: object str can't be used in 'await' expression`.
  The surrounding `except HTTPException` could not catch a TypeError, so the
  request fell through to RecovererMiddleware as a 500. The admin-session
  path - the ONLY path that endpoint accepts - was therefore entirely
  broken; only a caller who never authenticated could get a clean 401.
  Any future `await get_current_admin` reintroduces it.

The invariant: an authenticated admin session gets 200 from the audit
endpoints, an unauthenticated caller gets 401, and neither gets a 500.

Run:  python tests/test_admin_audit_gate.py   (pytest-compatible)
"""

import os
import sys
import tempfile
import time
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import audit  # noqa: E402


class _AuditDb:
    """Point the audit store at a throwaway directory for the test body.

    _AUDIT_DIR is the constant the path helpers resolve at call time, and it
    is derived from _DB_PATH once at import - patching _DB_PATH alone would
    leave the endpoints reading the repo's real data/audit/.
    """

    def __enter__(self):
        self._tmpdir = tempfile.mkdtemp()
        self._patch = mock.patch.multiple(
            audit,
            _DB_PATH=os.path.join(self._tmpdir, "audit.db"),
            _AUDIT_DIR=os.path.join(self._tmpdir, "audit"),
        )
        self._patch.start()
        audit._seq_state["next"] = None
        audit.init_audit_db()
        audit._ctx.set(None)
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        return False


def _admin_client():
    """A TestClient carrying a live admin session cookie."""
    sid = "test-admin-session"
    app_module.SESSIONS[sid] = time.time()
    client = TestClient(app_module.app, cookies={"session_id": sid})
    return client, sid


def test_rate_limits_admin_session_is_not_a_500():
    """The regression: this used to be a TypeError -> 500."""
    client, sid = _admin_client()
    try:
        with _AuditDb():
            r = client.get("/v1/audit/rate-limits")
        assert r.status_code == 200, (r.status_code, r.text)
        body = r.json()
        assert body["tokens"] == []
        assert "enabled" in body
    finally:
        app_module.SESSIONS.pop(sid, None)


def test_rate_limits_without_session_is_401():
    # _AuditDb only redirects the store - the request still runs the full
    # middleware stack, whose per-exchange audit hook would otherwise append
    # this 401 into the repo's real data/audit/.
    client = TestClient(app_module.app)
    with _AuditDb():
        r = client.get("/v1/audit/rate-limits")
    assert r.status_code == 401, (r.status_code, r.text)


def test_perf_accepts_admin_session():
    """Operators should not need the API key to read the perf roll-up."""
    client, sid = _admin_client()
    try:
        with _AuditDb():
            r = client.get("/v1/audit/perf")
        assert r.status_code == 200, (r.status_code, r.text)
        assert "error_rate" in r.json()
    finally:
        app_module.SESSIONS.pop(sid, None)


def test_perf_still_rejects_strangers():
    client = TestClient(app_module.app)
    with _AuditDb():
        r = client.get("/v1/audit/perf")
    assert r.status_code == 401, (r.status_code, r.text)


def main():
    tests = [
        v
        for k, v in sorted(globals().items())
        if k.startswith("test_") and callable(v)
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001 - test runner
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())