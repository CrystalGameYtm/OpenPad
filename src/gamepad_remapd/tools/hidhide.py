"""Windows: hide the original gamepad from games via HidHide (https://github.com/nefarius/HidHide).

Without this the game sees both the physical gamepad and the virtual one (a duplicate). HidHide is a driver that hides HID devices from all
programs except the allowed ones. This module only GENERATES the HidHideCLI commands for your device (it does not run them without `--run`):

    gamepad-remapd hidhide --vid-pid 054c:09cc
    gamepad-remapd hidhide --vid-pid 054c:09cc --run        

Three steps: hide the device by its instance-path, allow the daemon process (python.exe) to see the hidden device, enable
hiding. The hidapi device path (`\\\\?\\HID#VID_054C&PID_09CC#...#{guid}`) is converted to an instance-path
(`HID\\VID_054C&PID_09CC\\...`). Requires HidHide installed (CLI: HidHideCLI.exe).

WARNING: written from the HidHide documentation; not yet run on Windows.
"""
from __future__ import annotations

import re
import subprocess
import sys
from typing import Callable, List, Optional
from ..i18n import tr

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
    cmds = [[cli, "--app-reg", exe]]                     
    cmds += [[cli, "--dev-hide", ip] for ip in instance_paths]
    cmds.append([cli, "--cloak-on"])
    return cmds


def run(vid: int, pid: int, hid_paths: List[str], *, execute: bool = False, python_exe: Optional[str] = None,
        cli: str = "HidHideCLI", runner: Callable = subprocess.call, out=None) -> int:
    out = out or sys.stdout
    if not hid_paths:
        print(tr('devices {0:04x}:{1:04x} not found (run `gamepad-remapd list`)', vid, pid), file=sys.stderr)
        return 1
    ips = sorted({instance_path(p) for p in hid_paths})
    cmds = commands(ips, python_exe, cli)
    for c in cmds:
        line = " ".join(f'"{a}"' if " " in a else a for a in c)
        print(line, file=out)
        if execute:
            rc = runner(c)
            if rc != 0:
                print(tr('the command exited with code {0}; administrator rights and an installed HidHide are required', rc),
                      file=sys.stderr)
                return rc
    return 0
