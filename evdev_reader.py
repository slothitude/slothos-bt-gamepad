"""Reads /dev/input/event1 (ANBERNIC-keys) and exposes a callback API.

Runs in its own thread; calls the registered callback for every evdev event.
State (button bits / axis bytes / hat) is owned by the caller via a thread-safe
snapshot.

Device node may be /dev/input/event0 on some kernels — set via constructor.
"""

import logging
import threading
import time
from typing import Callable, Optional

import evdev
import evdev.ecodes as e

log = logging.getLogger("bt_gamepad.evdev")


class EvdevReader:
    def __init__(
        self,
        device_path: str = "/dev/input/event1",
        on_event: Optional[Callable[[int, int, int], None]] = None,
    ) -> None:
        self.device_path = device_path
        self._on_event = on_event
        self._dev: Optional[evdev.InputDevice] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # Cache absinfo once per code we care about.
        self.abs_info: dict = {}

    def open(self) -> None:
        log.info("opening %s", self.device_path)
        self._dev = evdev.InputDevice(self.device_path)
        caps = self._dev.capabilities(absinfo=True)
        # caps: {event_type: [(code, Absinfo), ...]} ; Absinfo only for EV_ABS
        for code, info in caps.get(e.EV_ABS, []):
            self.abs_info[code] = info
        log.info(
            "device name=%r phys=%r caps EV_ABS=%d EV_KEY=%d",
            self._dev.name,
            self._dev.phys,
            len(caps.get(e.EV_ABS, [])),
            len(caps.get(e.EV_KEY, [])),
        )

    def start(self) -> None:
        if self._dev is None:
            self.open()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="evdev-reader", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run(self) -> None:
        assert self._dev is not None
        # Brief retry loop on transient read errors; evdev.InputDevice.read_loop
        # raises OSError on USB/BT disconnect of the *source* device, which we
        # don't have here (built-in gamepad), but be defensive.
        while not self._stop.is_set():
            try:
                for event in self._dev.read_loop():
                    if self._stop.is_set():
                        break
                    if self._on_event is None:
                        continue
                    # Only forward sync'd events; EV_KEY/EV_ABS come paired
                    # with an EV_SYN at the end of a frame. We forward raw and
                    # let the consumer batch on EV_SYN.
                    self._on_event(event.type, event.code, event.value)
            except OSError as exc:
                log.warning("evdev read error: %s — retrying in 0.5s", exc)
                time.sleep(0.5)
            except Exception:
                log.exception("evdev reader fatal error")
                time.sleep(1.0)
