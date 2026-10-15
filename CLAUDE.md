# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commit Policy

**After every completed change — bug fix, new feature, refactor, doc update — commit it to git before moving on.** Do not batch unrelated work into one commit, and do not leave completed work uncommitted at the end of a turn. Match the existing commit style (short imperative subject, e.g. `Add ruff checking`, `Fix ci badge typo in readme`). Only push when explicitly asked.

## Commands

```bash
uv sync                            # install runtime deps
uv sync --extra dev --extra tests  # what CI installs (ruff + pytest + httpx)
cp .env.example .env               # first-time setup
uv run deeperseeker                # run the server (or: python app.py)
```

Lint and test — CI runs exactly these two, on Python 3.13:

```bash
.venv/bin/ruff check             # lint (config in pyproject.toml, line-length 88)
.venv/bin/ruff check --fix       # autofix
.venv/bin/pytest                 # full suite
.venv/bin/pytest tests/test_token_pool.py                       # one file
.venv/bin/pytest tests/test_token_pool.py::test_cooldown_auto_recovery  # one test
python tests/test_token_pool.py  # each test file is also directly runnable
```

Tests are pytest-compatible but written to run standalone (`python tests/<file>.py`), so each file inserts the repo root into `sys.path` itself. There is no `conftest.py` — do not assume one exists. `tmpdir`-style fixtures are often hand-rolled inside the test file.

Dependency changes go through `requirements*.in` (pip-compile) plus `uv.lock`, not by editing `requirements*.txt` by hand:
- `requirements.in` → runtime; `requirements-dev.in` → ruff/pip-tools; `requirements-tests.in` → pytest/httpx; `requirements-waf-backup.in` → the deprecated Playwright path.
- `pyproject.toml` reads these via `hatch-requirements-txt`, so the `.in` files are the source of truth.

## Architecture

A FastAPI reverse proxy that impersonates the DeepSeek web chat client and re-exposes it as OpenAI- and Anthropic-compatible APIs. Clients talk OpenAI/Anthropic wire formats; the proxy translates both directions over a persistent *web chat session* rather than a stateless API call.

The whole system is four flat modules at the repo root (no package directory) plus `static/`, `templates/`, `wasm/`:

- **`app.py`** (~2.5K lines) — the FastAPI surface. All HTTP routes, both streaming writers (`stream_response` for OpenAI, `stream_anthropic_response` for Anthropic), request/response format conversion, dashboard + admin auth, and the central `handle_chat` orchestration function.
- **`functions.py`** (~2K lines) — the upstream client and SQLite store. Talks to `chat.deepseek.com`, manages the auth-token pool, PoW challenge solving, cookie generation, session persistence, file upload, and SSE parsing. Also home to `StreamToolParser` and multi-format tool-call parsing.
- **`plugin_helper.py`** — prompt construction and the context policy. Canonicalization/message signature, history token budgeting, the summarize-and-rollover trigger, attachment description, and file extraction/upload. This is where context-window behavior lives, not in `app.py`.
- **`middleware.py`** — `RealIPMiddleware` (X-Forwarded-For resolution gated by `TRUSTED_PROXIES`), `RequestIDMiddleware`, `RecovererMiddleware` (converts unhandled exceptions into JSON errors instead of raw 500s).

### Request flow

`POST /v1/chat/completions` (or `/v1/messages`) → normalize the body into a common `messages` list → `handle_chat` in `app.py`.

`handle_chat` is the heart of the system and the file to read first:

1. **Session lookup.** `generate_signature` hashes the canonicalized message history (up to the last assistant turn), the model, and the API key scope. `find_session` maps that signature to an existing upstream web chat, so a continued conversation reuses its session. Creation is lock-protected per signature so concurrent identical requests don't create duplicate chats.
2. **Token selection.** `pick_token` chooses from the SQLite token pool with cooldown auto-recovery, least-in-flight selection, an idle-oldest tie-break, and a soft per-token concurrency cap (`acquire_token_slot`). Rate-limited tokens are excluded until their cooldown expires.
3. **Rollover check.** `needs_rollover` decides whether accumulated context is near the observed ~393K-token memory limit. If so, a *scratch* chat is created purely to generate a handoff summary, then a fresh real chat is seeded with that summary via `build_prompt`. The first exchange is never rolled over — the first-message path accepts ~1M tokens.
4. **Send and stream.** `send_message` drives the upstream SSE; results are parsed, tool calls extracted, and re-emitted in the caller's format.

### Failure handling — the recovery loops

`handle_chat` is retry-recursive (`_attempt`, `_auth_rotated`, `_exclude_token`) and this is the most intricate part of the codebase. On an upstream failure it can: rotate to another token pool entry, re-create the upstream chat, transfer the full conversation history (and re-home files owned by the old token via `_rehome_foreign_files` / `_copy_file_to_token`), and retry once. Locks (`_own_chat_lock`, `_chat_lock`, `_take_lock`) are held across the swap-and-send section specifically to stop a concurrent same-signature request from racing the session replacement — if you restructure this path, preserve the lock ordering or you reintroduce a known race.

`post_with_failover` in `functions.py` provides a second, independent failover axis: a primary upstream base plus an optional `DEEPSEEKER_UPSTREAM_FALLBACK`, tried in order.

Empty/failed upstream responses are classified rather than surfaced raw: `_raise_empty_sse_response`, `_sse_context_limit_hint`, and `_scan_recent_sse_errors` inspect recent SSE lines to distinguish a context overflow (which triggers the rollover/fresh-session path) from a generic upstream error (which becomes a JSON API error via `_api_error_response`).

### Upstream client details

- **Cookie generation.** DeepSeek enforces AWS WAF. The current mechanism uses Android client headers (`DEEPSEEKER_CLIENT_VERSION`) and needs no browser. The Playwright/Chromium path is deprecated but retained as `requirements-waf-backup.in` + `xvfb-run` (see the commented CMD in the Dockerfile); don't remove it without checking with the maintainer.
- **PoW.** `solve_create_pow` runs `wasm/deepseek_pow_solver.wasm` through wasmtime to answer the challenge. The wasm is vendored from a separate repo.
- **Blocking work off the loop.** SQLite helpers are synchronous by design (shared with CLI paths and tests) and are invoked through `_db(...)` = `asyncio.to_thread`. A planned aiosqlite migration is deferred to a "store split" stage. Follow this pattern for any new blocking call in a request path.
- **Model normalization.** There is effectively one upstream model, `v4.1flash`. `resolve_model` accepts legacy aliases (`instant`, `expert`, `vision`, `anthropic/claude-*`) and normalizes everything to it. `anthropic/claude-v4.1flash` exists in `/v1/models` purely for Claude Desktop auto-discovery. Declared `context_window`/`max_output_tokens` reflect *observed* upstream behavior, not documented limits.

### Configuration and defaults

Two defaults are deliberately non-obvious and security-relevant:

- `DEEPSEEKER_API_KEY` unset does **not** fall back to a public default — `_resolve_api_key` generates a `dsk-…` key, prints it once, and persists it to `data_dir()/api_key.txt` (mode 0600). `_log_security_banner` warns about insecure dashboard credentials on boot.
- `HOST` defaults to `127.0.0.1`; the `docker-compose.yml` binding is also loopback-only. Exposing the proxy is an explicit opt-in.

Dashboard auth uses session TTL plus brute-force lockout (5 attempts → 5 min) and a CSRF origin check. `TRUSTED_PROXIES` must be set correctly for `RealIPMiddleware` — an empty value means `X-Forwarded-For` is ignored, which is the safe default.

## Testing conventions

Tests are named by *audit stage / issue number*, not by module: `test_stage0_rails.py` (middleware, trusted proxies, per-chat locks, dual-endpoint failover), `test_token_pool.py` (the v2 scheduler, issue #32), `test_context_rollover.py` (issue #22), and so on. The docstring at the top of each file states which regression it guards and why the previous behavior was wrong — read it before modifying the code it covers, and keep the invariant it documents intact.

Tests mock upstream aggressively and never hit the network. Where they need to adjust thresholds they patch module constants with `mock.patch.object` rather than reloading the module (reload de-syncs references held by other tests). All upstream-facing work must stay mockable — no import-time network calls.

## Note on the domain

This project reverse-proxies a service in violation of that service's terms of use. The README warns to use a throwaway account. Keep that framing in mind when writing user-facing copy, and do not add features whose purpose is to evade detection or rate limits beyond the token-pool/cooldown behavior already documented.
