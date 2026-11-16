"""Manual rumble check without the daemon or a game.

    gamepad-remapd rumble-test /dev/hidraw8
    gamepad-remapd rumble-test /dev/hidraw8 --template "05 0f {strong} {weak}"
    gamepad-remapd rumble-test /dev/hidraw8 --sweep 0x05:0x00-0x20

Takes the handshake and the rumble template from the layout (like the daemon), spins the strong motor for 1 s, then the weak one for 1 s,
then stops. `--template` overrides the template (placeholders {strong} {weak} — u8, {strong16}/{weak16} — u16 LE);
`--sweep ID:FROM-TO` iterates over the second byte of the report (the command) — when the real format is unknown: on
each value it prints what was sent, and you say at which one it buzzed.
"""
from __future__ import annotations

import sys
import time
from typing import Callable, Dict, Optional, TextIO

from ..input import enumerate_devices, open_device, run_handshake
from ..logic.layout import DeviceLayout, RumbleSpec, _parse_rumble
from .monitor_cli import _find_layout
from ..i18n import tr


def _spec(template: str, pad_to: int, crc32_at: int = 0) -> RumbleSpec:
    d = {"op": "output", "template": template, "pad_to": pad_to}
    if crc32_at:
        d["crc32"] = {"seed": "a2", "at": crc32_at}            # DS4 over Bluetooth: CRC-32 of A2 + report[:at]
    return _parse_rumble(d)


def run(path: str, layouts: Dict[str, DeviceLayout], backend: str = "auto", template: Optional[str] = None,
        pad_to: int = 0, sweep: Optional[str] = None, hold: float = 1.0, crc32_at: int = 0, *,
        enumerate_fn: Callable = enumerate_devices, open_fn: Callable = open_device,
        sleep: Callable = time.sleep, out: Optional[TextIO] = None) -> int:
    out = out or sys.stdout
    info = next((i for i in enumerate_fn(backend) if i.path == path), None)
    if info is None:
        print(tr('device {0} not found', path), file=sys.stderr)
        return 1
    lay = _find_layout(info, layouts)
    specs = []                                   # (label, RumbleSpec)
    if sweep:
        try:
            rid, rng = sweep.split(":")
            lo, hi = (int(x, 0) for x in rng.split("-"))
            rid = int(rid, 0)
        except ValueError:
            print(tr('--sweep: the format is ID:FROM-TO, e.g. 0x05:0x00-0x20'), file=sys.stderr)
            return 1
        pad_to = pad_to or 12                    # SDL reports of Flydigi — 12 bytes (same as GET_INFO)
        for cmd in range(lo, hi + 1):
            specs.append((f"{rid:02x} {cmd:02x} {{strong}} {{weak}}", _spec(f"{rid:02x} {cmd:02x} {{strong}} {{weak}}", pad_to, crc32_at)))
    elif template:
        specs.append((template, _spec(template, pad_to, crc32_at)))
    elif lay is not None and lay.rumble is not None:
        specs.append((tr('from the layout ') + lay.id, lay.rumble))
    else:
        print(tr('layout: {0} (bus={1}, interface={2}); inherited: {3}; it has no rumble template', lay.id if lay else '—', info.bus, info.interface, getattr(lay, 'inherited', None)), file=sys.stderr)
        print(tr('specify --template or --sweep; if this is a DS4/GPS13B, check that the updated logic/layout.py and layouts/sony_ds4_bt.json are installed (grep -c inherit_from_builtin src/gamepad_remapd/logic/layout.py)'),
              file=sys.stderr)
        return 2
    dev = open_fn(info, 64)
    try:
        if lay is not None and lay.handshake:
            run_handshake(dev, lay.handshake)
        print(tr('layout: {0}; templates: {1}', lay.id if lay else '—', len(specs)), file=out)
        failed = False
        for label, spec in specs:
            if failed:
                break
            for name, s, w in ((tr('STRONG'), 0xFFFF, 0), (tr('WEAK'), 0, 0xFFFF)):
                data = spec.build(s, w)
                print(f"[{label}] {name}: {data.hex(' ')}", file=out, flush=True)
                try:
                    dev.write_output(data)
                except OSError as e:
                    print(tr('  write error: {0} — the sweep is stopped. For Errno 110 (timeout) the device does not answer: the gamepad is off/not paired with the receiver, or the receiver hung — reconnect it and check `monitor` first.', e), file=out)
                    failed = True
                    break
                sleep(hold)
                dev.write_output(spec.build(0, 0))
                sleep(0.2)
    finally:
        dev.close()
    return 0
