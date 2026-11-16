"""A short description of the device's "operating protocol" for the Web UI: how it is connected, the report format, and what it can do.

Taken from DeviceInfo (bus, backend, interface) and the layout (Report ID, report size, handshake, rumble,
gyro). It guesses nothing: if there is no layout, it shows only what is known about the connection.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from ..i18n import translate

_BUS = {3: "USB", 5: "Bluetooth", 0x18: "I²C", 0x19: 'host'}
_BACKEND = {"hidraw": 'hidraw (raw HID reports)', "evdev": 'evdev (kernel events)', "hidapi": "hidapi", "winhid": 'WinAPI HID (native)'}


def _m(template: str, *args) -> list:
    """A translatable message as JSON-friendly data: [English template, *args]; see render_protocol()."""
    return [template, *args]


def describe_protocol(info, layout=None) -> Dict[str, Any]:
    """Language-neutral description: {"summary": [msg, …], "items": [[msg, msg], …]}, where msg = [template, *args].
    The web process renders it in the language of each browser (render_protocol)."""
    items: List[list] = []

    def add(k: list, v: Optional[list]) -> None:
        if v:
            items.append([k, v])

    bus = _BUS.get(getattr(info, "bus", None))
    add(_m('Connection'), _m(bus) if bus else None)
    add(_m('Reading'), _m(_BACKEND.get(info.backend, info.backend)))
    if info.interface is not None:
        add(_m('HID interface'), _m(str(info.interface)))
    if layout is not None:
        add(_m('Layout'), _m(layout.name))
        if info.backend != "evdev":
            if layout.report_id is not None:
                add(_m('Report ID'), _m(f"0x{layout.report_id:02X}"))
            if layout.read_size:
                add(_m('Report size'), _m('{0} B', layout.read_size))
            sync = getattr(layout, "sync", None)
            if sync:
                add(_m('Report marker'), _m('byte {0} = 0x{1:02X}', sync[0], sync[1]))
            hs = [s for s in (layout.handshake or []) if getattr(s, "op", "") not in ("sleep_ms",)]
            add(_m('Handshake'), _m('{0} steps', len(hs)) if hs else _m('not needed'))
        rumble = getattr(layout, "rumble", None)
        if rumble is not None:
            add(_m('Rumble'), _m('hidraw report with CRC-32') if getattr(rumble, "crc32_seed", None) is not None else _m('hidraw report'))
        elif info.backend == "evdev":
            add(_m('Rumble'), _m('kernel FF (if the device supports it)'))
        else:
            add(_m('Rumble'), _m('none'))
        add(_m('Gyro'), _m('yes') if getattr(layout, "gyro", None) else _m('no'))
    parts = [_m(layout.name) if layout is not None else _m(info.name or 'unknown device'),
             _m(bus) if bus else None,
             _m(info.backend) if info.backend else None]
    if layout is not None and info.backend != "evdev" and layout.report_id is not None:
        parts.append(_m('report 0x{0:02X} ({1} B)', layout.report_id, layout.read_size) if layout.read_size
                     else _m('report 0x{0:02X}', layout.report_id))
    if layout is None:
        parts.append(_m('no layout'))
    return {"summary": [p for p in parts if p], "items": items}


def render_protocol(desc: Dict[str, Any], lang: Optional[str] = "en") -> Dict[str, Any]:
    """-> {"summary": "…", "items": [[name, value], …]} with plain strings in the language ``lang``."""
    def r(m: Any) -> str:
        return translate(lang, m[0], *m[1:]) if isinstance(m, list) and m else str(m)

    if isinstance(desc.get("summary"), str):          # already rendered
        return desc
    return {"summary": " · ".join(r(p) for p in desc["summary"]),
            "items": [[r(k), r(v)] for k, v in desc["items"]]}
