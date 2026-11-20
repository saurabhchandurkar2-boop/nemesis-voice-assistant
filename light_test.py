"""Light controller test - run this BEFORE using voice commands.   python light_test.py
Close Nemesis and the Arduino IDE Serial Monitor first (only one program can use the USB port).
It finds the Arduino, checks nemesis_light.ino is on it, works out the relay polarity with you,
and flashes the light 3 times. The light always ends OFF."""
import sys
import time
import home


def ask(q):
    try:
        return input(q + " [y/n] ").strip().lower().startswith("y")
    except (EOFError, KeyboardInterrupt):
        return False


def main():
    print("NEMESIS light test\n")
    L = home.Light(reset_wait=2.0)
    print("1. Looking for the Arduino (takes a few seconds)...")
    msg = L.connect()
    print("  ", msg)
    if not L.connected:
        print("\nFix that first, then run this again. Tips:\n  - USB cable plugged in?  - nemesis_light.ino uploaded?  - Serial Monitor closed?")
        print("  - Several boards? Put the port in home.json, for example  \"port\": \"COM5\"")
        return 1
    try:
        print("\n2. Relay check. Watch the RED LED on the relay board.")
        L.set(True)
        print("   The relay is now switched ON by software.")
        if ask("   Is the RED LED on the relay lit and did the relay click?"):
            print("   Good. Polarity is correct.")
        else:
            pol = L.flip_polarity()
            print(f"   Reversed the relay logic to {pol} and saved it in home.json.")
            L.set(True)
            if not ask("   Now is the RED LED lit?"):
                L.set(False)
                print("\n   Still no. Check: relay VCC -> Uno 5V, GND -> GND, IN -> pin 7. Then run this again.")
                return 2
        print("\n3. Flashing 3 times (2 seconds on, 2 seconds off)...")
        for i in range(3):
            L.set(True); print(f"   {i + 1}: ON");  time.sleep(2)
            L.set(False); print(f"   {i + 1}: OFF"); time.sleep(2)
        print("\nAll good. The light is OFF. Start Nemesis and say: turn on the lights.")
        return 0
    finally:
        L.shutdown()


if __name__ == "__main__":
    sys.exit(main())
