"""
NEMESIS v12 - voice assistant for Windows.  Free / local brain (Ollama).

Flow:  you speak (phone or PC mic) -> Nemesis thinks (Ollama) -> Nemesis speaks

DEFAULT = PHONE MODE: your Android phone is the microphone (and can play the
replies through any headphones connected to the phone). The PC is the brain.
Open the phone link shown in this window (or scan the QR code on the HUD).

Flags:  --pc-mic  use the PC microphone instead (needs a working mic)
        --setup   choose the PC speaker (and mic with --pc-mic) again
        --no-hud  do not open the HUD page in the browser
        --http    use the old unencrypted phone link (not recommended)
        --check   run the health check and exit
        --cam     also use a PC webcam as eyes (the phone camera works without it)
"""
import asyncio
import concurrent.futures
import hashlib
import collections
import ctypes
import datetime
import difflib
import io
import json
import os
import queue
import random
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import urllib.request
import wave
import xml.etree.ElementTree as ET
import webbrowser

import edge_tts
import numpy as np
import ollama
import pygame
import sounddevice as sd
from pygame._sdl2.audio import get_audio_device_names

from hud_server import FAILS, SESSIONS, STOP_RE, hub, phone_url, revoke_all, set_key, start_hud, tls_available
from vision import Vision, frame_loop, webcam_loop
import core
import guard
import home
import faces
import offline
import tools
from mind import Mind

# ----------------------------- settings ---------------------------------
NAME = "Nemesis"
VERSION = 16
WAKE_WORDS = ("nemesis", "nemisis", "nemeses", "nemesys")

SMART_MODEL = "llama3.2:3b"        # better answers, slower
FAST_MODEL = "llama3.2:1b"         # about twice as fast, simpler answers
OLLAMA_MODEL = SMART_MODEL         # the real choice is made at start-up (say "fast mode" / "smart mode")
WHISPER_MODEL = "base.en"          # only used with --pc-mic

VOICE = "en-US-GuyNeural"          # other male voices: en-GB-RyanNeural, en-US-AndrewNeural, en-IN-PrabhatNeural
VOICE_STYLE = "smooth"             # "smooth" = deep and clean, "robot" = metallic machine, "plain" = normal
VOICE_STYLES = {                   # (pitch, speed, metallic effect 0.0-0.6)
    "smooth": ("-12Hz", "-3%", 0.0),
    "robot":  ("-25Hz", "-8%", 0.25),
    "plain":  ("+0Hz", "+0%", 0.0),
}
VOICE_PITCH, VOICE_RATE, ROBOT_AMOUNT = VOICE_STYLES.get(VOICE_STYLE, VOICE_STYLES["smooth"])
ROBOT_EFFECT = ROBOT_AMOUNT > 0

SAMPLE_RATE = 16000
AUTO_CALIBRATE = True              # (--pc-mic) measure room noise at startup
SILENCE_THRESHOLD = 0.006          # (--pc-mic) used if calibration fails
SILENCE_SECONDS = 0.9              # (--pc-mic) pause that ends your sentence
CONVERSATION_TIMEOUT = 8           # (--pc-mic) quiet seconds before sleeping
PHONE_AWAKE_SECONDS = 30           # hands-free: stays awake this long after a reply

LOCAL_MIC = "--pc-mic" in sys.argv
HUD_OPEN_BROWSER = "--no-hud" not in sys.argv
HUD_PORT = 8765

for _stream in (sys.stdout, sys.stderr):      # Hindi text must never crash a print()
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "nemesis_config.json")
MEMORY_FILE = os.path.join(BASE_DIR, "nemesis_memory.json")
NOTES_FILE = os.path.join(BASE_DIR, "nemesis_notes.txt")
REM_FILE = os.path.join(BASE_DIR, "nemesis_reminders.json")
ERROR_LOG = os.path.join(BASE_DIR, "nemesis_error.log")
SHOT_DIR = os.path.join(BASE_DIR, "screenshots")

SYSTEM_PROMPT = (
    f"You are {NAME}, a calm, precise voice assistant with a dry sense of humor. "
    "Replies are spoken aloud: answer in one to three short sentences. "
    "No markdown, lists, emojis or asterisks. "
    "You cannot open apps, play music or change settings by yourself in a conversation; "
    "those are separate commands handled before you. Never say you are doing an action. "
    "If asked to do one, say briefly how to ask for it. Never invent facts about the user."
)


# ----------------------------- small helpers ----------------------------
def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


_save_lock = threading.Lock()


def save_json(path, data):
    """Write the file in one piece so a crash can never leave half a file."""
    with _save_lock:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)


def get_access_key():
    """A 6-digit secret that phones need (so strangers on Wi-Fi cannot use Nemesis)."""
    cfg = load_json(CONFIG_FILE, {})
    if len(str(cfg.get("key", ""))) < 12:      # old 6-digit keys are replaced by a strong one
        cfg["key"] = "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(14))
        save_json(CONFIG_FILE, cfg)
    return cfg["key"]


OFFLINE = bool(load_json(CONFIG_FILE, {}).get("offline", False))     # v12: work without the internet

# ----------------------------- audio devices ----------------------------
INPUT_DEVICE = None   # sounddevice index, or None = Windows default
MIC_NAME = "Phone microphone"
SPEAKER_NAME = "Windows default"
PC_AUDIO = True       # False if the PC has no usable audio output

speak_q = queue.Queue()      # text waiting to be turned into a voice
ready_q = queue.Queue()      # voices being made (kept in order)
play_q = queue.Queue()       # finished voices waiting to play on the PC speaker
TTS_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=2)   # two sentences are voiced at once
REPLY_TARGET = "pc"   # where replies are spoken: "pc" or "phone"
CORE = None           # the v13 shared core (set below)
STOP_GEN = 0          # bumped by "stop": speech queued before that moment is thrown away


def stop_speaking():
    """Shut Nemesis up right now: drop everything queued and cut the current sound."""
    global STOP_GEN
    STOP_GEN += 1
    for q in (speak_q, ready_q, play_q):
        try:
            while True:
                q.get_nowait()
                q.task_done()
        except (queue.Empty, ValueError):
            pass
    try:
        pygame.mixer.stop()
    except Exception:
        pass
    hub.set_state("listening")


def choose(title, names):
    print(f"\n{title}")
    print("  0) Windows default (follows whatever Windows is using)")
    for i, n in enumerate(names, 1):
        print(f"  {i}) {n}")
    while True:
        a = input("Choose a number: ").strip()
        if a.isdigit() and 0 <= int(a) <= len(names):
            return None if a == "0" else names[int(a) - 1]
        print("Not a valid number, try again.")


def input_device_list():
    """(index, name) of microphones on the default Windows audio API."""
    api = sd.default.hostapi
    return [(i, d["name"]) for i, d in enumerate(sd.query_devices())
            if d["max_input_channels"] > 0 and d["hostapi"] == api]


def setup_audio():
    """Pick the PC speaker (and the mic with --pc-mic)."""
    global INPUT_DEVICE, MIC_NAME, SPEAKER_NAME, PC_AUDIO
    cfg = load_json(CONFIG_FILE, {})
    force = "--setup" in sys.argv

    try:
        pygame.mixer.init(buffer=2048)
    except Exception as e:
        PC_AUDIO = False
        print(f"[!] The PC has no usable audio output ({e}). Phone replies still work.")
        return
    out_names = list(dict.fromkeys(n for n in get_audio_device_names(False) if n))
    in_devices = input_device_list() if LOCAL_MIC else []
    in_names = [n for _, n in in_devices]

    need_out = force or "output" not in cfg
    need_in = LOCAL_MIC and (force or "input" not in cfg)
    if need_out or need_in:
        print("=== Audio setup (wired, USB, Bluetooth, built-in all work) ===")
        if need_in:
            cfg["input"] = choose("MICROPHONE (what Nemesis listens with):", in_names)
        if need_out:
            cfg["output"] = choose("PC SPEAKER / HEADPHONES (what Nemesis talks through on the PC):", out_names)
        save_json(CONFIG_FILE, cfg)

    if LOCAL_MIC:
        want_in = cfg.get("input")
        if want_in and want_in in in_names:
            INPUT_DEVICE = in_devices[in_names.index(want_in)][0]
            MIC_NAME = want_in
        else:
            if want_in:
                print(f"[!] Saved mic '{want_in}' is not connected - using Windows default.")
            MIC_NAME = "Windows default"
    print(f"Mic: {MIC_NAME}")

    want_out = cfg.get("output")
    if want_out and want_out in out_names:
        pygame.mixer.quit()
        pygame.mixer.init(devicename=want_out, buffer=2048)
        SPEAKER_NAME = want_out
    else:
        if want_out:
            print(f"[!] Saved speaker '{want_out}' is not connected - using Windows default.")
        SPEAKER_NAME = "Windows default"
    print(f"PC speaker: {SPEAKER_NAME}")


# ----------------------------- voice output -----------------------------
def beep(freq=880, seconds=0.35):
    """Short test tone (no internet needed) to prove the PC speaker works."""
    try:
        rate, _fmt, channels = pygame.mixer.get_init()
        t = np.linspace(0, seconds, int(rate * seconds), False)
        wave_data = (np.sin(2 * np.pi * freq * t) * 0.4 * 32767).astype(np.int16)
        data = np.column_stack([wave_data] * channels) if channels > 1 else wave_data
        sound = pygame.sndarray.make_sound(np.ascontiguousarray(data))
        sound.play()
        time.sleep(seconds + 0.2)
    except Exception as e:
        print(f"[beep error] {e}")


def robotize(arr):
    """Metallic machine tone: ring modulation + short resonances + soft clip."""
    rate = pygame.mixer.get_init()[0]
    x = arr.astype(np.float32) / 32768.0
    n = x.shape[0]
    t = np.arange(n, dtype=np.float32) / rate
    mod = np.sin(2 * np.pi * 50 * t).astype(np.float32)
    if x.ndim == 2:
        mod = mod[:, None]
    y = x * (1.0 - ROBOT_AMOUNT) + (x * mod) * (ROBOT_AMOUNT * 1.6)
    out = y.copy()
    for delay_ms, gain in ((6, 0.35), (11, 0.20)):
        d = int(rate * delay_ms / 1000)
        if 0 < d < n:
            out[d:] += gain * y[:-d]
    out = np.tanh(out * 1.3)
    peak = float(np.max(np.abs(out))) or 1.0
    out = out / peak * 0.9
    return np.ascontiguousarray((out * 32767).astype(np.int16))


def smooth_edges(arr, rate, ms=8):
    """Tiny fade-in/out so there are no clicks between spoken chunks."""
    n = min(int(rate * ms / 1000), arr.shape[0] // 2)
    if n < 2:
        return arr
    x = arr.astype(np.float32)
    ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
    if x.ndim == 2:
        ramp = ramp[:, None]
    x[:n] *= ramp
    x[-n:] *= ramp[::-1]
    return np.ascontiguousarray(x.astype(np.int16))


def clean_for_speech(text: str) -> str:
    text = re.sub(r"[*_`#>~|]", "", text)
    text = re.sub(r"https?://\S+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def send_to_phone(path):
    """Turn the generated voice file into a robot-voice WAV and hand it to the phone."""
    data, ctype, duration = None, "audio/mpeg", 3.0
    try:
        snd = pygame.mixer.Sound(path)
        arr = np.ascontiguousarray(pygame.sndarray.array(snd))
        rate = pygame.mixer.get_init()[0]
        if ROBOT_EFFECT and arr.dtype == np.int16:
            arr = robotize(arr)
        if arr.dtype == np.int16:
            arr = smooth_edges(arr, rate)
        channels = 1 if arr.ndim == 1 else arr.shape[1]
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(channels)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(arr.tobytes())
        data, ctype = buf.getvalue(), "audio/wav"
        duration = arr.shape[0] / rate
    except Exception as e:  # decoding failed: send the plain voice instead
        print(f"[phone audio: plain voice used ({e})]")
        with open(path, "rb") as f:
            data = f.read()
    try:
        os.remove(path)
    except OSError:
        pass
    hub.add_audio(data, ctype)
    hub.set_state("speaking")
    threading.Timer(duration + 0.4, lambda: hub.set_state("listening")).start()


TTS_CACHE = os.path.join(BASE_DIR, "tts_cache")
TURN = {"t0": 0.0, "brain": 0.0, "said": 0.0, "voice": 0.0, "done": True}   # timing of the current answer
SPEED_LOG = collections.deque(maxlen=12)


def _tts_key(text, voice):
    return hashlib.sha1(f"{voice}|{VOICE_PITCH}|{VOICE_RATE}|{text}".encode("utf-8")).hexdigest()


def make_voice_file_offline(text, lang):
    """Windows' own voice (works with no internet), then the same robot effect as always."""
    key = _tts_key(text, "offline-" + lang)
    cached = os.path.join(TTS_CACHE, key + ".wav")
    path = os.path.join(tempfile.gettempdir(), f"nemesis_{time.time_ns()}.wav")
    if os.path.exists(cached):
        shutil.copyfile(cached, path)
        return path
    made = offline.make_wav(text, lang)
    shutil.move(made, path)
    if len(text) <= 160:
        try:
            os.makedirs(TTS_CACHE, exist_ok=True)
            shutil.copyfile(path, cached)
        except OSError:
            pass
    return path


def _lang_of_voice(voice):
    return "hi" if voice == HINDI_VOICE else "mr" if voice == MARATHI_VOICE else "en"


def make_voice_file(text, voice):
    """Text -> sound file path. Repeated sentences come from a small cache and are instant."""
    if OFFLINE and offline.tts_available():
        return make_voice_file_offline(text, _lang_of_voice(voice))
    key = _tts_key(text, voice)
    cached = os.path.join(TTS_CACHE, key + ".mp3")
    path = os.path.join(tempfile.gettempdir(), f"nemesis_{time.time_ns()}.mp3")
    if os.path.exists(cached):
        shutil.copyfile(cached, path)
        return path
    last = None
    for _ in range(2):                       # one automatic retry if the network hiccups
        try:
            asyncio.run(edge_tts.Communicate(text, voice, pitch=VOICE_PITCH, rate=VOICE_RATE).save(path))
            if os.path.getsize(path) > 200:
                break
        except Exception as e:
            last = e
            time.sleep(0.3)
    else:                                    # the online voice failed (no internet?): try Windows' own voice
        try:
            hub.set_info(net="OFFLINE")
            return make_voice_file_offline(text, _lang_of_voice(voice))
        except Exception:
            raise RuntimeError(last or "empty voice file")
    if len(text) <= 160:
        try:
            os.makedirs(TTS_CACHE, exist_ok=True)
            shutil.copyfile(path, cached)
        except OSError:
            pass
    return path


def _voice_job(text, gen):
    if gen != STOP_GEN:
        return None
    return make_voice_file(text, pick_voice(text))


def _tts_worker():
    """Takes text in order, starts making the voice at once (two at a time)."""
    while True:
        item = speak_q.get()
        if item is None:
            ready_q.put(None)
            speak_q.task_done()
            return
        text, target, gen = item
        text = clean_for_speech(text)
        fut = TTS_POOL.submit(_voice_job, text, gen) if text and gen == STOP_GEN else None
        ready_q.put((fut, target, gen))
        speak_q.task_done()


def _deliver_worker():
    """Hands finished voices on in the original order."""
    while True:
        item = ready_q.get()
        if item is None:
            play_q.put(None)
            ready_q.task_done()
            return
        fut, target, gen = item
        try:
            path = fut.result() if fut else None
            if path and gen != STOP_GEN:
                try:
                    os.remove(path)
                except OSError:
                    pass
            elif path:
                if TURN["voice"] == 0.0 and not TURN["done"]:
                    TURN["voice"] = time.time()
                    note_speed()
                if target == "both":
                    if PC_AUDIO:
                        copy = path + ".pc.mp3"
                        shutil.copyfile(path, copy)
                        play_q.put((copy, gen))
                    send_to_phone(path)
                elif target == "phone":
                    send_to_phone(path)
                elif PC_AUDIO:
                    play_q.put((path, gen))
        except Exception as e:  # no internet etc.
            print(f"[voice error - edge-tts needs internet] {e}")
            hub.log("sys", "Voice error - check internet connection")
        ready_q.task_done()


def note_speed():
    t0 = TURN["t0"]
    if not t0:
        return
    brain = (TURN["brain"] - t0) if TURN["brain"] else 0.0
    first = TURN["voice"] - t0
    SPEED_LOG.append((brain, first))
    hub.set_info(speed=f"{first:.1f}S")
    said = (TURN["said"] - t0) if TURN.get("said") else 0.0
    extra = f", first sentence ready {said:.1f}s" if said else ""
    print(f"[speed] brain {brain:.1f}s{extra}, first voice {first:.1f}s after you finished speaking")


def speed_report():
    if not SPEED_LOG:
        return "I have no timings yet. Ask me something first."
    avg = sum(f for _, f in SPEED_LOG) / len(SPEED_LOG)
    best = min(f for _, f in SPEED_LOG)
    return (f"Over my last {len(SPEED_LOG)} answers I started speaking after {avg:.1f} seconds on average, "
            f"and {best:.1f} at best. I am using the {'fast' if OLLAMA_MODEL == FAST_MODEL else 'smart'} brain.")


PRELOAD = ["Done.", "Yes?", "Okay, cancelled.", "Volume up.", "Volume down.", "Standing by.", "Noted.",
           "Hello. How can I help?", "You are welcome.", "All systems nominal. How can I help?",
           "I am Nemesis, your personal assistant.", "Opening chrome.", "Opening notepad.",
           "Opening youtube.", "Opening google.", "Which song should I play?", "Please tell me the song name.",
           "Which city are you in?", "Good night. Standing by.", "One moment.", "Let me think.", "Hmm."]
FILLERS = ("One moment.", "Let me think.", "Hmm.")


def preload_voices():
    """Make the common short replies once, so they play instantly later."""
    time.sleep(8)
    for text in PRELOAD:
        key = _tts_key(text, VOICE)
        if os.path.exists(os.path.join(TTS_CACHE, key + ".mp3")):
            continue
        try:
            os.remove(make_voice_file(text, VOICE))
        except Exception:
            return                              # no internet right now: try again next start
        time.sleep(0.5)


def trim_voice_cache(keep=400):
    try:
        files = sorted((os.path.join(TTS_CACHE, f) for f in os.listdir(TTS_CACHE)), key=os.path.getmtime)
        for fp in files[:-keep]:
            os.remove(fp)
    except OSError:
        pass


def play_file(path, gen=None):
    snd = pygame.mixer.Sound(path)
    try:
        os.remove(path)
    except OSError:
        pass
    arr = pygame.sndarray.array(snd)
    rate = pygame.mixer.get_init()[0]
    if arr.dtype == np.int16:
        if ROBOT_EFFECT:
            arr = robotize(arr)
        arr = smooth_edges(arr, rate)
        snd = pygame.sndarray.make_sound(arr)
    mono = arr.astype(np.float32) / 32768.0
    if mono.ndim == 2:
        mono = mono.mean(axis=1)
    win = max(1, int(rate * 0.05))
    channel = snd.play()
    if channel is None:
        return
    t0 = time.time()
    while channel.get_busy():  # animate the HUD with the voice
        if gen is not None and gen != STOP_GEN:
            channel.stop()
            break
        pos = int((time.time() - t0) * rate)
        seg = mono[pos:pos + win]
        lv = float(np.sqrt(np.mean(seg ** 2))) if len(seg) else 0.0
        hub.level(min(1.0, lv * 3.5), "voice")
        time.sleep(0.04)
    hub.level(0, "voice")


def _play_worker():
    while True:
        item = play_q.get()
        if item is None:
            play_q.task_done()
            return
        path, gen = item
        try:
            if gen != STOP_GEN:
                os.remove(path)
                play_q.task_done()
                continue
            hub.set_state("speaking")
            play_file(path, gen)
        except Exception as e:
            print(f"[play error] {e}")
        play_q.task_done()


def say(text: str, target=None):
    target = target or REPLY_TARGET
    print(f"{NAME}: {text}")
    hub.log("nemesis", text)
    if CORE is not None:
        CORE.note_spoken(text)
    speak_q.put((text, target, STOP_GEN))


def wait_until_done_speaking():
    speak_q.join()
    ready_q.join()
    play_q.join()


# ----------------------------- PC microphone (--pc-mic) ------------------
stt = None


def load_speech_model():
    global stt
    from faster_whisper import WhisperModel
    print("Loading speech recognition model (first run downloads it)...")
    stt = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")


def open_mic(block):
    try:
        return sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                              blocksize=block, device=INPUT_DEVICE)
    except Exception as e:
        print(f"[mic error] {e} - falling back to Windows default mic")
        return sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                              blocksize=block, device=None)


def calibrate():
    """Measure room noise and set the trigger level automatically."""
    global SILENCE_THRESHOLD
    if not AUTO_CALIBRATE:
        return
    print("Calibrating microphone - stay quiet for 2 seconds...")
    hub.log("sys", "Calibrating microphone - stay quiet")
    block = int(SAMPLE_RATE * 0.05)
    levels = []
    try:
        with open_mic(block) as stream:
            for _ in range(40):
                data, _ = stream.read(block)
                levels.append(float(np.sqrt(np.mean((data - data.mean()) ** 2))))
    except Exception as e:
        print(f"[calibration skipped] {e}")
        return
    ambient = float(np.percentile(levels, 90))
    SILENCE_THRESHOLD = min(0.05, max(0.004, ambient * 2.5 + 0.002))
    print(f"Room noise {ambient:.4f} -> trigger level {SILENCE_THRESHOLD:.4f}")


def draw_meter(rms, state):
    width = 30
    scale = SILENCE_THRESHOLD * 4
    filled = min(width, int(rms / scale * width))
    mark = min(width - 1, int(SILENCE_THRESHOLD / scale * width))
    bar = "".join("#" if i < filled else "-" for i in range(width))
    bar = bar[:mark] + "|" + bar[mark + 1:]
    sys.stdout.write(f"\r  Mic [{bar}] {rms:.4f}  {state:<10}")
    sys.stdout.flush()


def clear_meter():
    sys.stdout.write("\r" + " " * 70 + "\r")
    sys.stdout.flush()


def record_utterance(start_timeout=None, max_seconds=20):
    """Record until the person stops talking. Returns float32 audio or None."""
    block = int(SAMPLE_RATE * 0.05)
    preroll = collections.deque(maxlen=8)
    frames, speaking, silent = [], False, 0.0
    t0 = time.time()
    last_draw = 0.0
    with open_mic(block) as stream:
        while True:
            data, _ = stream.read(block)
            rms = float(np.sqrt(np.mean((data - data.mean()) ** 2)))
            loud = rms > SILENCE_THRESHOLD
            now = time.time()
            hub.level(min(1.0, rms / (SILENCE_THRESHOLD * 8)), "mic")
            if now - last_draw > 0.1:
                draw_meter(rms, "RECORDING" if speaking else ("HEARING" if loud else "quiet"))
                last_draw = now
            if not speaking:
                preroll.append(data.copy())
                if loud:
                    speaking, silent = True, 0.0
                    frames.extend(preroll)
                    hub.set_state("hearing")
                elif start_timeout and now - t0 > start_timeout:
                    clear_meter()
                    return None
            else:
                frames.append(data.copy())
                silent = 0.0 if loud else silent + 0.05
                if silent >= SILENCE_SECONDS or now - t0 > max_seconds:
                    break
    clear_meter()
    hub.level(0, "mic")
    return np.concatenate(frames).flatten()


def transcribe(audio) -> str:
    segments, _ = stt.transcribe(audio, language="en", vad_filter=True, beam_size=1)
    return " ".join(s.text.strip() for s in segments).strip()


# ----------------------------- skills -----------------------------------
APPS = {
    "chrome": "chrome", "notepad": "notepad", "calculator": "calc",
    "paint": "mspaint", "file explorer": "explorer", "explorer": "explorer",
    "command prompt": "cmd", "task manager": "taskmgr", "spotify": "spotify",
    "word": "winword", "excel": "excel", "vs code": "code", "settings": "ms-settings:",
}
SITES = {
    "youtube": "https://www.youtube.com", "google": "https://www.google.com",
    "gmail": "https://mail.google.com", "whatsapp": "https://web.whatsapp.com",
    "github": "https://github.com", "instagram": "https://www.instagram.com",
    "maps": "https://maps.google.com", "chat": "https://claude.ai",
}
NUM_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "fifteen": 15,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "half": 0.5,
}
UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600}

VK_VOLUME_MUTE, VK_VOLUME_DOWN, VK_VOLUME_UP = 0xAD, 0xAE, 0xAF
VK_MEDIA_NEXT, VK_MEDIA_PREV, VK_MEDIA_PLAY = 0xB0, 0xB1, 0xB3


def press_key(vk, times=1):
    try:
        user32 = ctypes.windll.user32
        for _ in range(times):
            user32.keybd_event(vk, 0, 0, 0)
            user32.keybd_event(vk, 0, 2, 0)
            time.sleep(0.01)
        return True
    except Exception:
        return False


def to_number(word):
    try:
        return float(word)
    except ValueError:
        return NUM_WORDS.get(word)


def pretty_duration(seconds):
    if seconds % 3600 == 0 and seconds >= 3600:
        v, u = int(seconds // 3600), "hour"
    elif seconds % 60 == 0 and seconds >= 60:
        v, u = int(seconds // 60), "minute"
    else:
        v, u = int(seconds), "second"
    return f"{v} {u}{'' if v == 1 else 's'}"


pending = {"song_until": 0.0, "city_until": 0.0, "confirm": None, "pin": None, "light_cal": 0.0}   # follow-up answers


def clean_query(q):
    q = q.strip(" .,!?")
    q = re.sub(r"\s+(?:on|in|from)\s+youtube$", "", q)
    q = re.sub(r"^(?:the |a )?(?:song|video|track)\s+(?:called|named)\s+", "", q)
    q = re.sub(r"^(?:any|some)\s+(?=\w+\s+\w+)", "", q)               # "any hindi song" -> "hindi song"
    return q.strip()


def youtube_first_video(query):
    """Find the first YouTube result so the song starts playing straight away."""
    import urllib.request
    from urllib.parse import quote_plus
    search_url = "https://www.youtube.com/results?search_query=" + quote_plus(query)
    if PRIVACY:
        return search_url
    try:
        req = urllib.request.Request(search_url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9"})
        html = urllib.request.urlopen(req, timeout=6).read().decode("utf-8", "ignore")
        m = (re.search(r'"videoRenderer":\{"videoId":"([\w-]{11})"', html)
             or re.search(r'"videoId":"([\w-]{11})"', html))
        if m:
            return f"https://www.youtube.com/watch?v={m.group(1)}&autoplay=1"
    except Exception as e:
        print(f"[youtube lookup failed, opening search page: {e}]")
    return search_url


def play_on_youtube(query):
    query = query.strip()
    if not query:
        return "Which song should I play?"
    webbrowser.open(youtube_first_video(query))
    return f"Playing {query} on YouTube."


def what_i_can_do():
    return ("I can chat, do exact maths and money calculations, and convert units. "
            "I give weather, news and Wikipedia answers. I open apps and websites, play YouTube, "
            "type text into Notepad or any window, take dictation, write Word documents, "
            "take screenshots and read the text on your screen. "
            "I set timers, alarms and daily reminders, and I can see and track objects through your phone camera. "
            "I speak English, Hindi and Marathi, and I can change my voice. I remember facts, notes and tasks, and can search your documents. "
            "I work offline, and I can check your PC for hidden programs. Say good morning for your briefing.")


class PrivacyBlocked(Exception):
    """Raised instead of going online while privacy mode is on."""


class OfflineBlocked(PrivacyBlocked):
    """Raised instead of going online while offline mode is on."""


OFFLINE_MSG = "I am in offline mode, so I cannot look that up. Say go online to allow it."


def http_get(url, timeout=8):
    if OFFLINE:
        raise OfflineBlocked()
    if PRIVACY:
        raise PrivacyBlocked()
    req = urllib.request.Request(url, headers={"User-Agent": "Nemesis (personal voice assistant)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def http_json(url, timeout=8):
    return json.loads(http_get(url, timeout).decode("utf-8", "ignore"))


NO_NET = "I could not reach the internet."


fmt_num = tools.fmt


# ----------------------------- v4: unit conversion ----------------------
_UNITS = {}
for _cat, _base, _names in (
        ("len", 1.0, {"meter": 1, "metre": 1, "m": 1, "kilometer": 1000, "kilometre": 1000, "km": 1000,
                      "centimeter": .01, "centimetre": .01, "cm": .01, "millimeter": .001, "mm": .001,
                      "mile": 1609.344, "foot": .3048, "feet": .3048, "ft": .3048, "inch": .0254,
                      "inches": .0254, "yard": .9144}),
        ("mass", 1.0, {"kilogram": 1, "kilo": 1, "kg": 1, "gram": .001, "g": .001, "pound": .45359237,
                       "lb": .45359237, "lbs": .45359237, "ounce": .028349523, "oz": .028349523}),
        ("vol", 1.0, {"liter": 1, "litre": 1, "l": 1, "milliliter": .001, "millilitre": .001, "ml": .001,
                      "gallon": 3.785411784, "cup": .2365882365}),
        ("temp", 1.0, {"celsius": 0, "centigrade": 0, "c": 0, "fahrenheit": 0, "f": 0, "kelvin": 0, "k": 0})):
    for _n, _f in _names.items():
        _UNITS[_n] = (_cat, _f)


def _unit(word):
    w = word.lower()
    for cand in (w, w.rstrip("s"), w[:-2] if w.endswith("es") else w):
        if cand in _UNITS:
            return cand, _UNITS[cand]
    return None, None


def convert_units(n, from_w, to_w):
    fn, fu = _unit(from_w)
    tn, tu = _unit(to_w)
    if not fu or not tu or fu[0] != tu[0]:
        return None
    if fu[0] == "temp":
        key = {"c": "c", "f": "f", "k": "k"}
        fk, tk = key[fn[0]], key[tn[0]]
        c = {"c": n, "f": (n - 32) * 5 / 9, "k": n - 273.15}[fk]
        v = {"c": c, "f": c * 9 / 5 + 32, "k": c + 273.15}[tk]
    else:
        v = n * fu[1] / tu[1]
    return f"{fmt_num(n)} {from_w} is {fmt_num(round(v, 4))} {to_w}."


def unit_skill(t):
    t = re.sub(r"\bdegrees?\s+", "", t)
    m = (re.search(r"convert (-?\d+(?:\.\d+)?) ([a-z]+) (?:to|into|in) ([a-z]+)", t)
         or re.search(r"what(?:'s| is) (-?\d+(?:\.\d+)?) ([a-z]+) (?:in|to|into) ([a-z]+)", t)
         or re.search(r"how many ([a-z]+) (?:are |is )?(?:there )?in (\d+(?:\.\d+)?) ([a-z]+)", t))
    if not m:
        return None
    g = m.groups()
    if t.startswith("how many"):
        return convert_units(float(g[1]), g[2], g[0])
    return convert_units(float(g[0]), g[1], g[2])


# ----------------------------- v4: weather ------------------------------
WMO = {0: "clear sky", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "foggy", 48: "foggy",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
       61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
       71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "rain showers",
       81: "rain showers", 82: "violent rain showers", 85: "snow showers", 86: "snow showers",
       95: "a thunderstorm", 96: "a thunderstorm with hail", 99: "a thunderstorm with hail"}
GEO_URL = "https://geocoding-api.open-meteo.com/v1/search?count=1&language=en&format=json&name="
WX_URL = ("https://api.open-meteo.com/v1/forecast?current=temperature_2m,apparent_temperature,"
          "weather_code,wind_speed_10m&daily=temperature_2m_max,temperature_2m_min,"
          "precipitation_probability_max&timezone=auto&forecast_days=2")


def saved_city():
    return load_json(CONFIG_FILE, {}).get("city")


def save_city(city):
    cfg = load_json(CONFIG_FILE, {})
    cfg["city"] = city
    cfg.pop("geo", None)
    save_json(CONFIG_FILE, cfg)


def geocode(city):
    cfg = load_json(CONFIG_FILE, {})
    geo = cfg.get("geo")
    if geo and geo.get("query", "").lower() == city.lower():
        return geo
    d = http_json(GEO_URL + urllib.parse.quote(city))
    r = (d.get("results") or [None])[0]
    if not r:
        return None
    geo = {"query": city, "name": r["name"], "lat": r["latitude"], "lon": r["longitude"]}
    if city.lower() == (cfg.get("city") or "").lower():
        cfg["geo"] = geo
        save_json(CONFIG_FILE, cfg)
    return geo


def fetch_weather(city, day=0):
    """Returns a dict, or None if the city is unknown. Raises on network errors."""
    geo = geocode(city)
    if not geo:
        return None
    d = http_json(WX_URL + f"&latitude={geo['lat']}&longitude={geo['lon']}")
    cur, daily = d["current"], d["daily"]
    return {"place": geo["name"], "temp": round(cur["temperature_2m"]),
            "feels": round(cur["apparent_temperature"]), "wind": round(cur["wind_speed_10m"]),
            "desc": WMO.get(cur["weather_code"], "unsettled"),
            "hi": round(daily["temperature_2m_max"][day]), "lo": round(daily["temperature_2m_min"][day]),
            "rain": daily["precipitation_probability_max"][day] or 0}


def weather_reply(city, tomorrow=False):
    try:
        w = fetch_weather(city, 1 if tomorrow else 0)
    except PrivacyBlocked as e:
        return OFFLINE_MSG if isinstance(e, OfflineBlocked) else PRIVACY_MSG
    except Exception as e:
        print(f"[weather error] {e}")
        return "I could not reach the weather service."
    if not w:
        return f"I could not find a city called {city}."
    if tomorrow:
        return (f"Tomorrow in {w['place']}: high {w['hi']}, low {w['lo']} degrees, "
                f"rain chance {w['rain']} percent.")
    return (f"In {w['place']} it is {w['temp']} degrees and {w['desc']}, feels like {w['feels']}. "
            f"Today's high is {w['hi']}, low {w['lo']}, rain chance {w['rain']} percent.")


def weather_skill(t):
    m = re.match(r"(?:set|change) my (?:city|location) to (.+)", t) or \
        re.match(r"(?:i live in|i am in|i'm in|my city is|my location is) (.+)", t)
    if m and "weather" not in t:
        city = m.group(1).strip().title()
        save_city(city)
        return f"Done. Your city is {city}."
    if not re.search(r"\b(weather|forecast|raining|will it rain|is it going to rain|how (?:hot|cold) is it|temperature (?:outside|today|now|in))\b", t):
        return None
    tomorrow = "tomorrow" in t
    s = re.sub(r"\b(today|tonight|right now|now|tomorrow|outside|please)\b", "", t).strip(" .?")
    m = re.search(r"\b(?:in|for|at)\s+([a-z][a-z .'-]*)$", s)
    city = m.group(1).strip().title() if m else None
    if city in ("My Area", "My City", "Here", "The City", "Weather"):
        city = None
    city = city or saved_city()
    if not city:
        pending["city_until"] = time.time() + 30
        return "Which city are you in?"
    return weather_reply(city, tomorrow)


# ----------------------------- v4: news + wikipedia ---------------------
NEWS_URL = "https://news.google.com/rss?hl=en-IN&gl=IN&ceid=IN:en"
NEWS_SEARCH = "https://news.google.com/rss/search?hl=en-IN&gl=IN&ceid=IN:en&q="
WIKI_SEARCH = "https://en.wikipedia.org/w/api.php?action=query&list=search&format=json&srlimit=1&srsearch="
WIKI_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/"


def fetch_headlines(topic=None, n=4):
    root = ET.fromstring(http_get(NEWS_SEARCH + urllib.parse.quote(topic) if topic else NEWS_URL))
    out = []
    for item in root.findall("./channel/item"):
        title = re.sub(r"\s+-\s+[^-]+$", "", item.findtext("title", "")).strip()
        if title:
            out.append(title[:140])
        if len(out) >= n:
            break
    return out


def news_skill(t):
    m = re.search(r"\b(?:news|headlines)\b(?: (?:about|on|for) (.+))?$", t)
    if not m or not re.search(r"\b(tell|read|give|what|show|latest|top|today|any|news|headlines)\b", t):
        return None
    topic = (m.group(1) or "").strip() or None
    try:
        heads = fetch_headlines(topic)
    except PrivacyBlocked as e:
        return OFFLINE_MSG if isinstance(e, OfflineBlocked) else PRIVACY_MSG
    except Exception as e:
        print(f"[news error] {e}")
        return "I could not fetch the news right now."
    if not heads:
        return "I found no headlines."
    lead = f"Top headlines about {topic}." if topic else "Here are the top headlines."
    return lead + " " + " ".join(f"{i}. {h}." if not h.endswith((".", "?", "!")) else f"{i}. {h}"
                                 for i, h in enumerate(heads, 1))


def wikipedia_summary(subject):
    d = http_json(WIKI_SEARCH + urllib.parse.quote(subject))
    hits = d.get("query", {}).get("search", [])
    if not hits:
        return None
    s = http_json(WIKI_SUMMARY + urllib.parse.quote(hits[0]["title"].replace(" ", "_")))
    ex = s.get("extract", "")
    if not ex:
        return None
    if s.get("type") == "disambiguation" or "may refer to" in ex[:200]:
        return "That name has several meanings. Please be more specific."
    ex = re.sub(r"\s*\([^)]*\)", "", ex)
    sentences = re.split(r"(?<=[.!?])\s+", ex)
    return " ".join(sentences[:2])[:420]


_WIKI_SKIP = {"you", "yourself", "me", "my", "your", "i", "he", "she", "it", "this", "that", "we",
              "they", "there", "nemesis", "them", "him", "her"}


def wiki_skill(t):
    m = re.match(r"(who (?:is|was)|tell me about|(?:search )?wikipedia(?: for)?|wiki)\s+(.+)", t)
    if not m:
        return None
    kind, subj = m.group(1), m.group(2).strip()
    if subj in _WIKI_SKIP or subj.split()[0] in _WIKI_SKIP or len(subj.split()) > 5:
        return None
    if kind.startswith("who") and subj.split()[0] in ("the", "a", "an"):
        return None
    try:
        r = wikipedia_summary(subj)
    except PrivacyBlocked:
        return None
    except Exception as e:
        print(f"[wikipedia error] {e}")
        return None            # no internet or API trouble: let the brain answer
    return r


# ----------------------------- v4: reminders, timers, alarms ------------
rem_lock = threading.Lock()
reminders = load_json(REM_FILE, [])
if not isinstance(reminders, list):
    reminders = []


def first_person_to_second(s):
    s = re.sub(r"\bmy\b", "your", s)
    s = re.sub(r"\bi\b", "you", s)
    s = re.sub(r"\bme\b", "you", s)
    return s


def add_reminder(when, text, repeat=None):
    with rem_lock:
        reminders.append({"when": when, "text": text, "repeat": repeat})
        reminders.sort(key=lambda r: r["when"])
        save_json(REM_FILE, reminders)


def clock_phrase(ts):
    d = datetime.datetime.fromtimestamp(ts)
    day = d.date()
    today = datetime.date.today()
    word = "today" if day == today else "tomorrow" if day == today + datetime.timedelta(days=1) else d.strftime("on %A")
    return d.strftime("%I:%M %p").lstrip("0") + " " + word


def next_clock(h, mi, ampm, tomorrow=False, now=None):
    now = now or datetime.datetime.now()
    if ampm == "pm" and h < 12:
        h += 12
    if ampm == "am" and h == 12:
        h = 0
    cands = [h] if (ampm or h >= 13 or h == 0) else ([h, h + 12] if h < 12 else [h])
    best = None
    for hh in cands:
        if hh > 23 or mi > 59:
            return None
        c = now.replace(hour=hh, minute=mi, second=0, microsecond=0)
        if tomorrow:
            c += datetime.timedelta(days=1)
        while c <= now:
            c += datetime.timedelta(days=1)
        best = c if best is None or c < best else best
    return best


def reminder_skill(t):
    t = re.sub(r"\b([ap])\.?\s?m\.?(?=\W|$)", r"\1m", t)
    if re.search(r"\b(?:list|what are|any|show|read)\b.*\b(?:reminders?|alarms?|timers?)\b", t) or \
            re.fullmatch(r"(?:my )?(?:reminders|alarms)", t):
        with rem_lock:
            items = list(reminders)
        if not items:
            return "You have no reminders."
        parts = [f"{r['text'].rstrip('.')}, at {clock_phrase(r['when'])}" + (", every day" if r.get("repeat") else "") for r in items[:5]]
        return f"You have {len(items)} reminder{'s' if len(items) != 1 else ''}. " + ". ".join(parts) + "."
    if re.search(r"\b(?:cancel|delete|clear|remove)\b.*\b(?:reminders?|alarms?|timers?)\b", t):
        with rem_lock:
            reminders.clear()
            save_json(REM_FILE, reminders)
        return "All reminders cleared."

    # relative: "remind me in 10 minutes to call mom" / "set a timer for 5 minutes"
    m = re.search(r"\bremind me (?:in|after) (\S+) (seconds?|minutes?|hours?)(?: (?:to|that|about) (.+))?", t)
    if m:
        n = to_number(m.group(1))
        if n:
            secs = n * UNIT_SECONDS[m.group(2).rstrip("s")]
            what = first_person_to_second(m.group(3) or "your reminder")
            add_reminder(time.time() + secs, f"Reminder: {what}.")
            return f"I will remind you in {pretty_duration(secs)}."
    m = re.search(r"\b(?:timer|alarm)\b.*?(\S+)\s+(seconds?|minutes?|hours?)\b", t)
    if m:
        n = to_number(m.group(1))
        if n:
            secs = n * UNIT_SECONDS[m.group(2).rstrip("s")]
            add_reminder(time.time() + secs, "Time is up.")
            return f"Timer set for {pretty_duration(secs)}."

    # clock time: "wake me up at 7 am" / "remind me at 5:30 pm to ..." / "every day at 8 am remind me to ..."
    if re.search(r"\b(remind me|wake me|alarm|reminder)\b", t):
        m = re.search(r"\b(?:at|for)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?(?:\s+o'?clock)?\b", t)
        if m:
            h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
            if not ap and h < 12 and re.search(r"\b(alarm|wake me)\b", t):
                ap = "am"          # "wake me up at 7" means the morning
            when = next_clock(h, mi, ap, tomorrow="tomorrow" in t)
            if not when:
                return "That time does not look right."
            repeat = "daily" if re.search(r"\bevery (?:day|morning|evening|night)\b|\bdaily\b", t) else None
            rest = (t[:m.start()] + " " + t[m.end():])
            mt = re.search(r"\b(?:to|that|about)\s+(.+)", rest)
            is_alarm = bool(re.search(r"\b(alarm|wake me)\b", t))
            what = first_person_to_second(mt.group(1).strip(" .")) if mt else ""
            what = re.sub(r"\b(?:every (?:day|morning|evening|night)|daily|tomorrow)\b", "", what).strip()
            text = ("Alarm. " + what) if is_alarm and not what else (f"Reminder: {what}." if what else "Your reminder is due.")
            if is_alarm and not what:
                text = "Wake up. This is your alarm."
            add_reminder(when.timestamp(), text, repeat)
            return f"{'Alarm' if is_alarm else 'Reminder'} set for {clock_phrase(when.timestamp())}" + (", every day." if repeat else ".")
    return None


def reminder_loop():
    """Background thread: speaks reminders when they fall due (survives restarts)."""
    while True:
        time.sleep(3)
        now = time.time()
        due = []
        with rem_lock:
            for r in list(reminders):
                if r["when"] <= now:
                    due.append((r["text"], now - r["when"]))
                    if r.get("repeat") == "daily":
                        r["when"] += 86400 * (int((now - r["when"]) // 86400) + 1)
                    else:
                        reminders.remove(r)
            if due:
                save_json(REM_FILE, reminders)
        for text, late in due:
            say(("Missed reminder. " if late > 600 else "") + text, target="both")


# ----------------------------- v4: PC control ---------------------------
PROCS = {"chrome": "chrome.exe", "google chrome": "chrome.exe", "notepad": "notepad.exe",
         "spotify": "spotify.exe", "word": "winword.exe", "excel": "excel.exe", "vs code": "code.exe",
         "paint": "mspaint.exe", "calculator": "CalculatorApp.exe", "firefox": "firefox.exe",
         "edge": "msedge.exe", "vlc": "vlc.exe", "freecad": "FreeCAD.exe", "explorer": "explorer.exe"}
POWER_WORDS = {"shutdown": "shut down", "restart": "restart", "sleep": "put to sleep"}


def run_power(action):
    try:
        if action == "shutdown":
            subprocess.Popen(["shutdown", "/s", "/t", "20"])
            return "Shutting down in 20 seconds. Say cancel shutdown to stop me."
        if action == "restart":
            subprocess.Popen(["shutdown", "/r", "/t", "20"])
            return "Restarting in 20 seconds. Say cancel shutdown to stop me."
        if action == "sleep":
            threading.Timer(3, lambda: subprocess.Popen(
                ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])).start()
            return "Going to sleep."
    except Exception as e:
        return f"I could not do that: {e}"
    return "Unknown action."


def set_volume(percent):
    percent = max(0, min(100, percent))
    press_key(VK_VOLUME_DOWN, 50)
    press_key(VK_VOLUME_UP, round(percent / 2))
    return f"Volume set to about {percent} percent."


def system_status():
    try:
        import psutil
    except ImportError:
        return "I need the psutil package for that. Run: pip install psutil"
    cpu = psutil.cpu_percent(interval=0.5)
    ram = psutil.virtual_memory()
    try:
        total, used, free = shutil.disk_usage(os.path.splitdrive(BASE_DIR)[0] + os.sep)
        disk = f" Drive {os.path.splitdrive(BASE_DIR)[0]} has {free / 1e9:.0f} gigabytes free."
    except Exception:
        disk = ""
    return f"CPU is at {cpu:.0f} percent. Memory is {ram.percent:.0f} percent used.{disk}"


_start_menu = {"names": None}


def start_menu_apps():
    if _start_menu["names"] is None:
        found = {}
        roots = [os.path.join(os.environ.get("ProgramData", ""), r"Microsoft\Windows\Start Menu\Programs"),
                 os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs")]
        for root in roots:
            for dp, _dn, files in os.walk(root):
                for f in files:
                    if f.lower().endswith(".lnk"):
                        found[os.path.splitext(f)[0]] = os.path.join(dp, f)
        _start_menu["names"] = found
    return _start_menu["names"]


def open_start_menu_app(name):
    apps = start_menu_apps()
    name = name.lower()
    exact = [k for k in apps if k.lower() == name]
    part = [k for k in apps if name in k.lower()] or \
           [k for k in apps if all(w in k.lower() for w in name.split())]
    pick = (exact or sorted(part, key=len) or [None])[0]
    if not pick:
        return None
    try:
        os.startfile(apps[pick])
    except Exception:
        return None
    return pick


PROTECTED_EXE = ("python", "pythonw", "powershell", "pwsh", "cmd", "conhost", "explorer", "ollama", "svchost", "system",
                 "csrss", "winlogon", "wininit", "services", "lsass", "dwm", "taskmgr", "msmpeng", "windowsterminal", "code")


def find_running_exe(target):
    """'close whatsapp' for a program that is not in PROCS: look at what is really running."""
    key = re.sub(r"[^a-z0-9]", "", target.lower())
    if len(key) < 3:
        return None
    try:
        import psutil
        for p in psutil.process_iter(["name"]):
            nm = (p.info.get("name") or "")
            base = re.sub(r"[^a-z0-9]", "", nm.lower().rsplit(".", 1)[0])
            if base and base not in PROTECTED_EXE and (key in base or base in key and len(base) >= 4):
                return nm
    except Exception:
        pass
    return None


def close_skill(t):
    """close tab / close this window / close youtube (a tab) - safe: never closes a terminal or Nemesis itself."""
    tab = re.fullmatch(r"(?:please )?close (?:this |the |current |that |my )?(?:tab|page|video|site|website)", t)
    win = re.fullmatch(r"(?:please )?(?:close|quit|exit) (?:this |the |current |that |my )?(?:window|app|program|application)|(?:please )?close (?:this|it|that)", t)
    m = re.fullmatch(r"(?:please )?close (?:the )?(youtube|google|gmail|facebook|instagram|twitter|whatsapp web|wikipedia|netflix|github|chatgpt|maps)(?: tab| page| site| website)?", t)
    if not (tab or win or m):
        return None
    title = tools.foreground_title()
    if re.search(tools.GUARD_RE, title) or re.search(r"nemesis", title, re.I):
        return "I will not close that window. It looks like a terminal or Nemesis itself."
    if m and m.group(1).split()[0] not in title.lower():
        return f"I do not see a {m.group(1)} tab in front. Click it, then say close tab."
    try:
        if win:
            tools.press("alt", "f4")
            return "Closed the window."
        tools.press("ctrl", "w")
        return "Closed the tab."
    except Exception:
        return "I could not close that."


def pc_skill(t):
    t = core.SHUT_RE.sub(r"shut down \1", t)
    if re.search(r"\b(lock)\b.*\b(computer|pc|screen|system|laptop)\b|^lock$", t):
        try:
            ctypes.windll.user32.LockWorkStation()
            return "Locking the computer."
        except Exception:
            return "I could not lock the computer."
    if re.search(r"\bcancel (?:the )?(?:shutdown|restart)\b", t):
        try:
            subprocess.Popen(["shutdown", "/a"])
        except Exception:
            pass
        return "Shutdown cancelled."
    m = re.search(r"\b(shut ?down|turn off|power off|restart|reboot|sleep|put)\b.*\b(computer|pc|system|laptop|machine)\b", t)
    if m:
        w = m.group(1).replace(" ", "")
        action = "restart" if w in ("restart", "reboot") else "sleep" if w in ("sleep", "put") else "shutdown"
        pending["confirm"] = (action, time.time() + 20)
        return f"Do you really want me to {POWER_WORDS[action]} the computer? Say yes or no."
    m = re.match(r"(?:set |change )?(?:the )?volume (?:to |at )?(\d{1,3})(?: percent| %)?$", t) or \
        re.match(r"set (?:the )?volume (?:to )?(\d{1,3})", t)
    if m:
        return set_volume(int(m.group(1)))
    if re.search(r"\b(system status|pc status|how is (?:my |the )?(?:pc|computer)|how are you running|cpu usage|ram usage|disk space|storage left|memory usage)\b", t):
        return system_status()
    r = close_skill(t)
    if r:
        return r
    m = re.match(r"(?:close|quit|exit|kill) (?:the )?(.+)", t)
    if m and m.group(1) not in ("nemesis", "this", "it", "window"):
        target = m.group(1).strip()
        exe = PROCS.get(target) or find_running_exe(target)
        if not exe:
            return f"I could not find a running program called {target}." if len(target.split()) <= 3 else None
        try:
            r = subprocess.run(["taskkill", "/im", exe], capture_output=True, text=True, timeout=10)
        except Exception as e:
            return f"I could not close {target}: {e}"
        return f"Closing {target}." if r.returncode == 0 else f"{target.title()} does not seem to be running."
    return None


# ----------------------------- v4: briefing + small talk ----------------
JOKES = [
    "I would tell you a UDP joke, but you might not get it.",
    "Why do programmers prefer dark mode? Because light attracts bugs.",
    "I asked the server for a joke. It said four hundred and four, joke not found.",
    "There are ten kinds of people. Those who understand binary, and those who do not.",
    "My neural network walked into a bar. It was a poor fit, but it kept training.",
]


def daily_briefing():
    now = datetime.datetime.now()
    greet = "Good morning" if now.hour < 12 else "Good afternoon" if now.hour < 18 else "Good evening"
    parts = [now.strftime(f"{greet}. It is %I:%M %p on %A, %d %B.").replace(" 0", " ", 1)]
    city = saved_city()
    if city:
        parts.append(weather_reply(city))
    else:
        parts.append("Tell me your city and I will add the weather.")
    with rem_lock:
        today = [r for r in reminders if datetime.datetime.fromtimestamp(r["when"]).date() == now.date()]
    if today:
        parts.append(f"You have {len(today)} reminder{'s' if len(today) != 1 else ''} today.")
    todo_line = mind.briefing_line()
    if todo_line:
        parts.append(todo_line)
    try:
        heads = fetch_headlines(n=3)
        if heads:
            parts.append("Top news. " + " ".join(f"{h.rstrip('.')}." for h in heads))
    except Exception:
        pass
    return " ".join(parts)


# ----------------------------- v5: voice change -------------------------
VOICE_CHOICES = {
    "guy": ("en-US-GuyNeural", "deep American"), "ryan": ("en-GB-RyanNeural", "British"),
    "andrew": ("en-US-AndrewNeural", "warm American"), "christopher": ("en-US-ChristopherNeural", "calm American"),
    "davis": ("en-US-DavisNeural", "smooth American"), "brian": ("en-US-BrianNeural", "casual American"),
    "prabhat": ("en-IN-PrabhatNeural", "Indian English"),
}
HINDI_VOICE = "hi-IN-MadhurNeural"
MARATHI_VOICE = "mr-IN-ManoharNeural"
voice_cfg = {"name": VOICE, "style": VOICE_STYLE, "pitch": 0, "rate": 0}
voice_cfg.update(load_json(CONFIG_FILE, {}).get("voice_cfg", {}))


def apply_voice(save=True):
    global VOICE, VOICE_STYLE, VOICE_PITCH, VOICE_RATE, ROBOT_AMOUNT, ROBOT_EFFECT
    base = VOICE_STYLES.get(voice_cfg["style"], VOICE_STYLES["smooth"])
    voice_cfg["pitch"] = max(-30, min(20, voice_cfg["pitch"]))
    voice_cfg["rate"] = max(-25, min(30, voice_cfg["rate"]))
    VOICE, VOICE_STYLE = voice_cfg["name"], voice_cfg["style"]
    VOICE_PITCH = f"{int(base[0][:-2]) + voice_cfg['pitch']:+d}Hz"
    VOICE_RATE = f"{int(base[1][:-1]) + voice_cfg['rate']:+d}%"
    ROBOT_AMOUNT = base[2]
    ROBOT_EFFECT = ROBOT_AMOUNT > 0
    hub.set_info(voice=f"{VOICE.split('-')[-1].replace('Neural', '')} / {VOICE_STYLE}")
    if save:
        cfg = load_json(CONFIG_FILE, {})
        cfg["voice_cfg"] = voice_cfg
        save_json(CONFIG_FILE, cfg)


def voice_skill(t):
    if not re.search(r"\bvoices?\b|\bspeak (?:faster|slower)\b|\btalk (?:faster|slower)\b|\bspeed up\b|\bslow down\b", t):
        return None
    done = "This is how I sound now."
    if re.search(r"\b(reset|default|original)\b.*\bvoice\b", t):
        voice_cfg.update({"name": "en-US-GuyNeural", "style": "smooth", "pitch": 0, "rate": 0})
        apply_voice()
        return f"Voice reset. {done}"
    if re.search(r"\b(which|what|list|available|different|other)\b.*\bvoices?\b|^(?:change|switch) (?:your |the )?voice$", t):
        names = ", ".join(f"{k.title()} ({d})" for k, (_, d) in VOICE_CHOICES.items())
        return ("My voices are: " + names + ". Styles are smooth, robot and plain. "
                "You can also say deeper, higher, faster or slower. For example: change voice to Ryan.")
    for key, (vid, desc) in VOICE_CHOICES.items():
        if re.search(rf"\b{key}\b", t):
            voice_cfg["name"] = vid
            apply_voice()
            return f"Voice changed to {key.title()}, {desc}. {done}"
    for word, key in (("british", "ryan"), ("english accent", "ryan"), ("uk", "ryan"), ("indian", "prabhat"),
                      ("american", "guy")):
        if re.search(rf"\b{word}\b", t):
            voice_cfg["name"] = VOICE_CHOICES[key][0]
            apply_voice()
            return f"Switched to the {VOICE_CHOICES[key][1]} voice. {done}"
    for words, style in (("robot|machine|terminator|metallic|cyborg", "robot"),
                         ("smooth|clear|clean|normal", "smooth"), ("plain|natural|human", "plain")):
        if re.search(rf"\b(?:{words})\b", t):
            voice_cfg["style"] = style
            apply_voice()
            return f"{style.title()} voice style on. {done}"
    if re.search(r"\b(deeper|lower|bass|heavier)\b", t):
        voice_cfg["pitch"] -= 6
        apply_voice()
        return f"Deeper. {done}"
    if re.search(r"\b(higher|lighter|thinner)\b", t):
        voice_cfg["pitch"] += 6
        apply_voice()
        return f"Higher. {done}"
    if re.search(r"\bfaster\b|\bspeed up\b", t):
        voice_cfg["rate"] += 8
        apply_voice()
        return f"Faster. {done}"
    if re.search(r"\bslower\b|\bslow down\b", t):
        voice_cfg["rate"] -= 8
        apply_voice()
        return f"Slower. {done}"
    return None


# ----------------------------- v5: Hindi / Marathi ----------------------
LANG = "en"                                  # language of the current conversation: en / hi / mr
lang_mode = {"force": None}                  # "speak hindi" / "speak english"
DEVANAGARI = re.compile(r"[ऀ-ॿ]")
WAKE_WORDS = WAKE_WORDS + ("नेमेसिस", "नेमसिस", "नेमिसिस", "नेमेसीस", "नेमेसिज")


def resolve_lang(text, ui_lang):
    if lang_mode["force"]:
        return lang_mode["force"]
    if DEVANAGARI.search(text):
        return "mr" if (ui_lang or "").startswith("mr") else "hi"
    return "en"


def pick_voice(text):
    letters = [c for c in text if c.isalpha()]
    if letters and sum(1 for c in letters if DEVANAGARI.match(c)) / len(letters) > 0.3:
        return MARATHI_VOICE if LANG == "mr" else HINDI_VOICE
    return VOICE


_APP_WORDS = {"क्रोम": "chrome", "नोटपैड": "notepad", "कैलकुलेटर": "calculator", "कॅल्क्युलेटर": "calculator",
              "यूट्यूब": "youtube", "युट्युब": "youtube", "गूगल": "google", "गुगल": "google",
              "व्हाट्सएप": "whatsapp", "व्हॉट्सऍप": "whatsapp", "व्हाट्सऐप": "whatsapp", "जीमेल": "gmail",
              "स्पॉटिफाई": "spotify", "फ्रीकैड": "freecad", "पेंट": "paint", "वर्ड": "word", "एक्सेल": "excel"}
_INDIC_RULES = [
    (r"(?:बत्ती|लाइट|लाईट|दिवा|दिवे|batti|bati|light|lights)\s*(?:चालू|ऑन|चालु|लावा|सुरू|chalu|chalao|on)\b", "turn on the lights"),
    (r"(?:बत्ती|लाइट|लाईट|दिवा|दिवे|batti|bati|light|lights)\s*(?:बंद|ऑफ|बंद करो|बंद करा|band|bandh|off)\b", "turn off the lights"),
    (r"(?:हिंदी|हिन्दी|hindi) (?:में|mein|me)? ?(?:बोलो|bolo|बात)|speak hindi|हिंदीत बोला", "speak hindi"),
    (r"(?:मराठी|marathi)(?:त| में| mein)? ?(?:बोला|बोलो|bola|bolo)|speak marathi", "speak marathi"),
    (r"(?:अंग्रेजी|अंग्रेज़ी|इंग्लिश|english) (?:में|मध्ये|mein)? ?(?:बोलो|बोला|bolo)|इंग्रजीत बोला", "speak english"),
    (r"सुप्रभात|शुभ प्रभात|suprabhat", "good morning"),
    (r"कितने बजे|समय क्या|टाइम क्या|वेळ काय|किती वाजले|samay kya|time kya", "what is the time"),
    (r"आज की तारीख|तारीख क्या|आज कौन सा दिन|आजची तारीख|aaj ki tarikh", "what is the date"),
    (r"मौसम|हवामान|mausam|havaman", "weather"),
    (r"खबर|समाचार|बातम्या|khabar|samachar", "news"),
    (r"जोक|चुटकुला|विनोद|chutkula", "tell me a joke"),
    (r"क्या दिख रहा|क्या दिखता|क्या देख रहे|काय दिसत|काय दिसतंय|kya dikh", "what do you see"),
    (r"कितने लोग|किती लोक|kitne log", "how many people"),
    (r"आवाज (?:बदल|बदला|badlo)", "change voice"),
    (r"आवाज (?:बढ़ा|बढा|वाढव|badha)|volume badha", "volume up"),
    (r"आवाज (?:कम|घटव|kam)", "volume down"),
    (r"म्यूट|mute karo", "mute"),
    (r"स्क्रीनशॉट|screenshot lo", "take a screenshot"),
    (r"कंप्यूटर (?:बंद|शटडाउन)|संगणक बंद|computer band", "shut down the computer"),
    (r"कंप्यूटर लॉक|संगणक लॉक|computer lock", "lock the computer"),
    (r"सिस्टम (?:स्टेटस|की स्थिति)|system status", "system status"),
    (r"सो जाओ|झोप|so jao", "go to sleep"),
    (r"रुको|रुक जाओ|चुप|थांब|शांत|ruko|chup", "stop"),
    (r"टाइमर|टायमर|timer", None),            # handled below (needs the number)
]


def indic_command(text):
    """Hindi / Marathi / Hinglish command -> the English command Nemesis already understands, or None."""
    t = text.strip().lower()
    if not t:
        return None
    m = re.search(r"(\d+)\s*(?:मिनट|मिनिट|minute|min)", t)
    if m and re.search(r"टाइमर|टायमर|timer", t):
        return f"set a timer for {m.group(1)} minutes"
    for pat, cmd in _INDIC_RULES:
        if cmd and re.search(pat, t):
            return cmd
    m = re.match(r"(.+?)\s*(?:खोलो|खोल दो|खोलिए|उघड|उघडा|kholo|khol do)$", t)
    if m:
        name = m.group(1).strip()
        return "open " + _APP_WORDS.get(name, name)
    m = re.match(r"(.+?)\s*(?:चलाओ|बजाओ|लगाओ|लावा|वाजवा|chalao|bajao|laga do)$", t)
    if m:
        name = re.sub(r"\s*(?:गाना|गाणे|गाणं|song|gaana)\s*$", "", m.group(1)).strip()
        return f"play {name}" if name else "play song"
    return None


_TR = [
    (r"^Done\.$", ("हो गया।", "झाले.")),
    (r"^Okay, cancelled\.$", ("ठीक है, रद्द कर दिया।", "ठीक आहे, रद्द केले.")),
    (r"^Opening (.+)\.$", ("{0} खोल रहा हूँ।", "{0} उघडत आहे.")),
    (r"^Playing (.+) on YouTube\.$", ("यूट्यूब पर {0} चला रहा हूँ।", "यूट्यूबवर {0} लावत आहे.")),
    (r"^Volume up\.$", ("आवाज़ बढ़ा दी।", "आवाज वाढवला.")),
    (r"^Volume down\.$", ("आवाज़ कम कर दी।", "आवाज कमी केला.")),
    (r"^It's (\d{1,2}:\d{2}) (?:AM|PM)\.$", ("अभी {0} बजे हैं।", "आत्ता {0} वाजले आहेत.")),
    (r"^Timer set for (.+)\.$", ("{0} का टाइमर लगा दिया।", "{0} चा टायमर लावला.")),
    (r"^Noted\.$", ("नोट कर लिया।", "नोंद केली.")),
    (r"^Standing by\.$", ("मैं तैयार हूँ।", "मी तयार आहे.")),
    (r"^Yes\?$", ("जी?", "होय?")),
    (r"^Screenshot saved in the screenshots folder\.$", ("स्क्रीनशॉट सेव हो गया।", "स्क्रीनशॉट सेव्ह झाला.")),
    (r"^Locking the computer\.$", ("कंप्यूटर लॉक कर रहा हूँ।", "संगणक लॉक करत आहे.")),
]


def localize(reply):
    if LANG == "en":
        return reply
    idx = 1 if LANG == "mr" else 0
    for pat, outs in _TR:
        m = re.match(pat, reply)
        if m:
            return outs[idx].format(*m.groups())
    return reply


def language_skill(t):
    if t == "speak hindi":
        lang_mode["force"] = "hi"
        return "ठीक है, अब मैं हिंदी में बोलूँगा।"
    if t == "speak marathi":
        lang_mode["force"] = "mr"
        return "ठीक आहे, आता मी मराठीत बोलेन."
    if t == "speak english":
        lang_mode["force"] = None
        return "Okay, I will speak English."
    return None


# ----------------------------- v5: eyes (vision) ------------------------
eyes = None


def vision_skill(t):
    pat_look = (r"\bwhat (?:do you|can you|are you) (?:see|seeing)\b|\bwhat(?:'s| is) in front of (?:me|you)\b|"
                r"\blook around\b|\bdescribe (?:the |this )?(?:scene|view|room|picture|image)\b|\bwhat(?:'s| is) around me\b")
    pat_this = r"\bwhat(?:'s| is) this\b(?: object)?$|\bwhat am i holding\b|\bidentify (?:this|it|the object)\b|\bwhat object is this\b"
    if re.search(r"\b(?:open|start|turn on|enable|wake)\b.*\b(?:eyes|camera|vision)\b", t):
        hub.emit(t="eyes", on=True)
        return "Opening my eyes. Allow the camera on your phone if it asks."
    if re.search(r"\b(?:close|stop|turn off|disable)\b.*\b(?:eyes|camera|vision)\b", t):
        hub.emit(t="eyes", on=False)
        return "Eyes closed."
    pat_read = (r"\bread (?:this|that|it|the (?:text|label|sign|page|paper|document|book|board|box|bottle|packet|note|writing|menu|bill|receipt|card))\b|"
                r"\bwhat does (?:this|it|that|the \w+) say\b|\bwhat(?:'s| is) written (?:here|on (?:this|it|that|the \w+))\b|"
                r"\bread (?:from |with )?(?:the |my )?camera\b|\bread what(?:'s| is) in front of me\b|\bscan (?:this )?(?:text|page|document|label)\b")
    pat_careful = (r"\b(?:look|scan|check) (?:carefully|closely|properly|in detail)\b|\bdescribe (?:everything|in detail)\b|"
                   r"\bdetailed (?:description|look)\b|\bwhat exactly (?:do you|can you) see\b|\blook again\b")
    pat_colour = r"\bwhat colou?r (?:is|are) (?:the |this |that |my |it)?(.*)$|\bwhat colou?r\b"
    if re.search(pat_read, t) and not re.search(r"\b(?:screen|window|display|desktop)\b", t):
        camera_live = eyes is not None and time.time() - eyes.last_seen < 10
        explicit = re.search(r"camera|label|sign|paper|book|board|box|bottle|packet|note|menu|bill|receipt|card|in front of me|writing", t)
        if camera_live or explicit:
            if eyes is None:
                return "My eyes are not ready."
            if PRIVACY:
                return "Privacy mode is on, so my camera is blocked. Say privacy mode off first."
            if not camera_live:
                hub.emit(t="eyes", on=True)
                return "My eyes were closed. I am opening them. Hold the text in front of the camera and ask again in a few seconds."
            say("Hold it steady. Reading.", "both")
            return eyes.read_text(tools.CTX["lang"]() if tools.CTX.get("lang") and tools.CTX["lang"]() != "en" else None)
    if not (re.search(pat_look, t) or re.search(pat_this, t) or re.search(pat_careful, t) or re.search(pat_colour, t)
            or re.search(r"\b(track|tracking|follow|where (?:is|are)|how many(?! \w+ (?:are |is )?(?:there )?in \d))\b", t)):
        return None
    if eyes is None:
        return "My eyes are not ready."
    if re.search(pat_careful, t):
        say("Looking carefully.", "both")
        return eyes.look_carefully()
    m = re.search(pat_colour, t)
    if m and not re.search(pat_look, t):
        word = (m.group(1) or "").strip() if m.lastindex else ""
        word = re.sub(r"^(?:the|this|that|my|it|a)\b\s*", "", word).strip()
        from vision import canon, COCO_NAMES
        if not word or word in ("it", "this", "that", "object", "thing"):
            return eyes.colour_of(None)
        if canon(word) in COCO_NAMES:
            return eyes.colour_of(word)
        return None
    if re.search(r"\bstop (?:tracking|following)\b|\buntrack\b", t):
        return eyes.stop_tracking()
    if re.search(pat_look, t):
        eyes.load()
        return eyes.describe()
    if re.search(pat_this, t):
        eyes.load()
        return eyes.centre_object()
    m = re.search(r"(?:start )?(?:track|tracking|follow|keep an eye on|watch for) (.+)", t)
    if m:
        from vision import canon, COCO_NAMES
        if canon(m.group(1)) in COCO_NAMES or "track" in t:
            eyes.load()
            return eyes.track(m.group(1))
        return None
    m = re.search(r"how many (.+?)(?: do you see| can you see| are there| are visible| are in front of me| are here)?$", t)
    if m:
        from vision import canon, COCO_NAMES
        if canon(m.group(1)) in COCO_NAMES:
            return eyes.count(m.group(1))
        return None
    m = re.search(r"where (?:is|are) (?:the |my |a )?(.+?)(?: now| right now)?$", t)
    if m:
        from vision import canon, COCO_NAMES
        if canon(m.group(1)) in COCO_NAMES:
            return eyes.where(m.group(1))
    return None


def background_skill(t):
    if re.search(r"\b(?:change|switch|next|cycle)\b.*\b(?:background|backdrop|wallpaper|hologram)\b", t):
        hub.emit(t="bg")
        return "Background changed."
    return None


# ----------------------------- v6: privacy and security -----------------
AUDIT_FILE = os.path.join(BASE_DIR, "nemesis_audit.log")
PRIVACY = bool(load_json(CONFIG_FILE, {}).get("privacy", False))
THINK_SOUND = bool(load_json(CONFIG_FILE, {}).get("think_sound", True))
PHONE_AWAKE_SECONDS = int(load_json(CONFIG_FILE, {}).get("awake_seconds", PHONE_AWAKE_SECONDS))
PRIVACY_MSG = "Privacy mode is on, so I will not go online for that. Say privacy mode off to allow it."
_DIGITS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "for": "4",
           "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9"}


def digits_from(text):
    words = [_DIGITS.get(w, w) for w in re.findall(r"[a-z]+|\d+", text.lower())]
    return "".join(re.sub(r"\D", "", w) for w in words)


def _pin_hash(pin, salt):
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), 100_000).hex()


def set_pin(pin):
    cfg = load_json(CONFIG_FILE, {})
    if pin:
        salt = secrets.token_hex(8)
        cfg["pin"] = {"salt": salt, "hash": _pin_hash(pin, salt)}
    else:
        cfg.pop("pin", None)
    save_json(CONFIG_FILE, cfg)


def pin_is_set():
    return bool(load_json(CONFIG_FILE, {}).get("pin"))


def check_pin(text):
    p = load_json(CONFIG_FILE, {}).get("pin")
    guess = digits_from(text)
    return bool(p and guess) and secrets.compare_digest(_pin_hash(guess, p["salt"]), p["hash"])


def is_secret_text(text):
    """True for sentences that carry a PIN - never shown on the HUD or written to the log."""
    return bool(pending.get("pin") and time.time() < pending["pin"][1]) or \
        bool(re.search(r"\bpin\b.*\d|\bpin\b.*\b(?:one|two|three|four|five|six|seven|eight|nine|zero)\b", text.lower()))


_AUDIT_PREV = ["0"]
_CHAIN = re.compile(r"  #([0-9a-f]{12})$")


def _audit_tail():
    try:
        with open(AUDIT_FILE, encoding="utf-8", errors="ignore") as f:
            last = f.readlines()[-1:]
        m = _CHAIN.search(last[0].rstrip("\n")) if last else None
        return m.group(1) if m else "0"
    except OSError:
        return "0"


def audit(source, text):
    """Every line carries a short fingerprint of the line before it, so edits or deleted lines show up."""
    try:
        if _AUDIT_PREV[0] == "0":
            _AUDIT_PREV[0] = _audit_tail()
        body = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {source:<6} {'(hidden)' if is_secret_text(text) else text}"
        h = hashlib.sha256((_AUDIT_PREV[0] + body).encode("utf-8")).hexdigest()[:12]
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(f"{body}  #{h}\n")
        _AUDIT_PREV[0] = h
    except OSError:
        pass


def verify_audit():
    """(ok, line number of the first break or 0)."""
    try:
        with open(AUDIT_FILE, encoding="utf-8", errors="ignore") as f:
            lines = [ln.rstrip("\n") for ln in f]
    except OSError:
        return True, 0
    prev = None
    for i, ln in enumerate(lines, 1):
        m = _CHAIN.search(ln)
        if not m:
            prev = None                      # an old line from before v12 starts a new chain
            continue
        body = ln[:m.start()]
        if prev is not None and hashlib.sha256((prev + body).encode("utf-8")).hexdigest()[:12] != m.group(1):
            return False, i
        prev = m.group(1)
    return True, 0


def on_new_device(ip, ua):
    """A phone just logged in: tell the PC screen and say so out loud."""
    hub.log("sys", f"New phone connected from {ip}")
    audit("secure", f"new phone from {ip}")
    say("A new phone has connected.", target="pc")


def lock_down_files():
    """Windows: only your own account may read the settings file (it holds the key and PIN hash)."""
    if os.name != "nt":
        return
    user = os.environ.get("USERNAME")
    if not user:
        return
    for p in (CONFIG_FILE, os.path.join(BASE_DIR, "certs", "nemesis.key"), os.path.join(BASE_DIR, "certs", "memory.key"),
              os.path.join(BASE_DIR, "nemesis_mind.dat")):
        if os.path.exists(p):
            try:
                subprocess.run(["icacls", p, "/inheritance:r", "/grant:r", f"{user}:(F)"],
                               capture_output=True, timeout=10)
            except Exception:
                pass


def tidy_old_files():
    """Delete screenshots older than 7 days and trim the command log to its last 2000 lines."""
    try:
        for name in os.listdir(SHOT_DIR):
            fp = os.path.join(SHOT_DIR, name)
            if os.path.isfile(fp) and time.time() - os.path.getmtime(fp) > 7 * 86400:
                os.remove(fp)
    except OSError:
        pass
    for path, keep in ((AUDIT_FILE, 2000), (ERROR_LOG, 400)):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            if len(lines) > keep:
                with open(path, "w", encoding="utf-8") as f:
                    f.writelines(lines[-keep:])
        except OSError:
            pass


def begin_power(action):
    """Called after the user said yes. Asks for the PIN first when one is set."""
    if pin_is_set():
        pending["pin"] = (action, time.time() + 30, 0)
        return "Please say or type your PIN."
    return run_power(action)


def privacy_skill(t):
    global PRIVACY
    if re.search(r"\b(?:privacy mode|private mode)\b", t):
        if re.search(r"\b(?:off|disable|stop|end|exit)\b", t):
            PRIVACY = False
            msg = "Privacy mode off. Camera and internet lookups are allowed again."
        elif re.search(r"\b(?:on|enable|start|activate|turn)\b", t):
            PRIVACY = True
            hub.emit(t="eyes", on=False)
            if eyes:
                eyes.objects, eyes.target = [], None
            msg = "Privacy mode on. The camera is off and I will not use the internet for weather, news or search."
        else:
            return "Privacy mode is " + ("on." if PRIVACY else "off.")
        hub.privacy = PRIVACY
        cfg = load_json(CONFIG_FILE, {})
        cfg["privacy"] = PRIVACY
        save_json(CONFIG_FILE, cfg)
        hub.set_info(privacy="ON" if PRIVACY else "OFF")
        return msg
    m = re.search(r"\b(?:set|change|create)\b.*\bpin\b.*?(\d[\d\s]{2,})", t) or \
        re.search(r"\b(?:set|change|create)\b.*\bpin\b (?:to |as )?(.+)", t)
    if m:
        pin = digits_from(m.group(1))
        if not 4 <= len(pin) <= 8:
            return "A PIN needs four to eight digits."
        set_pin(pin)
        return "PIN saved. I will ask for it before shutting down or restarting."
    if re.search(r"\b(?:remove|delete|clear|disable)\b.*\bpin\b", t):
        set_pin(None)
        return "PIN removed."
    if re.search(r"\b(?:delete|clear|remove|erase)\b.*\bscreenshots?\b", t):
        n = 0
        try:
            for name in os.listdir(SHOT_DIR):
                os.remove(os.path.join(SHOT_DIR, name))
                n += 1
        except OSError:
            pass
        return f"Deleted {n} screenshot{'s' if n != 1 else ''}."
    if re.search(r"\b(?:clear|delete|erase)\b.*\b(?:command log|audit log|the log|my history|history)\b", t):
        history.clear()
        try:
            os.remove(AUDIT_FILE)
        except OSError:
            pass
        _AUDIT_PREV[0] = "0"
        return "History and command log cleared."
    if re.search(r"\b(?:security|privacy) (?:status|report|check)\b", t):
        blocked = sum(1 for v in FAILS.values() if v[1] > time.time())
        return (f"Privacy mode is {'on' if PRIVACY else 'off'}. PIN is {'set' if pin_is_set() else 'not set'}. "
                f"The phone link is {'encrypted' if SECURE_LINK else 'not encrypted'}. "
                f"{len(SESSIONS)} phone{'s are' if len(SESSIONS) != 1 else ' is'} logged in. "
                f"{blocked} device{'s are' if blocked != 1 else ' is'} blocked right now. "
                f"{files_status_text()} "
                f"Offline mode is {'on' if OFFLINE else 'off'}. "
                "Camera pictures are never saved.")
    if re.search(r"\b(?:lock|log out|sign out|disconnect)\b.*\b(?:phones?|devices?|link|nemesis|everyone|all)\b|"
                 r"\b(?:lock|log out) (?:everything|all)\b", t):
        n = revoke_all()
        audit("secure", "all phones logged out")
        say(f"Done. {n} phone{'s' if n != 1 else ''} logged out. To connect again, scan the QR code on the PC screen.", target="pc")
        return tools.SILENT
    if re.search(r"\b(?:change|new|reset|rotate|renew|regenerate)\b.*\b(?:access key|phone key|link|secret key)\b|"
                 r"\bnew (?:key|link)\b", t):
        cfg = load_json(CONFIG_FILE, {})
        cfg["key"] = "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(14))
        save_json(CONFIG_FILE, cfg)
        set_key(cfg["key"])
        port = HUD_PORT + 1 if SECURE_LINK else HUD_PORT
        hub.set_link(phone_url(port, cfg["key"], SECURE_LINK))
        hub.emit_local(t="showlink")
        audit("secure", "access key changed")
        say("New access key made. All phones are logged out. Scan the new QR code on the PC screen.", target="pc")
        return tools.SILENT
    if re.search(r"\b(?:show|display|open)\b.*\b(?:qr|phone link|link)\b", t):
        hub.emit_local(t="showlink")
        return "The QR code is on the PC screen."
    if re.search(r"\b(?:hide)\b.*\b(?:qr|phone link|link)\b", t):
        hub.emit_local(t="hidelink")
        return "Hidden."
    return None


SECURE_LINK = False


# ----------------------------- v8: health check -------------------------
def _module_ok(name):
    import importlib.util
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def run_diagnostics():
    """Check every part of Nemesis. Prints a table, returns a short spoken summary."""
    import socket
    checks = []

    def add(name, ok, detail=""):
        checks.append((name, ok, detail))

    try:
        resp = ollama.list()
        models = getattr(resp, "models", None) or resp.get("models", [])
        names = [getattr(m, "model", None) or (m.get("model") if hasattr(m, "get") else "") or "" for m in models]
        have = any(n == OLLAMA_MODEL or n.startswith(OLLAMA_MODEL + ":") or n.split(":")[0] == OLLAMA_MODEL for n in names)
        add("Brain (Ollama)", have, OLLAMA_MODEL + (" ready" if have else f" not installed - run: ollama pull {OLLAMA_MODEL}"))
    except Exception:
        add("Brain (Ollama)", False, "Ollama is not running - start it from the Start menu")
    try:
        socket.create_connection(("speech.platform.bing.com", 443), 4).close()
        add("Voice service (internet)", True, "reachable")
    except OSError:
        add("Voice service (internet)", False, "no internet - I cannot speak without it")
    add("PC speaker", PC_AUDIO, SPEAKER_NAME if PC_AUDIO else "no audio output on this PC")
    add("Phone link", True, "connected" if hub.phones > 0 else "no phone connected right now (normal until you open the link)")
    add("Encrypted link", SECURE_LINK, "https" if SECURE_LINK else "plain http - run: pip install cryptography")
    add("Camera vision", _module_ok("ultralytics"),
        ("model loaded" if eyes and eyes.model else "installed") if _module_ok("ultralytics") else "run: pip install ultralytics")
    t_ok = bool(tools.tesseract_cmd()) and _module_ok("pytesseract")
    add("Reading text (OCR)", t_ok, "Tesseract found" if t_ok else "install Tesseract and pip install pytesseract")
    ok_h, why_h = (False, "run: pip install faster-whisper") if not _module_ok("faster_whisper") else ears.ready("en")
    add("Offline hearing", ok_h, "ready" if ok_h else (why_h or "model missing")[:90])
    vs = offline.sapi_voices()
    add("Offline voice", bool(vs), ("Windows voice: " + (offline.pick_voice("en") or vs[0]["name"])) if vs else "no Windows voice found")
    fstat, fchanged, fmissing = guard.verify()
    add("Nemesis files unchanged", fstat != "changed", {"ok": "sealed and intact", "unsealed": "not sealed yet (python nemesis.py --seal)",
        "changed": "CHANGED: " + ", ".join(fchanged + fmissing)}[fstat])
    aud_ok, aud_line = verify_audit()
    add("Command log intact", aud_ok, "intact" if aud_ok else f"edited near line {aud_line}")
    add("Word documents", _module_ok("docx"), "ready" if _module_ok("docx") else "run: pip install python-docx")
    add("Screenshots", _module_ok("PIL"), "ready" if _module_ok("PIL") else "run: pip install pillow")
    add("CPU and RAM stats", _module_ok("psutil"), "ready" if _module_ok("psutil") else "run: pip install psutil")
    for fname in ("hud.html", "phone.html", "tools.py", "vision.py", "hud_server.py", "qrmini.py", "offline.py", "guard.py", "mind.py"):
        if not os.path.exists(os.path.join(BASE_DIR, fname)):
            add("File " + fname, False, "missing from the Nemesis folder")
    try:
        free = shutil.disk_usage(os.path.splitdrive(BASE_DIR)[0] + os.sep).free / 1e9
        add("Disk space", free > 5, f"{free:.0f} GB free")
    except Exception:
        pass
    try:
        import psutil
        avail = psutil.virtual_memory().available / 1e9
        add("Free memory", avail > 1.2, f"{avail:.1f} GB free")
    except Exception:
        pass
    add("Privacy mode", True, "on" if PRIVACY else "off")
    add("Light controller", LIGHT.connected, f"Arduino on {LIGHT.port_name}, light {'on' if LIGHT.state else 'off'}" if LIGHT.connected
        else (LIGHT.last_error or "not connected (only needed for the light)"))
    add("Face models", not _face_models_missing(), "ready" if not _face_models_missing() else "download automatically the first time you use a face command")
    add("PIN", True, "set" if pin_is_set() else "not set")
    with rem_lock:
        add("Reminders", True, f"{len(reminders)} waiting")

    print("\n=== Nemesis health check ===")
    for name, ok, detail in checks:
        print(f"  [{'OK' if ok else '!!'}] {name}: {detail}")
    bad = [c for c in checks if not c[1]]
    hub.log("sys", f"Health check: {len(checks) - len(bad)} ok, {len(bad)} problems")
    if not bad:
        return f"All systems nominal. {len(checks)} checks passed."
    names = ", ".join(c[0] for c in bad[:3])
    return (f"{len(bad)} problem{'s' if len(bad) != 1 else ''}: {names}"
            f"{' and more' if len(bad) > 3 else ''}. The details are in the terminal window.")


def core_skill(t):
    m = re.fullmatch(r"(?:smart commands|command router|router) (on|off|enable|disable)", t)
    if m:
        CORE.router_on = m.group(1) in ("on", "enable")
        cfg = load_json(CONFIG_FILE, {})
        cfg["router"] = CORE.router_on
        save_json(CONFIG_FILE, cfg)
        return "Smart commands on. I will try to understand loosely worded commands." if CORE.router_on else \
            "Smart commands off. I only follow the exact command phrases."
    if re.search(r"\b(?:self[- ]?test|run the tests?)\b.*\b(?:commands?|phrases)\b|\bcommand test\b", t):
        import selftest
        ok, bad, _ = selftest.run(sys.modules[__name__], quiet=True)
        return f"Command test: {ok} passed, {bad} failed." + (" Details are in the terminal." if bad else " All good.")
    if re.search(r"\b(?:are you|is nemesis) frozen\b", t):
        return "Yes, I am frozen." if CORE.frozen else "No, I am running normally."
    return None


def extra_skill(t):
    """All the v4 skills. Returns a reply or None."""
    r = smalltalk_skill(t)
    if r:
        return r
    r = privacy_skill(t)
    if r:
        return r
    r = core_skill(t)
    if r:
        return r
    r = light_skill(t)
    if r:
        return r
    r = face_skill(t)
    if r:
        return r
    r = wake_skill(t)
    if r:
        return r
    r = status_skill(t)
    if r:
        return r
    r = guard_skill(t)
    if r:
        return r
    r = offline_skill(t)
    if r:
        return r
    r = mind.skill(t)
    if r:
        return r
    r = brain_mode_skill(t)
    if r:
        return r
    if re.search(r"\bspeed (?:report|test|check)\b|\bhow fast are you\b", t):
        return speed_report()
    if re.search(r"\b(?:diagnostics?|self[- ]?test|health check|system check)\b|\bcheck (?:yourself|your systems?)\b", t):
        return run_diagnostics()
    r = language_skill(t)
    if r:
        return r
    r = voice_skill(t)
    if r:
        return r
    r = background_skill(t)
    if r:
        return r
    r = vision_skill(t)
    if r:
        return r
    r = tools.skill(t)
    if r:
        return r
    r = reminder_skill(t)
    if r:
        return r
    if re.fullmatch(r"good (?:morning|afternoon|evening)|(?:daily|morning) (?:briefing|brief)|brief me|give me (?:my |the )?(?:briefing|update)", t):
        return daily_briefing()
    r = pc_skill(t)
    if r:
        return r
    r = unit_skill(t)
    if r:
        return r
    r = weather_skill(t)
    if r:
        return r
    r = news_skill(t)
    if r:
        return r
    if re.search(r"\b(tell me a joke|make me laugh|joke)\b", t):
        return random.choice(JOKES)
    if re.search(r"\bflip a coin\b|\btoss a coin\b", t):
        return random.choice(["Heads.", "Tails."])
    if re.search(r"\broll (?:a |the )?(?:dice|die)\b", t):
        return f"You rolled a {random.randint(1, 6)}."
    m = re.search(r"\brandom number (?:between )?(\d+) (?:and|to) (\d+)", t)
    if m:
        a, b = sorted((int(m.group(1)), int(m.group(2))))
        return f"{random.randint(a, b)}."
    return wiki_skill(t)


LOW_RAM = {"on": False, "n": 0, "held": False}


def resource_guard(ram):
    """RAM above 93% for ~12 s: pause the camera and background work so voice stays responsive. Back at 85%."""
    if ram >= 93:
        LOW_RAM["n"] += 1
    elif ram < 85:
        LOW_RAM["n"] = 0
    if not LOW_RAM["on"] and LOW_RAM["n"] >= 3:
        LOW_RAM["on"] = True
        if not CORE.frozen:
            hub.privacy = True
            hub.emit(t="eyes", on=False)
            LOW_RAM["held"] = True
        hub.set_info(lowram="YES")
        hub.log("sys", "Memory is very low - camera paused to keep me fast")
        say("Memory is very low. I paused the camera to stay fast. Close some programs.")
    elif LOW_RAM["on"] and ram < 85:
        LOW_RAM["on"] = False
        if LOW_RAM["held"] and not CORE.frozen:
            hub.privacy = PRIVACY
        LOW_RAM["held"] = False
        hub.set_info(lowram="NO")
        hub.log("sys", "Memory is back to normal - camera allowed again")


def stats_loop():
    """Feeds CPU / RAM / weather to the HUD."""
    try:
        import psutil
    except ImportError:
        print("[hint] pip install psutil   (shows CPU and RAM on the HUD)")
        psutil = None
    if psutil:
        psutil.cpu_percent(None)
    tick = 0
    while True:
        time.sleep(4)
        try:
            if eyes:
                hub.set_info(camera="ON" if time.time() - eyes.last_seen < 5 else "OFF")
            with rem_lock:
                nxt = min(reminders, key=lambda r: r["when"], default=None)
            hub.set_info(next=(datetime.datetime.fromtimestamp(nxt["when"]).strftime("%H:%M ") +
                               re.sub(r"^(?:Reminder: )", "", nxt["text"])[:22]) if nxt else "NONE",
                         pin="SET" if pin_is_set() else "NOT SET")
            if psutil:
                ram = psutil.virtual_memory().percent
                hub.set_info(cpu=f"{psutil.cpu_percent(None):.0f}%", ram=f"{ram:.0f}%")
                resource_guard(ram)
            if tick % 450 == 2 and saved_city():
                w = fetch_weather(saved_city())
                if w:
                    hub.set_info(weather=f"{w['temp']}C {w['desc']}")
        except Exception:
            pass
        tick += 1


def local_skill(text: str):
    """Fast offline commands. Return a reply string, or None to ask the brain."""
    t = text.lower().strip(" .!?,")
    if not t:
        return None

    # PIN check for power actions (checked before anything else, three tries)
    pp = pending.get("pin")
    if pp:
        pending["pin"] = None
        if time.time() < pp[1]:
            if check_pin(t):
                return run_power(pp[0])
            if pp[2] >= 2:
                return "Wrong PIN. Cancelled."
            pending["pin"] = (pp[0], time.time() + 30, pp[2] + 1)
            return "Wrong PIN. Try again."

    if mind.pending:
        r = mind._pending_answer(t)
        if r:
            return r

    if re.search(r"\bwhat('s| is) the time\b|\bcurrent time\b|\btime is it\b", t):
        return datetime.datetime.now().strftime("It's %I:%M %p.").lstrip("0")
    if re.search(r"\b(today'?s date|what('s| is) the date|what day is it)\b", t):
        return datetime.datetime.now().strftime("Today is %A, %d %B %Y.")
    if re.search(r"\bwhat can you do\b|\bhelp me with what\b|\byour (skills|abilities)\b", t):
        return what_i_can_do()

    # follow-up answers: yes/no for power actions, city for the weather
    c = pending.get("confirm")
    if c:
        pending["confirm"] = None
        if time.time() < c[1]:
            if re.fullmatch(r"(?:yes|yeah|yep|yup|confirm|do it|go ahead|sure|affirmative)(?: please)?", t):
                return begin_power(c[0])
            if re.fullmatch(r"(?:no|nope|cancel|never ?mind|abort|don'?t)(?: please)?", t):
                return "Okay, cancelled."
    if time.time() < pending["city_until"]:
        pending["city_until"] = 0.0
        if not re.search(r"\b(cancel|never ?mind|forget it)\b", t):
            city = re.sub(r"^(?:i am in|i'm in|i live in|my city is|it is|it's|in)\s+", "", t).strip().title()
            save_city(city)
            return weather_reply(city)

    reply = extra_skill(t)
    if reply:
        return reply

    # music on YouTube (checked before the media keys)
    if time.time() < pending["song_until"]:
        if re.search(r"\b(cancel|never ?mind|forget it)\b", t):
            pending["song_until"] = 0.0
            return "Okay, cancelled."
        q = clean_query(re.sub(r"^(?:play|search(?: for)?)\s+", "", t))
        if q in ("", "song", "songs", "music", "a song", "some music", "something"):
            pending["song_until"] = time.time() + 25
            return "Please tell me the song name."
        pending["song_until"] = 0.0
        return play_on_youtube(q)
    if (re.fullmatch(r"(?:open|launch|start)(?: the)? youtube(?: and| then)? play(?: a| some| the| any)? (?:song|songs|music|something)", t)
            or re.fullmatch(r"play(?: a| some| the)? (?:song|songs)", t)):
        pending["song_until"] = time.time() + 25
        return "Which song should I play?"
    if re.fullmatch(r"(?:(?:pause|stop|resume|play)(?: the)? (?:music|song|songs|playback|track|video)|pause|resume)", t):
        return "Done." if press_key(VK_MEDIA_PLAY) else "I could not control the media."
    m = (re.match(r"(?:open|launch|start)(?: the)? youtube(?: and| then)? (?:play|search(?: for)?) (.+)", t)
         or re.match(r"(?:search|find|look up) (.+?) on youtube$", t)
         or re.match(r"play (.+)", t))
    if m:
        q = clean_query(m.group(1))
        if q:
            return play_on_youtube(q)

    # volume + media
    if re.search(r"\b(volume up|louder|turn (it|the volume) up|increase (the )?volume)\b", t):
        return "Volume up." if press_key(VK_VOLUME_UP, 5) else "I could not change the volume."
    if re.search(r"\b(volume down|quieter|turn (it|the volume) down|lower the volume|decrease (the )?volume)\b", t):
        return "Volume down." if press_key(VK_VOLUME_DOWN, 5) else "I could not change the volume."
    if re.search(r"\b(unmute|mute)\b", t):
        return "Done." if press_key(VK_VOLUME_MUTE) else "I could not change the volume."
    if re.search(r"\bnext (song|track)\b", t):
        return "Next track." if press_key(VK_MEDIA_NEXT) else "I could not control the media."
    if re.search(r"\bprevious (song|track)\b", t):
        return "Previous track." if press_key(VK_MEDIA_PREV) else "I could not control the media."

    # notes
    m = re.match(r"(?:take a note|make a note|note down|note that|write down)\s+(.+)", t)
    if m:
        with open(NOTES_FILE, "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M}  {m.group(1)}\n")
        return "Noted."
    if re.search(r"\b(read|show|what are) (my |the )?notes\b", t):
        try:
            with open(NOTES_FILE, "r", encoding="utf-8") as f:
                lines = [ln.strip().split("  ", 1)[-1] for ln in f.readlines()[-3:]]
            return ("Your latest notes: " + ". ".join(lines)) if lines else "You have no notes."
        except OSError:
            return "You have no notes."


    # apps, websites, search (these open on the PC)
    m = re.match(r"(?:open|launch|start) (.+)", t)
    if m:
        target = m.group(1).replace("the ", "").strip()
        if target in APPS:
            subprocess.Popen(f"start {APPS[target]}", shell=True)
            return f"Opening {target}."
        if target in SITES:
            webbrowser.open(SITES[target])
            return f"Opening {target}."
        if re.fullmatch(r"[\w-]+(?:\.[\w-]+)*\.(?:com|org|net|in|io|co|edu|gov|ai)", target):
            webbrowser.open("https://" + target)
            return f"Opening {target}."
        if len(target.split()) <= 3:
            found = open_start_menu_app(target)
            return f"Opening {found}." if found else f"I could not find an app called {target}."
    m = re.match(r"(?:search|google|look up) (?:for )?(.+)", t)
    if m:
        webbrowser.open("https://www.google.com/search?q=" + m.group(1).replace(" ", "+"))
        return f"Searching for {m.group(1)}."
    return None


# ----------------------------- brain ------------------------------------
history = []


def build_system_prompt():
    prompt = SYSTEM_PROMPT
    if LANG == "hi":
        prompt += " The user speaks Hindi. Always reply in simple Hindi using Devanagari script."
    elif LANG == "mr":
        prompt += " The user speaks Marathi. Always reply in simple Marathi using Devanagari script, with short sentences."
    prompt += mind.facts_prompt()
    return prompt


def brain_options(max_tokens):
    """The SAME options for every call, so Ollama never reloads the model between calls."""
    try:
        import psutil
        threads = psutil.cpu_count(logical=False) or 4
    except Exception:
        threads = 4
    return {"num_predict": max_tokens, "num_ctx": 2048, "num_thread": threads, "temperature": 0.6}


def installed_models():
    try:
        resp = ollama.list()
        models = getattr(resp, "models", None) or resp.get("models", [])
        return [getattr(m, "model", None) or (m.get("model") if hasattr(m, "get") else "") or "" for m in models]
    except Exception:
        return []


def has_model(name, names=None):
    names = installed_models() if names is None else names
    return any(n == name or n.startswith(name) for n in names)


def choose_brain():
    """Fast brain if installed and chosen, otherwise the smart one."""
    global OLLAMA_MODEL
    cfg = load_json(CONFIG_FILE, {})
    names = installed_models()
    want = cfg.get("brain")
    if want is None:                                   # first run of v9: prefer speed when possible
        want = "fast" if has_model(FAST_MODEL, names) else "smart"
    if want == "fast" and not has_model(FAST_MODEL, names):
        want = "smart"
        print(f"[speed] For faster answers run once:  ollama pull {FAST_MODEL}   then say 'fast mode'.")
    OLLAMA_MODEL = FAST_MODEL if want == "fast" else SMART_MODEL


def brain_mode_skill(t):
    global OLLAMA_MODEL
    m = re.fullmatch(r"(?:switch to |use |go to |turn on |enable )?(fast|quick|smart|clever|accurate)(?: brain| mode)?(?: please)?", t)
    if not m or not re.search(r"\b(?:brain|mode)\b|^(?:fast|smart)$", t):
        return None
    fast = m.group(1) in ("fast", "quick")
    name = FAST_MODEL if fast else SMART_MODEL
    if not has_model(name):
        return f"That brain is not installed. In the terminal run: ollama pull {name}"
    old = OLLAMA_MODEL
    OLLAMA_MODEL = name
    cfg = load_json(CONFIG_FILE, {})
    cfg["brain"] = "fast" if fast else "smart"
    save_json(CONFIG_FILE, cfg)
    hub.set_info(model=OLLAMA_MODEL)

    def swap():
        try:
            if old != name:
                ollama.generate(model=old, prompt="", keep_alive=0)       # free the memory
            ollama.generate(model=name, prompt="Hi", keep_alive="30m", options=brain_options(1))
        except Exception:
            pass
    threading.Thread(target=swap, daemon=True).start()
    return ("Fast brain on. Shorter answers, quicker replies." if fast else
            "Smart brain on. Better answers, a little slower.")


def warm_up_brain():
    """Load the model into memory in the background so the first answer is faster."""
    try:
        ollama.generate(model=OLLAMA_MODEL, prompt="Hi", keep_alive="30m", options=brain_options(1))
        hub.log("sys", "Brain loaded")
    except Exception as e:
        msg = str(e)
        print(f"[brain warning] {msg}")
        if "not found" in msg.lower():
            print(f"    Run this once in the terminal:  ollama pull {OLLAMA_MODEL}")
        else:
            print("    Make sure the Ollama app is running (Start menu > Ollama).")
        hub.log("sys", "Brain not ready - is Ollama running?")


SMALLTALK = [
    (r"(?:hello|hi|hey|hey there|hello there|namaste|good day)", "Hello. How can I help?"),
    (r"(?:how are you|how are you doing|how do you do|are you ok|are you okay|how is it going)", "All systems nominal. How can I help?"),
    (r"(?:thank you|thanks|thank you very much|thanks a lot|ok thanks|okay thanks|great thanks)", "You are welcome."),
    (r"(?:who are you|what are you|what is your name|what's your name|tell me your name)", "I am Nemesis, your personal assistant."),
    (r"(?:good night|goodnight)", "Good night. Standing by."),
    (r"(?:नमस्ते|नमस्कार|हैलो|हेलो)", "नमस्ते। मैं आपकी क्या मदद कर सकता हूँ?"),
]


def smalltalk_skill(t):
    """Greetings and thanks are answered instantly, without waiting for the brain."""
    for pat, reply in SMALLTALK:
        if re.fullmatch(pat, t):
            return reply
    return None


def think_and_speak(user_text: str):
    hub.set_state("thinking")
    history.append({"role": "user", "content": user_text})
    full, buf, held, first = "", "", "", True
    my_gen = STOP_GEN
    state = {"said": False}

    def _thinking_sound():                      # the brain is slow: say a short word so the silence is not awkward
        if not state["said"] and STOP_GEN == my_gen and LANG == "en" and THINK_SOUND and not OFFLINE:
            say(random.choice(FILLERS))

    timer = threading.Timer(2.2, _thinking_sound)
    timer.daemon = True
    timer.start()
    fast = OLLAMA_MODEL == FAST_MODEL
    limit = (110 if fast else 170) if LANG == "en" else 300
    try:
        messages = [{"role": "system", "content": build_system_prompt()}] + history[-6:]
        for part in ollama.chat(model=OLLAMA_MODEL, messages=messages, stream=True,
                                keep_alive="30m", options=brain_options(limit)):
            if STOP_GEN != my_gen:      # the user said stop
                break
            chunk = part["message"]["content"]
            if not TURN["brain"] and not TURN["done"]:
                TURN["brain"] = time.time()
            full += chunk
            buf += chunk
            while True:  # speak as it streams in
                m = re.search(r"(?<=[.!?।])\s+", buf)
                if not m and first and len(buf) >= 30:       # first words: do not wait for the full stop
                    m = re.compile(r"(?<=[,;:])\s+").search(buf, 22)
                if not m:
                    break
                sentence, buf = buf[:m.start()].strip(), buf[m.end():]
                if not sentence:
                    continue
                if first:
                    state["said"] = True
                    TURN["said"] = TURN["said"] or time.time()
                    say(sentence)
                    first = False
                else:
                    held = (held + " " + sentence).strip()
                    if len(held) >= 45:                   # two voices are made in parallel, so send sooner
                        say(held)
                        held = ""
        tail = (held + " " + buf).strip()
        if tail and STOP_GEN == my_gen:
            state["said"] = True
            TURN["said"] = TURN["said"] or time.time()
            say(tail)
    except Exception as e:
        print(f"[brain error] {e}")
        full = "Sorry, my brain is not responding. Is Ollama running?"
        state["said"] = True
        say(full)
    finally:
        timer.cancel()
    history.append({"role": "assistant", "content": full or "..."})


def generate_text(prompt, max_tokens=600):
    """One-shot answer from the local brain (used for writing documents)."""
    r = ollama.chat(model=OLLAMA_MODEL, messages=[
        {"role": "system", "content": "You are a careful writing assistant. Output only the requested text."},
        {"role": "user", "content": prompt}], keep_alive="30m", options=brain_options(max_tokens))
    return r["message"]["content"]


# ----------------------------- v12: offline mode, PC guard, memory -------
ears = offline.Ears(lambda who, text: hub.log(who, text))
mind = Mind(generate=lambda p, n=600: generate_text(p, n))
LAST_FINDINGS = []


def _on_freeze(on):
    """Emergency freeze: silence, no camera, no typing, no pending power action."""
    if on:
        stop_speaking()
        for k in ("confirm", "pin"):
            pending[k] = None
        pending["city_until"] = pending["song_until"] = 0.0
        try:
            tools.DICT["on"] = False
        except Exception:
            pass
        try:
            subprocess.Popen(["shutdown", "/a"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        hub.privacy = True
        hub.emit(t="eyes", on=False)
        if eyes:
            eyes.objects, eyes.target = [], None
        hub.set_info(frozen="YES")
        try:
            LIGHT.set(False)
        except Exception:
            pass
        hub.log("sys", "FROZEN - say 'unfreeze Nemesis'")
    else:
        hub.privacy = PRIVACY
        hub.set_info(frozen="NO")
        hub.log("sys", "Unfrozen")


CORE = core.Core(pin_is_set=lambda: pin_is_set(), check_pin=lambda p: check_pin(p),
                 audit=lambda src, txt: audit(src, txt), busy=lambda: skills_waiting(),
                 on_freeze=_on_freeze)
CORE.router_on = bool(load_json(CONFIG_FILE, {}).get("router", True))


_CHAT_LINES = ("All systems nominal. Awaiting your command.",
               "Everything we say stays on this machine. I like it that way.",
               "The first computer bug was a real moth, found in a relay in 1947.",
               "Light travels about three hundred thousand kilometres every second.",
               "An octopus has three hearts.",
               "Honey almost never spoils. Jars thousands of years old have been found still good.",
               "I am here whenever you need me. Just say my name.")


def status_skill(t):
    if re.fullmatch(r"(?:please )?(?:just )?(?:say|speak|talk|tell me)(?: to me)?(?: something| anything)?(?: to me)?(?: please)?", t) and re.search(r"something|anything|talk", t):
        return random.choice(_CHAT_LINES)
    if re.search(r"\b(?:update|upgrade|version) status\b|\bare you up to date\b|\b(?:which|what) version\b|\byour (?:current )?(?:version|update status)\b|\bcurrent update status\b", t):
        return (f"I am {NAME} version {VERSION}. I do not update myself. A new version arrives as files from you, "
                "and the self-test runs before it is installed. Say system check for a full health report.")
    return None


# ----------------------------- v16: how long I stay awake, thinking sound -------
_NW = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30}


def _set_cfg(**kw):
    cfg = load_json(CONFIG_FILE, {})
    cfg.update(kw)
    try:
        save_json(CONFIG_FILE, cfg)
    except OSError:
        pass


def wake_skill(t):
    global PHONE_AWAKE_SECONDS, THINK_SOUND
    if re.search(r"\bhow long (?:do you|will you|are you) (?:stay|staying|keep|awake)\b|\bawake time\b", t):
        n = PHONE_AWAKE_SECONDS
        return f"I stay awake for {n // 60} minutes after each answer." if n >= 120 else f"I stay awake for {n} seconds after each answer."
    m = re.search(r"\b(?:stay|keep|remain)\s+(?:awake|listening)\b(?:\s+(?:for\s+)?(\d+|a|an|one|two|three|four|five|ten|fifteen|twenty|thirty)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?))?", t)
    if m:
        if m.group(1):
            n = int(m.group(1)) if m.group(1).isdigit() else _NW[m.group(1)]
            u = m.group(2)
            secs = n if u.startswith("sec") else n * 3600 if u.startswith(("hour", "hr")) else n * 60
            secs = max(10, min(secs, 8 * 3600))
        else:
            secs = 600
        PHONE_AWAKE_SECONDS = secs
        _set_cfg(awake_seconds=secs)
        return f"Okay. After each answer I will stay awake for {secs // 60} minutes. You do not need to say my name until then." if secs >= 60 else f"Okay. I will stay awake for {secs} seconds after each answer."
    if re.search(r"\b(?:normal|default|usual)\s+(?:listening|awake|sleep)\b|\bback to normal listening\b|\bstop staying awake\b", t):
        PHONE_AWAKE_SECONDS = 30
        _set_cfg(awake_seconds=30)
        return "Okay. I will go back to sleep 30 seconds after each answer. Say my name to wake me."
    m = re.search(r"\bthinking (?:sound|word)s?\b.*\b(on|off)\b|\b(?:turn|switch)\s+(on|off)\s+(?:the )?thinking (?:sound|word)s?\b", t)
    if m:
        THINK_SOUND = (m.group(1) or m.group(2)) == "on"
        _set_cfg(think_sound=THINK_SOUND)
        return "Okay. I will say a short word while I think." if THINK_SOUND else "Okay. I will stay silent while I think."
    return None


# ----------------------------- v15: faces (detect + recognise people you enrol) ------
FACES = faces.Faces(log=lambda m: print(f"[faces] {m}"))


def _face_frame():
    jpeg, fid = hub.frame, hub.frame_id
    if not jpeg or eyes is None:
        return None, fid
    return eyes.decode(jpeg), fid


def _face_ready():
    """None when the camera is live, otherwise the sentence to say."""
    if eyes is None:
        return "My eyes are not ready."
    if PRIVACY:
        return "Privacy mode is on, so my camera is blocked. Say privacy mode off first."
    if not hub.frame or time.time() - eyes.last_seen > 10:
        hub.emit(t="eyes", on=True)
        return "My eyes are closed. I am opening them. Allow the camera on your phone, then ask again in a few seconds."
    return None


def _face_models_missing():
    return any(not os.path.exists(os.path.join(faces.MODEL_DIR, n)) for n in faces.MODELS)


def face_skill(t):
    pat_forget = r"\b(?:forget|delete|erase|remove|clear)\b.*\bfaces?\b"
    pat_list = r"\b(?:whose|which|what) faces\b|\blist (?:the |my )?(?:saved |known )?faces\b|\bwho do you know\b|\bhow many faces do you know\b"
    pat_enrol = r"\b(?:remember|learn|save|enrol+|register|add|store)\b.*\b(?:my|his|her|their|this)\b.*\bface\b"
    pat_who = (r"\bwho am i\b|\bwho is this\b|\bwho(?:'s| is) (?:in front of (?:me|you|the camera)|there|that)\b|"
               r"\b(?:do you )?(?:recogni[sz]e|know|identify) (?:me|my face|who i am|this (?:person|face)|who this is)\b|"
               r"\bcan you (?:recogni[sz]e|identify)(?: me| my face| faces?| people)?$|\bface (?:recognition|recogni[sz]e|id)\b")
    pat_detect = (r"\b(?:detect|see|find|spot|look for|check for)\b.*\b(?:my |a |any |the )?faces?\b|\bis (?:there|anyone|somebody|someone)\b.*\b(?:face|in front of)\b|"
                  r"\bhow many faces\b|\bface detect(?:ion|or)?\b|\bdo you see (?:a |any )?(?:face|me)\b")
    if re.search(pat_forget, t):
        if re.search(r"\b(?:all|every|everyone|everybody|saved|stored)\b", t):
            return FACES.forget(None)
        m = re.search(r"\bfaces? (?:of|for) ([a-z][a-z .'-]{0,23})$|\b([a-z]{2,20})'s face\b", t)
        who = (m.group(1) or m.group(2)) if m else None
        if who and who not in ("my", "the", "this", "that"):
            return FACES.forget(who)
        names = FACES.names()
        if len(names) == 1:
            return FACES.forget(names[0])
        if not names:
            return "I have no saved faces."
        return "Whose face? Say forget the face of, then the name. Or say forget all faces."
    if re.search(pat_list, t):
        return FACES.list_text()
    if re.search(pat_enrol, t):
        m = re.search(r"\bas\s+([a-z][a-z .'-]{0,23})$|\b(?:name is|called)\s+([a-z][a-z .'-]{0,23})$|\bface of ([a-z][a-z .'-]{0,23})$", t)
        name = next((g for g in (m.groups() if m else ()) if g), None)
        if not name:
            return "Say remember my face as, then the name. Only save a face with that person's permission."
        bad = _face_ready()
        if bad:
            return bad
        if _face_models_missing():
            say("Downloading the face models one time. This can take a minute.", "both")
        return FACES.enrol(name, _face_frame, say)
    if re.search(pat_who, t) or re.search(pat_detect, t):
        bad = _face_ready()
        if bad:
            return bad
        if _face_models_missing():
            say("Downloading the face models one time. This can take a minute.", "both")
        frame, _ = _face_frame()
        if frame is None:
            return "I have no picture yet. Hold the camera steady and ask again."
        if re.search(pat_who, t) or FACES.names():
            return FACES.who_text(frame)
        return FACES.count_text(frame)
    return None


# ----------------------------- v14: light control (Arduino + relay) ------
def _light_changed(label):
    try:
        hub.set_info(light=label)
    except Exception:
        pass
    if label != "NOT CONNECTED":
        hub.log("sys", f"Light: {label}")


LIGHT = home.Light(on_change=_light_changed, announce=lambda t: say(t))

_L_NOUN = r"(?:lights?|lighting|leds?|led strip|led lights?|strip(?: light)?s?|lamps?|room light)"
_NUMW = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "sixty": 60}


def _light_minutes(t):
    m = re.search(r"\b(for|in|after)\s+(\d+|a|an|one|two|three|four|five|ten|fifteen|twenty|thirty|sixty)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?)\b", t)
    if not m:
        return None, None
    n = int(m.group(2)) if m.group(2).isdigit() else _NUMW[m.group(2)]
    unit = m.group(3)
    mins = max(1, round(n / 60)) if unit.startswith("sec") else n * 60 if unit.startswith(("hour", "hr")) else n
    return m.group(1), int(mins)


def light_skill(t):
    # relay calibration: "is the red light on the relay lit?"
    if time.time() < pending.get("light_cal", 0.0):
        pending["light_cal"] = 0.0
        if core.YES_RE.match(t):
            LIGHT.set(False)
            return "Good. The relay logic is correct. The light is off again."
        if core.NO_RE.match(t):
            try:
                pol = LIGHT.flip_polarity()
            except home.LightError as e:
                return str(e)
            return f"I reversed the relay logic to {pol} and saved it. Say turn on the lights to test it."
    if re.search(r"\b(?:calibrate|test|check|detect)\b.*\b(?:relay|light relay|light controller polarity)\b|\brelay (?:test|calibration)\b", t):
        ok, text = LIGHT.set(True)
        if not ok:
            return text
        pending["light_cal"] = time.time() + 40
        return "I switched the relay on. Is the red light on the relay board lit? Say yes or no."
    if re.search(r"\b(?:reconnect|connect|find|check)\b.*\b(?:light controller|arduino|light module)\b|\blight controller status\b", t):
        if LIGHT.connected:
            return f"The light controller is connected on {LIGHT.port_name}. The light is {'on' if LIGHT.state else 'off'}."
        return LIGHT.connect()
    if re.search(r"\bcancel\b.*\b(?:light|lights|lighting|led)\b.*\btimer\b|\bcancel (?:the )?light timer\b", t):
        return LIGHT.cancel_timer()
    if re.fullmatch(r"(?:turn|switch|put|power|shut) (?:off|down) (?:everything|all(?: the)? (?:devices|lights)|all)|everything off", t):
        ok, text = LIGHT.set(False)
        return "Everything is off." if ok else text
    if not re.search(r"\b" + _L_NOUN + r"\b", t):
        return None
    if re.search(r"\b(?:is|are|what'?s|check)\b.*\b" + _L_NOUN + r"\b.*\b(?:on|off|working|status|state)\b|\b" + _L_NOUN + r" status\b|\bstatus of (?:the |my )?" + _L_NOUN, t):
        return LIGHT.status_text()
    if re.search(r"\btoggle\b", t):
        return LIGHT.toggle()[1]
    m = re.search(r"\b(?:turn|switch|put|power|set)\b[^.]*?\b(on|off)\b", t) or re.fullmatch(r"(?:the |my )?(?:room )?" + _L_NOUN + r" (on|off)(?: please)?", t) \
        or re.match(r"(?:please )?(?:the |my )?" + _L_NOUN + r" (on|off)\b", t)
    if not m:
        return None
    want_on = m.group(1) == "on"
    kind, mins = _light_minutes(t)
    if mins and kind in ("in", "after"):
        ok, text = (True, "")
        return LIGHT.schedule(want_on, mins)
    ok, text = LIGHT.set(want_on, mins if (want_on and kind == "for") else None)
    return text


def _light_safe_off():
    try:
        LIGHT.shutdown()
    except Exception:
        pass


import atexit
atexit.register(_light_safe_off)


def set_offline(on):
    global OFFLINE
    OFFLINE = on
    cfg = load_json(CONFIG_FILE, {})
    cfg["offline"] = on
    save_json(CONFIG_FILE, cfg)
    hear_ok = bool(on) and ears.ready("en")[0]
    hub.set_info(net="OFFLINE" if on else "ONLINE", offmic="1" if hear_ok else "0")
    hub.emit(t="mode", offline=hear_ok)
    return hear_ok


def stt_from_phone(pcm, lang):
    """Called by the web server with 16 kHz audio from the phone."""
    return ears.transcribe(pcm, (lang or "en")[:2].lower(), allow_download=not OFFLINE)


def offline_skill(t):
    if re.search(r"\b(?:go|switch|turn|change|come)\b.*\bon-?line\b|\boffline mode (?:off|disable|stop)\b|\bstop (?:working )?offline\b|\buse the internet\b", t):
        set_offline(False)
        return "Back online. I will use the internet voice and lookups again."
    if re.search(r"\b(?:go|switch|turn|change|work)\b.*\boffline\b|\boffline mode (?:on|enable|start)\b", t):
        hear_ok, why = ears.ready("en")
        voice_ok = offline.tts_available()
        set_offline(True)
        parts = ["Offline mode is on. I will not use the internet."]
        parts.append("I will use Windows' own voice." if voice_ok else
                     "No Windows voice was found, so I can only speak while the internet is available.")
        parts.append("I will listen with my own speech model." if hear_ok else
                     f"I cannot hear you offline yet. {why}")
        return " ".join(parts)
    if re.search(r"\b(?:are you|is nemesis) (?:in )?offline\b|\boffline (?:status|mode status)\b|\bwhich mode\b", t):
        return f"I am in {'offline' if OFFLINE else 'online'} mode."
    return None


def files_status_text():
    st, changed, missing = guard.verify()
    aud_ok, _ = verify_audit()
    msg = {"ok": "My program files are unchanged since they were sealed.",
           "unsealed": "My program files are not sealed yet.",
           "changed": "Warning. These files changed since they were sealed: " + ", ".join(changed + missing) + "."}[st]
    return msg + ("" if aud_ok else " Warning. The command log has been edited.")


def run_pc_check(announce=True):
    spoken, findings = guard.pc_check()
    LAST_FINDINGS[:] = findings
    real = [f for f in findings if f["sev"] in ("high", "med")]
    hub.set_info(pccheck=f"{len(real)} ALERTS" if real else "CLEAN")
    audit("secure", f"pc check: {len(real)} alerts")
    return spoken


def guard_skill(t):
    if re.search(r"\b(?:check|scan|inspect|search)\b.*\b(?:my |the )?(?:pc|computer|laptop|system)\b.*\b(?:threats?|virus(?:es)?|malware|trojans?|hidden|suspicious|safe|unsafe|infected)\b|"
                 r"\bis my (?:pc|computer|laptop) (?:safe|infected|clean|hacked)\b|\bsecurity scan\b|\bscan for (?:malware|viruses|threats)\b|"
                 r"\bcheck for (?:hidden|suspicious) (?:programs|apps|tasks)\b|\bpc (?:threat )?check\b", t):
        say("Checking your PC. This takes up to a minute.", target="both")
        try:
            return run_pc_check()
        except Exception as e:
            return f"I could not run the PC check: {str(e)[:120]}"
    if re.search(r"\b(?:disable|switch off|turn off|stop)\b.*\bsuspicious (?:scheduled )?tasks\b", t):
        try:
            if not LAST_FINDINGS:
                run_pc_check()
            return guard.disable_tasks(LAST_FINDINGS)
        except Exception as e:
            return f"I could not do that: {str(e)[:120]}"
    if re.search(r"\b(?:stop|kill|end|close)\b.*\bsuspicious (?:programs?|processes|apps)\b", t):
        try:
            if not LAST_FINDINGS:
                run_pc_check()
            return guard.stop_processes(LAST_FINDINGS)
        except Exception as e:
            return f"I could not do that: {str(e)[:120]}"
    if re.search(r"\b(?:verify|check)\b.*\b(?:nemesis|your|my nemesis) (?:files|programs?|code)\b|\bare your files (?:safe|intact|changed)\b", t):
        return files_status_text()
    if re.search(r"\bseal\b.*\b(?:nemesis|your) (?:files|programs?)\b", t):
        return f"Sealed {guard.seal()} files. I will warn you if any of them change."
    if re.search(r"\b(?:check|verify)\b.*\b(?:command log|audit log)\b", t):
        ok, line = verify_audit()
        return "The command log is intact." if ok else f"Warning. The command log was edited near line {line}."
    return None


def background_pc_check():
    """Once a day, a quiet look for hidden startup items (it only reads)."""
    time.sleep(120)
    cfg = load_json(CONFIG_FILE, {})
    if time.time() - cfg.get("last_pc_check", 0) < 86400:
        return
    try:
        spoken = run_pc_check(False)
        cfg = load_json(CONFIG_FILE, {})
        cfg["last_pc_check"] = time.time()
        save_json(CONFIG_FILE, cfg)
        if spoken.startswith("Warning"):
            hub.log("sys", spoken)
            say(spoken.split(" The full list")[0] + " Say check my pc for the details.", target="pc")
    except Exception as e:
        print(f"[pc check skipped] {e}")


def startup_integrity():
    st, changed, missing = guard.verify()
    if st == "unsealed":
        guard.seal()
        st = "ok"
    hub.set_info(files="OK" if st == "ok" else "CHANGED")
    if st == "changed":
        msg = "Some of my program files changed since they were sealed: " + ", ".join(changed + missing)
        print(f"\n[security] {msg}\n  If you just updated Nemesis yourself, run:  python nemesis.py --seal")
        hub.log("sys", msg)
        say("Warning. Some of my program files have changed. " + "Check the terminal window.", target="pc")
    ok, line = verify_audit()
    if not ok:
        hub.log("sys", f"The command log was edited near line {line}")


def skills_waiting():
    """True while an older skill waits for yes / PIN / a city / a song name: the gate then lets the word through."""
    c = pending.get("confirm")
    return bool(pending.get("pin") or (c and time.time() < c[1]) or mind.pending
                or time.time() < pending["city_until"] or time.time() < pending["song_until"]
                or time.time() < pending.get("light_cal", 0.0))


def router_call(text):
    """Ask the local brain to pick ONE intent from the fixed menu. Returns a canonical command or None."""
    try:
        r = ollama.chat(model=OLLAMA_MODEL, format="json", keep_alive="30m",
                        messages=[{"role": "system", "content": core.Core.ROUTER_PROMPT},
                                  {"role": "user", "content": text}],
                        options={**brain_options(60), "temperature": 0})
        return CORE.parse_router(r["message"]["content"])
    except Exception as e:
        print(f"[router] {e}")
        return None


def route(text, origin="user", use_router=True):
    """text -> reply string, tools.SILENT, or None (= chat with the brain).  Everything goes through the gate."""
    verdict, payload = CORE.gate(text, origin)
    if verdict == "reply":
        return payload
    gated = verdict == "go!"                      # the user just confirmed this sentence
    text = payload
    if not gated:
        fu = CORE.followup(text)
        if fu:
            kind, val = fu
            if kind == "reply":
                return val
            text = val
    reply = local_skill(text)
    if reply is None and use_router and CORE.router_on and not OFFLINE_ROUTER_BLOCK() and CORE.looks_like_command(text):
        canon = router_call(text)
        if canon:
            print(f"[router] {text!r} -> {canon!r}")
            v2, p2 = CORE.gate(canon, "router")
            reply = p2 if v2 == "reply" else local_skill(p2)
            if reply:
                CORE.audit("router", f"{text} -> {canon}")
    if reply and reply != tools.SILENT:
        CORE.remember(text, reply)
    return reply


def OFFLINE_ROUTER_BLOCK():
    return False


def handle(text: str, original=None, origin="user"):
    hub.set_state("thinking")
    secret = is_secret_text(text)              # a PIN must not stay in the chat history
    reply = route(text, origin)
    if reply == tools.SILENT:
        return
    if reply:
        reply = localize(reply)
        history.append({"role": "user", "content": "(hidden)" if secret else text})
        history.append({"role": "assistant", "content": reply})
        say(reply)
    elif CORE.frozen:
        return
    else:
        think_and_speak(original or text)
    wait_until_done_speaking()


# ----------------------------- conversation logic -----------------------
conv = {"awake": False, "last": 0.0}
ORIGIN_NOW = ["user"]


WAKE_ALIASES = {"nemesis", "nemisis", "nemeses", "nemesys", "nemessis", "nemisys", "nemsis", "nemasis", "namesis", "nimesis",
                "nemesez", "nemeses", "nemises", "nemesisss", "nemisus", "nemesiss"}
_WAKE_LEAD = {"hey", "hi", "hello", "ok", "okay", "yo", "a", "oh", "hey,", "hi,", "ok,"}


def _wake_like(tok):
    """True when one spoken word sounds like 'Nemesis' (speech recognition often spells it differently)."""
    tok = re.sub(r"[^a-z]", "", tok.lower())
    if tok in WAKE_ALIASES:
        return True
    return 6 <= len(tok) <= 9 and tok[0] == "n" and difflib.SequenceMatcher(None, tok, "nemesis").ratio() >= 0.8


def strip_wake_word(text: str):
    low = text.lower()
    for w in WAKE_WORDS:
        i = low.find(w)
        if i != -1:
            rest = text[i + len(w):].lstrip(" ,.!?-")
            return True, rest
    toks = text.split()                       # a near-miss spelling, only at the start: "hey nemisis open notepad"
    for i, tok in enumerate(toks[:3]):
        if i and toks[i - 1].lower() not in _WAKE_LEAD:
            break
        if _wake_like(tok):
            return True, " ".join(toks[i + 1:]).lstrip(" ,.!?-")
    return False, text


def process(text: str, direct=False, window=None, lang=None) -> bool:
    """Handle one recognised sentence. Returns False when Nemesis should shut down.

    direct=True  : tap-to-talk / typed - no wake word needed.
    window       : seconds Nemesis stays awake after the last reply (None = the
                   PC-mic loop manages that itself).
    """
    global LANG
    text = text.strip()
    if not text:
        return True
    LANG = resolve_lang(text, lang)
    quick = strip_wake_word(text)[1].lower().strip(" .!?,")
    if core.FREEZE_RE.match(quick) or (CORE.frozen and core.UNFREEZE_RE.match(quick)):
        conv["awake"], conv["last"] = True, time.time()      # an emergency word needs no wake word
        handle(quick, origin=ORIGIN_NOW[0])
        return True
    now = time.time()
    TURN.update(t0=now, brain=0.0, said=0.0, voice=0.0, done=False)
    awake = conv["awake"] and (window is None or now - conv["last"] < window or tools.dictating())
    heard, rest = strip_wake_word(text)
    if not (awake or direct or heard):
        print("(no wake word - say 'Nemesis' first)")
        return True
    if heard:
        text = rest
    conv["awake"], conv["last"] = True, now

    if not text:
        say("Yes?")
        wait_until_done_speaking()
        return True
    if CORE.frozen or core.FREEZE_RE.match(text.lower().strip(" .!?,")):
        handle(text, origin=ORIGIN_NOW[0])  # frozen: only "unfreeze" gets through
        conv["last"] = time.time()
        return True
    if tools.dictating():                  # everything you say is typed until "stop dictation"
        conv["awake"], conv["last"] = True, time.time()
        r = tools.dictation_step(text)
        if r and r != tools.SILENT:
            say(localize(r))
            wait_until_done_speaking()
        return True
    original = text
    cmd = indic_command(text) if (DEVANAGARI.search(text) or LANG != "en" or re.search(r"\b(?:kholo|chalao|bajao|badha|mausam|khabar|samay|ruko|chup)\b", text.lower())) else None
    if cmd:
        text = cmd
    low = text.lower().strip(" .!?")
    if STOP_RE.match(low):
        stop_speaking()
        return True
    if low in ("shut down", "shutdown", "exit", "quit", "shut down nemesis"):
        say("Shutting down. Goodbye.")
        wait_until_done_speaking()
        return False
    if low in ("go to sleep", "goodbye", "bye", "that's all", "thanks that's all", "standby", "stand by",
               "sleep", "sleep mode", "standby mode", "go quiet", "be quiet now"):
        say("Standing by.")
        wait_until_done_speaking()
        conv["awake"] = False
        return True
    handle(text, None if cmd else original, origin=ORIGIN_NOW[0])
    conv["last"] = time.time()
    return True


def local_loop():
    """PC microphone loop (--pc-mic)."""
    global REPLY_TARGET
    REPLY_TARGET = "pc"
    while True:
        try:
            hub.set_state("listening" if conv["awake"] else "sleeping")
            print("\n(listening...)" if conv["awake"] else "\n(sleeping - say 'Nemesis')")
            audio = record_utterance(start_timeout=CONVERSATION_TIMEOUT if conv["awake"] else None)
            if audio is None:
                conv["awake"] = False
                continue
            if len(audio) < 0.3 * SAMPLE_RATE:  # a click or bump, not speech
                continue
            hub.set_state("thinking")
            text = transcribe(audio)
            if not text:
                print("(heard a sound but no clear words)")
                continue
            if CORE.is_echo(text):
                print(f"(ignored - that is what I just said: {text[:50]!r})")
                continue
            ORIGIN_NOW[0] = "pc-mic"
            shown = "(hidden)" if is_secret_text(text) else text
            print(f"You: {shown}")
            hub.log("you", shown)
            audit("pc-mic", text)
            if not process(text, direct=False, window=None):
                break
        except KeyboardInterrupt:
            raise
        except Exception as e:  # keep running whatever happens
            print(f"[error] {e}")
            hub.log("sys", f"Error: {e}")
            time.sleep(1)


def phone_loop():
    """Phone microphone loop (default): sentences arrive from the phone page."""
    global REPLY_TARGET
    while True:
        try:
            try:
                item = hub.inbox.get(timeout=0.5)
            except queue.Empty:
                if hub.state in ("listening", "hearing") and time.time() - conv["last"] > PHONE_AWAKE_SECONDS:
                    hub.set_state("sleeping")
                continue
            text = item["text"]
            if not item["direct"] and CORE.is_echo(text):
                print(f"(ignored - that is what I just said: {text[:50]!r})")
                audit("echo", "ignored: heard my own voice")
                continue
            ORIGIN_NOW[0] = "typed" if item["direct"] else "phone"
            shown = "(hidden)" if is_secret_text(text) else text
            print(f"You (phone): {shown}")
            hub.log("you", shown)
            audit("phone", text)
            REPLY_TARGET = "phone"
            if not process(text, direct=item["direct"], window=PHONE_AWAKE_SECONDS, lang=item.get("lang")):
                break
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"[error] {e}")
            log_error("phone_loop", traceback.format_exc())
            hub.log("sys", f"Error: {e}")
            hub.set_state("listening")
            say("Sorry, that did not work. The details are in the error log.")
            time.sleep(1)


# ----------------------------- main -------------------------------------
def main():
    print(f"{NAME} v{VERSION}")
    if "--selftest" in sys.argv:                   # python nemesis.py --selftest : tests the command routing, touches nothing
        import selftest
        ok, bad, _ = selftest.run(sys.modules[__name__])
        print("ALL GOOD." if not bad else f"{bad} problem(s) - send me the FAIL lines above.")
        sys.exit(1 if bad else 0)
    key = get_access_key()
    setup_audio()
    if "--seal" in sys.argv:                       # python nemesis.py --seal : remember the current files as the trusted ones
        print(f"Sealed {guard.seal()} files. Nemesis will warn you if any of them change.")
        return
    if "--get-offline" in sys.argv:                # python nemesis.py --get-offline : download the hearing models (needs internet)
        offline.download_models(lambda who, text: print(f"[{who}] {text}"))
        return
    if "--check" in sys.argv:                      # python nemesis.py --check : health check, then exit
        global SECURE_LINK
        SECURE_LINK = tls_available() and "--http" not in sys.argv
        print(run_diagnostics())
        return
    threading.Thread(target=_tts_worker, daemon=True).start()
    threading.Thread(target=_deliver_worker, daemon=True).start()
    threading.Thread(target=preload_voices, daemon=True).start()
    trim_voice_cache()
    threading.Thread(target=_play_worker, daemon=True).start()
    hub.stop_callback = stop_speaking
    global eyes
    eyes = Vision(hub, say)
    tools.init(say=say, hub=hub, generate=generate_text, base_dir=BASE_DIR, lang=lambda: LANG,
               skill=local_skill, last_reply=lambda: next((m["content"] for m in reversed(history)
                                                          if m["role"] == "assistant"), ""))
    threading.Thread(target=frame_loop, args=(eyes, hub), daemon=True).start()
    if "--cam" in sys.argv:
        threading.Thread(target=webcam_loop, args=(eyes,), daemon=True).start()
    threading.Thread(target=reminder_loop, daemon=True).start()
    threading.Thread(target=stats_loop, daemon=True).start()

    SECURE_LINK = tls_available() and "--http" not in sys.argv
    if not SECURE_LINK:
        print("[security] encrypted link needs:  pip install cryptography   (using plain HTTP for now)")
    tidy_old_files()
    hub.privacy = PRIVACY
    choose_brain()
    print(f"Brain: {OLLAMA_MODEL}")
    url = phone_url(HUD_PORT + 1 if SECURE_LINK else HUD_PORT, key, SECURE_LINK)
    apply_voice(save=False)
    hub.pin_required = pin_is_set
    hub.pin_check = check_pin
    hub.on_new_device = on_new_device
    hub.stt_callback = stt_from_phone
    hub.set_link(url)
    hub.set_info(model=OLLAMA_MODEL,
                 stt="whisper " + WHISPER_MODEL if LOCAL_MIC else "phone browser",
                 mic=MIC_NAME, speaker=SPEAKER_NAME, phone="waiting")
    hub.set_info(privacy="ON" if PRIVACY else "OFF", camera="OFF", link="encrypted" if SECURE_LINK else "plain",
                 sessions="0", pin="SET" if pin_is_set() else "NOT SET", speed="-", next="NONE")
    hub.set_info(net="OFFLINE" if OFFLINE else "ONLINE", offmic="0")
    lock_down_files()
    if start_hud(HUD_PORT, True, HUD_OPEN_BROWSER, key, tls=SECURE_LINK) is None:
        print(f"\n[!] Port {HUD_PORT} is already in use - Nemesis is probably already running in another window.")
        print("    Close that window (or restart the PC) and try again.")
        sys.exit(3)
    hub.log("sys", "Nemesis starting")
    hub.set_info(light="NOT CONNECTED")
    LIGHT.start()
    startup_integrity()
    threading.Thread(target=background_pc_check, daemon=True).start()
    if OFFLINE:
        threading.Thread(target=lambda: set_offline(True), daemon=True).start()
    threading.Thread(target=warm_up_brain, daemon=True).start()

    use_local = LOCAL_MIC
    if use_local:
        load_speech_model()
        t = threading.Thread(target=calibrate, daemon=True)
        t.start()
        t.join(12)
        if t.is_alive():
            print("\n[!] The PC microphone is not delivering any audio - switching to phone mode.")
            hub.log("sys", "PC microphone blocked - using the phone instead")
            use_local = False

    if PC_AUDIO:
        print("\nSpeaker test: you should hear a short beep on the PC now...")
        beep()
    say(f"{NAME} online. All systems nominal." + ("" if not use_local else " Say my name when you need me."),
        target="pc")
    wait_until_done_speaking()

    if use_local:
        local_loop()
    else:
        print("\n==================== PHONE LINK ====================")
        print("1. Put your phone on the SAME Wi-Fi (or USB tethering) as this PC.")
        print("2. Open Chrome on the phone and go to this address")
        print("   (or scan the QR code on the HUD page):")
        print(f"\n   {url}\n")
        if SECURE_LINK:
            print("   Chrome will say 'Your connection is not private' (the certificate is made by")
            print("   Nemesis itself). Tap Advanced > Proceed. This is normal and only needed once.")
        print("3. Tap the big mic button and speak.")
        if not SECURE_LINK:
            print("   (Plain http link: Chrome shows a one-time setting for the microphone.)")
        print("====================================================")
        print("If Windows asks about the firewall, click 'Allow access' (Private networks).")
        print("Waiting for the phone...  (Ctrl+C to stop)")
        phone_loop()
    hub.set_state("sleeping")


def log_error(where, text):
    try:
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(f"--- {datetime.datetime.now():%Y-%m-%d %H:%M:%S} {where}\n{text}\n")
    except OSError:
        pass


def _thread_crash(args):
    text = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
    print(f"[thread error] {args.exc_value}")
    log_error(f"thread {getattr(args.thread, 'name', '?')}", text)


threading.excepthook = _thread_crash


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nBye.")
    except Exception:
        crash = traceback.format_exc()
        print("\n[Nemesis crashed]\n" + crash)
        log_error("main", crash)
        sys.exit(1)          # a non-zero exit tells Start Nemesis.bat to restart
