"""Приховування фізичного геймпада від інших програм (Linux) — ВСІ його вузли, з обліком і відновленням.

Як SDL знаходить геймпади: перебирає /dev/input/event* (і js*) та /dev/hidraw* (HIDAPI) і ВІДКРИВАЄ кожен вузол.
Вузол, який відкрити не вдалося (EACCES), SDL мовчки пропускає. Тому єдиний надійний спосіб «зникнути» для будь-якої
SDL-програми — закрити права (chmod 0600) на ВСІ вузли фізичного пристрою, лишивши відкритими дескриптори демона.
(Лише EVIOCGRAB недостатньо: захоплений evdev-вузол SDL усе одно відкриває й бачить «мертвий» дублікат геймпада.)

* Вузли пристрою беремо зі sysfs: усі hidraw*, event* (лише геймпадоподібні — без клавіатур і мишей) та js* під
  тим самим USB-/HID-пристроєм. Для Bluetooth коренем є сам HID-пристрій (не USB-адаптер!), тож чужі BT-пристрої не чіпаємо.
* chmod чужого файла потребує CAP_FOWNER (сервіс `setup-hide --service` або `serve --privileged`) або root.
  Без цього «приховування» НЕ застосовується взагалі (жодних напівзаходів, що лишили б мертвий дублікат).
* Облік: вузол ховається один раз, повертається, коли його відпустила остання сесія. Стан пишеться у файл,
  тож `gamepad-remapd unhide` (і перший запуск демона) відновлюють права після аварії.
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import re
import stat as statmod
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

_HID_RE = re.compile(r"^[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}\.[0-9A-Fa-f]+$")
_HIDRAW_RE = re.compile(r"^hidraw\d+$")
_EVENT_RE = re.compile(r"^(event|js)\d+$")
_KEY_A = 30
_HIDDEN_MODE = 0o600

_lock = threading.Lock()
_REG: Dict[str, List[int]] = {}          # шлях вузла -> [початковий режим, лічильник сесій]


@dataclass
class HideResult:
    mode: str = "off"                    # off | none | hidden | partial | denied
    nodes: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"mode": self.mode, "nodes": self.nodes, "failed": self.failed}


# ---------------------------------------------------------------- пошук вузлів у sysfs
def _find_root(start: str, sys_root: str) -> Optional[str]:
    """Корінь фізичного пристрою: USB-пристрій (є idVendor) або — для Bluetooth/uhid — сам HID-пристрій."""
    cur = os.path.realpath(start)
    hid_dir: Optional[str] = None
    usb_dir: Optional[str] = None
    stop = os.path.realpath(sys_root)
    while cur and cur != stop and cur != "/":
        name = os.path.basename(cur)
        if hid_dir is None and _HID_RE.match(name):
            hid_dir = cur
        if os.path.exists(os.path.join(cur, "idVendor")):
            usb_dir = cur
            break
        cur = os.path.dirname(cur)
    if hid_dir and ("/bluetooth/" in hid_dir or "/virtual/" in hid_dir or hid_dir.split("/")[-1].startswith("0005:")):
        return hid_dir                   # BT/uhid: не піднімаємось до USB-адаптера
    return usb_dir or hid_dir


def _bitmap(path: str) -> int:
    try:
        words = Path(path).read_text().split()
    except OSError:
        return 0
    total, bits = 0, ctypes.sizeof(ctypes.c_ulong) * 8
    for w in words:
        total = (total << bits) | int(w, 16)
    return total


def _gamepad_like(event_sysdir: str) -> bool:
    """Не миша й не клавіатура: інакше закриття вузла зламало б ввід користувача (композитні ресивери/клавіатури)."""
    dev = os.path.join(event_sysdir, "device", "capabilities")
    if _bitmap(os.path.join(dev, "rel")):
        return False
    if (_bitmap(os.path.join(dev, "key")) >> _KEY_A) & 1:
        return False
    return True


def collect_nodes(info_path: str, backend: str, sys_root: str = "/sys", dev_root: str = "/dev") -> List[str]:
    """Усі вузли /dev фізичного пристрою, до якого належить `info_path` (hidrawN або eventN)."""
    base = os.path.basename(info_path)
    if backend == "hidraw":
        start = os.path.join(sys_root, "class", "hidraw", base, "device")
    elif backend == "evdev":
        start = os.path.join(sys_root, "class", "input", base)
    else:
        return []
    if not os.path.exists(start):
        return []
    root = _find_root(start, sys_root)
    if not root:
        return []
    out: List[str] = []
    for dirpath, dirnames, _ in os.walk(root):
        parent = os.path.basename(dirpath)
        for d in list(dirnames):
            full = os.path.join(dirpath, d)
            if parent == "hidraw" and _HIDRAW_RE.match(d):
                out.append(os.path.join(dev_root, d))
            elif _EVENT_RE.match(d) and re.match(r"^input\d+$", parent):
                if d.startswith("js") or _gamepad_like(full):
                    out.append(os.path.join(dev_root, "input", d))
    return sorted(set(out))


# ---------------------------------------------------------------- приховування
class Hider:
    def __init__(self, sys_root: str = "/sys", dev_root: str = "/dev", state_path: Optional[Path] = None,
                 chmod: Callable[[str, int], None] = os.chmod, stat: Callable = os.stat):
        self.sys_root, self.dev_root = sys_root, dev_root
        self.state_path = state_path
        self._chmod, self._stat = chmod, stat
        self._mine: List[str] = []

    def apply(self, info_path: str, backend: str) -> HideResult:
        nodes = collect_nodes(info_path, backend, self.sys_root, self.dev_root)
        if not nodes:
            return HideResult("none")
        res = HideResult()
        with _lock:
            for n in nodes:
                try:
                    if n in _REG:                                   # уже сховано нами (інша сесія цього ж пристрою)
                        _REG[n][1] += 1
                        self._mine.append(n)
                        res.nodes.append(n)
                        continue
                    mode = statmod.S_IMODE(self._stat(n).st_mode)
                    if mode & 0o077 == 0:                           # уже закрито не нами — не чіпаємо й не відновлюємо
                        res.nodes.append(n)
                        continue
                    self._chmod(n, _HIDDEN_MODE)
                    _REG[n] = [mode, 1]
                    self._mine.append(n)
                    res.nodes.append(n)
                except OSError as e:
                    res.failed.append(f"{n}: {e.strerror or e}")
            self._save()
        if res.failed and not res.nodes:
            res.mode = "denied"
        elif res.failed:
            res.mode = "partial"
        else:
            res.mode = "hidden"
        return res

    def release(self) -> None:
        with _lock:
            for n in self._mine:
                ent = _REG.get(n)
                if ent is None:
                    continue
                ent[1] -= 1
                if ent[1] <= 0:
                    try:
                        self._chmod(n, ent[0])
                    except FileNotFoundError:
                        pass                                       # пристрій уже від'єднано: вузла нема, права нові при появі
                    except OSError as e:
                        log.warning("не вдалося повернути права %s: %s", n, e)
                    del _REG[n]
            self._mine.clear()
            self._save()

    def _save(self) -> None:
        if self.state_path is None:
            return
        try:
            if _REG:
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                self.state_path.write_text(json.dumps({n: e[0] for n, e in _REG.items()}))
            elif self.state_path.exists():
                self.state_path.unlink()
        except OSError:
            pass


def restore_stale(state_path: Path, chmod: Callable[[str, int], None] = os.chmod,
                  stat: Callable = os.stat) -> Tuple[List[str], List[str]]:
    """Повернути права вузлам, схованим процесом, що не встиг їх відпустити (збій/kill -9). -> (відновлено, не вдалося)."""
    done: List[str] = []
    bad: List[str] = []
    try:
        data = json.loads(state_path.read_text())
    except (OSError, ValueError):
        return done, bad
    for n, mode in data.items():
        try:
            if statmod.S_IMODE(stat(n).st_mode) == _HIDDEN_MODE:    # не чіпаємо вузол, права якого вже змінились
                chmod(n, int(mode))
                done.append(n)
        except FileNotFoundError:
            continue                                               # вузол зник (від'єднали) — права й так нові
        except OSError as e:
            bad.append(f"{n}: {e.strerror or e}")
    if not bad:
        try:
            state_path.unlink()
        except OSError:
            pass
    return done, bad
