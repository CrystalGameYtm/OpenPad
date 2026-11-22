"""Service operations that used to exist only in the CLI — for the "System" tab of the Web UI.

Everything here is synchronous and short-lived; the server calls it via ``asyncio.to_thread``.
Nothing here changes the system without an explicit command (unhide, saving a layout).
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

from ..config import DaemonConfig, default_runtime_dir
from ..i18n import tr
from ..util import NotFoundError


class LayoutNotFound(ValueError, NotFoundError):
    pass


def system_info(cfg: DaemonConfig) -> Dict[str, Any]:
    hidden: List[str] = []
    state = default_runtime_dir() / "hidden.json"
    try:
        data = json.loads(state.read_text(encoding="utf-8"))
        hidden = sorted(data.get("nodes", data) if isinstance(data, dict) else data)
    except (OSError, ValueError):
        pass
    kb = cfg.keyboard
    return {"platform": "windows" if os.name != "posix" else sys.platform,
            "posix": os.name == "posix", "backend": cfg.backend,
            "config_dir": str(cfg.config_dir), "devices_dir": str(cfg.devices_dir),
            "hidden_nodes": [str(x) for x in hidden],
            "keyboard": {"enabled": kb.enabled, "device": list(kb.device), "toggle": list(kb.toggle)}}


def sdl_list() -> Dict[str, Any]:
    from ..tools.sdl_list import run
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = run(out=out)
    return {"rc": rc, "text": (out.getvalue() + err.getvalue()).strip()}


def unhide() -> Dict[str, Any]:
    if os.name != "posix":
        raise ValueError(tr('unhide is Linux-only'))
    from ..input.hide_linux import restore_stale
    done, bad = restore_stale(default_runtime_dir() / "hidden.json")
    try:                                    # direct-USB XInput: give interfaces left without a driver back to xpad
        from ..input.xusb_linux import reattach_kernel_drivers
        reattach_kernel_drivers()
    except Exception:  # noqa: BLE001
        pass
    return {"restored": [str(x) for x in done], "failed": [str(x) for x in bad]}


def udev_rules(layouts: dict, vid_pid: List[str], hide: List[str], group: Optional[str]) -> Dict[str, Any]:
    from ..tools.udev import generate, generate_hide, generate_hide_hidraw, pairs_from_layouts

    def pair(vp: str):
        vid, sep, pid = str(vp).partition(":")
        if not vid or (hide and not (sep and pid)):
            raise ValueError(tr('{0!r}: VID:PID format required (hex, e.g. 054c:09cc)', vp))
        return vid, pid or None, tr('manually: {0}', vp)
    if hide:
        pairs = [pair(v) for v in hide]
        text = generate_hide_hidraw(pairs, group) if group else generate_hide(pairs)
    else:
        pairs = pairs_from_layouts(layouts) + [pair(v) for v in vid_pid]
        text = generate(pairs)
    return {"text": text}


def hidhide_commands(vid: int, pid: int, paths: List[str]) -> Dict[str, Any]:
    from ..tools.hidhide import commands, instance_path
    ips = sorted({instance_path(p) for p in paths})
    return {"commands": [" ".join(f'"{a}"' if " " in a else a for a in c) for c in commands(ips)] if ips else []}


def keyboards(cfg: DaemonConfig) -> Dict[str, Any]:
    if os.name != "posix":
        return {"candidates": [], "note": tr('keyboard mode is Linux-only')}
    from ..input.keyboard_linux import _matches, enumerate_keyboards
    sel = list(cfg.keyboard.device)
    cands = enumerate_keyboards([])
    return {"candidates": [{"vid": f"{i.vid:04x}", "pid": f"{i.pid:04x}", "name": i.name or "?",
                            "paths": list(i.extra.get("paths", [])),
                            "selected": any(_matches(s, i.path, i.name, i.vid, i.pid) for s in sel)}
                           for i in cands],
            "note": "" if cands else tr('no keyboards found (or no permission to read /dev/input/event*)')}


def check_profile(data: Any, layout_id: Optional[str], layouts: dict) -> Dict[str, Any]:
    """Checks a profile; with a layout also compiles it (warnings about unavailable targets)."""
    from ..logic.layout import ReportParser
    from ..logic.profile import compile_profile, normalize_profile
    norm = normalize_profile(data)
    warnings: List[str] = []
    if layout_id:
        lay = layouts.get(layout_id)
        if lay is None:
            raise LayoutNotFound(tr('layout {0!r} not found', layout_id))
        if os.name == "posix":
            from ..output.uinput_linux import SPECS, UinputPad
            fd = os.open(os.devnull, os.O_WRONLY)
            try:
                cp = compile_profile(norm, lay, ReportParser(lay), UinputPad(SPECS["xbox360"], fd=fd))
            finally:
                os.close(fd)
        else:
            from ..output.dry import DryPad
            cp = compile_profile(norm, lay, ReportParser(lay), DryPad())
        warnings = list(cp.warnings)
    return {"ok": True, "warnings": warnings}


def autolayout(info, save: bool, devices_dir) -> Dict[str, Any]:
    """Layout from the device's HID descriptor (level 2), saved into the user directory if requested."""
    from ..logic.autolayout import build_layout_dict
    from ..logic.hid_descriptor import format_fields, parse_report_descriptor
    from ..logic.layout import DeviceLayout
    if not getattr(info, "descriptor", None):
        raise ValueError(tr("this device's report_descriptor is unavailable (Linux hidraw only)"))
    parsed = parse_report_descriptor(info.descriptor)
    d = build_layout_dict(parsed, info.vid, info.pid, info.name or "", info.interface)
    res: Dict[str, Any] = {"layout": d, "notes": list(d["_auto"]["notes"]), "fields": format_fields(parsed)}
    if save:
        from ..store import _atomic_write
        lid = f"auto_{info.vid:04x}_{info.pid:04x}" + (f"_if{info.interface}" if info.interface is not None else "")
        DeviceLayout.from_dict(d, lid)                 
        dest = devices_dir / f"{lid}.json"
        _atomic_write(dest, json.dumps(d, ensure_ascii=False, indent=2) + "\n")
        res["saved"] = str(dest)
        res["id"] = lid
    return res


def rumble(session, strong: float, weak: float, ms: int) -> Dict[str, Any]:
    """Short rumble test through the same path as game rumble."""
    lay = session.layout
    dev = session.dev
    if dev is None or ((lay is None or lay.rumble is None) and not getattr(dev, "rumble_ok", False)):
        raise ValueError(tr('rumble is not configured for this device (no rumble section in the layout)'))
    s16 = int(max(0.0, min(1.0, strong)) * 65535)
    w16 = int(max(0.0, min(1.0, weak)) * 65535)
    ms = max(50, min(3000, int(ms)))
    session._on_rumble(s16, w16)
    time.sleep(ms / 1000.0)
    session._on_rumble(0, 0)
    return {"ms": ms}
