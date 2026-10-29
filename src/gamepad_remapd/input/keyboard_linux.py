"""Linux: клавіатура як джерело подій геймпада (evdev -> KeyPacker -> синтетичні звіти).

Безпека — головне. Захоплення клавіатури (EVIOCGRAB) забирає її в усієї системи, тому:
  * вхід вимкнений, поки користувач сам не ввімкне `keyboard.enabled` і не вкаже `device`;
  * захоплення вмикається лише коли ВСІ клавіші відпущені (інакше в робочому столі «залипає» модифікатор);
  * комбінація `toggle` (за замовчуванням Ctrl+Alt+G) миттєво вмикає/вимикає режим геймпада й повертає
    клавіатуру системі; при падінні/виході демона ядро саме знімає захоплення (fd закривається);
  * кілька вузлів однієї клавіатури (клавіатурний + «consumer»/NKRO інтерфейси) читаються разом.
"""
from __future__ import annotations

import collections
import errno
import fcntl
import logging
import os
import select
import struct
from typing import Callable, Deque, Dict, List, Optional, Sequence, Set, Tuple

from ..logic.keyboard import KeyMap, KeyPacker, key_code, parse_chord
from ..util import IOC, IOC_READ
from .base import DeviceGone, DeviceInfo, InputDevice
from .evdev_linux import (DEV_INPUT, EV_KEY, EV_SYN, EVIOCGRAB, SYN_DROPPED, SYN_REPORT, SYS_INPUT, _EVENT,
                          _read, is_gamepad, parse_bits)

log = logging.getLogger(__name__)

KEY_A, KEY_W, KEY_SPACE = 30, 17, 57
DEFAULT_TOGGLE = ("LEFTCTRL", "LEFTALT", "G")


def EVIOCGKEY(length: int) -> int:
    return IOC(IOC_READ, "E", 0x18, length)


def _matches(selector: str, node: str, name: str, vid: int, pid: int) -> bool:
    s = selector.strip().lower()
    return bool(s) and (s == node.lower() or s in name.lower() or s == f"{vid:04x}:{pid:04x}")


def enumerate_keyboards(selectors: Sequence[str] = (), sys_root: str = SYS_INPUT, dev_root: str = DEV_INPUT
                        ) -> List[DeviceInfo]:
    """Вузли, схожі на повноцінну клавіатуру (є A, W, Space; не геймпад; не віртуальні).

    selectors: порожньо -> усі кандидати (для `gamepad-remapd keyboards`); інакше — лише ті, що збігаються
    за шляхом /dev/input/eventN, підрядком імені або VID:PID. Вузли однієї фізичної клавіатури
    (однаковий phys-префікс без суфікса /inputN) зводяться в один DeviceInfo з extra["paths"]."""
    cands = []
    try:
        names = [n for n in os.listdir(sys_root) if n.startswith("event")]
    except OSError:
        return []
    names.sort(key=lambda n: int(n[5:]) if n[5:].isdigit() else 0)
    for name in names:
        base = os.path.join(sys_root, name)
        if "/devices/virtual/" in os.path.realpath(base):
            continue
        dev_dir = os.path.join(base, "device")
        caps = os.path.join(dev_dir, "capabilities")
        ev = parse_bits(_read(os.path.join(caps, "ev")))
        if EV_KEY not in ev:
            continue
        keys = parse_bits(_read(os.path.join(caps, "key")))
        abs_codes = parse_bits(_read(os.path.join(caps, "abs")))
        if not {KEY_A, KEY_W, KEY_SPACE} <= keys or is_gamepad(keys, abs_codes):
            continue
        try:
            vid = int(_read(os.path.join(dev_dir, "id", "vendor")), 16)
            pid = int(_read(os.path.join(dev_dir, "id", "product")), 16)
            bus = int(_read(os.path.join(dev_dir, "id", "bustype")), 16)
        except ValueError:
            continue
        node = os.path.join(dev_root, name)
        cands.append((node, _read(os.path.join(dev_dir, "name")), vid, pid, bus))
    out: List[DeviceInfo] = []
    grouped: Dict[Tuple[int, int, int, str], List[Tuple[str, str]]] = {}
    for node, nm, vid, pid, bus in cands:
        if selectors and not any(_matches(s, node, nm, vid, pid) for s in selectors):
            continue
        grouped.setdefault((vid, pid, bus, nm), []).append((node, nm))
    for (vid, pid, bus, nm), nodes in grouped.items():
        paths = [n for n, _ in nodes]
        out.append(DeviceInfo(path=paths[0], vid=vid, pid=pid, name=nm, interface=None, bus=bus, backend="evdev",
                              extra={"kind": "keyboard", "paths": paths}))
    return out


def is_keyboard(info: DeviceInfo) -> bool:
    return bool(info.extra) and info.extra.get("kind") == "keyboard"


class KeyboardDevice(InputDevice):
    def __init__(self, info: DeviceInfo, keymap: KeyMap, read_size: int = 64, *, grab: bool = True,
                 toggle: Sequence = DEFAULT_TOGGLE, opener: Optional[Callable[[str], int]] = None,
                 ioctl_fn: Callable = fcntl.ioctl):
        super().__init__(info, read_size)
        self.keymap = keymap
        self.packer = KeyPacker(keymap)
        self.want_grab = grab
        self.chord: Tuple[int, ...] = parse_chord(toggle)
        self._opener = opener or (lambda p: os.open(p, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC))
        self._ioctl = ioctl_fn
        self.paths: List[str] = list(((info.extra or {}).get("paths")) or [info.path])
        self._fds: List[int] = []
        self._wake_r = self._wake_w = -1
        self._poll: Optional[select.poll] = None
        self._down: Set[int] = set()                  # усі фізично натиснуті клавіші (не лише з keymap)
        self.active = True                            # режим геймпада ввімкнено (не «поставлено на паузу» chord-ом)
        self.grabbed = False
        self._ready: Deque[bytes] = collections.deque()
        self._dropping: Set[int] = set()
        self.rumble_ok = False

    # ---- життєвий цикл ----
    def open(self) -> None:
        try:
            for p in self.paths:
                self._fds.append(self._opener(p))
            self._wake_r, self._wake_w = os.pipe()
            self._poll = select.poll()
            for fd in self._fds:
                self._poll.register(fd, select.POLLIN | select.POLLERR | select.POLLHUP)
            self._poll.register(self._wake_r, select.POLLIN)
            self._resync()
            self._maybe_grab()
            self._ready.append(self.packer.report())
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        for fd in self._fds:
            if self.grabbed:
                try:
                    self._ioctl(fd, EVIOCGRAB, 0)
                except OSError:
                    pass
        self.grabbed = False
        for fd in (*self._fds, self._wake_r, self._wake_w):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._fds, self._wake_r, self._wake_w = [], -1, -1

    def wake(self) -> None:
        if self._wake_w >= 0:
            try:
                os.write(self._wake_w, b"x")
            except OSError:
                pass

    def grab_siblings(self) -> int:
        return 0                                       # захоплення керується тут (when idle + toggle)

    # ---- захоплення ----
    def _set_grab(self, on: bool) -> None:
        ok = True
        for fd in self._fds:
            try:
                self._ioctl(fd, EVIOCGRAB, 1 if on else 0)
            except OSError as e:
                ok = False
                log.warning("%s: EVIOCGRAB(%d) не вдалося: %s", self.info.path, int(on), e)
        self.grabbed = on and ok

    def _maybe_grab(self) -> None:
        """Захоплюємо лише коли режим активний і нічого не натиснуто (інакше залипнуть клавіші в системі)."""
        if self.want_grab and self.active and not self.grabbed and not self._down:
            self._set_grab(True)

    # ---- стан ----
    def _resync(self) -> None:
        down: Set[int] = set()
        nbytes = 96                                    # 768 бітів — з запасом для будь-яких KEY_*
        for fd in self._fds:
            buf = bytearray(nbytes)
            try:
                self._ioctl(fd, EVIOCGKEY(nbytes), buf, True)
            except OSError:
                continue
            for code in range(nbytes * 8):
                if buf[code // 8] >> (code % 8) & 1:
                    down.add(code)
        self._down = down
        self.packer.clear()
        if self.active:
            for c in sorted(down):
                self.packer.press(c)

    def _on_key(self, code: int, value: int) -> bool:
        if value == 2:
            return False                               # автоповтор
        if value:
            self._down.add(code)
        else:
            self._down.discard(code)
        if code in self.chord and value and all(c in self._down for c in self.chord):
            self._toggle()
            return True
        if not self.active:
            return False
        changed = self.packer.press(code) if value else self.packer.release(code)
        if not value:
            self._maybe_grab()
        return changed

    def _toggle(self) -> None:
        self.active = not self.active
        log.info("%s: режим «клавіатура як геймпад» %s", self.info.path, "УВІМКНЕНО" if self.active else "вимкнено")
        self.packer.clear()
        if not self.active and self.grabbed:
            self._set_grab(False)                      # клавіатура одразу повертається системі
        # при вмиканні захоплення відкладається до моменту, коли всі клавіші відпущено (_maybe_grab)

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
                    data = os.read(fd, _EVENT.size * 64)
                except BlockingIOError:
                    return None
                except OSError as e:
                    raise DeviceGone(str(e)) from e
                if not data:
                    raise DeviceGone("keyboard: EOF")
                self._consume(fd, data)
                return self._ready.popleft() if self._ready else None
            raise DeviceGone("keyboard: POLLERR/POLLHUP (пристрій відключено)")
        return None

    def _consume(self, fd: int, data: bytes) -> None:
        changed = False
        for _s, _us, etype, code, value in _EVENT.iter_unpack(data[:len(data) - len(data) % _EVENT.size]):
            if etype == EV_SYN:
                if code == SYN_DROPPED:
                    self._dropping.add(fd)
                elif code == SYN_REPORT:
                    if fd in self._dropping:
                        self._dropping.discard(fd)
                        self._resync()
                        changed = True
                    if changed:
                        self._ready.append(self.packer.report())
                        changed = False
            elif etype == EV_KEY and fd not in self._dropping:
                changed |= self._on_key(code, value)

    # ---- не підтримується ----
    def send_feature(self, data: bytes) -> None:
        raise OSError(errno.ENOTSUP, "keyboard: feature-звіти не підтримуються")

    def get_feature(self, report_id: int, length: int) -> bytes:
        raise OSError(errno.ENOTSUP, "keyboard: feature-звіти не підтримуються")

    def write_output(self, data: bytes) -> None:
        raise OSError(errno.ENOTSUP, "keyboard: вібрації немає")
