"""Backend (daemon, CLI, API) message translation, gettext-style.

The source text of every message is English: ``tr("device {0!r} not found", sid)``.
A catalog maps the English text to its translation; messages missing from the
catalog are returned unchanged (English). Catalogs are flat JSON files
``messages/<code>.json`` ({"English text": "translation"}); the translation keeps
the same ``{0}``/``%s`` placeholders. The user may add or override messages in
``<config dir>/messages/<code>.json``.

The language is daemon-wide (``language`` in daemon.json; "auto" = English).
The web page language is chosen separately in the browser (see web/locales.py).
"""
from __future__ import annotations

import json
import logging
import os
import re
from contextvars import ContextVar
from pathlib import Path
from typing import Dict, Optional

log = logging.getLogger(__name__)

BUILTIN_DIR = Path(__file__).resolve().parent / "messages"
_CODE_RE = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})?$")

_catalog: Dict[str, str] = {}
_language = "en"
_user_dir: Optional[Path] = None
_by_lang: Dict[str, Dict[str, str]] = {}
_ctx_lang: ContextVar[Optional[str]] = ContextVar("gamepad_remapd_lang", default=None)


def detect_system_language() -> str:
    """Language code from the environment (LC_ALL, LC_MESSAGES, LANG) or the OS locale; "en" if unknown."""
    for var in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        value = os.environ.get(var, "").split(":")[0].split(".")[0].strip()
        if value and value not in ("C", "POSIX"):
            return value.replace("_", "-")
    try:
        import locale
        value = (locale.getlocale()[0] or "").strip()
        if value and value not in ("C", "POSIX"):
            return value.replace("_", "-")
    except Exception:
        pass
    return "en"


def _candidates(code: str):
    """'uk-UA' -> ['uk-UA', 'uk']."""
    code = code.replace("_", "-")
    yield code
    if "-" in code:
        yield code.split("-", 1)[0]


def _read(path: Path) -> Dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning("messages %s: %s", path, e)
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str) and not k.startswith("_")}


def set_language(code: Optional[str] = "auto", user_dir: Optional[Path] = None) -> str:
    """Load the catalog for ``code`` ("auto" = English). Returns the language actually used."""
    global _catalog, _language, _user_dir
    _user_dir = Path(user_dir) if user_dir else None
    _by_lang.clear()
    if not code or code == "auto":
        code = "en"            # the daemon, CLI and log stay English unless a language is set explicitly
    code = str(code)
    if not _CODE_RE.match(code):
        code = "en"
    catalog: Dict[str, str] = {}
    used = "en"
    for cand in _candidates(code):
        found = _read(BUILTIN_DIR / f"{cand}.json")
        user = _read(Path(user_dir) / f"{cand}.json") if user_dir else {}
        if found or user:
            catalog = {**found, **user}
            used = cand
            break
    _catalog, _language = catalog, used
    return used


def get_language() -> str:
    return _language


def use_language(code: Optional[str]):
    """Makes tr() answer in ``code`` for the current task/thread context (e.g. while serving one request of a
    browser whose page language differs from the daemon's). Returns a token for reset_language()."""
    return _ctx_lang.set(code if code and _CODE_RE.match(str(code)) else None)


def reset_language(token) -> None:
    _ctx_lang.reset(token)


def tr(msg: str, *args) -> str:
    """Translate ``msg`` and substitute ``{0}``-style placeholders with ``args`` (if any)."""
    text = catalog_for(_ctx_lang.get()).get(msg, msg)
    if not args:
        return text
    try:
        return text.format(*args)
    except (IndexError, KeyError, ValueError):
        try:
            return msg.format(*args)
        except (IndexError, KeyError, ValueError):
            return text


def catalog_for(code: Optional[str]) -> Dict[str, str]:
    """Catalog of an arbitrary language (cached): for rendering a message in the language of one client."""
    if not code or code == "auto":
        return _catalog
    code = str(code)
    if not _CODE_RE.match(code):
        return {}
    if code not in _by_lang:
        cat: Dict[str, str] = {}
        for cand in _candidates(code):
            found = _read(BUILTIN_DIR / f"{cand}.json")
            user = _read(_user_dir / f"{cand}.json") if _user_dir else {}
            if found or user:
                cat = {**found, **user}
                break
        _by_lang[code] = cat
    return _by_lang[code]


def translate(lang: Optional[str], msg: str, *args) -> str:
    """Like tr(), but in the language ``lang`` (not the daemon-wide one)."""
    text = catalog_for(lang).get(msg, msg)
    if not args:
        return text
    try:
        return text.format(*args)
    except (IndexError, KeyError, ValueError):
        try:
            return msg.format(*args)
        except (IndexError, KeyError, ValueError):
            return text
