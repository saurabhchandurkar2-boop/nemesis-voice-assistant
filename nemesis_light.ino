// NEMESIS light controller for Arduino Uno  (v1)
// Relay module: VCC -> 5V, GND -> GND, IN -> pin 7.   USB cable to the PC.
// Upload with the Arduino IDE (Tools > Board: Arduino Uno), then close the IDE's Serial Monitor.
//
// Text commands over USB (9600 baud), one per line:
//   PING          -> PONG NEMESIS-LIGHT 1
//   ON / OFF      -> OK ON / OK OFF
//   STATUS        -> STATE ON|OFF POL HIGH|LOW
//   POL HIGH|LOW  -> OK POL ...   (HIGH: relay needs a HIGH signal to switch on. LOW: it needs a LOW signal.) Saved in EEPROM.
//   HB            -> HB           (heartbeat from the PC)
// Safety: the light is OFF at power-up and after every reset. If the PC says nothing for
// 20 seconds while the light is on, the Uno switches it off by itself ("WDOG OFF").

#include <EEPROM.h>

const uint8_t RELAY_PIN = 7;
const unsigned long WATCHDOG_MS = 20000UL;

bool activeLow = false;      // false: HIGH = relay on.  true: LOW = relay on.
bool lightOn = false;
unsigned long lastRx = 0;
char buf[24];
uint8_t n = 0;

uint8_t levelFor(bool on) { return (on != activeLow) ? HIGH : LOW; }
void apply() { digitalWrite(RELAY_PIN, levelFor(lightOn)); }

void setup() {
  if (EEPROM.read(0) == 0xA5) activeLow = (EEPROM.read(1) == 1);
  digitalWrite(RELAY_PIN, levelFor(false));   // set the OFF level BEFORE the pin becomes an output: no click at start-up
  pinMode(RELAY_PIN, OUTPUT);
  Serial.begin(9600);
  lastRx = millis();
  Serial.println(F("READY NEMESIS-LIGHT 1"));
}

void handle(char *c) {
  lastRx = millis();
  if (!strcmp(c, "PING"))        { Serial.println(F("PONG NEMESIS-LIGHT 1")); }
  else if (!strcmp(c, "HB"))     { Serial.println(F("HB")); }
  else if (!strcmp(c, "ON"))     { lightOn = true;  apply(); Serial.println(F("OK ON")); }
  else if (!strcmp(c, "OFF"))    { lightOn = false; apply(); Serial.println(F("OK OFF")); }
  else if (!strcmp(c, "STATUS")) { Serial.print(F("STATE ")); Serial.print(lightOn ? F("ON") : F("OFF"));
                                   Serial.print(F(" POL ")); Serial.println(activeLow ? F("LOW") : F("HIGH")); }
  else if (!strncmp(c, "POL ", 4)) {
    bool low = !strcmp(c + 4, "LOW");
    if (low || !strcmp(c + 4, "HIGH")) {
      lightOn = false; activeLow = low; apply();              // always end up OFF after a change
      EEPROM.update(0, 0xA5); EEPROM.update(1, low ? 1 : 0);
      Serial.print(F("OK POL ")); Serial.println(low ? F("LOW") : F("HIGH"));
    } else Serial.println(F("ERR POL"));
  }
  else Serial.println(F("ERR"));
}

void loop() {
  while (Serial.available()) {
    char ch = Serial.read();
    if (ch == '\r') continue;
    if (ch == '\n') { buf[n] = 0; if (n) handle(buf); n = 0; }
    else if (n < sizeof(buf) - 1) { buf[n++] = (ch >= 'a' && ch <= 'z') ? ch - 32 : ch; }
    else n = 0;                                               // too long: throw it away
  }
  if (lightOn && millis() - lastRx > WATCHDOG_MS) {           // the PC went quiet: switch off
    lightOn = false; apply(); Serial.println(F("WDOG OFF"));
  }
}
