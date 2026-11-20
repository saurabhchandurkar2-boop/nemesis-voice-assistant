"""
NEMESIS eyes: object detection + tracking.

Frames come from the phone camera (EYES button on the phone page) or from a
PC webcam (python nemesis.py --cam). Detection uses YOLOv8-nano (80 everyday
object types) through the 'ultralytics' package. Everything runs on the PC.
"""
import os
import re
import threading
import time

import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(BASE_DIR, "models")

SYNONYMS = {
    "people": "person", "persons": "person", "man": "person", "woman": "person", "human": "person",
    "humans": "person", "boy": "person", "girl": "person", "face": "person",
    "phone": "cell phone", "mobile": "cell phone", "smartphone": "cell phone", "cellphone": "cell phone",
    "television": "tv", "monitor": "tv", "screen": "tv", "computer": "laptop", "bike": "bicycle",
    "motorbike": "motorcycle", "couch": "couch", "sofa": "couch", "glass": "wine glass", "plant": "potted plant",
    "bag": "handbag", "rucksack": "backpack", "table": "dining table", "doll": "teddy bear",
    "remote control": "remote", "mouse": "mouse", "keyboard": "keyboard", "pen": "scissors",
}


def canon(word):
    """Spoken word -> COCO class name."""
    w = word.lower().strip(" .?!")
    w = re.sub(r"^(?:the|a|an|my|that|this)\s+", "", w)
    if w in SYNONYMS:
        return SYNONYMS[w]
    if w.endswith("ies"):
        return w[:-3] + "y"
    if w.endswith("s") and w[:-1] in COCO_NAMES:
        return w[:-1]
    if w.endswith("es") and w[:-2] in COCO_NAMES:
        return w[:-2]
    return w


COCO_NAMES = {
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat", "traffic light",
    "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove", "skateboard", "surfboard",
    "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
    "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard",
    "cell phone", "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase",
    "scissors", "teddy bear", "hair drier", "toothbrush"}


def position_word(cx, cy):
    h = "left" if cx < 0.33 else "right" if cx > 0.66 else "center"
    v = "top" if cy < 0.33 else "bottom" if cy > 0.66 else ""
    if h == "center" and not v:
        return "in the center"
    if h == "center":
        return f"at the {v}"
    return f"on the {v + ' ' if v else ''}{h}"


def plural(label, n):
    if n == 1:
        return f"a {label}" if label[0] not in "aeiou" else f"an {label}"
    irregular = {"person": "people", "knife": "knives", "sheep": "sheep", "scissors": "scissors"}
    return f"{n} {irregular.get(label, label + ('es' if label.endswith(('s', 'x', 'ch', 'sh')) else 's'))}"


class Vision:
    def __init__(self, hub, say, conf=0.35, imgsz=320):
        self.hub, self.say = hub, say
        self.conf, self.imgsz = conf, imgsz
        self.model = None
        self.error = None
        self.tracks = {}            # id -> dict(label, box, seen)
        self.next_id = 1
        self.objects = []           # latest detections [{id,label,conf,box(0-1)}]
        self.size = (1, 1)
        self.last_seen = 0.0        # when the last frame was analysed
        self.last_frame = None      # newest picture analysed (for colours)
        self.last_text = ""         # last text read from the camera
        self.target = None          # label being tracked, e.g. "person"
        self._target_visible = None
        self._target_change = 0.0
        self._lock = threading.Lock()

    # ----- model -------------------------------------------------------
    def load(self):
        if self.model or self.error:
            return
        try:
            from ultralytics import YOLO
        except ImportError:
            self.error = "Vision is not installed. Run in the terminal: pip install ultralytics"
            return
        try:
            os.makedirs(MODEL_DIR, exist_ok=True)
            path = os.path.join(MODEL_DIR, "yolov8n.pt")
            self.hub.log("sys", "Loading vision model (first time downloads about 6 MB)")
            self.model = YOLO(path if os.path.exists(path) else "yolov8n.pt")
            if not os.path.exists(path) and os.path.exists("yolov8n.pt"):
                try:
                    os.replace("yolov8n.pt", path)
                except OSError:
                    pass
            self.hub.log("sys", "Vision ready")
        except Exception as e:
            self.error = f"Vision model failed to load: {e}"
            print(f"[vision] {self.error}")

    def decode(self, jpeg):
        import cv2
        arr = np.frombuffer(jpeg, np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)

    def detect(self, frame):
        """Returns [(label, conf, (x1, y1, x2, y2) in pixels)]."""
        res = self.model.predict(frame, imgsz=self.imgsz, conf=self.conf, verbose=False, device="cpu")[0]
        out = []
        names = res.names
        for b in res.boxes:
            x1, y1, x2, y2 = [float(v) for v in b.xyxy[0]]
            out.append((names[int(b.cls[0])], float(b.conf[0]), (x1, y1, x2, y2)))
        return out

    # ----- tracking ----------------------------------------------------
    def _assign(self, dets, w, h, now):
        """Give every detection a steady id (nearest same-label centre)."""
        objs, used = [], set()
        for label, conf, (x1, y1, x2, y2) in sorted(dets, key=lambda d: -d[1]):
            box = (x1 / w, y1 / h, x2 / w, y2 / h)
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            best, bd = None, 0.18
            for tid, tr in self.tracks.items():
                if tid in used or tr["label"] != label:
                    continue
                tx, ty = (tr["box"][0] + tr["box"][2]) / 2, (tr["box"][1] + tr["box"][3]) / 2
                d = ((tx - cx) ** 2 + (ty - cy) ** 2) ** 0.5
                if d < bd:
                    best, bd = tid, d
            if best is None:
                best = self.next_id
                self.next_id += 1
            used.add(best)
            self.tracks[best] = {"label": label, "box": box, "seen": now}
            objs.append({"id": best, "label": label, "conf": round(conf, 2), "box": [round(v, 3) for v in box]})
        for tid in [t for t, tr in self.tracks.items() if now - tr["seen"] > 2.0]:
            del self.tracks[tid]
        return objs

    def analyse(self, frame_or_jpeg, size=None):
        """Run detection on one frame. Returns the object list (also stored)."""
        frame = frame_or_jpeg
        if isinstance(frame_or_jpeg, (bytes, bytearray)):
            frame = self.decode(frame_or_jpeg)
        if frame is None:
            return []
        h, w = frame.shape[:2]
        dets = self.detect(frame)
        now = time.time()
        with self._lock:
            self.objects = self._assign(dets, w, h, now)
            self.size, self.last_seen = (w, h), now
            self.last_frame = frame
        self._update_target(now)
        self._publish()
        return self.objects

    def _update_target(self, now):
        if not self.target:
            return
        visible = any(o["label"] == self.target for o in self.objects)
        if self._target_visible is None:
            self._target_visible = visible
        elif visible != self._target_visible and now - self._target_change > 3:
            self._target_visible, self._target_change = visible, now
            self.say(f"Target {self.target} acquired." if visible else f"Target {self.target} lost.", "both")

    def _publish(self):
        counts = {}
        for o in self.objects:
            counts[o["label"]] = counts.get(o["label"], 0) + 1
        text = ", ".join(f"{n} {l}" for l, n in counts.items()) or "nothing"
        self.hub.set_info(sees=text)
        self.hub.emit(t="det", objs=self.objects, target=self.target)

    # ----- questions ---------------------------------------------------
    def _fresh(self):
        if self.error:
            return self.error
        if time.time() - self.last_seen > 6:
            return "My eyes are closed. Tap EYES on the phone page, or start me with the cam flag."
        return None

    def describe(self):
        bad = self._fresh()
        if bad:
            return bad
        if not self.objects:
            return "I do not see anything I recognise."
        counts = {}
        for o in self.objects:
            counts[o["label"]] = counts.get(o["label"], 0) + 1
        parts = [plural(l, n) for l, n in sorted(counts.items(), key=lambda x: -x[1])]
        text = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
        return f"I can see {text}."

    def centre_object(self):
        bad = self._fresh()
        if bad:
            return bad
        if not self.objects:
            return "I do not see anything I recognise."
        def score(o):
            b = o["box"]
            cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
            area = (b[2] - b[0]) * (b[3] - b[1])
            return ((cx - .5) ** 2 + (cy - .5) ** 2) - area * 0.5
        o = min(self.objects, key=score)
        return f"That looks like {plural(o['label'], 1)}, I am {int(o['conf'] * 100)} percent sure."

    def count(self, word):
        bad = self._fresh()
        if bad:
            return bad
        label = canon(word)
        n = sum(1 for o in self.objects if o["label"] == label)
        if n == 0:
            return f"I do not see any {label}."
        return f"I can see {plural(label, n)}."

    def where(self, word):
        bad = self._fresh()
        if bad:
            return bad
        label = canon(word)
        found = [o for o in self.objects if o["label"] == label]
        if not found:
            return f"I cannot see {plural(label, 1)} right now."
        parts = []
        for o in found[:3]:
            b = o["box"]
            parts.append(position_word((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) + ", " + self.distance_word(b))
        return f"The {label} is " + " and ".join(parts) + "." if len(found) == 1 else \
            f"I see {plural(label, len(found))}: " + ", ".join(parts) + "."

    def track(self, word):
        label = canon(word)
        if label not in COCO_NAMES:
            return f"I cannot recognise {word} yet. I know everyday things like person, cup, bottle, laptop, phone, book, chair and car."
        self.target, self._target_visible = label, None
        self._publish()
        return f"Tracking {label}. I will tell you when it appears or disappears."


    # ----- v11: colours, distance, careful look, reading text ----------
    @staticmethod
    def colour_name(crop):
        """Plain colour name for the middle of a picture patch (BGR numpy array)."""
        import cv2
        if crop is None or crop.size == 0:
            return None
        h, w = crop.shape[:2]
        crop = crop[int(h * .25):int(h * .75) or 1, int(w * .25):int(w * .75) or 1]
        if crop.size == 0:
            return None
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV).reshape(-1, 3)
        hh, ss, vv = (float(np.median(hsv[:, i])) for i in range(3))
        if vv < 55:
            return "black"
        if ss < 45:
            return "white" if vv > 200 else "light gray" if vv > 140 else "dark gray" if vv > 90 else "black"
        if hh < 8 or hh >= 172:
            return "dark red" if vv < 120 else "red"
        if hh < 20:
            return "brown" if vv < 160 else "orange"
        if hh < 35:
            return "yellow" if vv >= 130 else "olive"
        if hh < 85:
            return "green"
        if hh < 100:
            return "teal"
        if hh < 130:
            return "blue"
        if hh < 160:
            return "purple"
        return "pink" if ss < 170 else "magenta"

    def _colour_of_obj(self, o):
        fr = self.last_frame
        if fr is None:
            return None
        h, w = fr.shape[:2]
        b = o["box"]
        try:
            return self.colour_name(fr[int(b[1] * h):int(b[3] * h), int(b[0] * w):int(b[2] * w)])
        except Exception:
            return None

    @staticmethod
    def distance_word(box):
        area = (box[2] - box[0]) * (box[3] - box[1])
        return "very close" if area > .35 else "close" if area > .10 else "a little far" if area > .02 else "far away"

    def describe_detailed(self):
        bad = self._fresh()
        if bad:
            return bad
        if not self.objects:
            return "I do not see anything I recognise."
        objs = sorted(self.objects, key=lambda o: -((o["box"][2] - o["box"][0]) * (o["box"][3] - o["box"][1])))[:5]
        parts = []
        for o in objs:
            b = o["box"]
            col = self._colour_of_obj(o)
            name = plural(o["label"], 1)
            if col:
                name = name.replace("a ", f"a {col} ", 1) if name.startswith("a ") else name.replace("an ", f"a {col} ", 1)
            parts.append(f"{name} {position_word((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)}, {self.distance_word(b)}")
        more = len(self.objects) - len(objs)
        tail = f" And {more} more smaller things." if more > 0 else ""
        return "I can see " + "; ".join(parts) + "." + tail

    def colour_of(self, word=None):
        bad = self._fresh()
        if bad:
            return bad
        if not self.objects:
            return "I do not see anything I recognise."
        if word:
            label = canon(word)
            found = [o for o in self.objects if o["label"] == label]
            if not found:
                return f"I cannot see {plural(label, 1)} right now."
            o = max(found, key=lambda o: (o["box"][2] - o["box"][0]) * (o["box"][3] - o["box"][1]))
        else:
            def score(o):
                b = o["box"]
                return ((((b[0] + b[2]) / 2 - .5) ** 2 + ((b[1] + b[3]) / 2 - .5) ** 2)
                        - (b[2] - b[0]) * (b[3] - b[1]) * 0.5)
            o = min(self.objects, key=score)
        col = self._colour_of_obj(o)
        if not col:
            return "I could not tell the colour."
        return f"The {o['label']} looks {col}."

    def _still(self, wait=5.0):
        """(picture, sharp?) - a fresh sharp picture from the phone, or the newest small one."""
        if getattr(self.hub, "privacy", False):
            return None, False
        jpeg = self.hub.request_still(wait) if hasattr(self.hub, "request_still") else None
        if jpeg:
            fr = self.decode(jpeg)
            if fr is not None:
                return fr, True
        if self.last_frame is not None and time.time() - self.last_seen < 6:
            return self.last_frame, False
        return None, False

    def look_carefully(self):
        """One slower, more accurate pass on a sharp picture."""
        if getattr(self.hub, "privacy", False):
            return "Privacy mode is on, so my camera is blocked. Say privacy mode off first."
        self.load()
        if not self.model:
            return self.error or "My eyes are not ready."
        frame, sharp = self._still(4.0)
        if frame is None:
            return "I could not get a picture. Check that EYES is on."
        old = (self.conf, self.imgsz)
        self.conf, self.imgsz = 0.25, 640
        try:
            h, w = frame.shape[:2]
            dets = self.detect(frame)
            now = time.time()
            with self._lock:
                self.objects = self._assign(dets, w, h, now)
                self.size, self.last_seen, self.last_frame = (w, h), now, frame
        finally:
            self.conf, self.imgsz = old
        self._publish()
        return self.describe_detailed()

    # --- reading text from the camera ---
    @staticmethod
    def _ocr_score(data):
        words = [(t, float(c)) for t, c in zip(data["text"], data["conf"]) if t.strip() and float(c) > 0]
        if not words:
            return 0.0, 0, ""
        good = [w for w in words if sum(ch.isalnum() for ch in w[0]) >= max(1, len(w[0]) // 2)]
        if not good:
            return 0.0, 0, ""
        conf = sum(c * len(t) for t, c in good) / sum(len(t) for t, c in good)
        return conf * min(1.0, len(good) / 3.0), len(good), conf

    def read_text(self, prefer=None):
        """Read printed text in the camera picture. Tries a few clean-ups and keeps the best."""
        import cv2
        import tools
        exe = tools.tesseract_cmd()
        if not exe:
            return tools.OCR_HELP
        try:
            import pytesseract
        except ImportError:
            return "I need the pytesseract package. Run: pip install pytesseract"
        pytesseract.pytesseract.tesseract_cmd = exe
        if getattr(self.hub, "privacy", False):
            return "Privacy mode is on, so my camera is blocked. Say privacy mode off first."
        frame, sharp = self._still(5.0)
        if frame is None:
            return "I could not get a picture. Tap EYES on the phone page, then hold the text in front of the camera."
        try:
            have = set(pytesseract.get_languages())
        except Exception:
            have = {"eng"}
        want = ["eng"] + ([l for l in ("hin", "mar") if l in have] if prefer in ("hi", "mr") else [])
        langs = "+".join(l for l in want if l in have) or "eng"

        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if g.shape[1] < 1800:
            f = 1800 / g.shape[1]
            g = cv2.resize(g, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
        elif g.shape[1] > 2400:
            f = 2400 / g.shape[1]
            g = cv2.resize(g, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
        clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(g)
        thresh = cv2.adaptiveThreshold(cv2.GaussianBlur(clahe, (3, 3), 0), 255,
                                       cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 35, 12)
        best = (0.0, 0, 0.0, "", None)           # score, words, conf, text, image

        def attempt(img, psm=3):
            nonlocal best
            try:
                data = pytesseract.image_to_data(img, lang=langs, config=f"--psm {psm}", output_type=pytesseract.Output.DICT)
            except Exception:
                return
            sc, n, conf = self._ocr_score(data)
            if sc > best[0]:
                text = pytesseract.image_to_string(img, lang=langs, config=f"--psm {psm}")
                best = (sc, n, conf, text, img)

        attempt(clahe)
        if best[0] < 70:
            attempt(thresh)
        if best[0] < 45:                       # maybe the page is sideways or upside down
            base = best[4] if best[4] is not None else clahe
            for rot in (cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_90_COUNTERCLOCKWISE, cv2.ROTATE_180):
                attempt(cv2.rotate(base, rot))
                if best[0] >= 60:
                    break
        score, n, conf, text, _ = best
        text = re.sub(r"[ \t]+", " ", text)
        text = "\n".join(ln for ln in text.splitlines() if ln.strip() and sum(c.isalnum() for c in ln) >= len(ln.strip()) * 0.5)
        text = re.sub(r"\n{2,}", "\n", text).strip()
        if not text or n == 0:
            return ("I could not find any readable text. Hold the page flat, closer, in good light, "
                    "and say read this again.")
        words = len(text.split())
        copied = tools.to_clipboard(text)
        spoken = re.sub(r"\s+", " ", text)[:380]
        unsure = "" if conf >= 60 else " I am not fully sure, because the picture may be blurry or dark. I think it says: "
        lead = f"I read {words} words." + (unsure or " ")
        more = (" The full text is copied to your clipboard." if copied else "") if (len(text) > 380 or copied) else ""
        if not sharp:
            more += " This was a small picture. For better reading, hold the page closer."
        self.last_text = text
        return f"{lead}{spoken}{'...' if len(text) > 380 else ''}{more}"

    def stop_tracking(self):
        self.target = None
        self._publish()
        return "Tracking stopped."


def webcam_loop(vision, stop_flag=lambda: False, fps=3):
    """Optional: read a PC webcam (python nemesis.py --cam)."""
    try:
        import cv2
    except ImportError:
        print("[cam] needs opencv: pip install ultralytics")
        return
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("[cam] no PC webcam found - use the phone camera (EYES button) instead")
        return
    vision.load()
    while not stop_flag():
        ok, frame = cap.read()
        if ok and vision.model:
            try:
                vision.analyse(frame)
            except Exception as e:
                print(f"[cam error] {e}")
        time.sleep(1.0 / fps)


def frame_loop(vision, hub):
    """Analyse the newest frame the phone uploaded (skips any backlog)."""
    done = 0
    while True:
        if hub.frame_id == done:
            time.sleep(0.05)
            continue
        done, jpeg = hub.frame_id, hub.frame
        if hub.state in ("thinking", "speaking"):      # the brain gets the whole CPU while it answers
            continue
        try:
            vision.load()
            if vision.model:
                vision.analyse(jpeg)
        except Exception as e:
            print(f"[vision error] {e}")
            time.sleep(1)
