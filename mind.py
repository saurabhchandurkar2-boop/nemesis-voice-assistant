"""
NEMESIS mind: what it remembers about you, your notes and to-do list, and a search over your own documents.

Everything is stored on this PC in one encrypted file (nemesis_mind.dat). The key sits in certs/memory.key,
which Nemesis locks to your Windows account.  Nothing here goes online.
"""
import json
import math
import os
import re
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "nemesis_mind.dat")
KEY_FILE = os.path.join(BASE_DIR, "certs", "memory.key")
OLD_MEMORY = os.path.join(BASE_DIR, "nemesis_memory.json")
DOC_DIRS = ("documents", "knowledge")
STOP = set("the a an of to in on for and or is are was were be been it this that with as at by from about what who which "
           "how do does did i you me my your please tell say find search look documents document notes note say".split())


def _words(text):
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP and len(w) > 1]


class Mind:
    def __init__(self, generate=None):
        self.generate = generate
        self.lock = threading.RLock()
        self.pending = None                      # ("forget_all"|"clear_notes", expires)
        self.data = {"facts": [], "notes": [], "todos": []}
        self._index = None
        self._index_sig = None
        self._load()

    # ------------------------------------------------------------- storage (encrypted)
    def _fernet(self):
        try:
            from cryptography.fernet import Fernet
        except ImportError:
            return None
        try:
            os.makedirs(os.path.dirname(KEY_FILE), exist_ok=True)
            if os.path.exists(KEY_FILE):
                with open(KEY_FILE, "rb") as f:
                    key = f.read().strip()
            else:
                key = Fernet.generate_key()
                with open(KEY_FILE, "wb") as f:
                    f.write(key)
            return Fernet(key)
        except Exception:
            return None

    def _load(self):
        raw = None
        try:
            with open(DATA_FILE, "rb") as f:
                raw = f.read()
        except OSError:
            pass
        if raw:
            f = self._fernet()
            try:
                text = f.decrypt(raw).decode("utf-8") if f and raw[:1] != b"{" else raw.decode("utf-8")
                d = json.loads(text)
                for k in self.data:
                    self.data[k] = d.get(k, [])
            except Exception:
                pass
        elif os.path.exists(OLD_MEMORY):                  # first run: bring over the old simple memory
            try:
                with open(OLD_MEMORY, encoding="utf-8") as f:
                    old = json.load(f)
                for s in old if isinstance(old, list) else []:
                    if isinstance(s, str) and s.strip():
                        self.data["facts"].append({"t": s.strip(), "ts": time.time()})
                self._save()
            except Exception:
                pass

    def _save(self):
        with self.lock:
            blob = json.dumps(self.data, ensure_ascii=False).encode("utf-8")
            f = self._fernet()
            try:
                with open(DATA_FILE, "wb") as fh:
                    fh.write(f.encrypt(blob) if f else blob)
            except OSError:
                pass

    # ------------------------------------------------------------- what the brain gets to know
    def facts_prompt(self):
        facts = [x["t"] for x in self.data["facts"]][-14:]
        if not facts:
            return ""
        return " Facts the user told you to remember (use them when relevant, never invent others): " + "; ".join(facts) + "."

    def briefing_line(self):
        open_t = [t for t in self.data["todos"] if not t.get("done")]
        if not open_t:
            return ""
        first = open_t[0]["t"]
        return f"You have {len(open_t)} open task{'s' if len(open_t) != 1 else ''}. First: {first}."

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _clean(s):
        return re.sub(r"\s+", " ", s).strip(" .,!?")

    @staticmethod
    def _overlap(a, b):
        wa, wb = set(_words(a)), set(_words(b))
        return len(wa & wb) / (len(wa) or 1)

    def _pending_answer(self, t):
        if not self.pending:
            return None
        kind, until = self.pending
        self.pending = None
        if time.time() > until:
            return None
        if re.fullmatch(r"(?:yes|yeah|yep|yup|confirm|do it|go ahead|sure)(?: please)?", t):
            with self.lock:
                if kind == "forget_all":
                    self.data["facts"].clear()
                    self._save()
                    return "Done. I have forgotten everything you asked me to remember."
                if kind == "clear_notes":
                    self.data["notes"].clear()
                    self._save()
                    return "Your notes are deleted."
        if re.fullmatch(r"(?:no|nope|cancel|never ?mind|don'?t)(?: please)?", t):
            return "Okay, I will keep them."
        return None

    # ------------------------------------------------------------- the voice skill
    def skill(self, t):
        r = self._pending_answer(t)
        if r:
            return r
        # --- facts
        m = re.match(r"(?:please )?remember (?:that )?(.+)", t)
        if m and not re.match(r"(?:to|me to) ", m.group(1)):
            fact = self._clean(m.group(1))
            fact = re.sub(r"^my\b", "the user's", fact)
            fact = re.sub(r"^i am\b", "the user is", fact)
            fact = re.sub(r"^i\b", "the user", fact)
            with self.lock:
                if any(self._overlap(fact, f["t"]) >= 0.9 and self._overlap(f["t"], fact) >= 0.9 for f in self.data["facts"]):
                    return "I already know that."
                self.data["facts"].append({"t": fact, "ts": time.time()})
                self._save()
            return "I will remember that."
        m = re.match(r"(?:my name is|call me|i am called) ([a-z][a-z .'-]{1,30})$", t)
        if m:
            name = self._clean(m.group(1)).title()
            with self.lock:
                self.data["facts"] = [f for f in self.data["facts"] if not f["t"].startswith("the user's name is")]
                self.data["facts"].append({"t": f"the user's name is {name}", "ts": time.time()})
                self._save()
            return f"Nice to meet you, {name}. I will remember your name."
        m = re.match(r"i (?:live|stay) in ([a-z][a-z .'-]{1,30})$", t)
        if m:
            with self.lock:
                self.data["facts"].append({"t": f"the user lives in {self._clean(m.group(1)).title()}", "ts": time.time()})
                self._save()
            return "Got it, I will remember where you live."
        if re.search(r"\bwhat(?:'s| is) my name\b|\bdo you know my name\b|\bwho am i\b", t):
            for f in reversed(self.data["facts"]):
                mm = re.match(r"the user's name is (.+)", f["t"])
                if mm:
                    return f"Your name is {mm.group(1)}."
            return "You have not told me your name yet. Say my name is, and then your name."
        if re.search(r"\bwhat do you (?:remember|know) about me\b|\bwhat have i told you\b|\bwhat do you remember\b", t):
            facts = [f["t"] for f in self.data["facts"]]
            return ("I remember: " + ". ".join(facts) + ".") if facts else "I have nothing stored about you yet."
        if re.search(r"\b(?:forget everything|clear (?:your |my )?memory|erase (?:your |my )?memory)\b", t):
            self.pending = ("forget_all", time.time() + 30)
            return "This deletes everything I remember about you. Say yes to confirm."
        m = re.match(r"forget (?:that |about )?(?!everything)(.+)", t)
        if m and self.data["facts"]:
            q = m.group(1)
            with self.lock:
                keep = [f for f in self.data["facts"] if self._overlap(q, f["t"]) < 0.6]
                gone = len(self.data["facts"]) - len(keep)
                self.data["facts"] = keep
                self._save()
            return f"I forgot {gone} thing{'s' if gone != 1 else ''}." if gone else "I could not find that in my memory."
        # --- notes
        m = re.match(r"(?:please )?(?:take|make|write|add|save) (?:a )?note(?: that| to self)?[:,]? (.+)|note (?:down )?(?:that )?(.+)", t)
        if m:
            note = self._clean(m.group(1) or m.group(2))
            with self.lock:
                self.data["notes"].append({"t": note, "ts": time.time()})
                self._save()
            return "Noted."
        if re.search(r"\b(?:read|show|what are|list) (?:me )?(?:my )?notes\b|\bwhat did i (?:note|write down)\b", t):
            notes = self.data["notes"][-6:]
            if not notes:
                return "You have no notes."
            return f"You have {len(self.data['notes'])} note{'s' if len(self.data['notes']) != 1 else ''}. " + \
                   " ".join(f"{i + 1}. {n['t']}." for i, n in enumerate(notes))
        if re.search(r"\b(?:delete|clear|erase) (?:all )?(?:my )?notes\b", t):
            self.pending = ("clear_notes", time.time() + 30)
            return "This deletes all your notes. Say yes to confirm."
        # --- to-do list
        m = re.match(r"(?:please )?add (.+?) (?:to|on) (?:my |the )?(?:to-?do|task|tasks|shopping|grocery)(?: list)?$|"
                     r"(?:please )?(?:add|new|create) (?:a )?task[:,]? (.+)", t)
        if m:
            task = self._clean(m.group(1) or m.group(2))
            with self.lock:
                if any(not x.get("done") and x["t"].lower() == task.lower() for x in self.data["todos"]):
                    return f"{task} is already on your list."
                self.data["todos"].append({"t": task, "done": False, "ts": time.time()})
                self._save()
            return f"Added {task} to your list."
        if re.search(r"\bwhat(?:'s| is) on my (?:to-?do|task|tasks)(?: list)?\b|\bmy (?:to-?do list|tasks)\b|\bwhat (?:do i have|should i do|tasks do i have)\b", t):
            open_t = [x for x in self.data["todos"] if not x.get("done")]
            if not open_t:
                return "Your to-do list is empty."
            return f"You have {len(open_t)} open task{'s' if len(open_t) != 1 else ''}. " + \
                   " ".join(f"{i + 1}. {x['t']}." for i, x in enumerate(open_t[:8]))
        m = re.match(r"(?:mark |tick |check off |finish |complete )(?:task )?(.+?)(?: (?:as )?(?:done|complete|completed|finished))?$", t)
        if m and ("done" in t or "complete" in t or "finish" in t or "tick" in t or "check off" in t) and self.data["todos"]:
            q = m.group(1)
            best, score = None, 0.0
            for x in self.data["todos"]:
                if x.get("done"):
                    continue
                s = self._overlap(q, x["t"])
                if s > score:
                    best, score = x, s
            if best and score >= 0.5:
                with self.lock:
                    best["done"] = True
                    self._save()
                return f"Marked {best['t']} as done."
            return "I could not find that task."
        if re.search(r"\b(?:clear|delete|remove) (?:my )?(?:done|completed|finished) tasks\b", t):
            with self.lock:
                before = len(self.data["todos"])
                self.data["todos"] = [x for x in self.data["todos"] if not x.get("done")]
                self._save()
            return f"Removed {before - len(self.data['todos'])} finished tasks."
        # --- documents
        m = re.match(r"(?:search|look|find|check)(?: in)? (?:my |the )?(?:documents?|files|notes|knowledge)(?: for| about)? (.+)|"
                     r"what (?:do|does) my (?:documents?|notes|files) (?:say|know) about (.+)|"
                     r"(?:ask|search) my (?:documents?|files)[:,]? (.+)|"
                     r"find (.+) in my (?:documents?|files|notes)", t)
        if m:
            q = self._clean(next(g for g in m.groups() if g))
            return self.search_answer(q)
        if re.search(r"\b(?:re-?index|refresh|rebuild) (?:my )?(?:documents|index|knowledge)\b", t):
            n = self._build_index(force=True)
            return f"I read your documents again. I know {n} passages."
        return None

    # ------------------------------------------------------------- document search (BM25, no downloads)
    def _read_file(self, path):
        low = path.lower()
        try:
            if low.endswith((".txt", ".md")):
                with open(path, encoding="utf-8", errors="ignore") as f:
                    return f.read()
            if low.endswith(".docx"):
                import docx
                d = docx.Document(path)
                return "\n".join(p.text for p in d.paragraphs)
        except Exception:
            return ""
        return ""

    def _sources(self):
        files = []
        for d in DOC_DIRS:
            root = os.path.join(BASE_DIR, d)
            if os.path.isdir(root):
                for dp, _, names in os.walk(root):
                    for n in names:
                        if n.lower().endswith((".txt", ".md", ".docx")):
                            files.append(os.path.join(dp, n))
        return files

    def _build_index(self, force=False):
        files = self._sources()
        sig = (tuple((f, os.path.getmtime(f)) for f in files), len(self.data["notes"]), len(self.data["facts"]))
        if not force and sig == self._index_sig and self._index is not None:
            return len(self._index["chunks"])
        chunks = []
        for p in files:
            text = self._read_file(p)
            paras = [x.strip() for x in re.split(r"\n\s*\n|\r\n\s*\r\n", text) if x.strip()]
            buf = []
            for para in paras or [text]:
                buf.append(para)
                if sum(len(b.split()) for b in buf) >= 90:
                    chunks.append((os.path.basename(p), " ".join(buf)))
                    buf = []
            if buf:
                chunks.append((os.path.basename(p), " ".join(buf)))
        for n in self.data["notes"]:
            chunks.append(("your notes", n["t"]))
        for f in self.data["facts"]:
            chunks.append(("what you told me", f["t"]))
        toks = [_words(c[1]) for c in chunks]
        df = {}
        for tk in toks:
            for w in set(tk):
                df[w] = df.get(w, 0) + 1
        avg = (sum(len(t) for t in toks) / len(toks)) if toks else 1
        self._index = {"chunks": chunks, "toks": toks, "df": df, "avg": avg}
        self._index_sig = sig
        return len(chunks)

    def search(self, q, k=3):
        self._build_index()
        ix = self._index
        qw = _words(q)
        if not ix["chunks"] or not qw:
            return []
        n = len(ix["chunks"])
        scores = []
        for i, tk in enumerate(ix["toks"]):
            s = 0.0
            for w in qw:
                f = tk.count(w)
                if not f:
                    continue
                idf = math.log(1 + (n - ix["df"].get(w, 0) + 0.5) / (ix["df"].get(w, 0) + 0.5))
                s += idf * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * len(tk) / ix["avg"]))
            if s > 0:
                scores.append((s, i))
        scores.sort(reverse=True)
        return [(ix["chunks"][i][0], ix["chunks"][i][1], s) for s, i in scores[:k]]

    def search_answer(self, q):
        hits = self.search(q, 3)
        if not hits:
            return "I could not find anything about that in your documents or notes."
        top = hits[0]
        if self.generate and len(hits) and top[2] > 1.0:
            ctx = "\n\n".join(f"[{h[0]}] {h[1][:700]}" for h in hits)
            try:
                ans = self.generate(
                    "Answer the question using ONLY the passages below. If they do not contain the answer, say you could not "
                    "find it. Reply in at most two short sentences.\n\n" + ctx + f"\n\nQuestion: {q}", 120)
                ans = re.sub(r"\s+", " ", ans).strip()
                if ans:
                    return f"From {top[0]}: {ans}"
            except Exception:
                pass
        return f"From {top[0]}: " + re.sub(r"\s+", " ", top[1])[:320]
