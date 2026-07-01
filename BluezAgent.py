"""Minimal org.bluez.Agent1 with DisplayYesNo capability.

Forces BlueZ to use MITM-protected SSP, which generates a Type 5
(Authenticated Combination) link key instead of Type 4 (Unauthenticated).
Windows 11's HID stack rejects Type 4 keys — symptom: pair completes,
L2CAP PSM 17 connects successfully, then Windows immediately disconnects
without sending any HIDP data (HCI trace: ``Disconn req`` ~3 ms after
``Connect rsp``).

All prompts are auto-answered since this is a headless device:
  - RequestConfirmation: log the passkey, return success (numeric comparison
    is shown on the *host* side; the user confirms there).
  - RequestPinCode: return a fixed "0000".
  - AuthorizeService: always allow (we only expose one service anyway).
"""

import logging

import dbus
import dbus.service

log = logging.getLogger("bt_gamepad.agent")

AGENT_PATH = "/bluez/bluezgp/bt_agent"
CAPABILITY = "DisplayYesNo"


class BluezAgent(dbus.service.Object):
    """Auto-approving Agent1 — its existence forces MITM SSP."""

    def __init__(self, bus) -> None:
        dbus.service.Object.__init__(self, bus, AGENT_PATH)
        self._bus = bus

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Release(self):
        log.info("Agent1.Release")

    @dbus.service.method("org.bluez.Agent1", in_signature="s",
                         out_signature="s")
    def RequestPinCode(self, device):
        log.info("RequestPinCode device=%s — returning '0000'", device)
        return "0000"

    @dbus.service.method("org.bluez.Agent1", in_signature="s",
                         out_signature="u")
    def RequestPasskey(self, device):
        log.info("RequestPasskey device=%s — returning 0", device)
        return dbus.UInt32(0)

    @dbus.service.method("org.bluez.Agent1", in_signature="ss",
                         out_signature="")
    def DisplayPinCode(self, device, pincode):
        log.info("DisplayPinCode device=%s pincode=%s", device, pincode)

    @dbus.service.method("org.bluez.Agent1", in_signature="uuu",
                         out_signature="")
    def DisplayPasskey(self, device, passkey, entered):
        if entered == 0:
            log.info("DisplayPasskey device=%s passkey=%06d", device, passkey)

    @dbus.service.method("org.bluez.Agent1", in_signature="su",
                         out_signature="")
    def RequestConfirmation(self, device, passkey):
        # Numeric comparison — user confirms on the host. Auto-accept here.
        log.info("RequestConfirmation device=%s passkey=%06d — auto-accept",
                 device, passkey)
        self._mark_trusted(device)
        return

    @dbus.service.method("org.bluez.Agent1", in_signature="ss",
                         out_signature="")
    def AuthorizeService(self, device, uuid):
        log.info("AuthorizeService device=%s uuid=%s — allow", device, uuid)
        self._mark_trusted(device)
        return

    def _mark_trusted(self, device_path: str) -> None:
        """Set Trusted=true on the paired device so subsequent connections
        don't bounce. Without this, the bond is stored as Trusted=false and
        Windows' HID enumeration phase fails the auth check.
        """
        try:
            props = dbus.Interface(
                self._bus.get_object("org.bluez", device_path),
                "org.freedesktop.DBus.Properties",
            )
            props.Set("org.bluez.Device1", "Trusted", dbus.Boolean(True, variant_level=1))
            log.info("marked %s Trusted=true", device_path)
        except Exception as exc:
            log.warning("mark Trusted failed for %s: %s", device_path, exc)

    @dbus.service.method("org.bluez.Agent1", in_signature="",
                         out_signature="")
    def Cancel(self):
        log.info("Agent1.Cancel")


def register(bus) -> BluezAgent:
    """Create the agent and register it as the default for AgentManager1."""
    agent = BluezAgent(bus)
    manager = dbus.Interface(
        bus.get_object("org.bluez", "/org/bluez"),
        "org.bluez.AgentManager1",
    )
    manager.RegisterAgent(AGENT_PATH, CAPABILITY)
    manager.RequestDefaultAgent(AGENT_PATH)
    log.info("Agent1 registered (capability=%s) as default", CAPABILITY)
    return agent
