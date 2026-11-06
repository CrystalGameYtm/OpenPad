"""Командний рядок: ``gamepad-remapd <команда>`` (також ``python -m gamepad_remapd``)."""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__
from .config import BUILTIN_PROFILES, ConfigError, DaemonConfig, load_config


def _cfg(args) -> DaemonConfig:
    cfg = load_config(args.config, args.config_dir)
    if getattr(args, "no_web", False):
        cfg.web_enabled = False
    if getattr(args, "port", None):
        cfg.web_port = args.port
    return cfg


def _maybe_regroup(argv: List[str]) -> Optional[int]:
    """1) Якщо демон уже працює як systemd-сервіс — другий не запускаємо (порти/пристрої були б зайняті).
    2) Застарілий груповий режим (правило 71-…hide-hidraw): без групи hidraw оригіналу недоступний -> перезапуск із нею."""
    if os.name != "posix" or os.environ.get("GAMEPAD_REMAPD_NO_REEXEC"):
        return None
    import getpass
    import shutil
    from .tools import setup_hide as sh
    from .input.hidraw_linux import HIDE_GROUP
    if sh.UNIT_PATH.exists() and sh.service_active():
        print("Демон уже працює як systemd-сервіс gamepad-remapd. Web UI: http://127.0.0.1:8765/ ; журнал: "
              "journalctl -u gamepad-remapd -f ; зупинити: sudo systemctl stop gamepad-remapd", file=sys.stderr)
        return 0
    if not sh.needs_group(HIDE_GROUP):
        return None
    if shutil.which("sudo") is None:
        print(f"УВАГА: hidraw оригіналу закрито групою {HIDE_GROUP} (старе правило 71), а sudo немає. "
              "Приберіть його: gamepad-remapd setup-hide --uninstall", file=sys.stderr)
        return None
    print(f"hidraw оригіналу закрито групою {HIDE_GROUP} (старий груповий режим): перезапускаюсь із нею через sudo. "
          "Рекомендовано перейти на `setup-hide --uninstall` і `setup-hide --auto --service`.", file=sys.stderr)
    sh.reexec_with_group(HIDE_GROUP, os.environ.get("SUDO_USER") or getpass.getuser(), argv)
    return 1


def cmd_serve(args) -> int:
    rc = _maybe_regroup(sys.argv[1:] or ["serve"])
    if rc is not None:
        return rc
    if getattr(args, "privileged", False) and os.name == "posix" and not os.environ.get("GAMEPAD_REMAPD_NO_REEXEC"):
        from .tools import setup_hide as sh
        if not sh.has_fowner():
            import shutil
            if shutil.which("sudo") and shutil.which("setpriv"):
                print("Перезапуск із CAP_FOWNER через sudo (щоб закрити hidraw оригіналу від інших програм)…", file=sys.stderr)
                argv = [a for a in sys.argv[1:] if a != "--privileged"] or ["serve"]
                sh.reexec_privileged(argv)
            print("УВАГА: потрібні sudo і setpriv (util-linux) для --privileged; працюю без повного приховування.",
                  file=sys.stderr)
    from .daemon import run_all
    run_all(_cfg(args))
    return 0


def cmd_daemon(args) -> int:
    from .daemon import run_daemon
    run_daemon(_cfg(args))
    return 0


def cmd_web(args) -> int:
    try:
        from .web.app import run_web
    except ImportError as e:
        print(f"Web UI потребує aiohttp: pip install 'gamepad-remapd[web]'  ({e})", file=sys.stderr)
        return 1
    run_web(_cfg(args))
    return 0


def cmd_list(args) -> int:
    from .daemon import load_layouts_safe
    from .core.manager import Manager
    from .core.session import device_id
    from .store import ProfileStore
    cfg = _cfg(args)
    layouts = load_layouts_safe(cfg.layout_dirs())
    m = Manager(cfg, ProfileStore(cfg.profiles_dir, BUILTIN_PROFILES, cfg.state_path), layouts)
    infos = m.visible_devices()
    if not infos:
        print("HID-пристроїв не знайдено (або немає прав/бібліотеки).")
    for i in infos:
        lay = m.pick_layout(i)
        print(f"{device_id(i):<10} {i.vid:04x}:{i.pid:04x}  iface={i.interface!s:<4} {i.backend:<6} "
              f"{i.path:<16} {i.name or '?':<32} -> {lay.id if lay else '(немає розкладки)'}")
    return 0


def cmd_sniff(args) -> int:
    from .daemon import load_layouts_safe
    from .tools.sniff_cli import run
    cfg = _cfg(args)
    layout = None
    if args.layout:
        layout = load_layouts_safe(cfg.layout_dirs()).get(args.layout)
        if layout is None:
            print(f"розкладку {args.layout!r} не знайдено", file=sys.stderr)
            return 1
    learn = [x.strip() for x in (args.learn or "").split(",") if x.strip()]
    return run(args.path, layout, args.seconds, learn, args.numbered, cfg.backend)


def _read_descriptor_file(path: str) -> bytes:
    """Бінарний дескриптор (cat .../report_descriptor > f) або текстовий hex ("05 01 09 05 ...")."""
    from .util import parse_hex
    raw = Path(path).read_bytes()
    try:
        return parse_hex(raw.decode("ascii")) or raw
    except (UnicodeDecodeError, ValueError):
        return raw


def cmd_autolayout(args) -> int:
    """Показує/зберігає розкладку, складену з HID-дескриптора пристрою (рівень 2)."""
    from .logic.autolayout import AutoLayoutError, build_layout_dict
    from .logic.hid_descriptor import DescriptorError, format_fields, parse_report_descriptor
    from .logic.layout import DeviceLayout, LayoutError
    from .util import parse_id
    vid, pid, name, iface = parse_id(args.vid) or 0, parse_id(args.pid) or 0, "", None
    if args.descriptor_file:
        try:
            desc = _read_descriptor_file(args.descriptor_file)
        except OSError as e:
            print(f"не вдалося прочитати файл: {e}", file=sys.stderr)
            return 1
    elif args.path:
        from .input.hidraw_linux import enumerate_hidraw
        info = next((i for i in enumerate_hidraw() if i.path == args.path), None)
        if info is None or not info.descriptor:
            print(f"{args.path}: пристрій або його report_descriptor недоступні "
                  "(перевірте шлях; Windows поки не підтримується)", file=sys.stderr)
            return 1
        desc, vid, pid, name, iface = info.descriptor, info.vid, info.pid, info.name, info.interface
    else:
        print("вкажіть /dev/hidrawN або --descriptor-file", file=sys.stderr)
        return 1
    try:
        parsed = parse_report_descriptor(desc)
        if args.fields:
            print(format_fields(parsed))
            return 0
        d = build_layout_dict(parsed, vid, pid, name, iface)
    except (DescriptorError, AutoLayoutError) as e:
        print(f"ПОМИЛКА: {e}", file=sys.stderr)
        return 1
    print(json.dumps(d, ensure_ascii=False, indent=2))
    for note in d["_auto"]["notes"]:
        print(f"- {note}", file=sys.stderr)
    if args.save:
        if not (vid or pid):
            print("для --save вкажіть --vid та --pid (або читайте з /dev/hidrawN)", file=sys.stderr)
            return 1
        from .store import _atomic_write
        lid = f"auto_{vid:04x}_{pid:04x}" + (f"_if{iface}" if iface is not None else "")
        try:
            DeviceLayout.from_dict(d, lid)              # не зберігаємо те, що демон не завантажить
        except LayoutError as e:
            print(f"ПОМИЛКА: {e}", file=sys.stderr)
            return 1
        dest = _cfg(args).devices_dir / f"{lid}.json"
        _atomic_write(dest, json.dumps(d, ensure_ascii=False, indent=2) + "\n")
        print(f"збережено: {dest}", file=sys.stderr)
    return 0


def cmd_monitor(args) -> int:
    from .daemon import load_layouts_safe
    from .tools.monitor_cli import run
    cfg = _cfg(args)
    layouts = load_layouts_safe(cfg.layout_dirs())
    if args.layout and args.layout not in layouts:
        print(f"розкладку {args.layout!r} не знайдено (є: {', '.join(sorted(layouts)) or '—'})", file=sys.stderr)
        return 1
    return run(args.path, layouts, args.layout, args.seconds, cfg.backend, args.raw)


def cmd_rumble_test(args) -> int:
    from .daemon import load_layouts_safe
    from .tools.rumble_test import run
    cfg = _cfg(args)
    return run(args.path, load_layouts_safe(cfg.layout_dirs()), cfg.backend, args.template, args.pad_to,
               args.sweep, args.hold, args.crc32_at)


def cmd_keyboards(args) -> int:
    """Показує клавіатури, які можна використати як геймпад, і чи вибрані вони в daemon.json."""
    from .input.keyboard_linux import enumerate_keyboards, _matches
    cfg = _cfg(args)
    sel = list(cfg.keyboard.device)
    cands = enumerate_keyboards([])
    if not cands:
        print("клавіатур не знайдено (або немає прав читати /dev/input/event*: додайте себе в групу input).")
        return 1
    for i in cands:
        mark = "*" if any(_matches(s, i.path, i.name, i.vid, i.pid) for s in sel) else " "
        paths = ", ".join(i.extra["paths"])
        print(f"{mark} {i.vid:04x}:{i.pid:04x}  {i.name or '?'}  [{paths}]")
    state = "УВІМКНЕНО" if cfg.keyboard.enabled else "вимкнено"
    print(f"\nрежим клавіатури: {state}; keymap={cfg.keyboard.keymap if isinstance(cfg.keyboard.keymap, str) else 'inline'}; "
          f"перемикач: {'+'.join(cfg.keyboard.toggle)}; * — вибрано в daemon.json (keyboard.device)")
    if not cfg.keyboard.enabled:
        print('щоб увімкнути, додайте в daemon.json: "keyboard": {"enabled": true, "device": "<частина імені або VID:PID>"}')
    return 0


def cmd_sdl_rumble(args) -> int:
    from .tools import sdl_rumble
    return sdl_rumble.run_traced() if args.trace else sdl_rumble.run()


def cmd_hidhide(args) -> int:
    from .input import enumerate_devices
    from .tools.hidhide import run
    vid_s, sep, pid_s = args.vid_pid.partition(":")
    if not sep:
        print("--vid-pid: формат VID:PID (hex, напр. 054c:09cc)", file=sys.stderr)
        return 1
    vid, pid = int(vid_s, 16), int(pid_s, 16)
    paths = [i.path for i in enumerate_devices(_cfg(args).backend) if (i.vid, i.pid) == (vid, pid)]
    return run(vid, pid, paths, execute=args.run)


def cmd_testpage(args) -> int:
    from .daemon import load_layouts_safe
    from .tools.testpage import run
    cfg = _cfg(args)
    layouts = load_layouts_safe(cfg.layout_dirs())
    if args.layout and args.layout not in layouts:
        print(f"розкладку {args.layout!r} не знайдено (є: {', '.join(sorted(layouts)) or '—'})", file=sys.stderr)
        return 1
    from .logic.handshake_lib import BUILTIN_LIBRARY, HandshakeLibError, load_library
    try:
        library = load_library(BUILTIN_LIBRARY, cfg.config_dir / "handshakes")
    except HandshakeLibError as e:
        print(f"бібліотека handshake: {e}", file=sys.stderr)
        return 1
    return run(args.path, layouts, args.layout, cfg.backend, args.port, not args.no_browser,
               save_dir=cfg.devices_dir, library=library)


def cmd_handshake_probe(args) -> int:
    from .daemon import load_layouts_safe
    from .logic.handshake_lib import BUILTIN_LIBRARY, HandshakeLibError, load_library
    from .tools.handshake_probe import run_cli
    cfg = _cfg(args)
    try:
        library = load_library(BUILTIN_LIBRARY, cfg.config_dir / "handshakes")
    except HandshakeLibError as e:
        print(f"бібліотека handshake: {e}", file=sys.stderr)
        return 1
    return run_cli(args.path, load_layouts_safe(cfg.layout_dirs()), cfg.devices_dir, backend=cfg.backend,
                   broad=args.broad, use_user=not args.no_user, save=args.save, auto_s=args.auto_seconds,
                   user_s=args.user_seconds, library=library)


def cmd_calibrate(args) -> int:
    from .daemon import load_layouts_safe
    from .logic.handshake_lib import BUILTIN_LIBRARY, HandshakeLibError, load_library
    from .tools.calibrate_job import run_cli
    cfg = _cfg(args)
    try:
        library = load_library(BUILTIN_LIBRARY, cfg.config_dir / "handshakes")
    except HandshakeLibError as e:
        print(f"бібліотека handshake: {e}", file=sys.stderr)
        return 1
    return run_cli(args.path, load_layouts_safe(cfg.layout_dirs()), cfg.devices_dir, backend=cfg.backend,
                   broad=args.broad, save=args.save, library=library, paddles=args.paddles)


def cmd_setup_hide(args) -> int:
    """Автоналаштування приховування оригіналу: група + udev-правила (+ сервіс). Сама піднімається через sudo."""
    import getpass
    from pathlib import Path
    from .daemon import load_layouts_safe
    from .tools import setup_hide as sh
    from .tools.udev import pairs_from_layouts
    if os.name != "posix":
        print("setup-hide — лише Linux (на Windows див. `hidhide`)", file=sys.stderr)
        return 1
    if args.uninstall:
        files, cmds = sh.uninstall_plan()
        if os.geteuid() != 0 and not args.dry_run:
            sh.reexec_with_sudo(sys.argv[1:] if len(sys.argv) > 1 else ["setup-hide", "--uninstall"])
        for p in files:
            print("# rm", p) if args.dry_run else p.unlink()
            if not args.dry_run:
                print(f"видалено {p}")
        if not args.dry_run:
            for c in cmds:
                subprocess.run(c, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("Готово. Перепідключіть геймпад (або виконайте `sudo udevadm trigger`), щоб права на пристрій відновились.")
        return 0
    cfg = _cfg(args)
    layouts = load_layouts_safe(cfg.layout_dirs())
    hide = []
    for vp in args.hide or []:
        vid, sep, pid = vp.partition(":")
        if not sep or not pid:
            print(f"--hide {vp!r}: потрібен формат VID:PID (hex)", file=sys.stderr)
            return 1
        hide.append((vid, pid, f"вручну: {vp}"))
    if args.auto:
        from .input import enumerate_devices
        for info in enumerate_devices(cfg.backend):
            if info.backend == "hidraw" and any(l.matches(info.vid, info.pid, info.interface, info.bus)
                                                for l in layouts.values()):
                hide.append((f"{info.vid:04x}", f"{info.pid:04x}", info.name or "підключений геймпад"))
    if not hide:
        print("Не вказано, що ховати: --hide VID:PID (можна повторювати) або --auto (підключені зараз геймпади).",
              file=sys.stderr)
        return 1
    if os.geteuid() != 0 and not args.dry_run:
        sh.reexec_with_sudo(sys.argv[1:])
    user = os.environ.get("SUDO_USER") or getpass.getuser()
    import pwd
    uid = pwd.getpwnam(user).pw_uid
    project = Path(__file__).resolve().parents[2]
    src = str(Path(__file__).resolve().parents[1])
    access = list(pairs_from_layouts(layouts))
    known = {(str(v).lower().zfill(4), str(pid).lower().zfill(4) if pid else None) for v, pid, _ in access}
    access += [(v, pid, c) for v, pid, c in hide if (v.lower().zfill(4), pid.lower().zfill(4)) not in known]   # і доступ для того, що ховаємо (напр. BT d7d7:0041)
    files, cmds = sh.plan(access, hide, user=user, uid=uid,
                          service=args.service, project_dir=str(project), python=sys.executable,
                          src_dir=src if (project / "src").is_dir() else None)
    return sh.run(files, cmds, None, dry_run=args.dry_run)


def cmd_unhide(args) -> int:
    """Повернути права вузлам, схованим демоном (після аварії). Без прав — підказка про udevadm trigger."""
    from .config import default_runtime_dir
    from .input.hide_linux import restore_stale
    if os.name != "posix":
        print("unhide — лише Linux", file=sys.stderr)
        return 1
    path = default_runtime_dir() / "hidden.json"
    done, bad = restore_stale(path)
    for n in done:
        print(f"відновлено {n}")
    if bad:
        for b in bad:
            print(f"НЕ вдалося: {b}", file=sys.stderr)
        print("Спробуйте через sudo: `sudo env XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR python3 -m gamepad_remapd unhide` "
              "або `sudo udevadm trigger --action=change` (правила udev поставлять права за замовчуванням).", file=sys.stderr)
        return 1
    if not done:
        print("Нічого відновлювати. Якщо геймпад усе ж недоступний: `sudo udevadm trigger --action=change` або перепідключіть його.")
    return 0


def cmd_sdl_list(args) -> int:
    from .tools.sdl_list import run
    return run()


def cmd_udev(args) -> int:
    from .daemon import load_layouts_safe
    from .tools.udev import generate, generate_hide, generate_hide_hidraw, pairs_from_layouts
    cfg = _cfg(args)
    if args.hide:
        pairs = []
        for vp in args.hide:
            vid, sep, pid = vp.partition(":")
            if not sep or not pid:
                print(f"--hide {vp!r}: потрібен формат VID:PID (hex, напр. 054c:09cc)", file=sys.stderr)
                return 1
            pairs.append((vid, pid, f"вручну: {vp}"))
        if args.hidraw_group:
            sys.stdout.write(generate_hide_hidraw(pairs, args.hidraw_group))
        else:
            sys.stdout.write(generate_hide(pairs))
        return 0
    pairs = pairs_from_layouts(load_layouts_safe(cfg.layout_dirs()))
    for vp in args.vid_pid or []:
        vid, _, pid = vp.partition(":")
        pairs.append((vid, pid or None, f"вручну: {vp}"))
    sys.stdout.write(generate(pairs))
    return 0


def cmd_check(args) -> int:
    """Перевіряє профіль (і, якщо задано --layout, компілює його проти розкладки)."""
    from .logic.profile import ProfileError, normalize_profile
    try:
        data = json.loads(Path(args.profile).read_text(encoding="utf-8"))
        norm = normalize_profile(data)
        if args.layout:
            from .daemon import load_layouts_safe
            from .logic.layout import ReportParser
            from .logic.profile import compile_profile
            from .output.null import NullPad
            import os
            lay = load_layouts_safe(_cfg(args).layout_dirs())[args.layout]
            if os.name == "posix":
                from .output.uinput_linux import SPECS, UinputPad
                fd = os.open(os.devnull, os.O_WRONLY)
                cp = compile_profile(norm, lay, ReportParser(lay), UinputPad(SPECS["xbox360"], fd=fd))
                os.close(fd)
            else:                                      # Windows: без uinput
                from .output.dry import DryPad
                cp = compile_profile(norm, lay, ReportParser(lay), DryPad())
            for w in cp.warnings:
                print("попередження:", w)
    except (OSError, json.JSONDecodeError, ProfileError, KeyError) as e:
        print(f"ПОМИЛКА: {e}", file=sys.stderr)
        return 1
    print("профіль коректний")
    return 0


def cmd_bench(args) -> int:
    from .tools.bench import run
    st = run(args.reports, args.profile, args.layout)
    print(json.dumps(st, indent=2, ensure_ascii=False))
    ok = st.get("p999_us", 0) < 1500
    print("\nбюджет 1500 мкс (p99.9): " + ("ВИКОНАНО" if ok else "ПЕРЕВИЩЕНО"), file=sys.stderr)
    return 0 if ok else 3


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gamepad-remapd", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--config", type=Path, help="шлях до daemon.json")
    p.add_argument("--config-dir", type=Path, help="каталог конфігурації (профілі, розкладки)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="демон + Web UI (в окремому процесі)")
    s.add_argument("--privileged", action="store_true",
                   help="Linux: перезапуститись із CAP_FOWNER (через sudo+setpriv), щоб демон міг закрити hidraw оригіналу від інших програм")
    s.add_argument("--no-web", action="store_true")
    s.add_argument("--port", type=int)
    s.set_defaults(fn=cmd_serve)
    sub.add_parser("daemon", help="лише демон").set_defaults(fn=cmd_daemon)
    w = sub.add_parser("web", help="лише Web UI (підключається до запущеного демона)")
    w.add_argument("--port", type=int)
    w.set_defaults(fn=cmd_web)
    sub.add_parser("list", help="показати HID-пристрої та підібрані розкладки").set_defaults(fn=cmd_list)

    sn = sub.add_parser("sniff", help="hex-інспектор прихованих кнопок")
    sn.add_argument("path", nargs="?", help="/dev/hidrawN")
    sn.add_argument("--layout", help="id розкладки (для handshake та report_id)")
    sn.add_argument("--learn", help="імена кнопок через кому: M1,M2,M3,M4")
    sn.add_argument("--seconds", type=float, default=0, help="0 = до Ctrl-C")
    sn.add_argument("--numbered", action="store_true", help="перший байт звіту — Report ID")
    sn.set_defaults(fn=cmd_sniff)

    a = sub.add_parser("autolayout", help="розкладка з HID-дескриптора пристрою (авто-мапінг)")
    a.add_argument("path", nargs="?", help="/dev/hidrawN")
    a.add_argument("--descriptor-file", help="файл із дескриптором (бінарний або hex-текст)")
    a.add_argument("--vid", help="VID (hex), якщо читаємо з файлу")
    a.add_argument("--pid", help="PID (hex), якщо читаємо з файлу")
    a.add_argument("--fields", action="store_true", help="лише таблиця полів дескриптора")
    a.add_argument("--save", action="store_true", help="зберегти в каталог devices/ як звичайну розкладку")
    a.set_defaults(fn=cmd_autolayout)

    mo = sub.add_parser("monitor", help="живий монітор: які кнопки/осі розпізнано за розкладкою")
    mo.add_argument("path", help="/dev/hidrawN")
    mo.add_argument("--layout", help="id розкладки (за замовчуванням — підбір за VID/PID, потім авто)")
    mo.add_argument("--seconds", type=float, default=0, help="0 = до Ctrl-C")
    mo.add_argument("--raw", action="store_true", help="друкувати кожен сирий звіт hex-ом (діагностика handshake)")
    mo.set_defaults(fn=cmd_monitor)

    rt = sub.add_parser("rumble-test", help="вручну перевірити вібрацію (шаблон з розкладки, свій або перебір команд)")
    rt.add_argument("path", help="/dev/hidrawN")
    rt.add_argument("--template", help='напр. "05 0f {strong} {weak}"')
    rt.add_argument("--pad-to", type=int, default=0, help="добити звіт нулями до N байт")
    rt.add_argument("--sweep", help="ID:ВІД-ДО — перебір другого байта, напр. 0x05:0x00-0x20")
    rt.add_argument("--crc32-at", type=int, default=0, help="DS4 по Bluetooth: покласти CRC-32 (seed A2) у байти N..N+3, напр. 74")
    rt.add_argument("--hold", type=float, default=1.0, help="секунд на кожен мотор")
    rt.set_defaults(fn=cmd_rumble_test)

    sub.add_parser("keyboards", help="клавіатури, які можна використати як геймпад (див. секцію keyboard у daemon.json)"
                   ).set_defaults(fn=cmd_keyboards)

    sr = sub.add_parser("sdl-rumble", help="вібрація через SDL3 (еталон); --trace показує, що SDL пише в hidraw")
    sr.add_argument("--trace", action="store_true", help="запустити під strace й надрукувати hex записів")
    sr.set_defaults(fn=cmd_sdl_rumble)

    hh = sub.add_parser("hidhide", help="Windows: команди HidHide, щоб сховати оригінальний геймпад від ігор")
    hh.add_argument("--vid-pid", required=True, metavar="VID:PID", help="hex, напр. 054c:09cc")
    hh.add_argument("--run", action="store_true", help="виконати команди (адміністратор), а не лише надрукувати")
    hh.set_defaults(fn=cmd_hidhide)

    tp = sub.add_parser("testpage", help="веб-сторінка для перевірки кнопок і осей у браузері (без демона)")
    tp.add_argument("path", nargs="?", help="/dev/hidrawN (можна вибрати на сторінці)")
    tp.add_argument("--layout", help="id розкладки (за замовчуванням — за VID/PID, потім авто)")
    tp.add_argument("--port", type=int, default=8766)
    tp.add_argument("--no-browser", action="store_true", help="не відкривати браузер автоматично")
    tp.set_defaults(fn=cmd_testpage)

    hp = sub.add_parser("handshake-probe", help="автопошук handshake для мовчазного інтерфейсу (бібліотека відомих)")
    hp.add_argument("path", help="/dev/hidrawN (вендорський інтерфейс ресивера, що мовчить)")
    hp.add_argument("--broad", action="store_true", help="широкий перебір: пробувати й handshake інших виробників")
    hp.add_argument("--no-user", action="store_true", help="лише автономна фаза (без прохання натискати кнопки)")
    hp.add_argument("--save", action="store_true", help="зберегти знайдене як devices/learned_VID_PID_ifN.json")
    hp.add_argument("--auto-seconds", type=float, default=1.2)
    hp.add_argument("--user-seconds", type=float, default=2.5)
    hp.set_defaults(fn=cmd_handshake_probe)

    ca = sub.add_parser("calibrate", help="майстер калібрування: вивчити кнопки й осі будь-якого hidraw-геймпада")
    ca.add_argument("path", help="/dev/hidrawN")
    ca.add_argument("--broad", action="store_true",
                    help="якщо пристрій мовчить — пробувати й handshake інших виробників (SDL-бібліотека)")
    ca.add_argument("--paddles", action="store_true",
                    help="додати кроки ML/MR (бокові/задні «весла»; є не в усіх геймпадів)")
    ca.add_argument("--save", action="store_true", help="зберегти як devices/calibrated_VID_PID[_ifN].json")
    ca.set_defaults(fn=cmd_calibrate)

    u = sub.add_parser("udev-rules", help="вивести udev-правила для відомих розкладок")
    u.add_argument("--vid-pid", action="append", metavar="VID[:PID]", help="додатково (hex), можна повторювати")
    u.add_argument("--hide", action="append", metavar="VID:PID",
                   help="замість правил доступу вивести правила, що ховають оригінальний геймпад від програм "
                        "(SDL/Chrome/Steam бачитимуть лише віртуальний); можна повторювати")
    u.add_argument("--hidraw-group", metavar="GROUP",
                   help="разом з --hide: вивести правила, що закривають сам /dev/hidrawN від усіх, крім групи GROUP "
                        "(демон запускають із цією групою; без root). Файл 71-…, а не 69-…")
    u.set_defaults(fn=cmd_udev)

    uh = sub.add_parser("unhide", help="Linux: повернути права на вузли оригінального геймпада після аварії демона")
    uh.set_defaults(fn=cmd_unhide)
    sl = sub.add_parser("sdl-list", help="що бачить SDL3 (перевірка приховування): список джойстиків")
    sl.set_defaults(fn=cmd_sdl_list)

    sh = sub.add_parser("setup-hide", help="Linux: автоналаштування приховування оригіналу (група + udev + сервіс), через sudo")
    sh.add_argument("--hide", action="append", metavar="VID:PID", help="що ховати (можна повторювати)")
    sh.add_argument("--auto", action="store_true", help="сховати всі підключені зараз геймпади з розкладкою")
    sh.add_argument("--service", action="store_true", help="встановити systemd-сервіс: демон стартує сам від вашого користувача з CAP_FOWNER (може закривати hidraw оригіналу)")
    sh.add_argument("--dry-run", action="store_true", help="лише показати, що буде зроблено")
    sh.add_argument("--uninstall", action="store_true", help="прибрати правила й сервіс")
    sh.set_defaults(fn=cmd_setup_hide)

    c = sub.add_parser("check", help="перевірити JSON-профіль")
    c.add_argument("profile")
    c.add_argument("--layout")
    c.set_defaults(fn=cmd_check)

    b = sub.add_parser("bench", help="виміряти програмну затримку Pipeline")
    b.add_argument("--reports", type=int, default=200_000)
    b.add_argument("--profile", default="precision_aim")
    b.add_argument("--layout", default="sony_ds4_usb")
    b.set_defaults(fn=cmd_bench)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return args.fn(args)
    except ConfigError as e:
        print(f"конфігурація: {e}", file=sys.stderr)
        return 1
    except PermissionError as e:
        print(f"немає прав: {e}\nПеревірте udev-правила: gamepad-remapd udev-rules", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
