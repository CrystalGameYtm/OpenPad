"""Які вихідні протоколи можна запропонувати для пристрою: емуляція XInput чи «штатний» режим."""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

SONY_VID = 0x054C


def native_kind(vid: int, layout_id: Optional[str]) -> str:
    """DualShock-сумісні (Sony VID або розкладка sony_ds4*/ds4) -> "ds4", усі інші -> загальний "dinput"."""
    if vid == SONY_VID or (layout_id and "ds4" in layout_id.lower()):
        return "ds4"
    return "dinput"


def output_choices(vid: int, layout_id: Optional[str], windows: Optional[bool] = None) -> List[Dict[str, Any]]:
    windows = (os.name == "nt") if windows is None else windows
    xin = {"id": "xbox360", "label": "Емуляція XInput (Xbox 360)",
           "note": "Підходить майже всім іграм (XInput/SDL). Рекомендовано за замовчуванням."}
    if windows:
        return [dict(xin, note="На Windows вихід — віртуальний Xbox 360 (ViGEm).")]
    kind = native_kind(vid, layout_id)
    nat = ({"id": "ds4", "label": "Штатний: DualShock 4",
            "note": "Віртуальний DS4 (Sony 054c:09cc).",
            "warn": "Експериментально: SDL з HIDAPI віддає геймпади з VID:PID DualShock власному драйверу й може не показати такий пристрій. Для емуляторів і SDL-ігор обирайте XInput."}
           if kind == "ds4" else
           {"id": "dinput", "label": "Штатний: DInput (загальний геймпад)",
            "note": "Класичний evdev-джойстик (кнопки BTN_TRIGGER…, осі X/Y/Z/RZ) — як сам DInput-геймпад.",
            "warn": "Експериментально, у більшості ігор НЕ працює: для такого пристрою в SDL немає мапінгу, тож гра/емулятор не розпізнає його як геймпад (або плутає осі й кнопки). Для ігор і емуляторів обирайте «Емуляція XInput»."})
    return [xin, nat]


def warn_for(choices: List[Dict[str, Any]], kind: str) -> Optional[str]:
    for c in choices:
        if c["id"] == kind:
            return c.get("warn")
    return None


def pick_kind(wanted: Optional[str], choices: List[Dict[str, Any]], fallback: str) -> str:
    ids = [c["id"] for c in choices]
    for k in (wanted, fallback):
        if k in ids:
            return k          # type: ignore[return-value]
    return ids[0]
