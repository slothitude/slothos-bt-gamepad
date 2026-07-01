#!/usr/bin/env python3
"""SlothOS Controller — Bluetooth Mode splash.

Displays the splash image fullscreen on /dev/fb0 via direct mmap
(the stock H700 firmware ships SDL without fbcon support, so we
write pixels the same way Clock / Image_Browser do — PIL + mmap).

Ensures bt_gamepad.service is running, and watches /dev/input/event1
for the Start+Select combo. On combo, stops the service and exits.

Multiple readers of /dev/input/event1 coexist (bt_gamepad's
evdev_reader does not EVIOCGRAB), so button events still reach the
BT stack and the paired host while the splash is up.
"""
import os
import sys
import struct
import subprocess
import time
from fcntl import ioctl
import mmap

import evdev
from PIL import Image, ImageDraw, ImageFont

SPLASH_PATH = "/usr/local/slothos/bt_mode/splash.png"
INPUT_DEV = "/dev/input/event1"
FB_DEV = "/dev/fb0"
SERVICE = "bt_gamepad"
PANEL_W, PANEL_H = 640, 480
ERROR_HOLD_SEC = 5

# On the Anbernic H700 the Start and Select buttons are mapped to
# evdev codes 311 and 310 (BTN_TR / BTN_TL on a standard pad), not
# BTN_START/BTN_SELECT (315/314). Verified on firmware 20251225.
# See APPS/mod_tools/input.py for the stock keymap.
CODE_START = 311
CODE_SELECT = 310

# fb ioctl constants (linux/fb.h)
FBIOGET_VSCREENINFO = 0x4600
FBIOPUT_VSCREENINFO = 0x4601
FBIOBLANK = 0x4611
VSCREENINFO_SIZE = 160

# Hardcoded fb_var_screeninfo blob for the RG35xxH (hw_info=5) — sets
# 640x480 @ 32bpp RGBA. Lifted verbatim from Clock/graphic.py because
# the firmware's stock display mode (1280x1024 @ 16bpp RGB565) won't
# accept RGBA writes. FBIOPUT_VSCREENINFO with this blob switches the
# panel into the mode our RGBA mmap expects.
FB_VINFO_RG35XXH = bytes.fromhex(
    "80020000" "e0010000" "80020000" "c0030000"   # xres, yres, xv, yv
    "00000000" "00000000" "20000000" "00000000"   # xoff, yoff, bpp=32, gray
    "08000000" "08000000" "00000000"              # red: off=8 len=8 msb=0
    "08000000" "08000000" "00000000"              # green
    "08000000" "08000000" "00000000"              # blue
    "08000000" "08000000" "00000000"              # alpha
    "00000000"                                     # nonstd
    "00000000"                                     # activate
    "00000000"                                     # height
    "00000000"                                     # width
    "00000000"                                     # accel_flags
    "c2a20000"                                     # pixclock
    "1a000000"                                     # left_margin
    "54000000"                                     # right_margin
    "0b000000"                                     # upper_margin
    "1b000000"                                     # lower_margin
    "14000000"                                     # hsync_len
    "04000000"                                     # vsync_len
    "00000000" "00000000" "00000000" "00000000"   # sync, vmode, rotate, colorspace
)
assert len(FB_VINFO_RG35XXH) <= VSCREENINFO_SIZE
FB_VINFO_RG35XXH = FB_VINFO_RG35XXH.ljust(VSCREENINFO_SIZE, b"\x00")

FONT_PATH = "/usr/share/fonts/TTF/DejaVuSansMono.ttf"


class Framebuffer:
    """Minimal /dev/fb0 mmap wrapper — matches stock Clock graphic.py."""

    def __init__(self):
        self.fd = -1
        self.mm = None
        self.saved_vinfo = None

    def open(self):
        self.fd = os.open(FB_DEV, os.O_RDWR)
        # Snapshot vscreeninfo so we can restore on exit (defensive —
        # matches the fbset snapshot we used to take via fbset).
        buf = bytearray(VSCREENINFO_SIZE)
        try:
            ioctl(self.fd, FBIOGET_VSCREENINFO, buf)
            self.saved_vinfo = bytes(buf)
        except OSError:
            self.saved_vinfo = None
        # Switch to 640x480 @ 32bpp RGBA + unblank so RGBA mmap writes
        # land correctly. Stock firmware boots at 1280x1024 @ 16bpp.
        try:
            ioctl(self.fd, FBIOPUT_VSCREENINFO,
                  bytearray(FB_VINFO_RG35XXH))
        except OSError as exc:
            sys.stderr.write(
                f"warning: FBIOPUT_VSCREENINFO failed: {exc}\n")
        try:
            ioctl(self.fd, FBIOBLANK, 0)
        except OSError:
            pass
        size = PANEL_W * PANEL_H * 4
        self.mm = mmap.mmap(self.fd, size)

    def write_image(self, img):
        """Blit a PIL RGBA image to the framebuffer."""
        if img.size != (PANEL_W, PANEL_H):
            img = img.resize((PANEL_W, PANEL_H), Image.LANCZOS)
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        self.mm.seek(0)
        self.mm.write(img.tobytes())

    def fill(self, color=(0, 0, 0)):
        img = Image.new("RGBA", (PANEL_W, PANEL_H), color)
        self.write_image(img)

    def close(self):
        if self.mm is not None:
            try:
                self.mm.close()
            except Exception:
                pass
            self.mm = None
        if self.fd >= 0:
            if self.saved_vinfo is not None:
                try:
                    ioctl(self.fd, FBIOPUT_VSCREENINFO,
                          bytearray(self.saved_vinfo))
                except OSError:
                    pass
            try:
                os.close(self.fd)
            except Exception:
                pass
            self.fd = -1


def render_error(fb, msg):
    """Red bar with white text — used on service-start failure."""
    img = Image.new("RGBA", (PANEL_W, PANEL_H), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, PANEL_H // 2 - 30, PANEL_W, PANEL_H // 2 + 30],
                   fill=(180, 30, 30))
    try:
        font = ImageFont.truetype(FONT_PATH, 28)
    except OSError:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), msg, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((PANEL_W - tw) // 2 - bbox[0],
               (PANEL_H - th) // 2 - bbox[1]),
              msg, fill=(255, 255, 255), font=font)
    fb.write_image(img)


def render_splash(fb):
    img = Image.open(SPLASH_PATH).convert("RGBA")
    fb.write_image(img)


def service_is_active():
    return subprocess.run(
        ["systemctl", "is-active", "--quiet", SERVICE]
    ).returncode == 0


def combo_pressed(dev):
    """Non-blocking check for Start+Select currently held on dev."""
    start_held = False
    select_held = False
    try:
        for event in dev.read():
            if event.type != evdev.ecodes.EV_KEY:
                continue
            if event.code == CODE_START:
                start_held = bool(event.value)
            elif event.code == CODE_SELECT:
                select_held = bool(event.value)
            if start_held and select_held:
                return True
    except BlockingIOError:
        pass
    return False


def main():
    # --- ensure service is running (BEFORE framebuffer so headless test works) ---
    subprocess.run(["systemctl", "start", SERVICE], check=False)
    time.sleep(1)
    service_ok = service_is_active()

    # --- init framebuffer + splash (all best-effort) ---
    fb = Framebuffer()
    try:
        fb.open()
    except OSError as exc:
        sys.stderr.write(f"warning: framebuffer open failed: {exc}\n")
        fb = None

    if fb is not None:
        try:
            if service_ok:
                render_splash(fb)
            else:
                render_error(fb, "BT service failed to start")
        except Exception as exc:
            sys.stderr.write(f"warning: splash render failed: {exc}\n")

    if not service_ok:
        sys.stderr.write("bt_gamepad failed to start; exiting in "
                         f"{ERROR_HOLD_SEC}s\n")
        # Poll for early-exit combo during the hold so the user isn't
        # forced to wait the full 5s with a stuck error overlay.
        try:
            err_dev = evdev.InputDevice(INPUT_DEV)
        except OSError:
            err_dev = None
        deadline = time.monotonic() + ERROR_HOLD_SEC
        while time.monotonic() < deadline:
            if err_dev is not None and combo_pressed(err_dev):
                break
            time.sleep(0.05)
        if err_dev is not None:
            err_dev.close()
        subprocess.run(["systemctl", "stop", SERVICE], check=False)
        if fb is not None:
            fb.close()
        sys.exit(1)

    # --- watch for Start+Select combo ---
    try:
        dev = evdev.InputDevice(INPUT_DEV)
    except OSError as exc:
        sys.stderr.write(f"fatal: cannot open {INPUT_DEV}: {exc}\n")
        subprocess.run(["systemctl", "stop", SERVICE], check=False)
        if fb is not None:
            fb.close()
        sys.exit(1)

    start_held = False
    select_held = False
    try:
        for event in dev.read_loop():
            if event.type != evdev.ecodes.EV_KEY:
                continue
            if event.code == CODE_START:
                start_held = bool(event.value)
            elif event.code == CODE_SELECT:
                select_held = bool(event.value)
            if start_held and select_held:
                break
    finally:
        subprocess.run(["systemctl", "stop", SERVICE], check=False)
        if fb is not None:
            fb.close()
    sys.exit(0)


if __name__ == "__main__":
    main()
