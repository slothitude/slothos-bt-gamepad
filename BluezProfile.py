"""Minimal org.bluez.Profile1 D-Bus object for the HID device role.

Python 3 port of BluezGP's BluezProfile.py. BlueZ calls NewConnection when a
host connects to our registered profile; we don't use the fd BlueZ hands us
because we open our own raw L2CAP sockets on PSM 17/19 via PyBluez — the
hand-built SDP record is what makes BlueZ route the HID PSMs to us at all,
and PyBluez is what lets us push reports without going through BlueZ's
HID-Host plugin (which we disabled via --noplugin=*).

The Profile1 object exists primarily to satisfy BlueZ's API contract: it
needs *something* registered for our UUID via ProfileManager1.RegisterProfile
before it will publish our hand-built SDP record.
"""

import logging
import os

import dbus.service

log = logging.getLogger("bt_gamepad.profile")


class BluezProfile(dbus.service.Object):
    """A no-op Profile1 — the L2CAP sockets do the real work."""

    fd = -1

    def __init__(self, bus, path) -> None:
        dbus.service.Object.__init__(self, bus, path)
        self.path = path

    @dbus.service.method("org.bluez.Profile1", in_signature="", out_signature="")
    def Release(self):
        log.info("Profile1.Release")

    @dbus.service.method("org.bluez.Profile1", in_signature="", out_signature="")
    def Cancel(self):
        log.info("Profile1.Cancel")

    @dbus.service.method("org.bluez.Profile1", in_signature="oha{sv}", out_signature="")
    def NewConnection(self, path, fd, properties):
        # We don't keep this fd — PyBluez owns our L2CAP sockets. Close it.
        try:
            os.close(fd.take())
        except Exception as exc:
            log.debug("NewConnection fd close failed: %s", exc)
        log.info("Profile1.NewConnection path=%s", path)

    @dbus.service.method("org.bluez.Profile1", in_signature="o", out_signature="")
    def RequestDisconnection(self, path):
        log.info("Profile1.RequestDisconnection path=%s", path)
