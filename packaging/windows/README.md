# Windows

> The Windows part is written from documentation (ViGEm/vgamepad, hidapi, WinAPI SendInput, HidHide) and verified
> only on Linux with simulations in `tests/test_windows.py`. It has not been run on real Windows yet:
> expect minor bugs and report them.

## Setup

1. The **ViGEmBus** driver (https://github.com/nefarius/ViGEmBus/releases), then a reboot.
2. Python 3.11+ and `pip install ".[windows]"` (pulls in `hidapi`, `vgamepad`, `aiohttp`).
3. `gamepad-remapd list` — should show your gamepad/receiver (the `hidapi` backend). If a layout exists — good; if not — `gamepad-remapd calibrate <path>`
   (the path is from the `list` column) will learn any HID gamepad.
4. `gamepad-remapd serve` — the daemon + Web UI at http://127.0.0.1:8765/.

## What works

* Input through hidapi (Report ID, handshake, rumble — the same layouts as on Linux; the USB/Bluetooth bus
  is taken from the `bus_type` of hidapi ≥ 0.13).
* Output — a virtual **Xbox 360** (ViGEm). The game's rumble is sent to the physical gamepad.
* **Keyboard and mouse** as button targets (`KEY_F`, `MOUSE_LEFT` …) — through SendInput, with scan codes.
* The control channel — TCP `127.0.0.1:8764` with a token in `%APPDATA%\gamepad-remapd\control.token`.
* The reader thread: `THREAD_PRIORITY_HIGHEST` and a 1 ms timer are set automatically.

## Hiding the original

Otherwise the game sees both the physical and the virtual gamepad. Install **HidHide**
(https://github.com/nefarius/HidHide), then:

```
gamepad-remapd hidhide --vid-pid 054c:09cc
gamepad-remapd hidhide --vid-pid 054c:09cc --run
```

The commands: allow the Python process to see the hidden device, hide the device, enable hiding.

## Limitations

* XInput has no M1–M6/C/Z/ML/MR buttons: such targets are skipped (a warning in the profile). Remap them to
  existing buttons (`"M1": "LB"`) or to keys (`"M1": "KEY_F"`).
* DS4 output mode (`output_kind: ds4`) is not supported on Windows — it is always Xbox 360.
* "Keyboard as a gamepad" (`keyboard` in `daemon.json`) — Linux only.
* Auto-mapping from the HID descriptor is not implemented on Windows (it needs HidP_*): for unknown devices use
  `calibrate`.
* `bench` on Windows measures the Pipeline without the cost of system calls (dry output).
* Autostart: Task Scheduler → "At log on" → `pythonw -m gamepad_remapd serve`.

## Without hidapi

If `pip install hidapi` does not build (Python 3.14, no Visual C++ Build Tools), nothing needs to be installed:
on Windows the daemon falls back to the built-in `winhid` backend (WinAPI through ctypes). It is enough to run:

    pip install aiohttp vgamepad      # + the ViGEmBus driver
    python -m gamepad_remapd list

`backend: "winhid"` in the config forces it on. This backend has not yet been checked on real Windows — if `list`
shows nothing or crashes, send the output of `python -m gamepad_remapd -v list`.
