"""Вібрація через SDL3 — еталон «воно працює» і спосіб підгледіти, що саме SDL шле геймпаду.

    gamepad-remapd sdl-rumble              # сильний / слабкий / обидва мотори по 1 с на кожному геймпаді SDL
    gamepad-remapd sdl-rumble --trace      # те саме під strace: друкує hex усіх записів SDL у hidraw

Потрібні libSDL3 (Fedora: `sudo dnf install SDL3`) і для --trace — strace. Закрийте serve/Citron на час
перевірки. Нічого не змінює в пристрої, крім запуску моторів. Навмисно використовує hidraw-бекенд
HIDAPI (SDL_HIDAPI_LIBUSB=0), щоб записи були видні через strace; USB-трафік (ресивери) дивіться usbmon.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import re
import subprocess
import sys
import tempfile
import time
from typing import Callable, Dict, List, Optional, TextIO, Tuple

SDL_INIT_JOYSTICK = 0x200
SDL_INIT_GAMEPAD = 0x2000
HINTS = (b"SDL_JOYSTICK_HIDAPI", b"SDL_JOYSTICK_HIDAPI_FLYDIGI", b"SDL_JOYSTICK_HIDAPI_PS4",
         b"SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS")


def load_sdl():
    for n in ("libSDL3.so.0", ctypes.util.find_library("SDL3") or ""):
        if n:
            try:
                return ctypes.CDLL(n)
            except OSError:
                pass
    return None


def _setup(lib) -> None:
    lib.SDL_GetError.restype = ctypes.c_char_p
    lib.SDL_SetHint.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
    lib.SDL_GetGamepads.restype = ctypes.POINTER(ctypes.c_uint32)
    lib.SDL_GetGamepads.argtypes = [ctypes.POINTER(ctypes.c_int)]
    lib.SDL_OpenGamepad.restype = ctypes.c_void_p
    lib.SDL_OpenGamepad.argtypes = [ctypes.c_uint32]
    lib.SDL_GetGamepadName.restype = ctypes.c_char_p
    lib.SDL_GetGamepadName.argtypes = [ctypes.c_void_p]
    lib.SDL_RumbleGamepad.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint16, ctypes.c_uint32]
    lib.SDL_RumbleGamepad.restype = ctypes.c_bool


def run(lib=None, out: Optional[TextIO] = None, hold: float = 1.2, sleep: Callable = time.sleep) -> int:
    out = out or sys.stdout
    lib = lib or load_sdl()
    if lib is None:
        print("libSDL3 не знайдено: sudo dnf install SDL3", file=sys.stderr)
        return 1
    _setup(lib)
    for h in HINTS:
        lib.SDL_SetHint(h, b"1")
    lib.SDL_SetHint(b"SDL_HIDAPI_LIBUSB", b"0")
    if not lib.SDL_Init(SDL_INIT_GAMEPAD | SDL_INIT_JOYSTICK):
        print("SDL_Init: " + lib.SDL_GetError().decode(), file=sys.stderr)
        return 1
    for _ in range(30):
        lib.SDL_PumpEvents()
        sleep(0.1)
    n = ctypes.c_int(0)
    ids = lib.SDL_GetGamepads(ctypes.byref(n))
    print(f"геймпадів: {n.value}", file=out, flush=True)
    for i in range(n.value):
        g = lib.SDL_OpenGamepad(ids[i])
        name = lib.SDL_GetGamepadName(g)
        print(f"[{ids[i]}] {name.decode() if name else '?'}", file=out, flush=True)
        for lo, hi in ((0xFFFF, 0), (0, 0xFFFF), (0xFFFF, 0xFFFF)):
            ok = lib.SDL_RumbleGamepad(g, lo, hi, 1000)
            err = "" if ok else " " + (lib.SDL_GetError() or b"").decode()
            print(f"  rumble low={lo:04x} high={hi:04x}: {'OK' if ok else 'ПОМИЛКА' + err}", file=out, flush=True)
            for _ in range(int(hold / 0.1)):
                lib.SDL_PumpEvents()
                sleep(0.1)
    return 0 if n.value else 2


# ---------------------------------------------------------------- розбір strace
_ESC = re.compile(rb'\\(x[0-9a-fA-F]{2}|[0-7]{1,3}|.)', re.S)
_SIMPLE = {b"n": 10, b"t": 9, b"r": 13, b'"': 34, b"\\": 92, b"a": 7, b"b": 8, b"f": 12, b"v": 11}


def decode_strace_string(s: str) -> bytes:
    """Вміст рядка зі strace ("\\21\\300\\0...") -> байти."""
    raw = s.encode("latin-1", "replace")

    def one(m):
        t = m.group(1)
        if t[:1] == b"x":
            return bytes([int(t[1:], 16)])
        if t[:1].isdigit():
            return bytes([int(t, 8) & 0xFF])
        return bytes([_SIMPLE.get(t, t[0])])
    return _ESC.sub(one, raw)


_OPEN = re.compile(r'^(?:\d+\s+)?openat\(.*?"(/dev/hidraw\d+)".*\)\s*=\s*(\d+)')
_WRITE = re.compile(r'^(?:\d+\s+)?write\((\d+), "((?:[^"\\]|\\.)*)"(\.\.\.)?, (\d+)\)\s*=\s*(-?\d+)')
_CLOSE = re.compile(r'^(?:\d+\s+)?close\((\d+)\)')


def hidraw_writes(trace_lines) -> List[Tuple[str, bytes, int]]:
    """Із рядків strace -> [(шлях hidraw, записані байти, результат write)]."""
    fds: Dict[int, str] = {}
    out = []
    for line in trace_lines:
        m = _OPEN.match(line)
        if m:
            fds[int(m.group(2))] = m.group(1)
            continue
        m = _CLOSE.match(line)
        if m:
            fds.pop(int(m.group(1)), None)
            continue
        m = _WRITE.match(line)
        if m and int(m.group(1)) in fds:
            out.append((fds[int(m.group(1))], decode_strace_string(m.group(2)), int(m.group(5))))
    return out


def run_traced(out: Optional[TextIO] = None) -> int:
    out = out or sys.stdout
    with tempfile.TemporaryDirectory() as d:
        log = os.path.join(d, "strace.txt")
        cmd = ["strace", "-f", "-s", "512", "-e", "trace=openat,close,write", "-o", log,
               sys.executable, "-m", "gamepad_remapd", "sdl-rumble"]
        try:
            rc = subprocess.call(cmd)
        except FileNotFoundError:
            print("strace не знайдено: sudo dnf install strace", file=sys.stderr)
            return 1
        try:
            with open(log, encoding="latin-1") as f:
                lines = f.read().splitlines()
        except OSError:
            lines = []
    writes = hidraw_writes(lines)
    print("\nЗаписи SDL у hidraw:", file=out)
    seen = set()
    for path, data, res in writes:
        key = (path, data)
        mark = "" if key not in seen else "  (повтор)"
        seen.add(key)
        print(f"  {path} [{len(data)} Б] -> {res}{mark}\n    {data.hex(' ')}", file=out)
    if not writes:
        print("  (жодного запису: SDL не писав у hidraw)", file=out)
    return rc
