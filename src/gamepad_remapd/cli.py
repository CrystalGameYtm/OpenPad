"""Командний рядок: ``gamepad-remapd <команда>`` (також ``python -m gamepad_remapd``)."""
from __future__ import annotations

import argparse
import json
import logging
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


def cmd_serve(args) -> int:
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


def cmd_sdl_rumble(args) -> int:
    from .tools import sdl_rumble
    return sdl_rumble.run_traced() if args.trace else sdl_rumble.run()


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
                   broad=args.broad, save=args.save, library=library)


def cmd_udev(args) -> int:
    from .daemon import load_layouts_safe
    from .tools.udev import generate, pairs_from_layouts
    cfg = _cfg(args)
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
            from .output.uinput_linux import SPECS, UinputPad
            import os
            lay = load_layouts_safe(_cfg(args).layout_dirs())[args.layout]
            fd = os.open(os.devnull, os.O_WRONLY)
            cp = compile_profile(norm, lay, ReportParser(lay), UinputPad(SPECS["xbox360"], fd=fd))
            os.close(fd)
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

    sr = sub.add_parser("sdl-rumble", help="вібрація через SDL3 (еталон); --trace показує, що SDL пише в hidraw")
    sr.add_argument("--trace", action="store_true", help="запустити під strace й надрукувати hex записів")
    sr.set_defaults(fn=cmd_sdl_rumble)

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
    ca.add_argument("--save", action="store_true", help="зберегти як devices/calibrated_VID_PID[_ifN].json")
    ca.set_defaults(fn=cmd_calibrate)

    u = sub.add_parser("udev-rules", help="вивести udev-правила для відомих розкладок")
    u.add_argument("--vid-pid", action="append", metavar="VID[:PID]", help="додатково (hex), можна повторювати")
    u.set_defaults(fn=cmd_udev)

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
