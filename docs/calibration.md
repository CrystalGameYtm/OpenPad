# Calibration and bringing up a new gamepad

There are two unrelated things called "calibration" here:

1. **Layout calibration** (the wizard): teaches the program which bit of the raw report is which button and where the axes are. Needed for a gamepad that has no ready layout and no usable HID descriptor, typically the vendor interface of a 2.4 GHz receiver.
2. **Gyro zero calibration**: measures the resting offset of the gyroscope so that the aim does not drift. Needed only if you use gyro aiming.

Stick dead zones and curves are not calibrated; they belong to the [profile](profiles.md).

## Bringing up an unknown device: the order of attempts

1. `gamepad-remapd list`: is the device (and the right interface) visible?
2. If a layout exists it is used automatically. If not and `auto_layout` is on, the daemon builds one from the HID descriptor (or from evdev capabilities). `gamepad-remapd autolayout PATH --save` stores it in `<config>/devices/` so you can edit it. Roles of buttons and axes in such a layout are heuristics.
3. If the interface is silent, try `gamepad-remapd handshake-probe PATH` (add `--broad` to try other vendors' packets from the library). A working sequence can be saved with `--save` as `learned_<vid>_<pid>_ifN.json`.
4. If the device streams reports but there is no layout, run the wizard (below).
5. Check the result with `gamepad-remapd testpage` (buttons, axes, raw bytes with changed bytes highlighted), or `sniff` to find hidden buttons.

All the commands and their options are in the [command-line reference](cli.md). Layout files are described in [layouts](layouts.md).

## Layout calibration wizard

It only observes reports; nothing is written to the device (the handshake, if one is known, is sent before). It runs in two places:

* the command line: `gamepad-remapd calibrate /dev/hidrawN [--paddles] [--broad] [--save]`;
* the **test page** (`gamepad-remapd testpage`): a browser version of the same wizard with a "save" step. It needs neither the daemon nor `aiohttp`. evdev devices are not offered there because they already have standard codes.

### What it asks

1. **Noise baseline.** You pick the gamepad up and move it around a little without touching any control. Bytes that change by themselves (gyro, counters) are excluded from the button search.
2. **Buttons**: A, B, X, Y, LB, RB, Back, Start, L3, R3, Home. A button is exactly one bit that goes to 1 and stays there for several reports; if several things change at once, the step is repeated.
3. **D-pad**: Up, Right, Down, Left. It is recognized both as four bits and as a hat nibble.
4. **Extra buttons** M1–M4, C, Z, and ML/MR with `--paddles`. Skip those the gamepad does not have.
5. **Sticks**: for each of LX, LY, RX, RY move slowly all the way to one side, then the other, and back to the center, moving along that axis only. The wizard detects 8- and 16-bit values (little- or big-endian, signed or not) and the direction from the first move.
6. **Triggers**: press all the way and release.

Every step can be skipped. Conflicts and doubtful steps are reported at the end, and the result is checked against the recorded frames ("in the frame for button X, X must be the pressed one").

### Result

`calibrated_<vid>_<pid>[_ifN].json` in `<config>/devices/` (only with `--save` or the save button). It is an ordinary layout with `priority: 10`, so it wins over a built-in layout for the same device. Rumble, gyro and extra buttons you did not go through are inherited from the built-in layout of the same VID/PID when they do not collide with what was learned. A `_learned` section records the method and the warnings.

To go back to the built-in layout, delete the file and reopen the device (or restart the daemon).

## Gyro zero calibration

The sensor reports a small non-zero value at rest (zero-rate bias). Without correction the aim crawls.

How it works: with the gamepad lying still, the daemon collects raw gyro samples for `duration` seconds (default 1.5, limited to 0.5 … 5) and stores their mean as `gyro.bias` (`[yaw, pitch]`, in raw sensor units) **in the active profile**, not in the layout. The result is refused if the data is not clean: fewer than 40 reports, noise above 3 °/s or a drift above 2 °/s between the first and second half mean the gamepad was moving.

Ways to run it (the device must be open, its layout must describe a gyro):

* Web UI: **Gyro** tab → calibrate button. This saves the bias into the profile file.
* REST: `curl -X POST http://127.0.0.1:8765/api/devices/hidraw5/gyro/calibrate -H 'Content-Type: application/json' -d '{"save": true}'` (see [API](api.md)).
* IPC: command `calibrate_gyro` with `device`, `duration`, `save`.

With `save: false` the bias is applied live but not written to disk. The response contains `bias`, `samples` and `noise_dps`.

### Tuning

* Drift remains after calibration: calibrate again on a firm surface; raise `gyro.deadzone_dps` (for example from 1 to 2) in the profile.
* Aim is jittery: raise `gyro.smoothing` (0.2 … 0.4).
* Rotation looks twice too fast or slow: the layout's `gyro.dps_per_lsb` is wrong for that sensor; correct it in the layout (or scale with `full_dps` and `mouse_sens` in the profile).
* Wrong direction on an axis: `invert_yaw` / `invert_pitch`.
* Gyro only while aiming: set `gyro.activation` to a button name (for example `L2_BTN`).

## Verifying

* Open the test page or the Web UI with the gamepad untouched: sticks at 0, triggers at 0, no buttons pressed, gyro readout near 0.
* Move each stick around its rim and press each trigger fully; the live view should reach the extremes without clipping early.
* A stick that rests off-center or does not reach full range: fix `center`, `raw_min` or `raw_max` of that field in the layout. A stick that is merely too sensitive or has play belongs to the profile (`inner_deadzone`, `outer_deadzone`, `curve`).
