"""Classic BR/EDR Bluetooth HID device via hand-built SDP record + raw L2CAP.

This is the path that actually works on BlueZ 5.x for the HID *device* role:
  1. bluetoothd restarted with --compat (unlocks Profile1.RegisterProfile and
     the deprecated PSM-bindable L2CAP API).
  2. Register a Profile1 object + our hand-built HID SDP XML via
     ProfileManager1.RegisterProfile. The hand-built record is the missing
     ingredient — BlueZ's default HID device path publishes an SDP record
     lacking the HID-specific attributes (PSMs, descriptor, subclass) and
     Android refuses to open the HID channel.
  3. Open two raw L2CAP sockets via stdlib ``socket(AF_BLUETOOTH)``: PSM 17
     (control), PSM 19 (interrupt). Host connects once the SDP record
     advertises them. (We bypass PyBluez because its C extension raises
     ``PY_SSIZE_T_CLEAN`` errors on send/setsockopt with Python 3.10+.)
  4. Accept both, then pump HID reports on the interrupt socket.
"""

from __future__ import annotations

import errno
import logging
import os
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

import dbus
import dbus.service
# Attach the GLib main loop to dbus BEFORE any SystemBus object exports
# objects. Without this, dbus.service.Object.__init__ raises RuntimeError
# ("D-Bus connections must be attached to a main loop").
from dbus.mainloop.glib import DBusGMainLoop
DBusGMainLoop(set_as_default=True)
from gi.repository import GLib

# Raw AF_BLUETOOTH L2CAP constants (linux/l2cap.h, linux/socket.h).
# Using stdlib socket.socket avoids PyBluez's PY_SSIZE_T_CLEAN bugs.
AF_BLUETOOTH = 31
BTPROTO_L2CAP = 0
SOL_L2CAP = 6
L2CAP_LM = 3
L2CAP_LM_MASTER = 0x0001
L2CAP_LM_AUTH = 0x0002
L2CAP_LM_ENCRYPT = 0x0004
L2CAP_LM_TRUSTED = 0x0008
L2CAP_LM_SECURE = 0x0020

# Type alias for readability — could be a PyBluez BluetoothSocket OR a raw
# socket.socket; we use the raw form below.
L2CAPSocket = socket.socket

from BluezAgent import register as register_agent
from BluezProfile import BluezProfile

log = logging.getLogger("bt_gamepad.l2cap")

# Fixed HID PSMs (Bluetooth Core Spec, Vol 3, Part A, Section 4.2).
PSM_CONTROL = 17   # 0x0011
PSM_INTERRUPT = 19  # 0x0013

HID_PROFILE_UUID = "00001124-0000-1000-8000-00805f9b34fb"
PNP_PROFILE_UUID = "00001200-0000-1000-8000-00805f9b34fb"

# The UUID we register Profile1 under. May be any unique UUID; this is the
# one BluezGP uses. The host sees only the HID UUID from the SDP record.
PROFILE_UUID = "1f16e7c0-b59b-11e3-95d2-0002a5d5c51b"

PROFILE_PATH = "/bluez/bluezgp/bt_profile"

# CoD: 0x002504 = Peripheral / Joystick / (no services).
#   major service bits = 0x20 (Limited Discoverable Mode) | 0x04 (Capturing)
#   Actually: 0x002504 -> major device class 0x05 (Peripheral),
#             minor 0x04 (Joystick). See Bluetooth Assigned Numbers.
DEVICE_CLASS = "002504"


def _hciconfig_up() -> None:
    """Bring hci0 up. With bluetoothd --noplugin=* the adapter is not
    auto-powered, so this is required before Adapter1.Powered works.
    """
    try:
        subprocess.run(
            ["hciconfig", "hci0", "up"],
            check=True, capture_output=True, text=True, timeout=5,
        )
    except subprocess.CalledProcessError as exc:
        log.warning(
            "hciconfig hci0 up failed (rc=%d): %s",
            exc.returncode, (exc.stderr or "").strip(),
        )
    except Exception as exc:
        log.warning("hciconfig hci0 up failed: %s", exc)


def _set_class_of_device() -> None:
    """Force Class of Device to 0x002508 (Peripheral/Joystick/Capturing).

    BlueZ's external Profile1.RegisterProfile path does NOT update CoD
    from the profile's subclass (only built-in plugins get that treatment),
    and the Realtek firmware on this adapter silently ignores ``btmgmt
    class`` and main.conf ``Class =``. The only thing that works is a
    direct ``HCI_Write_Class_Of_Device`` (ogf=0x03 ocf=0x0024) with the
    3-byte CoD payload in little-endian.

    Without an explicit CoD, Windows 11's HidBth driver loads but fails
    to start (Problem 0xA / STATUS_UNSUCCESSFUL) and never even attempts
    to connect to PSM 17 — it rejects the device at the CoD-validation
    step before opening the HID channel.
    """
    # 0x002508 → little-endian bytes: 0x08, 0x25, 0x00
    try:
        subprocess.run(
            ["hcitool", "-i", "hci0", "cmd", "0x03", "0x0024",
             "0x08", "0x25", "0x00"],
            check=True, capture_output=True, text=True, timeout=5,
        )
        log.info("CoD set to 0x002508 (Peripheral/Joystick/Capturing)")
    except Exception as exc:
        log.warning("HCI Write Class of Device failed: %s", exc)


class L2CAPHIDServer:
    """Owns the Profile1 registration + raw L2CAP sockets + accept loop."""

    ADAPTER_PATH = "/org/bluez/hci0"

    def __init__(self, sdp_record_path: str, alias: str) -> None:
        self.sdp_record_path = Path(sdp_record_path)
        self.alias = alias
        self.bus: Optional[dbus.Bus] = None
        self.profile: Optional[BluezProfile] = None
        self.profile_pnp: Optional[BluezProfile] = None
        self.agent = None
        self.sock_ctrl: Optional[L2CAPSocket] = None
        self.sock_intr: Optional[L2CAPSocket] = None
        # Connected client sockets (set on accept, cleared on disconnect).
        self.client_intr: Optional[L2CAPSocket] = None
        self.client_ctrl: Optional[L2CAPSocket] = None
        self._client_lock = threading.Lock()
        self._accept_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._loop: Optional[GLib.MainLoop] = None
        # Hook fired right after both channels come up — lets the caller mark
        # the state dirty so a baseline INPUT report is emitted immediately.
        self.on_connect: Optional[callable] = None

    # ---------- adapter + SDP registration ----------

    def _setup_adapter(self) -> None:
        # Bring hci0 up at the kernel level first.
        _hciconfig_up()
        # Then drive Adapter1 properties via D-Bus (BlueZ 5.x ignores the
        # legacy hciconfig class/name/scan on most distros).
        self.bus = dbus.SystemBus()
        adapter = dbus.Interface(
            self.bus.get_object("org.bluez", self.ADAPTER_PATH),
            "org.freedesktop.DBus.Properties",
        )
        try:
            adapter.Set("org.bluez.Adapter1", "Powered", dbus.Boolean(True, variant_level=1))
            log.info("adapter Powered=true")
        except Exception as exc:
            log.warning("Adapter1.Powered=true failed: %s", exc)
        try:
            adapter.Set("org.bluez.Adapter1", "Alias",
                        dbus.String(self.alias, variant_level=1))
            log.info("adapter Alias=%r", self.alias)
        except Exception as exc:
            log.warning("Adapter1.Alias failed: %s", exc)
        try:
            adapter.Set("org.bluez.Adapter1", "Pairable",
                        dbus.Boolean(True, variant_level=1))
        except Exception as exc:
            log.warning("Adapter1.Pairable failed: %s", exc)
        try:
            adapter.Set("org.bluez.Adapter1", "Discoverable",
                        dbus.Boolean(True, variant_level=1))
        except Exception as exc:
            log.warning("Adapter1.Discoverable failed: %s", exc)
        # auth+encrypt on the adapter (kernel-enforced for all links).
        for flag in ("auth", "encrypt"):
            try:
                subprocess.run(["hciconfig", "hci0", flag],
                               check=True, capture_output=True, timeout=5)
            except Exception as exc:
                log.warning("hciconfig hci0 %s failed: %s", flag, exc)
        log.info("adapter ready: alias=%r pairable discoverable, AUTH+ENCRYPT set",
                 self.alias)

    def _force_cod_after_profile(self) -> None:
        """Re-apply CoD after Profile1 registration. BlueZ clears CoD when an
        external profile is registered (because the external-profile path
        doesn't propagate the profile's subclass to the kernel like built-in
        plugins do). Must be called AFTER ``RegisterProfile``.
        """
        _set_class_of_device()

    def _register_profile(self) -> None:
        # Register the SSP Agent FIRST — its existence with DisplayYesNo
        # capability makes BlueZ request MITM-protected SSP, producing a
        # Type 5 (Authenticated Combination) link key. Windows 11 HID
        # requires Type 5; with no agent registered BlueZ falls back to
        # Type 4 (Unauthenticated) and Windows refuses to enumerate.
        self.bus = dbus.SystemBus()
        try:
            self.agent = register_agent(self.bus)
        except Exception as exc:
            log.warning("Agent registration failed: %s — Type 5 keys unavailable", exc)

        manager = dbus.Interface(
            self.bus.get_object("org.bluez", "/org/bluez"),
            "org.bluez.ProfileManager1",
        )

        # HID profile (UUID 0x1124) — primary service.
        record_xml = self.sdp_record_path.read_text(encoding="utf-8")
        opts = {
            "ServiceRecord": record_xml,
            "Role": "server",
            # Windows 11 / Android require authenticated+encrypted L2CAP for
            # HID. With False the host connects, BlueZ rejects the security
            # request (HCI status 0x0E = "rejected due to security reasons"),
            # and the host disconnects within ~25 ms.
            "RequireAuthentication": dbus.Boolean(True),
            "RequireAuthorization": dbus.Boolean(False),
        }
        self.profile = BluezProfile(self.bus, PROFILE_PATH)
        manager.RegisterProfile(PROFILE_PATH, HID_PROFILE_UUID, opts)
        log.info("Profile1 registered (UUID %s) with hand-built SDP record",
                 HID_PROFILE_UUID)

        # PnP profile (UUID 0x1200) — override BlueZ's auto-generated record
        # which has bogus VendorIDSource/VID/PID. Windows looks up driver
        # matches by VID/PID; Linux Foundation / 0x0246 has no driver so
        # HidBth fails to start. We masquerade as Xbox 360 controller.
        pnp_path = self.sdp_record_path.parent / "sdp_record_pnp.xml"
        if pnp_path.exists():
            try:
                pnp_xml = pnp_path.read_text(encoding="utf-8")
                pnp_opts = {
                    "ServiceRecord": pnp_xml,
                    "Role": "server",
                    "RequireAuthentication": dbus.Boolean(False),
                    "RequireAuthorization": dbus.Boolean(False),
                }
                self.profile_pnp = BluezProfile(self.bus, PROFILE_PATH + "_pnp")
                manager.RegisterProfile(PROFILE_PATH + "_pnp", PNP_PROFILE_UUID, pnp_opts)
                log.info("PNP Profile1 registered (UUID %s) — VID/PID overridden",
                         PNP_PROFILE_UUID)
            except Exception as exc:
                log.warning("PNP profile registration failed: %s", exc)

        # BlueZ clears CoD when an external profile is registered — re-apply.
        self._force_cod_after_profile()
        # Delete BlueZ's auto-generated PNP record (handle 0x10000) which has
        # bogus VendorIDSource=0x1d6b (Linux Foundation) — Windows' HidBth
        # driver-matching uses the FIRST PNP record and rejects unknown vendors
        # with Problem 0xA. ``sdptool del 0x10000`` removes it; our PNP profile
        # (registered just above) becomes the only one. Must run AFTER our
        # profile registration so we still have a PNP record at all.
        try:
            subprocess.run(
                ["sdptool", "del", "0x10000"],
                check=True, capture_output=True, text=True, timeout=5,
            )
            log.info("deleted BlueZ's auto-PNP record (handle 0x10000) — our Xbox 360 override is now sole PNP")
        except subprocess.CalledProcessError as exc:
            log.warning("sdptool del 0x10000 failed (rc=%d): %s",
                        exc.returncode, (exc.stderr or "").strip())
        except Exception as exc:
            log.warning("sdptool del 0x10000 failed: %s", exc)

    # ---------- L2CAP sockets ----------

    def _bind_l2cap(self, psm: int) -> L2CAPSocket:
        """Bind a raw AF_BLUETOOTH L2CAP SEQPACKET socket to the given PSM.

        Uses stdlib ``socket.socket`` so ``setsockopt`` actually works
        (PyBluez's wrapper raises PY_SSIZE_T_CLEAN on Python 3.10+).
        ``L2CAP_LM = LM_AUTH | LM_ENCRYPT | LM_TRUSTED`` enforces
        authenticated + encrypted links at the L2CAP layer, which is what
        Windows 11 / modern Android require for HID. Without it, the host
        connects, the kernel refuses the security upgrade (HCI 0x0E
        "rejected due to security reasons"), and the host drops within
        ~10 ms.
        """
        sock = socket.socket(AF_BLUETOOTH, socket.SOCK_SEQPACKET, BTPROTO_L2CAP)
        flags = L2CAP_LM_AUTH | L2CAP_LM_ENCRYPT | L2CAP_LM_TRUSTED
        try:
            sock.setsockopt(SOL_L2CAP, L2CAP_LM, struct.pack("i", flags))
            log.info("PSM %d: L2CAP_LM=0x%x (auth|encrypt|trusted)", psm, flags)
        except OSError as exc:
            log.warning("L2CAP_LM setsockopt failed: %s (continuing)", exc)
        # Bind to BDADDR_ANY (all zeros) — kernel picks the local adapter.
        sock.bind(("00:00:00:00:00:00", psm))
        sock.listen(1)
        log.info("listening on PSM %d", psm)
        return sock

    # ---------- accept loop ----------

    def _accept_loop(self) -> None:
        """Accept control + interrupt connections, in that order.

        Host connects control first (handshake), then interrupt (data). We
        block-accept control, then interrupt, then mark connected. On either
        side disconnecting, we reset and re-accept.
        """
        assert self.sock_ctrl is not None and self.sock_intr is not None
        while not self._stop.is_set():
            c_ctrl = None
            c_intr = None
            ctrl_thread = None
            try:
                log.info("waiting for control channel...")
                c_ctrl, cinfo_ctrl = self.sock_ctrl.accept()
                log.info("control channel connected from %s", cinfo_ctrl)
                # Start the control-channel drainer IMMEDIATELY — Windows &
                # Android send SET_PROTOCOL / HANDSHAKE probes here and will
                # only open the interrupt channel once they get a response.
                ctrl_thread = threading.Thread(
                    target=self._drain_ctrl, args=(c_ctrl,),
                    name="l2cap-ctrl", daemon=True,
                )
                ctrl_thread.start()
                log.info("waiting for interrupt channel...")
                c_intr, cinfo_intr = self.sock_intr.accept()
                log.info("interrupt channel connected from %s", cinfo_intr)
                with self._client_lock:
                    self.client_intr = c_intr
                    self.client_ctrl = c_ctrl
                self._connected.set()
                # Let the caller push a baseline INPUT report so the host
                # enumerates the device (Android won't show a gamepad until
                # it sees at least one).
                if self.on_connect is not None:
                    try:
                        self.on_connect()
                    except Exception as exc:
                        log.warning("on_connect callback raised: %s", exc)
                # Block while the interrupt socket is alive; detect by
                # peeking. recv() returns b'' on orderly disconnect and
                # raises on reset.
                self._drain_until_closed(c_intr, c_ctrl)
                # Interrupt gone — close the control peer so the drainer exits.
                try:
                    c_ctrl.close()
                except Exception:
                    pass
            except OSError as exc:
                if not self._stop.is_set():
                    log.warning("accept loop error: %s — retrying", exc)
                    time.sleep(1.0)
            finally:
                with self._client_lock:
                    if self.client_intr is not None:
                        try:
                            self.client_intr.close()
                        except Exception:
                            pass
                        self.client_intr = None
                self._connected.clear()
                try:
                    if 'c_ctrl' in dir() and c_ctrl is not None:
                        c_ctrl.close()
                except Exception:
                    pass
                if not self._stop.is_set():
                    log.info("connection closed; re-listening")

    def _drain_ctrl(self, ctrl: L2CAPSocket) -> None:
        """Consume host->device HIDP messages on the control channel.

        Per HIDP spec the high nibble of byte 0 is the message type:
          0x0n = HANDSHAKE       (device->host response)
          0x1n = HID_CONTROL
          0x3n = DATA
          0x4n = (host -> device): GET_REPORT (n=report-type)
          0x5n = SET_REPORT
          0x6n = GET_PROTOCOL / 0x7n = SET_PROTOCOL
        The right answer for nearly every host->device probe is
        HANDSHAKE_SUCCESSFUL = 0x00 — we don't actually implement GET/SET
        semantics, we just stop the host from declaring us non-conforming.
        """
        ctrl.settimeout(1.0)
        while not self._stop.is_set():
            try:
                data = ctrl.recv(64)
            except (socket.timeout, TimeoutError):
                continue
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    continue
                if "timed out" in str(exc).lower():
                    continue
                log.info("control socket closed: %s", exc)
                return
            if data == b"":
                log.info("control socket orderly disconnect")
                return
            if data:
                log.debug("ctrl recv %d bytes: %s", len(data), data.hex())
                # Respond HANDSHAKE_SUCCESSFUL to any message that expects one.
                try:
                    if len(data) >= 1:
                        ctrl.send(b"\x00")
                        log.debug("ctrl sent HANDSHAKE 0x00")
                except OSError as exc:
                    log.info("ctrl send failed: %s", exc)
                    return

    def _drain_until_closed(self, intr: L2CAPSocket, ctrl: L2CAPSocket) -> None:
        """Block while the connection is up; consume any HANDOFF messages.

        PyBluez's L2CAP recv() can raise either ``socket.timeout`` or a plain
        ``OSError('timed out')`` — match on the message text to be robust.
        EAGAIN/EWOULDBLOCK also means "no data, retry" on a timeout-configured
        SEQPACKET socket — do not treat as disconnect.
        """
        intr.settimeout(1.0)
        while not self._stop.is_set():
            try:
                data = intr.recv(64)
            except (socket.timeout, TimeoutError):
                continue
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    continue
                if "timed out" in str(exc).lower():
                    continue
                log.info("interrupt socket closed: %s", exc)
                return
            if data == b"":
                log.info("interrupt socket orderly disconnect")
                return
            # Host -> device messages on the interrupt channel are rare;
            # most host traffic (SET_REPORT etc.) lands on control. Log only.
            if data:
                log.debug("intr recv %d bytes: %s", len(data), data.hex())

    # ---------- public API ----------

    def start(self) -> None:
        self._setup_adapter()
        self._register_profile()
        self.sock_ctrl = self._bind_l2cap(PSM_CONTROL)
        self.sock_intr = self._bind_l2cap(PSM_INTERRUPT)
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="l2cap-accept", daemon=True,
        )
        self._accept_thread.start()

    def serve(self) -> None:
        """Run the GLib main loop on the calling thread. Blocks until stop()."""
        self._loop = GLib.MainLoop()
        try:
            self._loop.run()
        except KeyboardInterrupt:
            self.stop()

    def stop(self) -> None:
        log.info("stopping L2CAP HID server")
        self._stop.set()
        if self._loop is not None:
            try:
                self._loop.quit()
            except Exception:
                pass
        for sock in (self.sock_ctrl, self.sock_intr):
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
        with self._client_lock:
            if self.client_intr is not None:
                try:
                    self.client_intr.close()
                except Exception:
                    pass
                self.client_intr = None
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2.0)
        # Unregister profile so a restart is clean.
        if self.bus is not None:
            try:
                manager = dbus.Interface(
                    self.bus.get_object("org.bluez", "/org/bluez"),
                    "org.bluez.ProfileManager1",
                )
                manager.UnregisterProfile(PROFILE_PATH)
                log.info("Profile1 unregistered")
            except Exception as exc:
                log.warning("UnregisterProfile failed: %s", exc)

    def is_connected(self) -> bool:
        return self._connected.is_set()

    def send_report(self, report: bytes) -> bool:
        """Send a HID report (already prefixed with 0xA1, report-id, ...) on
        the interrupt channel. Returns False if no host is connected.

        Raw ``socket.send`` on a SOCK_SEQPACKET socket writes one L2CAP
        datagram per call — exactly HIDP INPUT-report semantics.
        """
        with self._client_lock:
            sock = self.client_intr
        if sock is None:
            return False
        try:
            sock.send(bytes(report))
            return True
        except OSError as exc:
            log.warning("send_report failed: %s — marking disconnected", exc)
            self._connected.clear()
            return False
        except Exception as exc:
            log.warning("send_report non-OSError: %s", exc)
            return False
