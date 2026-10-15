"""Linux: геймпади через evdev (/dev/input/eventN) — XInput-режим, xpad, hid-playstation тощо.

Навіщо: коли геймпад перемкнуто в XInput-режим (або ядро має для нього власний драйвер), hidraw-вузла
може не бути взагалі, а стан віддає лише evdev. Пристрій читається як потік подій ядра, які
складаються в «синтетичні звіти» (формат — logic/evdev_layout.py), тож далі працює той самий
конвеєр, що й для hidraw. Власні віртуальні пристрої демона (uinput, /sys/devices/virtual/...)
ігноруються, щоб не вийшла петля «читаємо власний вихід».

Обмеження: вібрація (FF) через evdev поки не реалізована; handshake тут не потрібен.
"""
from __future__ import annotations

import collections
import errno
import fcntl
import logging
import os
import select
import struct
from typing import Callable, Deque, Dict, List, Optional, Set, Tuple

from ..logic.evdev_layout import (ABS_HAT0X, ABS_HAT0Y, REPORT_ID, S16_MAX, S16_MIN, EvdevSpec,
                                  is_gamepad, spec_from_caps)
from ..util import IOC, IOC_READ, IOR
from .base import DeviceGone, DeviceInfo, InputDevice

log = logging.getLogger(__name__)

SYS_INPUT = "/sys/class/input"
DEV_INPUT = "/dev/input"
EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
SYN_REPORT, SYN_DROPPED = 0, 3
_EVENT = struct.Struct("llHHi")                    # struct input_event
_WORD_BITS = struct.calcsize("L") * 8              # sysfs друкує слова розміру unsigned long
EVIOCGRAB = IOC(1, "E", 0x90, 4)


def EVIOCGABS(code: int) -> int:
    return IOR("E", 0x40 + code, 24)                # struct input_absinfo: 6 x int32


def EVIOCGKEY(length: int) -> int:
    return IOC(IOC_READ, "E", 0x18, length)


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def parse_bits(text: str) -> Set[int]:
    """sysfs capabilities: слова hex через пробіл, СТАРШЕ слово першим -> множина номерів бітів."""
    out: Set[int] = set()
    words = text.split()
    for wi, w in enumerate(reversed(words)):
        try:
            v = int(w, 16)
        except ValueError:
            continue
        b = 0
        while v:
            if v & 1:
                out.add(wi * _WORD_BITS + b)
            v >>= 1
            b += 1
    return out


def _abs_range_via_ioctl(node: str, code: int) -> Optional[Tuple[int, int]]:
    try:
        fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        buf = bytearray(24)
        fcntl.ioctl(fd, EVIOCGABS(code), buf, True)
        _value, lo, hi = struct.unpack_from("iii", buf)
        return lo, hi
    except OSError:
        return None
    finally:
        os.close(fd)


_range_cache: Dict[Tuple[str, str], Dict[int, Tuple[int, int]]] = {}


def enumerate_evdev(sys_root: str = SYS_INPUT, dev_root: str = DEV_INPUT, *,
                    read_abs: Callable[[str, int], Optional[Tuple[int, int]]] = _abs_range_via_ioctl
                    ) -> List[DeviceInfo]:
    """Геймпади з /sys/class/input (без відкриття вузлів для самого переліку; діапазони осей
    читаються best-effort через EVIOCGABS і кешуються)."""
    out: List[DeviceInfo] = []
    try:
        names = [n for n in os.listdir(sys_root) if n.startswith("event")]
    except OSError:
        return out
    names.sort(key=lambda n: int(n[5:]) if n[5:].isdigit() else 0)
    for name in names:
        base = os.path.join(sys_root, name)
        if "/devices/virtual/" in os.path.realpath(base):
            continue                                   # uinput та інші віртуальні (зокрема наш вихід)
        dev_dir = os.path.join(base, "device")
        caps = os.path.join(dev_dir, "capabilities")
        ev = parse_bits(_read(os.path.join(caps, "ev")))
        if EV_KEY not in ev or EV_ABS not in ev:
            continue
        keys = parse_bits(_read(os.path.join(caps, "key")))
        abs_codes = parse_bits(_read(os.path.join(caps, "abs")))
        if not is_gamepad(keys, abs_codes):
            continue
        try:
            vid = int(_read(os.path.join(dev_dir, "id", "vendor")), 16)
            pid = int(_read(os.path.join(dev_dir, "id", "product")), 16)
            bus = int(_read(os.path.join(dev_dir, "id", "bustype")), 16)
        except ValueError:
            continue
        node = os.path.join(dev_root, name)
        dev_name = _read(os.path.join(dev_dir, "name"))
        ck = (node, dev_name)
        if ck not in _range_cache:
            rng = {c: r for c in abs_codes if (r := read_abs(node, c)) is not None}
            _range_cache[ck] = rng
            if len(_range_cache) > 256:
                _range_cache.pop(next(iter(_range_cache)))
        rng = _range_cache[ck]
        abs_info = {c: rng.get(c, (-1, 1) if c in (ABS_HAT0X, ABS_HAT0Y) else (S16_MIN, S16_MAX))
                    for c in abs_codes}
        out.append(DeviceInfo(path=node, vid=vid, pid=pid, name=dev_name, interface=None, bus=bus,
                              backend="evdev",
                              extra={"keys": sorted(keys), "abs": abs_info,
                                     "range_known": all(c in rng for c in abs_codes)}))
    return out


def spec_for(info: DeviceInfo) -> EvdevSpec:
    ex = info.extra or {}
    return spec_from_caps(ex.get("keys", ()), {int(k): tuple(v) for k, v in ex.get("abs", {}).items()})


_HAT_VALUE = {(0, -1): 0, (1, -1): 1, (1, 0): 2, (1, 1): 3, (0, 1): 4, (-1, 1): 5, (-1, 0): 6, (-1, -1): 7}


class EvdevDevice(InputDevice):
    """Читає події ядра й віддає їх як синтетичні звіти (по одному на SYN_REPORT)."""

    def __init__(self, info: DeviceInfo, read_size: int = 64, *,
                 opener: Callable[[str], int] = lambda p: os.open(p, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)):
        super().__init__(info, read_size)
        self._opener = opener
        self.spec = spec_for(info)
        self._index = {k: i for i, k in enumerate(self.spec.keys)}
        self._axis = {a[0]: a for a in self.spec.axes}
        self._fd = self._wake_r = self._wake_w = -1
        self._poll: Optional[select.poll] = None
        self._keys = 0                                   # біт i = i-та кнопка спеки
        self._abs: Dict[int, int] = {}
        self._hat = [0, 0]
        self._ready: Deque[bytes] = collections.deque()
        self._dropping = False

    # ---- життєвий цикл ----
    def open(self) -> None:
        self._fd = self._opener(self.info.path)
        self._wake_r, self._wake_w = os.pipe()
        self._poll = select.poll()
        self._poll.register(self._fd, select.POLLIN | select.POLLERR | select.POLLHUP)
        self._poll.register(self._wake_r, select.POLLIN)
        self._resync()
        self._ready.append(self._pack())                 # початковий стан = «точка відліку» для UI

    def close(self) -> None:
        if self._fd >= 0:
            try:
                fcntl.ioctl(self._fd, EVIOCGRAB, 0)
            except OSError:
                pass
        for fd in (self._fd, self._wake_r, self._wake_w):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._fd = self._wake_r = self._wake_w = -1

    def wake(self) -> None:
        if self._wake_w >= 0:
            try:
                os.write(self._wake_w, b"x")
            except OSError:
                pass

    def grab_siblings(self) -> int:
        """Для evdev «дубль» — це сам вузол: захоплюємо його, щоб ігри бачили лише віртуальний пад."""
        try:
            fcntl.ioctl(self._fd, EVIOCGRAB, 1)
            return 1
        except OSError as e:
            log.warning("не вдалося захопити %s: %s", self.info.path, e)
            return 0

    # ---- стан ----
    def _resync(self) -> None:
        """Початковий/відновлений після SYN_DROPPED стан із ядра (best-effort)."""
        try:
            nbytes = (max(self.spec.keys, default=0) // 8) + 1
            buf = bytearray(nbytes)
            fcntl.ioctl(self._fd, EVIOCGKEY(nbytes), buf, True)
            keys = 0
            for code, i in self._index.items():
                if code // 8 < nbytes and buf[code // 8] >> (code % 8) & 1:
                    keys |= 1 << i
            self._keys = keys
        except OSError:
            pass
        for code in list(self._axis) + ([ABS_HAT0X, ABS_HAT0Y] if self.spec.hat else []):
            try:
                buf = bytearray(24)
                fcntl.ioctl(self._fd, EVIOCGABS(code), buf, True)
                v = struct.unpack_from("i", buf)[0]
            except OSError:
                continue
            if self.spec.hat and code == ABS_HAT0X:
                self._hat[0] = v
            elif self.spec.hat and code == ABS_HAT0Y:
                self._hat[1] = v
            else:
                self._abs[code] = v

    def _pack(self) -> bytes:
        sp = self.spec
        buf = bytearray(sp.size)
        buf[0] = REPORT_ID
        k = self._keys
        for b in range((len(sp.keys) + 7) // 8):
            buf[1 + b] = (k >> (8 * b)) & 0xFF
        if sp.hat:
            buf[sp.hat_byte] = _HAT_VALUE.get((max(-1, min(1, self._hat[0])),
                                               max(-1, min(1, self._hat[1]))), 8)
        for code, off, lo, hi in sp.axes:
            v = max(lo, min(hi, self._abs.get(code, 0)))
            struct.pack_into("<h", buf, off, v)
        return bytes(buf)

    # ---- гаряче читання ----
    def read(self) -> Optional[bytes]:
        if self._ready:
            return self._ready.popleft()
        for fd, mask in self._poll.poll():
            if fd == self._wake_r:
                os.read(self._wake_r, 64)
                return None
            if mask & select.POLLIN:
                try:
                    data = os.read(self._fd, _EVENT.size * 64)
                except BlockingIOError:
                    return None
                except OSError as e:
                    raise DeviceGone(str(e)) from e
                if not data:
                    raise DeviceGone("evdev: EOF")
                self._consume(data)
                return self._ready.popleft() if self._ready else None
            raise DeviceGone("evdev: POLLERR/POLLHUP (пристрій відключено)")
        return None

    def _consume(self, data: bytes) -> None:
        sp = self.spec
        for _s, _us, etype, code, value in _EVENT.iter_unpack(data[:len(data) - len(data) % _EVENT.size]):
            if etype == EV_SYN:
                if code == SYN_DROPPED:
                    self._dropping = True
                elif code == SYN_REPORT:
                    if self._dropping:
                        self._dropping = False
                        self._resync()
                    self._ready.append(self._pack())
            elif self._dropping:
                continue
            elif etype == EV_KEY:
                i = self._index.get(code)
                if i is not None and value != 2:             # 2 = автоповтор клавіші, ігноруємо
                    self._keys = (self._keys | (1 << i)) if value else (self._keys & ~(1 << i))
            elif etype == EV_ABS:
                if sp.hat and code == ABS_HAT0X:
                    self._hat[0] = value
                elif sp.hat and code == ABS_HAT0Y:
                    self._hat[1] = value
                elif code in self._axis:
                    self._abs[code] = value

    # ---- не підтримується (немає vendor-звітів/вібрації через evdev) ----
    def send_feature(self, data: bytes) -> None:
        raise OSError(errno.ENOTSUP, "evdev: feature-звіти не підтримуються")

    def get_feature(self, report_id: int, length: int) -> bytes:
        raise OSError(errno.ENOTSUP, "evdev: feature-звіти не підтримуються")

    def write_output(self, data: bytes) -> None:
        raise OSError(errno.ENOTSUP, "evdev: вихідні звіти (вібрація) поки не підтримуються")
