// SPDX-License-Identifier: GPL-3.0-or-later
/*
 * BlueDesk — ESP32-C3 firmware
 *
 * BLE control of 6 buttons of a motorized desk.
 * Command: "press button X for N ms".
 *
 * HARDWARE:
 *   ESP32-C3 GND and desk GND are shared (no galvanic isolation).
 *   Each C3 GPIO is connected to the corresponding STM32 button pin
 *   through a series resistor (1kΩ in this build, plus another 1kΩ
 *   already present on the original panel = 2kΩ total).
 *   Idle state is INPUT (high impedance): the STM32 doesn't see the C3.
 *   Pressing means OUTPUT LOW: pulls the STM32 pin to GND.
 *   The C3 NEVER drives the STM32 pin HIGH.
 *
 * GATT:
 *   Service:        custom UUID
 *   Characteristic: writable (Write / WriteNoResponse)
 *                   payload = 2 bytes: [button_id (0-5), duration_100ms (1-255)]
 *                   duration_100ms = 0 -> release immediately (stop)
 *   Characteristic: notify (button state, 1 byte bitmask)
 *
 * SAFETY:
 *   - automatic timeout for every button (no "infinite press")
 *   - BLE disconnect -> release all
 *   - max press duration capped at MAX_PRESS_MS
 *   - pinMode INPUT at boot before any other initialization
 */

#include <Arduino.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>

// ======== CONFIGURATION ========

// GPIOs connected to the STM32 button pins through a 1kΩ series resistor.
// On the C3 SuperMini, safe general-purpose pins are: 0, 1, 3, 4, 5, 6, 7, 10
// (and 20, 21 if needed). Avoid: GPIO 2/8/9 (strapping), 18/19 (USB).
const uint8_t BUTTON_PINS[6] = { 0, 1, 3, 4, 5, 6 };
const uint8_t NUM_BUTTONS = 6;

// SuperMini onboard status LED: GPIO 8, ACTIVE LOW.
// (LED turns on when the pin is LOW)
const uint8_t LED_PIN     = 8;
const uint8_t LED_ON      = LOW;
const uint8_t LED_OFF     = HIGH;

// Blink periods (ms)
const uint32_t LED_BLINK_ADVERTISING = 500;   // slow: waiting for connection
const uint32_t LED_BLINK_PRESSING    = 100;   // fast: at least one button active

// Maximum press duration (ms) — runaway protection
const uint32_t MAX_PRESS_MS = 15000;  // 15 seconds

// Drive mode: tri-stating through the series resistor.
//   PRESSED  -> GPIO in OUTPUT, LOW level (pulls the STM32 pin to GND)
//   RELEASED -> GPIO in INPUT (high-Z, the STM32 sees only its own pull-up)
// This scheme NEVER forces HIGH on the STM32 pin: in case of a software bug,
// the worst case is high impedance. The series resistor limits current in
// any abnormal scenario.

// UUIDs — generate your own at https://www.uuidgenerator.net/
#define SERVICE_UUID        "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
#define CHAR_CMD_UUID       "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
#define CHAR_STATE_UUID     "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

#define DEVICE_NAME         "DeskCtrl-C3"

// ======== INTERNAL STATE ========

struct ButtonState {
  bool      active;       // true if currently pressed
  uint32_t  releaseAtMs;  // millis() value at which to release
};

ButtonState buttons[6];

BLEServer*         pServer       = nullptr;
BLECharacteristic* pCharState    = nullptr;
bool               deviceConnected = false;
uint8_t            lastStateMask = 0;

// ======== HELPERS ========

void pressButton(uint8_t id, uint32_t durationMs) {
  if (id >= NUM_BUTTONS) return;
  if (durationMs > MAX_PRESS_MS) durationMs = MAX_PRESS_MS;

  // Output LOW: pulls the STM32 pin to GND through the series resistor
  digitalWrite(BUTTON_PINS[id], LOW);
  pinMode(BUTTON_PINS[id], OUTPUT);
  buttons[id].active      = true;
  buttons[id].releaseAtMs = millis() + durationMs;

  Serial.printf("PRESS  btn=%u for %lums\n", id, durationMs);
}

void releaseButton(uint8_t id) {
  if (id >= NUM_BUTTONS) return;
  // High-impedance input: the STM32 only sees its own pull-up again
  pinMode(BUTTON_PINS[id], INPUT);
  buttons[id].active      = false;
  buttons[id].releaseAtMs = 0;
  Serial.printf("RELEASE btn=%u\n", id);
}

void releaseAll() {
  for (uint8_t i = 0; i < NUM_BUTTONS; i++) releaseButton(i);
}

uint8_t computeStateMask() {
  uint8_t m = 0;
  for (uint8_t i = 0; i < NUM_BUTTONS; i++) {
    if (buttons[i].active) m |= (1 << i);
  }
  return m;
}

void updateStatusLed() {
  // Logic:
  //   - not connected      -> slow blink (advertising)
  //   - connected, idle    -> solid on
  //   - connected, pressing-> fast blink
  static uint32_t lastToggle = 0;
  static bool     ledState   = false;
  uint32_t now = millis();

  bool anyPressing = (computeStateMask() != 0);

  if (deviceConnected && !anyPressing) {
    // solid on
    digitalWrite(LED_PIN, LED_ON);
    ledState = true;
    return;
  }

  uint32_t period = anyPressing ? LED_BLINK_PRESSING : LED_BLINK_ADVERTISING;
  if (now - lastToggle >= period) {
    lastToggle = now;
    ledState = !ledState;
    digitalWrite(LED_PIN, ledState ? LED_ON : LED_OFF);
  }
}

void notifyStateIfChanged() {
  uint8_t m = computeStateMask();
  if (m != lastStateMask && deviceConnected && pCharState) {
    lastStateMask = m;
    pCharState->setValue(&m, 1);
    pCharState->notify();
  }
}

// ======== BLE CALLBACKS ========

class ServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer* s) override {
    deviceConnected = true;
    Serial.println("BLE: connected");
  }
  void onDisconnect(BLEServer* s) override {
    deviceConnected = false;
    Serial.println("BLE: disconnected -> release all");
    releaseAll();                  // safety: no buttons stuck pressed
    BLEDevice::startAdvertising(); // resume advertising
  }
};

class CmdCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic* c) override {
    String v = c->getValue();
    if (v.length() < 2) {
      Serial.println("CMD: payload too short (expected 2 bytes)");
      return;
    }
    uint8_t id   = (uint8_t)v[0];
    uint8_t dur  = (uint8_t)v[1];   // units of 100ms

	Serial.printf("Received id: %d, duration: %d\n", id, dur);
    if (id >= NUM_BUTTONS) {
      Serial.printf("CMD: id %u out of range\n", id);
      return;
    }

    if (dur == 0) {
      releaseButton(id);
    } else {
      pressButton(id, (uint32_t)dur * 100UL);
    }
  }
};

// ======== SETUP / LOOP ========

void setup() {
  // Button GPIOs: idle state = INPUT (high impedance).
  // Pre-set the latch to LOW so that when pressButton() calls pinMode(OUTPUT),
  // the pin goes LOW immediately without an intermediate HIGH glitch.
  for (uint8_t i = 0; i < NUM_BUTTONS; i++) {
    digitalWrite(BUTTON_PINS[i], LOW);
    pinMode(BUTTON_PINS[i], INPUT);
    buttons[i] = { false, 0 };
  }

  // Status LED (active LOW)
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LED_OFF);

  Serial.begin(115200);
  delay(200);
  Serial.println("\n=== BlueDesk ===");

  // BLE
  BLEDevice::init(DEVICE_NAME);
  pServer = BLEDevice::createServer();
  pServer->setCallbacks(new ServerCallbacks());

  BLEService* pService = pServer->createService(SERVICE_UUID);

  // Command characteristic (write + write no response)
  BLECharacteristic* pCharCmd = pService->createCharacteristic(
    CHAR_CMD_UUID,
    BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR
  );
  pCharCmd->setCallbacks(new CmdCallbacks());

  // State characteristic (notify)
  pCharState = pService->createCharacteristic(
    CHAR_STATE_UUID,
    BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_NOTIFY
  );
  pCharState->addDescriptor(new BLE2902());
  uint8_t zero = 0;
  pCharState->setValue(&zero, 1);

  pService->start();

  BLEAdvertising* pAdv = BLEDevice::getAdvertising();
  pAdv->addServiceUUID(SERVICE_UUID);
  pAdv->setScanResponse(true);
  BLEDevice::startAdvertising();

  Serial.println("BLE advertising...");
}

void loop() {
  uint32_t now = millis();

  // Auto-release: turn off buttons whose timer has expired
  for (uint8_t i = 0; i < NUM_BUTTONS; i++) {
    if (buttons[i].active && (int32_t)(now - buttons[i].releaseAtMs) >= 0) {
      releaseButton(i);
    }
  }

  notifyStateIfChanged();
  updateStatusLed();

  delay(5);
}
