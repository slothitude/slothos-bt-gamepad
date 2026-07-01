"""evdev event code -> HID report field mapping for the ANBERNIC RG35XX H.

Canonical mapping table. Phase 4 verifies this against a real host (Rog) with
jstest and corrects any wrong entries. The same table is the source of truth
applied back to ui/project.godot in Phase 6.

Wire layout reminder (see hid_descriptor.py):
    byte 1: buttons 1-8   (bit 0 = btn1, ..., bit 7 = btn8)
    byte 2: buttons 9-16  (bit 0 = btn9, ...)
    byte 3: buttons 17-24 (bit 0 = btn17, ...)
    byte 4: hat          (0=N,1=NE,2=E,3=SE,4=S,5=SW,6=W,7=NW,8=release)
    byte 5: X   (left stick X)
    byte 6: Y   (left stick Y)
    byte 7: Z   (unused on RG35XX H — no analog triggers; slot stays 0)
    byte 8: Rz  (unused on RG35XX H — slot stays 0)
    byte 9: Rx  (right stick X)
    byte 10: Ry  (right stick Y)

evdev codes referenced here are the standard Linux gamepad codes
(include/uapi/linux/input-event-codes.h).
"""

import evdev.ecodes as e

# ---------------------------------------------------------------- buttons
# Maps Linux evdev BTN_* / KEY_* code -> HID button number (1..24).
# HID button number 1 = bit 0 of byte 1, etc.
# 17 physical buttons on the Anbernic RG35XX H (per /dev/input/event1
# EV_KEY cap). Buttons 18-24 are unused reserve (HID field must be
# byte-aligned; rounded up to 24).
BUTTON_MAP = {
    # Standard gamepad face + shoulder + triggers
    e.BTN_SOUTH:   1,   # A          (evdev 304 / 0x130)
    e.BTN_EAST:    2,   # B          (evdev 305 / 0x131)
    e.BTN_NORTH:   3,   # Y          (evdev 307 / 0x133)
    e.BTN_WEST:    4,   # X          (evdev 308 / 0x134)
    e.BTN_TL:      5,   # L1         (evdev 310 / 0x136)
    e.BTN_TR:      6,   # R1         (evdev 311 / 0x137)
    e.BTN_TL2:     7,   # L2 button  (evdev 312 / 0x138)
    e.BTN_TR2:     8,   # R2 button  (evdev 313 / 0x139)
    e.BTN_SELECT:  9,   # Select     (evdev 314 / 0x13a)
    e.BTN_START:  10,   # Start      (evdev 315 / 0x13b)
    e.BTN_MODE:   11,   # Home       (evdev 316 / 0x13c)
    # Extra face buttons (Anbernic-specific)
    e.BTN_C:      12,   # C          (evdev 306 / 0x132)
    e.BTN_Z:      13,   # Z          (evdev 309 / 0x135)
    # Function / system keys
    e.KEY_GOTO:        14,  # Function/Menu  (evdev 354 / 0x162)
    e.KEY_ESC:         15,  # Power          (evdev   1 / 0x001)
    e.KEY_VOLUMEDOWN:  16,  # Vol Down       (evdev 114 / 0x072)
    e.KEY_VOLUMEUP:    17,  # Vol Up         (evdev 115 / 0x073)
    # 18-24 unused reserve (byte alignment)
}

# ---------------------------------------------------------------- axes
# Maps Linux evdev ABS_* code -> offset within the 6-byte axis block.
# Offsets are relative to the axis block start (NOT absolute report byte).
# main.py writes to body[4 + offset] — see GameState.set_axis.
#
# Anbernic RG35XX H non-standard axis mapping (verified via evdev capture
# 2026-06-18: left-stick wiggle emitted code=2/ABS_Z + code=5/ABS_RZ):
#   LEFT  stick X = ABS_Z  (code 2)  -> HID offset 0 (X)
#   LEFT  stick Y = ABS_RZ (code 5)  -> HID offset 1 (Y)
#   RIGHT stick X = ABS_RX (code 3)  -> HID offset 4 (Rx)
#   RIGHT stick Y = ABS_RY (code 4)  -> HID offset 5 (Ry)
# ABS_X/ABS_Y (codes 0/1) are NOT emitted by this device.
# HID offsets 2 (Z) and 3 (Rz) stay at 0 — RG35XX H has no analog
# triggers; L2/R2 are buttons only (BTN_TL2/BTN_TR2).
AXIS_MAP = {
    e.ABS_Z:    0,   # LEFT  stick X -> HID X
    e.ABS_RZ:   1,   # LEFT  stick Y -> HID Y
    e.ABS_RX:   4,   # RIGHT stick X -> HID Rx
    e.ABS_RY:   5,   # RIGHT stick Y -> HID Ry
}

# Aliases for kernels that expose sticks under non-standard codes. Empty
# on the RG35XX H — kept as a dict so main.py's `AXIS_ALIASES.get(...)`
# lookup stays a no-op rather than needing a hasattr guard.
AXIS_ALIASES = {}

# ---------------------------------------------------------------- D-pad
# RG35XX H D-pad may come in as a hat (ABS_HAT0X/ABS_HAT0Y) or as 4 discrete
# BTN_TRIGGER_HAPPY buttons. Both paths are handled below.
DPAD_AXIS_X = e.ABS_HAT0X
DPAD_AXIS_Y = e.ABS_HAT0Y

# ABS_HAT0X: -1=W, 0=release, 1=E
# ABS_HAT0Y: -1=N, 0=release, 1=S
# Combined to hat value (0..7, 8=release).
def hat_from_axes(x: int, y: int) -> int:
    if x == 0 and y == 0:
        return 8  # null / released
    # Build compass value: 0=N, 1=NE, 2=E, 3=SE, 4=S, 5=SW, 6=W, 7=NW
    table = {
        (0, -1): 0,
        (1, -1): 1,
        (1,  0): 2,
        (1,  1): 3,
        (0,  1): 4,
        (-1, 1): 5,
        (-1, 0): 6,
        (-1,-1): 7,
    }
    return table.get((x, y), 8)

# Discrete D-pad buttons (fallback if device uses BTN_DPAD_*).
DPAD_BUTTONS = {
    e.BTN_DPAD_UP:    (0, -1),
    e.BTN_DPAD_DOWN:  (0,  1),
    e.BTN_DPAD_LEFT:  (-1, 0),
    e.BTN_DPAD_RIGHT: (1,  0),
}

# ---------------------------------------------------------------- normalization
# evdev axis range -> HID 8-bit signed range.
# Sticks on the RG35XX H are -4096..4096 -> -127..127 around midpoint 0.
# Empty set: this device has no analog trigger axes (L2/R2 are buttons
# only). Kept as a set so `code in TRIGGER_AXES` is a clean no-op.
TRIGGER_AXES = set()

def normalize_axis(evdev_code: int, value: int, dev_info: dict) -> int:
    """Normalize an evdev axis value to a single HID byte.

    dev_info: {abs_code: InputDevice.absinfo(abs_code)} cached by the reader.
    """
    code = AXIS_ALIASES.get(evdev_code, evdev_code)
    if code in TRIGGER_AXES:
        # Trigger: 0 (released) -> 0, max -> 127
        info = dev_info.get(evdev_code)
        if info is None:
            return 0 if value == 0 else 127
        lo, hi = info.min, info.max
        span = max(1, hi - lo)
        v = (value - lo) * 127 // span
        return max(0, min(127, v))
    # Stick: linear map to -127..127 around the midpoint
    info = dev_info.get(evdev_code)
    if info is None:
        return 0
    lo, hi = info.min, info.max
    mid = (lo + hi) // 2
    span = max(1, (hi - lo) // 2)
    v = (value - mid) * 127 // span
    return max(-127, min(127, v))


def button_bit_index(evdev_code: int) -> int:
    """Return HID button number (1..24) for an evdev BTN_ code, or 0 if unmapped."""
    return BUTTON_MAP.get(evdev_code, 0)
