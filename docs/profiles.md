# Profiles

A **profile** says what the daemon does with the inputs of a gamepad: dead zones and curves of sticks and triggers, button remapping (including keyboard and mouse targets), trigger stages and gyro aiming. A profile does not know how to read the device; that is the job of a [layout](layouts.md).

Profiles are JSON files. Built-in ones live in `src/gamepad_remapd/profiles/`, user ones in `<config>/profiles/` (a user file with the same name overrides the built-in one). The name of the file must match `name` (`^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`). Built-in profiles: `default`, `precision_aim`, `ds4_gyro_aim`.

If a profile cannot be applied when a device opens (for example it fails validation against the layout), the daemon logs a warning and falls back to a neutral profile named `fallback`.

Which profile is active for which model is remembered in `<config>/state.json` (key `active`, per `vid:pid`). A model without a saved choice uses `default_profile` from `daemon.json`.

Validation is strict: an unknown key, a value out of range or a wrong type is an error with the path of the faulty field (`sticks.left.inner_deadzone: 1.2 is out of range 0.0..0.95`). Keys starting with `_` (for example `_comment`) are ignored everywhere. All sections are optional; missing values take the defaults below.

## Example

```json
{
  "name": "my_fps",
  "description": "Smaller dead zones, expo curve on the right stick, gyro on L2",
  "device": null,
  "button_map": {
    "M1": "A",
    "M2": ["LB", "RB"],
    "SHARE": "KEY_F12",
    "TOUCHPAD": "NONE",
    "DPAD_LEFT": "KEY_Q"
  },
  "sticks": {
    "left":  { "inner_deadzone": 0.08, "outer_deadzone": 0.98, "shape": "radial" },
    "right": { "inner_deadzone": 0.05, "curve": { "type": "expo", "k": 3.0 }, "sensitivity": 1.2 }
  },
  "triggers": {
    "left":  { "mode": "analog", "inner_deadzone": 0.03 },
    "right": { "mode": "digital", "threshold": 0.4,
               "stages": { "soft": { "at": 0.3, "targets": ["KEY_E"] },
                           "full": { "at": 0.9, "targets": ["MOUSE_LEFT"] } } }
  },
  "gyro": { "enabled": true, "target": "right_stick", "activation": "L2_BTN",
            "deadzone_dps": 2.0, "full_dps": 220, "smoothing": 0.25 }
}
```

## Top-level keys

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `name` | string | `unnamed` | Profile name (see the pattern above). |
| `description` | string | `""` | Free text shown in the UI. |
| `device` | string or `null` | `null` | Layout id this profile is restricted to (for example `sony_ds4_usb`). If set and a different layout is open, the profile is **not applied** (error). `null` = any layout. |
| `button_map` | object | `{}` | Button remapping, see below. |
| `sticks` | object | defaults | `left` and `right`, see below. |
| `triggers` | object | defaults | `left` and `right`, see below. |
| `gyro` | object | disabled | Gyro aiming, see below. |
| `swap_sticks` | bool | `false` | Swap the left and the right stick. |
| `swap_triggers` | bool | `false` | Swap LT and RT. |

## `button_map`

`{ "<source input>": <target(s)> }`

**Source inputs** are the button names of the active layout (`CROSS`, `L1`, `M1`, …; the names are whatever the layout defines; the list is shown in the UI for the open device) and the four hat directions `DPAD_UP`, `DPAD_DOWN`, `DPAD_LEFT`, `DPAD_RIGHT`. Inputs that are not listed keep the `target` that the layout gives them. A source name that the open layout does not have is an **error** (the profile is rejected and the message lists the available names), so a profile that uses layout-specific names such as `M1` or `TOUCHPAD` works only with layouts that have them; set `device` to say so.

**Targets** (a string or a list of strings):

| Target | Meaning |
|--------|---------|
| `A B X Y LB RB BACK START GUIDE LS RS` | Standard buttons of the virtual gamepad. |
| `UP DOWN LEFT RIGHT` | D-pad of the virtual gamepad. |
| `M1`…`M6`, `C`, `Z`, `ML`, `MR` | Extra buttons (appear as `BTN_TRIGGER_HAPPY*` on Linux). |
| `LT`, `RT` | Press the analog trigger fully ("digital trigger"). |
| `KEY_<name>` | A keyboard key, for example `KEY_F`, `KEY_SPACE`, `KEY_LEFTSHIFT`. Case does not matter. |
| `MOUSE_LEFT`, `MOUSE_RIGHT`, `MOUSE_MIDDLE`, `MOUSE_SIDE`, `MOUSE_EXTRA` | Mouse buttons. |
| `"NONE"` or `[]` | Disable the button. |

A list presses all targets while the source is held (a chord). Pressed targets are reference-counted, so two sources mapped to the same target do not release each other. Key and mouse targets create virtual keyboard/mouse devices on demand (see [architecture](architecture.md)).

## `sticks.left` / `sticks.right`

| Key | Type / range | Default | Meaning |
|-----|--------------|---------|---------|
| `inner_deadzone` | 0.0 … 0.95 | 0.05 | Below this deflection the output is 0. |
| `outer_deadzone` | 0.05 … 1.0 | 1.0 | Deflection that gives the full output. Must be greater than `inner_deadzone` by more than 0.01. |
| `shape` | `radial` / `axial` | `radial` | `radial`: dead zone on the vector length (a circle); `axial`: each axis separately. |
| `curve` | curve object | `linear` | Response curve, see below. |
| `sensitivity` | 0.1 … 4.0 | 1.0 | Output multiplier (clamped to full deflection). |
| `invert_x`, `invert_y` | bool | `false` | Invert an axis. |
| `swap_xy` | bool | `false` | Swap X and Y. |

## `triggers.left` / `triggers.right`

| Key | Type / range | Default | Meaning |
|-----|--------------|---------|---------|
| `inner_deadzone` | 0.0 … 0.95 | 0.0 | Pull below this is ignored. |
| `outer_deadzone` | 0.05 … 1.0 | 1.0 | Pull that gives the full output. |
| `curve` | curve object | `linear` | Response curve. |
| `sensitivity` | 0.1 … 4.0 | 1.0 | Output multiplier. |
| `invert` | bool | `false` | Invert the travel. |
| `mode` | `analog` / `digital` / `off` | `analog` | `digital`: output is 0 or full, switching at `threshold`; `off`: the trigger output is always 0 (use it together with `stages` or `button_map`). |
| `threshold` | 0.01 … 1.0 | 0.5 | Switching point for `digital` mode. |
| `button_value` | 0.0 … 1.0 | 1.0 | Trigger value reported when the trigger is "pressed" by a button target. |
| `stages` | object | none | Actions at points of the trigger travel, see below. |

### Trigger stages

`stages` has up to two entries, `soft` and `full`:

```json
"stages": { "soft": { "at": 0.3, "targets": ["KEY_E"] },
            "full": { "at": 0.9, "targets": ["MOUSE_LEFT"] } }
```

`at` (0.01 … 1.0, defaults 0.3 and 0.9) is the processed pull level (after the dead zone and curve) at which the targets are pressed. A stage without targets is dropped. `full.at` must be greater than `soft.at` by more than 0.02. Stages use hysteresis, so a trigger resting near a threshold does not chatter.

## `gyro`

Needs a layout that describes a gyroscope (the layout's `gyro` section). `enabled: true` with a layout that has none is an error, and so is an `activation` button that the layout does not have.

| Key | Type / range | Default | Meaning |
|-----|--------------|---------|---------|
| `enabled` | bool | `false` | Turn gyro aiming on. |
| `target` | `right_stick` / `left_stick` / `mouse` | `right_stick` | Where the motion goes. `mouse` produces relative mouse motion. |
| `activation` | string or `null` | `null` | Name of an input button of the layout (for example `L2_BTN`) that must be held; `null` = always active. |
| `deadzone_dps` | 0 … 200 | 1.0 | Angular speed (°/s) below which the motion is ignored. |
| `full_dps` | 5 … 4000 | 180 | Angular speed that gives full stick deflection. |
| `curve` | curve object | `linear` | Curve applied to the normalized speed. |
| `invert_yaw`, `invert_pitch` | bool | `false` | Fix the direction of an axis (depends on the sensor). |
| `smoothing` | 0.0 … 0.95 | 0.0 | Exponential smoothing; 0 = none. |
| `bias` | `[yaw, pitch]` | `[0, 0]` | Zero offset in raw sensor units; filled by gyro calibration, see [calibration](calibration.md). |
| `mouse_sens` | 0.1 … 200 | 6.0 | Mouse speed for `target: "mouse"`. |

## Curves

A curve maps a normalized value `t` in 0..1 to 0..1. It is precomputed into a lookup table, so it costs nothing on the hot path.

| `type` | Parameters | Notes |
|--------|------------|-------|
| `linear` | none | Identity (default). |
| `power` | `exponent` 0.1 … 10 | `t^exponent`; above 1 = softer near the center. |
| `smooth` | `strength` 0 … 1 (default 1) | S-shaped smoothing. |
| `expo` | `k` −20 … 20, not 0 (default 3) | Exponential curve; negative `k` is the opposite bend. |
| `points` | `points`: at least 2 pairs `[x, y]`, both 0..1, `x` strictly increasing | Piecewise-linear curve. |

## Changing a profile at runtime

* The Web UI edits the active profile live: every change is sent as a **JSON merge patch** (RFC 7386, `null` deletes a key) and the new compiled profile replaces the old one atomically. Nothing is written to disk until you save.
* `PATCH /api/devices/{dev}/profile?save=1` (or the IPC command `patch_profile` with `save: true`) also saves the result as the profile file.
* `POST /api/profiles-check` (IPC `check_profile`) validates a profile against a layout without applying it. Errors (unknown source button, gyro without a sensor, …) come back as an error; `warnings` lists targets the output device cannot produce (for example a key target when the keyboard/mouse output is unavailable) and stages for a trigger the layout does not have.

See [API](api.md) for the exact calls.
