"""Automatic setup for hiding the original gamepad (Linux): udev rules + a systemd service with CAP_FOWNER.

Principle: the daemon (run by your normal user) opens the original's hidraw node, then CLOSES the node for everyone
else (chmod 0600). The already open descriptor keeps working, so the daemon itself loses nothing, while SDL, emulators
and games can no longer open the node. Changing permissions on someone else's file needs one kernel capability —
CAP_FOWNER (not root and not a separate group): systemd grants it to the service (AmbientCapabilities), and for a manual
run `serve --privileged` does it (via sudo + setpriv). When the daemon stops, the permissions are restored; if it was killed
abnormally, the service asks udev to restore them after it stops.

Command:  gamepad-remapd setup-hide --auto --service   (asks for sudo itself). `plan()` is a pure function (no actions).
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .udev import generate, generate_hide, generate_hide_hidraw
from ..i18n import tr

RULES_DIR = Path("/etc/udev/rules.d")
UNIT_PATH = Path("/etc/systemd/system/gamepad-remapd.service")
F_ACCESS = RULES_DIR / "70-gamepad-remapd.rules"
F_HIDE = RULES_DIR / "69-gamepad-remapd-hide.rules"
F_HIDRAW = RULES_DIR / "71-gamepad-remapd-hide-hidraw.rules"


def unit_text(user: str, uid: int, project_dir: str, python: str, src_dir: Optional[str]) -> str:
    env = f"Environment=PYTHONPATH={src_dir}\n" if src_dir else ""
    return (
        "[Unit]\nDescription=gamepad-remapd (gamepad remapper)\nAfter=systemd-udevd.service\n"
        "StartLimitIntervalSec=0\n\n"
        "[Service]\n"
        f"User={user}\nSupplementaryGroups=input\n"
        "AmbientCapabilities=CAP_FOWNER CAP_SYS_NICE\nCapabilityBoundingSet=CAP_FOWNER CAP_SYS_NICE\n"
        f"Environment=XDG_RUNTIME_DIR=/run/user/{uid}\n{env}"
        f"WorkingDirectory={project_dir}\n"
        f"ExecStart={python} -m gamepad_remapd serve\n"
        "ExecStopPost=+/usr/bin/udevadm trigger --action=change --subsystem-match=hidraw\n"
        "Restart=always\nRestartSec=3\n\n"
        "[Install]\nWantedBy=multi-user.target\n")


def plan(pairs: Sequence[Tuple[str, Optional[str], str]], hide: Sequence[Tuple[str, str, str]], *,
         user: str, uid: int, service: bool, project_dir: str, python: str, src_dir: Optional[str]
         ) -> Tuple[Dict[Path, str], List[List[str]]]:
    """-> (files to write, commands to run AFTER writing). Rules: 69 (evdev) and 70 (user access)."""
    files: Dict[Path, str] = {F_ACCESS: generate(list(pairs)), F_HIDE: generate_hide(hide)}
    cmds: List[List[str]] = [["udevadm", "control", "--reload"],
                             ["udevadm", "trigger", "--subsystem-match=hidraw"],
                             ["udevadm", "trigger", "--subsystem-match=input"]]
    if service:
        files[UNIT_PATH] = unit_text(user, uid, project_dir, python, src_dir)
        cmds += [["systemctl", "daemon-reload"], ["systemctl", "enable", "--now", "gamepad-remapd.service"]]
    return files, cmds


def uninstall_plan(existing=lambda p: p.exists()) -> Tuple[List[Path], List[List[str]]]:
    """Removes what was installed (including the old 71 group-mode rule); systemctl only if the service exists."""
    files = [p for p in (F_ACCESS, F_HIDE, F_HIDRAW, UNIT_PATH) if existing(p)]
    cmds: List[List[str]] = []
    if UNIT_PATH in files:
        cmds += [["systemctl", "disable", "--now", "gamepad-remapd.service"]]
    if UNIT_PATH in files:
        cmds += [["systemctl", "daemon-reload"]]
    cmds += [["udevadm", "control", "--reload"], ["udevadm", "trigger", "--subsystem-match=hidraw"],
             ["udevadm", "trigger", "--subsystem-match=input"]]
    return files, cmds


def group_gid(group: str) -> Optional[int]:
    try:
        import grp
        return grp.getgrnam(group).gr_gid
    except (ImportError, KeyError):
        return None


def needs_group(group: str, rules_file: Path = F_HIDRAW) -> bool:
    """Hiding rules are installed, but this process has no group -> the original's hidraw is not accessible to it."""
    if os.name != "posix" or not rules_file.exists():
        return False
    gid = group_gid(group)
    return gid is not None and gid != os.getegid() and gid not in os.getgroups()


def service_active(runner=subprocess.run) -> bool:
    try:
        r = runner(["systemctl", "is-active", "--quiet", "gamepad-remapd.service"], check=False)
        return r.returncode == 0
    except (OSError, ValueError):
        return False


def has_fowner(status_path: str = "/proc/self/status") -> bool:
    """Whether the process has CAP_FOWNER (bit 3 in CapEff) — then it can close other hidraw nodes."""
    try:
        for line in Path(status_path).read_text().splitlines():
            if line.startswith("CapEff:"):
                return bool(int(line.split()[1], 16) & (1 << 3)) or os.geteuid() == 0
    except (OSError, ValueError):
        pass
    return os.geteuid() == 0


def fowner_exec_argv(uid: int, gid: int, argv: Sequence[str], environ=os.environ, python: str = sys.executable) -> List[str]:
    """sudo setpriv …: the same user and group, but with CAP_FOWNER/CAP_SYS_NICE (no root rights for anything else)."""
    keep = ("PYTHONPATH", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR", "HOME")
    env = [f"{k}={environ[k]}" for k in keep if k in environ] + ["GAMEPAD_REMAPD_NO_REEXEC=1"]
    return ["sudo", "setpriv", f"--reuid={uid}", f"--regid={gid}", "--init-groups",
            "--inh-caps=+fowner,+sys_nice", "--ambient-caps=+fowner,+sys_nice",
            "env", *env, python, "-m", "gamepad_remapd", *argv]


def reexec_privileged(argv: Sequence[str]) -> None:
    a = fowner_exec_argv(os.getuid(), os.getgid(), argv)
    os.execvp(a[0], a)


def group_exec_argv(group: str, user: str, argv: Sequence[str], environ=os.environ, python: str = sys.executable) -> List[str]:
    """sudo -g <group> -u <user> env … python -m gamepad_remapd <argv>: the same, but with the hiding group."""
    keep = ("PYTHONPATH", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR", "HOME")
    env = [f"{k}={environ[k]}" for k in keep if k in environ] + ["GAMEPAD_REMAPD_NO_REEXEC=1"]
    return ["sudo", "-g", group, "-u", user, "env", *env, python, "-m", "gamepad_remapd", *argv]


def reexec_with_group(group: str, user: str, argv: Sequence[str]) -> None:
    a = group_exec_argv(group, user, argv)
    os.execvp(a[0], a)


def reexec_with_sudo(argv: Sequence[str]) -> None:
    """Not root -> repeat the same command via sudo (it asks for the password itself)."""
    env = ["env"] + [f"{k}={os.environ[k]}" for k in ("PYTHONPATH", "XDG_CONFIG_HOME") if k in os.environ]
    os.execvp("sudo", ["sudo", *env, sys.executable, "-m", "gamepad_remapd", *argv])


def run(files: Dict[Path, str], cmds: List[List[str]], group: str, dry_run: bool = False,
        out=sys.stdout, runner=subprocess.run) -> int:
    steps = [["groupadd", "-f", group]] if group else []
    if dry_run:
        for c in steps:
            print("#", shlex.join(c), file=out)
        for p, t in files.items():
            print(f"# --- {p}\n{t}", file=out)
        for c in cmds:
            print("#", shlex.join(c), file=out)
        return 0
    for c in steps:
        runner(c, check=True)
    for p, t in files.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(t)
        print(tr('written {0}', p), file=out)
    for c in cmds:
        r = runner(c, check=False)
        if getattr(r, "returncode", 0):
            print(tr('WARNING: {0} -> code {1}', shlex.join(c), r.returncode), file=out)
    return 0
