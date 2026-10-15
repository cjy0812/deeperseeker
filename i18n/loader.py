"""Locale dictionary loading and lookup.

Deliberately imports nothing else in the project (the same decoupling rule
audit.py follows): the web UI must be renderable even when the proxy modules
fail to import, and this stays trivially unit-testable.

Dictionaries are flat JSON objects with dotted keys ("dashboard.title"), not
nested objects — lookup is then a plain dict hit, and there is no
getattr-style walking code to get wrong when a key is misspelled.
"""

import json
import os
import sys
import threading

LOCALES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales")

# The default/fallback language. Every key must exist here (a test enforces
# that the two dictionaries have identical key sets), so `_load` can treat a
# miss in the default dictionary as a genuine authoring bug.
DEFAULT_LANG = "en"

LANGS = ("en", "zh")

_LANG_NAMES = {"en": "English", "zh": "中文"}

_cache = {}
_lock = threading.Lock()


def lang_name(code):
    """Human label for a language code, for the switcher links."""
    return _LANG_NAMES.get(code, code)


def _load(lang):
    """Load one locale dictionary, memoized for the process lifetime.

    A missing or malformed file is NOT fatal: the UI falls back to the default
    language and then to raw keys. Returning an empty dict here means every
    lookup misses, which is exactly the fallback path.
    """
    with _lock:
        if lang in _cache:
            return _cache[lang]
    path = os.path.join(LOCALES_DIR, f"{lang}.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"{path}: expected a JSON object")
    except Exception as e:  # noqa: BLE001 - a bad locale must not break the UI
        print(f"[i18n] cannot load locale {lang!r}: {e}", file=sys.stderr)
        data = {}
    with _lock:
        _cache[lang] = data
    return data


def translate(lang, key, **fmt):
    """Resolve `key` for `lang` and apply `str.format` with `fmt`.

    Fallback chain: requested language -> DEFAULT_LANG -> the key itself.
    Missing keys never raise: a half-translated page is a cosmetic defect,
    a 500 on the login page is an outage.

    Formatting is best-effort too — a template with an unknown placeholder
    renders verbatim rather than throwing.
    """
    text = _load(lang).get(key)
    if text is None and lang != DEFAULT_LANG:
        text = _load(DEFAULT_LANG).get(key)
    if text is None:
        print(f"[i18n] missing key {key!r} ({lang})", file=sys.stderr)
        text = key
    if fmt:
        try:
            return text.format(**fmt)
        except (KeyError, IndexError, ValueError) as e:
            print(f"[i18n] bad format for {key!r}: {e}", file=sys.stderr)
            return text
    return text