"""Hiding the physical gamepad from other programs (Linux) — ALL of its nodes, with reference counting and restoration.

How SDL finds gamepads: it enumerates /dev/input/event* (and js*) and /dev/hidraw* (HIDAPI) and OPENS every node.
A node it cannot open (EACCES) is silently skipped by SDL. So the only reliable way to "disappear" from any
SDL program is to remove access (chmod 0600) from ALL nodes of the physical device, while the daemon keeps its
descriptors open. (EVIOCGRAB alone is not enough: SDL still opens the grabbed evdev node and sees a dead duplicate of the gamepad.)

* The device's nodes are taken from sysfs: all hidraw*, event* (gamepad-like only — no keyboards or mice) and js* under
  the same USB/HID device. For Bluetooth the root is the HID device itself (not the USB adapter), so other BT devices are not touched.
* chmod on someone else's file requires CAP_FOWNER (the `setup-hide --service` service or `serve --privileged`) or root.
  Without it, hiding is NOT applied at all (no half-measures that would leave a dead duplicate).
* Bookkeeping: a node is hidden once, and restored when the last session releases it. The state is written to a file,
  so `gamepad-remapd unhide` (and the first daemon start) restore the permissions after a crash.
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
from ..i18n import tr

log = logging.getLogger(__name__)

_HID_RE = re.compile(r"^[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}\.[0-9A-Fa-f]+$")
_HIDRAW_RE = re.compile(r"^hidraw\d+$")
_EVENT_RE = re.compile(r"^(event|js)\d+$")
_KEY_A = 30
_HIDDEN_MODE = 0o600

_lock = threading.Lock()
_REG: Dict[str, List[int]] = {}          


@dataclass
class HideResult:
    mode: str = "off"                    # off | none | hidden | partial | denied
    nodes: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"mode": self.mode, "nodes": self.nodes, "failed": self.failed}


# ---------------------------------------------------------------- finding nodes in sysfs
def _find_root(start: str, sys_root: str) -> Optional[str]:
    """Root of the physical device: the USB device (it has idVendor) or, for Bluetooth/uhid, the HID device itself."""
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
        return hid_dir                   # BT/uhid: do not climb up to the USB adapter
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
    """Not a mouse and not a keyboard: otherwise closing the node would break the user's input (composite receivers/keyboards)."""
    dev = os.path.join(event_sysdir, "device", "capabilities")
    if _bitmap(os.path.join(dev, "rel")):
        return False
    if (_bitmap(os.path.join(dev, "key")) >> _KEY_A) & 1:
        return False
    return True


def collect_nodes(info_path: str, backend: str, sys_root: str = "/sys", dev_root: str = "/dev") -> List[str]:
    """All /dev nodes of the physical device to which `info_path` belongs (hidrawN or eventN)."""
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


# ---------------------------------------------------------------- hiding
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
                    if n in _REG:                                   
                        _REG[n][1] += 1
                        self._mine.append(n)
                        res.nodes.append(n)
                        continue
                    mode = statmod.S_IMODE(self._stat(n).st_mode)
                    if mode & 0o077 == 0:                           
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
                        pass                                       
                    except OSError as e:
                        log.warning(tr('could not restore permissions on %s: %s'), n, e)
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
    """Restores permissions on nodes hidden by a process that did not get to release them (crash/kill -9). -> (restored, failed)."""
    done: List[str] = []
    bad: List[str] = []
    try:
        data = json.loads(state_path.read_text())
    except (OSError, ValueError):
        return done, bad
    for n, mode in data.items():
        try:
            if statmod.S_IMODE(stat(n).st_mode) == _HIDDEN_MODE:    
                chmod(n, int(mode))
                done.append(n)
        except FileNotFoundError:
            continue                                               
        except OSError as e:
            bad.append(f"{n}: {e.strerror or e}")
    if not bad:
        try:
            state_path.unlink()
        except OSError:
            pass
    return done, bad
