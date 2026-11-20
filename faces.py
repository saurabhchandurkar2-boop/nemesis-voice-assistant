"""
NEMESIS faces: face detection + recognising the people YOU enrol.

 * Detection and matching run only on this PC (OpenCV YuNet + SFace, small free models).
 * Nemesis only recognises people who were enrolled on purpose ("remember my face as Yash").
   Everyone else is just "a face I do not know". It never guesses a name for a stranger.
 * Saved faces are stored as numbers (not photos), encrypted in faces.dat with the same key as the memory file.
 * "forget my face" / "forget all faces" removes them.
 * Face matching is a convenience, not a lock. Do not use it to protect anything important.
"""
import json
import os
import re
import threading
import time
import urllib.request

import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(BASE_DIR, "models")
DATA_FILE = os.path.join(BASE_DIR, "faces.dat")
KEY_FILE = os.path.join(BASE_DIR, "certs", "memory.key")

MODELS = {
    "face_detection_yunet_2023mar.onnx": (
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx", 100_000),
    "face_recognition_sface_2021dec.onnx": (
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx", 30_000_000),
}
MATCH = 0.45          # cosine similarity needed to call a face "known" (the model's own default is 0.363: this is stricter)
MARGIN = 0.05         # the best match must beat the second best person by this much
MIN_FACE_PX = 70      # smaller faces are too blurry to trust
MAX_SAMPLES = 12      # per person
ENROL_SHOTS = 6
SHOT_GAP = 0.8        # seconds between enrolment pictures
MANUAL_HELP = ("Download these two files in a browser and put them in the models folder inside the Nemesis folder: "
               + " and ".join(u for u, _ in MODELS.values()))


def clean_name(raw):
    name = re.sub(r"[^A-Za-z .'-]", "", raw or "").strip(" .'-")
    name = re.sub(r"\s+", " ", name)
    if not name or len(name) > 24:
        return None
    return name.title()


def _unit(v):
    v = np.asarray(v, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(v))
    return v / n if n else v


class CvBackend:
    """OpenCV YuNet (finding faces) + SFace (turning a face into 128 numbers)."""
    def __init__(self, log=print):
        self.log = log
        self.det = None
        self.rec = None
        self.error = None
        self._lock = threading.Lock()

    def _ensure_files(self):
        os.makedirs(MODEL_DIR, exist_ok=True)
        for name, (url, minsize) in MODELS.items():
            path = os.path.join(MODEL_DIR, name)
            if os.path.exists(path) and os.path.getsize(path) >= minsize:
                continue
            self.log(f"Downloading face model {name} (one time)")
            tmp = path + ".part"
            with urllib.request.urlopen(url, timeout=90) as r, open(tmp, "wb") as f:
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
            if os.path.getsize(tmp) < minsize:
                os.remove(tmp)
                raise IOError(f"{name} download was incomplete")
            os.replace(tmp, path)

    def load(self):
        with self._lock:
            if self.det and self.rec:
                return None
            if self.error:
                return self.error
            try:
                import cv2
            except ImportError:
                self.error = "Face features need OpenCV. Run in the terminal: pip install opencv-python"
                return self.error
            if not (hasattr(cv2, "FaceDetectorYN") and hasattr(cv2, "FaceRecognizerSF")):
                self.error = "Your OpenCV is too old for faces. Run in the terminal: pip install --upgrade opencv-python"
                return self.error
            try:
                self._ensure_files()
            except Exception as e:
                msg = f"I could not download the face models ({type(e).__name__}). Check the internet and try again. {MANUAL_HELP}"
                self.log(f"[faces] {msg}")
                return msg                     # not remembered: the next try downloads again
            try:
                self.det = cv2.FaceDetectorYN.create(os.path.join(MODEL_DIR, "face_detection_yunet_2023mar.onnx"),
                                                     "", (320, 320), 0.8, 0.3, 50)
                self.rec = cv2.FaceRecognizerSF.create(os.path.join(MODEL_DIR, "face_recognition_sface_2021dec.onnx"), "")
            except Exception as e:
                self.error = f"The face models failed to load: {e}"
                self.log(f"[faces] {self.error}")
                return self.error
            return None

    def prepare(self, frame, max_w=640):
        import cv2
        h, w = frame.shape[:2]
        if w > max_w:
            frame = cv2.resize(frame, (max_w, int(h * max_w / w)))
        return frame

    def detect(self, frame):
        """frame already prepared -> list of face rows (numpy, 15 numbers each)."""
        h, w = frame.shape[:2]
        self.det.setInputSize((w, h))
        _, faces = self.det.detect(frame)
        return [] if faces is None else [f for f in faces]

    def embed(self, frame, row):
        aligned = self.rec.alignCrop(frame, row)
        return _unit(self.rec.feature(aligned))


class Faces:
    def __init__(self, backend=None, log=print, data_file=DATA_FILE, key_file=KEY_FILE):
        self.backend = backend or CvBackend(log)
        self.log = log
        self.data_file, self.key_file = data_file, key_file
        self.lock = threading.RLock()
        self.people = {}                  # name -> list of unit vectors
        self._load()

    # ----- encrypted storage ------------------------------------------
    def _fernet(self):
        try:
            from cryptography.fernet import Fernet
            os.makedirs(os.path.dirname(self.key_file), exist_ok=True)
            if os.path.exists(self.key_file):
                with open(self.key_file, "rb") as f:
                    key = f.read().strip()
            else:
                key = Fernet.generate_key()
                with open(self.key_file, "wb") as f:
                    f.write(key)
            return Fernet(key)
        except Exception:
            return None

    def _load(self):
        try:
            with open(self.data_file, "rb") as f:
                raw = f.read()
        except OSError:
            return
        fer = self._fernet()
        if not fer:
            return
        try:
            data = json.loads(fer.decrypt(raw).decode("utf-8"))
            self.people = {n: [_unit(v) for v in vs] for n, vs in data.get("people", {}).items()}
        except Exception as e:
            self.log(f"[faces] could not read faces.dat: {type(e).__name__}")

    def _save(self):
        fer = self._fernet()
        if not fer:
            return False
        blob = json.dumps({"people": {n: [v.tolist() for v in vs] for n, vs in self.people.items()}}).encode("utf-8")
        tmp = self.data_file + ".tmp"
        try:
            with open(tmp, "wb") as f:
                f.write(fer.encrypt(blob))
            os.replace(tmp, self.data_file)
            return True
        except OSError:
            return False

    # ----- helpers -----------------------------------------------------
    def names(self):
        with self.lock:
            return sorted(self.people)

    def _match(self, vec):
        """-> (name or None, score)."""
        with self.lock:
            scores = []
            for name, vs in self.people.items():
                scores.append((max(float(np.dot(vec, v)) for v in vs), name))
        if not scores:
            return None, 0.0
        scores.sort(reverse=True)
        best, name = scores[0]
        if best < MATCH:
            return None, best
        if len(scores) > 1 and best - scores[1][0] < MARGIN:
            return None, best
        return name, best

    # ----- what Nemesis says -----------------------------------------
    def count_text(self, frame):
        err, img, rows = self._all_faces(frame)
        if err:
            return err
        n = len(rows)
        if n == 0:
            return "I do not see a face. Face the camera in good light."
        return "I see one face." if n == 1 else f"I see {n} faces."

    def _all_faces(self, frame):
        err = self.backend.load()
        if err:
            return err, None, []
        img = self.backend.prepare(frame)
        return None, img, self.backend.detect(img)

    def who_text(self, frame):
        err, img, rows = self._all_faces(frame)
        if err:
            return err
        if not rows:
            return "I do not see a face. Face the camera in good light."
        if not self.people:
            return ("I see one face, but I do not know any faces yet. Say remember my face as, then your name."
                    if len(rows) == 1 else
                    f"I see {len(rows)} faces, but I do not know any faces yet.")
        known, unknown = [], 0
        for r in rows:
            if min(float(r[2]), float(r[3])) < MIN_FACE_PX:
                unknown += 1
                continue
            name, _ = self._match(self.backend.embed(img, r))
            if name:
                if name not in known:
                    known.append(name)
            else:
                unknown += 1
        parts = list(known)
        if unknown:
            parts.append("one face I do not know" if unknown == 1 else f"{unknown} faces I do not know")
        if len(parts) == 1:
            return f"I see {parts[0]}."
        return "I see " + ", ".join(parts[:-1]) + " and " + parts[-1] + "."

    def enrol(self, name, get_frame, say=lambda *a: None, wait=18.0):
        """Take several pictures of ONE face and remember it. get_frame() -> (frame, stamp)."""
        name = clean_name(name)
        if not name:
            return "Say remember my face as, then a name. Use letters only."
        err = self.backend.load()
        if err:
            return err
        say(f"Look at the camera, {name}. Hold still and turn your head a little.", "both")
        samples, last_stamp, t_end, last_take = [], None, time.time() + wait, 0.0
        while time.time() < t_end and len(samples) < ENROL_SHOTS:
            frame, stamp = get_frame()
            if frame is None or stamp == last_stamp or time.time() - last_take < SHOT_GAP:
                time.sleep(0.1)
                continue
            last_stamp = stamp
            img = self.backend.prepare(frame)
            rows = self.backend.detect(img)
            if len(rows) != 1:
                continue                                   # nobody, or more than one person: wait
            r = rows[0]
            if min(float(r[2]), float(r[3])) < MIN_FACE_PX:
                continue                                   # too far away
            samples.append(self.backend.embed(img, r))
            last_take = time.time()
        if len(samples) < 3:
            return "I could not get a clear picture. Make sure only one person is in front of the camera, closer, in good light. Then try again."
        with self.lock:
            existing = self.people.get(name, [])
            self.people[name] = (existing + samples)[-MAX_SAMPLES:]
        if not self._save():
            with self.lock:
                self.people.pop(name, None) if not existing else self.people.__setitem__(name, existing)
            return "I could not save that securely. Faces were not saved."
        return f"Done. I will recognise {name} now. Say who am I to test it."

    def forget(self, name=None):
        with self.lock:
            if name is None or name == "all":
                n = len(self.people)
                self.people = {}
                self._save()
                return "I forgot every saved face." if n else "I have no saved faces."
            key = clean_name(name)
            hit = next((k for k in self.people if k.lower() == (key or "").lower()), None)
            if not hit:
                return f"I do not have a saved face for {key or name}."
            del self.people[hit]
            self._save()
            return f"I forgot the face of {hit}."

    def list_text(self):
        n = self.names()
        if not n:
            return "I have no saved faces."
        return "I know " + ", ".join(n[:-1]) + (" and " if len(n) > 1 else "") + n[-1] + "."
