# Troubleshooting

Useful first steps for almost any problem:

```sh
gamepad-remapd -v list        # what the daemon can see (-v goes before the command)
gamepad-remapd sdl-list       # what SDL (games, emulators) sees
gamepad-remapd testpage       # buttons, axes and raw report bytes in the browser, without the daemon
```

The log of `serve` (or `journalctl -u gamepad-remapd -f` for the service) says what was chosen: layout, profile, output protocol and whether hiding was applied. All commands are in the [command-line reference](cli.md).

## Linux: permission errors

**Symptom:** `PermissionError` on `/dev/uinput` or `/dev/hidrawN`; the device is listed but cannot be opened.

1. Run `sudo packaging/linux/install.sh`. It installs the udev rules (`uaccess` for the logged-in desktop user, group `input` for others), loads the `uinput` module, adds you to the `input` group and sets real-time limits. Log out and in afterwards.
2. For a device that is not in the shipped rules, generate rules for it: `gamepad-remapd udev-rules` (options in the [command-line reference](cli.md)), or let `setup-hide --auto` do it together with hiding.
3. Check access with `getfacl /dev/uinput /dev/hidraw*` and `lsmod | grep uinput`.
4. Replug the gamepad (or `sudo udevadm trigger`) after changing rules.

If `serve` restarts itself through `sudo` and mentions a group: that is the old group-based hiding (rule 71). Remove it with `gamepad-remapd setup-hide --uninstall` and use `setup-hide --auto --service` instead.

## The device is not listed, or is listed but silent

* A 2.4 GHz receiver exposes several interfaces; the gamepad data is often on a vendor interface that does not appear until a handshake is sent. Look at `-v list` and try `gamepad-remapd handshake-probe /dev/hidrawN` (then `--broad`). See [calibration](calibration.md).
* Many pads have a mode switch (XInput / DInput / Switch / PS). In the wrong mode the pad may expose fewer buttons or no gyro, or a different protocol. Check which mode the device is in and which layout was chosen (log, Protocol tab). A model can have several modes (layouts of one `mode.group`); choose one in the UI.
* No layout and the descriptor is useless: run the wizard `gamepad-remapd calibrate /dev/hidrawN --save`.
* A layout was picked, but wrongly: bind a layout explicitly in `daemon.json`:

  ```json
  "overrides": [ { "vid": "04b4", "pid": "2412", "interface": 2, "layout": "flydigi_v1_2g4" } ]
  ```

* Log lines with `Errno 110` or `Errno 71` during the handshake: some receivers never answer the outgoing command, yet the device still streams reports and works. The daemon skips the handshake when the device already streams, and a handshake timeout is not fatal.
* An extra button (M1, ML, a paddle) is not visible: use `sniff` to see whether the bit appears at all; some pads only send these buttons in one mode.

## XInput pad stops responding (xpad)

The kernel `xpad` driver sometimes hangs after a replug or receiver glitch; only a reboot or `modprobe -r xpad` helps. Bypass it: set `"xinput_backend": "usb"` in `daemon.json`, install the udev rules (`sudo packaging/linux/install.sh`) and replug the pad. The daemon then talks to the pad over USB itself and gives `xpad` the device back on close (`gamepad-remapd unhide` also does this after a crash). Only the wired Xbox 360 protocol is supported (not the wireless receiver, not Xbox One).

## A game sees two gamepads, or inputs are doubled

The original device must be hidden from games. SDL finds gamepads by opening `/dev/input/event*`, `/dev/input/js*` and `/dev/hidraw*`, so the daemon makes those nodes of the original unopenable, which needs `CAP_FOWNER`.

1. Look at the **Protocol** tab (hiding status) or at the log. If it says hiding is not applied, the daemon lacks the capability.
2. Install it permanently: `gamepad-remapd setup-hide --auto --service` (asks for `sudo`), or run once with `gamepad-remapd serve --privileged`.
3. Check with `gamepad-remapd sdl-list`: only the virtual gamepad should be listed.
4. If a crash left nodes closed, run `gamepad-remapd unhide` (or `sudo udevadm trigger --action=change`). The daemon also restores them on its next start.
5. Per model you can turn hiding off or on with the "hide original" option in the Protocol tab.

On Windows use HidHide (`gamepad-remapd hidhide`, see `packaging/windows/README.md`).

## The game does not recognise the virtual gamepad, or buttons are mixed up

* Use the **XInput emulation (Xbox 360)** output protocol. The "native" DualShock 4 and DInput modes are experimental: SDL has no mapping for the virtual DInput device, and for a DualShock VID:PID SDL hands the device to its own HIDAPI driver. The UI warns about this.
* D-pad does not work in a particular game on Linux: set `"dpad_mode": "buttons"` in `daemon.json` (D-pad as four buttons instead of a hat).
* Extra buttons (M1…M6, ML, MR) are not standard XInput buttons: on Linux they appear as extra buttons; on Windows XInput has none, so such targets are skipped with a warning. Map them to existing buttons or keys in the [profile](profiles.md).

## Rumble does not work

* Test the device directly: `gamepad-remapd rumble-test` and the reference `gamepad-remapd sdl-rumble` (see [cli](cli.md)). If SDL cannot rumble it either, the problem is the device or its mode, not this program.
* A 2.4 GHz receiver can lose rumble (and more) when plugged into a USB 2.0 hub port: the OUT endpoint never completes (NAK). Move it to a port on the motherboard, preferably USB 3.0, and check again.
* Flydigi receivers in DInput mode do not accept rumble from the host (the OUT endpoint of the vendor interface is not served). In XInput mode rumble goes through the kernel `xpad` driver.
* DualShock 4 over Bluetooth needs a CRC-32 in the report; the built-in layout has it. DS4 over USB rumble follows the kernel driver but is not verified on every clone.
* A custom layout needs a `rumble` template; see [layouts](layouts.md).

## Gyro drifts or jitters

Calibrate with the pad lying still (Gyro tab, or see [calibration](calibration.md)), then raise `gyro.deadzone_dps` and `gyro.smoothing` in the profile, and set `gyro.activation` so the gyro works only while aiming. If the calibration is refused ("the gamepad was moving"), put the pad on a firm surface and repeat.

## Keyboard-as-gamepad took over the keyboard

The feature is off by default; with `keyboard.enabled: true` and `keyboard.grab: true` the selected keyboard is taken away from the system. Press the toggle chord (default Ctrl+Alt+G, `keyboard.toggle`) to release it. If you cannot type at all, stop the daemon. Use the exact device in `keyboard.device` (see `gamepad-remapd keyboards`) so that it does not match your main keyboard. Linux only.

## Web UI or control channel

* **403 Forbidden** in the browser: the `Host` header is not allowed. Open the UI as `http://127.0.0.1:8765/` or `http://localhost:8765/`. To reach it from another machine set `web_host` to that machine's own address (not `0.0.0.0`) and use that address in the URL. A request with a foreign `Origin` is refused as well.
* **503 / "daemon unavailable"**: the Web process cannot reach the daemon. Start `serve` (or the service); check that both run as the same user and use the same runtime directory. Under plain `sudo` `XDG_RUNTIME_DIR` is lost and the socket ends up in `/tmp/gamepad-remapd-0/`; use `sudo env XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR …` or set `control_endpoint` in `daemon.json`.
* Port in use: change `web_port` (or `--port`).
* The UI says the Web UI is disabled because of `aiohttp`: `pip install "gamepad-remapd[web]"`.

## Real-time priority warnings

The log may say `SCHED_FIFO is unavailable`. This is not fatal: the thread falls back to `nice -5` or normal priority. To get real-time scheduling, install the limits (`packaging/linux/limits.d/99-gamepad-remapd.conf`: `@input - rtprio 95`) and log in again, or give the service `AmbientCapabilities=CAP_SYS_NICE`. Measure the effect with `gamepad-remapd bench`.

## Windows

* Install the ViGEmBus driver and reboot; without it no virtual gamepad can be created.
* If `pip install hidapi` fails to build, do not install it: the built-in `winhid` backend is used automatically (set `"backend": "winhid"` to force it). More in `packaging/windows/README.md`.
* The Windows code has not been verified on real hardware yet; send `python -m gamepad_remapd -v list` output with bug reports.

## Reporting a problem

Include: the output of `gamepad-remapd -v list`, the layout and profile names from the log, the output protocol, whether hiding was applied, and for input problems a short `sniff` capture. For a new device, a captured layout (`calibrated_*.json` or `auto_*.json`) helps most.
