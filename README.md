# BlueDesk

A BLE-based controller to automate sit/stand cycles on a motorized desk.
An ESP32-C3 inside the original control panel simulates the press of preset
buttons; a Linux PC drives it over Bluetooth, alternating between the "sit"
and "stand" presets every hour of **active work** (idle time and breaks
don't count).

Originally developed for a FlexiSpot desk with an STM32F103-based controller,
but the approach generalizes to any motorized desk that exposes momentary
push buttons (especially preset buttons that recall stored heights).

Licensed under GPL-3.0.

---

## Functional requirements

### Core behavior
- The PC sends BLE commands to the ESP32, which simulates presses on the
  preset buttons of the desk panel.
- The buttons are **stored-height presets**, not hold-to-move SU/STAND
  buttons: a short click (~300ms) is enough to make the desk reach the
  saved height.
- The desk has 6 buttons; only 2 are needed for this use case (sit preset
  and stand preset). The hardware supports all 6.
- The controller alternates between presets every `WORK_INTERVAL` of
  **actively worked time** (default: 1 hour).

### Time tracking
- Breaks are not counted. When the user steps away or locks the screen, the
  timer pauses.
- When the user becomes active again, the timer resumes from where it left off.
- If the ESP is unreachable for longer than one full cycle (`WORK_INTERVAL`),
  the timer is reset on reconnection (typical scenario: laptop carried to
  the office and back home).
- State (next preset to click, last click timestamp) survives script and
  system restarts.

### Safety and robustness
- If the BLE connection drops while a button is being "held", the ESP
  releases automatically within a few seconds (so the desk can't keep moving
  unattended).
- If the ESP loses power or resets, it must not generate phantom presses.
- No galvanic isolation is required (GND is shared with the desk controller).

### Operations
- The service must start automatically on KDE login.
- It must self-restart on crash.
- It must handle: PC suspend/resume, ESP appearing/disappearing, stale
  BlueZ cache, system reboots.

---

## Architecture

```
┌────────────────────┐         BLE          ┌──────────────────┐
│  Linux PC (KDE)    │  ◄────────────────►  │  ESP32-C3 Super- │
│  bluedesk_client.py│                      │  Mini (Arduino   │
│  (systemd user)    │                      │  firmware)       │
└─────────┬──────────┘                      └────────┬─────────┘
          │                                          │ GPIO
          │ D-Bus (lock screen)                      │ + 1kΩ R
          ▼                                          ▼
┌────────────────────┐                      ┌──────────────────┐
│ org.freedesktop.   │                      │ STM32F103 (desk  │
│ ScreenSaver        │                      │ controller with  │
└────────────────────┘                      │ stored presets)  │
                                            └──────────────────┘
```

---

## Hardware

### Components
- **ESP32-C3 SuperMini** (Bluetooth 5 LE + Wi-Fi, 22.5 × 18 mm, fits inside
  the original control-panel enclosure).
- **Buck converter** stepping the desk's 29V DC down to 5V to power the ESP.
- **6× 1kΩ resistors** in series between ESP32 GPIOs and the panel button pins.

### Wiring
- Power: 29V → buck → 5V → Supermini's VBUS pin.
- GND: shared between ESP and desk controller (no galvanic isolation).
- Signals: each ESP GPIO connects to the corresponding button pin on the
  original panel through a series 1kΩ resistor. The original panel already
  has a 1kΩ + RC debounce filter between the button pin and the STM32 GPIO,
  which is left untouched.
- Resulting topology per channel:

```
    STM32 GPIO ── R_orig (1kΩ) ──┬── button ─── GND
                                 │
                                 │  C_debounce (on STM32 GPIO, already there)
                                 │
                                 R_added (1kΩ)
                                 │
                                 ESP32-C3 GPIO
```

### GPIOs used on the C3
- `GPIO 0..5` → 6 button channels (only 0 and 1 actually wired to SIT/STAND
  presets in this setup).
- `GPIO 8` → onboard blue status LED.
- Pins to avoid: 2/8/9 (strapping), 18/19 (USB), 20/21 (UART).

### Hardware design choices
- **No optocouplers**: without useful galvanic isolation (GND is shared),
  the opto would add no protection — only bulk. Internal space is very tight.
- **No transistors/MOSFETs**: same reason (space). The trade-off accepted:
  the ESP drives the button pin directly via tri-stating.
- **Tri-stating instead of active drive**: the GPIO sits in INPUT (high
  impedance) at rest, and goes to OUTPUT_LOW only during the "press". The
  ESP never forces HIGH on the STM32 pin. Combined with the series resistor,
  this prevents bus contention even in the face of software bugs.
- **1kΩ series resistor**: limits current to a few mA in any abnormal
  scenario (bus contention, ESP boot transients). The panel's original
  resistor is another 1kΩ in series on the STM32 side, for a total of 2kΩ.

---

## Firmware (Arduino, ESP32-C3)

File: `bluedesk.ino`

### BLE GATT protocol

Custom UUIDs (modeled after Nordic UART for ease of development):
- Service: `6e400001-b5a3-f393-e0a9-e50e24dcca9e`
- CMD characteristic (`6e400002-...`): WRITE + WRITE_NR
  - Payload = 2 bytes: `[button_id, duration_100ms]`
  - `duration_100ms = 0` → release that button immediately
- STATE characteristic (`6e400003-...`): READ + NOTIFY
  - Payload = 1 byte bitmask, bit i = button i is currently "pressed"

Advertised BLE name: `DeskCtrl-C3`.

### Firmware safety
- **Auto-release**: each "press" has an expiration timestamp; the main loop
  releases the GPIO automatically when it elapses.
- **MAX_PRESS_MS = 15000ms**: hard cap on any press duration, even if the
  client requests longer. Truncated silently.
- **Disconnect = release_all()**: the BLE disconnect callback puts every
  pin back to INPUT.
- **Clean boot**: the very first thing in `setup()` is `pinMode(INPUT)`
  on all button pins, before Serial and BLE init. No glitches.

### Status LED (GPIO 8, active low)
- Slow blink (500ms) → advertising, waiting for a client
- Solid on → connected to a client
- Fast blink (100ms) → at least one button currently active ("pressing")

---

## Python script (Linux PC)

File: `bluedesk_client.py`

Dependencies: `bleak >= 3.0`, `dbus-fast`. Installed in a local venv
(`./.venv/bin/python`).

### Main components

#### `LockMonitor`
Watches the screen-lock state via D-Bus (`org.freedesktop.ScreenSaver`).
Compatible with both X11 and Wayland (tested on KDE Plasma 6 + Wayland).
- `GetActive()` for initial state
- `ActiveChanged(bool)` signal for real-time updates
- Approximation: "screen locked" → user away. Granularity ~5 minutes
  (KDE auto-lock timeout).
- **Why not fine-grained idle time?** `GetSessionIdleTime` is deprecated on
  Wayland for security/sandbox reasons.

#### `TimerState`
Timer state, persistent across BLE sessions and script restarts.

In memory:
- `active_seconds`: time accumulated toward the next preset switch
- `next_preset`: which preset to click on the next trigger
- `last_seen_mono`: monotonic time of last ESP connection

On disk (`~/.cache/bluedesk/state.json`):
- `next_preset`
- `last_click_unix`: UNIX timestamp of last actual click

Startup load logic:
- No file → defaults
- File present → recompute `active_seconds = now - last_click_unix` if
  `< WORK_INTERVAL`, otherwise reset to 0
- Corrupt file / clock moved backwards → conservative reset

Post-absence reset logic:
- When the ESP reconnects after being seen, the absence interval is computed
  using `time.monotonic()`
- If absence > `RESET_AFTER_ABSENCE_SEC` (= `WORK_INTERVAL`) →
  `active_seconds` reset, but **not** `next_preset` (the "turn" is preserved)

Saving:
- Only after each actual click (`reset_after_press`), not on every tick
- Atomic write via tmp file + rename

#### Main loop
- `session()`: scan, connect, run `control_loop`, handle cleanup
- `control_loop`: every `TICK_SEC`, check `LockMonitor.is_active()`, advance
  `TimerState`, send a click when it's due
- `main()`: infinite `session()` loop with exponential backoff on scan
  (3s → 10s → 30s → 60s → 120s) when the ESP isn't found

### Edge cases handled
| Scenario | Behavior |
|---|---|
| ESP disconnected for 30s | Timer keeps `active_seconds`, resumes on reconnect |
| ESP absent 8 hours (office) | Reset `active_seconds = 0` on return; `next_preset` preserved |
| PC suspended for 1 hour | Timer frozen during suspend, resumes on wake |
| PC suspended overnight | Resume → BLE reconnects → `absence > WORK_INTERVAL` → reset |
| Crash + restart | `load()` recomputes `active_seconds` from `last_click_unix` |
| System reboot | Same as crash + restart |
| Stale BlueZ cache | Manually resolved with `bluetoothctl remove` (see setup) |

### Configurable parameters
```python
WORK_INTERVAL = 3600              # 1 hour of work between position changes
TICK_SEC = 30                     # lock-screen sampling period
PRESS_DURATION_100MS = 3          # 300ms click (short preset press)
RESET_AFTER_ABSENCE_SEC = WORK_INTERVAL
SCAN_BACKOFF_SEC = [3, 10, 30, 60, 120]
```

---

## Deployment: systemd user service

File: `bluedesk.service`

Installed in `~/.config/systemd/user/bluedesk.service`.

Properties:
- `Type=simple`, runs as the logged-in user.
- `Restart=always` with `RestartSec=5`: self-heals from crashes.
- `StartLimitBurst=10` over 60s (in `[Unit]` section): protects against
  crash loops.
- `WantedBy=graphical-session.target`: starts/stops with the KDE session.
  No `linger` needed (we want it running only when you're logged in).
- stdout/stderr → journal: viewable with `journalctl --user -u bluedesk`.

### Useful commands

```bash
# One-time installation
mkdir -p ~/.config/systemd/user
ln -s bluedesk.service ~/.config/systemd/user/bluedesk.service
systemctl --user daemon-reload
systemctl --user enable --now bluedesk.service

# Day-to-day
systemctl --user status  bluedesk.service
systemctl --user restart bluedesk.service
systemctl --user stop    bluedesk.service
journalctl --user -u bluedesk.service -f          # live tail
journalctl --user -u bluedesk.service --since "1 hour ago"
```

---

## Initial setup (full procedure)

1. Flash the firmware on the ESP32-C3 (Arduino IDE, board "ESP32C3 Dev Module").
2. Test the firmware with a phone app (BLE Scanner) or ToolBLEx on PC:
   writing `00 03` to the CMD characteristic should click button 0 for 300ms.
3. Hardware wiring (1kΩ series resistors on the 6 GPIOs).
4. On PC: create a venv, `pip install bleak dbus-fast`.
5. **Update `DEVICE_ADDRESS` in the script with your ESP's MAC.**
6. Manual script test: `python bluedesk_client.py`.
7. If BLE misbehaves on Linux: `bluetoothctl remove <MAC>` to clear the cache.
8. Install the systemd user service and enable it on login.

---

## Possible future enhancements

- Desktop notification on position change (`notify-send`).
- Manual override via KDE shortcut (publish a D-Bus topic the script
  subscribes to, or expose a local HTTP API).
- Daily statistics (how many times I switched to standing today, log to file).
- Finer idle detection via `org.kde.KIdleTime` (registration-based, with
  `timeoutReached`/`resumingFromIdle` signals) to reduce the ~5-minute
  margin of the lock-screen approach.
- Multi-user: multiple PCs taking turns driving the same desk (the firmware
  already supports this — it serves one BLE client at a time, sequentially).

---

## Project files

| File | Description |
|---|---|
| `bluedesk.ino` | Arduino firmware for ESP32-C3 |
| `bluedesk_client.py` | Python client running on the PC |
| `bluedesk.service` | systemd user unit file |
| `README.md` | This document |

---

## License

GPL-3.0. See `LICENSE` for the full text.
