"""What SDL3 really sees (emulators, games): the list of joysticks with their path, VID:PID and whether it is a "gamepad".

    gamepad-remapd sdl-list

SDL settings are NOT changed (the same defaults as the programs use), so this is an honest hiding check: the list
must contain only the VIRTUAL gamepad (/devices/virtual/…), with no original. Needs libSDL3 (Fedora: sudo dnf install SDL3).
"""
from __future__ import annotations

import ctypes
import sys
import time
from typing import Callable, Optional, TextIO

from .sdl_rumble import SDL_INIT_GAMEPAD, SDL_INIT_JOYSTICK, load_sdl
from ..i18n import tr


def _setup(lib) -> None:
    lib.SDL_GetError.restype = ctypes.c_char_p
    lib.SDL_GetJoysticks.restype = ctypes.POINTER(ctypes.c_uint32)
    lib.SDL_GetJoysticks.argtypes = [ctypes.POINTER(ctypes.c_int)]
    for fn in ("SDL_GetJoystickNameForID", "SDL_GetJoystickPathForID"):
        getattr(lib, fn).restype = ctypes.c_char_p
        getattr(lib, fn).argtypes = [ctypes.c_uint32]
    for fn in ("SDL_GetJoystickVendorForID", "SDL_GetJoystickProductForID"):
        getattr(lib, fn).restype = ctypes.c_uint16
        getattr(lib, fn).argtypes = [ctypes.c_uint32]
    lib.SDL_IsGamepad.restype = ctypes.c_bool
    lib.SDL_IsGamepad.argtypes = [ctypes.c_uint32]


def run(lib=None, out: Optional[TextIO] = None, sleep: Callable = time.sleep) -> int:
    out = out or sys.stdout
    lib = lib or load_sdl()
    if lib is None:
        print(tr('libSDL3 not found: sudo dnf install SDL3'), file=sys.stderr)
        return 1
    _setup(lib)
    if not lib.SDL_Init(SDL_INIT_GAMEPAD | SDL_INIT_JOYSTICK):
        print("SDL_Init: " + (lib.SDL_GetError() or b"").decode(), file=sys.stderr)
        return 1
    for _ in range(20):                                   
        lib.SDL_PumpEvents()
        sleep(0.1)
    n = ctypes.c_int(0)
    ids = lib.SDL_GetJoysticks(ctypes.byref(n))
    print(tr('SDL sees joysticks: {0}', n.value), file=out)
    for i in range(n.value):
        jid = ids[i]
        name = (lib.SDL_GetJoystickNameForID(jid) or b"?").decode(errors="replace")
        path = (lib.SDL_GetJoystickPathForID(jid) or b"").decode(errors="replace")
        vp = f"{lib.SDL_GetJoystickVendorForID(jid):04x}:{lib.SDL_GetJoystickProductForID(jid):04x}"
        kind = tr('gamepad') if lib.SDL_IsGamepad(jid) else tr('joystick (no mapping)')
        print(f"  [{jid}] {name}  {vp}  {kind}  {path}", file=out)
    lib.SDL_Quit()
    return 0
