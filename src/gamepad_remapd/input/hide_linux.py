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
                from ..config import ensure_private_dir
                ensure_private_dir(self.state_path.parent)
                tmp = self.state_path.with_name(self.state_path.name + ".tmp")
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(json.dumps({n: e[0] for n, e in _REG.items()}))
                os.replace(tmp, self.state_path)
            elif self.state_path.exists():
                self.state_path.unlink()
        except OSError:
            pass


_NODE_RE = re.compile(r"^(hidraw\d+|input/(event|js)\d+)$")


def _valid_node(path: object, dev_root: str) -> bool:
    """Only device nodes that this module can hide: `<dev_root>/hidrawN` and `<dev_root>/input/{event,js}N`, nothing else."""
    if not isinstance(path, str):
        return False
    prefix = dev_root.rstrip("/") + "/"
    return path.startswith(prefix) and bool(_NODE_RE.match(path[len(prefix):]))


def _valid_mode(mode: object) -> bool:
    """A mode that was a real device node's mode: plain permission bits, no setuid/setgid/sticky, no execute, no write-only oddities."""
    return isinstance(mode, int) and not isinstance(mode, bool) and 0 <= mode <= 0o666 and mode & 0o111 == 0


def _trusted_uids() -> set:
    """The current user, root, and (for `sudo ... unhide`) the user who ran sudo."""
    ok = {0}
    if hasattr(os, "geteuid"):
        ok.add(os.geteuid())
    sudo = os.environ.get("SUDO_UID", "")
    if sudo.isdigit():
        ok.add(int(sudo))
    return ok


def _state_file_trusted(state_path: Path) -> bool:
    """The state file must be a regular file (not a symlink) that group and others cannot write.

    The owner is not compared with the current user on purpose: `sudo gamepad-remapd unhide` runs as root on a file of the user.
    Safety there comes from the node whitelist in `restore_stale`, not from trusting the file.
    """
    try:
        st = os.lstat(state_path)
        dst = os.lstat(state_path.parent)
    except OSError:
        return False
    if not statmod.S_ISREG(st.st_mode) or st.st_mode & 0o022:
        return False
    if not statmod.S_ISDIR(dst.st_mode) or dst.st_mode & 0o022:      # not a symlink, not writable by others
        return False
    if hasattr(os, "geteuid"):                                         # planted by another user in a shared directory
        ok = _trusted_uids()
        return st.st_uid in ok and dst.st_uid in ok
    return True


def restore_stale(state_path: Path, chmod: Callable[[str, int], None] = os.chmod,
                  stat: Callable = os.stat, *, dev_root: str = "/dev",
                  is_node: Callable[[int], bool] = statmod.S_ISCHR) -> Tuple[List[str], List[str]]:
    """Restores permissions on nodes hidden by a process that did not get to release them (crash/kill -9). -> (restored, failed).

    The file lives in a user-writable directory while this function may run with CAP_FOWNER, so nothing in it is trusted:
    only `/dev/hidrawN` and `/dev/input/{event,js}N` that are character devices are touched, and only with plain permission bits.
    """
    done: List[str] = []
    bad: List[str] = []
    if not state_path.exists() and not state_path.is_symlink():
        return done, bad
    if not _state_file_trusted(state_path):
        log.warning(tr('ignoring %s: it, or its directory, is not trusted (not a regular file, writable by others, or owned by another user)'), state_path)
        return done, [f"{state_path}: untrusted"]
    try:
        data = json.loads(state_path.read_text())
    except (OSError, ValueError):
        return done, bad
    if not isinstance(data, dict):
        return done, bad
    for n, mode in data.items():
        if not _valid_node(n, dev_root) or not _valid_mode(mode):
            log.warning(tr('ignoring an invalid entry in %s: %r'), state_path, n)
            bad.append(f"{n}: invalid entry")
            continue
        try:
            st = os.lstat(n) if stat is os.stat else stat(n)
            if not is_node(st.st_mode) or statmod.S_ISLNK(st.st_mode):
                bad.append(f"{n}: not a device node")
                continue
            if statmod.S_IMODE(st.st_mode) == _HIDDEN_MODE:
                chmod(n, mode)
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
