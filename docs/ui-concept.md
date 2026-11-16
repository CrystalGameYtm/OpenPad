# Web UI concept: visual button remapping (future work)

Status: **idea, not implemented.** Source: an AI-generated mockup (a dark UI with a gamepad diagram, a mapping panel and
a trigger curve). The mockup is a reference for the layout only; its details (an Xbox Elite body mixed with PlayStation
names, macros) are not requirements.

## What we want to take

| Idea | Why | What it needs |
|------|-----|---------------|
| Clickable gamepad diagram (SVG) with callouts on the "Buttons" tab | Pick a button on the picture instead of a list | Button names and positions per layout; a default Xbox-like diagram; the list stays as a fallback for buttons that are not on the diagram |
| Detail panel for the selected button next to the list | "Overview on the left, editing on the right" | Reuse the existing target editor (button / key / mouse / trigger / stages) |
| Curve preview next to sliders (dead zone, trigger sensitivity) | See the effect of the change | Sample `logic/curves.py` and draw it on a canvas/SVG |
| "Unsaved changes" indicator + Save button | Clearer than the current profile bar | Dirty-state tracking of the edited profile in `app.js` |
| "Auto-detected" badge next to the device name | Shows that the layout was found automatically | Layout origin from the daemon (`_auto` / library / user) |

## What we do not take (for now)

* Macros and rapid fire: not supported by the daemon; a separate feature, not a UI change.
* A left sidebar: the current tabs (Device / Buttons / Sticks / Triggers / Gyro / Profiles / Diagnostics / System)
  cover more and keep the page usable on phones.

## Constraints

* Plain static files, no build step (as now): inline SVG + the existing `app.js`.
* All texts go through the client i18n (`web/static/locales/*.json`), no hard-coded strings.
* Works at phone width; on a narrow screen the diagram collapses above the list.
* Button labels come from the layout of the device, so DS4 / Switch / custom layouts show their own names.

## Suggested steps

1. SVG diagram + selection of a button (highlights the matching row, and vice versa).
2. Detail panel reusing the current editor.
3. Curve preview for sticks and triggers.
4. Dirty-state indicator and Save button.
5. Per-layout diagrams (DS4 / Switch) as optional files next to the layout JSON.
