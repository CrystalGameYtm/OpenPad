"""Windows: сховати оригінальний геймпад від ігор через HidHide (https://github.com/nefarius/HidHide).

Без цього гра бачить і фізичний геймпад, і віртуальний (дубль). HidHide — драйвер, що ховає HID-пристрої від усіх
програм, крім дозволених. Цей модуль лише ГЕНЕРУЄ команди HidHideCLI для вашого пристрою (не виконує їх без `--run`):

    gamepad-remapd hidhide --vid-pid 054c:09cc
    gamepad-remapd hidhide --vid-pid 054c:09cc --run        # виконати (потрібні права адміністратора)

Три кроки: сховати пристрій за instance-path, дозволити процес демона (python.exe) бачити приховане, увімкнути
приховування. Шлях пристрою hidapi (`\\\\?\\HID#VID_054C&PID_09CC#...#{guid}`) перетворюється на instance-path
(`HID\\VID_054C&PID_09CC\\...`). Потрібен встановлений HidHide (CLI: HidHideCLI.exe).

УВАГА: написано за документацією HidHide, на Windows ще не запускалось.
"""
from __future__ import annotations

import re
import subprocess
import sys
from typing import Callable, List, Optional

_GUID_TAIL = re.compile(r"#\{[0-9a-fA-F-]{36}\}$")


def instance_path(hid_path: str) -> str:
    """`\\\\?\\HID#VID_054C&PID_09CC#7&2a1b&0&0000#{4d1e55b2-...}` -> `HID\\VID_054C&PID_09CC\\7&2a1b&0&0000`."""
    p = hid_path.strip()
    if p.startswith("\\\\?\\") or p.startswith("\\\\.\\"):
        p = p[4:]
    p = _GUID_TAIL.sub("", p)
    return p.replace("#", "\\")


def commands(instance_paths: List[str], python_exe: Optional[str] = None, cli: str = "HidHideCLI") -> List[List[str]]:
    exe = python_exe or sys.executable
    cmds = [[cli, "--app-reg", exe]]                     # демон бачить приховані пристрої
    cmds += [[cli, "--dev-hide", ip] for ip in instance_paths]
    cmds.append([cli, "--cloak-on"])
    return cmds


def run(vid: int, pid: int, hid_paths: List[str], *, execute: bool = False, python_exe: Optional[str] = None,
        cli: str = "HidHideCLI", runner: Callable = subprocess.call, out=None) -> int:
    out = out or sys.stdout
    if not hid_paths:
        print(f"пристроїв {vid:04x}:{pid:04x} не знайдено (запустіть `gamepad-remapd list`)", file=sys.stderr)
        return 1
    ips = sorted({instance_path(p) for p in hid_paths})
    cmds = commands(ips, python_exe, cli)
    for c in cmds:
        line = " ".join(f'"{a}"' if " " in a else a for a in c)
        print(line, file=out)
        if execute:
            rc = runner(c)
            if rc != 0:
                print(f"команда завершилась з кодом {rc}; потрібні права адміністратора й встановлений HidHide",
                      file=sys.stderr)
                return rc
    return 0
