"""Daemon entry point: initializes the components and runs the asyncio loop.

Startup order (important):
  1. tune_interpreter()   — GC/GIL settings BEFORE the reader threads are created;
  2. load layouts         — an error in one user file is not fatal;
  3. control server       — so the Web UI can connect right away;
  4. device manager       — the first scan opens the gamepads it finds.
"""
from __future__ import annotations

import asyncio
import logging
import multiprocessing
import os
import signal
from pathlib import Path
from typing import Dict, Optional

from .config import BUILTIN_PROFILES, DaemonConfig, read_or_create_token
from .core.manager import Manager
from .core.realtime import tune_interpreter
from .ipc.server import ControlServer
from .logic.layout import DeviceLayout, LayoutError
from .store import ProfileStore
from .i18n import tr

log = logging.getLogger(__name__)


def load_layouts_safe(dirs) -> Dict[str, DeviceLayout]:
    """Like logic.layout.load_layouts, but a broken file is only logged (the daemon does not
    crash because of an error in a user layout).
    """
    out: Dict[str, DeviceLayout] = {}
    for d in dirs:
        d = Path(d)
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.json")):
            if p.name.startswith("_"):
                continue                      
            try:
                lay = DeviceLayout.from_file(p)
            except (LayoutError, OSError) as e:
                log.error(tr('layout %s skipped: %s'), p, e)
                continue
            out[lay.id] = lay
    return out


async def amain(cfg: DaemonConfig, stop: Optional[asyncio.Event] = None) -> None:
    tune_interpreter(cfg.switch_interval_s)
    layouts = load_layouts_safe(cfg.layout_dirs())
    log.info(tr('layouts loaded: %s'), ", ".join(sorted(layouts)) or "—")
    store = ProfileStore(cfg.profiles_dir, BUILTIN_PROFILES, cfg.state_path)
    manager = Manager(cfg, store, layouts)

    endpoint = cfg.endpoint()
    token = read_or_create_token(cfg.token_path) if endpoint.startswith("tcp:") else None
    server = ControlServer(manager, store, endpoint, token)
    await server.start()
    await manager.start()

    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    if os.name == "posix":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)   
    try:
        await stop.wait()
    finally:
        log.info(tr('stopping…'))
        await manager.stop()                         # release_all + destroy the uinput devices
        await server.stop()
        if endpoint.startswith("unix:"):
            try:
                os.unlink(endpoint[5:])
            except OSError:
                pass


def run_daemon(cfg: DaemonConfig) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s daemon %(levelname)s %(message)s")
    try:
        asyncio.run(amain(cfg))
    except KeyboardInterrupt:
        pass


def run_all(cfg: DaemonConfig) -> None:
    """The daemon in this process + the Web UI in a SEPARATE process (multiprocessing, spawn mode).

    spawn (not fork): the child does not inherit the daemon's threads/descriptors and
    works the same on Windows. A crash of the Web process does not affect the daemon.
    """
    proc = None
    if cfg.web_enabled:
        try:
            from .web.app import run_web
        except ImportError as e:        # aiohttp not installed: the daemon must work without the UI
            log.warning(tr("Web UI disabled: aiohttp is required (pip install 'gamepad-remapd[web]'): %s"), e)
        else:
            ctx = multiprocessing.get_context("spawn")
            proc = ctx.Process(target=run_web, args=(cfg,), name="gamepad-remapd-web", daemon=True)
            proc.start()
    try:
        run_daemon(cfg)
    finally:
        if proc is not None and proc.is_alive():
            proc.terminate()
            proc.join(3)
