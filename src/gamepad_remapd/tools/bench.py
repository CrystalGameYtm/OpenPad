"""Software latency benchmark of the Pipeline (no hardware).

Measures the full chain for one report: filter -> parse (bitmask + struct) ->
XOR-diff of buttons -> deadzone+stick curve -> building events -> write() to
/dev/null (in essence the same system call as write to /dev/uinput,
without the kernel cost of delivering events to consumers).

What this does NOT measure: the USB polling interval of the receiver (1–8 ms — a hardware limit
that software cannot change) and the delivery of the event to the game inside the kernel.
"""
from __future__ import annotations

import os
import random
import time
from typing import List

from ..config import BUILTIN_LAYOUTS, BUILTIN_PROFILES
from ..logic.layout import DeviceLayout, ReportParser
from ..logic.pipeline import Pipeline
from ..logic.profile import compile_profile
import json


def synth_reports(layout: DeviceLayout, n: int, seed: int = 1) -> List[bytes]:
    """A plausible stream: "wandering" sticks, random buttons/hat, slow gyro."""
    rnd = random.Random(seed)
    size = max(layout.read_size, layout.min_len)
    buf = bytearray(size)
    if layout.report_id is not None:
        buf[0] = layout.report_id
    for f in layout.fields.values():
        if f.fmt == "u8":
            buf[f.offset] = int(f.center)
    out = []
    for _ in range(n):
        for name in ("LX", "LY", "RX", "RY"):
            f = layout.fields.get(name)
            if f and f.fmt == "u8":
                buf[f.offset] = max(0, min(255, buf[f.offset] + rnd.randint(-6, 6)))
        for b in layout.buttons.values():
            if rnd.random() < 0.01:
                buf[b.byte] ^= 1 << b.bit
        out.append(bytes(buf))
    return out


def run(reports: int = 200_000, profile: str = "precision_aim", layout_id: str = "sony_ds4_usb") -> dict:
    layout = DeviceLayout.from_file(BUILTIN_LAYOUTS / f"{layout_id}.json")
    if os.name == "posix":
        from ..output.uinput_linux import UinputPad, SPECS
        fd = os.open(os.devnull, os.O_WRONLY)
        out = UinputPad(SPECS["xbox360"], fd=fd)
    else:                                   # Windows: no uinput; the system call cost is not included in the measurement
        from ..output.dry import DryPad
        fd, out = -1, DryPad()
    pipe = Pipeline(out)
    parser = ReportParser(layout)
    pdata = json.loads((BUILTIN_PROFILES / f"{profile}.json").read_text(encoding="utf-8"))
    pipe.cp = compile_profile(pdata, layout, parser, out)
    data = synth_reports(layout, reports)
    for r in data[:2000]:                   
        pipe.process(r)
    pipe.reset_stats()
    t0 = time.perf_counter()
    proc = pipe.process
    for r in data:
        proc(r)
    wall = time.perf_counter() - t0
    os.close(fd)
    st = pipe.stats()
    st["throughput_reports_per_s"] = int(reports / wall)
    st["profile"] = profile
    st["layout"] = layout_id
    return st
