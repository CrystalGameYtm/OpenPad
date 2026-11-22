"""Input backend factory: hidraw + evdev (Linux) or hidapi (Windows/macOS/fallback)."""
from __future__ import annotations

import os
from typing import Callable, List

from .base import BackendUnavailable, DeviceGone, DeviceInfo, InputDevice, evdev_format, is_streaming, run_handshake

__all__ = ["BackendUnavailable", "DeviceGone", "DeviceInfo", "InputDevice", "is_streaming", "run_handshake",
           "enumerate_devices", "open_device", "drop_shadowed_evdev", "evdev_format"]


def _linux() -> bool:
    return os.name == "posix" and os.path.isdir("/sys/class/hidraw")


def _default_backend() -> str:
    return "auto"


def enumerate_devices(backend: str = "auto", xinput: str = "evdev") -> List[DeviceInfo]:
    """auto: Linux -> hidraw + evdev (evdev nodes are filtered by `drop_shadowed_evdev`), otherwise hidapi.

    xinput="usb" (Linux): wired XInput gamepads are read directly over USB instead of through the `xpad` evdev node,
    when the USB device node is accessible; otherwise the evdev path is kept.
    """
    if backend == "auto":
        backend = "linux" if _linux() else "hidapi"
    if backend in ("linux", "hidraw", "evdev"):
        out: List[DeviceInfo] = []
        if backend in ("linux", "hidraw"):
            from .hidraw_linux import enumerate_hidraw
            out += enumerate_hidraw()
        if backend in ("linux", "evdev"):
            from .evdev_linux import enumerate_evdev
            ev = enumerate_evdev()
            if xinput == "usb":
                from .xusb_linux import enumerate_xusb, evdev_node_of
                xu = enumerate_xusb()
                if xu:
                    ev = [i for i in ev if not evdev_node_of(i, xu)]
                    out += xu
            out += ev
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
        from .winhid import enumerate_winhid          # Windows without hidapi: the native WinAPI backend (no compilation needed)
        return enumerate_winhid()


def drop_shadowed_evdev(infos: List[DeviceInfo], has_layout: Callable[[DeviceInfo], bool]) -> List[DeviceInfo]:
    """The same physical gamepad is often visible both as hidraw and as evdev. If the hidraw interface with the same VID:PID
    has a working layout, we hide the evdev node (otherwise it would be a duplicate). If there is no hidraw layout
    (XInput mode, xpad, Switch Pro ...), the evdev node stays.
    """
    covered = {(i.vid, i.pid) for i in infos if i.backend != "evdev" and has_layout(i)}
    return [i for i in infos if not (i.backend == "evdev" and (i.vid, i.pid) in covered)]


def open_device(info: DeviceInfo, read_size: int = 64) -> InputDevice:
    """Creates and opens a device with the matching backend."""
    if info.backend == "hidraw":
        from .hidraw_linux import HidrawDevice
        dev: InputDevice = HidrawDevice(info, read_size)
    elif info.backend == "xusb":
        from .xusb_linux import XusbDevice
        dev = XusbDevice(info, read_size)
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
