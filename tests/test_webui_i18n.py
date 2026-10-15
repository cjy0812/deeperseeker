"""Regression tests for the admin-UI i18n layer and the shared render helpers.

What these guard:

* Language resolution order is query > cookie > Accept-Language > default.
  A reordering silently ships the wrong language to every visitor.
* An unvalidated ``?lang=`` / ``ds_lang`` value must never reach the loader's
  file path: ``_norm`` reduces to the primary subtag and rejects anything not
  in LANGS. A regression here is a path-traversal primitive.
* A missing translation key falls back (requested -> en -> key itself) and
  never raises. A KeyError in a template is a 500 on the login page.
* The two dictionaries have IDENTICAL key sets, so adding an English string
  without its Chinese counterpart fails CI instead of shipping a half
  translated page.
* ``webui.gate`` returns a redirect response for a non-admin and None for an
  admin, and ``webui.render`` injects ``t``/``lang`` into every context.

Run:  python tests/test_webui_i18n.py   (pytest-compatible)
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import i18n  # noqa: E402
import webui  # noqa: E402
from i18n import loader  # noqa: E402


class _Req:
    """Minimal stand-in for a Starlette Request.

    Covers the two things the admin pages read off the request: the language
    inputs (query / cookies / headers) that resolve_lang consults, and ``url``,
    which the shared layout uses to build the language switcher's ``next``
    target.
    """

    def __init__(
        self, query=None, cookies=None, headers=None, path="/login", url_query=""
    ):
        self.query_params = query or {}
        self.cookies = cookies or {}
        self.headers = headers or {}
        self.url = SimpleNamespace(path=path, query=url_query)


# ==============================================================================
# resolve_lang
# ==============================================================================


def test_resolve_lang_defaults_to_en():
    assert i18n.resolve_lang(_Req()) == "en"


def test_query_beats_cookie_and_header():
    r = _Req(
        query={"lang": "zh"},
        cookies={i18n.LANG_COOKIE: "en"},
        headers={"accept-language": "en-US,en;q=0.9"},
    )
    assert i18n.resolve_lang(r) == "zh"


def test_cookie_beats_header():
    r = _Req(
        cookies={i18n.LANG_COOKIE: "zh"},
        headers={"accept-language": "en-US,en;q=0.9"},
    )
    assert i18n.resolve_lang(r) == "zh"


def test_header_used_when_no_query_or_cookie():
    r = _Req(headers={"accept-language": "zh-CN,zh;q=0.9,en;q=0.8"})
    assert i18n.resolve_lang(r) == "zh"


def test_header_qvalues_are_ordered_not_positional():
    """'en;q=0.5, zh;q=0.9' must pick zh: the first entry is not the best."""
    r = _Req(headers={"accept-language": "en;q=0.5, zh;q=0.9"})
    assert i18n.resolve_lang(r) == "zh"


def test_header_unknown_language_falls_back_to_default():
    r = _Req(headers={"accept-language": "fr-FR,fr;q=0.9"})
    assert i18n.resolve_lang(r) == "en"


@pytest.mark.parametrize("bad", ["../../etc/passwd", "zz", "", "en/../zh", "ZH-hant"])
def test_bad_lang_values_are_rejected_not_passed_through(bad):
    """Only exact LANGS members survive; everything else is dropped.

    ZH-hant is deliberately in here: it is a REAL tag but not one we ship,
    so it must reduce to a rejection, not to a file path.
    """
    r = _Req(query={"lang": bad})
    assert i18n.resolve_lang(r) == "en", bad


def test_lang_is_case_and_separator_insensitive():
    assert i18n.resolve_lang(_Req(query={"lang": "ZH"})) == "zh"
    assert i18n.resolve_lang(_Req(query={"lang": "zh_CN"})) == "zh"


# ==============================================================================
# translator
# ==============================================================================


def test_translate_returns_language_specific_text():
    assert i18n.translate("en", "nav.audit") == "Audit"
    assert i18n.translate("zh", "nav.audit") == "审计"


def test_missing_key_falls_back_to_the_key_itself_and_does_not_raise():
    assert i18n.translate("zh", "definitely.not.a.key") == "definitely.not.a.key"


def test_missing_key_in_non_default_language_falls_back_to_default():
    """A key present only in en still resolves when rendering zh."""
    key = "test.only_in_default"
    loader._cache.setdefault("en", {})[key] = "default text"
    try:
        assert i18n.translate("zh", key) == "default text"
    finally:
        loader._cache.get("en", {}).pop(key, None)


def test_format_arguments_are_applied():
    assert "3" in i18n.translate("en", "dashboard.tokens", n=3)


def test_bad_format_placeholder_does_not_raise():
    """A template that forgets an argument renders the raw string, not a 500."""
    out = i18n.translate("en", "dashboard.tokens")
    assert isinstance(out, str)


def test_unknown_locale_file_degrades_to_keys(monkeypatch):
    """A corrupt/absent locale must not break rendering."""
    monkeypatch.setitem(loader._cache, "en", {})
    assert i18n.translate("en", "nav.audit") == "nav.audit"


# ==============================================================================
# dictionary parity
# ==============================================================================


def test_both_dictionaries_have_identical_key_sets():
    en = loader._load("en")
    zh = loader._load("zh")
    assert set(en) == set(zh), {
        "missing_in_zh": sorted(set(en) - set(zh)),
        "extra_in_zh": sorted(set(zh) - set(en)),
    }


def test_no_empty_translation_values():
    for lang in i18n.LANGS:
        for key, value in loader._load(lang).items():
            assert value.strip(), f"{lang}:{key} is empty"


# ==============================================================================
# webui.gate / webui.render
# ==============================================================================


def _admin_checker_ok(request):
    return "admin"


def _admin_checker_denied(request):
    raise HTTPException(status_code=401)


def test_gate_returns_none_for_admin():
    assert webui.gate(_Req(), _admin_checker_ok) is None


def test_gate_returns_redirect_for_non_admin():
    resp = webui.gate(_Req(), _admin_checker_denied)
    assert resp is not None
    assert webui.LOGIN_REDIRECT in resp.body.decode()


def test_render_injects_translator_and_lang():
    resp = webui.render(_Req(query={"lang": "zh"}), "login.html", error=None)
    body = resp.body.decode("utf-8")
    assert resp.status_code == 200
    # The zh login heading proves both the lookup and the language choice.
    assert "登录" in body


def test_render_honours_status_code():
    resp = webui.render(_Req(), "login.html", status_code=403, error=None)
    assert resp.status_code == 403


def main():
    import subprocess

    return subprocess.call([sys.executable, "-m", "pytest", "-q", __file__])


if __name__ == "__main__":
    sys.exit(main())
