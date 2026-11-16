# Command-line reference

```sh
gamepad-remapd [--config FILE] [--config-dir DIR] [-v] [--version] <command> [options]
python -m gamepad_remapd <command> ...          # same thing, from a source checkout: PYTHONPATH=src
```

Without a command the program prints the help and exits. Command output and `--help` are in English unless
`language` in `daemon.json` is set to another language (see [Configuration](configuration.md)).

## Global options

| Option | Default | Meaning |
|--------|---------|---------|
| `--config FILE` | `<config dir>/daemon.json` | Path to `daemon.json`. |
| `--config-dir DIR` | `$GAMEPAD_REMAPD_HOME`, else `$XDG_CONFIG_HOME/gamepad-remapd` (`~/.config/gamepad-remapd`; `%APPDATA%\gamepad-remapd` on Windows) | Directory with `profiles/`, `devices/`, `keymaps/`, `handshakes/`, `state.json`. |
| `-v`, `--verbose` | off | Verbose (debug) logging. |
| `--version` | | Print the version. |

## Commands at a glance

| Group | Commands |
|-------|----------|
| Run | `serve`, `daemon`, `web` |
| Inspect | `list`, `monitor`, `keyboards`, `testpage`, `sdl-list` |
| Bring up a new device | `autolayout`, `calibrate`, `handshake-probe`, `sniff`, `rumble-test`, `sdl-rumble` |
| Hide the original gamepad | `setup-hide`, `udev-rules`, `unhide`, `hidhide` (Windows) |
| Check and measure | `check`, `bench` |

Device paths are `/dev/hidrawN` (Linux). Find them with `gamepad-remapd list`.

## Run

### `serve`

Daemon plus Web UI (the Web UI runs in a separate process).

| Option | Default | Meaning |
|--------|---------|---------|
| `--port PORT` | `web_port` from `daemon.json` (8765) | Web UI port. |
| `--no-web` | off | Do not start the Web UI even if `web_enabled` is true. |
| `--privileged` | off | Linux: restart with `CAP_FOWNER` through `sudo` + `setpriv`, so the daemon can close the original `hidraw` nodes to other programs (see `setup-hide` for a permanent setup). Needs `sudo` and `setpriv` (util-linux). |

On Linux `serve` first checks two things (skipped when `GAMEPAD_REMAPD_NO_REEXEC` is set):

* If the daemon already runs as the `gamepad-remapd` systemd service, it does not start a second one: it prints how to
  reach the Web UI and the log, and exits.
* If the original `hidraw` nodes are closed by the legacy group rule (`71-…hide-hidraw`, see `udev-rules
  --hidraw-group`), it restarts itself through `sudo` with that group.

```sh
gamepad-remapd serve
gamepad-remapd serve --privileged --port 9000
```

### `daemon`

Daemon only (no Web UI). It listens on the control endpoint (`control_endpoint`, by default a UNIX socket in
`$XDG_RUNTIME_DIR`). No options. Stop it with Ctrl-C.

### `web`

Web UI only; connects to an already running daemon.

| Option | Default | Meaning |
|--------|---------|---------|
| `--port PORT` | `web_port` | Web UI port. |

## Inspect

### `list`
Shows the HID devices and the layout matched to each. No options.

### `monitor PATH`
Live monitor: which buttons and axes the layout recognizes.

| Option | Default | Meaning |
|--------|---------|---------|
| `PATH` | required | `/dev/hidrawN`. |
| `--layout ID` | matched by VID/PID, then built automatically | Layout id. |
| `--seconds N` | 0 | Duration; 0 = until Ctrl-C. |
| `--raw` | off | Print every raw report as hex (handshake diagnostics). |

### `keyboards`
Lists keyboards usable as a gamepad (the values for `keyboard.device` in `daemon.json`). No options.

### `testpage [PATH]`
A web page to check buttons and axes in the browser, without the daemon.

| Option | Default | Meaning |
|--------|---------|---------|
| `PATH` | optional | `/dev/hidrawN`; can be chosen on the page. |
| `--layout ID` | by VID/PID, then auto | Layout id. |
| `--port PORT` | 8766 | Port. |
| `--no-browser` | off | Do not open the browser. |

### `sdl-list`
Shows what SDL3 sees (a check that hiding works). Needs the SDL3 library. No options.

## Bring up a new device

See [Calibration](calibration.md) for the workflow.

### `autolayout [PATH]`
Builds a layout from the device HID report descriptor.

| Option | Default | Meaning |
|--------|---------|---------|
| `PATH` | | `/dev/hidrawN` (or use `--descriptor-file`). |
| `--descriptor-file FILE` | | File with the descriptor (binary or hex text). |
| `--vid HEX`, `--pid HEX` | | VID/PID when reading from a file. |
| `--fields` | off | Only print the table of descriptor fields. |
| `--save` | off | Save into `devices/` as a regular layout. |

### `calibrate PATH`
Calibration wizard: learns the buttons and axes of any hidraw gamepad.

| Option | Default | Meaning |
|--------|---------|---------|
| `--broad` | off | If the device is silent, also try handshakes of other vendors (SDL library). |
| `--paddles` | off | Add the ML/MR steps (side/back paddles). |
| `--save` | off | Save as `devices/calibrated_VID_PID[_ifN].json`. |

### `handshake-probe PATH`
Automatic handshake search for a silent interface (a receiver that sends nothing until it is "woken").

| Option | Default | Meaning |
|--------|---------|---------|
| `--broad` | off | Also try other vendors' handshakes. |
| `--no-user` | off | Autonomous phase only (do not ask to press buttons). |
| `--save` | off | Save the result as `devices/learned_VID_PID_ifN.json`. |
| `--auto-seconds S` | 1.2 | Time to wait for each candidate in the autonomous phase. |
| `--user-seconds S` | 2.5 | Time per candidate in the phase where you press buttons. |

### `sniff [PATH]`
Hex inspector of reports, for finding hidden buttons.

| Option | Default | Meaning |
|--------|---------|---------|
| `--layout ID` | | Layout id (for the handshake and report id). |
| `--learn NAMES` | | Comma-separated button names to learn, e.g. `M1,M2,M3,M4`. |
| `--seconds N` | 0 | Duration; 0 = until Ctrl-C. |
| `--numbered` | off | The first byte of the report is the Report ID. |

### `rumble-test PATH`
Manual rumble check: the layout template, your own, or a command sweep.

| Option | Default | Meaning |
|--------|---------|---------|
| `--template TXT` | | e.g. `"05 0f {strong} {weak}"`. |
| `--pad-to N` | | Pad the report with zeros to N bytes. |
| `--sweep ID:FROM-TO` | | Sweep the second byte, e.g. `0x05:0x00-0x20`. |
| `--crc32-at N` | | DS4 over Bluetooth: put the CRC-32 (seed A2) into bytes N..N+3, e.g. `74`. |
| `--hold S` | 1.0 | Seconds per motor. |

### `sdl-rumble`
Rumble through SDL3 (reference). `--trace` runs it under `strace` and prints the hex of what SDL writes to `hidraw`.

## Hide the original gamepad

### `setup-hide`
Linux: automatic setup of hiding the original (group + udev rules + optional service), through `sudo`.

| Option | Meaning |
|--------|---------|
| `--hide VID:PID` | What to hide (hex; may be repeated). |
| `--auto` | Hide all connected gamepads that have a layout. |
| `--service` | Install a systemd service: the daemon starts as your user with `CAP_FOWNER`. |
| `--dry-run` | Only show what would be done. |
| `--uninstall` | Remove the rules and the service. |

```sh
gamepad-remapd setup-hide --auto --service
gamepad-remapd setup-hide --uninstall
```

### `udev-rules`
Prints udev rules for the known layouts (redirect into `/etc/udev/rules.d/`).

| Option | Meaning |
|--------|---------|
| `--vid-pid VID[:PID]` | Additional device (hex; may be repeated). |
| `--hide VID:PID` | Print rules that hide the original from programs (SDL/Chrome/Steam see only the virtual one; may be repeated). |
| `--hidraw-group GROUP` | With `--hide`: close `/dev/hidrawN` to everyone except GROUP (the daemon is started with this group, no root). The file is `71-…`, not `69-…`. |

```sh
gamepad-remapd udev-rules | sudo tee /etc/udev/rules.d/70-gamepad-remapd.rules
```

### `unhide`
Linux: restores permissions on the original nodes after a daemon crash. No options.

### `hidhide --vid-pid VID:PID`
Windows: HidHide commands that hide the original gamepad from games.

| Option | Meaning |
|--------|---------|
| `--vid-pid VID:PID` | Required, hex, e.g. `054c:09cc`. |
| `--run` | Run the commands (administrator) instead of only printing them. |

## Check and measure

### `check PROFILE`
Validates a JSON profile file; with `--layout ID` it is also compiled against that layout. See [Profiles](profiles.md).

### `bench`
Measures the software latency of the Pipeline.

| Option | Default | Meaning |
|--------|---------|---------|
| `--reports N` | 200000 | Number of reports. |
| `--profile NAME` | `precision_aim` | Profile. |
| `--layout ID` | `sony_ds4_usb` | Layout. |

## Environment variables

| Variable | Effect |
|----------|--------|
| `GAMEPAD_REMAPD_HOME` | Configuration directory. |
| `GAMEPAD_REMAPD_NO_REEXEC` | Disable the automatic re-execution in `serve`. |
| `GAMEPAD_REMAPD_GROUP` | Name of the group used for the `hidraw` hiding rules (default `gamepad-remapd`). |
| `SUDO_USER` | The real user when running through `sudo` (`serve`, `setup-hide`). |
