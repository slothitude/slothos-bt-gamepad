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


def main():
    pygame.init()
    pygame.mouse.set_visible(False)
    try:
        screen = pygame.display.set_mode((PANEL_W, PANEL_H))
        splash = pygame.image.load(SPLASH_PATH).convert()
        screen.blit(splash, (0, 0))
        pygame.display.flip()
    except Exception as exc:
        # No panel / no splash — keep running so input loop + service
        # management still work (e.g. over SSH for testing).
        sys.stderr.write(f"warning: splash render failed: {exc}\n")
        screen = None

    # --- ensure service is running ---
    subprocess.run(["systemctl", "start", SERVICE], check=False)
    time.sleep(1)
    if not service_is_active():
        if screen is not None:
            render_error(screen, "BT service failed to start")
        sys.stderr.write("bt_gamepad failed to start; exiting in "
                         f"{ERROR_HOLD_SEC}s\n")
        time.sleep(ERROR_HOLD_SEC)
        pygame.quit()
        sys.exit(1)

    # --- watch for Start+Select combo ---
    try:
        dev = evdev.InputDevice(INPUT_DEV)
    except OSError as exc:
        sys.stderr.write(f"fatal: cannot open {INPUT_DEV}: {exc}\n")
        subprocess.run(["systemctl", "stop", SERVICE], check=False)
        pygame.quit()
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
    sys.exit(0)


if __name__ == "__main__":
    main()
