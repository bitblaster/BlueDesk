# SPDX-License-Identifier: GPL-3.0-or-later
"""
BlueDesk client — drives the ESP32-C3 firmware to simulate desk button presses.

Switches preset (sit/stand) every WORK_INTERVAL seconds of **active work time**
(no idle, no locked screen). Breaks don't count.

Firmware protocol:
  CMD (write):  2 bytes [button_id, duration_100ms]   duration=0 -> release
  STATE notify: 1 byte bitmask
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from bleak import BleakClient, BleakScanner
from dbus_fast.aio import MessageBus
from dbus_fast import BusType

# --- CONFIGURATION ---
DEVICE_ADDRESS   = "F0:F5:BD:FC:09:96"
DEVICE_NAME      = "BlueDesk"             # for logs only, not used for discovery

SERVICE_UUID    = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
CHAR_CMD_UUID   = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
CHAR_STATE_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

BTN_PRESET_STAND = 0    # gpio0 -> "stand" preset
BTN_PRESET_SIT   = 1    # gpio1 -> "sit" preset

# Press duration: 300ms = short click on a preset button
PRESS_DURATION_100MS = 3

# "Active work" time between two preset switches
WORK_INTERVAL = 3600           # 1 hour

# Sampling period.
TICK_SEC = 30

# Periodic status log (every N ticks)
STATUS_LOG_EVERY = 10          # every 5 minutes

# If the ESP is absent for more than this, reset the timer on reconnect.
# Intentionally == WORK_INTERVAL: if a full cycle has passed without seeing it,
# it means I was at the office / unplugged, restart fresh.
RESET_AFTER_ABSENCE_SEC = WORK_INTERVAL

# --- BLE timing ---
SCAN_TIMEOUT       = 10.0
CONNECT_TIMEOUT    = 15.0
DISCONNECT_TIMEOUT = 5.0

# Scan backoff: increasing intervals when the ESP isn't found.
# Resets back to the first value as soon as it's found.
SCAN_BACKOFF_SEC = [3, 10, 30, 60, 120]

# Path of the persistent state file (survives script restarts).
# We store: next preset to click, timestamp of the last click, and the
# predicted next-switch time used by the desktop widget.
STATE_DIR  = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "bluedesk"
STATE_FILE = STATE_DIR / "state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


# ============================================================
# Timer state (persists across BLE sessions and restarts)
# ============================================================
class TimerState:
    """Persistent state.

    In memory:
      - active_seconds: time accumulated toward the next preset switch
      - next_preset:    which preset to click on the next trigger
      - last_seen_mono: monotonic time of last ESP connection (in-memory only,
                        used for post-absence reset within a single run)
      - _was_active:    last tick activity flag, for break-end edge detection

    Persisted on disk in state.json (survives script restarts):
      - next_preset:        so we know whether SIT or STAND is next at startup
      - next_preset_label:  human-readable label, for the desktop widget
      - last_click_unix:    UNIX timestamp of the last actual click; used at
                            startup to estimate active_seconds
      - next_switch_unix:   predicted UNIX time of the next preset switch,
                            assuming the user stays active. The desktop widget
                            reads this and computes "time remaining".
    """

    def __init__(self):
        self.active_seconds = 0
        self.next_preset = BTN_PRESET_STAND
        self.last_seen_mono = None
        self.last_click_unix = None       # None = never clicked
        self._was_active = True           # last tick activity, for edge detection

    # ---------- on-disk persistence ----------

    def save(self):
        """Write the full state to disk. Call after every event that changes
        either the persistent fields (next_preset, last_click_unix) or the
        prediction (next_switch_unix)."""
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            preset_label = (
                "SIT" if self.next_preset == BTN_PRESET_SIT else "STAND"
            )
            seconds_left = max(0, WORK_INTERVAL - self.active_seconds)
            data = {
                "next_preset": self.next_preset,
                "next_preset_label": preset_label,
                "last_click_unix": self.last_click_unix,
                "next_switch_unix": time.time() + seconds_left,
                "updated_unix": time.time(),
            }
            # Atomic write: write to tmp file, then rename
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(STATE_FILE)
        except Exception as e:
            logging.warning(f"Could not save state: {e}")

    def load(self):
        """Load state from disk if it exists, and compute initial active_seconds."""
        if not STATE_FILE.exists():
            logging.info("No previous state, starting from zero.")
            self.save()
            return

        try:
            data = json.loads(STATE_FILE.read_text())
            self.next_preset = data.get("next_preset", BTN_PRESET_STAND)
            self.last_click_unix = data.get("last_click_unix")

            label = "SIT" if self.next_preset == BTN_PRESET_SIT else "STAND"
            logging.info(f"State loaded: next preset = {label}")

            # Compute time elapsed since the last click (if any)
            if self.last_click_unix is not None:
                elapsed = time.time() - self.last_click_unix
                if elapsed < 0:
                    # System clock moved backwards? Be conservative.
                    logging.warning(
                        f"last_click_unix is in the future ({elapsed:.0f}s), starting from 0."
                    )
                    self.active_seconds = 0
                elif elapsed >= WORK_INTERVAL:
                    logging.info(
                        f"Last click {int(elapsed)}s ago (>={WORK_INTERVAL}s): "
                        f"starting from 0."
                    )
                    self.active_seconds = 0
                else:
                    self.active_seconds = int(elapsed)
                    logging.info(
                        f"Last click {int(elapsed)}s ago: "
                        f"resuming with {self.active_seconds}s accumulated."
                    )
        except Exception as e:
            logging.warning(f"Error loading state: {e}. Starting from zero.")

        self.save()

    # ---------- BLE events ----------

    def on_connected(self):
        """Call right after the ESP connects."""
        now = time.monotonic()
        if self.last_seen_mono is not None:
            absence = now - self.last_seen_mono
            if absence > RESET_AFTER_ABSENCE_SEC:
                logging.info(
                    f"ESP absent for {int(absence)}s "
                    f"(>{RESET_AFTER_ABSENCE_SEC}s): resetting timer."
                )
                self.active_seconds = 0
                # Timer was reset -> the predicted switch time moved forward
                self.save()
            else:
                logging.info(
                    f"ESP back after {int(absence)}s: "
                    f"resuming from {self.active_seconds}s."
                )
                # No reset: prediction is unchanged, no need to rewrite state
        self.last_seen_mono = now

    def on_disconnected(self):
        """Call when the ESP disconnects."""
        self.last_seen_mono = time.monotonic()

    # ---------- timer cycle ----------

    def tick(self, is_active: bool):
        # Detect break -> active edge: that's the moment when the predicted
        # switch time has visibly slipped (by the duration of the break) and
        # the widget must be refreshed. During the break itself the widget's
        # countdown stays "stale" but is actually correct: real wall-clock
        # time passing equals the time the deadline must slip by.
        if is_active and not self._was_active:
            self.save()

        self._was_active = is_active

        if is_active:
            self.active_seconds += TICK_SEC

    def time_to_press(self) -> bool:
        return self.active_seconds >= WORK_INTERVAL

    def reset_after_press(self):
        """Call AFTER a successful click."""
        self.active_seconds = 0
        self.next_preset = (
            BTN_PRESET_SIT if self.next_preset == BTN_PRESET_STAND
            else BTN_PRESET_STAND
        )
        self.last_click_unix = time.time()
        self.save()


# ============================================================
# Lock-screen monitor via D-Bus (compatible with X11/Wayland/Plasma 6)
# ============================================================
class LockMonitor:
    """Tracks whether the screen is locked.

    Uses the org.freedesktop.ScreenSaver interface:
      - GetActive() returns True if the screen is locked
      - ActiveChanged(bool) is emitted on every change

    Unlike GetSessionIdleTime (deprecated on Wayland), this works on all
    modern compositors.
    """

    def __init__(self):
        self._bus = None
        self._iface = None
        self._locked = False

    async def connect(self):
        self._bus = await MessageBus(bus_type=BusType.SESSION).connect()
        introspection = await self._bus.introspect(
            "org.freedesktop.ScreenSaver", "/ScreenSaver"
        )
        proxy = self._bus.get_proxy_object(
            "org.freedesktop.ScreenSaver", "/ScreenSaver", introspection
        )
        self._iface = proxy.get_interface("org.freedesktop.ScreenSaver")

        # Initial state
        try:
            self._locked = await self._iface.call_get_active()
        except Exception as e:
            logging.warning(f"GetActive not available: {e}. Assuming unlocked.")
            self._locked = False

        # Subscribe to state changes
        try:
            self._iface.on_active_changed(self._on_active_changed)
        except Exception as e:
            logging.warning(f"Could not subscribe to ActiveChanged: {e}")

    def _on_active_changed(self, active: bool):
        self._locked = active
        logging.info(f"Lock screen: {'LOCKED' if active else 'unlocked'}")

    def is_locked(self) -> bool:
        return self._locked

    def is_active(self) -> bool:
        """The user is considered 'active' if the screen is NOT locked."""
        return not self._locked


# ============================================================
# BLE
# ============================================================
def on_state_notify(_sender, data: bytearray) -> None:
    if len(data) >= 1:
        mask = data[0]
        active = [i for i in range(8) if mask & (1 << i)]
        if active:
            logging.info(f"ESP state: active buttons = {active}")
        else:
            logging.info("ESP state: all released")


async def send_press(client: BleakClient, button_id: int, duration_100ms: int) -> None:
    payload = bytes([button_id & 0xFF, duration_100ms & 0xFF])
    if button_id == BTN_PRESET_SIT:
        label = "PRESET_SIT"
    elif button_id == BTN_PRESET_STAND:
        label = "PRESET_STAND"
    else:
        label = f"btn{button_id}"
    logging.info(f"-> Click {label} (id={button_id}) for {duration_100ms*100}ms")
    try:
        await client.write_gatt_char(CHAR_CMD_UUID, payload, response=True)
    except Exception as e:
        logging.warning(f"Write with response failed ({e}), trying without response.")
        await client.write_gatt_char(CHAR_CMD_UUID, payload, response=False)


async def find_device():
    """Search by MAC. No name fallback (could match unrelated devices)."""
    return await BleakScanner.find_device_by_address(
        DEVICE_ADDRESS, timeout=SCAN_TIMEOUT
    )


# ============================================================
# Main loop: active-time tracking
# ============================================================
async def control_loop(
    client: BleakClient,
    monitor: "LockMonitor",
    timer: TimerState,
    disconnect_event: asyncio.Event,
) -> None:
    tick_count = 0

    await asyncio.sleep(0.5)
    try:
        await asyncio.wait_for(
            client.start_notify(CHAR_STATE_UUID, on_state_notify),
            timeout=5.0,
        )
        logging.info("State notifications enabled.")
    except Exception as e:
        logging.warning(f"Notifications not available: {e}")

    logging.info(
        f"Tracking started. Switching preset every {WORK_INTERVAL}s of "
        f"active time. Current state: {timer.active_seconds}s accumulated, "
        f"next preset = id {timer.next_preset}."
    )

    while not disconnect_event.is_set():
        is_active = monitor.is_active()
        timer.tick(is_active)

        tick_count += 1
        if tick_count % STATUS_LOG_EVERY == 0:
            remaining = WORK_INTERVAL - timer.active_seconds
            state = "ACTIVE" if is_active else "LOCKED"
            logging.info(
                f"[{state}] active time: {timer.active_seconds}s / "
                f"{WORK_INTERVAL}s ({remaining}s to go)"
            )

        if timer.time_to_press():
            try:
                await send_press(client, timer.next_preset, PRESS_DURATION_100MS)
                timer.reset_after_press()
            except Exception as e:
                logging.error(f"Error sending command: {e}")
                return

        try:
            await asyncio.wait_for(
                disconnect_event.wait(), timeout=TICK_SEC
            )
            return
        except asyncio.TimeoutError:
            pass


async def session(monitor: LockMonitor, timer: TimerState) -> bool:
    """One full session: scan -> connect -> control_loop -> cleanup.

    Returns True if the ESP was found (so we were connected at least briefly),
    False if not found. Used by main() to manage backoff.
    """
    device = await find_device()
    if device is None:
        return False

    logging.info(f"Found {device.name or DEVICE_NAME} ({device.address}), connecting...")

    disconnect_event = asyncio.Event()

    def on_disconnect(_client):
        logging.warning("Callback: device disconnected.")
        disconnect_event.set()

    client = BleakClient(device, disconnected_callback=on_disconnect)

    try:
        await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)
        logging.info("Connected.")
        timer.on_connected()
        await control_loop(client, monitor, timer, disconnect_event)
    except asyncio.TimeoutError:
        logging.error("Connection timeout.")
    except Exception as e:
        logging.error(f"Session error: {e}")
    finally:
        if client.is_connected:
            try:
                await asyncio.wait_for(
                    client.disconnect(), timeout=DISCONNECT_TIMEOUT
                )
            except Exception as e:
                logging.warning(f"Disconnect error: {e}")
        timer.on_disconnected()
        logging.info("Session closed.")

    return True


async def main() -> None:
    monitor = LockMonitor()
    try:
        await monitor.connect()
        logging.info(
            f"Lock monitor connected. Initial state: "
            f"{'LOCKED' if monitor.is_locked() else 'unlocked'}"
        )
    except Exception as e:
        logging.error(f"Could not connect to D-Bus: {e}")
        return

    timer = TimerState()
    timer.load()
    backoff_idx = 0   # index into SCAN_BACKOFF_SEC

    while True:
        found = await session(monitor, timer)

        if found:
            # Found and then disconnected: next attempt right away (reset backoff)
            backoff_idx = 0
            sleep_sec = SCAN_BACKOFF_SEC[0]
        else:
            # Not found: use backoff and advance for next time
            sleep_sec = SCAN_BACKOFF_SEC[backoff_idx]
            logging.info(
                f"ESP not found. Next scan in {sleep_sec}s."
            )
            if backoff_idx < len(SCAN_BACKOFF_SEC) - 1:
                backoff_idx += 1

        await asyncio.sleep(sleep_sec)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Terminated by user.")
