"""Regression tests for the raw-body store (audit_raw.py).

`audit.py` records WHAT happened (one JSONL row per event); `audit_raw.py`
records the WORDS - the assembled prompt sent upstream and the raw SSE lines
that came back - compressed and redacted, in a separate directory. The event
row keeps only a small index into that store.

Invariants guarded here:

* record_raw() NEVER raises. Raw capture is best-effort in exactly the same
  sense as audit.record(): an unwritable raw directory costs raw bodies,
  never a chat response.
* Raw capture is ON by default but `DEEPSEEKER_AUDIT_ENABLED=0` still wins,
  and `DEEPSEEKER_AUDIT_RAW_ENABLED=0` turns only the raw layer off.
* The scrub patterns are a deliberate DUPLICATE of errors.py (importing it
  would close the audit -> errors -> functions -> audit cycle). The
  duplication is only safe if it is byte-identical, so
  test_redaction_matches_errors_module is the tripwire.
* Retention is a two-gate policy: whole UTC days past the window go first,
  then the byte cap trims oldest-first - but the newest day is always kept,
  so a cap smaller than one day's traffic cannot wipe live data.
* A truncated or corrupt frame reads back as None (reported as missing), not
  as a store outage.

Run:  python tests/test_raw_audit.py   (pytest-compatible)
"""

import os
import sys
import tempfile
import time
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import audit_raw  # noqa: E402

# ==============================================================================
# Fixture
# ==============================================================================


def _fresh_raw(env=None):
    """Point the raw store at a throwaway directory.

    _RAW_DIR is resolved at call time by _file_for_ts(), so patching the
    module constant is what redirects the writes - patching _DB_PATH alone
    does not, because _RAW_DIR is derived from it once at import (same trap
    documented in tests/test_audit.py::_fresh_db).

    `env` overlays DEEPSEEKER_* switches for the duration of the test; the
    defaults are left untouched so the "on by default" contract is what the
    tests actually exercise.
    """
    tmpdir = tempfile.mkdtemp()
    patcher = mock.patch.multiple(
        audit_raw,
        _RAW_DIR=os.path.join(tmpdir, "audit", "raw"),
    )
    patcher.start()
    # Start from the shipped defaults so a leftover variable from the host
    # cannot make "on by default" pass (or fail) for the wrong reason.
    switches = {"DEEPSEEKER_AUDIT_ENABLED": "1", "DEEPSEEKER_AUDIT_RAW_ENABLED": "1"}
    switches.update(env or {})
    env_patcher = mock.patch.dict(os.environ, switches)
    env_patcher.start()
    return patcher, env_patcher


def _run_with_raw(fn, env=None):
    patchers = _fresh_raw(env)
    try:
        return fn()
    finally:
        for p in patchers:
            p.stop()


def _read_side(index, direction):
    entry = index[direction]
    path = os.path.join(audit_raw._RAW_DIR, entry["f"])
    return audit_raw.read_frame(path, entry["o"], entry["l"])


# ==============================================================================
# Round trip
# ==============================================================================


def test_write_read_roundtrip():
    def run():
        index = audit_raw.record_raw(
            "req-1",
            prompt="hello upstream",
            response='data: {"v": "hi there"}\n\ndata: [DONE]\n\n',
            meta={"model": "v4.1flash"},
        )
        assert index is not None, "record_raw returned no index"
        assert set(index) == {"req", "resp"}, index

        req = _read_side(index, "req")
        resp = _read_side(index, "resp")
        assert req["text"] == "hello upstream", req
        assert req["kind"] == "prompt"
        assert req["request_id"] == "req-1"
        assert req["meta"] == {"model": "v4.1flash"}
        assert resp["text"].startswith('data: {"v": "hi there"}'), resp
        assert resp["kind"] == "response"

    _run_with_raw(run)


def test_index_carries_file_offset_length():
    def run():
        index = audit_raw.record_raw("req-2", prompt="x" * 100)
        entry = index["req"]
        assert set(entry) == {"f", "o", "l"}, entry
        assert entry["f"].endswith("-req.jsonl.zst"), entry
        assert entry["o"] == 0, "first frame in a fresh file starts at 0"
        assert entry["l"] > 0, entry
        # Appending a second body to the same day+direction must not reuse
        # the first frame's offset.
        index2 = audit_raw.record_raw("req-3", prompt="y" * 100)
        assert index2["req"]["o"] == entry["l"], (entry, index2)

    _run_with_raw(run)


def test_either_side_may_be_absent():
    def run():
        only_prompt = audit_raw.record_raw("req-4", prompt="just the ask")
        assert set(only_prompt) == {"req"}, only_prompt

        only_resp = audit_raw.record_raw("req-5", response="just the answer")
        assert set(only_resp) == {"resp"}, only_resp

        nothing = audit_raw.record_raw("req-6")
        assert nothing is None, "no bodies means no index"

    _run_with_raw(run)


# ==============================================================================
# Redaction
# ==============================================================================


def test_redaction_matches_errors_module():
    """The duplicate regexes must stay byte-identical to errors.py.

    audit_raw cannot import errors (errors -> functions -> audit -> ...);
    the trade-off is only sound while the two copies agree.
    """
    import errors

    assert audit_raw._SECRET_RUN_RE.pattern == errors._SECRET_RUN_RE.pattern
    assert audit_raw._BEARER_RE.pattern == errors._BEARER_RE.pattern
    assert audit_raw._SECRET_RUN_RE.flags == errors._SECRET_RUN_RE.flags
    assert audit_raw._BEARER_RE.flags == errors._BEARER_RE.flags


def test_redaction_scrubs_bearer_and_long_runs():
    scrubbed = audit_raw.redact(
        "Authorization: Bearer sk-abc123DEF456ghi789jkl012 "
        "session aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa done"
    )
    assert "sk-abc123DEF456ghi789jkl012" not in scrubbed, scrubbed
    assert "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" not in scrubbed, scrubbed
    assert "Authorization: [redacted]" in scrubbed, scrubbed
    # Ordinary prose survives untouched.
    assert audit_raw.redact("hello world, 你好") == "hello world, 你好"


def test_redaction_applied_before_write():
    def run():
        index = audit_raw.record_raw("req-7", prompt="token " + "f" * 40)
        body = _read_side(index, "req")
        assert "f" * 40 not in body["text"], body["text"]
        assert "[redacted]" in body["text"], body["text"]

    _run_with_raw(run)


# ==============================================================================
# Failure domain
# ==============================================================================


def test_record_raw_never_raises_on_broken_dir():
    def run():
        # A file where the directory should be makes makedirs fail.
        blocker = os.path.join(audit_raw._RAW_DIR)
        os.makedirs(os.path.dirname(blocker), exist_ok=True)
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("not a directory")
        assert audit_raw.record_raw("req-8", prompt="boom") is None

    _run_with_raw(run)


def test_read_frame_tolerates_corrupt_frame():
    def run():
        index = audit_raw.record_raw("req-9", prompt="ok")
        entry = index["req"]
        path = os.path.join(audit_raw._RAW_DIR, entry["f"])
        # Overwrite the frame body with something that is not zstd.
        with open(path, "r+b") as fh:
            fh.seek(entry["o"])
            fh.write(b"not-zstd-at-all")
        assert audit_raw.read_frame(path, entry["o"], entry["l"]) is None
        # Reading past EOF is "missing", not an exception.
        assert audit_raw.read_frame(path, 10**9, 10) is None

    _run_with_raw(run)


# ==============================================================================
# Switches
# ==============================================================================


def test_disabled_writes_nothing():
    def run():
        assert audit_raw.enabled() is False
        assert audit_raw.record_raw("req-10", prompt="should not land") is None
        assert not os.path.exists(audit_raw._RAW_DIR) or not os.listdir(
            audit_raw._RAW_DIR
        )

    _run_with_raw(run, env={"DEEPSEEKER_AUDIT_RAW_ENABLED": "0"})


def test_audit_switch_wins_over_raw_switch():
    def run():
        # Raw enabled on its own must not survive the parent audit switch.
        assert audit_raw.enabled() is False
        assert audit_raw.record_raw("req-11", prompt="nope") is None

    _run_with_raw(run, env={"DEEPSEEKER_AUDIT_ENABLED": "0"})


def test_on_by_default():
    assert audit_raw.enabled() is True


def test_defaults_are_six_months_and_two_gib():
    for key in (
        "DEEPSEEKER_AUDIT_RAW_RETENTION_DAYS",
        "DEEPSEEKER_AUDIT_RAW_MAX_BYTES",
    ):
        os.environ.pop(key, None)
    assert audit_raw.retention_days() == 180
    assert audit_raw.max_bytes() == 2 * 1024**3


# ==============================================================================
# Retention
# ==============================================================================


def _write_day(day_offset, direction="req"):
    """Write one frame into a file dated `day_offset` days ago."""
    ts = time.time() - day_offset * 86400
    index = audit_raw.record_raw("req-day", prompt="p" * 50, ts=ts)
    return os.path.join(audit_raw._RAW_DIR, index[direction]["f"])


def test_prune_raw_drops_whole_old_days():
    def run():
        old = _write_day(10)
        fresh = _write_day(0)
        os.environ["DEEPSEEKER_AUDIT_RAW_RETENTION_DAYS"] = "7"
        removed = audit_raw.prune_raw()
        assert removed == 1, removed
        assert not os.path.exists(old), "10-day-old file should be gone"
        assert os.path.exists(fresh), "today's file must survive"

    _run_with_raw(run)


def test_prune_raw_respects_byte_cap_oldest_first():
    def run():
        days = [_write_day(n) for n in (5, 4, 3, 2, 1, 0)]
        os.environ["DEEPSEEKER_AUDIT_RAW_RETENTION_DAYS"] = "0"  # time gate off
        os.environ["DEEPSEEKER_AUDIT_RAW_MAX_BYTES"] = "1"  # force trimming
        audit_raw.prune_raw()
        assert not os.path.exists(days[0]), "oldest must go first"
        assert os.path.exists(days[-1]), "newest day is always kept"

    _run_with_raw(run)


def test_prune_raw_skips_when_under_cap():
    def run():
        kept = _write_day(3)
        os.environ["DEEPSEEKER_AUDIT_RAW_RETENTION_DAYS"] = "0"
        os.environ["DEEPSEEKER_AUDIT_RAW_MAX_BYTES"] = str(10 * 1024**2)
        assert audit_raw.prune_raw() == 0
        assert os.path.exists(kept), "under the cap nothing is deleted"

    _run_with_raw(run)


def test_prune_raw_never_raises():
    def run():
        os.environ["DEEPSEEKER_AUDIT_RAW_RETENTION_DAYS"] = "1"
        # Directory unreadable/absent must be swallowed, not raised.
        audit_raw._RAW_DIR = os.path.join(
            tempfile.mkdtemp(), "no", "such", "dir"
        )
        assert audit_raw.prune_raw() == 0

    _run_with_raw(run)


def test_prune_raw_disabled_is_noop():
    def run():
        kept = _write_day(30)
        os.environ["DEEPSEEKER_AUDIT_RAW_RETENTION_DAYS"] = "1"
        os.environ["DEEPSEEKER_AUDIT_RAW_ENABLED"] = "0"
        assert audit_raw.prune_raw() == 0
        assert os.path.exists(kept)

    _run_with_raw(run)


# ==============================================================================
# Standalone runner
# ==============================================================================

if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - test runner
            failures += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {name}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)