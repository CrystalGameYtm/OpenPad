"""Console hex inspector: finds the bits of "hidden" buttons (M1–M4/C/Z...).

    gamepad-remapd sniff /dev/hidraw3 --layout my_receiver --learn M1,M2,M3,M4

Without --learn it only shows live candidates. With --learn it asks in turn to hold
each button and prints a ready fragment of the "buttons" section for the JSON layout.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from typing import List, Optional

from ..input import DeviceGone, enumerate_devices, open_device, run_handshake
from ..logic.layout import DeviceLayout
from ..logic.sniffer import HexSniffer
from ..i18n import tr


def run(path: Optional[str], layout: Optional[DeviceLayout], seconds: float,
        learn: List[str], numbered: bool, backend: str = "auto") -> int:
    infos = enumerate_devices(backend)
    if path is None:
        for i in infos:
            print(f"{i.path}  {i.vid:04x}:{i.pid:04x}  iface={i.interface}  {i.name}")
        print(tr('\nSpecify the device path: gamepad-remapd sniff /dev/hidrawN'), file=sys.stderr)
        return 2
    info = next((i for i in infos if i.path == path), None)
    if info is None:
        print(tr('device {0} not found', path), file=sys.stderr)
        return 1
    dev = open_device(info, layout.read_size if layout else 64)
    if layout and layout.handshake:
        print("handshake…")
        run_handshake(dev, layout.handshake)
    sn = HexSniffer(numbered=numbered or bool(layout and layout.report_id is not None),
                    report_id=layout.report_id if layout else None)
    stop = threading.Event()

    def reader() -> None:
        try:
            while not stop.is_set():
                raw = dev.read()
                if raw:
                    sn.feed(raw)
        except DeviceGone:
            print(tr('\ndevice disconnected'))
            stop.set()

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    print(f"{info.path} {info.vid:04x}:{info.pid:04x} {info.name}")
    print(tr('Do not touch the gamepad for ~3 s (idle reference)…'))
    t_end = time.monotonic() + seconds if seconds > 0 else None
    queue = list(learn)
    try:
        while not stop.is_set() and (t_end is None or time.monotonic() < t_end):
            time.sleep(0.25)
            s = sn.summary()
            if s["phase"] == "baseline":
                continue
            if queue and s["learning"] is None:
                name = queue.pop(0)
                sn.learn(name)
                print(tr('\n→ hold the button “{0}”…', name), flush=True)
            cands = ", ".join(f"b{c['byte']}.{c['bit']}{'*' if c['down'] else ''}" for c in s["candidates"]) or "—"
            sys.stdout.write(tr('\r{0:<60} candidates: {1:<70}', s['hex'][:60], cands[:70]))
            sys.stdout.flush()
            if learn and not queue and s["learning"] is None and len(s["learned"]) >= len(learn):
                break
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        dev.wake()
        t.join(1.0)
        dev.close()
    s = sn.summary()
    print("\n")
    for h in s["hints"]:
        print(tr('hint:'), h)
    snippet = s["layout_snippet"] or {f"BTN_{c['byte']}_{c['bit']}": {"byte": c["byte"], "bit": c["bit"], "target": None}
                                       for c in s["candidates"]}
    print(tr('Fragment for the layout "buttons" section (replace target with M1/M2/…):'))
    print(json.dumps(snippet, indent=2, ensure_ascii=False))
    return 0

