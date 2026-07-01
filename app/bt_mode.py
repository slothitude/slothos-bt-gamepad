#!/usr/bin/env python3
"""SlothOS Controller — Bluetooth Mode splash.

Displays the splash image fullscreen on /dev/fb0 via pygame+fbcon,
ensures bt_gamepad.service is running, and watches /dev/input/event1
for the Start+Select combo. On combo, stops the service and exits.

Multiple readers of /dev/input/event1 coexist (bt_gamepad's
evdev_reader does not EVIOCGRAB), so button events still reach the
BT stack and the paired host while the splash is up.
"""
import os
import sys
import subprocess
import time

import evdev
import pygame

SPLASH_PATH = "/usr/local/slothos/bt_mode/splash.png"
INPUT_DEV = "/dev/input/event1"
SERVICE = "bt_gamepad"
PANEL_W, PANEL_H = 640, 480
ERROR_HOLD_SEC = 5

# --- init fbdev SDL backend BEFORE pygame.init ---
os.environ.setdefault("SDL_VIDEODRIVER", "fbcon")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("SDL_NOMOUSE", "1")
os.environ.setdefault("SDL_NOKEYBOARD", "1")


def render_error(screen, msg):
    """Draw a simple red bar with white text — used on service-start failure."""
    screen.fill((0, 0, 0))
    bar = pygame.Surface((PANEL_W, 60))
    bar.fill((180, 30, 30))
    screen.blit(bar, (0, PANEL_H // 2 - 30))
    try:
        font = pygame.font.Font(None, 28)
        text = font.render(msg, True, (255, 255, 255))
        screen.blit(text, text.get_rect(center=(PANEL_W // 2, PANEL_H // 2)))
    except Exception:
        pass
    pygame.display.flip()


def service_is_active():
    return subprocess.run(
        ["systemctl", "is-active", "--quiet", SERVICE]
    ).returncode == 0


def snapshot_fb_mode():
    """Snapshot the current fb0 mode string so we can re-apply it on exit.

    SDL's fbcon driver normally restores mode on pygame.quit(), but this
    is a belt-and-braces guard for the dmenu handoff. Returns None on
    failure (we then skip the re-apply).
    """
    try:
        out = subprocess.run(
            ["fbset", "-fb", "/dev/fb0"],
            check=False, capture_output=True, text=True,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout
    except Exception:
        pass
    return None


def restore_fb_mode(snapshot):
    if snapshot is None:
        return
    try:
        subprocess.run(
            ["fbset", "-fb", "/dev/fb0"],
            input=snapshot, check=False, text=True,
        )
    except Exception:
        pass


def combo_pressed(dev):
    """Non-blocking check for Start+Select currently held on dev."""
    start_held = False
    select_held = False
    try:
        for event in dev.read():
            if event.type != evdev.ecodes.EV_KEY:
                continue
            if event.code == evdev.ecodes.BTN_START:
                start_held = bool(event.value)
            elif event.code == evdev.ecodes.BTN_SELECT:
                select_held = bool(event.value)
            if start_held and select_held:
                return True
    except BlockingIOError:
        pass
    return False


def main():
    # --- ensure service is running (BEFORE pygame so headless test works) ---
    subprocess.run(["systemctl", "start", SERVICE], check=False)
    time.sleep(1)
    service_ok = service_is_active()

    # --- init pygame + splash (all of this is best-effort) ---
    screen = None
    fb_mode_snapshot = snapshot_fb_mode()
    try:
        pygame.init()
        pygame.mouse.set_visible(False)
        screen = pygame.display.set_mode((PANEL_W, PANEL_H))
        splash = pygame.image.load(SPLASH_PATH).convert()
        screen.blit(splash, (0, 0))
        pygame.display.flip()
    except Exception as exc:
        # No panel / no splash (e.g. launched over SSH with no tty) — keep
        # running so input loop + service management still work.
        sys.stderr.write(f"warning: splash render failed: {exc}\n")
        screen = None

    if not service_ok:
        if screen is not None:
            render_error(screen, "BT service failed to start")
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
        pygame.quit()
        restore_fb_mode(fb_mode_snapshot)
        sys.exit(1)

    # --- watch for Start+Select combo ---
    try:
        dev = evdev.InputDevice(INPUT_DEV)
    except OSError as exc:
        sys.stderr.write(f"fatal: cannot open {INPUT_DEV}: {exc}\n")
        subprocess.run(["systemctl", "stop", SERVICE], check=False)
        pygame.quit()
        restore_fb_mode(fb_mode_snapshot)
        sys.exit(1)

    start_held = False
    select_held = False
    try:
        for event in dev.read_loop():
            if event.type != evdev.ecodes.EV_KEY:
                continue
            if event.code == evdev.ecodes.BTN_START:
                start_held = bool(event.value)
            elif event.code == evdev.ecodes.BTN_SELECT:
                select_held = bool(event.value)
            if start_held and select_held:
                break
    finally:
        subprocess.run(["systemctl", "stop", SERVICE], check=False)
        pygame.quit()
        restore_fb_mode(fb_mode_snapshot)
    sys.exit(0)


if __name__ == "__main__":
    main()
