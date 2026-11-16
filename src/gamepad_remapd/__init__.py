"""gamepad-remapd — an async gamepad remapper with minimal latency.

Architecture (processes and threads):

    ┌──────────────── "daemon" process ───────────────┐      ┌──── "web" process ───┐
    │ reader thread (hot path, SCHED_FIFO):           │      │ aiohttp + WebSocket   │
    │   hidraw read ─► ReportParser ─► Pipeline ─►    │      │ REST/JSON API         │
    │   uinput write (+SYN_REPORT)                    │      │ static Web UI        │
    │                                                 │ NDJSON│ (Canvas)              │
    │ asyncio loop (cold path): hotplug, profiles,    │◄─────►│                       │
    │   FF/rumble, sniffer, control server            │ unix/ │                       │
    └─────────────────────────────────────────────────┘ tcp   └───────────────────────┘

The hot path NEVER enters asyncio: a blocking read plus synchronous processing in a
dedicated thread gives the lowest and most stable latency.
"""
__version__ = "0.1.0"
