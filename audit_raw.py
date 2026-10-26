"""Raw upstream text capture for the audit trail.

Stores the full text of what was sent upstream (the assembled prompt) and
what came back (the raw SSE lines), separately from the aggregate audit
events in `audit.py`. The event store keeps only an index; the bodies live
here, compressed and redacted.

Design constraints, mirroring `audit.py`:

* This module imports the STANDARD LIBRARY ONLY. `audit.py` and
  `functions.py` both import it, so importing either back would create a
  cycle. The secret-scrub patterns below are a deliberate duplicate of
  `errors.py` and must stay in sync with it - `test_redaction_regex_matches_errors_module`
  is the tripwire.
* Writes are best-effort: `record_raw()` NEVER raises. A broken or
  unwritable raw directory costs raw bodies, never a chat response.
* Reads deliberately raise so `/v1/audit/raw/*` can answer 503, the same
  contract `audit.py` uses.
* Path derivation is recomputed here rather than imported, exactly like
  `audit.py` does relative to `functions.py`: `DB_PATH` is the single
  override and both modules must agree on where `data/` is.

Storage layout: one file per UTC day and direction,

    data/audit/raw/raw-YYYY-MM-DD-req.jsonl.zst
    data/audit/raw/raw-YYYY-MM-DD-resp.jsonl.zst

Each captured request/response pair is ONE zstd frame appended to the day's
file. zstd permits concatenated frames, so:

* `stream_reader` decodes the whole file in order;
* random access is a seek to the recorded offset plus a single-frame
  decompress - no need to walk the file from the start;
* a crash truncates at most the last frame; earlier frames stay readable
  (each starts with the zstd magic number, so a torn tail is detectable).

The index that locates a frame is recorded as a `raw` column on the
ordinary audit event (see `audit.py`), never inside `detail` - that field
is capped at 2000 characters and means "METHOD PATH".
"""

import contextlib
import json
import os
import re
import sys
import threading
import time

# ==============================================================================
# Paths
# ==============================================================================

# Recomputed, not imported - see the module docstring. `_DB_PATH` mirrors
# audit.py:56 and functions.py:39 so all three agree on the data directory.
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_BASE_DIR, "data")
_DB_PATH = os.getenv("DB_PATH") or os.path.join(_DATA_DIR, "deeperseeker.db")
_RAW_DIR = os.path.join(os.path.dirname(os.path.abspath(_DB_PATH)), "audit", "raw")

# `raw-YYYY-MM-DD-req.jsonl.zst` / `raw-YYYY-MM-DD-resp.jsonl.zst`.
# Deliberately a separate pattern from audit.py's `_FILE_RE`: the audit
# readers glob only `audit-*.jsonl`, so raw files can never be mistaken for
# event files by `prune_audit()` or `query_events()`.
_FILE_RE = re.compile(r"^raw-(\d{4}-\d{2}-\d{2})-(req|resp)\.jsonl\.zst$")

_write_lock = threading.Lock()


# ==============================================================================
# Config
# ==============================================================================


def enabled():
    """Raw capture is ON by default and additionally gated by the audit switch.

    The user asked for raw capture to be on out of the box (2026-10-26), with
    volume controlled by the retention window and byte cap rather than by a
    default-off switch. `DEEPSEEKER_AUDIT_ENABLED=0` still wins: turning the
    whole audit layer off must not leave raw capture running.
    """
    if not _truthy(os.getenv("DEEPSEEKER_AUDIT_ENABLED", "1")):
        return False
    return _truthy(os.getenv("DEEPSEEKER_AUDIT_RAW_ENABLED", "1"))


def _truthy(value):
    return (value or "").strip().lower() not in ("0", "false", "no", "off")


def retention_days():
    """Days of raw bodies to keep. Default 180 (half a year); 0 = no time cap.

    Six months is the user's stated preference: keep everything as long as it
    is not taking much space. The byte cap in `prune_raw()` is what actually
    bounds growth; this window only stops a trickle of old days from
    accumulating forever.
    """
    try:
        return max(0, int(os.getenv("DEEPSEEKER_AUDIT_RAW_RETENTION_DAYS", "180")))
    except ValueError:
        return 180


def max_bytes():
    """Byte cap for the whole raw directory. Default 2 GiB; 0 = no cap.

    Pruning only runs when the directory exceeds this, so a low-volume
    deployment keeps its full 180-day history untouched.
    """
    try:
        return max(0, int(os.getenv("DEEPSEEKER_AUDIT_RAW_MAX_BYTES", str(2 * 1024**3))))
    except ValueError:
        return 2 * 1024**3


# ==============================================================================
# Redaction
# ==============================================================================

# MUST stay byte-identical to errors.py:74-75. `errors.py` imports functions
# and middleware, both of which import audit, so importing it here would
# close the cycle. The duplication is the same trade-off audit.py already
# makes for the data-directory path (audit.py:49-53, functions.py:42-44).
_SECRET_RUN_RE = re.compile(r"[A-Za-z0-9+/_=-]{32,}")
_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._+=/-]+")


def redact(text):
    """Scrub credential-shaped material before it reaches the disk."""
    scrubbed = _BEARER_RE.sub("[redacted]", str(text))
    return _SECRET_RUN_RE.sub("[redacted]", scrubbed)


# ==============================================================================
# File helpers
# ==============================================================================


def _warn(msg):
    """One stderr line per failure, independent of logging configuration."""
    with contextlib.suppress(Exception):
        print(f"deeperseeker audit_raw: {msg}", file=sys.stderr)


def _date_from_ts(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _file_for_ts(ts, direction):
    return os.path.join(_RAW_DIR, f"raw-{_date_from_ts(ts)}-{direction}.jsonl.zst")


def _file_date(path):
    m = _FILE_RE.match(os.path.basename(path))
    return m.group(1) if m else ""


def _raw_files():
    """Every raw file, oldest name first. Missing directory is empty, not an error."""
    try:
        names = os.listdir(_RAW_DIR)
    except FileNotFoundError:
        return []
    except OSError:
        raise
    out = []
    for name in names:
        if _FILE_RE.match(name):
            out.append(os.path.join(_RAW_DIR, name))
    out.sort()
    return out


def _append_frame(payload, direction, ts=None):
    """Append one zstd frame. Returns (path, offset, length). Raises on failure.

    The caller has already redacted and JSON-encoded `payload`.
    """
    import zstandard

    ts = time.time() if ts is None else ts
    path = _file_for_ts(ts, direction)
    data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    frame = zstandard.ZstdCompressor(level=3).compress(data)
    with _write_lock:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Offset is taken inside the lock so two concurrent appends cannot
        # record the same position.
        offset = os.path.getsize(path) if os.path.exists(path) else 0
        with open(path, "ab") as fh:
            fh.write(frame)
    return path, offset, len(frame)


def read_frame(path, offset, length):
    """Decompress one frame. Raises on I/O failure; returns None on a bad frame.

    A corrupt or truncated frame is data loss we can report as "missing",
    not a store outage - the 503 contract is about the directory being
    unreadable, which `open()` surfaces.
    """
    import zstandard

    with open(path, "rb") as fh:
        fh.seek(offset)
        blob = fh.read(length)
    if not blob:
        return None
    try:
        raw = zstandard.ZstdDecompressor().decompress(blob)
    except zstandard.ZstdError:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def read_body(index, direction):
    """The stored text for one side of an exchange, or None.

    `index` is the dict recorded on the audit event's `raw` column. A missing
    side (an aborted request has a prompt but no response), a deleted file and
    a corrupt frame all answer None - "this body is not available" - while an
    unreadable directory raises, matching read_frame()'s 503 contract.
    """
    entry = (index or {}).get(direction)
    if not isinstance(entry, dict):
        return None
    path = os.path.join(_RAW_DIR, os.path.basename(entry.get("f") or ""))
    if not entry.get("f"):
        return None
    frame = read_frame(path, int(entry.get("o") or 0), int(entry.get("l") or 0))
    if not isinstance(frame, dict):
        return None
    text = frame.get("text")
    return None if text is None else str(text)


# ==============================================================================
# Recording
# ==============================================================================


def record_raw(request_id, prompt=None, response=None, meta=None, ts=None):
    """Persist one captured exchange. NEVER raises.

    Returns the index dict that belongs on the audit event's `raw` column:

        {"req": {"f": "...", "o": 0, "l": 1234},
         "resp": {"f": "...", "o": 1234, "l": 5678}}

    Either side may be absent (an aborted request has a prompt but no
    response). Returns None when capture is disabled or every write failed.
    """
    if not enabled():
        return None
    ts = time.time() if ts is None else ts
    index = {}
    if prompt is not None:
        entry = _write_side(
            {"request_id": request_id, "kind": "prompt", "text": redact(prompt),
             "meta": meta or {}, "ts": ts},
            "req",
            ts,
        )
        if entry:
            index["req"] = entry
    if response is not None:
        entry = _write_side(
            {"request_id": request_id, "kind": "response", "text": redact(response),
             "ts": ts},
            "resp",
            ts,
        )
        if entry:
            index["resp"] = entry
    return index or None


def _write_side(payload, direction, ts):
    try:
        path, offset, length = _append_frame(payload, direction, ts)
    except Exception as e:  # noqa: BLE001 - capture must never break a request
        _warn(f"record_raw({direction}) failed: {e}")
        return None
    return {"f": os.path.basename(path), "o": offset, "l": length}


# ==============================================================================
# Retention
# ==============================================================================


def _dir_bytes(paths):
    total = 0
    for path in paths:
        try:
            total += os.path.getsize(path)
        except OSError:
            continue
    return total


def prune_raw():
    """Drop old raw bodies and enforce the byte cap. Returns files removed.

    Two independent gates, in this order:

    1. Whole-day files older than `retention_days()` go in one step. Raw
       files are split by UTC day precisely so this is a single unlink.
    2. If the directory still exceeds `max_bytes()`, delete the oldest
       remaining files until it fits - but always keep the newest day, so a
       cap smaller than one day's traffic cannot wipe live data.

    Never raises; failures are reported and counted as "not removed".
    """
    if not enabled():
        return 0
    removed = 0
    try:
        days = retention_days()
        if days > 0:
            cutoff_date = _date_from_ts(time.time() - days * 86400)
            for path in _raw_files():
                if _file_date(path) and _file_date(path) < cutoff_date:
                    try:
                        os.remove(path)
                        removed += 1
                    except OSError as e:
                        _warn(f"prune_raw could not drop {path}: {e}")
        cap = max_bytes()
        if cap > 0:
            files = _raw_files()
            total = _dir_bytes(files)
            if total > cap:
                newest_day = _file_date(files[-1]) if files else ""
                for path in files:
                    if total <= cap:
                        break
                    # Keep at least the newest UTC day.
                    if _file_date(path) == newest_day:
                        continue
                    try:
                        size = os.path.getsize(path)
                        os.remove(path)
                        total -= size
                        removed += 1
                    except OSError as e:
                        _warn(f"prune_raw could not drop {path}: {e}")
    except Exception as e:  # noqa: BLE001 - pruning is opportunistic
        _warn(f"prune_raw failed: {e}")
    return removed