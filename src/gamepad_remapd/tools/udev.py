"""Генератор udev-правил для доступу користувача до hidraw та /dev/uinput.

Навіщо: за замовчуванням /dev/hidrawN і /dev/uinput належать root. Запускати
демон від root — погана ідея (зайві привілеї для сервісу, що слухає сокет і
читає сирі байти з USB). Правильно — видати доступ ТІЛЬКИ потрібним вузлам.

Два механізми, обидва увімкнено:
  * TAG+="uaccess"        — systemd-logind видає ACL поточному користувачу локальної
                            сесії (seat). Працює «з коробки» на десктопі, без груп.
  * GROUP="input", 0660   — для сервісів/SSH/headless-сесій без seat (користувач має
                            бути в групі `input`: sudo usermod -aG input $USER).
Правила ОБОВ'ЯЗКОВО мають ім'я з номером < 73 (uaccess обробляється у 73-seat-late.rules).
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

HEADER = """\
# /etc/udev/rules.d/70-gamepad-remapd.rules
# Згенеровано: gamepad-remapd udev-rules
#
# Після зміни:  sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=hidraw
# Перевірка:    udevadm test $(udevadm info -q path -n /dev/hidraw0) 2>&1 | grep -i -E 'uaccess|GROUP|MODE'
#               getfacl /dev/hidraw0      (має бути ваш користувач)
"""

UINPUT_RULE = """\
# Віртуальний геймпад: створення пристроїв через /dev/uinput.
# static_node=uinput — вузол з'являється ще до завантаження модуля (щоб права вже були).
KERNEL=="uinput", SUBSYSTEM=="misc", OPTIONS+="static_node=uinput", TAG+="uaccess", GROUP="input", MODE="0660"
"""


def hidraw_rule(vid: str, pid: Optional[str], comment: str = "") -> str:
    """Правило для hidraw USB-пристрою. VID/PID — шістнадцяткові рядки (як у lsusb)."""
    vid = vid.lower().zfill(4)
    match = f'ATTRS{{idVendor}}=="{vid}"'
    if pid:
        match += f', ATTRS{{idProduct}}=="{pid.lower().zfill(4)}"'
    line = f'SUBSYSTEM=="hidraw", SUBSYSTEMS=="usb", {match}, TAG+="uaccess", GROUP="input", MODE="0660"'
    return (f"# {comment}\n" if comment else "") + line + "\n"


def generate(pairs: Iterable[Tuple[str, Optional[str], str]]) -> str:
    """pairs: (vid, pid|None, коментар). Дублікати прибираються."""
    seen = set()
    parts: List[str] = [HEADER, UINPUT_RULE]
    for vid, pid, comment in pairs:
        key = (vid.lower().zfill(4), (pid or "").lower().zfill(4) if pid else None)
        if key in seen:
            continue
        seen.add(key)
        parts.append(hidraw_rule(vid, pid, comment))
    return "\n".join(parts)


def pairs_from_layouts(layouts: Dict[str, object]) -> List[Tuple[str, Optional[str], str]]:
    out = []
    for lay in layouts.values():
        if getattr(lay, "vid", None) is None:
            continue
        for pid in getattr(lay, "pids", ()):       # одна розкладка може мати кілька PID
            out.append((f"{lay.vid:04x}", f"{pid:04x}", lay.name))
    return out
