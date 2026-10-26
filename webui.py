"""Admin UI rendering helpers.

Extracted from app.py so the "render an admin page" concern stops being
copy-pasted across four routes:

* ``render()`` resolves the request language, injects ``t`` / ``lang`` /
  ``langs`` into the template context and calls Jinja. Every admin template
  goes through here, so a page can never silently miss the translator.
* ``gate()`` collapses the repeated ``try: get_current_admin(...) except
  HTTPException: return <meta refresh to /login>`` dance into one call.

Dependency direction: this module imports NOTHING from app.py. The admin
check is passed in by the caller (app.get_current_admin), which keeps the
module a leaf -- app.py imports webui, never the reverse. That is also what
lets it be unit-tested without booting the proxy.
"""

import os

from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import i18n

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# The process-wide Jinja environment. Owned here rather than in app.py so the
# rendering path has a single owner; app.py imports this instance.
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Sent as the Location of a meta-refresh on a failed admin gate. Kept as a
# constant so the call sites cannot drift.
LOGIN_REDIRECT = "/login"


def _redirect_to_login():
    """The admin gate's failure response.

    A meta refresh rather than a 302: these routes are reached from a browser
    form post, and the original code used meta refresh deliberately. Kept
    identical here to preserve that behavior.
    """
    return HTMLResponse(
        f"<meta http-equiv='refresh' content='0;url={LOGIN_REDIRECT}'>"
    )


def gate(request, checker):
    """Run the admin check. Returns None on success, a response on failure.

    ``checker`` is app.get_current_admin. It raises HTTPException for both a
    missing session (401) and a cross-origin request (403); both mean "not an
    admin here", so both redirect to the login page rather than leaking the
    distinction.

    Callers use it as::

        denied = webui.gate(request, get_current_admin)
        if denied:
            return denied
    """
    try:
        checker(request)
    except HTTPException:
        return _redirect_to_login()
    return None


def translate(request, key, **fmt):
    """Translate `key` for the language this request resolves to.

    For the routes that build a message OUTSIDE a template (login errors).
    Without this they would have to re-derive the language and risk drifting
    from what render() chose for the same response."""
    return i18n.translate(i18n.resolve_lang(request), key, **fmt)


# The strings `audit.js` writes into the DOM, keyed by the name the script
# reads them under. Server-rendered text comes straight from `t()` in the
# template; anything a script writes has to be shipped as data instead, or
# the page would be half-translated. The mapping is explicit and lives here
# rather than in the template so the set of strings the script may show is
# auditable in one place -- next to the shape the script expects.
_CLIENT_STRINGS = {
    "loading": "audit.loading",
    "unavailable": "audit.unavailable",
    "disabled": "audit.disabled",
    "empty": "audit.perf_empty",
    "none": "common.none",
    # Read by audit_raw.js. Namespaced with `raw_` so the raw console cannot
    # silently inherit a string meant for the counters page (its "disabled"
    # and "empty" wording is different).
    "raw_loading": "audit_raw.loading",
    "raw_unavailable": "audit_raw.unavailable",
    "raw_disabled": "audit_raw.disabled",
    "raw_empty": "audit_raw.empty",
    "raw_pick": "audit_raw.pick",
    "raw_none": "common.none",
}

# The recovery counters, in the order the panel renders them. The keys are the
# names `audit.js` looks up in `I18N.counters`, NOT the locale keys.
_CLIENT_COUNTERS = {
    "rate_limit": "audit.counter_rate_limit",
    "rate_recover": "audit.counter_rate_recover",
    "failover": "audit.counter_failover",
    "retry": "audit.counter_retry",
    "rollover": "audit.counter_rollover",
}

# The upstream `reason` vocabulary -> the locale key that names it. Keys are
# the raw values stored in the audit table, so the mapping is data, not code:
# an unrecognized reason is shown verbatim by the page, except for the
# `http_4xx` family, which folds into `http_other`.
_CLIENT_REASONS = {
    "http_429": "audit.reason_http_429",
    "sse_rate_limit": "audit.reason_sse_rate_limit",
    "http_other": "audit.reason_http_other",
    "unknown": "audit.reason_unknown",
}


def _translate_all(lang, mapping):
    return {name: i18n.translate(lang, key) for name, key in mapping.items()}


def client_strings(lang):
    """The translation table handed to `audit.js` as JSON.

    The returned shape mirrors what the script reads -- flat keys, plus the
    nested `counters` and `reasons` objects -- so the two cannot drift without
    one of them visibly breaking.

    Returned as plain data (never interpolated into a JS literal) so a quote
    or a backslash in a translation cannot escape into code. The page embeds
    it with Jinja's `tojson`, which also escapes `<`/`>`/`&`."""
    strings = _translate_all(lang, _CLIENT_STRINGS)
    strings["counters"] = _translate_all(lang, _CLIENT_COUNTERS)
    strings["reasons"] = _translate_all(lang, _CLIENT_REASONS)
    return strings


# Token lifecycle states (the raw values stored in the tokens table) -> the
# locale key that names them in the UI. Kept here, next to the other
# value->key mappings, so the display translation is auditable in one place and
# the stored English value never has to change.
_STATUS_KEYS = {
    "ACTIVE": "dashboard.status_active",
    "RATE_LIMITED": "dashboard.status_rate_limited",
}


def status_label(lang):
    """Return a `status_label(raw_status)` bound to `lang`.

    Maps a stored token status to its translated label. An unknown status is
    returned verbatim -- the caller gets the raw English value, never a
    half-rendered ``dashboard.status_xxx`` key.
    """

    def label(status):
        key = _STATUS_KEYS.get(status)
        return i18n.translate(lang, key) if key else status

    return label


def render(request, template_name, status_code=200, **context):
    """Render an admin template with i18n wired in.

    ``lang`` is resolved from the request (query > cookie > Accept-Language >
    default) and ``t`` is the matching translator, so a template only ever
    writes ``{{ t('dashboard.title') }}``. ``js_i18n`` carries the same
    translations for the client, so a page can be translated end to end
    without the script hardcoding English.

    ``status_code`` lets a caller keep its existing status (the login page
    historically returned 200 with an error message rather than 401).
    """
    lang = i18n.resolve_lang(request)
    context.setdefault("lang", lang)
    context.setdefault("t", i18n.translator(lang))
    context.setdefault("langs", i18n.LANGS)
    context.setdefault("lang_name", i18n.lang_name)
    context.setdefault("status_label", status_label(lang))
    context.setdefault("js_i18n", client_strings(lang))
    return templates.TemplateResponse(
        request, template_name, context, status_code=status_code
    )
