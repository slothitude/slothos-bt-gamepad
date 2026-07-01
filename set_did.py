#!/usr/bin/env python3
"""Set Bluetooth DeviceID via raw mgmt socket.

BlueZ auto-generates a PnP SDP record (UUID 0x1200) with bogus values
derived from BD_ADDR OUI when MGMT_OP_SET_DEV_ID is never called. The
default record has VendorIDSource=0x1d6b (an invalid value; must be 1
for Bluetooth SIG or 2 for USB-IF) which Windows 11's HidBth driver
uses for driver matching. Without a known vendor/product, HidBth loads
but fails to start (Problem 0xA / STATUS_UNSUCCESSFUL) and never
attempts to connect to PSM 17.

``btmgmt did`` silently fails on this Realtek adapter for unclear
reasons (probably permissions in the bus name ownership), but a raw
mgmt socket works. Run this once at boot before bt_gamepad starts.

MGMT_OP_SET_DEV_ID payload format (mgmt-api.txt):
    struct mgmt_cp_set_dev_class {
        uint16_t source;     # 1=BT-SIG, 2=USB-IF
        uint16_t vendor;
        uint16_t product;
        uint16_t version;
    } __attribute__ ((packed));
"""

import socket
import struct
import sys

# MGMT socket constants (lib/hci.h)
HCI_CHANNEL_CONTROL = 1   # mgmt channel on raw HCI socket
MGMT_OP_SET_DEV_ID = 0x0023

# SlothOS Controller — VID/PID assigned by pid.codes (VID 0x1209 / PID 0x5017).
# PR: https://github.com/pidcodes/pidcodes.github.com/pull/1235
# 0x1209 is the pid.codes VID for open-source projects. If the PR is rejected
# or you want to ship your own values, edit here and re-pair on the host.
SOURCE = 0x0002  # USB-IF
VENDOR = 0x1209  # pid.codes
PRODUCT = 0x5017  # SlothOS Controller
VERSION = 0x0100


def main() -> int:
    # Open HCI socket via the bluetooth module if available, else raw
    try:
        import bluetooth
        sock = bluetooth._bt.hci_open_dev(0)
        fd = sock.fileno()
    except Exception:
        # Fall back to native socket
        # AF_BLUETOOTH=31, SOCK_RAW=3, BTPROTO_HCI=1
        sock = socket.socket(31, socket.SOCK_RAW | socket.SOCK_CLOEXEC, 1)
        # Bind to HCI dev 0
        sock.bind((0,))
        fd = sock.fileno()

    # Build MGMT command: header (opcode u16, index u16, len u16) + payload
    payload = struct.pack("<HHHH", SOURCE, VENDOR, PRODUCT, VERSION)
    # MGMT Channel Header: code=MGMT_OP_SET_DEV_ID, but mgmt socket uses a
    # different framing — actual format is opcode + index + param_len + param
    header = struct.pack("<HHH", MGMT_OP_SET_DEV_ID, 0, len(payload))
    pkt = header + payload

    # Send via the mgmt control channel. Different from HCI command sockets.
    # On Linux, raw HCI sockets with channel=CONTROL expect mgmt framing.
    # Workaround: send via the socket's raw write.
    import os
    try:
        os.write(fd, pkt)
    except OSError as exc:
        print(f"send failed: {exc}", file=sys.stderr)
        return 1

    # Read response
    import time
    time.sleep(0.5)
    sock.settimeout(2.0) if hasattr(sock, "settimeout") else None
    try:
        data = os.read(fd, 256)
        print(f"mgmt response: {data.hex()}")
        # Response: opcode(2) + index(2) + len(2) + status(1) + param
        if len(data) >= 7:
            status = data[6]
            print(f"status: 0x{status:02x} ({'OK' if status == 0 else 'ERROR'})")
            return 0 if status == 0 else 2
    except Exception as exc:
        print(f"recv failed: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
