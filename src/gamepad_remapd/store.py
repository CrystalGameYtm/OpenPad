"""Profile storage on disk (built-in + user) and the state of "which profile is active".

All writes are atomic: we write to a temporary file next to it and do ``os.replace`` —
a power loss mid-write will not leave a half-written JSON.
Profile names go through PROFILE_NAME_RE (the profile name check), so ``../`` or
absolute paths in a name are impossible (path traversal).
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .logic.profile import PROFILE_NAME_RE, ProfileError, normalize_profile
from .i18n import tr
from .util import NotFoundError


class StoreError(Exception):
    pass


class ProfileNotFound(StoreError, NotFoundError):
    pass


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())      
        os.replace(tmp, path)         
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class ProfileStore:
    def __init__(self, user_dir: Path, builtin_dir: Path, state_path: Path):
        self.user_dir = Path(user_dir)
        self.builtin_dir = Path(builtin_dir)
        self.state_path = Path(state_path)
        self._lock = threading.Lock()

    # ---- helpers ----
    @staticmethod
    def _check_name(name: str) -> str:
        if not isinstance(name, str) or not PROFILE_NAME_RE.match(name):
            raise StoreError(tr('invalid profile name (letters/digits/._- , up to 64 characters)'))
        return name

    def _path(self, base: Path, name: str) -> Path:
        return base / f"{self._check_name(name)}.json"

    @staticmethod
    def _read(path: Path) -> Dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise StoreError(tr('{0}: invalid JSON: {1}', path, e)) from None

    # ---- profiles ----
    def list(self) -> List[Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for source, base in (("builtin", self.builtin_dir), ("user", self.user_dir)):
            if not base.is_dir():
                continue
            for p in sorted(base.glob("*.json")):
                if not PROFILE_NAME_RE.match(p.stem):
                    continue
                try:
                    d = self._read(p)
                except StoreError:
                    d = {}
                out[p.stem] = {"name": p.stem, "source": source,
                               "description": str(d.get("description", "")),
                               "device": d.get("device")}
        return [out[k] for k in sorted(out)]

    def get(self, name: str) -> Dict[str, Any]:
        """A user profile overrides the built-in one with the same name."""
        for base in (self.user_dir, self.builtin_dir):
            p = self._path(base, name)
            if p.is_file():
                data = self._read(p)
                data.setdefault("name", name)
                return data
        raise ProfileNotFound(tr('profile {0!r} not found', name))

    def save(self, name: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Validates and saves a user profile. Returns the normalized dict."""
        self._check_name(name)
        data = dict(data)
        data["name"] = name
        norm = normalize_profile(data)         
        with self._lock:
            _atomic_write(self._path(self.user_dir, name),
                          json.dumps(norm, ensure_ascii=False, indent=2) + "\n")
        return norm

    def delete(self, name: str) -> None:
        p = self._path(self.user_dir, name)
        if not p.is_file():
            raise StoreError(tr('there is no user profile {0!r} (built-in ones cannot be deleted)', name))
        with self._lock:
            p.unlink()

    # ---- state (active profiles) ----
    def _load_state(self) -> Dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def get_active(self, device_key: str) -> Optional[str]:
        return (self._load_state().get("active") or {}).get(device_key)

    def set_active(self, device_key: str, name: str) -> None:
        self._check_name(name)
        with self._lock:
            st = self._load_state()
            st.setdefault("active", {})[device_key] = name
            try:
                _atomic_write(self.state_path, json.dumps(st, indent=2) + "\n")
            except OSError:
                pass                           

    # ---- selected protocol mode (layout id) for the device model ----
    def get_mode(self, device_key: str) -> Optional[str]:
        return (self._load_state().get("modes") or {}).get(device_key)

    def set_mode(self, device_key: str, layout_id: str) -> None:
        with self._lock:
            st = self._load_state()
            st.setdefault("modes", {})[device_key] = str(layout_id)
            try:
                _atomic_write(self.state_path, json.dumps(st, indent=2) + "\n")
            except OSError:
                pass

    # ---- device options: output protocol, original-device hiding ----
    def get_opts(self, device_key: str) -> dict:
        v = (self._load_state().get("opts") or {}).get(device_key)
        return dict(v) if isinstance(v, dict) else {}

    def set_opts(self, device_key: str, **opts) -> None:
        with self._lock:
            st = self._load_state()
            cur = st.setdefault("opts", {}).setdefault(device_key, {})
            cur.update({k: v for k, v in opts.items() if v is not None})
            try:
                _atomic_write(self.state_path, json.dumps(st, indent=2) + "\n")
            except OSError:
                pass


__all__ = ["ProfileStore", "StoreError", "ProfileError"]
