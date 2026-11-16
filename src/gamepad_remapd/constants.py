"""Linux kernel constants and the canonical (logical) button names.

Numeric codes are taken from `linux/input-event-codes.h`, `linux/uinput.h` and
`linux/input.h`. They are part of the kernel's stable ABI, so it is safe to
duplicate them here: this lets Linux work without the `python-evdev` library at all
(fewer dependencies, less overhead on the hot path).
"""
from __future__ import annotations

# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
EV_SYN = 0x00      
EV_KEY = 0x01      
EV_ABS = 0x03      
EV_FF = 0x15       # Force Feedback (rumble)
EV_UINPUT = 0x0101  

SYN_REPORT = 0     # "frame complete — deliver the events to the consumers"

# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
ABS_X, ABS_Y, ABS_Z = 0x00, 0x01, 0x02
ABS_RX, ABS_RY, ABS_RZ = 0x03, 0x04, 0x05
ABS_HAT0X, ABS_HAT0Y = 0x10, 0x11

# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
BTN_SOUTH = 0x130   # BTN_A
BTN_EAST = 0x131    # BTN_B
BTN_C = 0x132
BTN_NORTH = 0x133   # BTN_X in kernel terminology (yes, historically X == NORTH)
BTN_WEST = 0x134    # BTN_Y in kernel terminology
BTN_Z = 0x135
BTN_TL = 0x136
BTN_TR = 0x137
BTN_TL2 = 0x138
BTN_TR2 = 0x139
BTN_SELECT = 0x13A
BTN_START = 0x13B
BTN_MODE = 0x13C
BTN_TRIGGER = 0x120          # "classic" joystick (DInput): buttons 0x120..0x12A
BTN_THUMBL = 0x13D
BTN_THUMBR = 0x13E
BTN_DPAD_UP, BTN_DPAD_DOWN, BTN_DPAD_LEFT, BTN_DPAD_RIGHT = 0x220, 0x221, 0x222, 0x223
BTN_TRIGGER_HAPPY1 = 0x2C0  # "extra" buttons: M1–M6/C/Z go here

# --------------------------------------------------------------------------
# Force Feedback
# --------------------------------------------------------------------------
FF_RUMBLE = 0x50
FF_GAIN = 0x60
UI_FF_UPLOAD = 1
UI_FF_ERASE = 2

# --------------------------------------------------------------------------

# The backend rescales them into the ranges of the specific virtual device.
# --------------------------------------------------------------------------
STICK_MIN, STICK_MAX = -32768, 32767
TRIGGER_MIN, TRIGGER_MAX = 0, 255

# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
STANDARD_BUTTONS = ("A", "B", "X", "Y", "LB", "RB", "BACK", "START",
                    "GUIDE", "LS", "RS")
DPAD_BUTTONS = ("UP", "DOWN", "LEFT", "RIGHT")

# BTN_TRIGGER_HAPPY1.. — Steam Input / SDL see them as ordinary buttons.
# ML/MR — side/back paddles (Switch style, GameSir, 8BitDo). Appended AT THE END so the codes of existing

EXTRA_BUTTONS = ("M1", "M2", "M3", "M4", "M5", "M6", "C", "Z", "ML", "MR")
# "Digital triggers": a button press → the analog trigger is fully pressed.
ANALOG_BUTTON_TARGETS = ("LT", "RT")

ALL_TARGETS = (STANDARD_BUTTONS + DPAD_BUTTONS + EXTRA_BUTTONS
               + ANALOG_BUTTON_TARGETS)
