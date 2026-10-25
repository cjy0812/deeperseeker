# API reference

Every endpoint DeeperSeeker exposes, with the authentication it requires and the shape of what
comes back. For how the request travels internally see [ARCHITECTURE.md](ARCHITECTURE.md).

Base URL in the examples is `http://127.0.0.1:4000` (the `HOST`/`PORT` defaults).

## Contents

- [Authentication](#authentication)
- [OpenAI-compatible surface](#openai-compatible-surface)
- [Anthropic-compatible surface](#anthropic-compatible-surface)
- [Files](#files)
- [Models](#models)
- [Health](#health)
- [Audit](#audit)
- [Admin UI](#admin-ui)
- [Error format](#error-format)

## Authentication

The credential the client presents is the **bridge's own** `DEEPSEEKER_API_KEY`, never an upstream
DeepSeek credential. Three header spellings are accepted, checked in this order:

| Header | Example |
|---|---|
| `Authorization: Bearer <key>` | `Authorization: Bearer dseeker` |
| `Authorization: <key>` | `Authorization: dseeker` |
| `x-api-key: <key>` | `x-api-key: dseeker` |

The comparison is constant-time (`secrets.compare_digest`). A missing or wrong key yields
`401 {"error": "Invalid API key"}`.

The **admin UI** is a separate authentication realm: a `session_id` cookie obtained from
`POST /login`, bounded by `SESSION_TTL`, with a brute-force lockout (5 attempts -> 5 minutes) and an
Origin/CSRF check on state-changing requests. Admin routes redirect to `/login` when the session is
missing or expired.

Two endpoints accept **either**: `GET /v1/audit/perf` and `GET /v1/audit/rate-limits` take the API
key (for scraping) or a live admin session (for the `/audit` page).

## OpenAI-compatible surface

### `POST /v1/chat/completions`

Chat Completions, streaming and non-streaming.

| Field | Notes |
|---|---|
| `model` | any string; resolved to the single upstream model (see [Models](#models)) |
| `messages` | OpenAI message array; image parts (`image_url` with base64 or URL) are extracted |
| `stream` | `true` for SSE (`data: {...}` chunks, terminated by `data: [DONE]`) |
| `tools` / `tool_choice` | OpenAI function schemas; the reply's text-dialect calls are re-shaped into `tool_calls` |
| `stop`, `max_tokens` | **accepted but ignored** — the upstream web session exposes no stop/length controls |

Non-streaming replies carry `choices[0].message` with either `content` or `tool_calls`, plus a
`usage` block. Streaming replies carry `choices[0].delta`; the terminal chunk sets `finish_reason` to
`stop`, `tool_calls` or `length`.

```bash
curl -s http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $DEEPSEEKER_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4.1-flash","messages":[{"role":"user","content":"hi"}],"stream":false}'
```

### `POST /v1/responses` (alias `POST /responses`)

The OpenAI Responses API shape. Input may be a string or an item array
(`_responses_input_to_messages` normalizes it). `reasoning.effort` is mapped through
`_responses_effort`. The reply is a Responses object (`_build_response_object`), or a stream of
`response.*` events when `stream: true`.

## Anthropic-compatible surface

### `POST /v1/messages` (alias `POST /messages`)

The Anthropic Messages API. This is what Claude Code and Claude Desktop speak.

| Field | Notes |
|---|---|
| `model` | resolved through `resolve_model`; the `anthropic/claude-v4.1flash` alias exists for client discovery |
| `messages` | Anthropic message array; `convert_anthropic_messages` normalizes it inward |
| `system` | string or block array; folded into the upstream prompt |
| `tools` | Anthropic tool schemas; text-dialect calls in the reply become `tool_use` blocks |
| `thinking` | detected by `is_thinking_enabled`; think-tag content is reassembled across chunk boundaries |
| `stream` | `true` for Anthropic SSE: `message_start` -> `content_block_start/delta/stop` -> `message_delta` -> `message_stop` |

`stop_reason` is `end_turn`, `tool_use` or `max_tokens` (the last when the upstream stream was cut
inside a tool argument, and the call was therefore dropped rather than emitted half-written).

```bash
curl -s http://127.0.0.1:4000/v1/messages \
  -H "x-api-key: $DEEPSEEKER_API_KEY" \
  -H 'anthropic-version: 2023-06-01' \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4.1-flash","max_tokens":1024,"messages":[{"role":"user","content":"hi"}]}'
```

## Files

Uploads are pinned to the token that performed them (`record_file`), because upstream files are
account-scoped. Fetching with a scheduler-picked token would 404 whenever that token is not the
owner, so reads prefer the registered owner and only fall back to the scheduler for legacy or
unregistered ids.

### `POST /v1/files` and `POST /v1/files/upload`

`multipart/form-data` with a `file` field. Maximum size 25 MB (`413` above that). Both paths accept
the same body and return the same id; the two response shapes differ by surface:

```json
// /v1/files  (OpenAI shape)
{"id": "file-...", "object": "file", "bytes": 1234, "created_at": 1770000000,
 "filename": "a.png", "purpose": "answers"}

// /v1/files/upload  (Anthropic shape)
{"id": "file-...", "type": "file", "filename": "a.png", "size": 1234,
 "created_at": "2026-10-25T00:00:00Z"}
```

Errors: `400` no file provided, `413` too large, `500` upstream upload failed, `503` no tokens
available.

### `GET /v1/files/{file_id}` and `GET /v1/files/{file_id}/content`

Streams the file bytes back with the upstream content type. `404` when the upstream does not have
it, `502` when the fetch handshake fails.

```bash
curl -s -o out.png http://127.0.0.1:4000/v1/files/file-abc/content \
  -H "Authorization: Bearer $DEEPSEEKER_API_KEY"
```

## Models

### `GET /v1/models` (alias `GET /models`)

Returns one model entry plus the Anthropic-style alias. `context_window` and `max_output_tokens`
reflect **observed** upstream behavior (~1M for a single first message, ~393K of remembered
in-session context before rollover), not a guaranteed upstream limit. The `capabilities` block
advertises image/PDF input, structured outputs, thinking and effort levels so clients can
auto-discover features.

In `DEEPSEEKER_MODE=official` this returns `official_api.OFFICIAL_MODELS` instead.

```bash
curl -s http://127.0.0.1:4000/v1/models -H "Authorization: Bearer $DEEPSEEKER_API_KEY"
```

## Health

### `GET /health`

The shallow check is public and cheap: `200` when at least one token is `ACTIVE`, `503` when none
is. Token presence determines readiness; the WAF cookie file is only reported for backup visibility.

| Query | Auth | Behavior |
|---|---|---|
| *(none)* | none | `{"status": "ok" \| "degraded"}` only |
| *(none)* | API key | adds `active_tokens`, `cookies_valid`, `version` |
| `?deep=1` | **required** | one real end-to-end upstream round trip (PoW challenge -> solve -> chat session create); `401` before any upstream traffic if the key is missing |

```bash
curl http://127.0.0.1:4000/health
curl -H "Authorization: Bearer $DEEPSEEKER_API_KEY" 'http://127.0.0.1:4000/health?deep=1'
```

An authenticated deep probe adds `deep.upstream_reachable`; when that is not `true`, `status` becomes
`degraded` and the HTTP code `503`.

## Audit

Read-only. Both endpoints accept **either** the API key or a live admin session.

### `GET /v1/audit/perf?window=<seconds>`

Default window is 3600 seconds. Returns request volume, error rate, latency percentiles and the
recovery counters over that window, plus `enabled` reflecting whether the audit store is on.

```bash
curl -s 'http://127.0.0.1:4000/v1/audit/perf?window=900' \
  -H "Authorization: Bearer $DEEPSEEKER_API_KEY"
```

### `GET /v1/audit/rate-limits?since=<epoch>`

Admin-gated (token ids and per-token limit counts are operational detail). Returns
`{"tokens": [...], "enabled": bool}` — one row per token limited inside the window, with the reason
breakdown (`http_429` / `sse_rate_limit` / `http_401` / `http_403` / `http_other`) and how many times
it auto-recovered.

```bash
curl -s 'http://127.0.0.1:4000/v1/audit/rate-limits' -b cookies.txt
```

A store failure on either endpoint is `503 {"error": "audit store unavailable"}`, never a `500`.

## Admin UI

HTML routes, authenticated by the `session_id` cookie. State-changing routes also verify the Origin
header against the Host.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/login` | login form |
| `POST` | `/login` | sets `session_id`; 5 failed attempts -> 5 minute lockout |
| `GET` | `/logout` | clears the session |
| `GET` | `/lang/{code}` | switches UI language, persists in the `ds_lang` cookie |
| `GET` | `/audit` | audit & perf page |
| `GET` | `/dashboard` | token pool, usage by day x key, file stats |
| `GET` | `/` | redirects to the dashboard |
| `POST` | `/tokens/add` | form fields `auth_token`, `alias` |
| `POST` | `/tokens/{token_id}/delete` | removes a token |
| `POST` | `/tokens/{token_id}/restore` | the **only** exit from `QUARANTINED` |

All admin POSTs answer with a meta-refresh redirect back to `/dashboard` (or `/login` when not
authenticated), so they work from a plain HTML form without JavaScript.

## Error format

Errors are built in one place (`errors.py`) and always carry the correlation id.

**OpenAI surface**

```json
{"error": {"message": "...", "type": "api_error", "code": 502, "request_id": "6f1c..."}}
```

`type` is `rate_limit_error` when the status is 429, otherwise `api_error`.

**Anthropic surface**

```json
{"type": "error", "error": {"type": "api_error", "message": "... [trace: 6f1c...]"}}
```

The trace id goes inside the message text because Anthropic error objects have no extra-field
contract. Both surfaces set the `X-Request-ID` header.

Status mapping: the typed upstream status when there is one, `502` for connection-level failures, and
`503` when WAF cookies cannot be generated. Every message passes `_redact_secret_material`, so long
hex/base64 runs and `Bearer` schemes are replaced with `[redacted]` before reaching a client — even
when the text came out of an upstream SSE error event.
