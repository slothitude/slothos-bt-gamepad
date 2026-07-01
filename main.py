"""bt_gamepad — expose the RG35XX H as a Bluetooth HID gamepad.

Classic BR/EDR path: hand-built HID SDP record + raw L2CAP (PSM 17/19) via
PyBluez, registered through BlueZ's Profile1 (requires bluetoothd --compat).

Pipeline:
    /dev/input/event1 ─▶ evdev_reader ─▶ GameState ─▶ report-pump ─▶ L2CAP intr
                                                └─▶ Profile1 + SDP record (BlueZ)

CLI (unchanged from the GATT version):
    python3 main.py                       foreground, info logging
    python3 main.py --verbose             debug (evdev stream + report bytes)
    python3 main.py --device /dev/input/event0   alternate input node
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from pathlib import Path

import evdev.ecodes as e

import evdev_to_hid as mapping
from bt_l2cap_v2 import L2CAPHIDServer
from evdev_reader import EvdevReader

# SPP server is optional — if the import fails the HID stack still comes up.
# A log warning is emitted later in Controller.start once `log` exists.
try:
    from bt_spp import SppServer
except Exception as _spp_import_exc:  # pragma: no cover (defensive)
    SppServer = None  # type: ignore[assignment]
    _SPP_IMPORT_EXC = _spp_import_exc
else:
    _SPP_IMPORT_EXC = None

# SPP HTTP bridge (Phase B.4) — optional. Lets the assistant daemon call
# into our SppServer via a localhost HTTP endpoint. If the import fails
# (or the SPP server isn't up), HID still works.
try:
    from spp_http import SppHttpBridge
except Exception as _bridge_import_exc:  # pragma: no cover (defensive)
    SppHttpBridge = None  # type: ignore[assignment]
    _BRIDGE_IMPORT_EXC = _bridge_import_exc
else:
    _BRIDGE_IMPORT_EXC = None

log = logging.getLogger("bt_gamepad")

HERE = Path(__file__).resolve().parent
DEFAULT_SDP_RECORD = str(HERE / "sdp_record_gamepad.xml")

# HID interrupt reports are DATA | INPUT: prefix 0xA1, then Report ID.
HID_DATA_INPUT = 0xA1
HID_REPORT_ID = 0x01


class GameState:
    """Holds the current HID report body (10 bytes). Report ID prefixed on send."""

    def __init__(self) -> None:
        # Layout: [btn0, btn1, btn2, hat, X, Y, Z, Rz, Rx, Ry]
        # btn0..btn2 = 24-button field (LSB-first, button 1 = btn0 bit 0)
        # hat byte low nibble = compass, high nibble = 4-bit pad
        self._body = bytearray(10)
        self._lock = threading.Lock()
        self._dirty = True  # emit one report at startup so host sees a baseline

    def set_button(self, hid_button: int, pressed: bool) -> None:
        if hid_button < 1 or hid_button > 24:
            return
        bit = hid_button - 1
        byte_idx = bit >> 3           # 0, 1, or 2 for buttons 1-24
        bit_idx = bit & 0x7
        with self._lock:
            mask = 1 << bit_idx
            if pressed:
                self._body[byte_idx] |= mask
            else:
                self._body[byte_idx] &= ~mask
            self._dirty = True

    def set_hat(self, value: int) -> None:
        with self._lock:
            if self._body[3] != value:
                self._body[3] = value
                self._dirty = True

    def set_axis(self, offset: int, value: int) -> None:
        if offset < 0 or offset >= 6:
            return
        b = max(-127, min(127, value)) & 0xFF
        with self._lock:
            if self._body[4 + offset] != b:
                self._body[4 + offset] = b
                self._dirty = True

    def consume(self) -> bytes | None:
        """Return the 10-byte body if changed since last consume, else None."""
        with self._lock:
            if not self._dirty:
                return None
            self._dirty = False
            return bytes(self._body)

    def mark_dirty(self) -> None:
        """Force the next consume() to return the body even if unchanged.
        Used to push a baseline report on new connections."""
        with self._lock:
            self._dirty = True


class Controller:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.state = GameState()
        self._dpad_x = 0
        self._dpad_y = 0
        self._stop = threading.Event()
        self._reader = EvdevReader(
            device_path=args.device, on_event=self._on_evdev
        )
        self.server = L2CAPHIDServer(
            sdp_record_path=args.sdp_record,
            alias=args.alias,
        )
        self.server.on_connect = self._on_connect
        self._send_thread: threading.Thread | None = None
        self.spp: "SppServer | None" = None
        self.spp_bridge: "SppHttpBridge | None" = None

    def _on_connect(self) -> None:
        """Called from the accept loop when a host fully connects. Mark the
        state dirty so the next pump iteration emits a baseline INPUT report
        — hosts won't enumerate a HID device until they see at least one.
        """
        self.state.mark_dirty()

    # ---- evdev -> state struct ----
    def _on_evdev(self, etype: int, code: int, value: int) -> None:
        if etype == e.EV_KEY:
            if code in mapping.BUTTON_MAP:
                self.state.set_button(mapping.BUTTON_MAP[code], bool(value))
            elif code in mapping.DPAD_BUTTONS:
                x, y = mapping.DPAD_BUTTONS[code]
                if x:
                    self._dpad_x = x if value else 0
                if y:
                    self._dpad_y = y if value else 0
                self.state.set_hat(mapping.hat_from_axes(self._dpad_x, self._dpad_y))
        elif etype == e.EV_ABS:
            if code == mapping.DPAD_AXIS_X:
                self._dpad_x = max(-1, min(1, value))
                self.state.set_hat(mapping.hat_from_axes(self._dpad_x, self._dpad_y))
            elif code == mapping.DPAD_AXIS_Y:
                self._dpad_y = max(-1, min(1, value))
                self.state.set_hat(mapping.hat_from_axes(self._dpad_x, self._dpad_y))
            else:
                target = mapping.AXIS_ALIASES.get(code, code)
                if target in mapping.AXIS_MAP:
                    offset = mapping.AXIS_MAP[target]
                    normalized = mapping.normalize_axis(code, value, self._reader.abs_info)
                    self.state.set_axis(offset, normalized)
        elif etype == e.EV_SYN:
            pass

        if self.args.verbose and etype != e.EV_SYN:
            log.debug("evdev type=%d code=%d value=%d", etype, code, value)

    # ---- report pump ----
    def _send_loop(self) -> None:
        """Drain dirty state at ~120 Hz; emit HID interrupt report if connected."""
        while not self._stop.is_set():
            body = self.state.consume()
            if body is not None:
                # HID INPUT report header: 0xA1 (data | input), then report ID.
                report = bytes([HID_DATA_INPUT, HID_REPORT_ID]) + body
                ok = self.server.send_report(report)
                if ok and self.args.verbose:
                    log.debug("sent: %s", report.hex())
            time.sleep(1.0 / 120.0)

    # ---- lifecycle ----
    def start(self) -> None:
        log.info("bt_gamepad starting — input=%s", self.args.device)
        self._reader.open()
        self._reader.start()
        self.server.start()
        # Start the SPP server AFTER the HID stack is up. Wrapped in
        # try/except so any failure here never takes HID down — the HID
        # path runs on its own thread and is unaffected by SPP state.
        if SppServer is None:
            log.warning("SPP disabled (bt_spp import failed: %s)", _SPP_IMPORT_EXC)
        else:
            try:
                self.spp = SppServer(bus=self.server.bus, log_fn=self._spp_log)
                self.spp.start()
                log.info("[main] spp server started on channel %d",
                         self.spp.channel)
            except Exception as exc:
                log.error("SPP start failed (HID still OK): %s", exc)
                self.spp = None
            # Phase B.4: start the localhost HTTP bridge wrapping the
            # SppServer. Wrapped in try/except like the SPP start — a
            # failure here never takes HID down.
            if SppHttpBridge is None:
                log.warning("spp bridge disabled (import failed: %s)",
                            _BRIDGE_IMPORT_EXC)
            elif self.spp is None:
                log.warning("spp bridge disabled (spp server not running)")
            else:
                try:
                    self.spp_bridge = SppHttpBridge(spp=self.spp, port=8447)
                    self.spp_bridge.start()
                except Exception as exc:
                    log.error("spp bridge start failed (HID still OK): %s", exc)
                    self.spp_bridge = None
        log.info("ready — pair from host as '%s'", self.args.alias)
        self._send_thread = threading.Thread(
            target=self._send_loop, name="report-pump", daemon=True
        )
        self._send_thread.start()
        # GLib main loop owns the main thread (needed for Profile1 callbacks).
        self.server.serve()

    @staticmethod
    def _spp_log(msg: str) -> None:
        log.info("%s", msg)

    def stop(self) -> None:
        log.info("stopping")
        self._stop.set()
        if self.spp is not None:
            try:
                self.spp.stop()
            except Exception as exc:
                log.warning("SPP stop failed: %s", exc)
        if self.spp_bridge is not None:
            try:
                self.spp_bridge.stop()
            except Exception as exc:
                log.warning("spp bridge stop failed: %s", exc)
        self.server.stop()
        self._reader.stop()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="RG35XX H BT HID gamepad daemon (BR/EDR L2CAP)"
    )
    p.add_argument("--device", default="/dev/input/event1",
                   help="evdev input node (default: /dev/input/event1)")
    p.add_argument("--alias", default="SlothOS Controller",
                   help="Bluetooth display name (default: 'SlothOS Controller')")
    p.add_argument("--sdp-record", default=DEFAULT_SDP_RECORD,
                   help="Path to the hand-built HID SDP record XML")
    p.add_argument("--verbose", action="store_true", help="debug logging")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    ctrl = Controller(args)

    def _signal(signum, frame):
        log.info("caught signal %d", signum)
        ctrl.stop()
        sys.exit(0)
    signal.signal(signal.SIGINT, _signal)
    signal.signal(signal.SIGTERM, _signal)

    ctrl.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
