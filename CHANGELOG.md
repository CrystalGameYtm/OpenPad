# Changelog

## Unreleased
- New opt-in input backend `xusb`: `"xinput_backend": "usb"` in `daemon.json` reads wired Xbox 360-protocol XInput pads
  directly over USB (usbfs), without the kernel `xpad` driver (which can hang after a replug). Rumble goes through the OUT endpoint.
  Falls back to evdev when usbfs is not accessible. Input and rumble confirmed on a real pad (Flydigi Direwolf 3, XInput mode; rumble needs a good USB port, not a USB 2.0 hub); rebinding `xpad` after a crash not yet verified. udev rule added for access.
- Documentation in `docs/`: architecture, configuration, command-line, profiles, layouts, calibration, control protocol / Web API,
  troubleshooting, development. Fixed `list -v` in the Windows notes (`-v` goes before the command).
- Daemon/CLI messages are translatable too: English source text in code + `messages/<code>.json` catalogs (built-in `uk`,
  user overrides in `<config>/messages/`); language from `"language"` in `daemon.json` (`auto` = English; the web page follows the browser picker).
  The API now reports "not found" as code `not_found` (HTTP 404) independent of the message language.
- Web UI translations are now JSON files (`web/static/locales/*.json`, built-in `en` and `uk`; user files in `<config>/locales/`).
  The language comes from the header picker, `"language"` in `daemon.json`, or the browser. Applies to the main UI and the test page.
- CI: rolling `canary` pre-release built from `main` after the tests pass (`.github/workflows/canary.yml`).
- Triggers: sensitivity, travel inversion, `analog` / `digital` (+`threshold`) / `off` modes, two-stage actions
  `stages.soft|full` (button/key/mouse, thresholds with hysteresis), `button_value` — the LT/RT value while a button
  mapped to LT/RT is held.
- Web UI reworked: tabs Device / Buttons / Sticks / Triggers / Gyro / Profiles / Diagnostics / System;
  the profile bar (apply/save) is always at the top; responsive layout for phones.
- Functions that were previously CLI-only are now in the GUI: sdl-list, unhide, udev-rules, hidhide, keyboards, check
  (profile check), autolayout (layout from the descriptor), rumble-test (simple test), profile import/export/delete,
  JSON editor. New APIs: /api/system*, /api/profiles-check, /api/devices/{id}/autolayout|rumble.

## 0.2.0 — November 2026
* Remapping gamepad → keyboard keys and mouse (`KEY_*`, `MOUSE_*`), virtual keyboard (uinput / SendInput).
* D-pad as buttons mode (`dpad_mode: "buttons"`).
* Rumble: DS4 over Bluetooth (report `0x11` + CRC-32), `rumble-test`, `sdl-rumble`.
* Gyro: live °/s in the UI, zero calibration (`gyro.bias`).
* Web UI: "Protocol" tab (protocol description, device mode, output protocol, hiding the original); ML/MR removed from the UI.
* Output protocol per device: XInput emulation (recommended), native DS4 / DInput (experimental, with warnings).
* Hiding of the original rewritten: all nodes of the physical device (hidraw/event/js) are closed, tracked and restored;
  `setup-hide`, `unhide`, `sdl-list`, `serve --privileged`.
* Linux: udev rules for Bluetooth hidraw, friendlier messages and backoff when permissions are missing.
* Windows (basic, not verified on hardware): ViGEm, SendInput, HidHide, built-in WinAPI HID backend without hidapi.

## 0.1.0
* First public state: hidraw/evdev/hidapi → uinput, layouts, calibration, Web UI, profiles, udev.
