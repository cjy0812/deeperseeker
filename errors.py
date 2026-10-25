"""Client-visible error envelopes and the credential scrub they pass through.

Every upstream failure that reaches a client is built here: the typed status
from UpstreamError (B6), the secret scrub (P0-4), and the trace id that ties a
response to the access log and the pool view. Split out of app.py so the
error-shape contract has one home instead of being spread across the route
handlers.
"""

import re
import uuid

from fastapi.responses import JSONResponse

from functions import CookieGenerationError
from middleware import request_id_var


def _upstream_http_code(exc):
    """HTTP status carried by an upstream failure (B6).

    Typed UpstreamError exposes .status directly; anything else (connection-
    level failures, cookie generation) maps to None and callers fall back to
    502. The old str(e) regex parsing of the 'HTTP (\\d{3}):' prefix is gone —
    any upstream wording change could silently disable token rotation and
    rate-limit marking."""
    status = getattr(exc, "status", None)
    if isinstance(status, int) and 400 <= status <= 599:
        return status
    return None


def _rate_limit_reason(exc):
    """The audit vocabulary for WHY a token was marked RATE_LIMITED.

    A real HTTP 429 and a `rate_limit_reached` event inside an otherwise-200
    SSE body both reach the callers below as status 429; only
    UpstreamError.origin tells them apart, and audit.token_rate_limit_summary()
    keys its breakdown on exactly these strings. Non-429 statuses keep the
    plain `http_<code>` shape that view also matches on (http_4%).
    """
    code = _upstream_http_code(exc)
    origin = getattr(exc, "origin", "http")
    if code == 429 and origin == "sse":
        return "sse_rate_limit"
    return f"http_{code}" if code else None


def _api_error_response(e, is_anthropic=False):
    code = _upstream_http_code(e) or 502
    if isinstance(e, CookieGenerationError):
        code = 503  # WAF cookies cannot be produced right now — upstream unreachable, not a client error
    if code < 400 or code > 599:
        code = 502
    # P0-7: official-mode error bodies carry upstream text too — every
    # client-visible error body passes the secret scrub, one discipline.
    message = _redact_secret_material(str(e)[:500])
    if is_anthropic:
        payload = {
            "type": "error",
            "error": {"type": "api_error", "message": message},
        }
    else:
        err_type = "rate_limit_error" if code == 429 else "api_error"
        payload = {"error": {"message": message, "type": err_type, "code": code}}
    return JSONResponse(payload, status_code=code)


# P0-4: credential-shaped material must never reach a client-visible error
# body, even when the text came out of an upstream SSE error event. Scrub the
# shapes DeepSeek traffic actually carries — long hex/base64 runs (session and
# message ids, token material) and Bearer schemes — before the terminal
# envelope is built.
_SECRET_RUN_RE = re.compile(r"[A-Za-z0-9+/_=-]{32,}")
_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._+=/-]+")


def _redact_secret_material(text):
    scrubbed = _BEARER_RE.sub("[redacted]", str(text))
    scrubbed = _SECRET_RUN_RE.sub("[redacted]", scrubbed)
    return scrubbed


def _terminal_error_response(e, is_anthropic=False):
    """P0-4 (Day 13): the budget-exhausted exit returns a DESIGNED envelope.

    When every pooled token failed — the #33 terminal empty-SSE class — the
    client used to get whatever str(exception) happened to carry: no trace id,
    no status contract, upstream text verbatim. This exit reuses the typed
    UpstreamError machinery (B6) for the status, passes the message through
    the secret scrub above, and stamps the request's correlation id so the
    response, the access log and the pool view tell one story. There is no
    second error path: _api_error_response still owns the mid-retry shapes.

    The trace id travels in the OpenAI body as request_id, and inside the
    Anthropic message text (their error objects have no extra-field
    contract); the X-Request-ID header rides along on both regardless.
    """
    code = _upstream_http_code(e) or 502
    if isinstance(e, CookieGenerationError):
        code = 503
    if code < 400 or code > 599:
        code = 502
    trace = request_id_var.get()
    if trace == "-":
        trace = uuid.uuid4().hex  # direct invocation path (tests, scripts)
    message = _redact_secret_material(str(e)[:500])
    if is_anthropic:
        payload = {
            "type": "error",
            "error": {
                "type": "api_error",
                "message": f"{message} [trace: {trace}]",
            },
        }
    else:
        err_type = "rate_limit_error" if code == 429 else "api_error"
        payload = {
            "error": {
                "message": message,
                "type": err_type,
                "code": code,
                "request_id": trace,
            }
        }
    return JSONResponse(payload, status_code=code)
