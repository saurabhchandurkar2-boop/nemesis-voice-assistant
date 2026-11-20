"""NEMESIS light control: talks to an Arduino Uno running nemesis_light.ino (relay on pin 7).

Safe by design: the light is OFF at connect, on exit, on freeze, and the Uno switches it off itself
if the PC goes silent.  Every function returns a plain sentence Nemesis can speak.
"""
import json
import os
import re
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "home.json")
PROTOCOL = "NEMESIS-LIGHT"
DEFAULTS = {
    "port": "auto",           # "auto" or e.g. "COM5"
    "baud": 9600,
    "active_low": False,      # False: relay switches on with a HIGH signal.  True: with a LOW signal.
    "off_on_exit": True,      # switch the light off when Nemesis closes
    "auto_connect": True,
    "max_on_minutes": 0,      # 0 = no automatic switch-off
    "simulate": False,        # True = pretend Arduino, for testing without hardware
}
ARDUINO_VIDS = (0x2341, 0x2A03, 0x1A86, 0x0403, 0x10C4)     # Arduino, clones, CH340, FTDI, CP210x


class LightError(Exception):
    pass


def load_config(path=CONFIG_FILE):
    cfg = dict(DEFAULTS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(DEFAULTS, f, indent=2)
        except OSError:
            pass
    except Exception:
        pass
    return cfg


def save_config(cfg, path=CONFIG_FILE):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass


class SimArduino:
    """A pretend Uno that speaks the same text protocol (tests and simulate=true)."""

    def __init__(self, port="SIM", baud=9600, timeout=0.3, clock=time.time):
        self.clock, self.is_open = clock, True
        self.out = [b"READY NEMESIS-LIGHT 1\n"]
        self.active_low, self.on, self.last = False, False, clock()
        self.fail = False                       # set True to simulate a pulled USB cable

    def level(self):
        return 1 if self.on != self.active_low else 0

    def write(self, data):
        if self.fail:
            raise OSError("device disconnected")
        for c in data.decode().splitlines():
            c = c.strip().upper()
            if not c:
                continue
            self.last = self.clock()
            if c == "PING": self.out.append(b"PONG NEMESIS-LIGHT 1\n")
            elif c == "HB": self.out.append(b"HB\n")
            elif c == "ON": self.on = True; self.out.append(b"OK ON\n")
            elif c == "OFF": self.on = False; self.out.append(b"OK OFF\n")
            elif c == "STATUS": self.out.append(f"STATE {'ON' if self.on else 'OFF'} POL {'LOW' if self.active_low else 'HIGH'}\n".encode())
            elif c.startswith("POL "):
                v = c[4:]
                if v in ("LOW", "HIGH"):
                    self.on, self.active_low = False, v == "LOW"; self.out.append(f"OK POL {v}\n".encode())
                else: self.out.append(b"ERR POL\n")
            else: self.out.append(b"ERR\n")

    def readline(self):
        if self.fail:
            raise OSError("device disconnected")
        if self.on and self.clock() - self.last > 20:
            self.on = False; self.out.append(b"WDOG OFF\n")
        return self.out.pop(0) if self.out else b""

    def close(self):
        self.is_open = False


def default_ports():
    """Serial ports that could be an Arduino, best guesses first."""
    try:
        from serial.tools import list_ports
    except ImportError:
        raise LightError("I need the pyserial package. In PowerShell run:  pip install pyserial")
    found = []
    for p in list_ports.comports():
        text = f"{p.description} {p.manufacturer or ''}".lower()
        score = (2 if p.vid in ARDUINO_VIDS else 0) + (2 if re.search(r"arduino|ch340|ch341|usb-serial|usb serial|ftdi|cp210", text) else 0)
        found.append((score, {"device": p.device, "desc": p.description}))
    return [d for s, d in sorted(found, key=lambda x: -x[0]) if s > 0]


def default_opener(port, baud, timeout):
    try:
        import serial
    except ImportError:
        raise LightError("I need the pyserial package. In PowerShell run:  pip install pyserial")
    return serial.Serial(port, baud, timeout=timeout)


class Light:
    def __init__(self, opener=None, ports_fn=None, config_file=CONFIG_FILE, on_change=None, announce=None, reset_wait=2.0):
        self.config_file = config_file
        self.cfg = load_config(config_file)
        sim = bool(self.cfg.get("simulate"))
        self.opener = opener or (SimArduino if sim else default_opener)
        self.ports_fn = ports_fn or ((lambda: [{"device": "SIM", "desc": "simulated"}]) if sim else default_ports)
        self.reset_wait = 0.0 if (sim or opener is SimArduino) else reset_wait
        self.on_change, self.announce = on_change or (lambda s: None), announce or (lambda t: None)
        self.lock = threading.RLock()
        self.ser, self.port_name, self.state, self.last_error = None, "", None, ""
        self._timer, self._timer_info, self._stop = None, None, threading.Event()
        self._next_try, self._thread, self._fails = 0.0, None, 0

    # ------------------------------------------------ properties
    @property
    def connected(self):
        return self.ser is not None

    def label(self):
        if not self.connected:
            return "NOT CONNECTED"
        return "ON" if self.state else "OFF"

    def _notify(self):
        try:
            self.on_change(self.label())
        except Exception:
            pass

    # ------------------------------------------------ low level
    def _readline(self, ser):
        try:
            return ser.readline().decode("utf-8", "ignore").strip()
        except Exception as e:
            raise LightError(f"The USB link failed: {e}")

    def _wait_for(self, ser, prefix, timeout):
        end = time.time() + timeout
        while time.time() < end:
            line = self._readline(ser)
            if not line:
                time.sleep(0.01)
                continue
            if line.startswith("WDOG"):
                self.state = False; self._notify()
                self.announce("The Arduino switched the light off because I stopped talking to it.")
                continue
            if line.startswith(prefix):
                return line
        raise LightError("The Arduino did not answer.")

    def _xfer(self, command, prefix, timeout=1.5):
        with self.lock:
            if not self.ser:
                raise LightError("The light controller is not connected.")
            try:
                self.ser.write((command + "\n").encode())
                return self._wait_for(self.ser, prefix, timeout)
            except LightError as e:
                self._drop(str(e))
                raise
            except Exception as e:
                self._drop(str(e))
                raise LightError("The USB link to the Arduino failed. Check the cable.")

    def _drop(self, why=""):
        with self.lock:
            try:
                if self.ser:
                    self.ser.close()
            except Exception:
                pass
            self.ser, self.state, self.last_error = None, None, why
        self._notify()

    # ------------------------------------------------ connect
    def connect(self):
        """Find the Arduino, check it speaks our protocol, make sure the light is OFF. Returns a sentence."""
        with self.lock:
            if self.ser:
                return f"The light controller is connected on {self.port_name}."
            want = str(self.cfg.get("port", "auto")).strip()
            try:
                ports = [{"device": want, "desc": "configured"}] if want.lower() != "auto" else self.ports_fn()
            except LightError as e:
                self.last_error = str(e); return str(e)
            if not ports:
                self.last_error = "I cannot find the Arduino. Check that the USB cable is plugged in."
                return self.last_error
            busy, silent = [], []
            for p in ports:
                dev = p["device"]
                try:
                    ser = self.opener(dev, int(self.cfg.get("baud", 9600)), 0.3)
                except LightError as e:
                    self.last_error = str(e); return str(e)
                except Exception as e:
                    busy.append(dev); continue
                try:
                    time.sleep(self.reset_wait)                      # the Uno restarts when the port opens
                    while self._readline(ser):
                        pass
                    ser.write(b"PING\n")
                    self._wait_for(ser, "PONG " + PROTOCOL, 2.5)
                    ser.write(("POL " + ("LOW" if self.cfg.get("active_low") else "HIGH") + "\n").encode())
                    self._wait_for(ser, "OK POL", 1.5)
                    ser.write(b"OFF\n")
                    self._wait_for(ser, "OK OFF", 1.5)
                except Exception:
                    silent.append(dev)
                    try: ser.close()
                    except Exception: pass
                    continue
                self.ser, self.port_name, self.state, self.last_error = ser, dev, False, ""
                self._notify()
                return f"The light controller is connected on {dev}. The light is off."
            if busy:
                self.last_error = f"{busy[0]} is busy. Close the Arduino IDE Serial Monitor and try again."
            else:
                self.last_error = (f"I found a device on {silent[0]} but it did not answer. "
                                   "Upload nemesis_light.ino to the Uno with the Arduino IDE first.")
            return self.last_error

    def _ensure(self):
        if not self.ser:
            self.connect()
        if not self.ser:
            raise LightError(self.last_error or "The light controller is not connected.")

    # ------------------------------------------------ commands
    def set(self, on, minutes=None):
        """Switch the light. Returns (ok, sentence)."""
        for attempt in (1, 2):                       # one retry: the cable may have been pulled and put back
            try:
                with self.lock:
                    self._ensure()
                    self._xfer("ON" if on else "OFF", "OK " + ("ON" if on else "OFF"))
                    self.state = bool(on)
                break
            except LightError as e:
                if attempt == 2:
                    return False, str(e)
        try:
            self._cancel_timer()
            limit = self.cfg.get("max_on_minutes") or 0
            if on and (minutes or limit):
                self.schedule(False, minutes or limit, quiet=True)
            self._notify()
            text = "LED lighting switched on" if on else "LED lighting switched off"
            if on and minutes:
                text += f" for {minutes} minute{'s' if minutes != 1 else ''}"
            return True, text + "."
        except LightError as e:
            return False, str(e)

    def toggle(self):
        return self.set(not self.state)

    def status_text(self):
        if not self.ser:
            return self.last_error or "The light controller is not connected."
        try:
            line = self._xfer("STATUS", "STATE")
            on = line.split()[1] == "ON"
            if on != self.state:
                self.state = on; self._notify()
        except LightError as e:
            return str(e)
        extra = f" The light will switch {'off' if self._timer_info and not self._timer_info[0] else 'on'} in about {self._minutes_left()} minutes." if self._timer_info else ""
        return ("The light is on." if self.state else "The light is off.") + extra

    def flip_polarity(self):
        self.cfg["active_low"] = not self.cfg.get("active_low")
        save_config(self.cfg, self.config_file)
        if self.ser:
            self._xfer("POL " + ("LOW" if self.cfg["active_low"] else "HIGH"), "OK POL")
            self.state = False; self._notify()
        return "LOW" if self.cfg["active_low"] else "HIGH"

    # ------------------------------------------------ timers
    def schedule(self, on, minutes, quiet=False):
        self._cancel_timer()
        t = threading.Timer(max(1, minutes) * 60.0, self._timed, args=(on,))
        t.daemon = True
        self._timer, self._timer_info = t, (on, time.time() + minutes * 60.0)
        t.start()
        if quiet:
            return ""
        return f"I will switch the light {'on' if on else 'off'} in {minutes} minute{'s' if minutes != 1 else ''}."

    def _minutes_left(self):
        return max(1, int(round((self._timer_info[1] - time.time()) / 60.0))) if self._timer_info else 0

    def _cancel_timer(self):
        if self._timer:
            self._timer.cancel()
        self._timer, self._timer_info = None, None

    def cancel_timer(self):
        had = self._timer is not None
        self._cancel_timer()
        return "Light timer cancelled." if had else "There is no light timer."

    def _timed(self, on):
        self._timer, self._timer_info = None, None
        ok, text = self.set(on)
        self.announce("Timer finished. " + text if ok else text)

    # ------------------------------------------------ background upkeep
    def start(self):
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        first = True
        while not self._stop.wait(0.2 if first else 3.0):
            first = False
            try:
                if self.ser:
                    self._xfer("HB", "HB", 1.0)
                elif self.cfg.get("auto_connect", True) and time.time() >= self._next_try:
                    self.connect()
                    self._fails = 0 if self.ser else self._fails + 1
                    self._next_try = time.time() + min(10 * 2 ** self._fails, 120)     # 10 s, 20 s, 40 s ... at most 2 minutes
            except LightError:
                pass
            except Exception:
                pass

    def shutdown(self):
        """Nemesis is closing: switch off, then release the port."""
        self._stop.set()
        self._cancel_timer()
        try:
            if self.ser and self.cfg.get("off_on_exit", True):
                self._xfer("OFF", "OK OFF", 1.0)
        except Exception:
            pass
        self._drop("closed")
