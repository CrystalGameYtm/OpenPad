"""Сховище профілів на диску (вбудовані + користувацькі) та стан «який профіль активний».

Усі записи атомарні: пишемо у тимчасовий файл поруч і робимо ``os.replace`` —
аварійне вимкнення живлення не залишить напівзаписаного JSON.
Імена профілів проходять через PROFILE_NAME_RE (профіль), тож ``../`` або
абсолютні шляхи у імені неможливі (path traversal).
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .logic.profile import PROFILE_NAME_RE, ProfileError, normalize_profile


class StoreError(Exception):
    pass


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())      # дані на диску до того, як файл замінить старий
        os.replace(tmp, path)         # атомарна заміна (POSIX та Windows)
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

    # ---- допоміжне ----------------------------------------------------------
    @staticmethod
    def _check_name(name: str) -> str:
        if not isinstance(name, str) or not PROFILE_NAME_RE.match(name):
            raise StoreError("некоректне ім'я профілю (літери/цифри/._- , до 64 символів)")
        return name

    def _path(self, base: Path, name: str) -> Path:
        return base / f"{self._check_name(name)}.json"

    @staticmethod
    def _read(path: Path) -> Dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise StoreError(f"{path}: некоректний JSON: {e}") from None

    # ---- профілі ----------------------------------------------------------------
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
        """Користувацький профіль перекриває вбудований з тим самим ім'ям."""
        for base in (self.user_dir, self.builtin_dir):
            p = self._path(base, name)
            if p.is_file():
                data = self._read(p)
                data.setdefault("name", name)
                return data
        raise StoreError(f"профіль {name!r} не знайдено")

    def save(self, name: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Валідує та зберігає користувацький профіль. Повертає нормалізований dict."""
        self._check_name(name)
        data = dict(data)
        data["name"] = name
        norm = normalize_profile(data)         # кидає ProfileError з шляхом до поля
        with self._lock:
            _atomic_write(self._path(self.user_dir, name),
                          json.dumps(norm, ensure_ascii=False, indent=2) + "\n")
        return norm

    def delete(self, name: str) -> None:
        p = self._path(self.user_dir, name)
        if not p.is_file():
            raise StoreError(f"користувацького профілю {name!r} немає (вбудовані не видаляються)")
        with self._lock:
            p.unlink()

    # ---- стан (активні профілі) ---------------------------------------------------
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
                pass                           # стан — зручність, а не критична дія


__all__ = ["ProfileStore", "StoreError", "ProfileError"]
