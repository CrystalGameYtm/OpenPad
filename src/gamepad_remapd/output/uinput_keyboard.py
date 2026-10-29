"""Віртуальна клавіатура + кнопки миші (uinput) для цілей `KEY_*` / `MOUSE_*` у button_map.

Окремий пристрій від віртуального геймпада: гра бачить геймпад, а середовище/гра — звичайну клавіатуру.
Усі клавіші оголошуються наперед (uinput не вміє додавати їх після створення). Кнопки миші потребують,
щоб пристрій «виглядав» як вказівник, тому оголошено й відносні осі REL_X/Y/колесо (рухів ми не шлемо).

Як і в UinputPad: зміни кадру збираються в буфер і йдуть ОДНИМ write() разом із SYN_REPORT. Стан утримуваних
клавіш відстежується, щоб `release_all()` (зміна профілю, відключення геймпада) ніколи не лишав «залипла» клавішу.
"""
from __future__ import annotations

import errno
import fcntl
import logging
import os
from typing import Optional, Set

from .. import constants as C
from ..logic.keytargets import all_key_codes
from .uinput_linux import (BUS_USB, EVSZ, UI_DEV_CREATE, UI_DEV_DESTROY, UI_DEV_SETUP, UI_SET_EVBIT,
                           UI_SET_KEYBIT, _EV, _SETUP, _IOW)

log = logging.getLogger(__name__)

EV_REL = 0x02
REL_X, REL_Y, REL_WHEEL = 0x00, 0x01, 0x08
UI_SET_RELBIT = _IOW("U", 102, 4)
BUF_EVENTS = 128


class VirtualKeyboard:
    def __init__(self, path: str = "/dev/uinput", name: str = "Gamepad Remapper Virtual Keyboard",
                 fd: Optional[int] = None):
        """`fd` — готовий дескриптор (pipe у тестах): тоді пристрій не створюється, події пишуться в нього."""
        self.path, self.dev_name = path, name
        self._fd = -1 if fd is None else fd
        self._external_fd = fd is not None
        self._created = False
        self._buf = bytearray(EVSZ * BUF_EVENTS)
        self._mv = memoryview(self._buf)
        self._n = 0
        self._held: Set[int] = set()
        self.dropped_writes = 0

    def open(self) -> None:
        if self._external_fd or self._created:
            return
        fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            ioc = fcntl.ioctl
            ioc(fd, UI_SET_EVBIT, C.EV_KEY)
            ioc(fd, UI_SET_EVBIT, EV_REL)
            for code in all_key_codes():
                ioc(fd, UI_SET_KEYBIT, code)
            for code in (REL_X, REL_Y, REL_WHEEL):
                ioc(fd, UI_SET_RELBIT, code)
            ioc(fd, UI_DEV_SETUP, _SETUP.pack(BUS_USB, 0x1209, 0x0001, 1, self.dev_name.encode()[:79], 0))
            ioc(fd, UI_DEV_CREATE)
        except Exception:
            os.close(fd)
            raise
        self._fd, self._created = fd, True
        log.info("uinput: створено віртуальну клавіатуру %r", self.dev_name)

    def close(self) -> None:
        if self._fd >= 0 and not self._external_fd:
            try:
                self.release_all()
                if self._created:
                    fcntl.ioctl(self._fd, UI_DEV_DESTROY)
            except OSError:
                pass
            os.close(self._fd)
        self._fd, self._created = -1, False

    def set_key(self, code: int, down: bool) -> None:
        if down:
            self._held.add(code)
        else:
            self._held.discard(code)
        n = self._n
        if n >= len(self._buf) - 2 * EVSZ:
            self._write(n)
            n = 0
        _EV.pack_into(self._buf, n, 0, 0, C.EV_KEY, code, 1 if down else 0)
        self._n = n + EVSZ

    def flush(self) -> None:
        n = self._n
        if not n:
            return
        _EV.pack_into(self._buf, n, 0, 0, C.EV_SYN, C.SYN_REPORT, 0)
        self._write(n + EVSZ)
        self._n = 0

    def release_all(self) -> None:
        for code in sorted(self._held):
            self.set_key(code, False)
        self.flush()

    def _write(self, nbytes: int) -> None:
        try:
            os.write(self._fd, self._mv[:nbytes])
        except BlockingIOError:
            self.dropped_writes += 1
        except OSError as e:
            if e.errno in (errno.EBADF, errno.ENODEV):
                self.dropped_writes += 1
            else:
                raise
