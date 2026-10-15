"""gamepad-remapd — асинхронний ремапер геймпадів з мінімальною затримкою.

Архітектура (процеси та потоки):

    ┌──────────────── процес «daemon» ────────────────┐      ┌──── процес «web» ────┐
    │ потік-читач (hot path, SCHED_FIFO):             │      │ aiohttp + WebSocket   │
    │   hidraw read ─► ReportParser ─► Pipeline ─►    │      │ REST/JSON API         │
    │   uinput write (+SYN_REPORT)                    │      │ статичний Web UI      │
    │                                                 │ NDJSON│ (Canvas)              │
    │ asyncio-цикл (cold path): hotplug, профілі,     │◄─────►│                       │
    │   FF/rumble, sniffer, control-сервер            │ unix/ │                       │
    └─────────────────────────────────────────────────┘ tcp   └───────────────────────┘

Гарячий шлях НІКОЛИ не заходить в asyncio: блокуюче читання + синхронна
обробка в окремому потоці дають найменшу й найстабільнішу затримку.
"""
__version__ = "0.1.0"
