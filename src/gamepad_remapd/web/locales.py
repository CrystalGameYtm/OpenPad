"""Web UI translations stored as JSON files (standard library only; shared by both web servers).

Built-in locales live in ``web/static/locales/<code>.json``; users can add or override them with
``<config>/locales/<code>.json``. A locale file is a flat ``{"key": "text"}`` object; the optional
``"_name"`` key is the language's own name shown in the picker. ``{name}`` placeholders are filled
in by the client. English is the fallback for any key missing from another locale.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

BUILTIN_DIR = Path(__file__).resolve().parent / "static" / "locales"
CODE_RE = re.compile(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?")
FALLBACK = "en"


def _read(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(data, dict) else {}


def load_locale(code: str, user_dir: Optional[Path] = None) -> Optional[Dict[str, str]]:
    """Built-in strings overlaid with the user's file; None if the locale does not exist."""
    if not CODE_RE.fullmatch(code):
        return None
    merged: Dict[str, str] = {}
    found = False
    for d in (BUILTIN_DIR, user_dir):
        if d is not None and (d / f"{code}.json").is_file():
            merged.update(_read(d / f"{code}.json"))
            found = True
    return merged if found else None


def list_locales(user_dir: Optional[Path] = None) -> List[Dict[str, str]]:
    codes = set()
    for d in (BUILTIN_DIR, user_dir):
        if d is not None and d.is_dir():
            codes.update(p.stem for p in d.glob("*.json") if CODE_RE.fullmatch(p.stem))
    out = []
    for code in sorted(codes, key=lambda c: (c != FALLBACK, c)):
        loc = load_locale(code, user_dir) or {}
        out.append({"code": code, "name": loc.get("_name", code)})
    return out
