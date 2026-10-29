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
    """Правила для hidraw: USB і Bluetooth. VID/PID — шістнадцяткові рядки (як у lsusb).

    Bluetooth-hidraw не має USB-предка (немає idVendor/idProduct), тож шукаємо за ім'ям HID-пристрою
    `0005:<VID>:<PID>.<N>` (0005 = BUS_BLUETOOTH)."""
    vid = vid.lower().zfill(4)
    match = f'ATTRS{{idVendor}}=="{vid}"'
    kernels = f"0005:{vid.upper()}:"
    if pid:
        pid = pid.lower().zfill(4)
        match += f', ATTRS{{idProduct}}=="{pid}"'
        kernels += f"{pid.upper()}.*"
    else:
        kernels += "*"
    access = 'TAG+="uaccess", GROUP="input", MODE="0660"'
    line = (f'SUBSYSTEM=="hidraw", SUBSYSTEMS=="usb", {match}, {access}\n'
            f'SUBSYSTEM=="hidraw", KERNELS=="{kernels}", {access}')
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


def hide_rule(vid: str, pid: str, comment: str = "") -> str:
    """Правило «сховати оригінальний геймпад від програм»: прибирає з його evdev-вузлів ознаки геймпада
    (ID_INPUT_JOYSTICK...), за якими SDL, Steam, Chrome та libinput їх знаходять. Демон це НЕ ламає: він читає
    hidraw/evdev за шляхом, а не через udev-властивості. Віртуальні пристрої (uinput, /devices/virtual) не чіпаємо —
    інакше сховався б і наш власний віртуальний геймпад, якщо він має той самий VID:PID (output_kind=ds4).

    Не ховає сам /dev/hidrawN: SDL з HIDAPI читає hidraw напряму. Для таких програм — змінна середовища
    SDL_GAMECONTROLLER_IGNORE_DEVICES=0xVID/0xPID (див. README)."""
    vid, pid = vid.lower().zfill(4), pid.lower().zfill(4)
    return (f"# {comment or 'сховати оригінал'} ({vid}:{pid}): програми бачать лише віртуальний геймпад\n"
            f'SUBSYSTEM=="input", KERNEL=="event[0-9]*", DEVPATH!="/devices/virtual/*", '
            f'ATTRS{{id/vendor}}=="{vid}", ATTRS{{id/product}}=="{pid}", '
            'ENV{ID_INPUT_JOYSTICK}="", ENV{ID_INPUT_ACCELEROMETER}="", ENV{ID_INPUT_TOUCHPAD}="", '
            'ENV{LIBINPUT_IGNORE_DEVICE}="1"\n'
            f"# SDL з HIDAPI читає hidraw напряму; для таких ігор: SDL_GAMECONTROLLER_IGNORE_DEVICES=0x{vid}/0x{pid}\n")


def generate_hide(pairs: Iterable[Tuple[str, str, str]]) -> str:
    seen, parts = set(), [
        "# /etc/udev/rules.d/69-gamepad-remapd-hide.rules\n"
        "# Згенеровано: gamepad-remapd udev-rules --hide VID:PID\n"
        "# Номер 69: після 60-input-id (що ставить ID_INPUT_JOYSTICK) і ДО 70-uaccess (ACL за цією властивістю).\n"
        "# Застосувати: sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=input\n"
        "# Потім перепідключіть геймпад. Скасувати: видалити файл і повторити trigger.\n"]
    for vid, pid, comment in pairs:
        key = (vid.lower().zfill(4), pid.lower().zfill(4))
        if key not in seen:
            seen.add(key)
            parts.append(hide_rule(vid, pid, comment))
    return "\n".join(parts)
