"""Live layout monitor: checks that buttons and axes are recognized CORRECTLY.

    gamepad-remapd monitor /dev/hidraw8

Reads raw reports, parses them by the layout (saved or built from the descriptor) and
prints events: which button was pressed, which output target it maps to, and large axis changes.
Emulates and changes nothing — safe to run next to a game or the daemon (several processes can read hidraw at once).

Typical check of the auto-layout: press each button in turn — and compare the
name in the console with the label on the gamepad. Mismatches are fixed in the profile's `button_map`
or in the layout (`target`).
"""
from __future__ import annotations

import sys
import threading
import time
from typing import Callable, Dict, List, Optional, TextIO

from ..input import DeviceGone, enumerate_devices, open_device, run_handshake
from ..logic.autolayout import try_layout
from ..logic.layout import HAT_DEFAULT_TARGETS, DeviceLayout, ReportParser
from ..i18n import tr

AXIS_PRINT_FRACTION = 0.25      


def _find_layout(info, layouts: Dict[str, DeviceLayout]) -> Optional[DeviceLayout]:
    if info.backend in ("evdev", "xusb"):
        return try_layout(info)                  # evdev: synthetic report, the saved hidraw layouts do not fit
    cands = [l for l in layouts.values() if l.matches(info.vid, info.pid, info.interface, info.bus)]
    cands.sort(key=lambda l: (l.interface is None, -l.priority))       
    return cands[0] if cands else try_layout(info)


def run(path: str, layouts: Dict[str, DeviceLayout], layout_id: Optional[str] = None,
        seconds: float = 0.0, backend: str = "auto", raw_dump: bool = False, *,
        enumerate_fn: Callable = enumerate_devices, open_fn: Callable = open_device,
        out: Optional[TextIO] = None) -> int:
    out = out or sys.stdout
    info = next((i for i in enumerate_fn(backend) if i.path == path), None)
    if info is None:
        print(tr('device {0} not found', path), file=sys.stderr)
        return 1
    layout = layouts.get(layout_id) if layout_id else _find_layout(info, layouts)
    if layout is None:
        print(tr('{0}: there is no layout for this interface (and it does not look like a gamepad). Try another hidraw of the same device: gamepad-remapd list', path), file=sys.stderr)
        return 1
    pr = ReportParser(layout)
    dev = open_fn(info, layout.read_size)
    print(tr('{0} {1:04x}:{2:04x} {3}\n'
             'layout: {4}{5}\n'
             'Press buttons and move the sticks. Ctrl-C to exit.\n', info.path, info.vid, info.pid, info.name, layout.id, tr(' (auto, needs checking)') if layout.generated else ''), file=out, flush=True)
    stop = threading.Event()
    timer = None
    if seconds > 0:
        timer = threading.Timer(seconds, lambda: (stop.set(), dev.wake()))
        timer.daemon = True
        timer.start()
    prev_word: Optional[int] = None
    last_axis: Dict[str, int] = {}
    n = 0
    try:
        if layout.handshake:
            run_handshake(dev, layout.handshake)
        while not stop.is_set():
            raw = dev.read()
            if raw is None:
                continue
            if raw_dump:        
                bad = len(raw) < pr.min_len or (pr.rid is not None and raw[0] != pr.rid) \
                    or (pr.sync is not None and raw[pr.sync[0]] != pr.sync[1])
                print(f"{'x' if bad else ' '} {len(raw):>3}B  {raw.hex(' ')}", file=out, flush=True)
            if len(raw) < pr.min_len or (pr.rid is not None and raw[0] != pr.rid) \
                    or (pr.sync is not None and raw[pr.sync[0]] != pr.sync[1]):
                continue                                  
            n += 1
            word, vals = pr.parse(raw)
            if prev_word is None:
                prev_word = word                          
                last_axis = {name: vals[i] for name, i in pr.index.items()}
                continue
            changed = word ^ prev_word
            prev_word = word
            while changed:
                low = changed & -changed
                bit = low.bit_length() - 1
                changed ^= low
                name = pr.src_names[bit] if bit < len(pr.src_names) else None
                if name is None:
                    continue                              
                if name in layout.buttons:
                    target = layout.buttons[name].target
                else:
                    target = HAT_DEFAULT_TARGETS.get(name)
                sign = "+" if word >> bit & 1 else "-"
                print(f"{sign} {name:<16} → {target or '—'}", file=out, flush=True)
            for name, idx in pr.index.items():
                f = layout.fields[name]
                if abs(vals[idx] - last_axis[name]) >= (f.raw_max - f.raw_min) * AXIS_PRINT_FRACTION:
                    last_axis[name] = vals[idx]
                    print(f"  {name:<14} = {vals[idx]}", file=out, flush=True)
    except DeviceGone:
        print(tr('\ndevice disconnected'), file=out)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if timer:
            timer.cancel()
        dev.close()
    if n == 0:
        print(tr('\n'
                 'No reports arrived. Turn the gamepad on and press any button (a 2.4 GHz gamepad falls asleep and then stays silent); make sure this is the right hidraw interface.'), file=out)
    return 0
