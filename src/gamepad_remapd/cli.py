"""Command line: ``gamepad-remapd <command>`` (also ``python -m gamepad_remapd``)."""
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
from .config import BUILTIN_PROFILES, ConfigError, DaemonConfig, init_language, load_config
from .i18n import tr


def _cfg(args) -> DaemonConfig:
    cfg = load_config(args.config, args.config_dir)
    if getattr(args, "no_web", False):
        cfg.web_enabled = False
    if getattr(args, "port", None):
        cfg.web_port = args.port
    return cfg


def _maybe_regroup(argv: List[str]) -> Optional[int]:
    """1) If the daemon already runs as a systemd service, we do not start a second one (ports/devices would be busy).
        2) Legacy group mode (rule 71-…hide-hidraw): without the group the original's hidraw is unavailable -> restart with it.
    """
    if os.name != "posix" or os.environ.get("GAMEPAD_REMAPD_NO_REEXEC"):
        return None
    import getpass
    import shutil
    from .tools import setup_hide as sh
    from .input.hidraw_linux import HIDE_GROUP
    if sh.UNIT_PATH.exists() and sh.service_active():
        print(tr('The daemon is already running as the gamepad-remapd systemd service. Web UI: http://127.0.0.1:8765/ ; log: journalctl -u gamepad-remapd -f ; stop: sudo systemctl stop gamepad-remapd'), file=sys.stderr)
        return 0
    if not sh.needs_group(HIDE_GROUP):
        return None
    if shutil.which("sudo") is None:
        print(tr('WARNING: the original hidraw is closed by the group {0} (old rule 71) and sudo is missing. Remove it: gamepad-remapd setup-hide --uninstall', HIDE_GROUP), file=sys.stderr)
        return None
    print(tr('The original hidraw is closed by the group {0} (old group mode): restarting with it through sudo. It is recommended to switch to `setup-hide --uninstall` and `setup-hide --auto --service`.', HIDE_GROUP), file=sys.stderr)
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
                print(tr('Restarting with CAP_FOWNER through sudo (to close the original hidraw to other programs)…'), file=sys.stderr)
                argv = [a for a in sys.argv[1:] if a != "--privileged"] or ["serve"]
                sh.reexec_privileged(argv)
            print(tr('WARNING: sudo and setpriv (util-linux) are required for --privileged; continuing without full hiding.'),
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
        print(tr("The Web UI needs aiohttp: pip install 'gamepad-remapd[web]'  ({0})", e), file=sys.stderr)
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
        print(tr('No HID devices found (or no permissions/library).'))
    for i in infos:
        lay = m.pick_layout(i)
        print(f"{device_id(i):<10} {i.vid:04x}:{i.pid:04x}  iface={i.interface!s:<4} {i.backend:<6} "
              f"{i.path:<16} {i.name or '?':<32} -> {lay.id if lay else tr('(no layout)')}")
    return 0


def cmd_sniff(args) -> int:
    from .daemon import load_layouts_safe
    from .tools.sniff_cli import run
    cfg = _cfg(args)
    layout = None
    if args.layout:
        layout = load_layouts_safe(cfg.layout_dirs()).get(args.layout)
        if layout is None:
            print(tr('layout {0!r} not found', args.layout), file=sys.stderr)
            return 1
    learn = [x.strip() for x in (args.learn or "").split(",") if x.strip()]
    return run(args.path, layout, args.seconds, learn, args.numbered, cfg.backend)


def _read_descriptor_file(path: str) -> bytes:
    """Binary descriptor (cat .../report_descriptor > f) or a text hex dump ("05 01 09 05 ...")."""
    from .util import parse_hex
    raw = Path(path).read_bytes()
    try:
        return parse_hex(raw.decode("ascii")) or raw
    except (UnicodeDecodeError, ValueError):
        return raw


def cmd_autolayout(args) -> int:
    """Shows/saves a layout built from the device's HID descriptor (level 2)."""
    from .logic.autolayout import AutoLayoutError, build_layout_dict
    from .logic.hid_descriptor import DescriptorError, format_fields, parse_report_descriptor
    from .logic.layout import DeviceLayout, LayoutError
    from .util import parse_id
    vid, pid, name, iface = parse_id(args.vid) or 0, parse_id(args.pid) or 0, "", None
    if args.descriptor_file:
        try:
            desc = _read_descriptor_file(args.descriptor_file)
        except OSError as e:
            print(tr('could not read the file: {0}', e), file=sys.stderr)
            return 1
    elif args.path:
        from .input.hidraw_linux import enumerate_hidraw
        info = next((i for i in enumerate_hidraw() if i.path == args.path), None)
        if info is None or not info.descriptor:
            print(tr('{0}: the device or its report_descriptor is unavailable (check the path; Windows is not supported yet)', args.path), file=sys.stderr)
            return 1
        desc, vid, pid, name, iface = info.descriptor, info.vid, info.pid, info.name, info.interface
    else:
        print(tr('specify /dev/hidrawN or --descriptor-file'), file=sys.stderr)
        return 1
    try:
        parsed = parse_report_descriptor(desc)
        if args.fields:
            print(format_fields(parsed))
            return 0
        d = build_layout_dict(parsed, vid, pid, name, iface)
    except (DescriptorError, AutoLayoutError) as e:
        print(tr('ERROR: {0}', e), file=sys.stderr)
        return 1
    print(json.dumps(d, ensure_ascii=False, indent=2))
    for note in d["_auto"]["notes"]:
        print(f"- {note}", file=sys.stderr)
    if args.save:
        if not (vid or pid):
            print(tr('for --save specify --vid and --pid (or read from /dev/hidrawN)'), file=sys.stderr)
            return 1
        from .store import _atomic_write
        lid = f"auto_{vid:04x}_{pid:04x}" + (f"_if{iface}" if iface is not None else "")
        try:
            DeviceLayout.from_dict(d, lid)              
        except LayoutError as e:
            print(tr('ERROR: {0}', e), file=sys.stderr)
            return 1
        dest = _cfg(args).devices_dir / f"{lid}.json"
        _atomic_write(dest, json.dumps(d, ensure_ascii=False, indent=2) + "\n")
        print(tr('saved: {0}', dest), file=sys.stderr)
    return 0


def cmd_monitor(args) -> int:
    from .daemon import load_layouts_safe
    from .tools.monitor_cli import run
    cfg = _cfg(args)
    layouts = load_layouts_safe(cfg.layout_dirs())
    if args.layout and args.layout not in layouts:
        print(tr('layout {0!r} not found (available: {1})', args.layout, ', '.join(sorted(layouts)) or '—'), file=sys.stderr)
        return 1
    return run(args.path, layouts, args.layout, args.seconds, cfg.backend, args.raw)


def cmd_rumble_test(args) -> int:
    from .daemon import load_layouts_safe
    from .tools.rumble_test import run
    cfg = _cfg(args)
    return run(args.path, load_layouts_safe(cfg.layout_dirs()), cfg.backend, args.template, args.pad_to,
               args.sweep, args.hold, args.crc32_at)


def cmd_keyboards(args) -> int:
    """Shows keyboards that can be used as a gamepad and whether they are selected in daemon.json."""
    from .input.keyboard_linux import enumerate_keyboards, _matches
    cfg = _cfg(args)
    sel = list(cfg.keyboard.device)
    cands = enumerate_keyboards([])
    if not cands:
        print(tr('no keyboards found (or no permission to read /dev/input/event*: add yourself to the input group).'))
        return 1
    for i in cands:
        mark = "*" if any(_matches(s, i.path, i.name, i.vid, i.pid) for s in sel) else " "
        paths = ", ".join(i.extra["paths"])
        print(f"{mark} {i.vid:04x}:{i.pid:04x}  {i.name or '?'}  [{paths}]")
    state = tr('ENABLED') if cfg.keyboard.enabled else tr('disabled')
    print(tr('\n'
             'keyboard mode: {0}; keymap={1}; toggle: {2}; * — selected in daemon.json (keyboard.device)', state, cfg.keyboard.keymap if isinstance(cfg.keyboard.keymap, str) else 'inline', '+'.join(cfg.keyboard.toggle)))
    if not cfg.keyboard.enabled:
        print(tr('to enable it, add to daemon.json: "keyboard": {"enabled": true, "device": "<part of the name or VID:PID>"}'))
    return 0


def cmd_sdl_rumble(args) -> int:
    from .tools import sdl_rumble
    return sdl_rumble.run_traced() if args.trace else sdl_rumble.run()


def cmd_hidhide(args) -> int:
    from .input import enumerate_devices
    from .tools.hidhide import run
    vid_s, sep, pid_s = args.vid_pid.partition(":")
    if not sep:
        print(tr('--vid-pid: the format is VID:PID (hex, e.g. 054c:09cc)'), file=sys.stderr)
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
        print(tr('layout {0!r} not found (available: {1})', args.layout, ', '.join(sorted(layouts)) or '—'), file=sys.stderr)
        return 1
    from .logic.handshake_lib import BUILTIN_LIBRARY, HandshakeLibError, load_library
    try:
        library = load_library(BUILTIN_LIBRARY, cfg.config_dir / "handshakes")
    except HandshakeLibError as e:
        print(tr('handshake library: {0}', e), file=sys.stderr)
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
        print(tr('handshake library: {0}', e), file=sys.stderr)
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
        print(tr('handshake library: {0}', e), file=sys.stderr)
        return 1
    return run_cli(args.path, load_layouts_safe(cfg.layout_dirs()), cfg.devices_dir, backend=cfg.backend,
                   broad=args.broad, save=args.save, library=library, paddles=args.paddles)


def cmd_setup_hide(args) -> int:
    """Automatic setup of original-device hiding: group + udev rules (+ service). Elevates itself via sudo."""
    import getpass
    from pathlib import Path
    from .daemon import load_layouts_safe
    from .tools import setup_hide as sh
    from .tools.udev import pairs_from_layouts
    if os.name != "posix":
        print(tr('setup-hide is Linux only (on Windows see `hidhide`)'), file=sys.stderr)
        return 1
    if args.uninstall:
        files, cmds = sh.uninstall_plan()
        if os.geteuid() != 0 and not args.dry_run:
            sh.reexec_with_sudo(sys.argv[1:] if len(sys.argv) > 1 else ["setup-hide", "--uninstall"])
        for p in files:
            print("# rm", p) if args.dry_run else p.unlink()
            if not args.dry_run:
                print(tr('removed {0}', p))
        if not args.dry_run:
            for c in cmds:
                subprocess.run(c, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(tr('Done. Reconnect the gamepad (or run `sudo udevadm trigger`) so that the device permissions are restored.'))
        return 0
    cfg = _cfg(args)
    layouts = load_layouts_safe(cfg.layout_dirs())
    hide = []
    for vp in args.hide or []:
        vid, sep, pid = vp.partition(":")
        if not sep or not pid:
            print(tr('--hide {0!r}: the format must be VID:PID (hex)', vp), file=sys.stderr)
            return 1
        hide.append((vid, pid, tr('manually: {0}', vp)))
    if args.auto:
        from .input import enumerate_devices
        for info in enumerate_devices(cfg.backend):
            if info.backend == "hidraw" and any(l.matches(info.vid, info.pid, info.interface, info.bus)
                                                for l in layouts.values()):
                hide.append((f"{info.vid:04x}", f"{info.pid:04x}", info.name or tr('connected gamepad')))
    if not hide:
        print(tr('Nothing to hide was specified: --hide VID:PID (can be repeated) or --auto (the gamepads connected now).'),
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
    access += [(v, pid, c) for v, pid, c in hide if (v.lower().zfill(4), pid.lower().zfill(4)) not in known]   
    files, cmds = sh.plan(access, hide, user=user, uid=uid,
                          service=args.service, project_dir=str(project), python=sys.executable,
                          src_dir=src if (project / "src").is_dir() else None)
    return sh.run(files, cmds, None, dry_run=args.dry_run)


def cmd_unhide(args) -> int:
    """Gives back permissions to nodes hidden by the daemon (after a crash). Without permissions — a hint about udevadm trigger."""
    from .config import default_runtime_dir
    from .input.hide_linux import restore_stale
    if os.name != "posix":
        print(tr('unhide is Linux only'), file=sys.stderr)
        return 1
    path = default_runtime_dir() / "hidden.json"
    done, bad = restore_stale(path)
    for n in done:
        print(tr('restored {0}', n))
    if bad:
        for b in bad:
            print(tr('FAILED: {0}', b), file=sys.stderr)
        print(tr('Try with sudo: `sudo env XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR python3 -m gamepad_remapd unhide` or `sudo udevadm trigger --action=change` (the udev rules will set the default permissions).'), file=sys.stderr)
        return 1
    if not done:
        print(tr('Nothing to restore. If the gamepad is still inaccessible: `sudo udevadm trigger --action=change` or reconnect it.'))
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
                print(tr('--hide {0!r}: the format must be VID:PID (hex, e.g. 054c:09cc)', vp), file=sys.stderr)
                return 1
            pairs.append((vid, pid, tr('manually: {0}', vp)))
        if args.hidraw_group:
            sys.stdout.write(generate_hide_hidraw(pairs, args.hidraw_group))
        else:
            sys.stdout.write(generate_hide(pairs))
        return 0
    pairs = pairs_from_layouts(load_layouts_safe(cfg.layout_dirs()))
    for vp in args.vid_pid or []:
        vid, _, pid = vp.partition(":")
        pairs.append((vid, pid or None, tr('manually: {0}', vp)))
    sys.stdout.write(generate(pairs))
    return 0


def cmd_check(args) -> int:
    """Checks a profile (and, if --layout is given, compiles it against the layout)."""
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
            else:                                      # Windows: no uinput
                from .output.dry import DryPad
                cp = compile_profile(norm, lay, ReportParser(lay), DryPad())
            for w in cp.warnings:
                print(tr('warnings:'), w)
    except (OSError, json.JSONDecodeError, ProfileError, KeyError) as e:
        print(tr('ERROR: {0}', e), file=sys.stderr)
        return 1
    print(tr('the profile is valid'))
    return 0


def cmd_bench(args) -> int:
    from .tools.bench import run
    st = run(args.reports, args.profile, args.layout)
    print(json.dumps(st, indent=2, ensure_ascii=False))
    ok = st.get("p999_us", 0) < 1500
    print(tr('\nbudget 1500 µs (p99.9): ') + (tr('MET') if ok else tr('EXCEEDED')), file=sys.stderr)
    return 0 if ok else 3


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gamepad-remapd", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--config", type=Path, help=tr('path to daemon.json'))
    p.add_argument("--config-dir", type=Path, help=tr('configuration directory (profiles, layouts)'))
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help=tr('daemon + Web UI (in a separate process)'))
    s.add_argument("--privileged", action="store_true",
                   help=tr('Linux: restart with CAP_FOWNER (through sudo+setpriv) so the daemon can close the original hidraw to other programs'))
    s.add_argument("--no-web", action="store_true")
    s.add_argument("--port", type=int)
    s.set_defaults(fn=cmd_serve)
    sub.add_parser("daemon", help=tr('daemon only')).set_defaults(fn=cmd_daemon)
    w = sub.add_parser("web", help=tr('Web UI only (connects to a running daemon)'))
    w.add_argument("--port", type=int)
    w.set_defaults(fn=cmd_web)
    sub.add_parser("list", help=tr('show HID devices and the matched layouts')).set_defaults(fn=cmd_list)

    sn = sub.add_parser("sniff", help=tr('hex inspector of hidden buttons'))
    sn.add_argument("path", nargs="?", help="/dev/hidrawN")
    sn.add_argument("--layout", help=tr('layout id (for the handshake and report_id)'))
    sn.add_argument("--learn", help=tr('button names separated by commas: M1,M2,M3,M4'))
    sn.add_argument("--seconds", type=float, default=0, help=tr('0 = until Ctrl-C'))
    sn.add_argument("--numbered", action="store_true", help=tr('the first byte of the report is the Report ID'))
    sn.set_defaults(fn=cmd_sniff)

    a = sub.add_parser("autolayout", help=tr('layout from the device HID descriptor (auto-mapping)'))
    a.add_argument("path", nargs="?", help="/dev/hidrawN")
    a.add_argument("--descriptor-file", help=tr('file with the descriptor (binary or hex text)'))
    a.add_argument("--vid", help=tr('VID (hex), if reading from a file'))
    a.add_argument("--pid", help=tr('PID (hex), if reading from a file'))
    a.add_argument("--fields", action="store_true", help=tr('only the table of descriptor fields'))
    a.add_argument("--save", action="store_true", help=tr('save into the devices/ directory as a regular layout'))
    a.set_defaults(fn=cmd_autolayout)

    mo = sub.add_parser("monitor", help=tr('live monitor: which buttons/axes are recognized by the layout'))
    mo.add_argument("path", help="/dev/hidrawN")
    mo.add_argument("--layout", help=tr('layout id (default: matched by VID/PID, then auto)'))
    mo.add_argument("--seconds", type=float, default=0, help=tr('0 = until Ctrl-C'))
    mo.add_argument("--raw", action="store_true", help=tr('print every raw report as hex (handshake diagnostics)'))
    mo.set_defaults(fn=cmd_monitor)

    rt = sub.add_parser("rumble-test", help=tr('check rumble manually (the layout template, your own, or a command sweep)'))
    rt.add_argument("path", help="/dev/hidrawN")
    rt.add_argument("--template", help=tr('e.g. "05 0f {strong} {weak}"'))
    rt.add_argument("--pad-to", type=int, default=0, help=tr('pad the report with zeros to N bytes'))
    rt.add_argument("--sweep", help=tr('ID:FROM-TO — sweep the second byte, e.g. 0x05:0x00-0x20'))
    rt.add_argument("--crc32-at", type=int, default=0, help=tr('DS4 over Bluetooth: put the CRC-32 (seed A2) into bytes N..N+3, e.g. 74'))
    rt.add_argument("--hold", type=float, default=1.0, help=tr('seconds per motor'))
    rt.set_defaults(fn=cmd_rumble_test)

    sub.add_parser("keyboards", help=tr('keyboards that can be used as a gamepad (see the keyboard section of daemon.json)')
                   ).set_defaults(fn=cmd_keyboards)

    sr = sub.add_parser("sdl-rumble", help=tr('rumble through SDL3 (reference); --trace shows what SDL writes to hidraw'))
    sr.add_argument("--trace", action="store_true", help=tr('run under strace and print the hex of the writes'))
    sr.set_defaults(fn=cmd_sdl_rumble)

    hh = sub.add_parser("hidhide", help=tr('Windows: HidHide commands to hide the original gamepad from games'))
    hh.add_argument("--vid-pid", required=True, metavar="VID:PID", help=tr('hex, e.g. 054c:09cc'))
    hh.add_argument("--run", action="store_true", help=tr('run the commands (administrator) instead of only printing them'))
    hh.set_defaults(fn=cmd_hidhide)

    tp = sub.add_parser("testpage", help=tr('a web page for checking buttons and axes in the browser (without the daemon)'))
    tp.add_argument("path", nargs="?", help=tr('/dev/hidrawN (can be chosen on the page)'))
    tp.add_argument("--layout", help=tr('layout id (default: by VID/PID, then auto)'))
    tp.add_argument("--port", type=int, default=8766)
    tp.add_argument("--no-browser", action="store_true", help=tr('do not open the browser automatically'))
    tp.set_defaults(fn=cmd_testpage)

    hp = sub.add_parser("handshake-probe", help=tr('automatic handshake search for a silent interface (library of known ones)'))
    hp.add_argument("path", help=tr('/dev/hidrawN (the vendor interface of a silent receiver)'))
    hp.add_argument("--broad", action="store_true", help=tr('broad search: also try other vendors’ handshakes'))
    hp.add_argument("--no-user", action="store_true", help=tr('the autonomous phase only (without asking to press buttons)'))
    hp.add_argument("--save", action="store_true", help=tr('save the result as devices/learned_VID_PID_ifN.json'))
    hp.add_argument("--auto-seconds", type=float, default=1.2)
    hp.add_argument("--user-seconds", type=float, default=2.5)
    hp.set_defaults(fn=cmd_handshake_probe)

    ca = sub.add_parser("calibrate", help=tr('calibration wizard: learn the buttons and axes of any hidraw gamepad'))
    ca.add_argument("path", help="/dev/hidrawN")
    ca.add_argument("--broad", action="store_true",
                    help=tr('if the device is silent, also try other vendors’ handshakes (SDL library)'))
    ca.add_argument("--paddles", action="store_true",
                    help=tr('add the ML/MR steps (side/back “paddles”; not every gamepad has them)'))
    ca.add_argument("--save", action="store_true", help=tr('save as devices/calibrated_VID_PID[_ifN].json'))
    ca.set_defaults(fn=cmd_calibrate)

    u = sub.add_parser("udev-rules", help=tr('print udev rules for the known layouts'))
    u.add_argument("--vid-pid", action="append", metavar="VID[:PID]", help=tr('additionally (hex), can be repeated'))
    u.add_argument("--hide", action="append", metavar="VID:PID",
                   help=tr('instead of access rules, print rules that hide the original gamepad from programs (SDL/Chrome/Steam will see only the virtual one); can be repeated'))
    u.add_argument("--hidraw-group", metavar="GROUP",
                   help=tr('together with --hide: print rules that close /dev/hidrawN itself to everyone except the GROUP group (the daemon is started with this group; no root). The file is 71-…, not 69-…'))
    u.set_defaults(fn=cmd_udev)

    uh = sub.add_parser("unhide", help=tr('Linux: restore permissions on the original gamepad nodes after a daemon crash'))
    uh.set_defaults(fn=cmd_unhide)
    sl = sub.add_parser("sdl-list", help=tr('what SDL3 sees (a hiding check): the list of joysticks'))
    sl.set_defaults(fn=cmd_sdl_list)

    sh = sub.add_parser("setup-hide", help=tr('Linux: automatic setup of hiding the original (group + udev + service), through sudo'))
    sh.add_argument("--hide", action="append", metavar="VID:PID", help=tr('what to hide (can be repeated)'))
    sh.add_argument("--auto", action="store_true", help=tr('hide all gamepads with a layout that are connected now'))
    sh.add_argument("--service", action="store_true", help=tr('install a systemd service: the daemon starts by itself as your user with CAP_FOWNER (it can close the original hidraw)'))
    sh.add_argument("--dry-run", action="store_true", help=tr('only show what would be done'))
    sh.add_argument("--uninstall", action="store_true", help=tr('remove the rules and the service'))
    sh.set_defaults(fn=cmd_setup_hide)

    c = sub.add_parser("check", help=tr('check a JSON profile'))
    c.add_argument("profile")
    c.add_argument("--layout")
    c.set_defaults(fn=cmd_check)

    b = sub.add_parser("bench", help=tr('measure the software latency of the Pipeline'))
    b.add_argument("--reports", type=int, default=200_000)
    b.add_argument("--profile", default="precision_aim")
    b.add_argument("--layout", default="sony_ds4_usb")
    b.set_defaults(fn=cmd_bench)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", type=Path)
    pre.add_argument("--config-dir", type=Path)
    known, _ = pre.parse_known_args(argv)
    init_language(known.config, known.config_dir)      # before build_parser: help texts are translated too
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return args.fn(args)
    except ConfigError as e:
        print(tr('configuration: {0}', e), file=sys.stderr)
        return 1
    except PermissionError as e:
        print(tr('no permission: {0}\nCheck the udev rules: gamepad-remapd udev-rules', e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
