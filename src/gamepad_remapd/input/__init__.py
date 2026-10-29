"""Фабрика вхідних backend-ів: hidraw + evdev (Linux) або hidapi (Windows/macOS/резерв)."""
from __future__ import annotations

import os
from typing import Callable, List

from .base import BackendUnavailable, DeviceGone, DeviceInfo, InputDevice, run_handshake

__all__ = ["BackendUnavailable", "DeviceGone", "DeviceInfo", "InputDevice", "run_handshake",
           "enumerate_devices", "open_device", "drop_shadowed_evdev"]


def _linux() -> bool:
    return os.name == "posix" and os.path.isdir("/sys/class/hidraw")


def _default_backend() -> str:
    return "auto"


def enumerate_devices(backend: str = "auto") -> List[DeviceInfo]:
    """auto: Linux -> hidraw + evdev (evdev-вузли відсіює `drop_shadowed_evdev`), інакше hidapi."""
    if backend == "auto":
        backend = "linux" if _linux() else "hidapi"
    if backend in ("linux", "hidraw", "evdev"):
        out: List[DeviceInfo] = []
        if backend in ("linux", "hidraw"):
            from .hidraw_linux import enumerate_hidraw
            out += enumerate_hidraw()
        if backend in ("linux", "evdev"):
            from .evdev_linux import enumerate_evdev
            out += enumerate_evdev()
        return out
    if backend == "winhid":
        from .winhid import enumerate_winhid
        return enumerate_winhid()
    from .hidapi_backend import enumerate_hidapi
    try:
        return enumerate_hidapi()
    except BackendUnavailable:
        if os.name != "nt":
            raise
        from .winhid import enumerate_winhid          # Windows без hidapi: рідний WinAPI-backend (без компіляції)
        return enumerate_winhid()


def drop_shadowed_evdev(infos: List[DeviceInfo], has_layout: Callable[[DeviceInfo], bool]) -> List[DeviceInfo]:
    """Той самий фізичний геймпад часто видно і як hidraw, і як evdev. Якщо hidraw-інтерфейс із тим
    самим VID:PID має робочу розкладку, evdev-вузол приховуємо (інакше вийшов би дубль). Якщо
    розкладки для hidraw немає (XInput-режим, xpad, Switch Pro ...), лишається evdev."""
    covered = {(i.vid, i.pid) for i in infos if i.backend != "evdev" and has_layout(i)}
    return [i for i in infos if not (i.backend == "evdev" and (i.vid, i.pid) in covered)]


def open_device(info: DeviceInfo, read_size: int = 64) -> InputDevice:
    """Створює і відкриває пристрій відповідним backend-ом."""
    if info.backend == "hidraw":
        from .hidraw_linux import HidrawDevice
        dev: InputDevice = HidrawDevice(info, read_size)
    elif info.backend == "evdev":
        from .evdev_linux import EvdevDevice
        dev = EvdevDevice(info, read_size)
    elif info.backend == "winhid":
        from .winhid import WinHidDevice
        dev = WinHidDevice(info, read_size)
    else:
        from .hidapi_backend import HidapiDevice
        dev = HidapiDevice(info, read_size)
    dev.open()
    return dev
