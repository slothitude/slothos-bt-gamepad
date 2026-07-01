"""HID Report Descriptor for a standard gamepad.

Layout: 24 buttons (3 bytes) + hat (4-bit + 4-bit pad) + 6 axes.
Report ID 1 prefix on the wire.

Wire report body (10 bytes total, 11 bytes on wire with 0xA1 header + ID):
    byte 0: buttons 1-8   (bit 0 = btn1 = A, bit 1 = btn2 = B, ...)
    byte 1: buttons 9-16  (bit 0 = btn9, ...)
    byte 2: buttons 17-24 (bit 0 = btn17, ...)
    byte 3: hat (0=N, 1=NE, 2=E, 3=SE, 4=S, 5=SW, 6=W, 7=NW, 8=release)
            (high nibble = 4-bit constant pad for byte alignment)
    byte 4: X   (left stick X, -127..127, centered at 0)
    byte 5: Y   (left stick Y, -127..127)
    byte 6: Z   (L2 trigger, 0..127 — we map half-range to keep sym with sticks)
    byte 7: Rz  (R2 trigger, 0..127)
    byte 8: Rx  (right stick X, -127..127)
    byte 9: Ry  (right stick Y, -127..127)

24-button field is the next byte boundary after the RG35XX H's 17 physical
buttons; HID requires button arrays be byte-aligned before the next Input
item. Buttons 18-24 are unused (reserve).
"""

REPORT_ID = 0x01
REPORT_BYTES = 11  # 1 (id) + 3 (btns) + 1 (hat+pad) + 6 (axes)

HID_DESCRIPTOR = bytes([
    0x05, 0x01,        # Usage Page (Generic Desktop Ctrls)
    0x09, 0x05,        # Usage (Game Pad)
    0xA1, 0x01,        # Collection (Application)
    0x85, REPORT_ID,   #   Report ID (1)
    # ---- 24 buttons (3 bytes) ----
    0x05, 0x09,        #   Usage Page (Button)
    0x19, 0x01,        #   Usage Minimum (0x01)
    0x29, 0x18,        #   Usage Maximum (0x18)  -- 24 buttons
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x01,        #   Logical Maximum (1)
    0x35, 0x00,        #   Physical Minimum (0)
    0x45, 0x01,        #   Physical Maximum (1)
    0x75, 0x01,        #   Report Size (1)
    0x95, 0x18,        #   Report Count (24)
    0x81, 0x02,        #   Input (Data,Var,Abs)
    # ---- D-pad hat ----
    0x05, 0x01,        #   Usage Page (Generic Desktop Ctrls)
    0x09, 0x39,        #   Usage (Hat switch)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x07,        #   Logical Maximum (7)
    0x35, 0x00,        #   Physical Minimum (0)
    0x46, 0x3B, 0x01,  #   Physical Maximum (315)
    0x65, 0x14,        #   Unit (Rotation, Degrees)
    0x55, 0x00,        #   Unit Exponent (0)
    0x75, 0x04,        #   Report Size (4)
    0x95, 0x01,        #   Report Count (1)
    0x81, 0x42,        #   Input (Data,Var,Abs,Null State)
    # ---- 4-bit pad to byte-align the next field ----
    # Without this Windows 11 HidBth rejects the descriptor with
    # "HID Report Descriptor failed validation. A report was not byte
    # aligned." (CM_PROB 10). Buttons (24) + hat (4) = 28 bits = 3.5
    # bytes; the next item would start mid-byte. This constant pad
    # brings the total to 32 bits (4 bytes) before the 8-bit axes.
    0x75, 0x04,        #   Report Size (4)
    0x95, 0x01,        #   Report Count (1)
    0x81, 0x03,        #   Input (Constant,Var,Abs)
    # ---- X / Y / Z / Rz (left stick + L2/R2 triggers) ----
    0x05, 0x01,        #   Usage Page (Generic Desktop Ctrls)
    0x09, 0x30,        #   Usage (X)
    0x09, 0x31,        #   Usage (Y)
    0x09, 0x32,        #   Usage (Z)
    0x09, 0x35,        #   Usage (Rz)
    0x15, 0x81,        #   Logical Minimum (-127)
    0x25, 0x7F,        #   Logical Maximum (127)
    0x75, 0x08,        #   Report Size (8)
    0x95, 0x04,        #   Report Count (4)
    0x81, 0x02,        #   Input (Data,Var,Abs)
    # ---- Rx / Ry (right stick) ----
    0x09, 0x33,        #   Usage (Rx)
    0x09, 0x34,        #   Usage (Ry)
    0x15, 0x81,        #   Logical Minimum (-127)
    0x25, 0x7F,        #   Logical Maximum (127)
    0x75, 0x08,        #   Report Size (8)
    0x95, 0x02,        #   Report Count (2)
    0x81, 0x02,        #   Input (Data,Var,Abs)
    0xC0,              # End Collection
])

# Alias used by callers (bt_gatt.Report Map characteristic, etc.).
DESC_BYTES = HID_DESCRIPTOR
