"""Tiny Korean/English switch.

Usage: ``L("한국어 문장", "English sentence")`` returns the string for the active language.
Language order: ``TOKENALARM_LANG`` env → ``set_lang()`` (from config ``language``) → OS UI language.
Keep this module import-light: the status line fast path uses it.
"""
from __future__ import annotations

import locale
import os
import sys

_lang: str | None = None


def detect() -> str:
    """Return "ko" if the OS UI language is Korean, else "en"."""
    if sys.platform == "win32":
        try:
            import ctypes

            langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            return "ko" if (langid & 0x3FF) == 0x12 else "en"  # LANG_KOREAN
        except Exception:
            pass
    for var in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        v = os.environ.get(var)
        if v:
            return "ko" if v.lower().startswith("ko") else "en"
    try:
        loc = locale.getlocale()[0] or ""
    except Exception:
        loc = ""
    return "ko" if loc.lower().startswith(("ko", "korean")) else "en"


def _normalise(code: str | None) -> str | None:
    if not code:
        return None
    c = str(code).strip().lower()
    if c in ("auto", ""):
        return None
    return "ko" if c.startswith("ko") else "en"


def set_lang(code: str | None) -> None:
    """Set from config (``auto``/``None`` → detect). The env var still wins."""
    global _lang
    _lang = _normalise(code) or detect()


def lang() -> str:
    env = _normalise(os.environ.get("TOKENALARM_LANG"))
    if env:
        return env
    global _lang
    if _lang is None:
        _lang = detect()
    return _lang


def L(ko: str, en: str) -> str:
    return ko if lang() == "ko" else en


def reset() -> None:
    """For tests."""
    global _lang
    _lang = None
