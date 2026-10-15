"""Server-side i18n for the admin UI.

Mechanism: a flat-key JSON dictionary per language, rendered by Jinja, with
the chosen language persisted in a cookie. No client-side framework, no build
step, no flash of the wrong language — the server emits the right text on the
first byte.

Public API:
    LANGS, DEFAULT_LANG, LANG_COOKIE          constants
    resolve_lang(request) -> str              pick the language for a request
    translator(lang) -> t(key, **fmt)         the Jinja-callable lookup

This package imports nothing else in the project (same rule as audit.py), so
it can be unit-tested and reused without dragging in the proxy.
"""

from .loader import DEFAULT_LANG, LANGS, lang_name, translate  # noqa: F401

# The cookie the language switcher writes. Namespaced so it cannot collide
# with the admin session cookie.
LANG_COOKIE = "ds_lang"


def _norm(code):
    """Normalize a language tag to one of LANGS, or None.

    Accepts full Accept-Language style tags ("zh-CN", "en-US;q=0.9") and
    reduces them to the primary subtag. Anything not in LANGS is rejected
    rather than passed through — the cookie and query parameter are
    attacker-controlled, and an unvalidated value must never reach a file
    path in the loader.
    """
    if not code:
        return None
    parts = str(code).strip().lower().replace("_", "-").split("-")
    primary = parts[0]
    if primary not in LANGS:
        return None
    # A script subtag ("zh-Hant", "zh-Hans") selects a written variant, not
    # just a regional flavour, and we ship only one Chinese dictionary. A
    # region subtag ("zh-CN", "en-GB") is a safe reduction to the base
    # language; a script is not, so drop the whole tag and let the caller fall
    # back to DEFAULT_LANG rather than serve the wrong script silently.
    if len(parts) > 1 and len(parts[1]) == 4 and parts[1].isalpha():
        return None
    return primary


def _from_accept_language(header):
    """First LANGS member mentioned in an Accept-Language header, if any.

    Parses the q-value list properly (order by q, descending) instead of
    taking the first entry, which is wrong for "en;q=0.5, zh;q=0.9".
    """
    if not header:
        return None
    candidates = []
    for i, part in enumerate(header.split(",")):
        part = part.strip()
        if not part:
            continue
        bits = part.split(";")
        tag = bits[0].strip()
        q = 1.0
        for b in bits[1:]:
            b = b.strip()
            if b.startswith("q="):
                try:
                    q = float(b[2:])
                except ValueError:
                    q = 0.0
        candidates.append((q, i, tag))
    for _q, _i, tag in sorted(candidates, key=lambda c: (-c[0], c[1])):
        norm = _norm(tag)
        if norm:
            return norm
    return None


def resolve_lang(request):
    """Decide which language to render for this request.

    Priority: explicit ?lang= (the switcher link) > the ds_lang cookie (the
    remembered choice) > Accept-Language > DEFAULT_LANG.
    """
    lang = _norm(request.query_params.get("lang"))
    if lang:
        return lang
    lang = _norm(request.cookies.get(LANG_COOKIE))
    if lang:
        return lang
    lang = _from_accept_language(request.headers.get("accept-language"))
    if lang:
        return lang
    return DEFAULT_LANG


def translator(lang):
    """Return a `t(key, **fmt)` bound to `lang`, for injection into templates."""

    def t(key, **fmt):
        return translate(lang, key, **fmt)

    return t