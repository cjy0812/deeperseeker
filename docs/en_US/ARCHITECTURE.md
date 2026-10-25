# Architecture

DeeperSeeker is a reverse proxy that puts an **OpenAI- and Anthropic-shaped API** in front of a
**DeepSeek web chat session**. Clients (Claude Code, Cline, the OpenAI SDK, Claude Desktop) speak
their normal protocol; the proxy drives a browser-equivalent upstream session, then translates the
reply back.

This document describes how a request travels through the process, what each module owns, and the
invariants that keep the pool healthy. For the wire-level endpoint reference see
[API.md](API.md); for configuration see the `Configuration` table in the [README](../../README.md).

## Contents

- [Shape of the process](#shape-of-the-process)
- [Request lifecycle](#request-lifecycle)
- [Module map](#module-map)
- [Token pool and its state machine](#token-pool-and-its-state-machine)
- [Context: sessions and rollover](#context-sessions-and-rollover)
- [Streaming, tool calls and truncation](#streaming-tool-calls-and-truncation)
- [Errors, correlation and redaction](#errors-correlation-and-redaction)
- [Audit and observability](#audit-and-observability)

## Shape of the process

One ASGI application (`app.py`) served by uvicorn. There is no worker pool and no external queue:
in-flight counters, the session LRU and the cooldown timers are **per-process** state, so a
multi-worker deployment would see each worker report only its own numbers.

Persistent state lives in a single SQLite database under the data directory (`DB_PATH`, default
`data/deeperseeker.db`) plus per-day JSONL audit files (`data/audit/audit-YYYY-MM-DD.jsonl`).
SQLite calls are synchronous and are therefore run off the event loop through `app._db`, a thin
`asyncio.to_thread` wrapper — every route that touches the store awaits `_db(...)` rather than
calling `functions.*` directly.

Import-time state is deliberately minimal: paths resolve from `BASE_DIR` rather than a global
`os.chdir`, and the banner, database schema and API-key file are all initialized inside the
`lifespan` hook, so importing the module (tests, embedding) has no side effects.

## Request lifecycle

```
client
  |
  v
RecovererMiddleware      last-resort exception barrier -> logged JSON 500 with X-Request-ID
  v
RequestIDMiddleware      per-request correlation id (request_id_var) + access log line
  v
RealIPMiddleware         real client IP behind a trusted proxy; fail-closed otherwise
  v
route handler            check_key() -> token slot -> session lookup -> upstream SSE
  v
upstream (DeepSeek web)  signed request (PoW where required) returning text/event-stream
  v
DSML / XML / JSON parse  dsml.StreamToolParser or dsml.parse_tools()
  v
protocol re-shaping      formats.py -> OpenAI chunk / Anthropic event / Responses object
  v
client
```

Middleware order is registered as RealIP -> RequestID -> Recoverer, which Starlette executes in
reverse: **Recoverer runs first (outermost)**, so anything raised below it is converted into a
proper 500 that still carries the correlation header.

Every handler that serves model traffic starts with the same three steps:

1. `check_key(request)` — the bridge's own `DEEPSEEKER_API_KEY`, not the upstream credential.
2. Acquire a **token slot** (`functions.acquire_token_slot`) so the pool's in-flight count for that
   token reflects real concurrency; the slot is released in a `finally`.
3. Resolve or create an upstream **session** for the conversation signature
   (`functions.find_session` / `save_session`).

## Module map

| Module | Lines | Owns |
|---|---|---|
| `app.py` | ~3200 | FastAPI app, all public routes, the four large streaming handlers (`handle_chat`, `stream_response`, `stream_anthropic_response`, `stream_responses_response`), admin pages, `main()` |
| `functions.py` | ~1580 | Token CRUD and the scheduler, session store, file-ownership map, upstream HTTP + PoW signing, SSE reading, usage recording, summarization |
| `dsml.py` | ~840 | The upstream tool-call dialect: `parse_tools()` for a full body, `StreamToolParser` for incremental chunks, truncation detection, name repair |
| `formats.py` | ~680 | Protocol conversion: Anthropic messages in/out, OpenAI Responses objects, model resolution, thinking/effort detection, token estimation helpers |
| `audit.py` | ~620 | Append-only JSONL event store plus read-time aggregation (`perf_summary`, `token_rate_limit_summary`) |
| `plugin_helper.py` | ~700 | The tool-use instruction block injected into upstream prompts so the model emits tool calls in a parseable dialect |
| `middleware.py` | ~240 | The three pure-ASGI middlewares described above |
| `official_api.py` | ~210 | `DEEPSEEKER_MODE=official` passthrough to `api.deepseek.com` |
| `webui.py` | ~140 | Admin page rendering: language resolution, Jinja context, session gate |
| `errors.py` | ~106 | Client-visible error envelopes and the credential scrub they pass through |
| `i18n/` | — | Flat-key JSON dictionaries per language, cookie-persisted selection |
| `routes/audit_api.py` | ~60 | The two read-only audit endpoints, built as a router with injected dependencies |

Two dependency rules keep this from turning into a tangle:

* **`audit.py`, `i18n/`, `webui.py`, `errors.py`, `formats.py`, `dsml.py` import nothing from
  `app.py`.** They are leaves — testable without booting the proxy.
* **`routes/` never imports `app`.** `build_router(...)` receives `admin_or_key`,
  `get_current_admin`, `run_db` and `logger` from the host module, because `app.py` imports the
  router and the reverse direction would be circular.

`app.py` still re-exports the public names of `errors.py` and `formats.py`. That is deliberate:
existing tests and downstream importers use `app` as the facade (`from app import SINGLE_MODEL`),
and many tests patch attributes **on `app`'s globals**, which only works if the name is looked up
there. When moving a function out of `app.py`, check whether a test patches it via
`patch.object(app_module, "...")` first — the standard rule is *patch where it is looked up*.

## Token pool and its state machine

A "token" is one DeepSeek web account credential. The pool rotates among the tokens you added; it
never provisions one.

| State | Meaning | Leaves it via |
|---|---|---|
| `ACTIVE` | usable, in rotation | an upstream 401/403/429 marks it limited |
| `RATE_LIMITED` | cooling down for `DEEPSEEKER_RATE_LIMIT_COOLDOWN` seconds | automatic, when the cooldown expires |
| `QUARANTINED` | credential rejected — out of rotation | manual **Restore** on the dashboard only |

The mapping from upstream status to state (`functions.mark_limited`):

| Upstream signal | Effect |
|---|---|
| `429` (HTTP, or a `rate_limit_reached` event inside an otherwise-200 SSE body) | cooldown; the audit `reason` distinguishes `http_429` from `sse_rate_limit` |
| `403` below the escalation threshold | cooldown, and one 403 is added to the streak counter |
| `403` reaching `DEEPSEEKER_QUARANTINE_AFTER_403` with no successful turn in between | `QUARANTINED` |
| `401` | `QUARANTINED` immediately — a rejected credential is not a rate limit, and re-arming it every minute is how accounts get banned |

A successful turn clears the 403 streak. A stale in-flight success **cannot** lift a quarantine.

On a limiting status the broken web session is discarded and the turn is retried on a *different*
token with jittered backoff — up to `DEEPSEEKER_MAX_UPSTREAM_ATTEMPTS` — with full history
injection, and account-scoped files are re-homed onto that token first. Per-token concurrency is
capped by `TOKEN_CONCURRENCY`; slots are acquired with `acquire_token_slot` and always released in a
`finally`, including on the file-upload and file-download paths.

The pool view (`get_pool_view`) exposes each token's state, in-flight count, last use and remaining
cooldown **by alias, with no credential material**.

## Context: sessions and rollover

Upstream context belongs to the **chat session**, not to the token. The proxy therefore keeps a
signature -> `(token_id, session_id, parent_message_id)` map in SQLite:

* `find_session(sig)` returns a remembered session for the conversation signature, preferring one
  whose token is still usable.
* `save_session(...)` records the session after a successful turn; `next_parent(...)` maintains the
  message chain the upstream expects.
* `prune_sessions()` bounds the table by `MAX_SESSIONS`, evicting least-recently-used rows;
  `PRUNE_EVERY` amortizes the sweep instead of paying it on every write. `MAX_SIG_LOCKS` bounds the
  in-process lock table that serializes concurrent turns on the same signature.
* If the signature is unknown, or the account fails over, the full conversation history is injected
  into a fresh chat.

**Rollover.** Remembered in-session context reaches roughly `DEEPSEEKER_MEMORY_LIMIT_TOKENS`
(~393K) before the proxy summarizes the conversation with the model and continues in a fresh chat
seeded with that handoff summary, keeping `ROLLOVER_SAFETY_TOKENS` of headroom and capping the
summary at `MAX_SUMMARY_TOKENS`. The estimate counts tool schemas and prompt overhead
(`DEEPSEEKER_PROMPT_OVERHEAD_TOKENS`) so the proactive rollover fires *before* the session dies.

Two escape hatches matter for agent harnesses:

* A **context-class failure** (upstream context wording, or an empty SSE body carrying a context
  hint) forces a summarize-and-rollover on the retry instead of re-sending the same oversized
  context, which is how a harness replaying its whole history would otherwise fail identically on
  every attempt.
* The **latest user message** is hard-capped at `DEEPSEEKER_MAX_USER_MESSAGE_TOKENS` (tail kept), so
  a first message above the ~1M upstream ceiling passes instead of failing.

The first exchange is never rolled over — the first-message path accepts up to ~1M tokens.
`/v1/models` declares `context_window` and `max_output_tokens` from this **observed** behavior, not
from a guaranteed upstream limit.

## Streaming, tool calls and truncation

The upstream returns `text/event-stream` whose payload is plain text, not structured tool calls.
Tool calls arrive as XML-ish blocks (`DSML` markers, `parameter` tag blocks, or bare JSON),
sometimes with full-width `｜｜` (U+FF5C) bars where ASCII `||` was expected.

* `dsml.parse_tools(text)` handles a complete body: it strips stray markers and wrapper tags,
  separates prose from call blocks, parses parameters, repairs hallucinated tool names against the
  declared schemas, and returns `(clean_text, tool_calls)`.
* `dsml.StreamToolParser` handles incremental chunks: it buffers only as much as it needs to decide
  whether a prefix is plausible (`_is_plausible_stream_entry_prefix`), so think-tag and call-block
  boundaries split across network chunks are reassembled rather than emitted half-written.
* **Truncation is judged narrowly.** A missing outer closing tag is normal model behavior and is
  *not* treated as truncation. Only an unclosed final `parameter` tag or a JSON brace that was cut
  mid-object means the stream ended inside an argument (`tool_block_is_truncated`). When that
  happens the call is not emitted at all — emitting a half-written call is worse than emitting none,
  because the client will try to execute it. The reply then ends with `finish_reason: "length"`
  (OpenAI) / `stop_reason: "max_tokens"` (Anthropic) so the harness knows to retry.

Re-shaping is `formats.py`'s job: `convert_anthropic_messages` and `_responses_input_to_messages`
normalize requests inward, `format_anthropic_response`, `format_responses_response` and the
per-chunk builders normalize replies outward. Anthropic streams keep `content_block_start/delta/stop`
with a monotonic `index`; OpenAI streams keep `choices[0].delta` with a single terminal
`finish_reason`.

## Errors, correlation and redaction

Every client-visible failure is built in `errors.py`; there is no second error path.

* `_upstream_http_code(exc)` reads the typed `.status` off `UpstreamError` (or returns `None` for
  connection-level failures and cookie generation, which fall back to `502`). The old approach —
  regex-parsing the `HTTP (\d{3}):` prefix out of `str(e)` — is gone, because any upstream wording
  change could silently disable token rotation and rate-limit marking.
* `_api_error_response(e, is_anthropic)` owns the mid-retry shapes; `_terminal_error_response(e, ...)`
  owns the budget-exhausted exit (every pooled token failed). The terminal exit reuses the same typed
  status machinery and stamps the correlation id: `error.request_id` on the OpenAI surface, a
  `[trace: …]` suffix in the message on the Anthropic surface (their error objects have no extra-field
  contract). `X-Request-ID` rides along on both.
* `_redact_secret_material(text)` scrubs long hex/base64 runs and `Bearer` schemes out of every
  message before it reaches a response body — including text that came out of an upstream SSE error
  event. `CookieGenerationError` maps to `503`, because the upstream being unreachable is not a
  client error.

The correlation id comes from `RequestIDMiddleware` via the `request_id_var` context variable, so
the response body, the access log line and the dashboard's pool view all tell one story.

## Audit and observability

`audit.py` is standalone (it imports nothing from the project) and appends one JSON object per line
to `data/audit/audit-YYYY-MM-DD.jsonl`, one file per **UTC** day (the filename date must be UTC
because `ts` is epoch seconds; a local date would misfile cross-midnight rows). An append is a single
locked `write()` with no fsync — `record()` already runs off the event loop, and the lock is what
keeps a large detail field from interleaving.

What gets recorded: every client request, every rate-limit hit (with the `reason` vocabulary
`http_429` / `sse_rate_limit` / `http_401` / `http_403`), every cooldown auto-recovery, every
failover, and per-turn usage. Retention is bounded by `DEEPSEEKER_AUDIT_ENABLED`,
`DEEPSEEKER_AUDIT_RETENTION_DAYS` and `DEEPSEEKER_AUDIT_MAX_ROWS`.

Read paths:

* `audit.perf_summary(window)` — request volume, error rate, latency percentiles and recovery
  counters over the trailing window; served at `GET /v1/audit/perf`.
* `audit.token_rate_limit_summary(since)` — one row per limited token with the reason breakdown and
  auto-recovery count; served at `GET /v1/audit/rate-limits`.
* Usage aggregation (`usage_by_day_key`) — requests, input/output tokens and a flat-tariff cost
  estimate per day x per masked key alias, rendered on the dashboard. One row per *completed* turn,
  so failed attempts and streamed retries never double-count.

Both audit endpoints are best-effort: a store failure is a `503` ("audit store unavailable"), never a
`500`, because the data is diagnostic.
