"""Точка входу процесу-демона: збирає всі компоненти та запускає asyncio-цикл.

Порядок запуску (важливий):
  1. tune_interpreter()   — GC/GIL налаштування ДО створення потоків-читачів;
  2. завантаження розкладок (помилка в одному користувацькому файлі не фатальна);
  3. control-сервер       — щоб Web UI міг підключитись одразу;
  4. менеджер пристроїв   — перший scan відкриває знайдені геймпади.
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

log = logging.getLogger(__name__)


def load_layouts_safe(dirs) -> Dict[str, DeviceLayout]:
    """Як logic.layout.load_layouts, але зламаний файл лише логується (демон не падає
    через помилку в користувацькій розкладці)."""
    out: Dict[str, DeviceLayout] = {}
    for d in dirs:
        d = Path(d)
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.json")):
            if p.name.startswith("_"):
                continue                      # шаблони
            try:
                lay = DeviceLayout.from_file(p)
            except (LayoutError, OSError) as e:
                log.error("розкладка %s пропущена: %s", p, e)
                continue
            out[lay.id] = lay
    return out


async def amain(cfg: DaemonConfig, stop: Optional[asyncio.Event] = None) -> None:
    tune_interpreter(cfg.switch_interval_s)
    layouts = load_layouts_safe(cfg.layout_dirs())
    log.info("завантажено розкладок: %s", ", ".join(sorted(layouts)) or "—")
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
            loop.add_signal_handler(sig, stop.set)   # коректне завершення: відпускаємо кнопки
    try:
        await stop.wait()
    finally:
        log.info("зупинка…")
        await manager.stop()                         # release_all + знищення uinput-пристроїв
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
    """Демон у цьому процесі + Web UI в ОКРЕМОМУ процесі (multiprocessing, режим spawn).

    spawn (а не fork): дочірній процес не успадковує потоки/дескриптори демона й
    однаково працює на Windows. Падіння Web-процесу не зачіпає демон.
    """
    proc = None
    if cfg.web_enabled:
        try:
            from .web.app import run_web
        except ImportError as e:        # aiohttp не встановлено: демон повинен працювати й без UI
            log.warning("Web UI вимкнено: потрібен aiohttp (pip install 'gamepad-remapd[web]'): %s", e)
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
