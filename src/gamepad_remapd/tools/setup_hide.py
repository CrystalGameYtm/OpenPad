"""Автоналаштування приховування оригінального геймпада (Linux): udev-правила + systemd-сервіс із CAP_FOWNER.

Принцип: демон (від вашого звичайного користувача) відкриває hidraw оригіналу, а потім ЗАКРИВАЄ вузол для всіх інших
(chmod 0600). Відкритий дескриптор лишається робочим, тож сам демон нічого не втрачає, а SDL/емулятори/ігри відкрити
вузол уже не можуть. Для chmod чужого файла потрібна одна можливість ядра — CAP_FOWNER (не root і не окрема група):
її сервісу дає systemd (AmbientCapabilities), а для ручного запуску — `serve --privileged` (через sudo + setpriv).
При зупинці демона права повертаються; якщо його вбили аварійно — сервіс після зупинки просить udev відновити права.

Команда:  gamepad-remapd setup-hide --auto --service   (сама просить sudo). `plan()` — чиста функція (без дій).
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .udev import generate, generate_hide, generate_hide_hidraw

RULES_DIR = Path("/etc/udev/rules.d")
UNIT_PATH = Path("/etc/systemd/system/gamepad-remapd.service")
F_ACCESS = RULES_DIR / "70-gamepad-remapd.rules"
F_HIDE = RULES_DIR / "69-gamepad-remapd-hide.rules"
F_HIDRAW = RULES_DIR / "71-gamepad-remapd-hide-hidraw.rules"


def unit_text(user: str, uid: int, project_dir: str, python: str, src_dir: Optional[str]) -> str:
    env = f"Environment=PYTHONPATH={src_dir}\n" if src_dir else ""
    return (
        "[Unit]\nDescription=gamepad-remapd (ремапер геймпадів)\nAfter=systemd-udevd.service\n"
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
    """-> (файли для запису, команди для виконання ПІСЛЯ запису). Правила: 69 (evdev) і 70 (доступ користувачу)."""
    files: Dict[Path, str] = {F_ACCESS: generate(list(pairs)), F_HIDE: generate_hide(hide)}
    cmds: List[List[str]] = [["udevadm", "control", "--reload"],
                             ["udevadm", "trigger", "--subsystem-match=hidraw"],
                             ["udevadm", "trigger", "--subsystem-match=input"]]
    if service:
        files[UNIT_PATH] = unit_text(user, uid, project_dir, python, src_dir)
        cmds += [["systemctl", "daemon-reload"], ["systemctl", "enable", "--now", "gamepad-remapd.service"]]
    return files, cmds


def uninstall_plan(existing=lambda p: p.exists()) -> Tuple[List[Path], List[List[str]]]:
    """Прибирає те, що встановлено (разом зі старим 71-правилом групового режиму); systemctl — лише якщо є сервіс."""
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
    """Правила приховування встановлено, а цей процес групи не має -> hidraw оригіналу йому недоступний."""
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
    """Чи є в процесу CAP_FOWNER (біт 3 в CapEff) — тоді він може закривати чужі hidraw-вузли."""
    try:
        for line in Path(status_path).read_text().splitlines():
            if line.startswith("CapEff:"):
                return bool(int(line.split()[1], 16) & (1 << 3)) or os.geteuid() == 0
    except (OSError, ValueError):
        pass
    return os.geteuid() == 0


def fowner_exec_argv(uid: int, gid: int, argv: Sequence[str], environ=os.environ, python: str = sys.executable) -> List[str]:
    """sudo setpriv …: той самий користувач і група, але з CAP_FOWNER/CAP_SYS_NICE (без root-прав на решту)."""
    keep = ("PYTHONPATH", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR", "HOME")
    env = [f"{k}={environ[k]}" for k in keep if k in environ] + ["GAMEPAD_REMAPD_NO_REEXEC=1"]
    return ["sudo", "setpriv", f"--reuid={uid}", f"--regid={gid}", "--init-groups",
            "--inh-caps=+fowner,+sys_nice", "--ambient-caps=+fowner,+sys_nice",
            "env", *env, python, "-m", "gamepad_remapd", *argv]


def reexec_privileged(argv: Sequence[str]) -> None:
    a = fowner_exec_argv(os.getuid(), os.getgid(), argv)
    os.execvp(a[0], a)


def group_exec_argv(group: str, user: str, argv: Sequence[str], environ=os.environ, python: str = sys.executable) -> List[str]:
    """sudo -g <група> -u <користувач> env … python -m gamepad_remapd <argv>: те саме, але з групою приховування."""
    keep = ("PYTHONPATH", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR", "HOME")
    env = [f"{k}={environ[k]}" for k in keep if k in environ] + ["GAMEPAD_REMAPD_NO_REEXEC=1"]
    return ["sudo", "-g", group, "-u", user, "env", *env, python, "-m", "gamepad_remapd", *argv]


def reexec_with_group(group: str, user: str, argv: Sequence[str]) -> None:
    a = group_exec_argv(group, user, argv)
    os.execvp(a[0], a)


def reexec_with_sudo(argv: Sequence[str]) -> None:
    """Не root -> повторюємо ту саму команду через sudo (він сам запитає пароль)."""
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
        print(f"записано {p}", file=out)
    for c in cmds:
        r = runner(c, check=False)
        if getattr(r, "returncode", 0):
            print(f"УВАГА: {shlex.join(c)} -> код {r.returncode}", file=out)
    return 0
