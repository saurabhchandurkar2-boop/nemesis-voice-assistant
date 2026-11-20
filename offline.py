"""
NEMESIS offline mode: speech recognition and speech output that work with no internet.

  Hearing : the phone records your voice, sends it to the PC, and faster-whisper (running here) turns it into text.
  Speaking: the voices already installed in Windows (SAPI, e.g. "Microsoft David") make the sound,
            then Nemesis adds its robot effect on top, just like the online voice.

The models download once, while you are online:   python nemesis.py --get-offline
"""
import io
import os
import re
import subprocess
import tempfile
import threading
import time
import wave

import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(BASE_DIR, "models")
IS_WIN = os.name == "nt"
_NOWIN = 0x08000000 if IS_WIN else 0          # CREATE_NO_WINDOW

# ------------------------------------------------------------------ offline voice (Windows SAPI)
_LIST = ("Add-Type -AssemblyName System.Speech; "
         "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
         "$s.GetInstalledVoices() | Where-Object { $_.Enabled } | ForEach-Object { "
         "$_.VoiceInfo.Name + '|' + $_.VoiceInfo.Gender + '|' + $_.VoiceInfo.Culture.Name }")
_SPEAK = ("Add-Type -AssemblyName System.Speech; "
          "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          "if ($env:NEM_VOICE) { try { $s.SelectVoice($env:NEM_VOICE) } catch {} }; "
          "$s.Rate = [int]$env:NEM_RATE; $s.Volume = 100; "
          "$s.SetOutputToWaveFile($env:NEM_OUT); "
          "$s.Speak([System.IO.File]::ReadAllText($env:NEM_TEXT, [System.Text.Encoding]::UTF8)); $s.Dispose()")
_voices = None


def _powershell(script, env=None, timeout=40):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], env=e,
                          capture_output=True, text=True, timeout=timeout, creationflags=_NOWIN)


def parse_voices(text):
    out = []
    for line in (text or "").splitlines():
        parts = line.strip().split("|")
        if len(parts) == 3 and parts[0]:
            out.append({"name": parts[0], "gender": parts[1], "culture": parts[2]})
    return out


def sapi_voices(refresh=False):
    global _voices
    if _voices is not None and not refresh:
        return _voices
    _voices = []
    if IS_WIN:
        try:
            _voices = parse_voices(_powershell(_LIST, timeout=30).stdout)
        except Exception:
            _voices = []
    return _voices


def pick_voice(lang="en", voices=None):
    """Best installed Windows voice for a language: a male voice if there is one."""
    voices = sapi_voices() if voices is None else voices
    want = {"hi": "hi", "mr": "mr"}.get(lang, "en")
    pool = [v for v in voices if v["culture"].lower().startswith(want)]
    if not pool and want != "en":
        pool = [v for v in voices if v["culture"].lower().startswith("hi")] if want == "mr" else []
    if not pool:
        return None if want != "en" else None
    male = [v for v in pool if v["gender"].lower() == "male"]
    prefer = [v for v in (male or pool) if "david" in v["name"].lower() or "mark" in v["name"].lower()]
    return (prefer or male or pool)[0]["name"]


def tts_available():
    return bool(sapi_voices())


def deepen(wav_bytes, factor=1.07):
    """Slightly slower resample = lower, heavier voice."""
    with wave.open(io.BytesIO(wav_bytes)) as w:
        ch, rate, n = w.getnchannels(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
        width = w.getsampwidth()
    if width != 2 or n == 0:
        return wav_bytes
    arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    if ch > 1:
        arr = arr.reshape(-1, ch).mean(axis=1)
    idx = np.linspace(0, len(arr) - 1, int(len(arr) * factor))
    out = np.interp(idx, np.arange(len(arr)), arr).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(out.tobytes())
    return buf.getvalue()


def make_wav(text, lang="en", rate=-1, deeper=1.07):
    """Text -> path of a WAV file made with a Windows voice. Raises RuntimeError if impossible."""
    if not IS_WIN:
        raise RuntimeError("the offline voice needs Windows")
    voice = pick_voice(lang)
    if voice is None and sapi_voices():
        voice = sapi_voices()[0]["name"]
    if not sapi_voices():
        raise RuntimeError("no Windows voice is installed")
    tmp = tempfile.gettempdir()
    stamp = time.time_ns()
    txt, out = os.path.join(tmp, f"nem_{stamp}.txt"), os.path.join(tmp, f"nem_{stamp}.wav")
    try:
        with open(txt, "w", encoding="utf-8") as f:
            f.write(text)
        r = _powershell(_SPEAK, {"NEM_VOICE": voice or "", "NEM_RATE": str(rate), "NEM_OUT": out, "NEM_TEXT": txt})
        if not os.path.exists(out) or os.path.getsize(out) < 200:
            raise RuntimeError((r.stderr or "Windows voice made no sound").strip()[:200])
        if deeper and deeper != 1.0:
            with open(out, "rb") as f:
                data = f.read()
            with open(out, "wb") as f:
                f.write(deepen(data, deeper))
        return out
    finally:
        try:
            os.remove(txt)
        except OSError:
            pass


# ------------------------------------------------------------------ offline hearing (faster-whisper)
GHOSTS = {"thank you", "thank you.", "thanks for watching", "thanks for watching.", "you", "you.", "bye", "bye.",
          "the end", "the end.", "subtitles by the amara.org community", "."}


def model_name(lang):
    return "base.en" if lang == "en" else "base"


class Ears:
    """Loads the right Whisper model on demand and turns phone audio into text."""

    def __init__(self, log=print):
        self.log = log
        self.models = {}
        self.lock = threading.Lock()

    def _load(self, name, allow_download):
        if name in self.models:
            return self.models[name]
        from faster_whisper import WhisperModel
        os.makedirs(MODEL_DIR, exist_ok=True)
        try:
            m = WhisperModel(name, device="cpu", compute_type="int8", download_root=MODEL_DIR, local_files_only=True)
        except Exception:
            if not allow_download:
                raise RuntimeError("The offline hearing model is not downloaded yet. While you are online, run in "
                                   "PowerShell: python nemesis.py --get-offline")
            self.log("sys", f"Downloading the speech model {name} (one time)")
            m = WhisperModel(name, device="cpu", compute_type="int8", download_root=MODEL_DIR)
        self.models[name] = m
        return m

    def ready(self, lang="en"):
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return False, "faster-whisper is not installed. Run: pip install faster-whisper"
        try:
            self._load(model_name(lang), False)
            return True, ""
        except Exception as e:
            return False, str(e)

    def transcribe(self, pcm16, lang="en", allow_download=False):
        """pcm16: bytes of 16 kHz mono signed 16-bit audio."""
        audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        if audio.size < 4000 or float(np.sqrt(np.mean(audio ** 2))) < 0.003:
            return ""
        with self.lock:
            model = self._load(model_name(lang), allow_download)
            kw = dict(beam_size=1, vad_filter=True, condition_on_previous_text=False, temperature=0.0)
            if lang != "en":
                kw["language"] = lang
            segs, _ = model.transcribe(audio, **kw)
            text = " ".join(s.text.strip() for s in segs).strip()
        if text.lower() in GHOSTS and audio.size < 16000 * 2:
            return ""
        return re.sub(r"\s+", " ", text)


def download_models(log=print):
    """Run once while online: fetch the hearing models and check the Windows voices."""
    ears = Ears(log)
    for lang in ("en", "hi"):
        name = model_name(lang)
        log("sys", f"Getting speech model {name} ...")
        try:
            ears._load(name, True)
            log("sys", f"  {name}: ready")
        except Exception as e:
            log("sys", f"  {name}: failed - {e}")
    v = sapi_voices(True)
    log("sys", "Windows voices: " + (", ".join(x["name"] for x in v) if v else "none found"))
