"""Тестові двійники: фейковий HID-пристрій, декодер подій uinput, збирач DS4-звітів."""
from __future__ import annotations

import os
import queue
import select
import struct
import time
from typing import List, Optional, Tuple

from gamepad_remapd import constants as C
from gamepad_remapd.input.base import DeviceGone, DeviceInfo, InputDevice
from gamepad_remapd.output.uinput_linux import EVSZ, SPECS, UinputPad

_WAKE, _GONE = object(), object()
EV = struct.Struct("llHHi")


class FakeDevice(InputDevice):
    """Те саме, що hidraw: блокуюче read(), wake(), feature/output-звіти (запам'ятовуються)."""

    def __init__(self, info: DeviceInfo, read_size: int = 64):
        super().__init__(info, read_size)
        self.q: "queue.Queue" = queue.Queue()
        self.features: List[bytes] = []
        self.outputs: List[bytes] = []
        self.closed = False

    def open(self) -> None: ...
    def close(self) -> None: self.closed = True

    def read(self) -> Optional[bytes]:
        try:
            item = self.q.get(timeout=0.2)
        except queue.Empty:
            return None
        if item is _GONE:
            raise DeviceGone("fake unplug")
        return None if item is _WAKE else item

    def wake(self) -> None: self.q.put(_WAKE)
    def send_feature(self, data: bytes) -> None: self.features.append(bytes(data))
    def get_feature(self, report_id: int, length: int) -> bytes: return bytes(length)
    def write_output(self, data: bytes) -> None: self.outputs.append(bytes(data))
    def inject(self, raw: bytes) -> None: self.q.put(bytes(raw))
    def unplug(self) -> None: self.q.put(_GONE)


def ds4_info(path: str = "/dev/hidraw7") -> DeviceInfo:
    return DeviceInfo(path=path, vid=0x054C, pid=0x05C4, name="Wireless Controller",
                      interface=0, bus=3, backend="hidraw")


def ds4_report(cross=False, circle=False, l1=False, hat=8, lx=128, ly=128, rx=128, ry=128,
               l2=0, r2=0, gyro_x=0, gyro_y=0, ps=False, l2_btn=False) -> bytes:
    """Звіт DS4 USB (report 0x01, 64 байти) за розкладкою sony_ds4_usb."""
    b = bytearray(64)
    b[0] = 1
    b[1], b[2], b[3], b[4] = lx, ly, rx, ry
    b[5] = (hat & 0x0F) | (0x20 if cross else 0) | (0x40 if circle else 0)
    b[6] = (0x01 if l1 else 0) | (0x04 if l2_btn else 0)
    b[7] = 0x01 if ps else 0
    b[8], b[9] = l2, r2
    struct.pack_into("<hh", b, 13, gyro_x, gyro_y)
    return bytes(b)


def make_pad(kind: str = "xbox360") -> Tuple[UinputPad, int]:
    """UinputPad, що пише у pipe замість /dev/uinput. Повертає (pad, read_fd)."""
    r, w = os.pipe()
    os.set_blocking(r, False)
    return UinputPad(SPECS[kind], fd=w), r


def drain(r: int, timeout: float = 0.0) -> List[Tuple[int, int, int]]:
    """Зчитує всі накопичені події (type, code, value)."""
    out: List[Tuple[int, int, int]] = []
    end = time.monotonic() + timeout
    while True:
        rl, _, _ = select.select([r], [], [], max(0.0, end - time.monotonic()) if timeout else 0)
        if not rl:
            break
        try:
            data = os.read(r, 65536)
        except BlockingIOError:
            break
        for off in range(0, len(data) - EVSZ + 1, EVSZ):
            _, _, t, c, v = EV.unpack_from(data, off)
            out.append((t, c, v))
        if timeout == 0:
            continue
    return out


def frames(events: List[Tuple[int, int, int]]) -> List[List[Tuple[int, int, int]]]:
    """Ділить потік подій на кадри за SYN_REPORT."""
    res, cur = [], []
    for e in events:
        if e[0] == C.EV_SYN:
            res.append(cur)
            cur = []
        else:
            cur.append(e)
    return res


def wait_frames(r: int, n: int = 1, timeout: float = 2.0) -> List[List[Tuple[int, int, int]]]:
    """Чекає, доки в pipe з'явиться принаймні n кадрів."""
    acc: List[Tuple[int, int, int]] = []
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        acc += drain(r, 0.05)
        if len(frames(acc)) >= n:
            break
    return frames(acc)
