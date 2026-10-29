"""Цілі «клавіатура/миша» для button_map: `KEY_F`, `KEY_SPACE`, `KEY_LEFTSHIFT`, `MOUSE_LEFT` ...

Кнопка геймпада в профілі може віддавати не кнопку віртуального геймпада, а клавішу клавіатури
чи кнопку миші (окремий віртуальний пристрій uinput). Ім'я цілі — `KEY_<клавіша>` з таблиці
logic/keyboard.py (літери, цифри, F1–F12, стрілки, модифікатори, numpad ...), кілька медіаклавіш або
`MOUSE_LEFT|RIGHT|MIDDLE|SIDE|EXTRA`.
"""
from __future__ import annotations

from typing import Dict, Optional


# Основна таблиця клавіш (linux/input-event-codes.h). Навмисно власна, а не імпорт з logic/keyboard.py:
# цілі KEY_* працюють і без модуля «клавіатура як геймпад». Збіг із keyboard.py перевіряє tests/test_key_targets.py.
_KEYS: Dict[str, int] = {"ESC": 1, "MINUS": 12, "EQUAL": 13, "BACKSPACE": 14, "TAB": 15, "LEFTBRACE": 26,
                         "RIGHTBRACE": 27, "ENTER": 28, "LEFTCTRL": 29, "SEMICOLON": 39, "APOSTROPHE": 40,
                         "GRAVE": 41, "LEFTSHIFT": 42, "BACKSLASH": 43, "COMMA": 51, "DOT": 52, "SLASH": 53,
                         "RIGHTSHIFT": 54, "KPASTERISK": 55, "LEFTALT": 56, "SPACE": 57, "CAPSLOCK": 58,
                         "NUMLOCK": 69, "SCROLLLOCK": 70, "KPMINUS": 74, "KPPLUS": 78, "KPDOT": 83,
                         "KPENTER": 96, "RIGHTCTRL": 97, "KPSLASH": 98, "RIGHTALT": 100, "HOME": 102,
                         "UP": 103, "PAGEUP": 104, "LEFT": 105, "RIGHT": 106, "END": 107, "DOWN": 108,
                         "PAGEDOWN": 109, "INSERT": 110, "DELETE": 111, "PAUSE": 119, "LEFTMETA": 125,
                         "RIGHTMETA": 126}
for _i, _c in enumerate("1234567890"):
    _KEYS[_c] = 2 + _i
for _i, _c in enumerate("QWERTYUIOP"):
    _KEYS[_c] = 16 + _i
for _i, _c in enumerate("ASDFGHJKL"):
    _KEYS[_c] = 30 + _i
for _i, _c in enumerate("ZXCVBNM"):
    _KEYS[_c] = 44 + _i
for _i in range(1, 11):
    _KEYS[f"F{_i}"] = 58 + _i
_KEYS["F11"], _KEYS["F12"] = 87, 88
for _i, _n in enumerate(("7", "8", "9"), 71):
    _KEYS["KP" + _n] = _i
for _i, _n in enumerate(("4", "5", "6"), 75):
    _KEYS["KP" + _n] = _i
for _i, _n in enumerate(("1", "2", "3", "0"), 79):
    _KEYS["KP" + _n] = _i

MOUSE_TARGETS: Dict[str, int] = {"MOUSE_LEFT": 0x110, "MOUSE_RIGHT": 0x111, "MOUSE_MIDDLE": 0x112,
                                 "MOUSE_SIDE": 0x113, "MOUSE_EXTRA": 0x114}

# Додаткові клавіші, яких немає в ігровій таблиці keyboard.py (медіа, меню, PrintScreen, F13–F24).
_EXTRA_KEYS: Dict[str, int] = {"MUTE": 113, "VOLUMEDOWN": 114, "VOLUMEUP": 115, "MENU": 139, "SYSRQ": 99,
                               "PLAYPAUSE": 164, "STOPCD": 166, "NEXTSONG": 163, "PREVIOUSSONG": 165,
                               "F13": 183, "F14": 184, "F15": 185, "F16": 186, "F17": 187, "F18": 188,
                               "F19": 189, "F20": 190, "F21": 191, "F22": 192, "F23": 193, "F24": 194}


def key_target_code(name: str) -> Optional[int]:
    """«KEY_F» / «MOUSE_LEFT» -> код evdev, інакше None (це не ціль клавіатури/миші)."""
    n = str(name).strip().upper()
    if n in MOUSE_TARGETS:
        return MOUSE_TARGETS[n]
    if n.startswith("KEY_"):
        k = n[4:]
        return _KEYS.get(k, _EXTRA_KEYS.get(k))
    return None


def all_key_codes() -> list:
    """Усі коди, які віртуальна клавіатура мусить оголосити заздалегідь (uinput не вміє додавати їх потім)."""
    return sorted(set(_KEYS.values()) | set(_EXTRA_KEYS.values()) | set(MOUSE_TARGETS.values()))


def key_target_names() -> list:
    """Для підказок у Web UI."""
    return sorted("KEY_" + k for k in {**_KEYS, **_EXTRA_KEYS}) + sorted(MOUSE_TARGETS)
