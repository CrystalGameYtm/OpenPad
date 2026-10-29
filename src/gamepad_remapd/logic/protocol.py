"""Короткий опис «протоколу роботи» пристрою для Web UI: як підключено, який формат звітів, що вміє.

Береться з DeviceInfo (шина, backend, інтерфейс) і розкладки (Report ID, розмір звіту, handshake, вібрація,
гіроскоп). Нічого не вгадує: якщо розкладки немає — показує лише те, що відомо про підключення.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

_BUS = {3: "USB", 5: "Bluetooth", 0x18: "I²C", 0x19: "хост"}
_BACKEND = {"hidraw": "hidraw (сирі HID-звіти)", "evdev": "evdev (події ядра)", "hidapi": "hidapi", "winhid": "WinAPI HID (рідний)"}


def describe_protocol(info, layout=None) -> Dict[str, Any]:
    """-> {"summary": "DualShock 4 · Bluetooth · hidraw · звіт 0x11 (78 Б)…", "items": [[назва, значення], …]}"""
    items: List[List[str]] = []

    def add(k: str, v: Optional[str]) -> None:
        if v:
            items.append([k, v])

    bus = _BUS.get(getattr(info, "bus", None))
    add("Підключення", bus)
    add("Читання", _BACKEND.get(info.backend, info.backend))
    if info.interface is not None:
        add("HID-інтерфейс", str(info.interface))
    if layout is not None:
        add("Розкладка", layout.name)
        if info.backend != "evdev":
            if layout.report_id is not None:
                add("Report ID", f"0x{layout.report_id:02X}")
            if layout.read_size:
                add("Розмір звіту", f"{layout.read_size} Б")
            sync = getattr(layout, "sync", None)
            if sync:
                add("Маркер звіту", f"байт {sync[0]} = 0x{sync[1]:02X}")
            hs = [s for s in (layout.handshake or []) if getattr(s, "op", "") not in ("sleep_ms",)]
            add("Handshake", f"{len(hs)} кр." if hs else "не потрібен")
        rumble = getattr(layout, "rumble", None)
        if rumble is not None:
            add("Вібрація", "hidraw-звіт з CRC-32" if getattr(rumble, "crc32_seed", None) is not None else "hidraw-звіт")
        elif info.backend == "evdev":
            add("Вібрація", "ядровий FF (якщо пристрій підтримує)")
        else:
            add("Вібрація", "немає")
        add("Гіроскоп", "так" if getattr(layout, "gyro", None) else "ні")
    parts = [layout.name if layout is not None else (info.name or "невідомий пристрій"), bus,
             info.backend if info.backend else None]
    if layout is not None and info.backend != "evdev" and layout.report_id is not None:
        parts.append(f"звіт 0x{layout.report_id:02X}" + (f" ({layout.read_size} Б)" if layout.read_size else ""))
    if layout is None:
        parts.append("розкладки немає")
    return {"summary": " · ".join(p for p in parts if p), "items": items}
