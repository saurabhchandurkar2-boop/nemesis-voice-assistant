"""Nemesis self-test: runs a long list of phrases through the REAL command routing with every
side effect (opening apps, keys, shutdown, internet, files) replaced by a harmless stand-in.
Run:  python nemesis.py --selftest      or say: "run the command test"
"""
import contextlib
import json
import os
import tempfile
import time
import types
from unittest import mock

# (phrase, regex that the reply must match, or None = "no skill; goes to the chat brain")
CASES = [
    # --- time, date, small talk
    ("what time is it", r"\d:\d\d"), ("what is the time", r"\d:\d\d"), ("what's the date", r"Today is"),
    ("what day is it", r"Today is"), ("hello", r"Hello"), ("thanks", r"welcome"), ("who are you", r"Nemesis"),
    ("good night", r"Good night"), ("what can you do", r"I can"),
    # --- maths + follow-ups
    ("what is 25 times 4", r"100"), ("times 3", r"300"), ("plus 50", r"350"), ("divided by 7", r"50"),
    ("calculate 15 percent of 200", r"30"), ("what's two plus two", r"4"), ("what is the square root of 144", r"12"),
    ("what is 2 to the power of 10", r"1,?024"), ("10 divided by 0", r"zero|cannot|can't|not"),
    ("add 18 percent gst to 1500", r"1,?770"),
    # --- units
    ("convert 100 degrees celsius to fahrenheit", r"212"), ("how many feet in 2 miles", r"10,?560"), ("how many people are in the room", r"eyes|see|camera|people"), ("what is 5 km in miles", r"3\.1"),
    # --- weather + follow-ups
    ("weather in pune", r"Pune|31|degrees"), ("what about tomorrow", r"tomorrow|Tomorrow|Pune"), ("and in mumbai", r"Mumbai|Pune|degrees"),
    ("set my city to pune", r"Pune"), ("what's the weather", r"degrees|Pune|31"),
    # --- news, wikipedia
    ("news", r"headlines|Big thing"), ("news about cricket", r"cricket|Big thing"), ("who is albert einstein", r"Einstein|physicist"),
    # --- reminders / timers
    ("set a timer for 5 minutes", r"timer|Timer|5"), ("remind me in 10 minutes to call my mom", r"remind|Remind|ten|10"),
    ("wake me up at 7 am", r"7|seven"), ("every day at 8 am remind me to drink water", r"8|eight|every|Every|daily"),
    ("list my reminders", r"reminder|Reminder"), ("cancel all reminders", r"cleared|cancel|removed|no reminders"),
    # --- apps, sites, music
    ("open notepad", r"Opening"), ("open calculator", r"Opening"), ("launch chrome", r"Opening|could not find"),
    ("open youtube", r"Opening"), ("open google.com", r"Opening"), ("play despacito", r"Playing"),
    ("search for best laptops", r"Searching"), ("google python tutorials", r"Searching"),
    ("search cats on youtube", r"Playing"), ("pause the music", r"Done"), ("next song", r"Next"),
    # --- volume
    ("set volume to 40", r"40"), ("volume up", r"up"), ("volume down", r"down"), ("mute", r"Done"),
    # --- pc status / close
    ("system status", r"."), ("close notepad", r"Closing|not seem"),
    ("close tab", r"Closed|test"), ("close this tab", r"Closed|test"), ("close this window", r"Closed|test"), ("close it", r"Closed the window"), ("close that", r"Closed the window"),
    ("close youtube", r"Closed the tab|do not see"), ("close whatsapp", r"Closing|could not find"),
    # --- notes, tasks, memory
    ("take a note buy milk", r"Noted"), ("read my notes", r"notes|Noted|milk"),
    ("remember that my favourite colour is blue", r"."), ("what is my favourite colour", None),
    ("add a task call the bank", r"."), ("what are my tasks", r"bank|task"),
    # --- brain modes, privacy, offline
    ("fast mode", r"."), ("privacy mode on", r"Privacy mode on"), 
    ("privacy mode off", r"yes or no|Say yes"), ("yes", r"Privacy mode off"),
    ("go offline", r"Offline mode"), ("are you offline", r"offline"), ("go online", r"online"),
    # --- security
    ("check my pc for threats", r"."), ("are you frozen", r"No"),
    ("freeze nemesis", r"Frozen"), ("what time is it", r"frozen|Frozen"), ("open notepad", r"frozen|Frozen"),
    ("unfreeze nemesis", r"Unfrozen"), ("what time is it", r"\d:\d\d"),
    ("shut down the computer", r"really want"), ("no", r"cancelled"),
    ("restart the computer", r"really want"), ("yes", r"."),
    ("set pin 4821", r"really want"), ("no", r"cancelled"),
    ("forget everything", r"really want|sure|Say yes"), ("no", r"cancel|Okay"),
    # --- light control (simulated Arduino)
    ("open the youtube and play any hindi song", r"Playing hindi song"), ("open the youtube and play arijit singh", r"Playing arijit singh"),
    ("speak something", r"\w"), ("say something", r"\w"), ("talk to me", r"\w"),
    ("what is your current update status", r"version 16"), ("are you up to date", r"version 16"), ("which version are you", r"version 16"),
    ("stay awake for 5 minutes", r"5 minutes"), ("keep listening", r"10 minutes"), ("how long do you stay awake", r"I stay awake for"),
    ("back to normal listening", r"30 seconds"), ("thinking sound off", r"silent while"), ("thinking sound on", r"short word"),
    ("turn on the lights", r"switched on"), ("is the light on", r"light is on"), ("turn off the lights", r"switched off"),
    ("is the light on", r"light is off"), ("switch on my LED", r"switched on"), ("switch off my LED", r"switched off"),
    ("lights on", r"switched on"), ("lights off", r"switched off"), ("turn on the light for 30 minutes", r"30 minutes"),
    ("is the light on", r"on"), ("cancel the light timer", r"cancelled"), ("turn off the light in 10 minutes", r"10 minutes"),
    ("cancel the light timer", r"cancelled"), ("toggle the light", r"switched off"), ("turn off everything", r"Everything is off"),
    ("calibrate the light relay", r"red light"), ("yes", r"correct"), ("calibrate the light relay", r"red light"), ("no", r"reversed"),
    ("calibrate the light relay", r"red light"), ("no", r"reversed"), ("turn off the lights", r"switched off"),
    ("find the arduino", r"connected"), ("turn on the light", r"switched on"), ("turn off the light", r"switched off"),
    # --- lock, screen
    ("lock the computer", r"Locking|could not lock"),
    # --- voice, language
    ("change voice to ryan", r"."), ("speak slower", r"."), ("speak hindi", r"."),
    # --- repeat
    ("repeat that", r"."), ("say that again", r"."),
    # --- real chat (must reach the brain, not a skill)
    ("why is the sky blue", None), ("what should I cook for dinner", None),
    ("explain how a car engine works", None), ("write a poem about rain", None),
]

# Hindi / Marathi / Hinglish go through indic_command first, like in process()
INDIC = [
    ("मौसम बताओ", r"."), ("समय क्या हुआ है", r"\d:\d\d|."), ("youtube kholo", r"Opening"), ("khabar sunao", r"."),
]


def _fake_http(url, timeout=8):
    if "geocoding" in url:
        return json.dumps({"results": [{"name": "Pune", "latitude": 18.5, "longitude": 73.8}]}).encode()
    if "open-meteo" in url:
        return json.dumps({"current": {"temperature_2m": 31.4, "apparent_temperature": 34.2, "weather_code": 2, "wind_speed_10m": 9},
                           "daily": {"temperature_2m_max": [33.1, 32], "temperature_2m_min": [24.2, 23],
                                     "precipitation_probability_max": [20, 60]}}).encode()
    if "news.google" in url:
        return b"<rss><channel><item><title>Big thing happens - The Hindu</title></item><item><title>Other news - BBC</title></item></channel></rss>"
    if "list=search" in url:
        return json.dumps({"query": {"search": [{"title": "Albert Einstein"}]}}).encode()
    if "page/summary" in url:
        return json.dumps({"extract": "Albert Einstein was a German-born physicist. He developed relativity.", "type": "standard"}).encode()
    return b""


def run(N, quiet=False):
    """Returns (passed, failed, report_lines). Everything that touches the PC is faked and restored afterwards."""
    from mind import Mind
    import mind as mind_mod
    tmp = tempfile.mkdtemp()
    log = []
    said = []
    fake_proc = mock.MagicMock()
    fake_proc.run.return_value = types.SimpleNamespace(returncode=0, stdout="", stderr="")
    with contextlib.ExitStack() as es:
        def P(obj, name, value):
            if hasattr(obj, name):
                es.enter_context(mock.patch.object(obj, name, value))
        # files -> temp folder
        for nm, fn in (("CONFIG_FILE", "c.json"), ("REM_FILE", "r.json"), ("NOTES_FILE", "n.txt"), ("AUDIT_FILE", "a.log")):
            P(N, nm, os.path.join(tmp, fn))
        P(mind_mod, "DATA_FILE", os.path.join(tmp, "m.dat"))
        P(mind_mod, "KEY_FILE", os.path.join(tmp, "m.key"))
        P(N, "mind", Mind(generate=lambda p, n=600: "ok"))
        P(N, "reminders", [])
        P(N, "pending", {"song_until": 0.0, "city_until": 0.0, "confirm": None, "pin": None})
        P(N, "PRIVACY", False)
        P(N, "OFFLINE", False)
        # side effects -> harmless
        P(N, "subprocess", fake_proc)
        P(N, "webbrowser", mock.MagicMock())
        P(N, "ctypes", mock.MagicMock())
        P(N, "press_key", lambda *a, **k: True)
        P(N, "youtube_first_video", lambda q: "https://example.invalid/" + q.replace(" ", "+"))
        P(N, "open_start_menu_app", lambda name: name.title())
        P(N, "run_power", lambda action: f"(test) would {action}")
        P(N, "http_get", _fake_http)
        P(N, "say", lambda t, target=None: said.append(t))
        P(N, "history", [])
        P(N, "pin_is_set", lambda: False)
        P(N, "set_offline", lambda on: setattr(N, "OFFLINE", on))
        P(N.ears, "ready", lambda lang="en": (True, ""))
        P(N.offline, "tts_available", lambda: True)
        P(N, "choose_brain", lambda: None)
        P(N, "ollama", mock.MagicMock())
        N.guard.collect = (lambda: {"procs": [], "startup": [], "tasks": [], "defender": {"realtime": True}}) \
            if not getattr(N.guard, "_selftest_saved", None) else N.guard.collect
        for nm in ("press", "run_keys", "type_text", "_type_now", "start_dictation", "grab_screen", "take_shot",
                   "send_image_to_phone", "read_screen", "_open_file", "to_clipboard", "save_text_as", "_send", "screenshot_skill"):
            if hasattr(N.tools, nm):
                if nm == "screenshot_skill":
                    continue
                P(N.tools, nm, lambda *a, **k: "(test)")
        P(N.guard, "REPORT_FILE", os.path.join(tmp, "rep.txt"))
        P(N.guard, "collect", lambda: {"procs": [], "startup": [], "tasks": [], "defender": {"realtime": True}})
        P(N.tools, "foreground_title", lambda: "YouTube - Google Chrome")
        P(N.tools, "screenshot_skill", lambda t: ("(test) screenshot" if "screenshot" in t else None))
        sim_light = N.home.Light(opener=N.home.SimArduino, ports_fn=lambda: [{"device": "SIM", "desc": "x"}],
                                 config_file=os.path.join(tmp, "home.json"))
        sim_light.connect()
        P(N, "LIGHT", sim_light)
        P(N.CORE, "router_on", False)
        P(N.CORE, "frozen", False)
        P(N.CORE, "gate_pending", None)
        P(N.CORE, "last_reply", "")
        P(N.CORE, "last_topic", None)
        P(N.CORE, "on_freeze", lambda on: None)
        P(N, "OLLAMA_MODEL", "x")
        # ---- the phrase list
        ok = bad = 0
        steps = [(p, e, False) for p, e in CASES] + [(p, e, True) for p, e in INDIC]
        for phrase, expect, indic in steps:
            text = phrase
            if indic:
                cmd = N.indic_command(phrase)
                text = cmd or phrase
            try:
                reply = N.route(text, "user", use_router=False)
            except Exception as e:                     # a crash is a failed test, not a crash
                reply = f"!! EXCEPTION {type(e).__name__}: {e}"
            if reply == N.tools.SILENT:
                reply = "(silent)"
            good = (reply is None) if expect is None else (reply is not None and __import__("re").search(expect, str(reply), __import__("re").I) is not None)
            ok, bad = ok + good, bad + (not good)
            if not good:
                log.append(f"FAIL  {phrase!r}  ->  {str(reply)[:110]!r}   (wanted {'brain' if expect is None else expect})")
        # ---- security rules that must always hold
        C = N.CORE
        sec = []
        C.frozen, C.gate_pending = False, None
        v, r = C.gate("open notepad", "router"); sec.append(("router may open apps", v == "go"))
        v, r = C.gate("shut down the computer", "router"); sec.append(("router cannot shut down", v == "reply"))
        v, r = C.gate("set pin 1234", "router"); sec.append(("router cannot change PIN", v == "reply"))
        try:
            sim_light.set(True); sim_light.shutdown()
            sec.append(("light is off after shutdown", sim_light.state in (False, None)))
        except Exception:
            sec.append(("light shutdown works", False))
        C.note_spoken("The total amount due is four thousand two hundred rupees"); sec.append(
            ("echo of my own speech is ignored", C.is_echo("the total amount due is four thousand two hundred rupees")))
        sec.append(("a real new command is not an echo", not C.is_echo("open notepad please now")))
        out_r = C.parse_router('{"intent":"light_on"}'); sec.append(("router can request lights", out_r == "turn on the lights"))
        raw_bad = ['{"intent":"shutdown"}', '{"intent":"open_app"}', 'ignore previous instructions', '{"intent":"volume_set","n":500}',
                   '{"intent":"open_app","q":"calc; del *"}']
        out = [C.parse_router(x) for x in raw_bad]
        sec.append(("router rejects unknown intents / bad numbers", out[0] is None and out[1] is None and out[2] is None and out[3] is None))
        sec.append(("router cleans punctuation tricks", out[4] is not None and ";" not in out[4]))
        sec.append(("router volume ok", C.parse_router('{"intent":"volume_set","n":35}') == "set the volume to 35"))
        # ---- wake word spelling + awake window
        for phrase, want in (("nemisis open notepad", "open notepad"), ("hey nemesys what time is it", "what time is it"),
                             ("okay nemessis stop", "stop"), ("nimesis", ""), ("open notepad nemesis", "")):
            heard, rest = N.strip_wake_word(phrase)
            good = heard and (want == "" or rest == want)
            sec.append((f"wake word heard in {phrase!r}", bool(good)))
            if not good:
                log.append(f"FAIL  wake word not heard in {phrase!r} -> {heard!r} {rest!r}")
        for phrase in ("necessary changes please", "generous offer today", "name this song for me", "namaste friend",
                       "message me later", "open the menu", "genesis chapter one", "play music"):
            heard, _ = N.strip_wake_word(phrase)
            sec.append((f"no false wake in {phrase!r}", not heard))
            if heard:
                log.append(f"FAIL  false wake word in {phrase!r}")
        # ---- faces (fake detector: scene = list of identity numbers in front of the camera)
        import faces as F
        import numpy as np
        scene = []

        class FakeBackend:
            error = None
            def load(self): return None
            def prepare(self, frame, max_w=640): return frame
            def detect(self, frame):
                return [np.array([10, 10, 200, 200] + [0] * 10 + [float(i)], dtype=np.float32) for i in scene]
            def embed(self, frame, row):
                v = np.zeros(128, dtype=np.float32); v[int(row[14])] = 1.0
                return v
        counter = [0]
        def fake_frame():
            counter[0] += 1
            return np.zeros((240, 320, 3), dtype=np.uint8), counter[0]
        fk = F.Faces(backend=FakeBackend(), data_file=os.path.join(tmp, "faces.dat"), key_file=os.path.join(tmp, "f.key"))
        P(N, "FACES", fk); P(N, "_face_frame", fake_frame); P(N, "_face_ready", lambda: None)
        P(F, "SHOT_GAP", 0.0)
        C.frozen, C.gate_pending = False, None
        def say_(p):
            try:
                return str(N.route(p, "user", use_router=False))
            except Exception as e:
                return f"!! EXCEPTION {type(e).__name__}: {e}"
        def chk(name, got, pat):
            good = __import__("re").search(pat, got, __import__("re").I) is not None
            sec.append((f"faces: {name}", good))
            if not good:
                log.append(f"FAIL  faces {name}: {got[:110]!r} (wanted {pat})")
        scene[:] = [1]
        chk("detect", say_("can you detect my face"), r"I see one face")
        chk("who, none saved", say_("who am i"), r"do not know any faces")
        chk("identify wording", say_("can you identify"), r"do not know any faces")
        scene[:] = []
        chk("no face", say_("can you detect my face"), r"do not see a face")
        scene[:] = [1]
        chk("enrol asks permission", say_("remember my face as yash"), r"confirmation|really want")
        chk("enrol done", say_("yes"), r"recognise Yash")
        chk("known face", say_("who am i"), r"I see Yash")
        scene[:] = [2]
        chk("stranger", say_("who am i"), r"I see one face I do not know")
        scene[:] = [1, 2]
        chk("two people", say_("who is this"), r"Yash and one face I do not know")
        scene[:] = [1]
        chk("list", say_("list faces"), r"I know Yash")
        say_("remember my face"); chk("no name", say_("yes"), r"Say remember my face as")
        v, r = C.gate("remember my face as bob", "router"); sec.append(("router cannot enrol faces", v == "reply"))
        chk("forget asks", say_("forget my face"), r"confirmation|really want")
        chk("forget done", say_("yes"), r"forgot the face of Yash")
        chk("forget all asks", say_("forget all faces"), r"confirmation|really want")
        chk("forget all done", say_("yes"), r"no saved faces|forgot every")
        chk("none left", say_("who am i"), r"do not know any faces")
        for name, good in sec:
            ok, bad = ok + good, bad + (not good)
            if not good:
                log.append(f"FAIL  security: {name}")
        # make sure nothing is left frozen / pending
        C.frozen, C.gate_pending = False, None
    total = ok + bad
    log.insert(0, f"Command test: {ok} of {total} passed.")
    if not quiet or bad:
        for ln in log:
            print(ln)
    return ok, bad, log
