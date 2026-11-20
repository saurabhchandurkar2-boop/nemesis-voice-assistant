"""Nemesis shared core (v13): one gate that every command passes through.

 * Permission levels  READ / REVERSIBLE / SENSITIVE / ADMIN, with a rule table.
 * Outside text (OCR, web, documents, anything Nemesis reads aloud) is DATA, never a command:
   the echo filter drops it if the microphone hears it again.
 * Emergency freeze / unfreeze.
 * Allow-listed brain router: the AI may only pick from a fixed menu; the result is turned
   into a normal command and goes through this same gate with origin="router".
 * Follow-ups: "repeat that", "times 3", "what about tomorrow", "and in Mumbai".
Pure Python, no Windows calls, so it can be tested anywhere.
"""
import json
import re
import time

READ, REVERSIBLE, SENSITIVE, ADMIN = 0, 1, 2, 3
LEVEL_NAMES = {READ: "read", REVERSIBLE: "reversible", SENSITIVE: "sensitive", ADMIN: "admin"}

# (pattern, level, own_confirm, label).  First match wins.  own_confirm=True means the
# existing skill already asks "yes/no" (+PIN) itself, so the gate must not ask twice.
RULES = [
    (r"\b(?:shut ?down|turn off|power off|restart|reboot|sleep|put)\b.*\b(?:computer|pc|system|laptop|machine)\b", SENSITIVE, True, "power"),
    (r"\b(?:set|change|create|remove|delete|clear|disable)\b.*\bpin\b", ADMIN, False, "pin"),
    (r"\b(?:forget|erase|wipe|delete|clear|remove)\b.*\bfaces?\b", SENSITIVE, False, "face-erase"),
    (r"\b(?:remember|learn|save|enrol+|register|add|store)\b.*\b(?:my|his|her|their|this)\b.*\bface\b", SENSITIVE, False, "face-enrol"),
    (r"\b(?:forget|erase|wipe|delete|clear)\b.*\b(?:everything|all|memory|memories|notes|tasks|history|facts)\b", SENSITIVE, False, "erase"),
    (r"\b(?:disable|remove|delete|kill|stop)\b.*\b(?:suspicious|startup|scheduled|task|tasks|threats?)\b", ADMIN, False, "guard"),
    (r"\b(?:seal|reseal|trust)\b.*\b(?:files|manifest|code)\b", ADMIN, False, "seal"),
    (r"\bprivacy mode (?:off|disable)|\bturn (?:off|disable) privacy\b", SENSITIVE, False, "privacy-off"),
    (r"^(?:type|dictate)\b|\bstart dictation\b|\bopen .+ and type\b", SENSITIVE, False, "typing"),
    (r"\b(?:close|quit|exit|kill)\b (?!nemesis)", REVERSIBLE, False, "close-app"),
    (r"\b(?:open|launch|start|play|set|turn|switch|mute|unmute|volume|lock|take a screenshot|screenshot|remind|timer|alarm|note|search|google)\b", REVERSIBLE, False, "action"),
]
_RULES = [(re.compile(p), lv, oc, lab) for p, lv, oc, lab in RULES]

FREEZE_RE = re.compile(r"^(?:emergency stop|freeze(?: nemesis)?|lock ?down(?: nemesis)?|freeze everything|stop everything now)$")
UNFREEZE_RE = re.compile(r"^(?:unfreeze(?: nemesis)?|resume nemesis|end lock ?down|cancel lock ?down|lift freeze)$")
YES_RE = re.compile(r"^(?:yes|yeah|yep|yup|confirm|do it|go ahead|sure|affirmative)(?: please)?$")
NO_RE = re.compile(r"^(?:no|nope|cancel|never ?mind|abort|don'?t)(?: please)?$")
REPEAT_RE = re.compile(r"^(?:repeat(?: that| it)?|say (?:that|it) again|what did you say|come again|pardon|sorry what|once more)(?: please)?$")

# Which levels each origin may trigger without the user saying them directly.
ORIGIN_MAX = {"user": ADMIN, "typed": ADMIN, "phone": ADMIN, "pc-mic": ADMIN, "router": REVERSIBLE}


SHUT_RE = re.compile(r"\bshut (?:the |my )?(computer|pc|laptop|machine|system) down\b")


def classify(text):
    """-> (level, own_confirm, label) for a sentence."""
    t = text.lower().strip(" .!?,")
    t = SHUT_RE.sub(r"shut down \1", t)
    for rx, lv, oc, lab in _RULES:
        if rx.search(t):
            return lv, oc, lab
    return READ, False, "read"


def _words(s):
    return re.findall(r"[a-z0-9ऀ-ॿ']+", s.lower())


class Core:
    def __init__(self, pin_is_set=lambda: False, check_pin=lambda p: False, audit=lambda *a, **k: None,
                 busy=lambda: False, on_freeze=lambda on: None):
        self.pin_is_set, self.check_pin, self.audit = pin_is_set, check_pin, audit
        self.busy = busy                       # True while an older skill waits for yes / PIN / a city
        self.on_freeze = on_freeze
        self.frozen = False
        self.gate_pending = None               # dict(text, level, label, until, stage)
        self.spoken = []                       # (time, words) of things Nemesis said recently
        self.last_reply = ""
        self.last_topic = None                 # ("weather", city, tomorrow) / ("math", number)
        self.router_on = True

    # ----- echo filter: outside text is data, never a command -----
    def note_spoken(self, text):
        w = _words(text)
        if w:
            self.spoken.append((time.time(), set(w), len(w)))
            self.spoken = [s for s in self.spoken if time.time() - s[0] < 90][-30:]

    def is_echo(self, text):
        w = _words(text)
        if len(w) < 3:
            return False
        ws = set(w)
        now = time.time()
        for t0, sw, n in self.spoken:
            if now - t0 < 60 and len(ws & sw) / len(ws) >= 0.75:
                return True
        return False

    # ----- freeze -----
    def freeze(self, on):
        self.frozen = on
        self.gate_pending = None
        self.audit("freeze", "on" if on else "off")
        try:
            self.on_freeze(on)
        except Exception:
            pass

    # ----- the gate -----
    def gate(self, text, origin="user"):
        """-> ("go", text) to carry on, or ("reply", message) to say it and stop."""
        t = text.lower().strip(" .!?,")
        if not t:
            return "go", text
        # freeze phrases always work, from anywhere
        if FREEZE_RE.match(t):
            self.freeze(True)
            return "reply", "Frozen. I will not do anything until you say unfreeze Nemesis."
        if self.frozen:
            if UNFREEZE_RE.match(t):
                if self.pin_is_set():
                    self.gate_pending = {"stage": "unfreeze-pin", "until": time.time() + 30, "tries": 0}
                    return "reply", "Please say or type your PIN to unfreeze."
                self.freeze(False)
                return "reply", "Unfrozen. I am listening."
            gp = self.gate_pending
            if gp and gp["stage"] == "unfreeze-pin" and time.time() < gp["until"]:
                if self.check_pin(t):
                    self.freeze(False)
                    return "reply", "Unfrozen. I am listening."
                gp["tries"] += 1
                if gp["tries"] >= 3:
                    self.gate_pending = None
                    return "reply", "Wrong PIN. Still frozen."
                return "reply", "Wrong PIN. Try again."
            return "reply", "I am frozen. Say unfreeze Nemesis."

        gp = self.gate_pending
        if gp:
            if time.time() > gp["until"]:
                self.gate_pending = None
            elif gp["stage"] == "confirm":
                if YES_RE.match(t):
                    if gp["level"] >= ADMIN and self.pin_is_set():
                        gp["stage"], gp["until"], gp["tries"] = "pin", time.time() + 30, 0
                        return "reply", "Please say or type your PIN."
                    self.gate_pending = None
                    self.audit("gate-ok", gp["label"])
                    return "go!", gp["text"]
                if NO_RE.match(t):
                    self.gate_pending = None
                    return "reply", "Okay, cancelled."
                self.gate_pending = None                 # something else: forget the question
            elif gp["stage"] == "pin":
                if self.check_pin(t):
                    self.gate_pending = None
                    self.audit("gate-ok", gp["label"])
                    return "go!", gp["text"]
                gp["tries"] += 1
                if gp["tries"] >= 3:
                    self.gate_pending = None
                    return "reply", "Wrong PIN. Cancelled."
                return "reply", "Wrong PIN. Try again."

        if self.busy():                         # an older skill is waiting for yes / PIN / city: let it have the word
            return "go", text

        level, own, label = classify(t)
        if level > ORIGIN_MAX.get(origin, REVERSIBLE):
            self.audit("gate-blocked", f"{origin}: {label}")
            return "reply", "I will not do that on my own. Please say it yourself."
        if level >= SENSITIVE and not own:
            self.gate_pending = {"stage": "confirm", "text": text, "level": level, "label": label,
                                 "until": time.time() + 20}
            what = {"erase": "erase stored data", "pin": "change the PIN", "guard": "change startup or scheduled items",
                    "seal": "re-seal the trusted files", "privacy-off": "turn privacy mode off",
                    "typing": "type on this computer",
                    "face-erase": "delete saved faces", "face-enrol": "save a face on this PC, and does that person agree"}.get(label, "do that")
            return "reply", f"That needs your confirmation. Do you really want me to {what}? Say yes or no."
        return "go", text

    # ----- follow-ups -----
    def followup(self, text):
        """-> ("reply", str) / ("rewrite", str) / None."""
        t = text.lower().strip(" .!?,")
        if REPEAT_RE.match(t):
            return ("reply", self.last_reply) if self.last_reply else ("reply", "I have not said anything yet.")
        tp = self.last_topic
        if not tp:
            return None
        if tp[0] == "math":
            m = re.fullmatch(r"(?:now |and |then )?(?:(times|multiplied by|multiply by|x)|(plus|add|and add)|(minus|subtract|take away)|"
                             r"(divided by|divide by|over)|(to the power of|power))\s*(-?\d+(?:\.\d+)?)", t)
            if m:
                op = ["times", "plus", "minus", "divided by", "to the power of"][[i for i in range(1, 6) if m.group(i)][0] - 1]
                return "rewrite", f"what is {tp[1]} {op} {m.group(6)}"
            if t in ("double that", "double it", "twice that"):
                return "rewrite", f"what is {tp[1]} times 2"
            if t in ("half of that", "half that", "halve it"):
                return "rewrite", f"what is {tp[1]} divided by 2"
            if t in ("square it", "square that"):
                return "rewrite", f"what is {tp[1]} times {tp[1]}"
        if tp[0] == "weather":
            if re.fullmatch(r"(?:and |what about |how about )?tomorrow", t):
                return "rewrite", "weather tomorrow" + (f" in {tp[1]}" if tp[1] else "")
            m = re.fullmatch(r"(?:and |what about |how about )(?:in )?([a-z][a-z .'-]{1,30})", t)
            if m and m.group(1) not in ("today", "now", "tonight"):
                return "rewrite", f"weather in {m.group(1)}" + (" tomorrow" if tp[2] else "")
            if re.fullmatch(r"(?:and |what about |how about )?(?:today|now|tonight)", t):
                return "rewrite", "weather" + (f" in {tp[1]}" if tp[1] else "")
        return None

    def remember(self, text, reply):
        """Called after every answered command: keeps what a follow-up may need."""
        if not reply:
            return
        self.last_reply = reply
        t = text.lower()
        m = re.search(r"(-?\d[\d,]*(?:\.\d+)?)\s*\.?\s*$", reply.strip())
        if m and re.search(r"\b(?:plus|minus|times|divided|multiplied|percent|root|power|square|cube|\d\s*[-+*/x]\s*\d)", t):
            self.last_topic = ("math", m.group(1).replace(",", ""))
        elif re.search(r"\b(?:weather|forecast|temperature|rain)\b", t):
            c = re.search(r"\b(?:in|for|at)\s+([a-z][a-z .'-]*?)(?:\s+tomorrow)?$", t.strip(" ?.!"))
            self.last_topic = ("weather", c.group(1).title() if c else None, "tomorrow" in t)
        elif re.fullmatch(r"(?:what is |what's |calculate |)?[\d.\s+\-*/x%()]+", t.strip(" ?.")):
            if m:
                self.last_topic = ("math", m.group(1).replace(",", ""))
        else:
            self.last_topic = None

    # ----- router (allow-list) -----
    INTENTS = {
        "volume_set": ("set the volume to {n}", "n"),
        "volume_up": ("volume up", None),
        "volume_down": ("volume down", None),
        "mute": ("mute", None),
        "open_app": ("open {q}", "q"),
        "weather": ("weather{in_q}", "q?"),
        "news": ("news{about_q}", "q?"),
        "time": ("what is the time", None),
        "date": ("what is the date", None),
        "timer": ("set a timer for {n} minutes", "n"),
        "play": ("play {q}", "q"),
        "search": ("search for {q}", "q"),
        "note": ("take a note {q}", "q"),
        "joke": ("tell me a joke", None),
        "status": ("system status", None),
        "screenshot": ("take a screenshot", None),
        "read_text": ("read this", None),
        "look": ("what do you see", None),
        "light_on": ("turn on the lights", None),
        "light_off": ("turn off the lights", None),
        "light_status": ("is the light on", None),
    }
    ROUTER_PROMPT = (
        "You turn a spoken command into JSON. Reply with ONLY a JSON object like "
        '{"intent":"open_app","q":"calculator"}. Allowed intents: '
        + ", ".join(sorted(INTENTS)) + ", none. "
        'Use "n" for a number and "q" for text. If the sentence is a question, chat, or not clearly one of these commands, use {"intent":"none"}. '
        "Never invent intents. Never output anything except the JSON.")
    COMMANDLIKE = re.compile(r"^(?:please |can you |could you |would you |will you |nemesis |just |kindly )*"
                             r"(?:open|launch|start|run|play|set|turn|put|make|switch|show|bring|get|find|check|tell|read|look|take|increase|decrease|raise|lower|reduce|mute|unmute|pause|resume)\b")

    def looks_like_command(self, text):
        t = text.lower().strip(" .!?,")
        return bool(self.COMMANDLIKE.match(t)) and len(t.split()) <= 14

    @staticmethod
    def _clean(v, limit=40):
        v = re.sub(r"[^\w\s.'&-]", " ", str(v), flags=re.UNICODE)
        return re.sub(r"\s+", " ", v).strip()[:limit]

    def parse_router(self, raw):
        """Raw model output -> a canonical command string, or None.  Anything unexpected -> None."""
        try:
            m = re.search(r"\{.*\}", raw, re.S)
            data = json.loads(m.group(0)) if m else None
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        intent = str(data.get("intent", "")).strip().lower()
        spec = self.INTENTS.get(intent)
        if not spec:
            return None
        template, need = spec
        n = data.get("n")
        q = self._clean(data.get("q", "")) if data.get("q") is not None else ""
        if need == "n":
            try:
                nv = int(float(n))
            except Exception:
                return None
            if not 0 <= nv <= (100 if intent == "volume_set" else 600):
                return None
            return template.format(n=nv)
        if need == "q":
            return template.format(q=q) if q else None
        if need == "q?":
            return template.format(in_q=f" in {q}" if q else "", about_q=f" about {q}" if q else "")
        return template
