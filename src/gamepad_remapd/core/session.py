"""Session for one physical device: hidraw -> parsing -> mapping -> virtual gamepad.

This is the daemon core. Threading model:

    reader thread (this module, `_reader`)       asyncio thread (manager/control)
    ──────────────────────────────────           ───────────────────────────────
    poll()/read()  — blocks in the kernel        compile_profile(...)   (expensive)
    Pipeline.process(raw)  — synchronous         pipeline.cp = new_cp   (atomic swap)
    write(uinput)  — one call per report         rumble: FF event -> write_output()

The reader thread knows nothing about asyncio and never waits on any lock: the
interaction with the control thread is plain reference assignment (`pipeline.cp`,
`pipeline.sniffer`), which is atomic in CPython. So neither the profile nor the
sniffer adds latency.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from ..config import DaemonConfig
from ..input import DeviceGone, DeviceInfo, InputDevice, is_streaming, open_device as _default_open, run_handshake
from ..logic.layout import DeviceLayout, ReportParser
from ..logic.pipeline import Pipeline
from ..logic.profile import CompiledProfile, ProfileError, compile_profile, merge_patch
from ..logic.sniffer import HexSniffer
from ..logic.protocol import describe_protocol
from ..output import create_output as _default_create_output
from ..output.base import OutputBackend
from ..output.null import NullPad
from ..store import ProfileStore, StoreError
from .realtime import boost_current_thread
from ..i18n import tr

log = logging.getLogger(__name__)


def device_id(info: DeviceInfo) -> str:
    """Stable short id for the API: ``hidraw3`` (Linux) or ``hid-1a2b3c4d`` (hidapi)."""
    if info.backend in ("hidraw", "evdev"):
        return os.path.basename(info.path)
    if info.backend == "xusb":
        return "usb-{0}-{1}".format("-".join(info.path.rstrip("/").split("/")[-2:]), info.interface or 0)
    return "hid-" + hashlib.sha1(info.path.encode()).hexdigest()[:8]


def device_key(info: DeviceInfo) -> str:
    """Key for the "active profile": the device model, not a specific hidrawN."""
    return f"{info.vid:04x}:{info.pid:04x}"


class Session:
    def __init__(self, info: DeviceInfo, layout: Optional[DeviceLayout], cfg: DaemonConfig,
                 store: ProfileStore, *,
                 open_fn: Callable[[DeviceInfo, int], InputDevice] = _default_open,
                 output_fn: Callable[..., OutputBackend] = _default_create_output,
                 on_dead: Optional[Callable[["Session"], None]] = None,
                 opts: Optional[dict] = None):
        self.id = device_id(info)
        self.opts = dict(opts or {})               # {output_kind, hide} for this model (the Protocol tab)
        self.output_kind = self.opts.get("output_kind") or cfg.output_kind
        self.hide_state: Dict[str, Any] = {"requested": False, "mode": "off", "nodes": [], "failed": []}
        self._hider = None
        self.info = info
        self.layout = layout                       # None => sniffing-only mode
        self.cfg = cfg
        self.store = store
        self._open_fn = open_fn
        self._output_fn = output_fn
        self._on_dead = on_dead                    
        self.dev: Optional[InputDevice] = None
        self.out: OutputBackend = NullPad()
        self.parser: Optional[ReportParser] = None
        self.pipeline: Optional[Pipeline] = None
        self.profile_name: Optional[str] = None
        self.warnings: List[str] = []
        self.rt_mode = "n/a"
        self.error: Optional[str] = None
        self.errors = 0                            
        self.started_at = 0.0
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._wlock = threading.Lock()             
        self._last_snap: Optional[dict] = None     

    # ===================================================================
    
    # ===================================================================
    def prepare(self) -> None:
        """BLOCKING preparation (call in an executor): opens the device, performs the
        handshake, creates the virtual gamepad and compiles the initial profile.
        """
        lay = self.layout
        # 1) open /dev/hidrawN (or the hidapi path) with the required report size
        self.dev = self._open_fn(self.info, lay.read_size if lay else 64)
        try:
            # 2) handshake: switch the 2.4 GHz receiver into vendor mode (feature/output reports)
            if lay:
                self.parser = ReportParser(lay)
            if lay and lay.handshake:
                if self.parser.is_distinctive and is_streaming(self.dev, self.parser.accepts):
                    log.info(tr('%s: the device already delivers reports in the required mode — handshake skipped'), self.id)
                else:
                    run_handshake(self.dev, lay.handshake)
            if lay:
                # 3) exclusively grab the system evdev nodes of this device so the game
                
                hide = bool(self.opts.get("hide", self.cfg.grab_siblings))
                self.hide_state = {"requested": hide, "mode": "off", "nodes": [], "failed": []}
                if hide:
                    self._hide_original()
                else:
                    log.info(tr('%s: hiding the original is DISABLED in the model settings of %s — SDL/games will see the original too (enable: the “Protocol” tab in the web UI)'), self.id, device_key(self.info))
                from ..logic.outputs import output_choices, warn_for
                w = warn_for(output_choices(self.info.vid, lay.id), self.output_kind)
                if w:
                    log.warning(tr('%s: output “%s”: %s'), self.id, self.output_kind, w)
                # 4) create the virtual gamepad (uinput / ViGEm)
                extra = {} if self.cfg.dpad_mode == "hat" else {"dpad_mode": self.cfg.dpad_mode}
                self.out = self._output_fn(self.output_kind,
                                           name=self.cfg.output_name or f"{lay.name} (remapped)",
                                           uinput_path=self.cfg.uinput_path, **extra)
                self.out.open()
                self.out.on_rumble = self._on_rumble
            self.pipeline = Pipeline(self.out)
            if lay:
                self._load_initial_profile()
            else:
                
                self.pipeline.sniffer = HexSniffer(numbered=False)
        except BaseException:
            self._teardown()
            raise
        self.started_at = time.time()

    def _hide_original(self) -> None:
        """Close ALL nodes of the physical device for other programs (see input/hide_linux.py).
        Only when this succeeds: a "half-hide" (EVIOCGRAB alone) leaves a dead duplicate
        of the gamepad in SDL, so without permissions we do not hide at all.
        """
        if self.info.backend == "xusb":                    # the kernel driver is detached: no nodes exist to hide
            self.hide_state["mode"] = "n/a"
            log.info(tr('%s: read directly over USB — the kernel creates no gamepad node, nothing to hide'), self.id)
            return
        if os.name != "posix" or self.info.backend not in ("hidraw", "evdev"):
            self.hide_state["mode"] = "n/a"
            return
        from ..input.evdev_linux import EvdevDevice
        from ..input.hidraw_linux import HidrawDevice
        if not isinstance(self.dev, (HidrawDevice, EvdevDevice)):      
            self.hide_state["mode"] = "n/a"
            return
        from ..input.hide_linux import Hider
        from ..config import default_runtime_dir
        self._hider = Hider(state_path=default_runtime_dir() / "hidden.json")
        res = self._hider.apply(self.info.path, self.info.backend)
        self.hide_state.update(res.as_dict())
        if res.mode in ("hidden", "partial"):
            n = self.dev.grab_siblings()               
            log.info(tr('%s: original hidden (%d node(s)%s), evdev grabbed: %d'), self.id, len(res.nodes),
                     tr(', partially') if res.mode == "partial" else "", n)
        elif res.mode == "denied":
            log.warning(tr('%s: original NOT hidden: no permission to close the nodes (%s). SDL/games will see both the original and the virtual gamepad. CAP_FOWNER is required: `setup-hide --auto --service` or `serve --privileged`'),
                        self.id, res.failed[0] if res.failed else "")
        else:
            log.info(tr('%s: no nodes to hide found (sysfs)'), self.id)

    def activate(self, loop) -> None:
        """In the asyncio thread: subscribes to FF reads and starts the reader thread."""
        self.out.attach_loop(loop)
        self._running = True
        self._thread = threading.Thread(target=self._reader, name=f"reader-{self.id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stops the reader, releases all buttons and destroys the virtual device."""
        self._running = False
        if self.dev is not None:
            self.dev.wake()                         
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)
        self._teardown()

    def _teardown(self) -> None:
        try:
            if self.pipeline is not None:
                self.out.release_all()              
        except Exception:  # noqa: BLE001
            pass
        if self._hider is not None:
            self._hider.release()                   
            self._hider = None
        for obj in (self.out, self.dev):
            try:
                if obj is not None:
                    obj.close()
            except Exception:  # noqa: BLE001
                log.debug(tr('close error'), exc_info=True)
        self.dev = None

    # ===================================================================
    
    # ===================================================================
    def _reader(self) -> None:
        
        if self.cfg.realtime:
            self.rt_mode = boost_current_thread(self.cfg.realtime_priority)
            log.info(tr('%s: reader thread, scheduler mode: %s'), self.id, self.rt_mode)
        
        read = self.dev.read
        process = self.pipeline.process
        last_log = 0.0
        try:
            while self._running:
                raw = read()                        
                if raw is None:                     # wake() or timeout -> re-check _running
                    continue
                try:
                    process(raw)                    
                except Exception:  # noqa: BLE001  
                    self.errors += 1
                    now = time.monotonic()
                    if now - last_log > 5.0:        
                        last_log = now
                        log.exception(tr('%s: report handling error (total %d)'), self.id, self.errors)
        except DeviceGone as e:
            self.error = tr('device disconnected: {0}', e)
            log.warning("%s: %s", self.id, self.error)
        except Exception as e:  # noqa: BLE001
            self.error = tr('reader failure: {0}', e)
            log.exception(tr('%s: reader failure'), self.id)
        finally:
            self._running = False
            cb = self._on_dead
            if cb is not None:
                cb(self)                            

    # ===================================================================
    
    # ===================================================================
    def _load_initial_profile(self) -> None:
        """Active profile for this model -> default from configuration -> empty."""
        names = [self.store.get_active(device_key(self.info)), self.cfg.default_profile]
        for name in names:
            if not name:
                continue
            try:
                self.set_profile(self.store.get(name))
                return
            except (ProfileError, StoreError) as e:
                log.warning(tr('%s: profile %r not applied: %s'), self.id, name, e)
        self.set_profile({"name": "fallback"})

    def set_profile(self, data: Dict[str, Any]) -> CompiledProfile:
        """Compiles the profile (outside the hot thread) and HOT-swaps it in.

                The expensive work (curve LUTs, bit tables) happens here; the reader thread
                sees the new `pipeline.cp` on the next report and resets its state itself.
        """
        if self.layout is None or self.parser is None:
            raise ProfileError(tr('session without a layout: profiles are unavailable (sniffing only)'))
        cp = compile_profile(data, self.layout, self.parser, self.out)
        self.pipeline.cp = cp                       
        self.profile_name = cp.name
        self.warnings = list(cp.warnings)
        return cp

    def patch_profile(self, patch: Dict[str, Any]) -> CompiledProfile:
        """JSON merge-patch (RFC 7386) over the active profile: "changed the slider — it applies"."""
        cp = self.pipeline.cp
        if cp is None:
            raise ProfileError(tr('profile not loaded yet'))
        merged = merge_patch(cp.data, patch)
        merged["name"] = cp.data["name"]            
        return self.set_profile(merged)

    @property
    def profile_data(self) -> Optional[Dict[str, Any]]:
        cp = self.pipeline.cp if self.pipeline else None
        return cp.data if cp else None

    # ===================================================================
    # SNIFFER
    # ===================================================================
    def sniff(self, action: str, name: Optional[str] = None, baseline_s: Optional[float] = None) -> dict:
        p = self.pipeline
        if action == "start":
            lay = self.layout
            p.sniffer = HexSniffer(
                baseline_s=baseline_s or 2.5,
                numbered=(lay.report_id is not None) if lay else False,
                report_id=lay.report_id if lay else None)
        elif action == "stop":
            p.sniffer = None
            return {"phase": "off"}
        elif p.sniffer is None:
            raise ProfileError(tr('sniffer is not running (action=start)'))
        elif action == "reset":
            p.sniffer.reset()
        elif action == "learn":
            if not name:
                raise ProfileError(tr('learn: a button name is required (name)'))
            p.sniffer.learn(name)
        elif action != "summary":
            raise ProfileError(tr('unknown sniffer action: {0!r}', action))
        return p.sniffer.summary()

    # ===================================================================
    # RUMBLE (game FF request -> physical device)
    # ===================================================================
    def _on_rumble(self, strong16: int, weak16: int) -> None:
        lay, dev = self.layout, self.dev
        if dev is None:
            return
        if (lay is None or lay.rumble is None) and getattr(dev, "rumble_ok", False):
            try:                                    # evdev device: the kernel knows the format itself (FF_RUMBLE)
                with self._wlock:
                    dev.set_rumble(strong16, weak16)
            except Exception as e:  # noqa: BLE001
                log.warning(tr('%s: rumble not delivered: %s'), self.id, e)
            return
        if lay is None or lay.rumble is None:
            return
        spec = lay.rumble
        try:
            report = spec.build(strong16, weak16)
            with self._wlock:                       
                if spec.op == "feature":
                    dev.send_feature(report)
                else:
                    dev.write_output(report)
        except Exception as e:  # noqa: BLE001
            log.warning(tr('%s: rumble not delivered: %s'), self.id, e)

    # ===================================================================
    
    # ===================================================================
    def snapshot(self) -> Dict[str, Any]:
        """Live state for the Web UI. Called from the asyncio thread ~60 Hz; it only READS
        pipeline attributes (atomic in CPython), so the hot path is not slowed down.

                src : hex string of the input button bitmask (arbitrary length, no limit on
                      the number of buttons; JS parses it with BigInt)
                out : names of the virtual buttons currently held
                ax  : last values sent for LX LY RX RY (int16) and LT RT (0..255)
                raw : RAW sticks, normalized to [-1,1] (before deadzone/curves) so the UI
                      shows what the dead zone actually does
        """
        p = self.pipeline
        snap: Dict[str, Any] = {"dev": self.id, "src": format(p.src_prev, "x"),
                                "ax": list(p.last), "out": [], "raw": None}
        cp = p.cp
        if cp is not None:
            try:
                names = cp.target_names
                snap["out"] = [names[i] for i, r in enumerate(list(p.ref)) if r > 0 and i < len(names) and names[i]]
                raw = p.last_raw
                if raw and len(raw) >= cp.parser.min_len:
                    _, vals = cp.parser.parse(raw)
                    snap["raw"] = [self._norm(cp.left, vals, 0), self._norm(cp.left, vals, 1),
                                   self._norm(cp.right, vals, 0), self._norm(cp.right, vals, 1)]
                    g = self._gyro_rates(cp, vals)
                    if g is not None:
                        snap["gyro"] = g
            except Exception:  # noqa: BLE001 — UI state must never crash the daemon
                pass
        sn = p.sniffer
        if sn is not None:
            s = sn.summary()
            snap["sniff"] = {"phase": s["phase"], "progress": s["progress"], "hex": s["hex"]}
        return snap

    def _gyro_rates(self, cp, vals: tuple) -> Optional[List[Optional[int]]]:
        """Rotation rates [yaw, pitch, roll] in °/s (with the profile bias applied, no inversions) for the live UI view."""
        lg = self.layout.gyro if self.layout else None
        if not lg or self.parser is None:
            return None
        idx, sc = self.parser.index, float(lg["dps_per_lsb"])
        by, bp = cp.data["gyro"]["bias"]
        out: List[Optional[int]] = []
        for key, b in (("yaw", by), ("pitch", bp), ("roll", 0.0)):
            name = lg.get(key)
            out.append(round((vals[idx[name]] - b) * sc) if isinstance(name, str) and name in idx else None)
        return out

    @staticmethod
    def _norm(stick, vals: tuple, axis: int) -> Optional[float]:
        if stick is None:
            return None
        if axis == 0:
            return round((vals[stick.ix] - stick.cx) * stick.kx, 3)
        return round((vals[stick.iy] - stick.cy) * stick.ky, 3)

    def describe(self) -> Dict[str, Any]:
        """Static session description for the device list (JSON-compatible)."""
        d: Dict[str, Any] = {**self.info.as_dict(), "id": self.id, "open": True,
                             "layout": self.layout.id if self.layout else None,
                             "layout_name": self.layout.name if self.layout else None,
                             "profile": self.profile_name, "warnings": self.warnings,
                             "running": self._running, "error": self.error, "rt": self.rt_mode,
                             "errors": self.errors, "sniff_only": self.layout is None,
                             "protocol": describe_protocol(self.info, self.layout),
                             "output_kind": self.output_kind, "hide_state": self.hide_state}
        if self.parser is not None:
            d["inputs"] = [[i, n] for i, n in enumerate(self.parser.src_names) if n]
            d["default_targets"] = {n: b.target for n, b in self.layout.buttons.items()}
            d["has_gyro"] = bool(self.layout.gyro)
        return d

    def stats(self) -> Dict[str, Any]:
        return self.pipeline.stats() if self.pipeline else {}
