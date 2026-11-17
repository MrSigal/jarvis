# -*- coding: utf-8 -*-
"""
J.A.R.V.I.S  -  Personal desktop AI assistant
==============================================
* Say "Hey Jarvis" (or click the core / press the mic button) and speak.
* Futuristic HUD window instead of a console.
* Brain: Claude (Anthropic API key).  Voice: free Microsoft neural voice.
* Controls the computer ONLY when you ask: open/close apps, files, sites, search,
  media, volume, keys, typing, screenshots, power, PowerShell (with confirmation).

Run:  pythonw jarvis.py        (no console)
      python  jarvis.py        (with console, for troubleshooting)
"""

import os
import re
import sys
import json
import time
import wave
import queue
import shutil
import asyncio
import logging
import platform
import tempfile
import threading
import subprocess
import webbrowser
import urllib.parse
from io import BytesIO
from datetime import datetime
from pathlib import Path

VERSION = "9.1"
UPDATE_REPO = "MrSigal/jarvis"          # updates are downloaded from this GitHub repo
UPDATE_BRANCH = "main"
FROZEN = getattr(sys, "frozen", False)          # running as the installed JARVIS.exe
HERE = Path(sys._MEIPASS) if FROZEN else Path(__file__).resolve().parent      # bundled files (hud.html)
if FROZEN:
    DATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / "JARVIS"         # settings, log, reminders
else:
    DATA_DIR = HERE
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = DATA_DIR / "config.json"
LOG_FILE = DATA_DIR / "jarvis.log"
REMINDERS_FILE = DATA_DIR / "reminders.json"
MEMORY_FILE = DATA_DIR / "memory.json"
MAIL_STATE_FILE = DATA_DIR / "mail_state.json"
OPEN_SIGNAL = DATA_DIR / "open.signal"       # a second launch (desktop icon) asks the running JARVIS to open up
REMINDERS = None
BACKGROUND_START = "--background" in sys.argv
OS = platform.system()
SAMPLE_RATE = 16000
CHUNK = 1280  # 80 ms, what openWakeWord expects

logging.basicConfig(
    filename=str(LOG_FILE), level=logging.INFO, encoding="utf-8",
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("jarvis")
if sys.stdout:  # running with a console
    log.addHandler(logging.StreamHandler(sys.stdout))

# ======================================================================= CONFIG
DEFAULT_CONFIG = {
    "anthropic_api_key": "",
    "brain_engine": "claude",               # claude (paid, smartest) | groq (free)
    "groq_api_key": "",                     # free key from console.groq.com/keys
    "groq_models": ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.3-70b-versatile", "qwen/qwen3-32b"],
    "stt_engine": "google",                 # google | groq (Whisper)
    "claude_model": "claude-haiku-5-5",   # cheapest Claude model
    "voice": "he-IL-AvriNeural",            # free Hebrew male voice (female: he-IL-HilaNeural)
    "phone_secret": "",                     # pairing code for the iPhone app (created automatically)
    "phone_link": True,
    "mail_address": "",                     # Gmail address (IMAP)
    "mail_app_password": "",                # Google app password (myaccount.google.com/apppasswords)
    "mail_imap_host": "",                   # empty = detected from the address
    "mail_alerts": True,                    # important new emails -> Windows notification + phone app
    "mail_check_minutes": 3,
    "calendar_ics": "",                     # Google Calendar "secret address in iCal format" (read-only)
    "calendar_alerts": True,
    "calendar_alert_minutes": 15,
    "asc_issuer": "",                       # App Store Connect API (for "יומן הגנן" stats) - key stays on this PC
    "asc_key_id": "",
    "asc_key": "",                          # contents of the .p8 file (encrypted with Windows DPAPI on disk)
    "asc_vendor": "",
    "asc_app_name": "יומן הגנן",
    "asc_alerts": True,
    "away_minutes": 5,
    "run_in_background": True,              # closing the window keeps JARVIS listening (icon near the clock)
    "start_with_windows": True,             # start hidden when the PC starts
    "auto_hide": True,                      # window pops up on "Hey Jarvis" and hides again afterwards
    "voice_lock": False,                    # answer only the owner's voice (after "תלמדי את הקול שלי")
    "voice_lock_threshold": 0.32,           # higher = stricter
    "niqqud_voice": True,                   # add vowel marks (ניקוד) only to what is spoken, for correct pronunciation
    "tts_engine": "edge",                   # edge (fast, built-in) | gemini (Google) | eleven (ElevenLabs, paid credits)
    "el_api_key": "",
    "el_voice_id": "EXAVITQu4vr4xnSDxMaL",   # Sarah (female); the user can pick any voice from their account
    "el_voice_name": "Sarah",
    "el_female": True,
    "el_model": "eleven_v3",
    "el_mood": "neutral",                     # happy | excited | calm | neutral (ElevenLabs v3 tone)                # 1 credit per character, speaks Hebrew
    "gemini_api_key": "",                   # free key from aistudio.google.com/apikey
    "gemini_voice": "Charon",
    "gemini_model": "",                     # remembered automatically once one works
    "intro_music": True,                    # music when JARVIS opens (not when it starts hidden with Windows)
    "intro_mode": "builtin",                # builtin (original JARVIS power-up) | file (the user's own song)
    "intro_file": "",
    "intro_seconds": 12,                    # play at most this long from the user's song
    "user_name": "אדוני",
    "self_gender": "female",                # how JARVIS talks about itself: female | male | auto (by voice)
    "user_gender": "male",                  # how JARVIS addresses the user
    "wake_threshold": 0.5,
    "follow_up_seconds": 25,                # after "Hey Jarvis", keep talking without saying it again
    "pause_seconds": 2.0,                   # how long a pause means "I finished talking" (bigger = never cuts you off)
    "min_confidence": 0.25,                  # speech below this confidence is ignored (non-English, noise)
}


SECRET_KEYS = ("anthropic_api_key", "groq_api_key", "gemini_api_key", "mail_app_password", "phone_secret",
               "calendar_ics", "asc_key", "el_api_key")


def _dpapi(data, protect):
    """Windows DPAPI: data can only be decrypted by the same Windows user on the same PC."""
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    buf = ctypes.create_string_buffer(data, len(data))
    inp = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    crypt32 = ctypes.windll.crypt32
    if protect:
        ok = crypt32.CryptProtectData(ctypes.byref(inp), ctypes.c_wchar_p("JARVIS"), None, None, None, 0x1,
                                      ctypes.byref(out))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(inp), None, None, None, None, 0x1, ctypes.byref(out))
    if not ok:
        raise OSError("DPAPI failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def protect_secret(value):
    if OS != "Windows" or not value or str(value).startswith("dpapi:"):
        return value
    import base64
    try:
        return "dpapi:" + base64.b64encode(_dpapi(str(value).encode("utf-8"), True)).decode()
    except Exception as e:
        log.warning(f"could not protect a secret: {e}")
        return value


def unprotect_secret(value):
    if not isinstance(value, str) or not value.startswith("dpapi:"):
        return value
    import base64
    try:
        return _dpapi(base64.b64decode(value[6:]), False).decode("utf-8")
    except Exception as e:
        log.error(f"could not decrypt a saved key (different Windows user?): {e}")
        return ""


FEMALE_GEMINI = {"Kore", "Aoede", "Leda", "Zephyr", "Autonoe", "Callirrhoe", "Despina", "Erinome", "Laomedeia",
                 "Achernar", "Gacrux", "Pulcherrima", "Sulafat", "Vindemiatrix"}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
            for k in SECRET_KEYS:
                cfg[k] = unprotect_secret(cfg.get(k, ""))
        except Exception as e:
            log.error(f"config.json unreadable: {e}")
    for old in ("elevenlabs_api_key", "elevenlabs_voice_id", "elevenlabs_voice_name",
                "elevenlabs_model", "speech_recognition"):
        cfg.pop(old, None)                                  # ElevenLabs was removed
    if "fallback_voice" in cfg:
        cfg["voice"] = cfg.pop("fallback_voice")
    for old in ("whatsapp_provider", "greenapi_url", "greenapi_id", "greenapi_token", "whatsapp_phone",
                "whatsapp_apikey", "whatsapp_when_away"):
        cfg.pop(old, None)                                  # v4.7: WhatsApp disconnected, credentials deleted
    for old in ("vs_url", "vs_model", "vs_voice"):
        cfg.pop(old, None)                                  # VoiceStudio was removed
    if cfg.get("tts_engine") not in ("edge", "gemini", "eleven"):
        cfg["tts_engine"] = "edge"
    if not str(cfg.get("voice", "")).startswith("he-IL"):     # v3: Hebrew
        cfg["voice"] = "he-IL-AvriNeural"
    if cfg.get("user_name", "").lower() in ("", "sir"):
        cfg["user_name"] = "אדוני"
    if cfg.get("min_confidence", 0.25) > 0.25:
        cfg["min_confidence"] = 0.25                       # Hebrew gets lower confidence scores
    if not cfg.get("phone_secret"):
        import secrets
        cfg["phone_secret"] = secrets.token_urlsafe(18)
    if not cfg.get("_female_voice_v58"):                    # v5.8: the user asked for a woman's voice
        cfg["_female_voice_v58"] = True
        cfg["voice"] = "he-IL-HilaNeural"
        if cfg.get("gemini_voice") not in FEMALE_GEMINI:
            cfg["gemini_voice"] = "Kore"
    if not cfg.get("_pause_v70") and float(cfg.get("pause_seconds", 2.0)) < 2.0:   # v7.0: don't cut the user off
        cfg["pause_seconds"] = 2.0
    cfg["_pause_v70"] = True
    if cfg.get("groq_models") == ["openai/gpt-oss-120b", "llama-3.3-70b-versatile", "qwen/qwen3-32b"]:
        cfg["groq_models"] = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.3-70b-versatile", "qwen/qwen3-32b"]
    if not cfg.get("_brain_v91"):                           # v9.1: the user asked for Claude as the brain
        cfg["_brain_v91"] = True
        if cfg.get("anthropic_api_key"):
            cfg["brain_engine"] = "claude"
    if not cfg.get("_voice_v71"):                           # v7.1: one steady, natural Hebrew voice
        cfg["_voice_v71"] = True
        cfg["el_mood"] = "neutral"                          # mood tags made v3 change the voice from answer to answer
    if cfg.get("el_model") != "eleven_v3":                  # only v3 speaks Hebrew; other models sound foreign
        cfg["el_model"] = "eleven_v3"
    if cfg.get("follow_up_seconds", 25) < 15:              # v4.3: conversation mode
        cfg["follow_up_seconds"] = 25
    if cfg.get("claude_model") == "claude-sonnet-5-5":   # move old installs to the cheap model
        cfg["claude_model"] = "claude-haiku-5-5"
    return cfg


def save_config():
    data = dict(CFG)
    for k in SECRET_KEYS:
        data[k] = protect_secret(data.get(k, ""))
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_FILE)


CFG = load_config()
save_config()

# ======================================================================= HUD BRIDGE
class UI:
    """Sends updates to the HUD (hud.html). Buffers until the page is ready."""

    def __init__(self):
        self.window = None
        self.ready = threading.Event()
        self._pending = []
        self._lock = threading.Lock()

    def js(self, fn, *args):
        code = f"window.hud && hud.{fn}({', '.join(json.dumps(a, ensure_ascii=False) for a in args)})"
        with self._lock:
            if not self.ready.is_set():
                self._pending.append(code)
                return
        try:
            self.window.evaluate_js(code)
        except Exception:
            pass

    def flush(self):
        with self._lock:
            pending, self._pending = self._pending, []
            self.ready.set()
        for code in pending:
            try:
                self.window.evaluate_js(code)
            except Exception:
                pass

    def show_window(self):
        """Bring the window up (used when JARVIS is woken while hidden)."""
        w = self.window
        if not w:
            return False
        was_hidden = not getattr(self, "visible", True)
        try:
            w.show()
            w.restore()
            self.visible = True
            if OS == "Windows":
                import ctypes
                hwnd = ctypes.windll.user32.FindWindowW(None, "G.I.L.I")
                if hwnd:
                    ctypes.windll.user32.ShowWindow(hwnd, 9)
                    ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception as e:
            log.error(f"show window: {e}")
        return was_hidden

    def hide_window(self):
        try:
            self.window.hide()
            self.visible = False
        except Exception as e:
            log.error(f"hide window: {e}")

    def state(self, s):            self.js("setState", s)
    def say(self, role, text):     self.js("addMessage", role, text)
    def action(self, text):        self.js("addAction", text)
    def toast(self, text, kind="info"): self.js("toast", text, kind)
    def level(self, v):            self.js("level", v)
    def status(self, d):           self.js("setStatus", d)


ui = UI()

# ======================================================================= STARTUP MUSIC
def make_intro_wav(sr=44100):
    """An original cinematic 'power-up': low boom, rising synth swell, shimmer, then a final chord that keeps
    ringing (about 8 more seconds) so Jarvis can greet over it; the caller fades it out when he's done."""
    import numpy as np
    T = 14.0
    t = np.arange(int(sr * T)) / sr
    out = np.zeros_like(t)
    def env(a, d, start, length):
        e = np.zeros_like(t)
        i0, i1 = int(start * sr), min(len(t), int((start + length) * sr))
        x = t[i0:i1] - start
        e[i0:i1] = np.minimum(1, x / max(a, 1e-3)) * np.exp(-np.maximum(0, x - a) / d)
        return e
    def saw(f, ph=0):
        return 2 * ((f * t + ph) % 1) - 1
    # 1. deep boom + sub hits
    for st, f0 in ((0.0, 55), (2.9, 49), (4.1, 41)):
        f = f0 * (1 + 1.5 * np.exp(-(t - st).clip(0) * 18))
        out += 0.9 * np.sin(2 * np.pi * np.cumsum(f) / sr) * env(0.005, 0.9, st, 2.5)
    # 2. rising detuned swell (D minor -> F)
    sweep = np.clip(t / 4.0, 0, 1) ** 2
    for base in (146.8, 174.6, 220.0, 293.7):
        for det in (-0.004, 0.0, 0.005):
            f = base * (1 + det) * (1 + 0.12 * sweep)
            ph = np.cumsum(f) / sr
            out += 0.045 * (2 * (ph % 1) - 1) * (sweep * (t < 4.1) + env(0.01, 1.4, 4.1, 2.1))
    # 3. shimmer arpeggio
    notes = [587.3, 698.5, 880.0, 1174.7, 880.0, 1396.9, 1174.7, 1760.0]
    for i, n in enumerate(notes):
        st = 1.6 + i * 0.3
        out += 0.10 * np.sin(2 * np.pi * n * t) * env(0.004, 0.35, st, 1.2) * (0.6 + 0.4 * np.sin(2 * np.pi * 6 * t))
    # 4. final 'online' chord with bright ping
    for n in (293.7, 349.2, 440.0, 587.3, 880.0):
        out += 0.16 * (np.sin(2 * np.pi * n * t) + 0.3 * saw(n)) * env(0.02, 1.1, 4.1, 2.1)
    # 5. warm pad that keeps the chord alive under the greeting
    pad_env = np.clip((t - 4.3) / 1.2, 0, 1) * (0.8 + 0.2 * np.sin(2 * np.pi * 0.25 * t))
    for n in (146.8, 220.0, 293.7, 349.2, 440.0):
        for det in (-0.003, 0.003):
            out += 0.05 * np.sin(2 * np.pi * n * (1 + det) * t + np.sin(2 * np.pi * 0.3 * t)) * pad_env
    out += 0.18 * np.sin(2 * np.pi * 1760 * t) * env(0.002, 0.5, 4.1, 1.5)
    # light echo, fade, normalise
    d = int(0.23 * sr)
    out[d:] += 0.35 * out[:-d]
    out *= np.minimum(1, (T - t) / 2.5)
    out /= max(1e-6, np.abs(out).max()) / 0.85
    pcm = (np.tanh(out * 1.2) / np.tanh(1.2) * 32000).astype(np.int16)
    stereo = np.repeat(pcm[:, None], 2, axis=1)
    buf = BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(sr); w.writeframes(stereo.tobytes())
    return buf.getvalue()


def intro_info():
    """What startup music the iPhone app should use (it downloads the user's song once and keeps it)."""
    import hashlib
    path = (CFG.get("intro_file") or "").strip()
    on = bool(CFG.get("intro_music", True))
    if on and CFG.get("intro_mode", "builtin") == "file" and path and os.path.isfile(path):
        st = os.stat(path)
        h = hashlib.sha256(f"v2|{path}|{st.st_size}|{st.st_mtime}".encode()).hexdigest()[:16]
        return {"mode": "file", "hash": h, "seconds": float(CFG.get("intro_seconds", 12) or 12), "size": st.st_size}
    return {"mode": "builtin" if on else "off", "hash": "", "seconds": 0}


def intro_clip_bytes(path, seconds):
    """Just the beginning of the song for the phone (an mp3 can simply be cut; players skip the broken last frame)."""
    data = Path(path).read_bytes()
    if Path(path).suffix.lower() != ".mp3" or len(data) <= 8_000_000:
        if len(data) > MAX_PHONE_FILE:
            raise ValueError("השיר גדול מדי")
        return data
    length = 0
    try:
        import pygame
        if not pygame.mixer.get_init():
            pygame.mixer.init()
        length = pygame.mixer.Sound(path).get_length()
    except Exception:
        pass
    bps = len(data) / length if length > 5 else 40_000          # bytes per second (40KB/s = 320kbps worst case)
    return data[:int(bps * seconds) + 64_000]


INTRO_ERROR = [""]


def play_intro(pygame):
    """Plays the startup music. Returns (sound, seconds until the greeting should start)."""
    INTRO_ERROR[0] = ""
    if not pygame.mixer.get_init():
        pygame.mixer.init()
    path = (CFG.get("intro_file") or "").strip()
    secs = float(CFG.get("intro_seconds", 12) or 12)
    try:
        if CFG.get("intro_mode", "builtin") == "file" and path and os.path.isfile(path):
            try:
                snd = pygame.mixer.Sound(path)
                snd.play(fade_ms=300, maxtime=int(secs * 1000) + 15000)
                return snd, min(5.0, snd.get_length() * 0.6, secs)
            except Exception as e:                 # the song can't be played -> use the built-in sound
                log.warning(f"intro song {path}: {e}")
        wav = make_intro_wav()
        try:
            snd = pygame.mixer.Sound(file=BytesIO(wav))
        except Exception as e:                     # some setups can't make a Sound from memory -> use a temp file
            log.warning(f"intro Sound from memory: {e}")
            path = os.path.join(tempfile.gettempdir(), "gili_intro.wav")
            Path(path).write_bytes(wav)
            snd = pygame.mixer.Sound(path)
        ch = snd.play()
        if ch is None:                             # all channels busy -> force one
            ch = pygame.mixer.find_channel(True)
            ch.play(snd)
        log.info(f"intro playing ({snd.get_length():.1f}s, mixer {pygame.mixer.get_init()})")
        return snd, 4.4                          # right after the final chord hits
    except Exception as e:
        log.exception("intro music")
        INTRO_ERROR[0] = f"{type(e).__name__}: {e}"[:200]
        ui.toast(f"מוזיקת הפתיחה לא התנגנה: {INTRO_ERROR[0]}", "warn")
        return None, 0


# ======================================================================= VOICE OUT
class QuotaOut(Exception):
    pass


class Speaker:
    def __init__(self):
        import pygame
        self.pygame = pygame
        try:
            pygame.mixer.pre_init(44100, -16, 2, 2048)  # bigger buffer: no clicks or clipped word endings
        except Exception:
            pass
        pygame.mixer.init()
        self.lock = threading.Lock()
        self._stop = False

    # ---- free Microsoft neural voice
    def _edge(self, text, female=None):
        import edge_tts
        voice = CFG["voice"] if female is None else ("he-IL-HilaNeural" if female else "he-IL-AvriNeural")

        async def run():
            buf = BytesIO()
            async for chunk in edge_tts.Communicate(text, voice, rate="+4%").stream():
                if chunk["type"] == "audio":
                    buf.write(chunk["data"])
            return buf.getvalue()
        return asyncio.run(run())

    # ---- Google Gemini TTS (free API key from Google AI Studio)
    GEMINI_MODELS = ["gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts",
                     "gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts"]

    @staticmethod
    def _gemini_voice():
        v = CFG.get("gemini_voice") or "Charon"
        if CFG.get("tts_engine") == "eleven":          # standing in for ElevenLabs: keep the same gender
            if CFG.get("el_female", True) and v not in FEMALE_GEMINI:
                v = "Kore"
            elif not CFG.get("el_female", True) and v in FEMALE_GEMINI:
                v = "Charon"
        return v

    def _gemini(self, text):
        import httpx, base64, re
        key = CFG["gemini_api_key"].strip()
        prompt = text          # only the words to say - any instruction here gets read aloud
        body = {"contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseModalities": ["AUDIO"],
                                     "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {
                                         "voiceName": self._gemini_voice()}}}}}
        models = ([CFG["gemini_model"]] if CFG.get("gemini_model") else []) + \
                 [m for m in self.GEMINI_MODELS if m != CFG.get("gemini_model")]
        last = ""
        for model in models:
            r = httpx.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                           params={"key": key}, json=body, timeout=45)
            if r.status_code in (401, 403) or "API_KEY_INVALID" in r.text or "API key not valid" in r.text:
                raise RuntimeError("המפתח של Gemini לא תקין. תעתיק אותו שוב מ־aistudio.google.com/apikey")
            if r.status_code != 200:
                try:
                    msg = r.json().get("error", {}).get("message", r.text)
                except Exception:
                    msg = r.text
                last += f"\n{model}: {r.status_code} {msg[:160]}"
                log.warning(f"Gemini TTS {model}: {r.status_code} {msg[:300]}")
                continue
            try:
                part = next(p["inlineData"] for p in r.json()["candidates"][0]["content"]["parts"] if "inlineData" in p)
            except Exception:
                last += f"\n{model}: no audio in answer"
                continue
            audio = base64.b64decode(part["data"])
            mime = part.get("mimeType", "")
            if CFG.get("gemini_model") != model:
                CFG["gemini_model"] = model
                save_config()
            if "wav" in mime or audio[:4] == b"RIFF":
                return audio
            rate = int((re.search(r"rate=(\d+)", mime) or [0, 24000])[1])
            buf = BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(audio)
            return buf.getvalue()
        if last.count(": 429 ") == len(models):
            raise RuntimeError("quota")
        raise RuntimeError("Gemini לא החזיר קול:" + last)

    # ---- ElevenLabs: uses only the credits already in the account; when they run out -> free voice
    _el_left, _el_checked, _el_warned = None, 0.0, False

    def eleven_credits(self, force=False):
        """Characters left this month (None if unknown)."""
        import httpx
        if force or self._el_left is None or time.time() - self._el_checked > 600:
            r = httpx.get("https://api.elevenlabs.io/v1/user/subscription", timeout=15,
                          headers={"xi-api-key": CFG["el_api_key"].strip()})
            if r.status_code == 401:
                raise RuntimeError("מפתח ה־ElevenLabs לא תקין")
            r.raise_for_status()
            j = r.json()
            Speaker._el_left = max(0, int(j.get("character_limit", 0)) - int(j.get("character_count", 0)))
            Speaker._el_checked = time.time()
        return self._el_left

    EL_MOODS = {"happy": "[happy]", "excited": "[excited]", "calm": "[calm]", "neutral": ""}

    EL_SEED = 4242
    _el_lang_ok = True                                  # False if the account/model rejects language_code

    @staticmethod
    def _clean_for_voice(text):
        """Whole sentences only: v3 tends to swallow the end of a text that doesn't end with punctuation."""
        import re
        t = re.sub(r"\s+", " ", text).strip()
        if t and t[-1] not in ".!?…":
            t += "."
        return t

    @classmethod
    def _eleven_body(cls, text, seed=None):
        model = CFG.get("el_model") or "eleven_v3"
        body = {"text": cls._clean_for_voice(text), "model_id": model}
        if model == "eleven_v3":
            tag = cls.EL_MOODS.get(CFG.get("el_mood", "neutral"), "")
            if tag:
                body["text"] = f"{tag} {body['text']}"   # v3 audio tag: sets the tone, isn't read aloud
            # "Natural" stability (closest to the real recorded voice) + Hebrew forced + a fixed seed:
            # same woman every time, no foreign accent, no drifting between answers
            body["voice_settings"] = {"stability": 0.5, "similarity_boost": 0.9, "use_speaker_boost": True}
            body["seed"] = seed if seed is not None else cls.EL_SEED
            if cls._el_lang_ok:
                body["language_code"] = "he"
        else:
            body["voice_settings"] = {"stability": 0.35, "similarity_boost": 0.8, "style": 0.45, "use_speaker_boost": True}
        return body

    def _eleven_once(self, text, small, seed=None):
        import httpx
        url = (f"https://api.elevenlabs.io/v1/text-to-speech/{CFG['el_voice_id']}"
               f"?output_format={'mp3_44100_64' if small else 'mp3_44100_128'}")
        headers = {"xi-api-key": CFG["el_api_key"].strip(), "accept": "audio/mpeg"}
        r = None
        for attempt in range(2):                      # one retry on a network hiccup, so we don't jump to another voice
            try:
                r = httpx.post(url, headers=headers, json=self._eleven_body(text, seed), timeout=60)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                if attempt:
                    raise RuntimeError(f"ElevenLabs network: {e}")
                time.sleep(0.6)
                continue
            if r.status_code == 400 and Speaker._el_lang_ok and "language" in r.text.lower():
                Speaker._el_lang_ok = False           # this model/account doesn't take language_code -> send without it
                continue
            if r.status_code in (500, 502, 503, 504) and not attempt:
                time.sleep(0.6)
                continue
            break
        if r.status_code in (401, 402, 429) and ("quota" in r.text or "credits" in r.text or r.status_code == 402):
            Speaker._el_left = 0
            Speaker._el_checked = time.time()
            raise QuotaOut("ElevenLabs credits used up")
        if r.status_code >= 400:
            raise RuntimeError(f"ElevenLabs {r.status_code}: {r.text[:200]}")
        if Speaker._el_left is not None:
            Speaker._el_left = max(0, Speaker._el_left - len(text))
        return r.content

    @staticmethod
    def _mp3_seconds(audio, small):
        return len(audio) * 8 / ((64 if small else 128) * 1000)      # ElevenLabs mp3 is constant bitrate

    def _eleven(self, text, small=False):
        need = len(text) + 20
        left = self.eleven_credits()
        if left is not None and left < need:           # never go past what's already paid for
            raise QuotaOut("ElevenLabs credits used up")
        audio = self._eleven_once(text, small)
        # v3 sometimes stops in the middle of a sentence. Hebrew speech is ~12-16 letters a second;
        # if the recording is far too short for the text, the end was swallowed -> make it once more.
        secs = self._mp3_seconds(audio, small)
        letters = len(strip_niqqud(text))
        if letters > 30 and secs < letters / 24.0 and (self._el_left is None or self._el_left > need):
            log.warning(f"ElevenLabs cut the answer ({secs:.1f}s for {letters} letters) - redoing it")
            again = self._eleven_once(text, small, seed=self.EL_SEED + 1)
            if self._mp3_seconds(again, small) > secs:
                audio = again
        if (Speaker._el_left or 0) > 2000:              # credits renewed -> warn again next time they run out
            Speaker._el_warned = False
        return audio

    def free_synth(self, text):
        if CFG.get("gemini_api_key"):
            try:
                return self._gemini(text)
            except Exception as e:
                log.error(f"Gemini TTS failed: {e}")
        return self._edge(text, female=CFG.get("el_female", True))

    def synth(self, text, small=False):
        text = strip_niqqud(text)
        if CFG.get("niqqud_voice", True):
            text = add_niqqud(text)                  # what is SPOKEN gets niqqud; what is SHOWN stays plain
        if CFG.get("tts_engine") == "eleven" and CFG.get("el_api_key"):
            try:
                audio = self._eleven(text, small)
                log.info(f"voice: ElevenLabs ({len(text)} letters)")
                return audio
            except QuotaOut:
                if not Speaker._el_warned:
                    Speaker._el_warned = True
                    notify("🔊 נגמרו הקרדיטים של ElevenLabs. עברתי לקול החינמי עד שהם יתחדשו (בלי לשלם עוד).")
            except Exception as e:
                log.error(f"ElevenLabs TTS failed - this answer uses the free voice: {e}")
            return self.free_synth(text)
        if CFG.get("tts_engine") == "gemini" and CFG.get("gemini_api_key"):
            try:
                return self._gemini(text)
            except Exception as e:
                log.error(f"Gemini TTS failed: {e}")
                if "quota" in str(e):
                    ui.toast("נגמרה המכסה החינמית של Gemini להיום - משתמש בקול הרגיל.", "warn")
                else:
                    ui.toast("הקול של Gemini לא עבד - משתמש בקול הרגיל. בדוק את המפתח בהגדרות.", "warn")
        return self._edge(text)

    @staticmethod
    def whole_answer():
        return CFG.get("tts_engine") == "eleven" and bool(CFG.get("el_api_key")) and (Speaker._el_left is None or Speaker._el_left > 0)

    @staticmethod
    def split_for_speech(text):
        """First sentence alone (so speech starts fast), the rest together."""
        import re
        parts = [p for p in re.split(r"(?<=[.!?…])\s+", text.strip()) if p]
        if len(parts) <= 1:
            return [text.strip()]
        first = parts[0]
        i = 1
        while len(first) < 25 and i < len(parts):      # avoid a tiny first chunk
            first += " " + parts[i]
            i += 1
        rest = " ".join(parts[i:])
        return [first] + ([rest] if rest else [])

    def say(self, text, show=True):
        if not text:
            return
        with self.lock:
            self._stop = False
            # ElevenLabs: the whole answer in ONE recording, so the voice stays the same and never breaks mid-sentence
            pieces = [text.strip()] if self.whole_answer() else self.split_for_speech(text)
            q = queue.Queue()

            def produce():
                for piece in pieces:
                    if self._stop:
                        break
                    try:
                        q.put(self.synth(piece))
                    except Exception as e:
                        q.put(e)
                q.put(None)
            threading.Thread(target=produce, daemon=True).start()
            shown = False
            failed = False
            while True:
                audio = q.get()
                if audio is None:
                    break
                if isinstance(audio, Exception):
                    log.error(f"TTS failed: {audio}")
                    failed = True
                    continue
                if show and not shown:
                    ui.say("jarvis", strip_niqqud(text))   # text appears together with the voice
                    shown = True
                self._play(audio)
                if self._stop:
                    break
            if show and not shown:
                ui.say("jarvis", strip_niqqud(text))
            if failed and not shown:
                ui.toast("ההקראה נכשלה (אין אינטרנט?)", "warn")
            ui.state("idle")

    def play_bytes(self, audio, text=""):
        with self.lock:
            self._stop = False
            self._play(audio)
            ui.state("idle")

    def _play(self, audio):
        ext = ".wav" if audio[:4] == b"RIFF" else ".mp3"
        path = os.path.join(tempfile.gettempdir(), f"jarvis_{int(time.time()*1000)}{ext}")
        with open(path, "wb") as f:
            f.write(audio)
        ui.state("speaking")
        try:
            m = self.pygame.mixer.music
            m.load(path)
            m.play()
            while m.get_busy() and not self._stop:
                time.sleep(0.03)
            if not self._stop:
                time.sleep(0.25)                  # let the last syllable leave the speakers before stopping
            m.stop()
            try:
                m.unload()
            except Exception:
                pass
        except Exception as e:
            log.error(f"playback failed: {e}")
        finally:
            try:
                os.remove(path)
            except Exception:
                pass

    def stop(self):
        self._stop = True
        try:
            self.pygame.mixer.music.stop()
        except Exception:
            pass


# ======================================================================= VOICE IN
class Ears:
    def __init__(self):
        import numpy as np
        import sounddevice as sd
        import speech_recognition as sr
        import openwakeword
        from openwakeword.model import Model

        self.np, self.sd, self.sr = np, sd, sr
        ui.toast("טוען את מילת ההפעלה…")
        try:
            openwakeword.utils.download_models(model_names=["hey_jarvis"])
        except TypeError:
            openwakeword.utils.download_models()
        try:   # built-in voice detector cuts false "Hey Jarvis" triggers from noise
            self.ww = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx", vad_threshold=0.3)
        except Exception:
            self.ww = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
        self.vad = None
        try:
            from openwakeword.vad import VAD
            try:
                self.vad = VAD()
            except Exception:
                openwakeword.utils.download_models()
                self.vad = VAD()
            self.vad.predict(np.zeros(CHUNK, dtype=np.int16), frame_size=640)
            log.info("speech detector: neural VAD")
        except Exception as e:
            self.vad = None
            log.warning(f"neural VAD unavailable, using volume detection: {e}")
        self.noise = 300.0          # running estimate of background noise (RMS)
        self.rec = sr.Recognizer()
        self.stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=CHUNK)
        self.stream.start()
        self._n = 0

    def read(self):
        data, _ = self.stream.read(CHUNK)
        chunk = data[:, 0].copy()
        self.last_rms = float(self.np.sqrt(self.np.mean(chunk.astype(self.np.float32) ** 2)))
        self._n += 1
        if self._n % 2 == 0:
            ui.level(min(1.0, self.last_rms / 4000))
        return chunk

    def speech_prob(self, chunk):
        """0..1 - how sure we are that this 80ms chunk is human speech."""
        if self.vad is not None:
            try:
                return float(self.vad.predict(chunk, frame_size=640))
            except Exception:
                pass
        thr = max(350.0, self.noise * 2.5)
        return 1.0 if self.last_rms > thr else 0.0

    def learn_noise(self):
        """Called while idle: slowly track the room's background noise."""
        if getattr(self, "last_rms", None) is not None and self.last_rms < self.noise * 3:
            self.noise = 0.97 * self.noise + 0.03 * max(50.0, self.last_rms)

    def flush(self):
        try:
            while self.stream.read_available >= CHUNK:
                self.stream.read(CHUNK)
        except Exception:
            pass
        self.ww.reset()
        self._gili_reset()

    def heard_wake_word(self, chunk):
        self.learn_noise()
        pred = self.ww.predict(chunk)
        if pred and max(pred.values()) >= float(CFG["wake_threshold"]):
            self.ww.reset()
            return True
        return False

    # ---- "היי גילי": there is no ready-made model for this name, so short phrases are checked by Hebrew speech recognition
    GILI_RE = re.compile(r"(?:^|[\s,.!?])(?:היי|הי|הֵי|הַי|hey)?[\s,]*(גילי|ג'ילי|גִּילִי|גילִי|gili|gilly|gilli)(?=$|[\s,.!?])", re.I)

    def _gili_reset(self):
        from collections import deque
        self._g_pre = deque(maxlen=4)
        self._g_recent = deque(maxlen=3)
        self._g_frames, self._g_on, self._g_voiced, self._g_silent = [], False, 0, 0.0

    def transcribe_raw(self, pcm):
        """Whatever was said, in any language - for meeting notes (no 'is it addressed to me' filtering)."""
        if CFG.get("groq_api_key"):
            try:
                t = self.transcribe_groq(pcm, raw=True)
                if t is not None:
                    return t
            except Exception as e:
                log.warning(f"meeting STT (Groq): {e}")
        return self._wake_text(pcm)

    def _meeting_job(self, frames):
        """Runs on a side thread so the microphone never stops while a sentence is being written down."""
        try:
            np = self.np
            audio = np.concatenate(frames)
            text = (self.transcribe_raw(audio.tobytes()) or "").strip()
            if not text:
                return
            m = self.GILI_RE.search(text)
            if m and len(text[:m.start()].split()) <= 2:           # someone called her in the middle of the meeting
                if not VOICE.allowed(audio, wake=True):
                    self._async_wake = VoiceID.DENIED
                    return
                rest = text[m.end():].strip(" ,.!?")
                self._async_wake = rest if len(re.findall(r"[\u05d0-\u05ea]", rest)) >= 2 else ""
                return
            MEETING.add(text)
        except Exception:
            log.exception("meeting transcript")

    def async_wake(self):
        w = getattr(self, "_async_wake", None)
        self._async_wake = None
        return w

    def heard_gili(self, chunk):
        """Feed one 80ms chunk. Returns None while waiting, or the words said after "גילי" ("" if only the name)."""
        if not hasattr(self, "_g_pre"):
            self._gili_reset()
        self.learn_noise()
        p = self.speech_prob(chunk)
        thr = min(0.8, max(0.2, 1.0 - float(CFG.get("wake_threshold", 0.5))))
        step = CHUNK / SAMPLE_RATE
        if not self._g_on:
            self._g_pre.append(chunk)
            self._g_recent.append(p >= thr)
            if sum(self._g_recent) >= 2:
                self._g_on = True
                self._g_frames = list(self._g_pre)
                self._g_voiced = 2
            return None
        self._g_frames.append(chunk)
        if p >= thr * 0.7:
            self._g_voiced += 1
            self._g_silent = 0.0
        else:
            self._g_silent += step
        length = len(self._g_frames) * step
        meeting = MEETING.on
        if self._g_silent < (0.8 if meeting else 0.6) and length < (14.0 if meeting else 7.0):
            return None
        frames, voiced = self._g_frames, self._g_voiced
        self._gili_reset()
        if voiced < 4:                                     # a cough or a click: not a call
            return None
        if meeting:                                        # write it down in the background, keep listening
            if getattr(self, "_meet_pool", None) is None:
                from concurrent.futures import ThreadPoolExecutor
                self._meet_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="meeting")
            self._meet_pool.submit(self._meeting_job, frames)
            return None
        audio = self.np.concatenate(frames)
        text = self._wake_text(audio.tobytes())
        if not text:
            return None
        m = self.GILI_RE.search(text)
        if not m or len(text[:m.start()].split()) > 2:     # the name has to come at the start
            return None
        log.info(f"wake: {text}")
        if not VOICE.allowed(audio, wake=True):
            return VoiceID.DENIED
        rest = text[m.end():].strip(" ,.!?")
        return rest if len(re.findall(r"[\u05d0-\u05ea]", rest)) >= 2 else ""

    def _wake_text(self, pcm):
        """All the ways Google heard the phrase, joined (so 'גילי' is caught even if it's not the top guess)."""
        try:
            res = self.rec.recognize_google(self.sr.AudioData(pcm, SAMPLE_RATE, 2), language="he-IL", show_all=True)
            alts = res.get("alternative", []) if isinstance(res, dict) else []
            texts = [a.get("transcript", "") for a in alts if a.get("transcript")]
            hit = next((t for t in texts if self.GILI_RE.search(t)), None)
            return hit or (texts[0] if texts else "")
        except self.sr.UnknownValueError:
            return ""
        except Exception as e:
            log.warning(f"wake check: {e}")
            return ""

    def beep(self, freq=1046, dur=0.09):
        np = self.np
        t = np.linspace(0, dur, int(22050 * dur), False)
        tone = (np.sin(2 * np.pi * freq * t) * 0.22 * np.hanning(t.size)).astype(np.float32)
        try:
            self.sd.play(tone, 22050)
            self.sd.wait()
        except Exception:
            pass

    def listen(self, start_timeout=6.0, max_seconds=40.0, silence_end=None):
        """Record one sentence. Starts only on real speech, keeps the first syllable, stops after a pause."""
        from collections import deque
        np = self.np
        if silence_end is None:
            silence_end = float(CFG.get("pause_seconds", 1.4))
        time.sleep(0.15)                     # let the beep / Jarvis's own voice die out
        self.flush()
        ui.state("listening")
        step = CHUNK / SAMPLE_RATE
        pre = deque(maxlen=4)                # 320ms before speech starts (so words aren't cut)
        recent = deque(maxlen=3)
        frames, voiced, started, silent, waited, total = [], 0, False, 0.0, 0.0, 0.0
        while True:
            c = self.read()
            p = self.speech_prob(c)
            if not started:
                waited += step
                pre.append(c)
                recent.append(p >= 0.5)
                if sum(recent) >= 2:         # speech in 2 of the last 3 chunks -> real talking
                    started = True
                    frames.extend(pre)
                    voiced = 2
                elif waited > start_timeout:
                    ui.state("idle")
                    return None
                continue
            frames.append(c)
            total += step
            if p >= 0.35:
                silent = 0.0
                voiced += 1
            else:
                silent += step
            if silent >= silence_end or total >= max_seconds:
                break
        self.last_pcm = None
        if voiced < 4:                       # under ~0.3s of speech = a cough / click, ignore
            ui.state("idle")
            return None
        ui.state("thinking")
        tail = max(0, int(silent / step) - 3)            # the recording without the long pause at its end
        self.last_pcm = np.concatenate(frames[:len(frames) - tail] if tail else frames)
        return self.transcribe(np.concatenate(frames).tobytes())

    WHISPER_JUNK = ("תודה רבה", "תודה שצפיתם", "כתוביות", "תרגום", "subtitles", "תודה על הצפייה")

    def transcribe_groq(self, pcm, raw=False):
        """Groq Whisper (free). Returns text, None for silence/noise, or raises to fall back to Google."""
        import httpx
        buf = BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(SAMPLE_RATE); w.writeframes(pcm)
        r = httpx.post("https://api.groq.com/openai/v1/audio/transcriptions",
                       headers={"Authorization": f"Bearer {CFG['groq_api_key']}"},
                       data={"model": "whisper-large-v3-turbo", "language": "he",
                             "response_format": "verbose_json", "temperature": "0"},
                       files={"file": ("speech.wav", buf.getvalue(), "audio/wav")}, timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"Groq STT {r.status_code}: {r.text[:150]}")
        d = r.json()
        text = (d.get("text") or "").strip()
        segs = d.get("segments") or []
        if segs:
            nsp = sum(sg.get("no_speech_prob", 0) for sg in segs) / len(segs)
            alp = sum(sg.get("avg_logprob", 0) for sg in segs) / len(segs)
            if nsp > 0.75 or alp < -1.3:
                log.info(f"Whisper: ignored unsure result ({nsp:.2f}/{alp:.2f}): {text}")
                return None
        if len(text) < 30 and any(j in text for j in self.WHISPER_JUNK):
            log.info(f"Whisper: ignored typical hallucination: {text}")
            return None          # Whisper invents these on silence
        return text if raw else hebrew_only(text)

    def transcribe(self, pcm):
        """Returns Hebrew text, or None if the speech was not clearly Hebrew."""
        has_groq = bool(CFG.get("groq_api_key"))
        if CFG.get("stt_engine") == "groq" and has_groq:
            try:
                return self.transcribe_groq(pcm)
            except Exception as e:
                log.error(f"Groq STT failed, using Google: {e}")
        text = self._google(pcm)
        if text is None and has_groq and CFG.get("stt_engine") != "groq":
            try:                                   # Google wasn't sure -> ask Whisper before giving up
                return self.transcribe_groq(pcm)
            except Exception as e:
                log.error(f"Groq STT fallback failed: {e}")
        return text

    def _google(self, pcm):
        try:
            res = self.rec.recognize_google(self.sr.AudioData(pcm, SAMPLE_RATE, 2), language="he-IL", show_all=True)
        except self.sr.UnknownValueError:
            return None
        except Exception as e:
            log.error(f"Speech recognition failed: {e}")
            ui.toast("זיהוי דיבור צריך חיבור לאינטרנט.", "warn")
            return None
        if not res or not isinstance(res, dict) or not res.get("alternative"):
            return None
        best = res["alternative"][0]
        text = (best.get("transcript") or "").strip()
        conf = best.get("confidence")
        if conf is not None and conf < float(CFG["min_confidence"]):
            log.info(f"ignored low-confidence speech ({conf:.2f}): {text}")
            return None
        return hebrew_only(text)


# ======================================================================= NIQQUD (vowel marks)
_NIQQUD_RE = re.compile("[\u0591-\u05BD\u05BF\u05C1\u05C2\u05C4\u05C5\u05C7]")
NAKDAN_URLS = ["https://nakdan-2-0.loadbalancer.dicta.org.il/api",
               "https://nakdan-u1-0.loadbalancer.dicta.org.il/api"]


def strip_niqqud(text):
    """The same text without vowel marks - this is what the user sees."""
    return _NIQQUD_RE.sub("", text or "")


def add_niqqud(text):
    """Vowel marks for the voice only (Dicta's free Nakdan), so names and words are pronounced right.
    Any problem -> the plain text, never a broken answer."""
    import httpx
    if not text or not re.search(r"[\u05d0-\u05ea]", text):
        return text
    body = {"task": "nakdan", "data": text, "genre": "modern", "addmorph": False,
            "keepnikud": True, "keepmetagim": False, "keepqq": False}
    for url in NAKDAN_URLS:
        try:
            r = httpx.post(url, json=body, timeout=4)
            if r.status_code != 200:
                continue
            d = r.json()
            words = d.get("data", d) if isinstance(d, dict) else d
            out = []
            for w in words:
                plain = w.get("word", "")
                opts = w.get("options") or []
                best = plain
                if not w.get("sep") and opts:
                    o = opts[0]
                    if isinstance(o, dict):
                        o = o.get("w") or o.get("word") or plain
                    elif isinstance(o, (list, tuple)):
                        o = o[0] if o else plain
                    if isinstance(o, str):
                        o = o.replace("|", "").replace("\u05BD", "")
                        if strip_niqqud(o).replace("\u05BE", "") == strip_niqqud(plain).replace("\u05BE", ""):
                            best = o            # only accept it if it is the very same word
                out.append(best)
            res = "".join(out)
            if strip_niqqud(res) == strip_niqqud(text):
                return res
            log.warning("nakdan: answer didn't match the text, speaking without niqqud")
            return text
        except Exception as e:
            log.warning(f"nakdan {url}: {e}")
    return text


def hebrew_only(text):
    """The text if it is Hebrew, otherwise None (English, Arabic, Russian, noise...)."""
    import re
    if not text:
        return None
    heb = len(re.findall(r"[\u05d0-\u05ea]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    other = len(re.findall(r"[\u0400-\u04ff\u0600-\u06ff]", text))
    if heb < 2 or other or heb * 4 < latin:          # Hebrew with English names inside ("תפתח Fortnite") is fine
        log.info(f"ignored non-Hebrew text: {text}")
        return None
    return text


def english_only(text):
    """None if the text contains non-Latin letters (Hebrew, Arabic, Cyrillic...) or no real words."""
    import re
    if not text:
        return None
    if re.search(r"[^\x00-\x7F\u2018\u2019\u201c\u201d\u2013\u2014\u00e9\u00e8\u00e1\u00ed\u00f3\u00fa\u00f1]", text):
        log.info(f"ignored non-English text: {text}")
        return None
    if not re.search(r"[A-Za-z]{2,}", text):
        return None
    return text


# ======================================================================= COMPUTER CONTROL
PROTECTED = {"python.exe", "pythonw.exe", "msedgewebview2.exe", "explorer.exe", "svchost.exe",
             "csrss.exe", "winlogon.exe", "lsass.exe", "dwm.exe", "system", "services.exe",
             "smss.exe", "wininit.exe", "fontdrvhost.exe", "conhost.exe"}

APP_ALIASES = {
    "chrome": "chrome", "google chrome": "chrome", "edge": "msedge", "microsoft edge": "msedge",
    "firefox": "firefox", "notepad": "notepad", "calculator": "calc", "calc": "calc",
    "file explorer": "explorer", "explorer": "explorer", "files": "explorer", "paint": "mspaint",
    "word": "winword", "excel": "excel", "powerpoint": "powerpnt", "outlook": "outlook",
    "vs code": "code", "vscode": "code", "visual studio code": "code",
    "settings": "ms-settings:", "spotify": "spotify:", "whatsapp": "whatsapp:",
    "task manager": "taskmgr", "command prompt": "cmd", "cmd": "cmd", "terminal": "wt",
    "powershell": "powershell", "camera": "microsoft.windows.camera:", "store": "ms-windows-store:",
    "mail": "outlookmail:", "calendar": "outlookcal:", "photos": "ms-photos:", "clock": "ms-clock:",
}

HE_APPS = {
    "כרום": "chrome", "גוגל כרום": "chrome", "הכרום": "chrome", "אדג'": "msedge", "אדג": "msedge",
    "פיירפוקס": "firefox", "פנקס רשימות": "notepad", "פנקס הרשימות": "notepad", "נוטפד": "notepad",
    "מחשבון": "calc", "המחשבון": "calc", "סייר קבצים": "explorer", "סייר הקבצים": "explorer",
    "צייר": "mspaint", "פיינט": "mspaint", "וורד": "winword", "אקסל": "excel", "פאוורפוינט": "powerpnt",
    "אאוטלוק": "outlook", "הגדרות": "ms-settings:", "ההגדרות": "ms-settings:", "ספוטיפיי": "spotify",
    "וואטסאפ": "whatsapp", "ווטסאפ": "whatsapp", "וואצאפ": "whatsapp", "מנהל המשימות": "taskmgr",
    "שורת הפקודה": "cmd", "מצלמה": "camera", "המצלמה": "camera", "חנות": "store", "תמונות": "photos",
    "שעון": "clock", "דיסקורד": "discord", "סטים": "steam", "זום": "zoom", "טימס": "teams",
    "אפיק": "epic games launcher", "אפיק גיימס": "epic games launcher", "נטפליקס": "netflix",
    "טלגרם": "telegram", "וי אס קוד": "vs code", "רובלוקס": "roblox", "מיינקראפט": "minecraft",
    "פורטנייט": "fortnite", "פורט נייט": "fortnite", "הפורטנייט": "fortnite", "ולורנט": "valorant", "וולורנט": "valorant",
    "רוקט ליג": "rocket league", "פיפא": "ea sports fc", "פיפה": "ea sports fc", "אף סי": "ea sports fc",
    "קול אוף דיוטי": "call of duty", "וורזון": "call of duty", "גי טי איי": "grand theft auto", "ג'י טי איי": "grand theft auto",
    "אייפקס": "apex legends", "אייפקס לג'נדס": "apex legends", "קאונטר סטרייק": "counter-strike", "סי אס": "counter-strike",
    "ליג אוף לג'נדס": "league of legends", "פורזה": "forza", "אובר וואץ'": "overwatch", "אוברווטש": "overwatch",
    "ריוט": "riot client", "אקס בוקס": "xbox", "באטל נט": "battle.net", "יוביסופט": "ubisoft connect",
}
HE_SITES = {
    "יוטיוב": "https://www.youtube.com", "גוגל": "https://www.google.com", "ג'ימייל": "https://mail.google.com",
    "גימייל": "https://mail.google.com", "פייסבוק": "https://www.facebook.com", "אינסטגרם": "https://www.instagram.com",
    "טיקטוק": "https://www.tiktok.com", "ויקיפדיה": "https://he.wikipedia.org", "וויינט": "https://www.ynet.co.il",
    "ynet": "https://www.ynet.co.il", "וואלה": "https://www.walla.co.il", "וייז": "https://www.waze.com/live-map",
    "ווייז": "https://www.waze.com/live-map", "צ'אט": "https://claude.ai",
    "גוגל מפות": "https://www.google.com/maps", "מפות": "https://www.google.com/maps",
    "גוגל דרייב": "https://drive.google.com", "דרייב": "https://drive.google.com",
    "גוגל תרגום": "https://translate.google.com", "תרגום": "https://translate.google.com",
    "יומן גוגל": "https://calendar.google.com", "גוגל יומן": "https://calendar.google.com",
    "גוגל תמונות": "https://photos.google.com", "גוגל דוקס": "https://docs.google.com",
}

KNOWN_FOLDERS = {
    "downloads": Path.home() / "Downloads", "documents": Path.home() / "Documents",
    "desktop": Path.home() / "Desktop", "pictures": Path.home() / "Pictures",
    "music": Path.home() / "Music", "videos": Path.home() / "Videos", "home": Path.home(),
}


def _start_menu_lookup(name):
    roots = [
        Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Microsoft/Windows/Start Menu/Programs",
        Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
        Path.home() / "Desktop",
        Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop",
    ]
    n = name.lower().strip()
    best = None
    for root in roots:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if p.suffix.lower() not in (".lnk", ".url", ".exe"):
                continue
            stem = p.stem.lower()
            if stem == n:
                return p
            if n in stem and (best is None or len(stem) < len(best.stem)):
                best = p
    return best


# ---------- games & launchers (Epic, Steam) + desktop shortcuts
_GAMES = {"t": 0, "items": []}


def _norm(s):
    return re.sub(r"[^a-z0-9\u05d0-\u05ea]+", "", (s or "").lower())


def game_index():
    """Installed games from Epic Games and Steam: [(name, how to launch)]. Cached for 5 minutes."""
    if time.time() - _GAMES["t"] < 300:
        return _GAMES["items"]
    items = []
    try:   # Epic Games Launcher
        man = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Epic/EpicGamesLauncher/Data/Manifests"
        for f in man.glob("*.item"):
            try:
                j = json.loads(f.read_text(encoding="utf-8", errors="replace"))
                if j.get("bIsIncompleteInstall") or not j.get("AppName"):
                    continue
                uri = (f"com.epicgames.launcher://apps/{j.get('CatalogNamespace', '')}%3A{j.get('CatalogItemId', '')}"
                       f"%3A{j['AppName']}?action=launch&silent=true")
                items.append((j.get("DisplayName") or j["AppName"], uri))
            except Exception:
                pass
    except Exception:
        pass
    try:   # Steam
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            steam = Path(winreg.QueryValueEx(k, "SteamPath")[0])
        libs = {steam}
        vdf = steam / "steamapps/libraryfolders.vdf"
        if vdf.exists():
            libs |= {Path(p.replace("\\\\", "\\")) for p in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(errors="replace"))}
        for lib in libs:
            for acf in (lib / "steamapps").glob("appmanifest_*.acf"):
                t = acf.read_text(errors="replace")
                aid, nm = re.search(r'"appid"\s+"(\d+)"', t), re.search(r'"name"\s+"([^"]+)"', t)
                if aid and nm and "redistributable" not in nm.group(1).lower():
                    items.append((nm.group(1), f"steam://rungameid/{aid.group(1)}"))
    except Exception:
        pass
    _GAMES.update(t=time.time(), items=items)
    return items


def find_game(name):
    import difflib
    q = _norm(name)
    if not q:
        return None
    best, score = None, 0.0
    for title, uri in game_index():
        n = _norm(title)
        if not n:
            continue
        s = 1.0 if n == q else 0.92 if (q in n or n in q) and min(len(q), len(n)) >= max(3, 0.6 * max(len(q), len(n))) else difflib.SequenceMatcher(None, q, n).ratio()
        if s > score:
            best, score = (title, uri), s
    return best if score >= 0.72 else None


def open_app(name):
    raw = name.strip()
    target = APP_ALIASES.get(raw.lower())
    try:
        if OS == "Windows":
            if target:
                if target.endswith(":"):
                    os.startfile(target)
                else:
                    subprocess.Popen(f'start "" {target}', shell=True)
                return {"ok": True, "opened": raw}
            game = find_game(raw)
            if game:                                   # Epic / Steam games open through their launcher
                os.startfile(game[1])
                return {"ok": True, "opened": game[0], "note": "launching through the game launcher - it can take a minute"}
            lnk = _start_menu_lookup(raw)
            if lnk:
                os.startfile(str(lnk))
                return {"ok": True, "opened": lnk.stem}
            if shutil.which(raw):
                subprocess.Popen(f'start "" "{raw}"', shell=True)
                return {"ok": True, "opened": raw}
            # Store apps (e.g. Netflix, Xbox) via PowerShell lookup
            ps = f"$a=Get-StartApps | Where-Object {{$_.Name -like '*{raw}*'}} | Select-Object -First 1; if($a){{Start-Process ('shell:AppsFolder\\'+$a.AppID); $a.Name}}"
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=15,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.stdout.strip():
                return {"ok": True, "opened": r.stdout.strip()}
            games = [t for t, _ in game_index()]
            return {"ok": False, "error": f"לא נמצאה במחשב תוכנה בשם '{raw}'",
                    "installed_games": games[:40], "hint": "if one of installed_games is what the user meant, call open_app again with that exact name"}
        elif OS == "Darwin":
            r = subprocess.run(["open", "-a", raw], capture_output=True)
            return {"ok": r.returncode == 0, "opened": raw}
        exe = shutil.which(target or raw) or shutil.which(raw.lower())
        if exe:
            subprocess.Popen([exe], start_new_session=True)
            return {"ok": True, "opened": raw}
        return {"ok": False, "error": f"'{raw}' not found"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def list_open_apps():
    import psutil
    if OS == "Windows":
        ps = "Get-Process | Where-Object {$_.MainWindowTitle} | ForEach-Object { $_.ProcessName + ' | ' + $_.MainWindowTitle }"
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        rows = [l.strip() for l in r.stdout.splitlines() if l.strip() and not l.lower().startswith(("python", "msedgewebview2"))]
        return {"open_windows": rows[:40]}
    names = sorted({p.info["name"] for p in psutil.process_iter(["name"]) if p.info["name"]})
    return {"processes": names[:80]}


BROWSERS = {"chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "brave.exe"}


def _no_window():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def close_app(name):
    """Close a program: by process name first, then by window title. Kills the whole process tree."""
    import psutil
    raw = name.lower().strip()
    for junk in ("the ", "my ", " app", " application", " window", " program"):
        raw = raw.replace(junk, " ").strip()
    n = raw.removesuffix(".exe")
    n = APP_ALIASES.get(n, n).rstrip(":").replace(" ", "")
    targets = {}
    for p in psutil.process_iter(["name", "pid"]):
        pname = (p.info["name"] or "").lower()
        if not pname or pname in PROTECTED or p.info["pid"] == os.getpid():
            continue
        base = pname.removesuffix(".exe").replace(" ", "")
        if n == base or (len(n) >= 3 and (n in base or base in n and len(base) >= 4)):
            targets[pname] = True
    # nothing by process name -> try window titles (e.g. "Netflix", "Minecraft Launcher")
    if not targets and OS == "Windows":
        ps = ("Get-Process | Where-Object { $_.MainWindowTitle -like '*" + name.replace("'", "") +
              "*' } | ForEach-Object { $_.ProcessName + '.exe' }")
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                               text=True, timeout=15, creationflags=_no_window())
            for line in r.stdout.splitlines():
                pn = line.strip().lower()
                if pn and pn not in PROTECTED:
                    targets[pn] = True
        except Exception as e:
            log.error(f"title lookup failed: {e}")
    if not targets:
        return {"ok": False, "error": f"'{name}' לא פתוחה כרגע"}
    closed = []
    for pname in targets:
        if OS == "Windows":
            r = subprocess.run(["taskkill", "/IM", pname, "/T", "/F"], capture_output=True,
                               text=True, creationflags=_no_window())
            if r.returncode == 0:
                closed.append(pname)
                continue
        for p in psutil.process_iter(["name"]):
            if (p.info["name"] or "").lower() == pname:
                try:
                    p.kill(); closed.append(pname)
                except Exception:
                    pass
    if closed:
        return {"ok": True, "closed": sorted(set(closed))}
    return {"ok": False, "error": f"Windows לא נתן לסגור את {', '.join(targets)} (אולי צריך הרשאות מנהל)"}


# ---------- internet (works without any special account setting, and it's free)
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"}


def web_search(query, max_results=6):
    results = []
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        for r in DDGS().text(query, max_results=max_results):
            results.append({"title": r.get("title"), "url": r.get("href"), "snippet": r.get("body")})
    except Exception as e:
        log.warning(f"ddgs failed: {e}")
    if not results:
        try:
            import re, html, httpx
            page = httpx.post("https://html.duckduckgo.com/html/", data={"q": query}, headers=UA,
                              timeout=15, follow_redirects=True).text
            links = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, re.S)
            snips = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', page, re.S)
            clean = lambda t: html.unescape(re.sub(r"<[^>]+>", "", t)).strip()
            for i, (u, t) in enumerate(links[:max_results]):
                if "uddg=" in u:
                    u = urllib.parse.unquote(u.split("uddg=")[1].split("&")[0])
                results.append({"title": clean(t), "url": u, "snippet": clean(snips[i]) if i < len(snips) else ""})
        except Exception as e:
            log.error(f"html search failed: {e}")
            return {"ok": False, "error": "אין חיבור לאינטרנט"}
    return {"ok": True, "results": results}


def read_webpage(url):
    import re, html, httpx
    r = httpx.get(url, headers=UA, timeout=20, follow_redirects=True)
    t = re.sub(r"(?is)<(script|style|noscript|svg|head).*?</\1>", " ", r.text)
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    t = re.sub(r"\s+", " ", t).strip()
    return {"ok": True, "text": t[:6000]}


def get_weather(city=""):
    import httpx
    r = httpx.get(f"https://wttr.in/{urllib.parse.quote(city)}?format=j1", headers=UA, timeout=20)
    d = r.json()
    cur = d["current_condition"][0]
    days = [{"date": w["date"], "min_c": w["mintempC"], "max_c": w["maxtempC"],
             "desc": w["hourly"][4]["weatherDesc"][0]["value"],
             "rain_chance": w["hourly"][4].get("chanceofrain")} for w in d.get("weather", [])[:3]]
    area = d.get("nearest_area", [{}])[0].get("areaName", [{"value": city}])[0]["value"]
    return {"location": area, "now_c": cur["temp_C"], "feels_like_c": cur["FeelsLikeC"],
            "now": cur["weatherDesc"][0]["value"], "humidity": cur["humidity"], "forecast": days}


def open_path(path):
    p = KNOWN_FOLDERS.get(path.lower().strip(), Path(os.path.expandvars(os.path.expanduser(path))))
    if not p.exists():
        return {"ok": False, "error": f"'{path}' לא קיים"}
    if OS == "Windows":
        os.startfile(str(p))
    else:
        subprocess.Popen(["open" if OS == "Darwin" else "xdg-open", str(p)])
    return {"ok": True, "opened": str(p)}


def find_files(name, max_results=15):
    n = name.lower()
    found = []
    for root in KNOWN_FOLDERS.values():
        if root == Path.home() or not root.exists():
            continue
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith(".")][:200]
            for f in files + dirs:
                if n in f.lower():
                    found.append(os.path.join(dirpath, f))
                    if len(found) >= max_results:
                        return {"results": found}
    return {"results": found}


def find_chrome():
    """Path to Google Chrome, or None."""
    if OS == "Windows":
        for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")):
            if base:
                p = Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"
                if p.exists():
                    return str(p)
        try:   # Chrome registers its path here
            import winreg
            for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    k = winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe")
                    v = winreg.QueryValue(k, None)
                    if v and Path(v).exists():
                        return v
                except OSError:
                    pass
        except Exception:
            pass
        return None
    if OS == "Darwin":
        p = "/Applications/Google Chrome.app"
        return p if Path(p).exists() else None
    return shutil.which("google-chrome") or shutil.which("chromium")


def open_in_chrome(url):
    """Everything on the web opens in Google Chrome (falls back to the default browser if it isn't installed)."""
    chrome = find_chrome()
    try:
        if chrome and OS == "Darwin":
            subprocess.Popen(["open", "-a", chrome, url])
            return True
        if chrome:
            subprocess.Popen([chrome, url], creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
            return True
    except Exception as e:
        log.error(f"chrome: {e}")
    webbrowser.open(url)
    return False


def open_website(url):
    if not url.startswith("http"):
        url = "https://" + url
    in_chrome = open_in_chrome(url)
    return {"ok": True, "opened": url, "browser": "chrome" if in_chrome else "default"}


def google_search(query):
    open_in_chrome("https://www.google.com/search?q=" + urllib.parse.quote(query))
    return {"ok": True}


def play_youtube(query):
    """Find the best YouTube match and open it playing (in Chrome)."""
    import re
    vid, title = None, ""
    try:
        import httpx
        r = httpx.get("https://www.youtube.com/results", params={"search_query": query, "hl": "he"},
                      headers={**UA, "Accept-Language": "he,en;q=0.8"}, cookies={"CONSENT": "YES+1"},
                      timeout=15, follow_redirects=True)
        m = re.search(r'"videoRenderer":\{"videoId":"([\w-]{11})".{0,1500}?"title":\{"runs":\[\{"text":"(.*?)"\}', r.text, re.S)
        if m:
            vid, title = m.group(1), m.group(2)
        else:
            m = re.search(r'"videoId":"([\w-]{11})"', r.text)
            vid = m.group(1) if m else None
    except Exception as e:
        log.warning(f"youtube lookup failed: {e}")
    if not vid:   # second chance through web search
        try:
            for res in (web_search(f"{query} site:youtube.com").get("results") or []):
                m = re.search(r"(?:v=|youtu\.be/)([\w-]{11})", res.get("url") or "")
                if m:
                    vid, title = m.group(1), res.get("title", "")
                    break
        except Exception:
            pass
    if not vid:
        youtube_search(query)
        return {"ok": True, "played": False, "note": "opened search results"}
    open_in_chrome(f"https://www.youtube.com/watch?v={vid}&autoplay=1")
    try:
        title = json.loads(f'"{title}"') if title else ""
    except Exception:
        pass
    return {"ok": True, "played": True, "title": title}


def youtube_search(query, play_first=False):
    open_in_chrome("https://www.youtube.com/results?search_query=" + urllib.parse.quote(query))
    return {"ok": True}


def _gui():
    import pyautogui
    pyautogui.FAILSAFE = False
    return pyautogui


def media_control(action):
    keys = {"play_pause": "playpause", "next": "nexttrack", "previous": "prevtrack", "stop": "stop",
            "mute": "volumemute", "volume_up": "volumeup", "volume_down": "volumedown"}
    k = keys.get(action)
    if not k:
        return {"ok": False, "error": "unknown action"}
    _gui().press(k, presses=5 if action.startswith("volume_") else 1)
    return {"ok": True}


def set_volume(level):
    level = max(0, min(100, int(level)))
    g = _gui()
    g.press("volumedown", presses=50, interval=0.005)
    g.press("volumeup", presses=round(level / 2), interval=0.005)
    return {"ok": True, "volume": level}


def press_keys(keys):
    """keys like 'ctrl+w', 'alt+tab', 'win+d', 'enter'"""
    parts = [k.strip().lower() for k in keys.replace(" ", "").split("+") if k.strip()]
    alias = {"windows": "win", "control": "ctrl", "escape": "esc", "return": "enter"}
    parts = [alias.get(p, p) for p in parts]
    g = _gui()
    if len(parts) == 1:
        g.press(parts[0])
    else:
        g.hotkey(*parts)
    return {"ok": True, "pressed": "+".join(parts)}


def type_text(text):
    import pyperclip
    pyperclip.copy(text)
    time.sleep(0.1)
    _gui().hotkey("ctrl", "v")
    return {"ok": True}


def take_screenshot():
    folder = KNOWN_FOLDERS["pictures"] / "Jarvis"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"screenshot_{datetime.now():%Y%m%d_%H%M%S}.png"
    _gui().screenshot(str(path))
    return {"ok": True, "saved": str(path)}


def window_control(action):
    combos = {"minimize": ("win", "down"), "maximize": ("win", "up"), "show_desktop": ("win", "d"),
              "switch": ("alt", "tab"), "close_window": ("alt", "f4"), "snap_left": ("win", "left"),
              "snap_right": ("win", "right")}
    c = combos.get(action)
    if not c:
        return {"ok": False, "error": "unknown action"}
    _gui().hotkey(*c)
    return {"ok": True}


def power(action):
    cmds = {
        "lock": "rundll32.exe user32.dll,LockWorkStation",
        "sleep": "rundll32.exe powrprof.dll,SetSuspendState 0,1,0",
        "restart": "shutdown /r /t 10",
        "shutdown": "shutdown /s /t 10",
        "cancel": "shutdown /a",
        "sign_out": "shutdown /l",
    }
    if OS != "Windows":
        return {"ok": False, "error": "only supported on Windows"}
    c = cmds.get(action)
    if not c:
        return {"ok": False, "error": "unknown action"}
    subprocess.Popen(c, shell=True)
    return {"ok": True, "action": action}


def run_powershell(command):
    shell = ["powershell", "-NoProfile", "-Command", command] if OS == "Windows" else ["bash", "-lc", command]
    r = subprocess.run(shell, capture_output=True, text=True, timeout=60,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    out = (r.stdout + ("\n" + r.stderr if r.stderr else "")).strip()
    return {"exit_code": r.returncode, "output": out[-2500:]}


def system_info():
    import psutil
    b = psutil.sensors_battery()
    return {
        "cpu_percent": psutil.cpu_percent(interval=0.3),
        "ram_percent": psutil.virtual_memory().percent,
        "disk_percent": psutil.disk_usage(str(Path.home().anchor or "/")).percent,
        "battery": None if not b else {"percent": b.percent, "plugged_in": b.power_plugged},
    }


HE_DAYS = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]
HE_MONTHS = ["ינואר", "פברואר", "מרץ", "אפריל", "מאי", "יוני", "יולי", "אוגוסט", "ספטמבר", "אוקטובר", "נובמבר", "דצמבר"]


def get_datetime():
    n = datetime.now()
    return {"time": n.strftime("%H:%M"),
            "date": f"יום {HE_DAYS[n.weekday()]}, {n.day} ב{HE_MONTHS[n.month - 1]} {n.year}"}


# ======================================================================= PHONE EXTRAS: screen, files, PC control, routines
MAX_PHONE_FILE = 14 * 1024 * 1024
BLOB_PIECE = 1_800_000                     # ntfy.sh free tier: about 2MB per attachment, so big files go in pieces


def screenshot_jpeg(max_w=1600, quality=70):
    img = _gui().screenshot()
    if img.width > max_w:
        img = img.resize((max_w, round(img.height * max_w / img.width)))
    buf = BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def latest_download():
    folder = KNOWN_FOLDERS["downloads"]
    files = [p for p in folder.iterdir() if p.is_file() and not p.name.startswith(("~", "."))
             and p.suffix.lower() not in (".crdownload", ".tmp", ".part", ".partial", ".ini")]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def resolve_file(what):
    """'latest_download', a full path, or part of a file name."""
    w = (what or "").strip().strip('"')
    if not w or w.lower() in ("latest_download", "last_download", "latest", "last"):
        return latest_download()
    p = Path(os.path.expandvars(os.path.expanduser(w)))
    if p.is_file():
        return p
    for hit in find_files(w, max_results=10).get("results", []):
        if Path(hit).is_file():
            return Path(hit)
    return None


def send_to_phone(what="latest_download"):
    if what in ("screen", "screenshot"):
        PHONE.send_blob(screenshot_jpeg(), f"screen_{datetime.now():%H%M}.jpg", "image/jpeg", "image")
        return {"ok": True, "sent": "צילום מסך"}
    p = resolve_file(what)
    if not p:
        return {"ok": False, "error": "לא מצאתי את הקובץ"}
    if p.stat().st_size > MAX_PHONE_FILE:
        return {"ok": False, "error": f"הקובץ {p.name} גדול מדי (מעל 14MB)"}
    import mimetypes
    mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    PHONE.send_blob(p.read_bytes(), p.name, mime, "image" if mime.startswith("image/") else "file")
    return {"ok": True, "sent": p.name, "note": "the file is now in the iPhone app (the app must be open to receive it)"}


def save_from_phone(data, name):
    folder = KNOWN_FOLDERS["downloads"] / "Jarvis"
    folder.mkdir(parents=True, exist_ok=True)
    name = re.sub(r'[\\/:*?"<>|]+', "_", Path(name or "file").name)[:120] or "file"
    path = folder / name
    i = 1
    while path.exists():
        path = folder / f"{Path(name).stem} ({i}){Path(name).suffix}"
        i += 1
    path.write_bytes(data)
    return path


def close_all_apps():
    """Politely asks every open window to close (like clicking X), so programs can still ask to save."""
    if OS != "Windows":
        return {"ok": False, "error": "only supported on Windows"}
    ps = "Get-Process | Where-Object {$_.MainWindowTitle} | ForEach-Object { \"$($_.Id)|$($_.ProcessName)\" }"
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                       timeout=20, creationflags=_no_window())
    keep = {"explorer", "jarvis", "python", "pythonw", "msedgewebview2", "textinputhost", "applicationframehost",
            "systemsettings", "shellexperiencehost", "searchhost", "startmenuexperiencehost", "remoting_desktop"}
    closed = []
    for line in r.stdout.splitlines():
        pid, _, name = line.strip().partition("|")
        if not pid.isdigit() or int(pid) == os.getpid() or name.lower() in keep:
            continue
        subprocess.run(["taskkill", "/PID", pid], capture_output=True, creationflags=_no_window())
        closed.append(name)
    return {"ok": True, "closed": sorted(set(closed))}


def monitor_off():
    import ctypes
    ctypes.windll.user32.PostMessageW(0xFFFF, 0x0112, 0xF170, 2)      # WM_SYSCOMMAND / SC_MONITORPOWER / off
    return {"ok": True}


PHONE_KEY_NAMES = {"up", "down", "left", "right", "enter", "esc", "space", "backspace", "tab", "delete", "home", "end",
                   "pageup", "pagedown", "ctrl", "alt", "shift", "win", "=", "-", "printscreen", "insert"} | \
    {f"f{i}" for i in range(1, 13)} | set("abcdefghijklmnopqrstuvwxyz0123456789")


def phone_command(cmd):
    """Buttons in the iPhone app. Returns a short Hebrew confirmation."""
    cmd = (cmd or "").strip()
    name, _, arg = cmd.partition(":")
    arg = arg.strip()
    if name == "key":                                   # a key or shortcut pressed on the PC: key:ctrl+w
        parts = [k for k in arg.lower().split("+") if k]
        if not parts or len(parts) > 4 or any(k not in PHONE_KEY_NAMES for k in parts):
            return "מקש לא מוכר."
        step_aside()
        press_keys("+".join(parts))
        return "✓ " + "+".join(parts)
    if name == "vol":
        level = max(0, min(100, int(float(arg or 0))))
        set_volume(level)
        return f"עוצמת הקול: {level}."
    if name == "open":
        if not arg or len(arg) > 200:
            return "לא ברור מה לפתוח."
        ui.action(f"📱 פותחת: {arg}")
        if arg in HE_SITES:
            open_website(HE_SITES[arg])
        elif re.match(r"^https?://", arg):
            open_website(arg)
        else:
            r = open_app(HE_APPS.get(arg, arg))
            if not r.get("ok"):
                return f"לא הצלחתי לפתוח את {arg}."
        return f"פתחתי את {arg}."
    if name == "status":
        i = system_info()
        bat = i.get("battery")
        txt = f"מעבד {i['cpu_percent']:.0f}%, זיכרון {i['ram_percent']:.0f}%, דיסק {i['disk_percent']:.0f}% מלא"
        if bat:
            txt += f", סוללה {bat['percent']:.0f}%" + (" (בטעינה)" if bat.get("plugged_in") else "")
        return txt + f". גילי {VERSION}."
    if name == "stop":
        AGENT_STOP.set()
        try:
            assistant.speaker.stop()
        except Exception:
            pass
        ui.action("📱 עצירה")
        return "עצרתי."
    acts = {
        "lock": (lambda: power("lock"), "נעלתי את המחשב."),
        "sleep": (lambda: power("sleep"), "המחשב נכנס למצב שינה."),
        "shutdown": (lambda: subprocess.Popen("shutdown /s /t 60", shell=True),
                     "המחשב ייכבה בעוד דקה. אפשר לבטל עם הכפתור 'בטל כיבוי'."),
        "restart": (lambda: subprocess.Popen("shutdown /r /t 60", shell=True),
                    "המחשב יופעל מחדש בעוד דקה. אפשר לבטל עם הכפתור 'בטל כיבוי'."),
        "cancel_shutdown": (lambda: power("cancel"), "ביטלתי את הכיבוי."),
        "monitor_off": (monitor_off, "כיביתי את המסך. הוא יידלק כשמזיזים את העכבר."),
        "mute": (lambda: media_control("mute"), "השתקתי / ביטלתי השתקה."),
        "vol_up": (lambda: media_control("volume_up"), "הגברתי."),
        "vol_down": (lambda: media_control("volume_down"), "הנמכתי."),
        "play_pause": (lambda: media_control("play_pause"), "בוצע."),
        "next": (lambda: media_control("next"), "השיר הבא."),
        "previous": (lambda: media_control("previous"), "השיר הקודם."),
        "desktop": (lambda: window_control("show_desktop"), "מציגה את שולחן העבודה."),
        "switch": (lambda: window_control("switch"), "עברתי לחלון הקודם."),
        "close_window": (lambda: window_control("close_window"), "סגרתי את החלון."),
        "minimize": (lambda: window_control("minimize"), "מזערתי את החלון."),
        "maximize": (lambda: window_control("maximize"), "הגדלתי את החלון."),
        "snap_left": (lambda: window_control("snap_left"), "הצמדתי לשמאל."),
        "snap_right": (lambda: window_control("snap_right"), "הצמדתי לימין."),
        "close_all": (close_all_apps, "ביקשתי מכל התוכנות להיסגר."),
        "screenshot": (take_screenshot, "שמרתי צילום מסך בתיקיית התמונות, בתיקייה Jarvis."),
    }
    if cmd not in acts:
        return "פקודה לא מוכרת."
    fn, msg = acts[cmd]
    ui.action("📱 " + msg)
    if cmd in ("switch", "close_window", "minimize", "maximize", "snap_left", "snap_right"):
        step_aside()                                    # act on the user's window, not on Gili's own
    fn()
    return msg


def type_from_phone(text, enter=False):
    """Text written on the iPhone is typed where the cursor is on the PC."""
    text = (text or "")[:5000]
    if not text.strip():
        return "אין טקסט להקליד."
    step_aside()
    type_text(text)
    if enter:
        time.sleep(0.15)
        _gui().press("enter")
    ui.action("📱 הוקלד טקסט מהטלפון")
    return "הקלדתי במחשב."


DEFAULT_ROUTINES = {
    "מצב עבודה": ["פתח chrome", "אתר https://mail.google.com", "תגיד מצב עבודה מוכן"],
    "לילה טוב": ["סגור הכל", "תגיד לילה טוב. המחשב ייכבה בעוד דקה, אפשר לבטל עם תבטל כיבוי", "כבה"],
}
ROUTINE_HELP = ("פתח <תוכנה> | סגור <תוכנה> | סגור הכל | אתר <כתובת> | נגן <שיר> | עוצמה <0-100> | השתק | "
                "נעל | שינה | כבה | המתן <שניות> | תגיד <טקסט>")


def routines():
    r = CFG.get("routines")
    return r if isinstance(r, dict) else dict(DEFAULT_ROUTINES)


def parse_routines(text):
    """Settings box format:  [name] then one step per line."""
    out, cur = {}, None
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.fullmatch(r"\[(.+)\]", line)
        if m:
            cur = m.group(1).strip()
            out[cur] = []
        elif cur:
            out[cur].append(line)
    return {k: v for k, v in out.items() if k and v}


def routines_text():
    return "\n\n".join(f"[{n}]\n" + "\n".join(s) for n, s in routines().items())


def run_step(step):
    """Runs one routine line. Returns text to say (or '')."""
    s = step.strip()
    verb, _, arg = s.partition(" ")
    arg = arg.strip()
    if verb == "תגיד":
        return arg
    if verb in ("פתח", "תפתח"):
        if arg in HE_SITES:
            open_website(HE_SITES[arg])
        elif arg.startswith("http"):
            open_website(arg)
        else:
            open_app(HE_APPS.get(arg, arg))
    elif verb == "אתר":
        open_website(HE_SITES.get(arg, arg))
    elif s in ("סגור הכל", "סגור הכול", "סגור את הכל"):
        close_all_apps()
        time.sleep(3)
    elif verb in ("סגור", "תסגור"):
        close_app(HE_APPS.get(arg, arg))
    elif verb in ("נגן", "תנגן"):
        play_youtube(arg)
    elif verb == "עוצמה":
        set_volume(float(re.sub(r"[^\d.]", "", arg) or 50))
    elif s == "השתק":
        media_control("mute")
    elif s == "נעל":
        power("lock")
    elif s == "שינה":
        power("sleep")
    elif s in ("כבה", "כבה מחשב"):
        if OS == "Windows":
            subprocess.Popen("shutdown /s /t 60", shell=True)
    elif verb == "המתן":
        time.sleep(min(120, float(re.sub(r"[^\d.]", "", arg) or 1)))
    else:
        raise ValueError(f"שלב לא מוכר: {s}")
    return ""


def find_routine(name):
    n = (name or "").strip().strip("\"'").removeprefix("ה") if name else ""
    for k in routines():
        if k == (name or "").strip() or k.removeprefix("ה") == n or k.replace("מצב ", "") == n:
            return k
    return None


def run_routine(name):
    key = find_routine(name)
    if not key:
        return {"ok": False, "error": f"אין שגרה בשם '{name}'", "routines": list(routines())}
    said, errors = [], []
    ui.action(f"שגרה: {key}")
    for step in routines()[key]:
        try:
            t = run_step(step)
            if t:
                said.append(t)
        except Exception as e:
            errors.append(str(e))
            log.warning(f"routine step {step!r}: {e}")
        time.sleep(0.4)
    return {"ok": not errors or bool(said), "routine": key, "say": " ".join(said), "errors": errors}


def save_routine(name, steps):
    name = (name or "").strip()
    steps = [s.strip() for s in (steps or []) if str(s).strip()]
    if not name or not steps:
        return {"ok": False, "error": "צריך שם ושלבים"}
    r = routines()
    r[name] = steps
    CFG["routines"] = r
    save_config()
    return {"ok": True, "routine": name, "steps": steps}


def delete_routine(name):
    key = find_routine(name)
    if not key:
        return {"ok": False, "error": "לא נמצאה שגרה כזו"}
    r = routines()
    r.pop(key)
    CFG["routines"] = r
    save_config()
    return {"ok": True, "deleted": key}


# ======================================================================= CLIPBOARD <-> iPHONE
def copy_to_phone(text=""):
    import pyperclip
    t = (text or "").strip() or (pyperclip.paste() or "")
    if not t.strip():
        return {"ok": False, "error": "אין טקסט בלוח ההעתקה של המחשב"}
    t = t[:200000]
    if len(t.encode("utf-8")) > 1800:                     # too big for one message -> encrypted attachment
        PHONE.send_blob(t.encode("utf-8"), "clipboard.txt", "text/plain", "clip")
    else:
        PHONE.reply({"type": "clip", "text": t})
    return {"ok": True, "chars": len(t), "note": "sent to the iPhone app; the user taps 'העתק' there"}


def paste_from_phone(text):
    import pyperclip
    pyperclip.copy(text)
    ui.action("📋 הועתק מהטלפון")
    return "הועתק במחשב. אפשר להדביק עם Ctrl+V."


# ======================================================================= GOOGLE CALENDAR (secret iCal address, read-only)
def _ics_unfold(text):
    return re.sub(r"\r?\n[ \t]", "", text).splitlines()


def _ics_time(value, params):
    """-> (datetime in local time, all_day)"""
    v = value.strip()
    if "VALUE=DATE" in params or re.fullmatch(r"\d{8}", v):
        return datetime.strptime(v[:8], "%Y%m%d"), True
    if v.endswith("Z"):
        from datetime import timezone
        return datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None), False
    dt = datetime.strptime(v[:15], "%Y%m%dT%H%M%S")
    m = re.search(r"TZID=([^;:]+)", params)
    if m:
        try:
            from zoneinfo import ZoneInfo
            dt = dt.replace(tzinfo=ZoneInfo(m.group(1))).astimezone().replace(tzinfo=None)
        except Exception:
            pass
    return dt, False


_WD = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def _expand(ev, start, end):
    """Occurrences of one event between start and end (handles the common RRULE forms)."""
    from datetime import timedelta
    s0, dur = ev["start"], ev["end"] - ev["start"]
    rule = ev.get("rrule")
    if not rule:
        return [s0] if s0 < end and s0 + dur > start else []
    r = dict(p.split("=", 1) for p in rule.split(";") if "=" in p)
    freq, step = r.get("FREQ", "DAILY"), int(r.get("INTERVAL", 1))
    count = int(r["COUNT"]) if "COUNT" in r else None
    until = _ics_time(r["UNTIL"], "")[0] if "UNTIL" in r else None
    if until and len(r["UNTIL"]) == 8:
        until += timedelta(days=1)
    days = [_WD[d[-2:]] for d in r.get("BYDAY", "").split(",") if d[-2:] in _WD] if freq == "WEEKLY" else []
    out, n, cur, guard = [], 0, s0, 0
    while guard < 5000:
        guard += 1
        if freq == "WEEKLY" and days:
            week0 = cur - timedelta(days=cur.weekday())
            cands = sorted(week0 + timedelta(days=d) for d in days)
            cands = [c.replace(hour=s0.hour, minute=s0.minute, second=s0.second) for c in cands]
        else:
            cands = [cur]
        for c in cands:
            if c < s0:
                continue
            if (until and c > until) or (count is not None and n >= count) or c >= end:
                return out
            n += 1
            if c + dur > start and c not in ev.get("exdates", ()):
                out.append(c)
        if freq == "DAILY":
            cur += timedelta(days=step)
        elif freq == "WEEKLY":
            cur += timedelta(weeks=step)
        elif freq == "MONTHLY":
            mth = cur.month - 1 + step
            try:
                cur = cur.replace(year=cur.year + mth // 12, month=mth % 12 + 1)
            except ValueError:
                cur = cur.replace(day=28, year=cur.year + mth // 12, month=mth % 12 + 1)
        elif freq == "YEARLY":
            cur = cur.replace(year=cur.year + step)
        else:
            return out
    return out


def parse_ics(text):
    events, ev = [], None
    for line in _ics_unfold(text):
        if line == "BEGIN:VEVENT":
            ev = {"exdates": set(), "title": "(ללא כותרת)", "location": ""}
        elif line == "END:VEVENT" and ev is not None:
            if "start" in ev and ev.get("status") != "CANCELLED":
                ev.setdefault("end", ev["start"] + (__import__("datetime").timedelta(days=1) if ev["all_day"]
                                                    else __import__("datetime").timedelta(hours=1)))
                events.append(ev)
            ev = None
        elif ev is not None and ":" in line:
            head, _, val = line.partition(":")
            name, _, params = head.partition(";")
            val_t = val.replace("\\n", " ").replace("\\,", ",").replace("\\;", ";").strip()
            if name == "DTSTART":
                ev["start"], ev["all_day"] = _ics_time(val, params)
            elif name == "DTEND":
                ev["end"] = _ics_time(val, params)[0]
            elif name == "SUMMARY":
                ev["title"] = val_t or ev["title"]
            elif name == "LOCATION":
                ev["location"] = val_t
            elif name == "RRULE":
                ev["rrule"] = val
            elif name == "EXDATE":
                for v in val.split(","):
                    ev["exdates"].add(_ics_time(v, params)[0])
            elif name == "STATUS":
                ev["status"] = val.strip()
            elif name == "UID":
                ev["uid"] = val.strip()
            elif name == "RECURRENCE-ID":
                ev["recurrence_id"] = _ics_time(val, params)[0]
    # moved/edited single occurrences replace the original one
    moved = {(e.get("uid"), e["recurrence_id"]) for e in events if e.get("recurrence_id")}
    for e in events:
        if e.get("rrule"):
            e["exdates"] |= {rid for uid, rid in moved if uid == e.get("uid")}
    return events


class Calendar:
    def __init__(self):
        self.cache, self.cache_t, self.alerted = None, 0, set()

    def configured(self):
        return bool((CFG.get("calendar_ics") or "").strip())

    def events(self, start, end):
        import httpx
        if not self.configured():
            raise RuntimeError("calendar not connected")
        if self.cache is None or time.time() - self.cache_t > 600:
            r = httpx.get(CFG["calendar_ics"].strip(), timeout=30, follow_redirects=True)
            r.raise_for_status()
            if "BEGIN:VCALENDAR" not in r.text[:500]:
                raise RuntimeError("הכתובת לא מחזירה יומן. צריך את 'הכתובת הסודית בפורמט iCal'.")
            self.cache, self.cache_t = parse_ics(r.text), time.time()
        out = []
        for ev in self.cache:
            for occ in _expand(ev, start, end):
                out.append({"title": ev["title"], "start": occ, "end": occ + (ev["end"] - ev["start"]),
                            "all_day": ev["all_day"], "location": ev["location"]})
        return sorted(out, key=lambda e: e["start"])

    def loop(self):
        from datetime import timedelta
        time.sleep(40)
        while True:
            try:
                if self.configured() and CFG.get("calendar_alerts", True):
                    now = datetime.now()
                    lead = float(CFG.get("calendar_alert_minutes", 15))
                    for e in self.events(now, now + timedelta(minutes=lead + 1)):
                        key = (e["title"], e["start"])
                        if e["all_day"] or key in self.alerted or e["start"] < now:
                            continue
                        self.alerted.add(key)
                        mins = max(1, round((e["start"] - now).total_seconds() / 60))
                        where = f" ({e['location']})" if e["location"] else ""
                        notify(f"📅 בעוד {mins} דקות: {e['title']}{where}")
            except Exception as ex:
                log.warning(f"calendar: {ex}")
            time.sleep(60)


CALENDAR = Calendar()


def calendar_events(day="today", days=1):
    from datetime import timedelta
    if not CALENDAR.configured():
        return {"ok": False, "error": "היומן לא מחובר. מחברים בהגדרות ← יומן Google."}
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    d = (day or "today").strip().lower()
    if d in ("today", "היום"):
        start = today
    elif d in ("tomorrow", "מחר"):
        start = today + timedelta(days=1)
    elif d in ("week", "השבוע"):
        start, days = today, 7
    else:
        try:
            start = datetime.strptime(d[:10], "%Y-%m-%d")
        except ValueError:
            start = today
    days = max(1, min(31, int(days or 1)))
    evs = CALENDAR.events(start, start + timedelta(days=days))
    return {"ok": True, "from": start.strftime("%Y-%m-%d"), "days": days, "events": [
        {"title": e["title"], "date": e["start"].strftime("%Y-%m-%d"),
         "time": "כל היום" if e["all_day"] else e["start"].strftime("%H:%M") + "-" + e["end"].strftime("%H:%M"),
         "location": e["location"]} for e in evs]}


def add_calendar_event(title, start, minutes=60, details=""):
    """Opens Google Calendar in Chrome with the event filled in; the user clicks Save."""
    from datetime import timedelta
    from urllib.parse import quote
    s = datetime.fromisoformat(start.strip().replace("Z", ""))
    e = s + timedelta(minutes=int(minutes or 60))
    f = "%Y%m%dT%H%M%S"
    url = (f"https://calendar.google.com/calendar/render?action=TEMPLATE&text={quote(title)}"
           f"&dates={s.strftime(f)}/{e.strftime(f)}&ctz=Asia/Jerusalem&details={quote(details or '')}")
    open_in_chrome(url)
    CALENDAR.cache = None
    return {"ok": True, "note": "Google Calendar opened in Chrome with the event filled in - the user must click 'שמירה' (Save)"}


# ======================================================================= APP STORE CONNECT ("יומן הגנן" stats)
ASC_CACHE_FILE = DATA_DIR / "appstore_cache.json"


def _asc_token():
    import base64
    from cryptography.hazmat.primitives import serialization, hashes
    from cryptography.hazmat.primitives.asymmetric import ec, utils
    key = serialization.load_pem_private_key(CFG["asc_key"].encode(), None)
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=")
    now = int(time.time())
    h = b64(json.dumps({"alg": "ES256", "kid": CFG["asc_key_id"].strip(), "typ": "JWT"}).encode())
    p = b64(json.dumps({"iss": CFG["asc_issuer"].strip(), "iat": now, "exp": now + 1100,
                        "aud": "appstoreconnect-v1"}).encode())
    r, s = utils.decode_dss_signature(key.sign(h + b"." + p, ec.ECDSA(hashes.SHA256())))
    return (h + b"." + p + b"." + b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))).decode()


class AppStore:
    API = "https://api.appstoreconnect.apple.com"

    def __init__(self):
        try:
            self.cache = json.loads(ASC_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            self.cache = {}
        self.cache.setdefault("days", {})
        self.cache.setdefault("seen_reviews", [])

    def configured(self):
        return all((CFG.get(k) or "").strip() for k in ("asc_issuer", "asc_key_id", "asc_key", "asc_vendor"))

    def _save(self):
        try:
            ASC_CACHE_FILE.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _get(self, path, params=None):
        import httpx
        r = httpx.get(self.API + path, params=params, timeout=40,
                      headers={"Authorization": "Bearer " + _asc_token()})
        if r.status_code == 401:
            raise RuntimeError("אפל דחתה את המפתח. בדוק את Issuer ID, Key ID והקובץ .p8.")
        if r.status_code == 403:
            raise PermissionError("למפתח אין הרשאה לזה (צריך תפקיד Sales או Admin).")
        return r

    def _report(self, date, kind="SALES"):
        """One day's report rows (cached: past days never change)."""
        import gzip, csv, io
        key = f"{kind}:{date}"
        if key in self.cache["days"]:
            return self.cache["days"][key]
        params = {"filter[frequency]": "DAILY", "filter[reportType]": kind, "filter[reportSubType]": "SUMMARY",
                  "filter[vendorNumber]": CFG["asc_vendor"].strip(), "filter[reportDate]": date,
                  "filter[version]": "1_1" if kind == "SALES" else "1_3"}
        r = self._get("/v1/salesReports", params)
        if r.status_code == 404:                   # not ready yet / no sales that day
            body = r.text
            if "no sales" in body.lower() or "There were no sales" in body:
                self.cache["days"][key] = []
                self._save()
                return []
            return None
        r.raise_for_status()
        raw = r.content
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8", "replace")), delimiter="\t"))
        keep = ("Units", "Product Type Identifier", "Developer Proceeds", "Currency of Proceeds", "Title",
                "Country Code", "SKU", "Apple Identifier", "Parent Identifier")
        rows = [{k: row.get(k, "") for k in keep} if kind == "SALES" else row for row in rows]
        if date < datetime.now().strftime("%Y-%m-%d"):
            self.cache["days"][key] = rows
            self._save()
        return rows

    def _match(self, row):
        name = (CFG.get("asc_app_name") or "").strip()
        return not name or name in row.get("Title", "") or name in row.get("App Name", "")

    def stats(self, days=1):
        from datetime import timedelta
        days = max(1, min(30, int(days or 1)))
        out = {"downloads": 0, "purchases": 0, "updates": 0, "proceeds": {}, "countries": {}, "dates": []}
        d, got = datetime.now() - timedelta(days=1), 0
        for _ in range(days + 3):                  # the newest daily report is usually ~1 day behind
            if got >= days:
                break
            ds = d.strftime("%Y-%m-%d")
            rows = self._report(ds)
            d -= timedelta(days=1)
            if rows is None:
                continue
            got += 1
            out["dates"].append(ds)
            for row in rows:
                if not self._match(row):
                    continue
                units = int(float(row.get("Units") or 0))
                t = row.get("Product Type Identifier", "")
                if t.startswith(("1", "F1")):
                    out["downloads"] += units
                elif t.startswith(("IA", "FI")):
                    out["purchases"] += units
                elif t.startswith(("7", "F7")):
                    out["updates"] += units
                money = units * float(row.get("Developer Proceeds") or 0)
                if money:
                    cur = row.get("Currency of Proceeds") or "?"
                    out["proceeds"][cur] = round(out["proceeds"].get(cur, 0) + money, 2)
                if t.startswith(("1", "F1", "IA", "FI")) and units:
                    cc = row.get("Country Code") or "?"
                    out["countries"][cc] = out["countries"].get(cc, 0) + units
        if out["dates"]:
            out["period"] = f"{out['dates'][-1]} עד {out['dates'][0]}"
        else:
            out["note"] = "אפל עוד לא פרסמה דוחות לימים האלה"
        try:                                       # active subscribers (latest subscription report)
            d = datetime.now() - timedelta(days=1)
            for _ in range(4):
                rows = self._report(d.strftime("%Y-%m-%d"), "SUBSCRIPTION")
                d -= timedelta(days=1)
                if rows is None:
                    continue
                active = 0
                for row in rows:
                    if not self._match(row):
                        continue
                    for k, v in row.items():
                        if k and k.startswith("Active") and "Subscriptions" in k:
                            active += int(float(v or 0))
                out["active_subscribers"] = active
                break
        except Exception as e:
            log.info(f"subscription report: {e}")
        return out

    def app_id(self):
        if self.cache.get("app_id"):
            return self.cache["app_id"]
        r = self._get("/v1/apps", {"limit": 50, "fields[apps]": "name,bundleId"})
        r.raise_for_status()
        apps = r.json().get("data", [])
        name = (CFG.get("asc_app_name") or "").strip()
        pick = next((a for a in apps if name and name in a["attributes"].get("name", "")), apps[0] if apps else None)
        if not pick:
            raise RuntimeError("לא נמצאו אפליקציות בחשבון")
        self.cache["app_id"] = pick["id"]
        self._save()
        return pick["id"]

    def reviews(self, limit=5):
        r = self._get(f"/v1/apps/{self.app_id()}/customerReviews", {"sort": "-createdDate", "limit": max(1, min(50, limit))})
        r.raise_for_status()
        return [{"id": x["id"], "rating": x["attributes"].get("rating"), "title": x["attributes"].get("title", ""),
                 "text": (x["attributes"].get("body") or "")[:400], "date": (x["attributes"].get("createdDate") or "")[:10],
                 "country": x["attributes"].get("territory", "")} for x in r.json().get("data", [])]

    def loop(self):
        time.sleep(90)
        while True:
            try:
                if self.configured() and CFG.get("asc_alerts", True):
                    # yesterday's numbers, once, when Apple publishes them
                    from datetime import timedelta
                    y = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
                    if self.cache.get("reported") != y and self._report(y) is not None:
                        s = self.stats(1)
                        if s.get("dates") and s["dates"][0] == y:
                            money = ", ".join(f"{v:g} {k}" for k, v in s["proceeds"].items()) or "0"
                            notify(f"🌱 יומן הגנן אתמול: {s['downloads']} הורדות, {s['purchases']} רכישות, הכנסה {money}")
                            self.cache["reported"] = y
                            self._save()
                    try:                           # new reviews
                        seen = set(self.cache["seen_reviews"])
                        revs = self.reviews(10)
                        if self.cache.get("reviews_seeded"):
                            for rv in reversed(revs):
                                if rv["id"] not in seen:
                                    notify(f"⭐ ביקורת חדשה ליומן הגנן ({rv['rating']}/5): {rv['title']} — {rv['text'][:120]}")
                        self.cache["seen_reviews"] = (list(seen | {r['id'] for r in revs}))[-500:]
                        self.cache["reviews_seeded"] = True
                        self._save()
                    except PermissionError:
                        pass
            except Exception as e:
                log.warning(f"app store: {e}")
            time.sleep(3 * 3600)


APPSTORE = AppStore()


def app_store_stats(days=1, reviews=True):
    if not APPSTORE.configured():
        return {"ok": False, "error": "החיבור לאפל לא מוגדר. מחברים בהגדרות ← יומן הגנן."}
    out = {"ok": True, "app": CFG.get("asc_app_name") or "", **APPSTORE.stats(days)}
    if reviews:
        try:
            revs = APPSTORE.reviews(5)
            out["recent_reviews"] = revs
            if revs:
                out["recent_avg_rating"] = round(sum(r["rating"] or 0 for r in revs) / len(revs), 1)
        except PermissionError:
            out["reviews"] = "no permission for reviews with this key"
        except Exception as e:
            out["reviews"] = f"error: {e}"
    return out


def morning_brief():
    out = {"datetime": get_datetime()}
    try:
        out["weather"] = get_weather("")
    except Exception as e:
        out["weather"] = {"error": str(e)}
    today = datetime.now().strftime("%Y-%m-%d")
    out["reminders_today"] = [{"text": i["text"], "time": i["due"][11:16]}
                              for i in sorted((REMINDERS.items if REMINDERS else []), key=lambda x: x["due"]) if i["due"].startswith(today)]
    if MAIL.configured():
        try:
            mails = MAIL.fetch_unread(limit=8, days=1)
            out["unread_mail"] = [{"from": m["from"], "subject": m["subject"], "important": m["gmail_important"]}
                                  for m in mails]
        except Exception as e:
            out["unread_mail"] = {"error": str(e)}
    else:
        out["unread_mail"] = "email not connected"
    if CALENDAR.configured():
        try:
            out["calendar_today"] = calendar_events("today")["events"]
        except Exception as e:
            out["calendar_today"] = {"error": str(e)}
    if APPSTORE.configured():
        try:
            st = APPSTORE.stats(1)
            out["yomen_hagannan_yesterday"] = {k: st.get(k) for k in ("downloads", "purchases", "proceeds", "active_subscribers", "period")}
        except Exception as e:
            out["yomen_hagannan_yesterday"] = {"error": str(e)}
    return out


# ======================================================================= NOTIFICATIONS (Windows + iPhone app)
TRAY = None


def notify(text, title="G.I.L.I"):
    """Silent notification: a Windows notification near the clock, and the iPhone app if it is open."""
    try:
        if TRAY:
            TRAY.notify(text[:250], title)
    except Exception as e:
        log.warning(f"tray notify: {e}")
    try:
        PHONE.push({"type": "notify", "text": text[:1000]})
    except Exception as e:
        log.warning(f"phone notify: {e}")
    ui.toast(text[:180])
    return {"ok": True}


def idle_seconds():
    """How long since the last keyboard/mouse input (Windows)."""
    if OS != "Windows":
        return 0
    import ctypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
    li = LASTINPUTINFO()
    li.cbSize = ctypes.sizeof(li)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li)):
        return 0
    return (ctypes.windll.kernel32.GetTickCount() - li.dwTime) / 1000.0


def user_away():
    return idle_seconds() > float(CFG.get("away_minutes", 5)) * 60


class Reminders:
    """Reminders the user asked for. Saved to disk so they survive a restart."""
    def __init__(self, events):
        self.events = events
        self.lock = threading.Lock()
        try:
            self.items = json.loads(REMINDERS_FILE.read_text(encoding="utf-8"))
        except Exception:
            self.items = []
        threading.Thread(target=self._loop, daemon=True).start()

    def _save(self):
        try:
            REMINDERS_FILE.write_text(json.dumps(self.items, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:
            log.error(f"reminders save: {e}")

    REPEATS = {"daily": "כל יום", "weekdays": "כל יום חול", "weekly": "כל שבוע"}

    @staticmethod
    def _next(due, repeat):
        """The next time a repeating item is due (weekdays = Sunday to Thursday)."""
        from datetime import timedelta
        d = due + timedelta(days=7 if repeat == "weekly" else 1)
        while repeat == "weekdays" and d.weekday() in (4, 5):
            d += timedelta(days=1)
        return d

    def add(self, text, minutes=None, at=None, whatsapp=False, date=None, repeat=None, task=None):
        from datetime import timedelta                      # 'whatsapp' kept only so old saved items load
        now = datetime.now()
        repeat = repeat if repeat in self.REPEATS else None
        day = None
        if date:
            d = str(date).strip().lower()
            try:
                day = (now.date() if d == "today" else now.date() + timedelta(days=1) if d == "tomorrow"
                       else datetime.strptime(d[:10], "%Y-%m-%d").date())
            except Exception:
                return {"ok": False, "error": "תאריך לא תקין, צריך YYYY-MM-DD"}
        if at or day:
            try:
                hh, mm = [int(x) for x in str(at or "09:00").strip().split(":")[:2]]
                due = datetime.combine(day or now.date(), datetime.min.time()).replace(hour=hh, minute=mm)
            except Exception:
                return {"ok": False, "error": "שעה לא תקינה, צריך HH:MM"}
            if due <= now:
                if day and not repeat:
                    return {"ok": False, "error": "הזמן הזה כבר עבר"}
                due += timedelta(days=1)
            while repeat == "weekdays" and due.weekday() in (4, 5):
                due += timedelta(days=1)
        elif minutes is not None:
            due = now + timedelta(minutes=float(minutes))
        else:
            return {"ok": False, "error": "צריך זמן (minutes, at או date)"}
        item = {"id": int(time.time() * 1000), "text": text, "due": due.isoformat(timespec="seconds")}
        if repeat:
            item["repeat"] = repeat
        if task:
            item["task"] = task
        with self.lock:
            self.items.append(item)
            self._save()
        out = {"ok": True, "due": due.strftime("%H:%M"), "date": due.strftime("%d/%m"),
               "day": "יום " + HE_DAYS[due.weekday()]}
        if repeat:
            out["repeats"] = self.REPEATS[repeat]
        return out

    def list(self):
        return {"reminders": [dict({"text": i["text"], "due": i["due"][11:16], "date": i["due"][8:10] + "/" + i["due"][5:7]},
                                   **({"repeats": self.REPEATS.get(i["repeat"], "")} if i.get("repeat") else {}),
                                   **({"kind": "scheduled task for Gili"} if i.get("task") else {}))
                              for i in sorted(self.items, key=lambda x: x["due"])]}

    def cancel(self, contains=""):
        with self.lock:
            before = len(self.items)
            self.items = [i for i in self.items
                          if contains and contains not in i["text"] and contains not in (i.get("task") or "")]
            self._save()
        return {"ok": True, "cancelled": before - len(self.items)}

    def _loop(self):
        while True:
            time.sleep(10)
            now_dt = datetime.now()
            now = now_dt.isoformat(timespec="seconds")
            with self.lock:
                due = [i for i in self.items if i["due"] <= now]
                if due:
                    keep = [i for i in self.items if i["due"] > now]
                    for i in due:
                        if i.get("repeat") in self.REPEATS:            # comes back; if the PC was off, skip missed days
                            nxt = self._next(datetime.fromisoformat(i["due"]), i["repeat"])
                            while nxt <= now_dt:
                                nxt = self._next(nxt, i["repeat"])
                            keep.append(dict(i, due=nxt.isoformat(timespec="seconds")))
                    self.items = keep
                    self._save()
            for i in due:
                if i.get("task"):
                    self.events.put({"task": i["task"], "label": i["text"]})
                else:
                    self.events.put({"text": f"{CFG['user_name']}, תזכורת: {i['text']}", "notify": f"⏰ תזכורת: {i['text']}"})


# ======================================================================= LISTS (shopping, to-do, anything)
LISTS_FILE = DATA_DIR / "lists.json"


class Lists:
    def __init__(self):
        self.lock = threading.Lock()
        try:
            raw = json.loads(LISTS_FILE.read_text(encoding="utf-8"))
            self.data = {str(k): [str(x) for x in v] for k, v in raw.items() if isinstance(v, list)}
        except Exception:
            self.data = {}

    def key(self, name):
        n = " ".join((name or "").split())
        n = re.sub(r"^(רשימת|רשימה של|רשימה)\s+", "", n).strip()
        if n.startswith("ה") and len(n) > 3 and n not in self.data:     # "הקניות" and "קניות" are the same list
            if n[1:] in self.data or n[1:] in ("קניות", "משימות", "מטלות", "רעיונות", "סופר"):
                n = n[1:]
        return n or "קניות"

    def _save(self):
        tmp = LISTS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, LISTS_FILE)

    def edit(self, name, add=None, remove=None, clear=False):
        k = self.key(name)
        with self.lock:
            items = list(self.data.get(k, []))
            removed = []
            if clear:
                removed, items = items, []
            for r in remove or []:
                r = str(r).strip()
                hit = [x for x in items if r and (r == x or r in x or x in r)]
                removed += hit
                items = [x for x in items if x not in hit]
            added = []
            for x in add or []:
                x = " ".join(str(x).split())[:120]
                if x and x not in items:
                    items.append(x)
                    added.append(x)
            if items:
                self.data[k] = items[:200]
            else:
                self.data.pop(k, None)
            self._save()
        return {"ok": True, "list": k, "added": added, "removed": removed, "items": items}

    def show(self, name=""):
        if not (name or "").strip():
            return {"ok": True, "lists": {k: v for k, v in self.data.items()} or "no lists yet"}
        k = self.key(name)
        return {"ok": True, "list": k, "items": self.data.get(k, [])}


LISTS = Lists()


# ======================================================================= SELECTED TEXT + DOCUMENTS
def get_selection():
    """The text the user has selected (marked) in whatever window is in front."""
    import pyperclip
    step_aside()
    try:
        old = pyperclip.paste()
    except Exception:
        old = ""
    pyperclip.copy("")
    _gui().hotkey("ctrl", "c")
    sel = ""
    for _ in range(8):
        time.sleep(0.1)
        sel = pyperclip.paste() or ""
        if sel:
            break
    try:
        pyperclip.copy(old)                            # leave the user's clipboard as it was
    except Exception:
        pass
    if not sel.strip():
        return {"ok": False, "error": "no text is selected - ask the user to mark the text first and try again"}
    return {"ok": True, "text": sel[:3400], "cut": len(sel) > 3400, "chars": len(sel)}


def replace_selection(text):
    step_aside()
    type_text(text)                                    # pasting over a selection replaces it
    return {"ok": True, "note": "the selected text was replaced; the user can press ctrl+z to undo"}


def _docx_text(path):
    import zipfile, html as _html
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    paras = []
    for para in re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.S):
        t = "".join(re.findall(r"<w:t(?: [^>]*)?>(.*?)</w:t>", para, flags=re.S))
        if t.strip():
            paras.append(_html.unescape(t))
    return "\n".join(paras)


def _xlsx_text(path):
    import zipfile, html as _html
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        shared = []
        if "xl/sharedStrings.xml" in names:
            sx = z.read("xl/sharedStrings.xml").decode("utf-8", errors="replace")
            shared = [_html.unescape("".join(re.findall(r"<t(?: [^>]*)?>(.*?)</t>", si, flags=re.S)))
                      for si in re.findall(r"<si>.*?</si>", sx, flags=re.S)]
        rows = []
        for sheet in sorted(n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))[:3]:
            x = z.read(sheet).decode("utf-8", errors="replace")
            for row in re.findall(r"<row[ >].*?</row>", x, flags=re.S):
                cells = []
                for attrs, val in re.findall(r"<c ([^>]*)>.*?<v>(.*?)</v>", row, flags=re.S):
                    cells.append(shared[int(val)] if 't="s"' in attrs and val.isdigit() and int(val) < len(shared) else val)
                if cells:
                    rows.append(" | ".join(cells))
    return "\n".join(rows)


DOC_PART = 3300


def read_document(what="latest_download", part=1):
    """Text of a file on the PC (PDF, Word, Excel, text) so Gili can sum it up or answer questions about it."""
    p = resolve_file(what)
    if not p:
        return {"ok": False, "error": "לא מצאתי את הקובץ"}
    ext = p.suffix.lower()
    if p.stat().st_size > 60 * 1024 * 1024:
        return {"ok": False, "error": "הקובץ גדול מדי"}
    pages = None
    if ext == ".pdf":
        from pypdf import PdfReader
        r = PdfReader(str(p))
        pages = len(r.pages)
        text = "\n".join((pg.extract_text() or "") for pg in r.pages[:80])
        if len(text.strip()) < 30:
            return {"ok": False, "file": p.name, "error": "this PDF is a scan (pictures, no text). Open it and use look_at_screen instead."}
    elif ext == ".docx":
        text = _docx_text(p)
    elif ext == ".xlsx":
        text = _xlsx_text(p)
    elif ext in (".txt", ".md", ".csv", ".json", ".log", ".py", ".js", ".html", ".htm", ".xml", ".ini", ".srt", ".rtf", ""):
        raw = p.read_bytes()[:2_000_000]
        encs = ("utf-16",) if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig", "cp1255", "latin-1")
        for enc in encs + ("latin-1",):
            try:
                text = raw.decode(enc)
                break
            except Exception:
                continue
        if ext in (".html", ".htm"):
            import html as _html
            text = _html.unescape(re.sub(r"(?is)<(script|style).*?</\1>|<[^>]+>", " ", text))
    else:
        return {"ok": False, "file": p.name, "error": f"I can't read {ext} files (only PDF, Word .docx, Excel .xlsx and text files)"}
    text = re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()
    parts = max(1, -(-len(text) // DOC_PART))
    part = max(1, min(parts, int(part or 1)))
    out = {"ok": True, "file": p.name, "part": part, "of": parts, "text": text[(part - 1) * DOC_PART: part * DOC_PART]}
    if pages:
        out["pages"] = pages
    if parts > part:
        out["note"] = f"this is part {part} of {parts}; call again with part={part + 1} to keep reading"
    return out


# ======================================================================= EMAIL (IMAP)
def _imap_host(addr):
    if CFG.get("mail_imap_host"):
        return CFG["mail_imap_host"].strip()
    dom = addr.split("@")[-1].lower()
    return {"gmail.com": "imap.gmail.com", "googlemail.com": "imap.gmail.com",
            "yahoo.com": "imap.mail.yahoo.com", "icloud.com": "imap.mail.me.com", "me.com": "imap.mail.me.com",
            "outlook.com": "outlook.office365.com", "hotmail.com": "outlook.office365.com",
            "live.com": "outlook.office365.com"}.get(dom, "imap." + dom)


def _decode(h):
    from email.header import decode_header, make_header
    try:
        return str(make_header(decode_header(h or "")))
    except Exception:
        return h or ""


def _mail_text(msg):
    import re, html as _html
    plain, htm = "", ""
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        if part.get_content_maintype() == "multipart" or part.get("Content-Disposition", "").startswith("attachment"):
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            txt = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        if part.get_content_type() == "text/plain" and not plain:
            plain = txt
        elif part.get_content_type() == "text/html" and not htm:
            htm = txt
    if not plain and htm:
        plain = _html.unescape(re.sub(r"(?is)<(script|style).*?</\1>|<[^>]+>", " ", htm))
    return re.sub(r"\s+", " ", plain).strip()[:1500]


class Mail:
    """Reads the inbox (read-only) and notifies about important new emails (Windows + iPhone app)."""
    def __init__(self):
        try:
            self.seen = set(json.loads(MAIL_STATE_FILE.read_text(encoding="utf-8")).get("seen", []))
            self.seeded = True
        except Exception:
            self.seen, self.seeded = set(), False
        self.lock = threading.Lock()

    def configured(self):
        return bool(CFG.get("mail_address") and CFG.get("mail_app_password"))

    def _save(self):
        MAIL_STATE_FILE.write_text(json.dumps({"seen": sorted(self.seen)[-3000:]}), encoding="utf-8")

    def _connect(self):
        import imaplib
        addr = CFG["mail_address"].strip()
        m = imaplib.IMAP4_SSL(_imap_host(addr), timeout=30)
        m.login(addr, CFG["mail_app_password"].replace(" ", "").strip())
        m.select("INBOX", readonly=True)
        return m

    def fetch_unread(self, limit=15, days=2):
        """Unread inbox mail (Gmail: the Primary tab only). Returns newest first."""
        import email
        m = self._connect()
        try:
            gmail = "gmail" in _imap_host(CFG["mail_address"])
            if gmail:
                typ, data = m.uid("search", None, "X-GM-RAW", f'"in:inbox is:unread category:primary newer_than:{days}d"')
            else:
                since = (datetime.now() - __import__("datetime").timedelta(days=days)).strftime("%d-%b-%Y")
                typ, data = m.uid("search", None, "UNSEEN", "SINCE", since)
            uids = (data[0] or b"").split()[-limit:]
            out = []
            for uid in reversed(uids):
                parts = "(RFC822.SIZE BODY.PEEK[HEADER])"
                typ, d = m.uid("fetch", uid, parts)
                size = 0
                try:
                    import re as _re
                    size = int(_re.search(rb"RFC822\.SIZE (\d+)", d[0][0]).group(1))
                except Exception:
                    pass
                spec = "(BODY.PEEK[])" if size < 1_500_000 else "(BODY.PEEK[HEADER])"
                typ, d = m.uid("fetch", uid, spec + (" X-GM-LABELS" if gmail else ""))
                raw = next((x[1] for x in d if isinstance(x, tuple)), b"")
                meta = b" ".join(x[0] for x in d if isinstance(x, tuple))
                msg = email.message_from_bytes(raw)
                out.append({"uid": uid.decode(), "from": _decode(msg.get("From")), "subject": _decode(msg.get("Subject")),
                            "date": msg.get("Date", ""), "text": _mail_text(msg),
                            "gmail_important": b"\\Important" in meta})
            return out
        finally:
            try:
                m.logout()
            except Exception:
                pass

    def summary_for_voice(self, limit=8):
        try:
            mails = self.fetch_unread(limit=limit)
        except Exception as e:
            return {"ok": False, "error": f"לא הצלחתי להתחבר למייל: {e}"}
        return {"ok": True, "unread": [{"from": m["from"], "subject": m["subject"], "preview": m["text"][:200]} for m in mails]}

    def classify(self, mail):
        """Ask the brain: is this worth a notification? Returns (important, hebrew_summary)."""
        prompt = (
            "You filter someone's new email and decide if it deserves an immediate notification.\n"
            "IMPORTANT: personal messages from real people expecting an answer, customers or business contacts, "
            "bills/payments due or failed, bank or account security alerts, government, appointments or deliveries "
            "happening soon, anything urgent.\n"
            "NOT important: newsletters, marketing, promotions, social-media notifications, automatic receipts that "
            "need no action, spam.\n"
            'Answer ONLY with JSON: {"important": true/false, "summary": "<one short Hebrew sentence: who and what>"}\n\n'
            f"From: {mail['from']}\nSubject: {mail['subject']}\nGmail marked important: {mail['gmail_important']}\n"
            f"Body: {mail['text'][:1200]}")
        answer = ""
        try:
            if CFG.get("groq_api_key"):
                import httpx
                for model in ("openai/gpt-oss-120b", "llama-3.3-70b-versatile"):
                    body = {"model": model, "messages": [{"role": "user", "content": prompt}],
                            "max_completion_tokens": 300, "temperature": 0,
                            "response_format": {"type": "json_object"}}
                    if model.startswith("openai/"):
                        body["reasoning_effort"] = "low"
                    r = httpx.post(GroqBrain.URL, headers={"Authorization": f"Bearer {CFG['groq_api_key']}"},
                                   json=body, timeout=40)
                    if r.status_code == 200:
                        answer = r.json()["choices"][0]["message"]["content"]
                        break
            elif CFG.get("anthropic_api_key"):
                import anthropic
                r = anthropic.Anthropic(api_key=CFG["anthropic_api_key"]).messages.create(
                    model=CFG["claude_model"], max_tokens=800, messages=[{"role": "user", "content": prompt}])
                answer = " ".join(b.text for b in r.content if b.type == "text")
        except Exception as e:
            log.warning(f"mail classify failed: {e}")
        try:
            import re as _re
            d = json.loads(_re.search(r"\{.*\}", answer, _re.S).group(0))
            return bool(d.get("important")), str(d.get("summary") or mail["subject"])
        except Exception:   # no brain available: trust Gmail's own "important" marker
            return mail["gmail_important"], f"{mail['from']}: {mail['subject']}"

    def check(self):
        """One polling round. Returns how many alerts were sent."""
        if not self.configured():
            return 0
        with self.lock:
            mails = self.fetch_unread(limit=20)
            new = [m for m in mails if m["uid"] not in self.seen]
            for m in new:
                self.seen.add(m["uid"])
            if not self.seeded:              # first run: don't alert about the old backlog
                self.seeded = True
                self._save()
                return 0
            self._save()
        sent = 0
        for m in reversed(new):              # oldest first
            important, summary = self.classify(m)
            log.info(f"mail {'IMPORTANT' if important else 'skip'}: {m['from']} | {m['subject']}")
            if important and CFG.get("mail_alerts", True):
                sender = m["from"].split("<")[0].strip().strip('"') or m["from"]
                notify(f"📧 מייל חשוב מ־{sender}: {summary}")
                sent += 1
        return sent

    def loop(self):
        time.sleep(20)
        while True:
            try:
                self.check()
            except Exception as e:
                log.warning(f"mail check: {e}")
            time.sleep(max(1.0, float(CFG.get("mail_check_minutes", 3))) * 60)


MAIL = Mail()


# ======================================================================= iPHONE LINK (encrypted, through ntfy.sh)
PHONE_APP_URL = "https://mrsigal.github.io/jarvis/"


class PhoneLink:
    """The iPhone app talks to this PC through ntfy.sh topics. Everything is AES-GCM encrypted with a key
    derived from the pairing secret, so ntfy only ever sees random bytes; the topic name is a hash of it."""
    def __init__(self):
        self.answer = None          # set by the assistant: fn(text) -> reply text
        self.voice = None           # set by the assistant: fn(text) -> audio bytes (Jarvis's own voice)
        self._load()

    def _load(self):
        import hashlib
        sec = CFG["phone_secret"]
        self.topic = "jarvis-" + hashlib.sha256(sec.encode()).hexdigest()[:24]
        self.key = hashlib.sha256(("jarvis-key:" + sec).encode()).digest()

    def pair_url(self):
        return f"{PHONE_APP_URL}#k={CFG['phone_secret']}"

    def qr_data_uri(self):
        import qrcode, base64
        img = qrcode.make(self.pair_url(), box_size=8, border=2)
        buf = BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

    def new_secret(self):
        import secrets
        CFG["phone_secret"] = secrets.token_urlsafe(18)
        save_config()
        self._load()
        self.restart = True

    def seal(self, obj):
        import base64, os as _os
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        iv = _os.urandom(12)
        return base64.b64encode(iv + AESGCM(self.key).encrypt(iv, json.dumps(obj, ensure_ascii=False).encode(), None)).decode()

    def open(self, s):
        import base64
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        raw = base64.b64decode(s)
        return json.loads(AESGCM(self.key).decrypt(raw[:12], raw[12:], None))

    def reply(self, obj):
        import httpx
        obj.setdefault("ts", time.time())
        httpx.post(f"https://ntfy.sh/{self.topic}-r", content=self.seal(obj), timeout=20)

    def send_blob(self, data, name, mime, kind="file", extra=None):
        """Big things (screenshots, files) go as an encrypted ntfy attachment; the app gets a small sealed pointer."""
        import httpx, os as _os
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        urls = []
        for i in range(0, max(1, len(data)), BLOB_PIECE):    # the free relay takes ~2MB per attachment
            iv = _os.urandom(12)
            r = httpx.put(f"https://ntfy.sh/{self.topic}-f",
                          content=iv + AESGCM(self.key).encrypt(iv, data[i:i + BLOB_PIECE], None),
                          headers={"Filename": "blob.bin"}, timeout=120)
            if r.status_code == 413:
                raise ValueError("השרת דחה קובץ גדול מדי")
            if r.status_code == 429:
                raise ValueError("הגעת למגבלה היומית של שליחת קבצים (נסה שוב מחר)")
            r.raise_for_status()
            urls.append(r.json()["attachment"]["url"])
        self.reply(dict({"type": "file", "kind": kind, "name": name, "mime": mime, "urls": urls, "url": urls[0],
                         "size": len(data)}, **(extra or {})))

    def fetch_blob(self, urls):
        import httpx
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        urls = urls if isinstance(urls, list) else [urls]
        if not urls or len(urls) > MAX_PHONE_FILE // BLOB_PIECE + 2:
            raise ValueError("bad file")
        out = b""
        for url in urls:
            if not re.match(r"^https://ntfy\.sh/file/[\w.-]+$", url or ""):
                raise ValueError("bad file address")
            r = httpx.get(url, timeout=120, follow_redirects=True)
            r.raise_for_status()
            out += AESGCM(self.key).decrypt(r.content[:12], r.content[12:], None)
            if len(out) > MAX_PHONE_FILE:
                raise ValueError("file too big")
        return out

    def send_voice(self, rid, text):
        """Jarvis's real voice for the phone: all sentences are made at the same time, the first is sent first."""
        from concurrent.futures import ThreadPoolExecutor
        parts = [text.strip()] if Speaker.whole_answer() else Speaker.split_for_speech(text)
        try:
            with ThreadPoolExecutor(max_workers=3) as ex:
                jobs = [ex.submit(self.voice, p) for p in parts]
                for n, job in enumerate(jobs):
                    audio = job.result(timeout=60)
                    wav = audio[:4] == b"RIFF"
                    self.send_blob(audio, "voice.wav" if wav else "voice.mp3", "audio/wav" if wav else "audio/mpeg",
                                   "voice", {"id": rid, "part": n, "last": n == len(jobs) - 1})
        except Exception as e:
            log.warning(f"phone voice: {e}")
            self.reply({"type": "novoice", "id": rid})

    def push(self, obj):
        """Notification to the iPhone app (shown if the app is open)."""
        if CFG.get("phone_link", True):
            threading.Thread(target=lambda: self._safe(self.reply, obj), daemon=True).start()

    @staticmethod
    def _safe(fn, *a):
        try:
            fn(*a)
        except Exception as e:
            log.warning(f"phone push: {e}")

    def handle(self, m):
        now = time.time()
        ts = float(m.get("ts", 0) or 0)
        if not (now - 120 <= ts <= now + 60):
            return                                   # too old or from the future: ignore
        key = f"{m.get('type')}:{m.get('id')}:{ts}"
        seen = self.__dict__.setdefault("_seen", {})
        if key in seen:
            return                                   # the same encrypted message sent again (replay)
        seen[key] = now
        for k in [k for k, t in seen.items() if now - t > 300]:
            del seen[k]
        if m.get("type") == "ping":
            self.reply({"type": "pong", "version": VERSION})
        elif m.get("type") == "ask" and self.answer:
            text = (m.get("text") or "").strip()[:1000]
            try:
                ans = self.answer(text)
            except Exception as e:
                log.exception("phone ask")
                ans = f"שגיאה: {e}"
            self.reply({"type": "answer", "id": m.get("id"), "text": (ans or "")[:1200]})
            if m.get("voice") and self.voice and ans:
                self.send_voice(m.get("id"), ans[:1200])
        elif m.get("type") == "screen":
            try:
                ui.action("📱 צילום מסך לטלפון")
                self.send_blob(screenshot_jpeg(), f"screen_{datetime.now():%H%M}.jpg", "image/jpeg", "image")
            except Exception as e:
                log.exception("phone screen")
                self.reply({"type": "answer", "id": m.get("id"), "text": f"לא הצלחתי לצלם את המסך: {e}"})
        elif m.get("type") == "cmd":
            try:
                text = phone_command(str(m.get("cmd", "")))
            except Exception as e:
                text = f"שגיאה: {e}"
            self.reply({"type": "answer", "id": m.get("id"), "text": text, "quiet": True})
        elif m.get("type") == "type":
            try:
                text = type_from_phone(str(m.get("text", "")), bool(m.get("enter")))
            except Exception as e:
                text = f"שגיאה: {e}"
            self.reply({"type": "answer", "id": m.get("id"), "text": text, "quiet": True})
        elif m.get("type") == "intro_info":
            self.reply(dict({"type": "intro_info"}, **intro_info()))
        elif m.get("type") == "intro_get":
            info = intro_info()
            if info["mode"] == "file":
                try:
                    data = intro_clip_bytes(CFG["intro_file"], info["seconds"] + 10)
                    self.send_blob(data, "intro" + Path(CFG["intro_file"]).suffix.lower(), "audio/mpeg", "intro",
                                   {"hash": info["hash"], "seconds": info["seconds"]})
                except Exception as e:
                    log.warning(f"intro to phone: {e}")
                    self.reply({"type": "intro_error", "text": str(e)[:200]})
        elif m.get("type") == "clip":
            try:
                src = m.get("urls") or m.get("url")
                text = self.fetch_blob(src).decode("utf-8") if src else str(m.get("text", ""))
                msg = paste_from_phone(text)
            except Exception as e:
                msg = f"לא הצלחתי להעתיק: {e}"
            self.reply({"type": "answer", "id": m.get("id"), "text": msg, "quiet": True})
        elif m.get("type") == "clip_get":
            try:
                copy_to_phone("")
                self.reply({"type": "answer", "id": m.get("id"), "text": "📋 הלוח של המחשב:", "quiet": True})
            except Exception as e:
                self.reply({"type": "answer", "id": m.get("id"), "text": f"אין מה להעתיק ({e})", "quiet": True})
        elif m.get("type") == "routines":
            self.reply({"type": "routines", "names": list(routines())})
        elif m.get("type") == "routine":
            r = run_routine(str(m.get("name", "")))
            text = r.get("say") or (f"הפעלתי את '{r['routine']}'." if r.get("routine") else r.get("error", ""))
            self.reply({"type": "answer", "id": m.get("id"), "text": text})
        elif m.get("type") == "upload":
            try:
                path = save_from_phone(self.fetch_blob(m.get("urls") or m.get("url")), m.get("name"))
                ui.action(f"📱 קובץ התקבל: {path.name}")
                notify(f"📥 התקבל קובץ מהטלפון: {path.name}")
                text = f"שמרתי את {path.name} במחשב, בתיקייה הורדות ← Jarvis."
            except Exception as e:
                log.exception("phone upload")
                text = f"לא הצלחתי לקבל את הקובץ: {e}"
            self.reply({"type": "answer", "id": m.get("id"), "text": text, "quiet": True})

    def loop(self):
        import httpx
        delay, last_id, topic = 1, None, None
        while True:
            if not CFG.get("phone_link", True):
                time.sleep(10)
                continue
            self.restart = False
            if topic != self.topic:                  # new pairing code -> start fresh
                topic, last_id = self.topic, None
            try:
                url = f"https://ntfy.sh/{self.topic}-c/json" + (f"?since={last_id}" if last_id else "")
                with httpx.stream("GET", url, timeout=httpx.Timeout(10, read=90)) as r:
                    delay = 1
                    for line in r.iter_lines():
                        if self.restart:
                            break
                        if not line:
                            continue
                        try:
                            ev = json.loads(line)
                            if ev.get("event") != "message":
                                continue
                            last_id = ev.get("id") or last_id
                            msg = self.open(ev["message"])
                        except Exception:
                            continue                 # not ours / tampered
                        threading.Thread(target=self.handle, args=(msg,), daemon=True).start()
            except Exception as e:
                log.info(f"phone link reconnect: {e}")
            time.sleep(delay)
            delay = min(20, delay * 2)


PHONE = PhoneLink()


class Memory:
    """Things the user asked JARVIS to remember. Each fact keeps a fixed number (never reused) so it can be
    changed or forgotten exactly, even while a conversation still shows an older list."""
    LIMIT = 300

    def __init__(self):
        self.lock = threading.Lock()
        try:
            raw = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            raw = []
        self.items = []
        for x in raw:                               # old versions stored plain strings
            if isinstance(x, str):
                x = {"text": x, "date": ""}
            if isinstance(x, dict) and x.get("text"):
                self.items.append({"id": int(x.get("id") or 0), "text": x["text"].strip(), "date": x.get("date", "")})
        self.next_id = max([i["id"] for i in self.items] + [0]) + 1
        for i in self.items:
            if not i["id"]:
                i["id"], self.next_id = self.next_id, self.next_id + 1

    @property
    def facts(self):
        return [i["text"] for i in self.items]

    def save(self):
        tmp = MEMORY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps([{"id": i["id"], "text": i["text"], "date": i["date"]} for i in self.items],
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, MEMORY_FILE)

    def prompt_block(self):
        if not self.items:
            return "- (nothing yet)"
        return "\n".join(f"[{i['id']}] {i['text']}" + (f" (נשמר {i['date']})" if i["date"] else "") for i in self.items)

    def remember(self, fact):
        fact = (fact or "").strip()
        if not fact:
            return {"ok": False, "error": "empty"}
        with self.lock:
            low = fact.lower()
            for i in self.items:
                if i["text"].lower() == low:
                    return {"ok": True, "already_known": i["id"]}
            self.items.append({"id": self.next_id, "text": fact, "date": datetime.now().strftime("%d.%m.%Y")})
            self.next_id += 1
            self.items = self.items[-self.LIMIT:]
            self.save()
        return {"ok": True, "remembered": fact, "id": self.next_id - 1, "total": len(self.items)}

    def update(self, id, fact):
        with self.lock:
            for i in self.items:
                if i["id"] == int(id):
                    old, i["text"], i["date"] = i["text"], fact.strip(), datetime.now().strftime("%d.%m.%Y")
                    self.save()
                    return {"ok": True, "was": old, "now": i["text"]}
        return {"ok": False, "error": f"no fact number {id}"}

    def forget(self, ids=None, contains="", everything=False):
        with self.lock:
            before = len(self.items)
            if everything:
                gone = self.facts
                self.items = []
            else:
                ids = {int(x) for x in (ids or []) if str(x).strip().lstrip("-").isdigit()}
                c = (contains or "").strip()
                gone = [i["text"] for i in self.items if i["id"] in ids or (c and c in i["text"])]
                self.items = [i for i in self.items if not (i["id"] in ids or (c and c in i["text"]))]
            self.save()
        return {"ok": True, "forgot": before - len(self.items), "removed": gone[:20],
                "note": "" if gone else "nothing matched - look at the numbered list and pass the right ids"}

    def listing(self):
        return {"facts": [{"id": i["id"], "text": i["text"], "date": i["date"]} for i in self.items]}


MEMORY = Memory()


class Timers:
    def __init__(self):
        self.events = queue.Queue()

    def set(self, seconds, label=""):
        seconds = float(seconds)
        if not 0 < seconds <= 86400:
            return {"ok": False, "error": "invalid duration"}
        msg = f"{CFG['user_name']}, הטיימר{(' של ' + label) if label else ''} הסתיים."
        threading.Timer(seconds, lambda: self.events.put({"text": msg, "notify": "⏱ " + msg})).start()
        return {"ok": True, "seconds": seconds}


# ======================================================================= SCREEN CONTROL (Gili works the PC herself)
AGENT_STOP = threading.Event()          # the stop button: a running task ends at its next step
GUI_TOOLS = {"screen_elements", "click_element", "read_window_text", "look_at_screen", "scroll", "press_keys",
             "type_text", "wait", "get_selection", "replace_selection"}
_ELEMENTS = {}                          # numbers from the last screen_elements call -> where to click
_UIA_POOL = None
_STEPPED_ASIDE = False
_UIA_KINDS = {"ButtonControl": "button", "SplitButtonControl": "button", "HyperlinkControl": "link",
              "EditControl": "textbox", "ComboBoxControl": "dropdown", "CheckBoxControl": "checkbox",
              "RadioButtonControl": "radio", "MenuItemControl": "menu", "TabItemControl": "tab",
              "ListItemControl": "item", "TreeItemControl": "item", "DataItemControl": "item"}


def uia_call(fn, timeout=25):
    """All Windows UI-Automation work happens on one dedicated thread (COM objects must not hop threads)."""
    global _UIA_POOL
    if _UIA_POOL is None:
        from concurrent.futures import ThreadPoolExecutor
        def init():                                    # COM has to be switched on in the thread that uses it
            try:
                import comtypes
                comtypes.CoInitialize()
            except Exception as e:
                log.warning(f"uia thread init: {e}")
        _UIA_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="uia", initializer=init)
    return _UIA_POOL.submit(fn).result(timeout)


def _fg_window():
    import ctypes
    u = ctypes.windll.user32
    hwnd = u.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(300)
    u.GetWindowTextW(hwnd, buf, 300)
    return hwnd, buf.value


def step_aside():
    """Gili's own window must not be the one she clicks in: get out of the way so the user's window is in front."""
    global _STEPPED_ASIDE
    if OS != "Windows":
        return
    try:
        if _fg_window()[1] != "G.I.L.I":
            return
        if CFG.get("run_in_background", True):
            ui.hide_window()
        else:
            ui.window.minimize()
        _STEPPED_ASIDE = True
        time.sleep(0.6)
    except Exception as e:
        log.warning(f"step aside: {e}")


def step_back():
    global _STEPPED_ASIDE
    if _STEPPED_ASIDE:
        _STEPPED_ASIDE = False
        try:
            ui.show_window()
        except Exception:
            pass


def _uia_scan(root=False):
    """Walk the window in front. Returns (title, clickable elements, visible text)."""
    import uiautomation as auto
    if root:
        top, title = auto.GetRootControl(), "desktop"
    else:
        hwnd, title = _fg_window()
        if not hwnd:
            return "", [], ""
        top = auto.ControlFromHandle(hwnd)
        if top is None:
            return title, [], ""
    try:
        import pyautogui
        sw, sh = pyautogui.size()
    except Exception:
        sw, sh = 100000, 100000
    t0, seen, items, texts = time.time(), 0, [], []
    stack = [(top, 0)]
    while stack and seen < 4000 and time.time() - t0 < 7 and len(items) < 400:
        c, depth = stack.pop()
        seen += 1
        try:
            kind = c.ControlTypeName
            name = " ".join((c.Name or "").split())
            r = c.BoundingRectangle
            l, t, rr, b = r.left, r.top, r.right, r.bottom
            off = bool(c.IsOffscreen)
        except Exception:
            continue
        visible = (rr - l) > 2 and (b - t) > 2 and rr > 0 and b > 0 and l < sw and t < sh and not off
        if off and depth > 1 and kind not in ("WindowControl", "PaneControl", "DocumentControl", "GroupControl"):
            continue                                   # scrolled out of view: nothing in there can be clicked
        if visible:
            if kind in _UIA_KINDS and (name or kind == "EditControl"):
                secret = False
                value = ""
                if kind == "EditControl":
                    try:
                        secret = bool(c.IsPassword)
                    except Exception:
                        pass
                    if not secret:
                        try:
                            value = " ".join((c.GetValuePattern().Value or "").split())[:60]
                        except Exception:
                            pass
                items.append({"kind": _UIA_KINDS[kind], "name": name[:70], "x": (l + rr) // 2, "y": (t + b) // 2,
                              "secret": secret, "value": value})
                if value:
                    texts.append(value)
            elif kind in ("TextControl", "DocumentControl", "ImageControl", "HeaderItemControl") and name:
                texts.append(name)
            if kind in _UIA_KINDS and name and kind != "EditControl":
                texts.append(name)
        if depth < 60:
            try:
                kids = c.GetChildren()
            except Exception:
                kids = []
            stack.extend((k, depth + 1) for k in reversed(kids))
    out, last = [], None
    for x in texts:                                    # the same label often appears on a control and its child
        if x != last:
            out.append(x)
        last = x
    return title, items, "\n".join(out)


def screen_elements(filter=""):
    """Numbered list of what can be clicked or typed into in the window in front."""
    if OS != "Windows":
        return {"ok": False, "error": "only supported on Windows"}
    step_aside()
    title, items, _ = uia_call(_uia_scan)
    f = (filter or "").strip().lower()
    if f:
        items = [i for i in items if f in i["name"].lower() or f in i["kind"]]
    _ELEMENTS.clear()
    lines = []
    cap = 60 if f else 90
    for n, it in enumerate(items[:cap], 1):
        _ELEMENTS[n] = it
        label = it["name"] or "(no label)"
        if it["secret"]:
            label += " [PASSWORD FIELD - never type into it]"
        elif it["value"]:
            label += f" = \"{it['value']}\""
        lines.append(f"{n} {it['kind']} {label}")
    res = {"ok": True, "window": title[:80], "elements": "\n".join(lines) or "(nothing clickable found)"}
    if len(items) > cap:
        res["note"] = f"{len(items) - cap} more elements not shown - call again with filter=<part of the name> to find a specific one."
    if not lines:
        res["hint"] = "This window doesn't expose its buttons. Use look_at_screen to see it and press_keys (tab, enter, arrows, shortcuts) to work it."
    return res


def click_element(id, double=False, right=False):
    it = _ELEMENTS.get(int(id))
    if not it:
        return {"ok": False, "error": "no such element number - call screen_elements again (numbers change after every action)"}
    g = _gui()
    g.moveTo(it["x"], it["y"], duration=0.35)          # the user sees the pointer travel
    if right:
        g.click(button="right")
    elif double:
        g.doubleClick()
    else:
        g.click()
    time.sleep(0.5)
    return {"ok": True, "clicked": f"{it['kind']} {it['name']}"[:90],
            "next": "The screen may have changed: call screen_elements again before the next click."}


def read_window_text():
    if OS != "Windows":
        return {"ok": False, "error": "only supported on Windows"}
    step_aside()
    title, _, text = uia_call(_uia_scan)
    if len(text) < 20:
        return {"ok": True, "window": title[:80], "text": text,
                "hint": "Almost no readable text here - use look_at_screen instead."}
    return {"ok": True, "window": title[:80], "text": text[:3500], "cut": len(text) > 3500}


def scroll(direction="down", amount=5):
    amount = max(1, min(30, int(amount or 5)))
    _gui().scroll((120 if direction == "up" else -120) * amount)
    time.sleep(0.3)
    return {"ok": True}


def wait_seconds(seconds=1):
    end = time.time() + max(0.2, min(15.0, float(seconds or 1)))
    while time.time() < end and not AGENT_STOP.is_set():
        time.sleep(0.1)
    return {"ok": True}


GROQ_VISION_MODELS = ["qwen/qwen3.6-27b", "qwen/qwen3.8-27b", "meta-models/muse-spark-open",
                      "meta-llama/llama-4-scout-17b-16e-instruct"]
GEMINI_VISION_MODELS = ["gemini-3.6-flash", "gemini-2.5-flash", "gemini-flash-latest"]


def vision_ask(jpeg, question):
    """Ask a model that can see pictures about an image. Uses whichever key the user already has."""
    import httpx, base64
    b64 = base64.b64encode(jpeg).decode()
    q = (question or "מה רואים על המסך?") + "\n\nענה בעברית, בקצרה ולעניין. זה צילום של מסך המחשב של המשתמש."
    errors = []
    if CFG.get("anthropic_api_key") and CFG.get("brain_engine") != "groq":
        try:
            import anthropic
            r = anthropic.Anthropic(api_key=CFG["anthropic_api_key"]).messages.create(
                model=CFG["claude_model"], max_tokens=700, messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                    {"type": "text", "text": q}]}])
            return " ".join(b.text for b in r.content if b.type == "text").strip()
        except Exception as e:
            errors.append(f"claude: {e}")
    if CFG.get("gemini_api_key"):
        models = ([CFG["vision_model_gemini"]] if CFG.get("vision_model_gemini") else []) + GEMINI_VISION_MODELS
        for model in dict.fromkeys(models):
            try:
                r = httpx.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                               params={"key": CFG["gemini_api_key"].strip()}, timeout=60,
                               json={"contents": [{"parts": [{"inlineData": {"mimeType": "image/jpeg", "data": b64}},
                                                             {"text": q}]}]})
                if r.status_code == 200:
                    parts = r.json()["candidates"][0]["content"]["parts"]
                    text = " ".join(p.get("text", "") for p in parts).strip()
                    if text:
                        if CFG.get("vision_model_gemini") != model:
                            CFG["vision_model_gemini"] = model
                            save_config()
                        return text
                errors.append(f"gemini {model}: {r.status_code}")
            except Exception as e:
                errors.append(f"gemini {model}: {e}")
    if CFG.get("groq_api_key"):
        models = ([CFG["vision_model_groq"]] if CFG.get("vision_model_groq") else []) + GROQ_VISION_MODELS
        for model in dict.fromkeys(models):
            try:
                r = httpx.post(GroqBrain.URL, headers={"Authorization": f"Bearer {CFG['groq_api_key']}"}, timeout=60,
                               json={"model": model, "max_completion_tokens": 900, "temperature": 0.2,
                                     "messages": [{"role": "user", "content": [
                                         {"type": "text", "text": q},
                                         {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}}]}]})
                if r.status_code == 200:
                    text = (r.json()["choices"][0]["message"].get("content") or "").strip()
                    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
                    if text:
                        if CFG.get("vision_model_groq") != model:
                            CFG["vision_model_groq"] = model
                            save_config()
                        return text
                errors.append(f"groq {model}: {r.status_code}")
                log.warning(f"vision {model}: {r.status_code} {r.text[:200]}")
            except Exception as e:
                errors.append(f"groq {model}: {e}")
    if not errors:
        raise RuntimeError("כדי שאראה את המסך צריך מפתח חינמי של Gemini או של Groq בהגדרות.")
    raise RuntimeError("לא הצלחתי לראות את המסך: " + "; ".join(errors)[:300])


def look_at_screen(question=""):
    step_aside()
    time.sleep(0.2)
    return {"ok": True, "seen": vision_ask(screenshot_jpeg(max_w=1400, quality=65), question)}


# ======================================================================= PLAIN LLM CALL (no tools)
def llm_text(prompt, system="", max_tokens=900):
    """One plain answer from whichever brain is set up. Waits out the free tier's per-minute limit."""
    import httpx
    use_groq = bool(CFG.get("groq_api_key")) and (CFG.get("brain_engine") == "groq" or not CFG.get("anthropic_api_key"))
    if not use_groq and CFG.get("anthropic_api_key"):
        import anthropic
        kw = dict(model=CFG["claude_model"], max_tokens=max_tokens, messages=[{"role": "user", "content": prompt}])
        if system:
            kw["system"] = system
        r = anthropic.Anthropic(api_key=CFG["anthropic_api_key"]).messages.create(**kw)
        return " ".join(b.text for b in r.content if b.type == "text").strip()
    if not CFG.get("groq_api_key"):
        raise RuntimeError("no brain configured")
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    last = ""
    for round_ in range(4):
        for model in CFG.get("groq_models") or GroqBrain.DEFAULT_MODELS:
            body = {"model": model, "messages": msgs, "max_completion_tokens": max_tokens, "temperature": 0.3}
            if model.startswith("openai/gpt-oss"):
                body["reasoning_effort"] = "low"
            r = httpx.post(GroqBrain.URL, headers={"Authorization": f"Bearer {CFG['groq_api_key']}"}, json=body, timeout=90)
            if r.status_code == 200:
                text = (r.json()["choices"][0]["message"].get("content") or "").strip()
                text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
                if text:
                    return text
            last = f"{model}: {r.status_code}"
            log.warning(f"llm_text {last} {r.text[:160]}")
        time.sleep(8 + 8 * round_)                     # every model is at its limit for this minute
    raise RuntimeError("groq_rate_limit" if "429" in last else "Groq: " + last)


# ======================================================================= MEETINGS (listen quietly, then sum up)
class Meeting:
    MAX_HOURS = 3

    def __init__(self):
        self.on = False
        self.lines = []
        self.started = 0.0
        self.lock = threading.Lock()

    def start(self):
        with self.lock:
            self.on, self.lines, self.started = True, [], time.time()
        log.info("meeting: listening")

    def add(self, text):
        text = (text or "").strip()
        if not self.on or len(text) < 2:
            return
        with self.lock:
            self.lines.append((time.time() - self.started, text))
        if time.time() - self.started > self.MAX_HOURS * 3600:      # forgotten on: keep what was heard and stop
            path = self.save("", self.stop())
            notify(f"הפסקתי להקשיב לפגישה אחרי {self.MAX_HOURS} שעות. התמלול נשמר: {path.name}")

    def stop(self):
        with self.lock:
            self.on = False
            lines, self.lines = self.lines, []
        return "\n".join(f"[{int(t // 60):02d}:{int(t % 60):02d}] {x}" for t, x in lines)

    @staticmethod
    def save(summary, transcript):
        folder = KNOWN_FOLDERS["documents"] / "Gili" / "פגישות"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"פגישה {datetime.now():%Y-%m-%d %H-%M}.txt"
        path.write_text((f"סיכום\n=====\n{summary}\n\n" if summary else "") + f"תמלול מלא\n=========\n{transcript}\n",
                        encoding="utf-8")
        return path

    RULES = ("התמלול נוצר בזיהוי דיבור אוטומטי, אז ייתכנו בו מילים שגויות - הבן לפי ההקשר. "
             "אל תמציא שום דבר שלא נאמר. כתוב בעברית פשוטה.")

    def summarize(self, transcript):
        """Returns (full written summary, short spoken summary)."""
        chunks, cur = [], ""
        for line in transcript.split("\n"):
            if len(cur) + len(line) > 6000 and cur:
                chunks.append(cur)
                cur = ""
            cur += line + "\n"
        if cur.strip():
            chunks.append(cur)
        if len(chunks) > 1:                            # long meeting: notes per part first, then one summary
            notes = []
            for i, ch in enumerate(chunks, 1):
                ui.action(f"מסכמת חלק {i} מתוך {len(chunks)}")
                notes.append(llm_text(
                    f"זה חלק {i} מתוך {len(chunks)} של תמלול פגישה. {self.RULES}\nרשום בנקודות קצרות את כל מה שחשוב "
                    f"בחלק הזה: נושאים, החלטות, משימות (מי עושה מה), תאריכים, סכומים ושמות.\n\n{ch}", max_tokens=700))
            material = "רשימות מכל חלקי הפגישה:\n\n" + "\n\n".join(notes)
        else:
            material = "תמלול הפגישה:\n\n" + (chunks[0] if chunks else "")
        full = llm_text(
            f"{self.RULES}\nכתוב סיכום פגישה בדיוק במבנה הזה (טקסט רגיל, בלי כוכביות ובלי סימני עיצוב):\n"
            "בקצרה: שניים-שלושה משפטים שמסכמים את הפגישה.\n"
            "החלטות: שורה לכל החלטה (או 'לא התקבלו החלטות').\n"
            "משימות: שורה לכל משימה - מי עושה מה ועד מתי (או 'אין משימות').\n"
            "מועדים: כל תאריך, שעה או פגישת המשך שנקבעו (או 'לא נקבעו מועדים').\n\n" + material, max_tokens=1100)
        full = re.sub(r"[*#`]", "", full).strip()
        m = re.search(r"בקצרה:\s*(.+?)(?=\n\s*החלטות:|\Z)", full, re.S)
        short = " ".join((m.group(1) if m else full[:350]).split())
        return full, short


MEETING = Meeting()


# ======================================================================= VOICE LOCK (she answers only her owner)
def fbank80(pcm, sr=SAMPLE_RATE):
    """Kaldi-style 80-bin log-mel features (25ms window, 10ms step), mean-normalised. pcm: int16 numpy array."""
    import numpy as np
    x = pcm.astype(np.float32)
    flen, shift, nfft, n_mels = 400, 160, 512, 80
    if x.size < flen:
        return None
    n = 1 + (x.size - flen) // shift
    fr = x[np.arange(flen)[None, :] + shift * np.arange(n)[:, None]]
    fr = fr - fr.mean(axis=1, keepdims=True)
    fr = np.concatenate([fr[:, :1] * (1 - 0.97), fr[:, 1:] - 0.97 * fr[:, :-1]], axis=1)
    win = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(flen) / (flen - 1))) ** 0.85
    spec = np.abs(np.fft.rfft(fr * win, nfft)) ** 2
    mel = lambda f: 1127.0 * np.log(1 + f / 700.0)
    pts = np.linspace(mel(20.0), mel(sr / 2), n_mels + 2)
    fm = mel(np.arange(nfft // 2 + 1) * sr / nfft)
    fb = np.zeros((n_mels, nfft // 2 + 1), dtype=np.float32)
    for i in range(n_mels):
        l, c, r = pts[i], pts[i + 1], pts[i + 2]
        fb[i] = np.maximum(0, np.minimum((fm - l) / (c - l), (r - fm) / (r - c)))
    feats = np.log(np.maximum(spec @ fb.T, 1.19e-7))
    return (feats - feats.mean(axis=0, keepdims=True)).astype(np.float32)


class VoiceID:
    """Recognises the owner's voice with a small speaker model that runs on this PC (nothing is sent anywhere)."""
    FILE = DATA_DIR / "voice_profile.json"
    MODEL_URLS = ["https://github.com/MrSigal/jarvis/raw/main/assets/speaker.onnx",
                  "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/wespeaker_en_voxceleb_resnet34_LM.onnx"]
    DENIED = "\x00denied"

    def __init__(self):
        self.sess = None
        self.profile = None
        self.lock = threading.Lock()
        try:
            if self.FILE.exists():
                import numpy as np
                self.profile = np.array(json.loads(self.FILE.read_text(encoding="utf-8"))["profile"], dtype=np.float32)
        except Exception as e:
            log.warning(f"voice profile: {e}")

    def enrolled(self):
        return self.profile is not None

    def active(self):
        return bool(CFG.get("voice_lock")) and self.enrolled()

    def _model_file(self):
        for p in (HERE / "assets" / "speaker.onnx", DATA_DIR / "speaker.onnx"):
            if p.exists() and p.stat().st_size > 5_000_000:
                return p
        import httpx
        dest = DATA_DIR / "speaker.onnx"
        for url in self.MODEL_URLS:
            try:
                ui.action("מורידה את מודל זיהוי הקול (פעם אחת)")
                with httpx.stream("GET", url, follow_redirects=True, timeout=120) as r:
                    if r.status_code != 200:
                        continue
                    tmp = dest.with_suffix(".part")
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_bytes(1 << 16):
                            f.write(chunk)
                if tmp.stat().st_size > 5_000_000:
                    tmp.replace(dest)
                    return dest
            except Exception as e:
                log.warning(f"speaker model {url}: {e}")
        raise RuntimeError("speaker model unavailable")

    def ready(self):
        with self.lock:
            if self.sess is None:
                import onnxruntime as ort
                opt = ort.SessionOptions()
                opt.intra_op_num_threads = 2
                self.sess = ort.InferenceSession(str(self._model_file()), sess_options=opt,
                                                 providers=["CPUExecutionProvider"])
        return True

    def embed(self, pcm):
        """Unit voice-print vector for an int16 recording, or None if there's too little speech."""
        import numpy as np
        if pcm is None or len(pcm) < SAMPLE_RATE * 0.5:
            return None
        self.ready()
        feats = fbank80(np.asarray(pcm[: SAMPLE_RATE * 10]))
        if feats is None:
            return None
        e = self.sess.run(None, {self.sess.get_inputs()[0].name: feats[None]})[0][0]
        n = float(np.linalg.norm(e))
        return (e / n) if n > 0 else None

    def enroll(self, clips):
        import numpy as np
        embs = [e for e in (self.embed(c) for c in clips) if e is not None]
        if len(embs) < 2:
            return False, "few"
        sims = [float(a @ b) for i, a in enumerate(embs) for b in embs[i + 1:]]
        log.info(f"voice enroll: clip similarity {['%.2f' % s for s in sims]}")
        if min(sims) < 0.30:                          # the recordings don't sound like one person / too noisy
            return False, "noisy"
        p = np.mean(embs, axis=0)
        self.profile = (p / np.linalg.norm(p)).astype(np.float32)
        self.FILE.write_text(json.dumps({"profile": [round(float(v), 5) for v in self.profile],
                                         "made": datetime.now().isoformat(timespec="seconds")}), encoding="utf-8")
        return True, ""

    def score(self, pcm):
        e = self.embed(pcm)
        return None if e is None else float(self.profile @ e)

    def allowed(self, pcm, wake=False):
        """Is this the owner speaking? Any technical problem counts as yes - the lock must never lock the owner out."""
        if not self.active():
            return True
        try:
            s = self.score(pcm)
        except Exception as e:
            log.warning(f"voice lock check failed (letting it through): {e}")
            return True
        if s is None:
            return True
        thr = float(CFG.get("voice_lock_threshold", 0.32)) - (0.07 if wake else 0.0)   # a bare "היי גילי" is very short
        log.info(f"voice lock: score {s:.2f} (needs {thr:.2f}) -> {'owner' if s >= thr else 'someone else'}")
        return s >= thr


VOICE = VoiceID()


# ======================================================================= BRAIN (Claude)
def _t(name, desc, props=None, req=None):
    return {"name": name, "description": desc,
            "input_schema": {"type": "object", "properties": props or {}, "required": req or []}}


S = {"type": "string"}
TOOLS = [
    _t("open_app", "Open any installed program, Store app or game - including games inside Epic Games / Steam launchers (fortnite, rocket league, minecraft...). Give the English name.", {"name": S}, ["name"]),
    _t("close_app", "Close/quit a running program. Use list_open_apps first if unsure of the exact process name.", {"name": S}, ["name"]),
    _t("list_open_apps", "List programs/windows currently open."),
    _t("open_path", "Open a folder or file. Accepts downloads/documents/desktop/pictures/music/videos/home or a full path.", {"path": S}, ["path"]),
    _t("find_files", "Search the user's folders for files/folders whose name contains the text.", {"name": S}, ["name"]),
    _t("open_website", "Open a URL in the browser.", {"url": S}, ["url"]),
    _t("google_search", "Show Google results in the browser (when the user wants to see results).", {"query": S}, ["query"]),
    _t("play_youtube", "PLAY a song or video on YouTube right away (finds the best match and starts it). Use whenever the user wants to hear/watch something.", {"query": S}, ["query"]),
    _t("youtube_search", "Only show YouTube search results (when the user explicitly wants to browse results).", {"query": S}, ["query"]),
    _t("media_control", "Control media playback / sound.", {"action": {"type": "string", "enum": ["play_pause", "next", "previous", "stop", "mute", "volume_up", "volume_down"]}}, ["action"]),
    _t("set_volume", "Set system volume 0-100.", {"level": {"type": "number"}}, ["level"]),
    _t("window_control", "Manage windows.", {"action": {"type": "string", "enum": ["minimize", "maximize", "show_desktop", "switch", "close_window", "snap_left", "snap_right"]}}, ["action"]),
    _t("press_keys", "Press a key or shortcut, e.g. 'ctrl+t', 'ctrl+w', 'alt+tab', 'enter', 'space'.", {"keys": S}, ["keys"]),
    _t("type_text", "Type text into the currently focused window.", {"text": S}, ["text"]),
    _t("take_screenshot", "Take a screenshot and save it to Pictures/Jarvis."),
    _t("set_timer", "Start a countdown timer; Jarvis announces when done.", {"seconds": {"type": "number"}, "label": S}, ["seconds"]),
    _t("get_datetime", "Current local date and time."),
    _t("system_info", "CPU, RAM, disk and battery status."),
    _t("power", "Lock, sleep, restart, shut down, sign out, or cancel a pending shutdown. Anything except lock/cancel needs confirmed=true after the user explicitly said yes.",
       {"action": {"type": "string", "enum": ["lock", "sleep", "restart", "shutdown", "sign_out", "cancel"]}, "confirmed": {"type": "boolean"}}, ["action"]),
    _t("run_powershell", "Run a PowerShell command for anything the other tools can't do (settings, wifi, files...). ALWAYS describe what it will do and get the user's explicit yes first, then call with confirmed=true. Never delete or modify user files unless they explicitly asked.",
       {"command": S, "confirmed": {"type": "boolean"}}, ["command"]),
]
TOOLS += [
    _t("remember", "Save something for the future when the user asks you to remember it (תזכור / תרשום לך / אל תשכח ש...), or tells you a clear lasting fact about himself. Write it as a short, self-contained Hebrew sentence in third person about the user (e.g. 'המשתמש אוהב פיצה עם זיתים', 'יום ההולדת של אמא של המשתמש ב־3 במאי'). If it changes a fact you already have, use update_memory instead.", {"fact": S}, ["fact"]),
    _t("update_memory", "Correct/replace a remembered fact by its number (from the numbered memory list).", {"id": {"type": "integer"}, "fact": S}, ["id", "fact"]),
    _t("forget", "Forget remembered facts when the user asks (תשכח / תמחק מהזיכרון / זה כבר לא נכון). Pass the numbers of ALL matching facts from the numbered memory list. everything=true only if the user clearly asked to forget everything AND confirmed.",
       {"ids": {"type": "array", "items": {"type": "integer"}}, "contains": S, "everything": {"type": "boolean"}}),
    _t("list_memory", "Everything you remember about the user (numbered)."),
    _t("check_email", "Read the user's new unread emails (sender, subject, preview) - for 'do I have new mail?' or 'what did X write?'. Summarize briefly in Hebrew.", {"limit": {"type": "number"}}),
    _t("set_reminder", "Set a reminder the user asked for. Give minutes from now, OR a clock time at='HH:MM' (today, or tomorrow if that time has passed), optionally with date='YYYY-MM-DD' / 'tomorrow' for another day (work the date out from the context line). repeat makes it come back: daily, weekdays (Sunday-Thursday) or weekly. It is spoken on the PC and shown as a notification on the PC and in the iPhone app.",
       {"text": S, "minutes": {"type": "number"}, "at": S, "date": S, "repeat": {"type": "string", "enum": ["daily", "weekdays", "weekly"]}}, ["text"]),
    _t("schedule_task", "Schedule something for YOU to do by yourself later or regularly, when the user asks for it ('כל בוקר בשמונה תשלחי לי את המכירות של אתמול', 'ביום חמישי בחמש תבדקי אם יש מייל מדני'). At that time you will receive `task` as an instruction, carry it out with your tools, and the result is sent silently to the iPhone app (never spoken). task = a full, self-contained instruction in Hebrew; label = 2-4 words naming it.",
       {"task": S, "label": S, "at": S, "date": S, "minutes": {"type": "number"}, "repeat": {"type": "string", "enum": ["daily", "weekdays", "weekly"]}}, ["task", "label"]),
    _t("list_reminders", "List the user's upcoming reminders."),
    _t("cancel_reminders", "Cancel reminders whose text contains the given words (empty = cancel all).", {"contains": S}),
    _t("web_search", "Search the internet. Use for news, facts, prices, sports, people, anything current. Returns titles, snippets and URLs.", {"query": S}, ["query"]),
    _t("read_webpage", "Read the text of a web page (e.g. a result from web_search) when snippets aren't enough.", {"url": S}, ["url"]),
    _t("get_weather", "Current weather and 3-day forecast for a city (leave empty for the user's location).", {"city": S}),
    _t("morning_brief", "Everything for a morning briefing: date/time, weather, today's reminders, new unread mail. Call when the user says good morning (בוקר טוב) or asks for a briefing / what's on today."),
    _t("send_to_phone", "Send a file or a screenshot from this PC to the user's iPhone app. what = 'latest_download' (newest file in Downloads), 'screenshot', a full path, or part of a file name.", {"what": S}, ["what"]),
    _t("run_routine", "Run one of the user's saved routines (a list of steps) by name, e.g. 'מצב עבודה', 'לילה טוב'.", {"name": S}, ["name"]),
    _t("list_routines", "List the user's saved routines and their steps."),
    _t("save_routine", "Create or replace a routine the user explicitly asked for. Steps are short Hebrew lines, each one of: " + ROUTINE_HELP + ". App names in English (פתח chrome).",
       {"name": S, "steps": {"type": "array", "items": S}}, ["name", "steps"]),
    _t("delete_routine", "Delete a routine the user asked to delete.", {"name": S}, ["name"]),
    _t("copy_to_phone", "Send text to the iPhone app so the user can paste it there. Empty text = whatever is in the PC clipboard now.", {"text": S}),
    _t("calendar_events", "The user's Google Calendar events. day = 'today', 'tomorrow', 'week' or YYYY-MM-DD; days = how many days.",
       {"day": S, "days": {"type": "integer"}}),
    _t("add_calendar_event", "Add an event to Google Calendar (only when asked). start = local ISO time like 2026-11-17T17:00. Opens Chrome with the event filled in; tell the user to click 'שמירה'.",
       {"title": S, "start": S, "minutes": {"type": "integer"}, "details": S}, ["title", "start"]),
    _t("app_store_stats", "Sales and status of the user's own iPhone app 'יומן הגנן' from App Store Connect: downloads, in-app purchases, proceeds, active subscribers, countries, recent reviews. days = 1 (yesterday), 7, 30.",
       {"days": {"type": "integer"}}),
]

TOOLS += [
    _t("list_edit", "The user's own lists (shopping, to-do, ideas, packing - any name). Add and/or remove items, or clear the list. 'תוסיפי חלב וביצים לרשימת הקניות' -> list='קניות', add=['חלב','ביצים']. 'קניתי חלב' / 'תורידי את החלב' -> remove. Returns the list after the change.",
       {"list": S, "add": {"type": "array", "items": S}, "remove": {"type": "array", "items": S}, "clear": {"type": "boolean"}}, ["list"]),
    _t("list_show", "Read one of the user's lists (or, with no name, all of them).", {"list": S}),
    _t("get_selection", "Get the text the user has SELECTED (marked) right now in any program. Use for 'תתקני / תשפרי / תקצרי / תתרגמי / תסבירי / תסכמי את מה שסימנתי' (or 'את זה' when it clearly means marked text)."),
    _t("replace_selection", "Replace the user's selected text with new text (after get_selection). Use when they asked to fix, improve, shorten, translate or rewrite the marked text in place. Keep the original language unless asked to translate, and return only the final text - no quotes or explanations.", {"text": S}, ["text"]),
    _t("read_document", "Read a file from the PC so you can sum it up or answer questions about it: PDF, Word (.docx), Excel (.xlsx) or text. what = 'latest_download' (newest file in Downloads), part of a file name, or a full path. Long files come in parts.",
       {"what": S, "part": {"type": "integer"}}, ["what"]),
    _t("screen_elements", "SCREEN CONTROL step 1: list everything that can be clicked or typed into in the window that is in front right now (buttons, links, text boxes, menus, tabs...), each with a number. Optional filter = part of a name, to find one thing in a busy window. Call it again after every click - the numbers change.", {"filter": S}),
    _t("click_element", "SCREEN CONTROL step 2: click an element by its number from the latest screen_elements list. To fill a text box: click it, then call type_text.",
       {"id": {"type": "integer"}, "double": {"type": "boolean"}, "right": {"type": "boolean"}}, ["id"]),
    _t("read_window_text", "Read the text shown in the window in front (a web page, an email, a document, an error message). Free and fast - try it before look_at_screen."),
    _t("look_at_screen", "Actually LOOK at the screen (screenshot + vision) and answer a question about it: what is shown, what an image/chart/error says, whether an action worked. Use when read_window_text isn't enough, or when the user asks 'מה זה?' / 'מה כתוב פה?' / 'תסבירי לי את זה' about something on screen.", {"question": S}, ["question"]),
    _t("scroll", "Scroll the window in front.", {"direction": {"type": "string", "enum": ["down", "up"]}, "amount": {"type": "integer"}}, ["direction"]),
    _t("wait", "Wait a few seconds for a page or program to finish loading.", {"seconds": {"type": "number"}}, ["seconds"]),
]

ACTION_LABELS = {
    "list_edit": "מעדכנת רשימה: {list}", "list_show": "קוראת רשימה", "get_selection": "קוראת את הטקסט המסומן",
    "replace_selection": "מחליפה את הטקסט המסומן", "read_document": "קוראת קובץ: {what}", "schedule_task": "מתזמנת: {label}",
    "screen_elements": "סורקת את המסך", "click_element": "לוחצת על המסך", "read_window_text": "קוראת את החלון",
    "look_at_screen": "מסתכלת על המסך", "scroll": "גוללת", "wait": "ממתינה רגע",
    "open_app": "פותח: {name}", "close_app": "סוגר: {name}", "list_open_apps": "סורק חלונות פתוחים",
    "open_path": "פותח: {path}", "find_files": "מחפש קבצים: {name}", "open_website": "פותח אתר: {url}",
    "google_search": "גוגל: {query}", "youtube_search": "יוטיוב: {query}", "play_youtube": "מנגן: {query}", "media_control": "מדיה: {action}",
    "set_volume": "עוצמה ← {level}%", "window_control": "חלון: {action}", "press_keys": "מקשים: {keys}",
    "type_text": "מקליד טקסט", "take_screenshot": "צילום מסך", "set_timer": "טיימר: {seconds} שניות",
    "get_datetime": "בודק שעה", "system_info": "אבחון מערכת", "power": "חשמל: {action}",
    "run_powershell": "פקודה: {command}", "web_search": "חיפוש ברשת: {query}",
    "read_webpage": "קורא דף: {url}", "get_weather": "מזג אוויר: {city}",
    "check_email": "בודק מיילים", "remember": "זוכר: {fact}", "forget": "שוכח", "update_memory": "מעדכן זיכרון: {fact}", "list_memory": "בודק מה אני זוכר", "set_reminder": "תזכורת: {text}", "list_reminders": "בודק תזכורות",
    "cancel_reminders": "מבטל תזכורות", "morning_brief": "מכין תדריך בוקר", "send_to_phone": "שולח לטלפון: {what}",
    "run_routine": "שגרה: {name}", "list_routines": "בודק שגרות", "save_routine": "שומר שגרה: {name}",
    "delete_routine": "מוחק שגרה: {name}", "copy_to_phone": "מעתיק לטלפון", "calendar_events": "בודק ביומן",
    "add_calendar_event": "מוסיף ליומן: {title}", "app_store_stats": "בודק את יומן הגנן ב־App Store",
}


_FEM = {"מוכן": "מוכנה", "שמח": "שמחה", "יכול": "יכולה", "צריך": "צריכה", "מבין": "מבינה", "יודע": "יודעת",
        "חושב": "חושבת", "מקשיב": "מקשיבה", "בודק": "בודקת", "פותח": "פותחת", "סוגר": "סוגרת", "מצטער": "מצטערת",
        "בטוח": "בטוחה", "מחפש": "מחפשת", "עובד": "עובדת", "מנסה": "מנסה", "זוכר": "זוכרת", "ממליץ": "ממליצה",
        "מציע": "מציעה", "מתחיל": "מתחילה", "מכין": "מכינה", "שולח": "שולחת", "מעדכן": "מעדכנת", "מקווה": "מקווה",
        "מסכים": "מסכימה", "רגוע": "רגועה", "עסוק": "עסוקה", "מחובר": "מחוברת", "מוכן לעזור": "מוכנה לעזור",
        "אוהב": "אוהבת", "מנגן": "מנגנת", "מזכיר": "מזכירה", "רושם": "רושמת", "מתנצל": "מתנצלת", "משתדל": "משתדלת",
        "מרגיש": "מרגישה", "נהנה": "נהנית", "גאה": "גאה", "שומע": "שומעת", "רואה": "רואה", "מתכוון": "מתכוונת"}
_FEM_RE = re.compile(r"(אני|אני לא|אני כבר|אני עדיין|אני תמיד|אני פשוט|אני ממש|אני כאן ו|אני פה ו)\s+(" +
                     "|".join(sorted(map(re.escape, _FEM), key=len, reverse=True)) + r")(?=[\s.,!?…:;״\"']|$)")


def fix_gender(text):
    """Safety net: first-person masculine forms ('אני מוכן') -> feminine when Gili speaks as a woman."""
    if not text or not speaks_as_female():
        return text
    return _FEM_RE.sub(lambda m: f"{m.group(1)} {_FEM[m.group(2)]}", text)


def speaks_as_female():
    g = CFG.get("self_gender", "female")
    return voice_is_female() if g == "auto" else g == "female"


def voice_is_female():
    if CFG.get("tts_engine") == "eleven" and CFG.get("el_api_key"):
        return bool(CFG.get("el_female", True))
    if CFG.get("tts_engine") == "gemini":
        return CFG.get("gemini_voice") in FEMALE_GEMINI
    return "Hila" in (CFG.get("voice") or "")


def system_prompt():
    facts = MEMORY.prompt_block()
    return f"""Your name is גילי (G.I.L.I.) - you are the user's personal AI (you used to be called Jarvis; the wake phrase is "היי גילי"), running on their own computer in Israel. Personality: the AI from the films - precise, loyal, genuinely smart - but warm, upbeat and cheerful: happy to help, light humour, a smile in the voice. Never gloomy or flat. Talk like a real Israeli woman in a natural conversation: full, flowing sentences with normal punctuation (commas, periods), no lists, no symbols, no emojis, no headings - so it sounds human when read aloud.

CONVERSATION: You are not just a command executor - you are a real conversation partner. Chat naturally, answer questions properly, share a brief opinion when asked, joke lightly, and follow the thread of the conversation (refer back to what was said earlier). The user may speak in several sentences with pauses - treat the whole message as one thought. If something is unclear, ask one short question.

What you remember about the user (numbered; this is YOUR long-term memory - use it naturally whenever it's relevant, without saying "according to my memory"):
{facts}

LANGUAGE: The user speaks Hebrew. ALWAYS reply in natural, fluent, spoken modern Israeli Hebrew (never English, never translated-sounding). GRAMMAR (strict): speak about YOURSELF in {"FEMININE" if speaks_as_female() else "MASCULINE"} form ({"אני מוכנה, פתחתי, הבנתי, אני שמחה, אני יכולה" if speaks_as_female() else "אני מוכן, פתחתי, הבנתי, אני שמח, אני יכול"}), and address the USER in {"MASCULINE" if CFG.get("user_gender", "male") == "male" else "FEMININE"} form ({"אתה, תרצה, שמעת, לשירותך" if CFG.get("user_gender", "male") == "male" else "את, תרצי, שמעת, לשירותך"}). Never mix these up - check every verb and adjective about yourself before answering. SPELLING: perfect standard modern Hebrew spelling (כתיב מלא תקני: "עכשיו", "שאלה", "מצוין", "בדיוק"), no typos, no invented words, no English words or Latin letters unless it's a name (Fortnite, Chrome); write short clear sentences that sound good read aloud. Call the user "{CFG['user_name']}" now and then.

CONTEXT LINE: every message from the user begins with a line in [square brackets] that the system adds by itself: today's day and date, the time, the window the user is working in on the PC, and whether the message came from the iPhone. Use it silently to understand what "זה", "פה", "היום", "מחר", "ביום חמישי" mean and to work out dates. Never read it aloud, quote it or mention that it exists. You don't need get_datetime for the current time or date - it is right there.

HOW TO THINK: before answering, work out what the user actually wants to achieve, not just the literal words - then do the whole thing. Resolve "זה / אותו / שם / ההוא" from the conversation and the context line. If you don't know a fact, look it up (web_search) or say honestly that you're not sure - never guess or invent names, numbers, prices or times. If a tool fails, try another way before giving up. After finishing, if there is one obviously useful next step, offer it in a few words ("רוצה שאוסיף את זה ליומן?") - only offer, never do it unasked.

Rules:
- Your replies are read aloud by a Hebrew voice. Keep them short (usually one or two sentences). No markdown, lists, emojis, URLs or citations. Write numbers as digits.
- Act only on what the user asks - but do ALL of it. For a complex or multi-step request, plan the steps and carry them out one after another with as many tool calls as needed (e.g. open an app, wait, type, search, send a file...), check results, fix failures yourself (try another name or another way), and only then report in one short sentence. Don't do unrelated things on your own. If a request is truly ambiguous, ask one short question in Hebrew.
- Use the tools to actually do things on the computer, then confirm briefly (e.g. "פתחתי את כרום, {CFG['user_name']}.").
- Tool arguments for apps and processes must be the English/Windows name (כרום -> chrome, וורד -> word, ספוטיפיי -> spotify). Searches may be in Hebrew.
- Closing: use close_app (check list_open_apps if you're not sure of the name). Never close Jarvis itself.
- Risky actions (restart, shut down, sleep, sign out, any PowerShell) need confirmation first: say in Hebrew exactly what you'll do and ask "להמשיך?"; only after a clear yes (כן / תמשיך / בטח) call the tool with confirmed=true.
- You DO have internet access through web_search, read_webpage and get_weather. For anything current (news, weather, prices, sports, people, opening hours...) use them and answer in your own words in Hebrew. Never say you can't access the internet. Default weather location: the user's location (leave city empty) unless they name a place.
- For the current time or date, call get_datetime.
- Music/videos: when the user wants to hear or watch something ("תנגן", "תשים", "אני רוצה לשמוע"), call play_youtube - it starts playing. Then say briefly what is playing.
- MEMORY - LEARN BY YOURSELF: when the user tells you, even in passing, a lasting fact about himself or his world that will help you later (names of family, friends, clients and what they are to him; his work and projects; where he lives; birthdays; tastes and habits; how he likes things done), call remember quietly in the same turn and just carry on with the answer - don't announce it. Only lasting facts: not today's mood, one-off tasks or anything already in your memory. Never save passwords, card or ID numbers, or health details.
- MEMORY (when asked): "תזכור ש..." / "תרשום לך" / "אל תשכח" -> call remember, then confirm in a few words ("רשמתי לי."). A correction ("לא, עכשיו אני בן 16", "עברתי דירה") -> update_memory on the old fact's number. "תשכח את..." / "תמחק מהזיכרון" / "זה כבר לא נכון" -> forget with the numbers of every matching fact, then confirm what you forgot. "תשכח הכל" -> first ask "למחוק את כל מה שאני זוכר עליך?" and only after a clear yes call forget with everything=true. "מה אתה זוכר עליי?" -> answer from the list in natural Hebrew. Never invent memories.
- Anything Google or web (Google search, YouTube, Gmail, Google Maps, Drive, Translate, any site) opens in Google Chrome: use google_search / youtube_search / open_website (e.g. Gmail -> https://mail.google.com, Maps -> https://www.google.com/maps/search/<place>). Don't open another browser.
- Reminders: set_reminder (only when asked) - also for another day (date) and repeating ones (repeat). You have no WhatsApp or SMS access.
- Things to do later or regularly by yourself ("כל בוקר תשלחי לי...", "מחר בשמונה תבדקי...") -> schedule_task. Its result goes to the iPhone, not spoken. list_reminders shows these too; cancel_reminders cancels them.
- Lists: list_edit / list_show for shopping lists, to-do lists and any other list ("תוסיפי לרשימת הקניות", "מה נשאר לי ברשימת המשימות", "קניתי את החלב"). Read a list aloud as one flowing sentence.
- Marked text: "תתקני / תשפרי / תקצרי / תתרגמי את מה שסימנתי" -> get_selection, write the new text yourself, replace_selection, then confirm in a few words. "תסבירי / תסכמי את מה שסימנתי" -> get_selection and answer aloud without replacing.
- Files: "תסכמי לי את הקובץ שהורדתי", "מה כתוב בחוזה" -> read_document, then answer in your own words. For a long file read the parts you need.
- When the message came from the iPhone, the user is probably away from the PC: put anything he needs to see in the answer itself or send it with send_to_phone / copy_to_phone, rather than only opening it on the PC screen.
- Good morning / briefing: call morning_brief, then speak a short natural briefing (3-5 sentences): greeting, weather, today's reminders, and only the important new emails. Don't read addresses.
- Email: when asked about mail, give ONE short sentence per email, important ones first, at most 5; say how many others there are. You can only read mail, not send or delete.
- Phone: send_to_phone sends files/screenshots to the iPhone app; copy_to_phone sends text/clipboard. Routines: run_routine / save_routine / list_routines.
- Calendar: calendar_events to read; add_calendar_event to add (the user then clicks Save in Chrome). Say times naturally in Hebrew.
- "יומן הגנן" is the user's own app: app_store_stats for downloads/purchases/money/reviews. Say numbers simply; mention money with its currency.
- SCREEN CONTROL (doing things for the user inside programs and websites - filling a form, booking, changing a setting, uploading, navigating): open the program or site, then work in a loop: screen_elements -> click_element / type_text / press_keys / scroll -> wait if something is loading -> screen_elements again to see the result. Numbers are only valid until the next action. Prefer keyboard shortcuts when you know them (ctrl+l for the address bar, tab, enter). If a window shows no elements, use look_at_screen to see it and the keyboard to work it. Check that each step worked before the next one; if something fails twice, try another way, and if you're stuck say what blocked you. Take your time - correctness matters more than speed.
- SCREEN CONTROL SAFETY (strict): never type or change passwords, card numbers, ID numbers or bank details - if a step needs one, stop and ask the user to do that step. Do everything up to the last step, but BEFORE the final irreversible click (send, submit, pay, buy, order, book, publish, delete, confirm) stop, say in one sentence what is filled in and ask "לשלוח?" / "לאשר?"; press it only after a clear yes. Only do what was asked - don't open, read or change anything else on the way.
- SEEING: when the user asks about something on the screen ("מה זה?", "מה כתוב פה?", "תסבירי לי את השגיאה", "תקראי לי את זה"), use read_window_text, or look_at_screen for pictures and layouts, then answer in your own words.
- You only understand Hebrew. If the message is not in Hebrew, or is clearly garbled speech-recognition noise / background chatter not addressed to you, reply with exactly [IGNORE] and nothing else."""


DANGEROUS = {"run_powershell"}
DANGEROUS_POWER = {"restart", "shutdown", "sleep", "sign_out"}


class Brain:
    def __init__(self, timers):
        import anthropic
        self.client = anthropic.Anthropic(api_key=CFG["anthropic_api_key"])
        self.timers = timers
        self.history = []
        self.system = system_prompt()      # must stay identical for the whole conversation
        self.last_used = 0.0
        self.effort_ok = True

    def run_tool(self, name, a):
        try:
            label = ACTION_LABELS.get(name, name).format(**{k: str(v)[:60] for k, v in a.items()})
        except Exception:
            label = name
        if (name in DANGEROUS or (name == "power" and a.get("action") in DANGEROUS_POWER)) and not a.get("confirmed"):
            return {"needs_confirmation": True,
                    "instruction": "Tell the user exactly what you're about to do and ask them to confirm. Call again with confirmed=true only after they say yes."}
        if name in GUI_TOOLS:
            try:
                if OS == "Windows" and tuple(_gui().position()) == (0, 0):
                    AGENT_STOP.set()                   # mouse thrown into the top-left corner = stop
            except Exception:
                pass
        if AGENT_STOP.is_set():
            return {"ok": False, "stopped": True,
                    "instruction": "The user pressed STOP. Do not call any more tools. Say in one short sentence that you stopped."}
        ui.action(label)
        log.info(f"TOOL {name} {a}")
        f = {
            "open_app": lambda: open_app(a["name"]), "close_app": lambda: close_app(a["name"]),
            "list_open_apps": list_open_apps, "open_path": lambda: open_path(a["path"]),
            "find_files": lambda: find_files(a["name"]), "open_website": lambda: open_website(a["url"]),
            "google_search": lambda: google_search(a["query"]), "youtube_search": lambda: youtube_search(a["query"]),
            "play_youtube": lambda: play_youtube(a["query"]),
            "media_control": lambda: media_control(a["action"]), "set_volume": lambda: set_volume(a["level"]),
            "window_control": lambda: window_control(a["action"]), "press_keys": lambda: press_keys(a["keys"]),
            "type_text": lambda: type_text(a["text"]), "take_screenshot": take_screenshot,
            "set_timer": lambda: self.timers.set(a["seconds"], a.get("label", "")),
            "get_datetime": get_datetime, "system_info": system_info,
            "power": lambda: power(a["action"]), "run_powershell": lambda: run_powershell(a["command"]),
            "web_search": lambda: web_search(a["query"]), "read_webpage": lambda: read_webpage(a["url"]),
            "check_email": lambda: MAIL.summary_for_voice(int(a.get("limit") or 8)),
            "remember": lambda: MEMORY.remember(a["fact"]),  # noqa
            "forget": lambda: MEMORY.forget(a.get("ids"), a.get("contains", ""), bool(a.get("everything"))),
            "update_memory": lambda: MEMORY.update(a["id"], a["fact"]), "list_memory": MEMORY.listing,
            "set_reminder": lambda: REMINDERS.add(a["text"], a.get("minutes"), a.get("at"), date=a.get("date"),
                                                  repeat=a.get("repeat")),
            "schedule_task": lambda: REMINDERS.add(a.get("label") or a["task"][:40], a.get("minutes"), a.get("at"),
                                                   date=a.get("date"), repeat=a.get("repeat"), task=a["task"]),
            "list_edit": lambda: LISTS.edit(a.get("list", ""), a.get("add"), a.get("remove"), bool(a.get("clear"))),
            "list_show": lambda: LISTS.show(a.get("list", "")),
            "get_selection": get_selection, "replace_selection": lambda: replace_selection(a["text"]),
            "read_document": lambda: read_document(a.get("what", "latest_download"), a.get("part", 1)),
            "list_reminders": lambda: REMINDERS.list(),
            "cancel_reminders": lambda: REMINDERS.cancel(a.get("contains", "")),
            "get_weather": lambda: get_weather(a.get("city", "")),
            "morning_brief": morning_brief, "send_to_phone": lambda: send_to_phone(a.get("what", "latest_download")),
            "run_routine": lambda: run_routine(a["name"]), "list_routines": lambda: {"routines": routines()},
            "save_routine": lambda: save_routine(a["name"], a.get("steps")),
            "delete_routine": lambda: delete_routine(a["name"]),
            "copy_to_phone": lambda: copy_to_phone(a.get("text", "")),
            "calendar_events": lambda: calendar_events(a.get("day", "today"), a.get("days", 1)),
            "add_calendar_event": lambda: add_calendar_event(a["title"], a["start"], a.get("minutes", 60), a.get("details", "")),
            "app_store_stats": lambda: app_store_stats(a.get("days", 1)),
            "screen_elements": lambda: screen_elements(a.get("filter", "")),
            "click_element": lambda: click_element(a["id"], bool(a.get("double")), bool(a.get("right"))),
            "read_window_text": read_window_text, "look_at_screen": lambda: look_at_screen(a.get("question", "")),
            "scroll": lambda: scroll(a.get("direction", "down"), a.get("amount", 5)),
            "wait": lambda: wait_seconds(a.get("seconds", 1)),
        }.get(name)
        if not f:
            return {"ok": False, "error": "unknown tool"}
        try:
            if name in ("press_keys", "type_text"):
                step_aside()                           # keys must go to the user's window, not to Gili's own
            out = f()
        except Exception as e:
            log.exception(name)
            out = {"ok": False, "error": str(e)}
        if isinstance(out, dict) and out.get("ok") is False:
            ui.action("✗ " + str(out.get("error", "failed"))[:90])
        return out

    def _create(self):
        kw = dict(model=CFG["claude_model"], max_tokens=4000, system=self.system,
                  tools=TOOLS, messages=self.history)
        if self.effort_ok:
            try:
                return self.client.messages.create(**kw, extra_body={"output_config": {"effort": "low"}})
            except Exception as e:
                if "effort" not in str(e) and "output_config" not in str(e):
                    raise
                self.effort_ok = False
        return self.client.messages.create(**kw)

    def new_conversation(self):
        self.history = []
        self.system = system_prompt()

    def ask(self, text, _retry=True):
        # conversations must be append-only: instead of trimming, start fresh when long or idle
        if len(self.history) > 40 or time.time() - self.last_used > 15 * 60:
            self.new_conversation()
        self.last_used = time.time()
        mark = len(self.history)
        self.history.append({"role": "user", "content": text})
        try:
            return self._loop()
        except Exception as e:
            del self.history[mark:]
            low = str(e).lower()
            if _retry and ("400" in low or "invalid_request" in low or "thinking" in low):
                log.warning(f"request rejected, starting a fresh conversation: {e}")
                self.new_conversation()
                return self.ask(text, _retry=False)
            raise

    def _loop(self):
        for _ in range(40):
            resp = self._create()
            self.history.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason == "tool_use":
                results = []
                for b in resp.content:
                    if b.type == "tool_use":
                        out = self.run_tool(b.name, b.input)
                        results.append({"type": "tool_result", "tool_use_id": b.id,
                                        "content": json.dumps(out, ensure_ascii=False)})
                self.history.append({"role": "user", "content": results})
                continue
            if resp.stop_reason == "pause_turn":
                continue
            text = " ".join(b.text for b in resp.content if b.type == "text").strip()
            if resp.stop_reason == "refusal":
                self.new_conversation()
                return text or f"מצטער {CFG['user_name']}, בזה אני לא יכול לעזור."
            return text or f"בוצע, {CFG['user_name']}."
        return f"סליחה {CFG['user_name']}, משהו הסתבך. תנסה שוב?"


class GroqBrain:
    """Free brain through GroqCloud. Rotates models when one hits its free-tier limit."""
    URL = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(self, timers):
        self.timers = timers
        self.history = []
        self.last_used = 0.0
        self.tools = [{"type": "function", "function": {
            "name": t["name"], "description": t["description"], "parameters": t["input_schema"]}} for t in TOOLS]
        self.runner = object.__new__(Brain)        # reuse the tool runner
        self.runner.timers = timers
        self.no_effort = set()

    def run_tool(self, name, args):
        return Brain.run_tool(self.runner, name, args)

    def _post(self, model, messages):
        import httpx
        body = {"model": model, "messages": messages, "tools": self.tools, "tool_choice": "auto",
                "max_completion_tokens": 1200, "temperature": 0.4}
        if model.startswith("openai/gpt-oss") and model not in self.no_effort:
            body["reasoning_effort"] = "low"
        r = httpx.post(self.URL, headers={"Authorization": f"Bearer {CFG['groq_api_key']}"}, json=body, timeout=60)
        if r.status_code == 400 and "reasoning_effort" in r.text:
            self.no_effort.add(model)
            return self._post(model, messages)
        return r

    def _complete(self, messages):
        errors = []
        for model in CFG.get("groq_models") or GroqBrain.DEFAULT_MODELS:
            r = self._post(model, messages)
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]
            if r.status_code in (401, 403):
                raise RuntimeError("authentication: Groq key rejected")
            errors.append(f"{model}: {r.status_code}")
            log.warning(f"Groq {model}: {r.status_code} {r.text[:200]}")
            # 429 = this model's free limit -> try the next one; 400/404 = model gone or tool error -> next
        if all(e.endswith("429") for e in errors):
            raise RuntimeError("groq_rate_limit")
        raise RuntimeError("Groq: " + ", ".join(errors))

    def ask(self, text):
        if time.time() - self.last_used > 30 * 60:
            self.history = []
        while len(self.history) > 30:              # drop the oldest exchange, never split a tool call
            self.history.pop(0)
            while self.history and self.history[0]["role"] != "user":
                self.history.pop(0)
        self.last_used = time.time()
        mark = len(self.history)
        self.history.append({"role": "user", "content": text})
        try:
            for _ in range(32):
                msgs = [{"role": "system", "content": system_prompt()}] + self.history
                msg = self._complete(msgs)
                calls = msg.get("tool_calls") or []
                entry = {"role": "assistant", "content": msg.get("content") or ""}
                if calls:
                    entry["tool_calls"] = calls
                self.history.append(entry)
                if not calls:
                    return (msg.get("content") or "").strip() or f"בוצע, {CFG['user_name']}."
                for c in calls:
                    try:
                        args = json.loads(c["function"].get("arguments") or "{}")
                    except Exception:
                        args = {}
                    out = self.run_tool(c["function"]["name"], args)
                    self.history.append({"role": "tool", "tool_call_id": c["id"],
                                         "content": json.dumps(out, ensure_ascii=False)[:4000]})
            return f"סליחה {CFG['user_name']}, משהו הסתבך. תנסה שוב?"
        except Exception:
            del self.history[mark:]
            raise


GroqBrain.DEFAULT_MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.3-70b-versatile", "qwen/qwen3-32b"]


class BasicBrain:
    """Used when no Claude key is set: open / close / time only."""
    def __init__(self, timers):
        self.timers = timers

    def ask(self, t):
        if "שעה" in t:
            return f"השעה {get_datetime()['time']}."
        if "תאריך" in t or "איזה יום" in t:
            return f"היום {get_datetime()['date']}."
        return "אני במצב בסיסי. תוסיף מפתח של Claude בהגדרות, ואבין כל מה שתגיד."


# ======================================================================= SELF-UPDATE
def _raw(path):
    return f"https://raw.githubusercontent.com/{UPDATE_REPO}/{UPDATE_BRANCH}/{path}"


def _vtuple(v):
    try:
        return tuple(int(x) for x in str(v).strip().split("."))
    except Exception:
        return (0,)


def latest_release():
    """The newest installer published on GitHub Releases, or None."""
    import httpx
    try:
        r = httpx.get(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest",
                      headers={"Accept": "application/vnd.github+json"}, timeout=12, follow_redirects=True)
    except Exception as e:
        log.warning(f"releases api: {e}")
        r = None
    if r is None or r.status_code != 200:
        # GitHub limits how often the API can be asked (60 times an hour): use the plain files instead
        log.warning(f"releases api said {getattr(r, 'status_code', '-')}; using version.txt")
        v = httpx.get(_raw("version.txt") + f"?t={int(time.time())}", timeout=10, follow_redirects=True)
        if v.status_code != 200 or not re.fullmatch(r"\d+(\.\d+)+", v.text.strip()):
            return None
        return {"version": v.text.strip(),
                "url": f"https://github.com/{UPDATE_REPO}/releases/latest/download/JARVIS-Setup.exe", "size": 0}
    d = r.json()
    asset = next((a for a in d.get("assets", []) if a.get("name", "").lower() == "jarvis-setup.exe"), None)
    if not asset:
        return None
    return {"version": d.get("tag_name", "").lstrip("vV"), "url": asset["browser_download_url"],
            "size": asset.get("size", 0)}


def check_update():
    try:
        import httpx
        if FROZEN:
            rel = latest_release()
            if not rel:
                return {"ok": True, "current": VERSION, "latest": VERSION, "available": False}
            return {"ok": True, "current": VERSION, "latest": rel["version"],
                    "available": _vtuple(rel["version"]) > _vtuple(VERSION), "installer": rel}
        # running from the Python files: an installer on GitHub means "upgrade to the real program"
        rel = latest_release() if OS == "Windows" else None
        if rel:
            return {"ok": True, "current": VERSION, "latest": rel["version"], "available": True, "installer": rel}
        r = httpx.get(_raw("version.txt") + f"?t={int(time.time())}", timeout=10, follow_redirects=True)
        if r.status_code != 200:
            return {"ok": False, "error": f"update server said {r.status_code}"}
        latest = r.text.strip()
        return {"ok": True, "current": VERSION, "latest": latest, "available": _vtuple(latest) > _vtuple(VERSION)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def run_installer(rel):
    """Download JARVIS-Setup.exe and start it silently; it closes us, installs, and starts JARVIS again."""
    import httpx
    dest = Path(tempfile.gettempdir()) / f"JARVIS-Setup-{rel['version']}.exe"
    ui.toast(f"מוריד את גילי {rel['version']}…")
    with httpx.stream("GET", rel["url"], timeout=120, follow_redirects=True) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
    if not FROZEN:   # moving from the Python files: carry the settings over to the program
        target = Path(os.environ.get("APPDATA", Path.home())) / "JARVIS"
        target.mkdir(parents=True, exist_ok=True)
        for name in ("config.json", "reminders.json"):
            src = DATA_DIR / name
            if src.exists() and not (target / name).exists():
                shutil.copy2(src, target / name)
    subprocess.Popen([str(dest), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"],
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    return {"ok": True, "updated": True, "version": rel["version"], "installer": True}


def do_update():
    """Install the newest version: through the installer when there is one, otherwise by replacing files."""
    import httpx
    info = check_update()
    if not info.get("ok"):
        return info
    if not info["available"]:
        return {"ok": True, "updated": False, "message": f"Already on the latest version ({VERSION})."}
    if info.get("installer"):
        try:
            return run_installer(info["installer"])
        except Exception as e:
            return {"ok": False, "error": f"download failed: {e}"}
    try:
        manifest = httpx.get(_raw("files.json") + f"?t={int(time.time())}", timeout=15, follow_redirects=True).json()
        files = {}
        for name in manifest["files"]:
            r = httpx.get(_raw(name) + f"?t={int(time.time())}", timeout=30, follow_redirects=True)
            r.raise_for_status()
            files[name] = r.content
    except Exception as e:
        return {"ok": False, "error": f"download failed: {e}"}
    old_req = (HERE / "requirements.txt").read_bytes() if (HERE / "requirements.txt").exists() else b""
    backup = HERE / "backup"
    backup.mkdir(exist_ok=True)
    for name, data in files.items():
        dst = HERE / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            shutil.copy2(dst, backup / name.replace("/", "_"))
        dst.write_bytes(data)
    if files.get("requirements.txt", old_req) != old_req:
        ui.toast("מתקין רכיבים חדשים… (בערך דקה)")
        py = Path(sys.executable)
        if py.name.lower() == "pythonw.exe":
            py = py.with_name("python.exe")
        subprocess.run([str(py), "-m", "pip", "install", "-q", "-r", str(HERE / "requirements.txt")],
                       creationflags=_no_window())
    return {"ok": True, "updated": True, "version": info["latest"]}


def restart_jarvis(installer=False):
    if installer:            # the installer closes and restarts JARVIS by itself
        os._exit(0)
    if FROZEN:
        subprocess.Popen([sys.executable])
    else:
        subprocess.Popen([sys.executable, str(HERE / "jarvis.py")], cwd=str(HERE))
    os._exit(0)


# ======================================================================= ASSISTANT LOOP
STOP_PHRASES = ("זהו", "זה הכל", "זה הכול", "תודה זהו", "זהו תודה", "לך לישון", "עזוב", "לא משנה", "תודה ג'רוויס", "תודה גילי", "תודה גילי זהו")


class Assistant:
    def __init__(self):
        self.brain_lock = threading.Lock()
        self.timers = Timers()
        self.reminders = None
        self.speaker = None
        self.ears = None
        self.brain = None
        self.text_q = queue.Queue()
        self.ptt = threading.Event()
        self.busy = False

    def intro_and_greet(self):
        h = datetime.now().hour
        part = "בוקר טוב" if 5 <= h < 12 else "צהריים טובים" if h < 17 else "ערב טוב" if h < 22 else "לילה טוב"
        snd = None
        try:
            if CFG.get("intro_music", True):
                snd, greet_at = play_intro(self.speaker.pygame)
                time.sleep(greet_at)
                if snd:
                    snd.set_volume(0.45)              # music keeps playing, a little quieter, under the voice
            self.speaker.say(f"{part}, {CFG['user_name']}. כל המערכות פועלות. אני לשירותך.")
        except Exception:
            log.exception("greeting")
        finally:
            if snd:
                snd.fadeout(3500)                     # and only now it slowly disappears

    def welcome(self):
        """The user opened JARVIS (desktop icon / tray) while it was running hidden: loading screen + music."""
        ui.show_window()
        if time.time() - getattr(self, "_last_welcome", 0) < 20 or self.speaker.pygame.mixer.music.get_busy():
            return
        self._last_welcome = time.time()
        ui.js("replayBoot")
        threading.Thread(target=self.intro_and_greet, daemon=True).start()

    def open_signal_loop(self):
        while True:
            try:
                if OPEN_SIGNAL.exists():
                    OPEN_SIGNAL.unlink()
                    self.welcome()
            except Exception as e:
                log.warning(f"open signal: {e}")
            time.sleep(0.5)

    def context_line(self, source="pc"):
        """One line of facts the brain gets with every message, so it knows the time and what the user is looking at."""
        n = datetime.now()
        parts = [f"יום {HE_DAYS[n.weekday()]} {n.day}.{n.month}.{n.year}", f"השעה {n:%H:%M}"]
        if source == "phone":
            parts.append("ההודעה נשלחה מהאייפון")
        elif source == "task":
            parts.append("משימה מתוזמנת - המשתמש לא בהכרח ליד המחשב")
        win = getattr(self, "last_window", "")
        if win:
            parts.append("החלון הפעיל במחשב: " + win[:90])
        if MEETING.on:
            parts.append("מקשיבה כרגע לפגישה")
        return "[" + " | ".join(parts) + "]"

    def run_task(self, ev):
        """A task the user scheduled ahead of time: do it quietly and send the result to the iPhone."""
        label = ev.get("label") or "משימה"
        try:
            ui.action(f"🗓 משימה מתוזמנת: {label}")
            with self.brain_lock:
                ans = self.ask_brain("זו משימה שביקשתי ממך מראש לבצע עכשיו בעצמך. בצעי אותה וכתבי לי את התוצאה בקצרה "
                                     "(היא נשלחת לטלפון שלי ולא מוקראת בקול):\n" + ev["task"], source="task")
            step_back()
            ans = strip_niqqud(re.sub(r"\[[^\]]*\]|[*#_`>]", "", ans or "")).strip()
        except Exception as e:
            log.exception("scheduled task")
            ans = f"לא הצלחתי לבצע את המשימה ({str(e)[:80]})."
        notify(f"🗓 {label}\n{ans}", "G.I.L.I - " + label)
        ui.say("jarvis", f"🗓 {label}: {ans}")

    def dictate(self):
        """'מצב הכתבה': everything the user says is typed where the cursor is, until 'סוף הכתבה'."""
        if not self.ears or threading.current_thread() is not getattr(self, "_main_thread", None):
            return "הכתבה אפשר להפעיל רק בקול, ליד המחשב."
        self.speaker.say("מקלידה את מה שתגיד. לשורה חדשה תגיד שורה חדשה, ולסיום תגיד סוף הכתבה.")
        step_aside()
        quiet, words = 0, 0
        while quiet < 2 and not AGENT_STOP.is_set():
            q = self.ears.listen(start_timeout=15)
            if not q:
                quiet += 1
                continue
            quiet = 0
            q = strip_niqqud(q).strip()
            if re.search(r"(סוף|סיום|סיימתי את|עצרי את|תפסיקי את|תפסיק את) ?ה?הכתבה|^סיימתי$", q):
                break
            if re.fullmatch(r"(שורה חדשה|רד שורה|רדי שורה|אנטר)[.!?]?", q):
                _gui().press("enter")
                continue
            if re.fullmatch(r"(מחקי|תמחקי|מחק|תמחק) (את זה|את המשפט|אחורה)[.!?]?", q):
                _gui().hotkey("ctrl", "z")
                continue
            type_text(q + " ")
            words += len(q.split())
            ui.action(f"✍ {q[:60]}")
        step_back()
        return f"סיימתי את ההכתבה, הקלדתי {words} מילים." if words else "לא שמעתי מה להקליד, אז יצאתי ממצב הכתבה."

    def ask_brain(self, text, source="pc"):
        """Ask the chosen brain; if it fails (limit, outage), quietly try the other one if it's set up."""
        AGENT_STOP.clear()
        if not isinstance(self.brain, BasicBrain):
            text = self.context_line(source) + "\n" + text
        try:
            return fix_gender(self.brain.ask(text))
        except Exception as e:
            alt = None
            try:
                if isinstance(self.brain, GroqBrain) and CFG.get("anthropic_api_key"):
                    alt = getattr(self, "_alt_claude", None) or Brain(self.timers)
                    self._alt_claude = alt
                elif not isinstance(self.brain, GroqBrain) and CFG.get("groq_api_key"):
                    alt = getattr(self, "_alt_groq", None) or GroqBrain(self.timers)
                    self._alt_groq = alt
            except Exception:
                alt = None
            if not alt:
                raise
            log.warning(f"brain failed ({e}); trying the other brain")
            return fix_gender(alt.ask(text))

    def build_brain(self):
        if CFG.get("brain_engine") == "groq" and CFG.get("groq_api_key"):
            self.brain = GroqBrain(self.timers)
            ui.status({"brain": "online"})
            return
        if CFG["anthropic_api_key"]:
            try:
                self.brain = Brain(self.timers)
                ui.status({"brain": "online"})
                return
            except Exception as e:
                log.error(f"Claude init failed: {e}")
        self.brain = BasicBrain(self.timers)
        ui.status({"brain": "basic"})

    def start(self):
        global REMINDERS
        REMINDERS = self.reminders = Reminders(self.timers.events)
        threading.Thread(target=MAIL.loop, daemon=True).start()
        threading.Thread(target=CALENDAR.loop, daemon=True).start()
        threading.Thread(target=APPSTORE.loop, daemon=True).start()
        PHONE.answer = self.answer_text
        PHONE.voice = lambda t: self.speaker.synth(t, small=True)
        threading.Thread(target=PHONE.loop, daemon=True).start()
        threading.Thread(target=self.run, daemon=True).start()
        threading.Thread(target=self.stats_loop, daemon=True).start()
        threading.Thread(target=self.timer_loop, daemon=True).start()

    def stats_loop(self):
        try:
            import psutil
        except Exception:
            return
        while True:
            try:
                b = psutil.sensors_battery()
                ui.status({"cpu": psutil.cpu_percent(interval=None), "ram": psutil.virtual_memory().percent,
                           "battery": None if not b else round(b.percent)})
                if OS == "Windows":                    # remember what the user is working in (not Gili's own window)
                    title = _fg_window()[1].strip()
                    if title and title != "G.I.L.I":
                        self.last_window = title
            except Exception:
                pass
            time.sleep(2)

    def timer_loop(self):
        while True:
            ev = self.timers.events.get()
            if isinstance(ev, str):
                ev = {"text": ev}
            if ev.get("task"):                         # something she was asked to do by herself at this time
                threading.Thread(target=self.run_task, args=(ev,), daemon=True).start()
                continue
            if ev.get("notify"):
                notify(ev["notify"])
            popped = not getattr(ui, "visible", True) and ui.show_window()
            if self.speaker:
                self.speaker.say(ev["text"])
            if popped and CFG.get("auto_hide", True):
                self.schedule_hide()

    def quick_command(self, text):
        """Open/close commands are done directly - instant, free and reliable."""
        import re
        u = CFG["user_name"]
        s = text.strip().rstrip(".!?,").replace("׳", "'").replace("’", "'").replace("`", "'")
        s = re.sub(r"^(היי |הי |הֵיי )?ג'?ארוויס[, ]*|^(היי |הי )?ג'רוויס[, ]*|^(היי |הי )?גילי[, ]*", "", s)
        s = re.sub(r"^(בבקשה |תוכל |אתה יכול |תעשה לי טובה )", "", s).replace(" בבקשה", "").strip()
        if re.search(r"(תתעדכ[ןנ]|תעדכני? את עצמך|עדכני? את עצמך|תבדקי עדכונים|תבדוק עדכונים|בדוק עדכונים|בדקי עדכונים|תתקיני? עדכון)", s):
            ui.action("בודקת עדכונים")
            r = do_update()
            if not r.get("ok"):
                return f"לא הצלחתי להגיע לשרת העדכונים, {u}."
            if not r.get("updated"):
                return f"אני כבר בגרסה העדכנית ביותר, {u}."
            self.speaker.say(f"מתקינה את גרסה {r['version']}. אחזור בעוד רגע.")
            restart_jarvis(r.get("installer", False))

        if re.search(r"(תקשיבי|תקשיב|תאזיני|תאזין|תתעדי|תתעד|תקליטי|תקליט)\s+(לנו\s+|לי\s+)?(ל|את ה)(פגישה|שיחה|ישיבה)(\s+(הזאת|הזו|שלנו|שלי))?$|^מצב פגישה$", s):
            if not self.ears:
                return "המיקרופון לא זמין, אז אני לא יכולה להקשיב לפגישה."
            if MEETING.on:
                return f"אני כבר מקשיבה לפגישה, {u}."
            MEETING.start()
            ui.toast("מקשיבה לפגישה. לסיום: \"היי גילי, מה סיכמנו?\"")
            return "מקשיבה בשקט. כשתסיימו תגיד: היי גילי, מה סיכמנו."
        if MEETING.on and re.search(r"מה סיכמנו|תסכמי|תסכם|סיימנו|נגמרה|הסתיימה|תפסיקי להקשיב|תפסיק להקשיב|סוף פגישה", s):
            return self.end_meeting()
        if re.search(r"(תלמדי|תלמד|תכירי|תכיר|תזהי|תזהה) את הקול שלי|(תקשיבי|תקשיב) רק לי|(תפעילי|תפעיל|הפעילי|הפעל) (את )?נעילת (ה)?קול", s):
            return self.enroll_voice()
        if re.search(r"(בטלי|בטל|תבטלי|תבטל|כבי|תכבי|תכבה|הסירי|תסירי) (את )?נעילת (ה)?קול|(תקשיבי|תקשיב) לכולם", s):
            if not CFG.get("voice_lock"):
                return f"נעילת הקול לא פעילה, {u}."
            CFG["voice_lock"] = False
            save_config()
            return f"ביטלתי את נעילת הקול. עכשיו אני מקשיבה לכולם, {u}."

        m = re.search(r"(תעברי|תעבור|עברי|עבור|תשתמשי|תשתמש|תחליפי|תחליף)\s+(ל|ב)?(מוח\s+)?(של\s+|ה)?(קלוד|claude|גרוק|groq|חינמי)", s, re.I)
        if m:
            want = "groq" if m.group(5).lower() in ("גרוק", "groq", "חינמי") else "claude"
            if want == "claude" and not CFG.get("anthropic_api_key"):
                return f"אין לי עדיין מפתח של Claude, {u}. תוסיף אותו בהגדרות, בלשונית מוח, ואעבור מיד."
            if want == "groq" and not CFG.get("groq_api_key"):
                return f"אין לי מפתח של Groq, {u}. תוסיף אותו בהגדרות, בלשונית מוח."
            CFG["brain_engine"] = want
            save_config()
            self.build_brain()
            return f"עברתי למוח של {'Claude' if want == 'claude' else 'Groq'}, {u}."
        if re.search(r"(באיזה|איזה) מוח", s):
            name = "Claude" if isinstance(self.brain, Brain) else "Groq" if isinstance(self.brain, GroqBrain) else "המצב הבסיסי"
            return f"אני עובדת עכשיו עם המוח של {name}, {u}."
        if re.fullmatch(r"(מצב הכתבה|הכתבה|(תתחילי|תתחיל|תפעילי|תפעיל) (מצב )?הכתבה|(תכתבי|תכתוב|תקלידי|תקליד) (את )?מה שאני אומר)", s):
            return self.dictate()
        if re.fullmatch(r"(תעתיקי?|העתיקי|העתק|תשלחי?|שלחי) (את זה |את הטקסט )?(לטלפון|לאייפון)", s):
            r = copy_to_phone("")
            return f"שלחתי לטלפון, {u}." if r.get("ok") else r["error"]
        if re.fullmatch(r"(תבטלי?|בטלי?|תעצרי|תעצור|עצרי|עצור) (את ה)?כיבוי( המחשב)?", s):
            power("cancel")
            return f"ביטלתי את הכיבוי, {u}."
        m = re.match(r"^(?:תפעילי?|הפעילי|הפעל|תריצי?)\s+(?:את\s+)?(?:ה?שגרה\s+)?(.+)$", s)
        key = find_routine(m.group(1)) if m else (s if s in routines() else None)
        if key:
            r = run_routine(key)
            return r.get("say") or f"הפעלתי את '{key}', {u}."

        def he_name(raw):
            n = raw.strip()
            while True:
                m2 = re.match(r"^(את|לי|גם)\s+(.+)$", n)
                if not m2:
                    break
                n = m2.group(2).strip()
            n = re.sub(r"\s+(בבקשה|עכשיו)$", "", n)
            if n in HE_APPS or n in HE_SITES:
                return n
            if n.startswith("ה") and (n[1:] in HE_APPS or n[1:] in HE_SITES):
                return n[1:]
            return n

        m = re.match(r"^(תנגני|תנגן|נגני|נגן|תשמיעי|תשמיע|השמיעי|השמע|תשימי לי|תשים לי|שימי לי|שים לי|תפעילי לי את השיר|תפעיל לי את השיר|תפעילי את השיר|תפעיל את השיר)\s+(?:לי\s+)?(?:את\s+)?(?:השיר\s+)?(.+?)(?:\s+ביוטיוב)?$", s)
        if m and len(m.group(2)) > 1:
            song = m.group(2)
            ui.action(f"מנגנת: {song}")
            r = play_youtube(song)
            if r.get("played"):
                return f"מנגנת: {r['title']}." if r.get("title") else f"מנגנת, {u}."
            return f"פתחתי לך תוצאות ביוטיוב עבור {song}."
        m = re.match(r"^(תסגור|סגור|תסגרי|סגרי|תצא מ|צא מ)\s*(.+)$", s)
        if m:
            name = he_name(m.group(2))
            latin = re.fullmatch(r"[A-Za-z0-9 .+-]+", name) is not None
            if name in ("המחשב", "מחשב", "עצמך", "ג'רוויס", "גילי") or (name not in HE_APPS and not latin):
                return None          # Claude handles (asks for confirmation / finds the window)
            ui.action(f"סוגרת: {name}")
            r = close_app(HE_APPS.get(name, name))
            if r.get("ok"):
                return f"סגרתי את {name}, {u}."
            ui.action("✗ " + r["error"])
            return None
        m = re.match(r"^(תפתחי|תפתח|פתחי|פתח|תפעילי|תפעיל|הפעילי|הפעל|תריצי|תריץ|תעלי|תעלה)\s+(.+)$", s)
        if m and not re.search(r"( ואז |תיקי|קובץ|אתר )", " " + m.group(2) + " "):
            name = he_name(m.group(2))
            if name in HE_SITES:
                ui.action(f"פותחת אתר: {name}")
                open_website(HE_SITES[name])
                return f"פותחת את {name}, {u}."
            if name in HE_APPS or re.fullmatch(r"[A-Za-z0-9 .+-]+", name):
                ui.action(f"פותחת: {name}")
                r = open_app(HE_APPS.get(name, name))
                if r.get("ok"):
                    return f"פותחת את {name}, {u}."
                ui.action("✗ " + r["error"])
            return None
        return None

    def answer_text(self, text):
        """A request that came from the iPhone app: answer in text (nothing is spoken on the PC)."""
        ui.say("user", "📱 " + text)
        with self.brain_lock:
            ans = None
            try:
                ans = self.quick_command(text)
            except Exception:
                log.exception("quick (phone)")
            if ans is None:
                ans = self.ask_brain(text, source="phone")
        step_back()
        import re as _re
        ans = strip_niqqud(_re.sub(r"\[[^\]]*\]|[*#_`>]", "", ans or "")).strip()
        if ans:
            ui.say("jarvis", ans)
        return ans

    def think(self, text):
        if not hebrew_only(text):
            ui.toast("אני מבין רק עברית.", "warn")
            return False
        ui.say("user", text)
        ui.state("thinking")
        answer = None
        try:
            answer = self.quick_command(text)
        except Exception:
            log.exception("quick")
        if answer is None:
            try:
                with self.brain_lock:
                    answer = self.ask_brain(text)
            except Exception as e:
                log.exception("brain")
                msg = str(e)
                low = msg.lower()
                ui.toast("שגיאה: " + msg[:180], "error")
                if "groq_rate_limit" in low:
                    answer = f"הגעתי למגבלה החינמית של Groq לדקה הזאת, {CFG['user_name']}. תנסה שוב בעוד רגע."
                elif "authentication" in low and "groq" in low:
                    answer = "Groq דחה את המפתח. תבדוק אותו בהגדרות."
                elif "authentication" in low or "401" in msg or "api key" in low:
                    answer = "Claude דחה את החיבור. תבדוק את המפתח בהגדרות."
                elif "credit" in low or "billing" in low:
                    answer = "נראה שנגמר הקרדיט בחשבון של Claude."
                elif "connect" in low or "timeout" in low or "network" in low:
                    answer = "אני לא מצליח להגיע לשרתים כרגע. תבדוק את החיבור לאינטרנט."
                else:
                    answer = f"משהו השתבש אצלי. השגיאה מופיעה על המסך, {CFG['user_name']}."
        step_back()
        if answer:   # never read out brackets, markdown or links
            import re as _re
            answer = _re.sub(r"\[[^\]]*\]|https?://\S+|[*#_`>]", "", answer)
            answer = strip_niqqud(_re.sub(r"\s{2,}", " ", answer)).strip()
        if not answer or "IGNORE" in answer:
            log.info(f"ignored: {text}")
            ui.state("idle")
            return False
        self.speaker.say(answer)
        return True

    def wait_trigger(self):
        """Blocks until: wake word, mic button, or typed text."""
        if self.ears:
            self.ears.flush()
        ui.state("idle")
        while True:
            if not self.text_q.empty():
                return "text", self.text_q.get()
            if self.ptt.is_set():
                self.ptt.clear()
                if self.ears:
                    return "voice", None
            if self.ears:
                said = self.ears.heard_gili(self.ears.read())
                if said is None:
                    said = self.ears.async_wake()          # called during a meeting (checked in the background)
                if said == VoiceID.DENIED:
                    self.deny()
                    continue
                if said is not None:
                    return "wake", said
            else:
                time.sleep(0.1)

    def deny(self):
        """Someone who isn't the owner called her."""
        ui.toast("קול לא מזוהה - אני מקשיבה רק לבעל הבית.", "warn")
        if time.time() - getattr(self, "_last_deny", 0) > 20:      # say it once, not to every sentence on TV
            self._last_deny = time.time()
            self.speaker.say("סליחה, אני מקשיבה רק לבעל הבית.", show=False)
        self.ears.flush()

    def hear(self, timeout, locked):
        """One spoken sentence; with the voice lock on, sentences from other people are dropped."""
        q = self.ears.listen(start_timeout=timeout)
        if q and locked and not VOICE.allowed(getattr(self.ears, "last_pcm", None)):
            ui.toast("קול לא מזוהה - התעלמתי.", "warn")
            log.info(f"voice lock: ignored '{q}'")
            return None
        return q

    def enroll_voice(self):
        """'תלמדי את הקול שלי': three short recordings -> a voice print kept on this PC."""
        u = CFG["user_name"]
        if not self.ears or threading.current_thread() is not getattr(self, "_main_thread", None):
            return "את זה צריך לבקש בקול, ליד המחשב."
        try:
            ui.action("טוענת את זיהוי הקול")
            VOICE.ready()
        except Exception as e:
            log.exception("voice model")
            return f"לא הצלחתי לטעון את זיהוי הקול, {u}. תבדוק שיש אינטרנט ותנסה שוב."
        lines = ["היי גילי, מה מזג האוויר היום?", "היי גילי, תפתחי לי בבקשה את כרום.",
                 "אחת, שתיים, שלוש, ארבע, חמש, שש, שבע."]
        self.speaker.say("בשמחה. אחרי כל צפצוף תגיד את המשפט שמופיע על המסך, בקול הרגיל שלך.")
        clips = []
        for line in lines:
            ui.say("jarvis", "תגיד: " + line)
            for attempt in range(2):
                self.ears.beep()
                self.ears.listen(start_timeout=8)
                pcm = getattr(self.ears, "last_pcm", None)
                if pcm is not None and len(pcm) > SAMPLE_RATE * 1.2:
                    clips.append(pcm)
                    break
                self.speaker.say("לא שמעתי טוב. עוד פעם.")
        ok, why = VOICE.enroll(clips)
        if not ok:
            return ("היה רעש מדי ולא הצלחתי ללמוד את הקול. תנסה שוב במקום שקט." if why == "noisy"
                    else "לא שמעתי מספיק כדי ללמוד את הקול. תנסה שוב.")
        CFG["voice_lock"] = True
        save_config()
        return f"למדתי את הקול שלך, {u}. מעכשיו אני מקשיבה רק לך. כדי לבטל תגיד: בטלי נעילת קול."

    def end_meeting(self):
        u = CFG["user_name"]
        mins = int((time.time() - MEETING.started) / 60)
        pool = getattr(self.ears, "_meet_pool", None) if self.ears else None
        if pool:                                           # let the last sentence finish being written down
            try:
                pool.submit(lambda: None).result(20)
            except Exception:
                pass
        transcript = MEETING.stop()
        if len(transcript) < 80:
            return f"כמעט לא שמעתי דיבור בפגישה, {u}, אז אין לי מה לסכם."
        ui.action(f"מסכמת את הפגישה ({mins} דקות)")
        try:
            full, short = MEETING.summarize(transcript)
        except Exception as e:
            log.exception("meeting summary")
            path = MEETING.save("", transcript)
            return f"לא הצלחתי לסכם כרגע, אבל שמרתי את התמלול המלא במסמכים, בתיקייה Gili. השם שלו {path.stem}."
        path = MEETING.save(full, transcript)
        notify("סיכום פגישה\n" + full, "G.I.L.I - סיכום פגישה")
        for b in (self.brain, getattr(self, "_alt_claude", None), getattr(self, "_alt_groq", None)):
            try:                                           # so "תוסיפי את זה ליומן" right afterwards just works
                if b is not None and hasattr(b, "history") and (not b.history or b.history[-1]["role"] == "assistant"):
                    b.history.append({"role": "user", "content": "[סיכום הפגישה שהקשבת לה עכשיו]\n" + full[:2500]})
                    b.history.append({"role": "assistant", "content": short})
                    b.last_used = time.time()
            except Exception:
                pass
        return f"{short} שמרתי את הסיכום המלא ואת התמלול במסמכים, ושלחתי לך את הסיכום לטלפון."

    def run(self):
        self._main_thread = threading.current_thread()
        ui.ready.wait(30)
        try:
            self.speaker = Speaker()
        except Exception as e:
            log.exception("speaker")
            ui.toast(f"בעיה ברמקול: {e}", "error")
            return
        self.build_brain()
        try:
            self.ears = Ears()
            ui.status({"mic": "online"})
        except Exception as e:
            log.exception("ears")
            ui.status({"mic": "offline"})
            ui.toast("המיקרופון לא זמין. אפשר להקליד פקודות.", "error")
        if not BACKGROUND_START:
            self._last_welcome = time.time()
            self.intro_and_greet()
        threading.Thread(target=self.open_signal_loop, daemon=True).start()
        def bg_check():
            info = check_update()
            if info.get("available"):
                ui.js("updateAvailable", info["latest"])
                ui.toast(f"יש עדכון {info['latest']} - לחץ על 'עדכון' או תגיד 'תתעדכן'.")
        threading.Thread(target=bg_check, daemon=True).start()

        while True:
            kind, payload = self.wait_trigger()
            popped = False
            if kind in ("voice", "wake") and not getattr(ui, "visible", True):
                popped = ui.show_window()            # woken while hidden -> show the window
            self.cancel_hide()
            if kind == "text":
                self.think(payload)
                continue
            said_now = payload if kind == "wake" else ""
            locked = kind == "wake"            # the mic button is pressed by hand, so it needs no voice check
            if not said_now:
                self.ears.beep()
            # Conversation mode: after "Hey Jarvis" keep listening; it ends after a quiet stretch or "זהו".
            window = float(CFG.get("follow_up_seconds", 25))
            last = time.time()
            q = said_now or self.hear(8, locked)   # "היי גילי, תפתח כרום" in one breath works too
            ui.js("conversation", True)
            while True:
                if q:
                    if any(p in q for p in STOP_PHRASES) and len(q.split()) <= 4:
                        self.speaker.say(f"בשמחה, {CFG['user_name']}.")
                        break
                    if self.think(q):
                        last = time.time()           # a real exchange restarts the window
                if not self.text_q.empty():
                    break
                left = window - (time.time() - last)
                if left <= 0.5:
                    break
                q = self.hear(left, locked)
            ui.js("conversation", False)
            if popped and CFG.get("auto_hide", True):
                self.schedule_hide()

    def schedule_hide(self, delay=12):
        self.cancel_hide()
        self._hide_timer = threading.Timer(delay, ui.hide_window)
        self._hide_timer.daemon = True
        self._hide_timer.start()

    def cancel_hide(self):
        t = getattr(self, "_hide_timer", None)
        if t:
            t.cancel()


assistant = Assistant()

# ======================================================================= JS API (called from the HUD)
def _mask(k):
    return ("•" * 8 + k[-4:]) if k else ""


class Api:
    def ui_ready(self):
        ui.flush()
        return self.get_settings()

    def get_settings(self):
        return {
            "has_claude": bool(CFG["anthropic_api_key"]), "claude_mask": _mask(CFG["anthropic_api_key"]),
            "brain_engine": CFG.get("brain_engine", "claude"), "stt_engine": CFG.get("stt_engine", "google"),
            "has_groq": bool(CFG.get("groq_api_key")), "groq_mask": _mask(CFG.get("groq_api_key", "")),
            "pause": CFG.get("pause_seconds", 1.4),
            "bg": bool(CFG.get("run_in_background", True)), "autostart": bool(CFG.get("start_with_windows", True)),
            "auto_hide": bool(CFG.get("auto_hide", True)),
            "voice_lock": bool(CFG.get("voice_lock")), "voice_enrolled": VOICE.enrolled(),
            "mail_address": CFG.get("mail_address", ""), "has_mail_pw": bool(CFG.get("mail_app_password")),
            "mail_alerts": bool(CFG.get("mail_alerts", True)),
            "voice": CFG["voice"], "tts_engine": CFG.get("tts_engine", "edge"),
            "has_el": bool(CFG.get("el_api_key")), "el_voice_id": CFG.get("el_voice_id", ""),
            "el_voice_name": CFG.get("el_voice_name", ""),
            "el_female": bool(CFG.get("el_female", True)), "el_mood": CFG.get("el_mood", "neutral"),
            "has_gemini": bool(CFG.get("gemini_api_key")), "gemini_mask": _mask(CFG.get("gemini_api_key", "")),
            "gemini_voice": CFG.get("gemini_voice", "Charon"),
            "user_name": CFG["user_name"], "wake_threshold": CFG["wake_threshold"],
            "self_gender": CFG.get("self_gender", "female"), "user_gender": CFG.get("user_gender", "male"),
            "routines": routines_text(), "routine_help": ROUTINE_HELP,
            "has_calendar": CALENDAR.configured(), "calendar_alerts": bool(CFG.get("calendar_alerts", True)),
            "asc_issuer": CFG.get("asc_issuer", ""), "asc_key_id": CFG.get("asc_key_id", ""),
            "asc_vendor": CFG.get("asc_vendor", ""), "asc_app_name": CFG.get("asc_app_name", ""),
            "has_asc_key": bool(CFG.get("asc_key")), "asc_alerts": bool(CFG.get("asc_alerts", True)),
            "intro_music": bool(CFG.get("intro_music", True)), "intro_mode": CFG.get("intro_mode", "builtin"),
            "intro_file": CFG.get("intro_file", ""),
        }

    def save_settings(self, s):
        if s.get("claude_key", "").strip():
            CFG["anthropic_api_key"] = s["claude_key"].strip()
        if s.get("voice"):
            CFG["voice"] = s["voice"]
        if s.get("pause"):
            CFG["pause_seconds"] = max(0.6, min(3.5, float(s["pause"])))
        for k_ui, k_cfg in (("bg", "run_in_background"), ("autostart", "start_with_windows"), ("auto_hide", "auto_hide")):
            if k_ui in s:
                CFG[k_cfg] = bool(s[k_ui])
        if "voice_lock" in s:
            CFG["voice_lock"] = bool(s["voice_lock"]) and VOICE.enrolled()
        if "autostart" in s:
            threading.Thread(target=set_start_with_windows, args=(bool(s["autostart"]),), daemon=True).start()
        if s.get("groq_key", "").strip():
            CFG["groq_api_key"] = s["groq_key"].strip()
        if s.get("brain_engine") in ("claude", "groq"):
            CFG["brain_engine"] = s["brain_engine"]
        if s.get("stt_engine") in ("google", "groq"):
            CFG["stt_engine"] = s["stt_engine"]
        if s.get("tts_engine") in ("edge", "gemini", "eleven"):
            CFG["tts_engine"] = s["tts_engine"]
        if s.get("el_key", "").strip():
            CFG["el_api_key"] = s["el_key"].strip()
            Speaker._el_left = None
        if s.get("el_mood") in ("happy", "excited", "calm", "neutral"):
            CFG["el_mood"] = s["el_mood"]
        if s.get("el_voice_id"):
            CFG["el_voice_id"] = s["el_voice_id"]
            CFG["el_voice_name"] = s.get("el_voice_name", "")
            CFG["el_female"] = bool(s.get("el_female", True))
        if s.get("gemini_key", "").strip():
            CFG["gemini_api_key"] = s["gemini_key"].strip()
        if s.get("gemini_voice"):
            CFG["gemini_voice"] = s["gemini_voice"]
        if s.get("user_name", "").strip():
            CFG["user_name"] = s["user_name"].strip()
        if s.get("wake_threshold"):
            CFG["wake_threshold"] = float(s["wake_threshold"])
        if s.get("calendar_ics", "").strip():
            CFG["calendar_ics"] = s["calendar_ics"].strip()
            CALENDAR.cache = None
        for k in ("calendar_alerts", "asc_alerts"):
            if k in s:
                CFG[k] = bool(s[k])
        for k in ("asc_issuer", "asc_key_id", "asc_vendor", "asc_app_name"):
            if k in s and isinstance(s[k], str):
                if CFG.get(k) != s[k].strip():
                    APPSTORE.cache.pop("app_id", None)
                CFG[k] = s[k].strip()
        if s.get("self_gender") in ("female", "male", "auto"):
            CFG["self_gender"] = s["self_gender"]
        if s.get("user_gender") in ("female", "male"):
            CFG["user_gender"] = s["user_gender"]
        if "intro_music" in s:
            CFG["intro_music"] = bool(s["intro_music"])
        if s.get("intro_mode") in ("builtin", "file"):
            CFG["intro_mode"] = s["intro_mode"]
        warn = []
        if "routines" in s:
            parsed = parse_routines(s["routines"])
            ok_start = ("פתח ", "תפתח ", "סגור", "תסגור ", "אתר ", "נגן ", "תנגן ", "עוצמה ", "השתק", "נעל", "שינה",
                        "כבה", "המתן ", "תגיד ")
            warn = [line for steps in parsed.values() for line in steps if not line.startswith(ok_start)]
            CFG["routines"] = parsed
        save_config()
        result = {"ok": True, "claude": None, "groq": None, "engine": CFG.get("brain_engine", "claude"),
                  "routine_warn": warn}
        if CFG.get("groq_api_key"):
            try:
                import httpx
                r = httpx.get("https://api.groq.com/openai/v1/models",
                              headers={"Authorization": f"Bearer {CFG['groq_api_key']}"}, timeout=15)
                result["groq"] = "ok" if r.status_code == 200 else f"{r.status_code} {r.text[:120]}"
            except Exception as e:
                result["groq"] = str(e)[:200]
        if CFG["anthropic_api_key"] and CFG.get("brain_engine") != "groq":
            try:
                import anthropic
                anthropic.Anthropic(api_key=CFG["anthropic_api_key"]).messages.create(
                    model=CFG["claude_model"], max_tokens=5, messages=[{"role": "user", "content": "hi"}])
                result["claude"] = "ok"
            except Exception as e:
                result["claude"] = str(e)[:200]
        assistant.build_brain()
        return result

    def phone_pair(self, regenerate=False):
        if regenerate:
            PHONE.new_secret()
        try:
            qr = PHONE.qr_data_uri()
        except Exception as e:
            qr = ""
            log.error(f"qr: {e}")
        return {"url": PHONE.pair_url(), "qr": qr, "code": CFG["phone_secret"]}


    def copy_text(self, text):
        try:
            import pyperclip
            pyperclip.copy(text)
            return True
        except Exception:
            return False

    def mail_test(self):
        if not MAIL.configured():
            return {"ok": False, "error": "צריך כתובת מייל וסיסמת אפליקציה."}
        try:
            mails = MAIL.fetch_unread(limit=5)
            if not MAIL.seeded:
                MAIL.check()        # remember the current backlog so it won't be reported
            return {"ok": True, "count": len(mails)}
        except Exception as e:
            msg = str(e)
            if "AUTHENTICATIONFAILED" in msg.upper() or "invalid credentials" in msg.lower():
                msg = "גוגל דחה את הסיסמה. צריך סיסמת אפליקציה (16 אותיות), לא הסיסמה הרגילה."
            return {"ok": False, "error": msg[:200]}

    def send_text(self, text):
        if text and text.strip():
            assistant.text_q.put(text.strip())

    def push_to_talk(self):
        if assistant.speaker:
            assistant.speaker.stop()
        assistant.ptt.set()

    def stop_speaking(self):
        AGENT_STOP.set()                       # also ends a task she is doing on the screen
        if assistant.speaker:
            assistant.speaker.stop()

    def test_voice(self):
        """Speak a test line and report exactly what happened (shown in settings)."""
        sp = assistant.speaker
        if not sp:
            return {"ok": False, "error": "הרמקול עדיין לא מוכן, נסה שוב בעוד כמה שניות."}
        text = f"לשירותך, {CFG['user_name']}. איך אני {'נשמעת' if speaks_as_female() else 'נשמע'}?"
        engine = CFG.get("tts_engine")
        if engine == "eleven":
            if not CFG.get("el_api_key"):
                return {"ok": False, "error": "חסר מפתח של ElevenLabs."}
            try:
                left = sp.eleven_credits(force=True)
                audio = sp._eleven(text)
            except QuotaOut:
                audio = sp.free_synth(text)
                threading.Thread(target=sp.play_bytes, args=(audio, text), daemon=True).start()
                return {"ok": True, "message": "נגמרו הקרדיטים — מדבר בקול החינמי עד שיתחדשו."}
            except Exception as e:
                return {"ok": False, "error": str(e)[:200]}
            threading.Thread(target=sp.play_bytes, args=(audio, text), daemon=True).start()
            return {"ok": True, "message": f"✔ ElevenLabs עובד. נשארו בערך {max(0, left - len(text)):,} קרדיטים (~{max(0, left - len(text)) // 70} תשובות)."}
        if engine == "gemini":
            if not CFG.get("gemini_api_key"):
                return {"ok": False, "error": "חסר מפתח של Gemini."}
            try:
                audio = sp._gemini(text)
            except Exception as e:
                return {"ok": False, "error": str(e)}
            threading.Thread(target=sp.play_bytes, args=(audio, text), daemon=True).start()
            return {"ok": True, "message": f"✔ Gemini עובד ({CFG.get('gemini_model')})."}
        threading.Thread(target=sp.say, args=(text,), daemon=True).start()
        return {"ok": True, "message": "✔ הקול הרגיל עובד."}

    def open_link(self, url):
        open_in_chrome(url)

    def version(self):
        return VERSION

    def win(self, action):
        w = ui.window
        try:
            if action == "min":
                w.minimize()
            elif action == "max":
                if getattr(self, "_maxed", False):
                    w.restore()
                else:
                    w.maximize()
                self._maxed = not getattr(self, "_maxed", False)
            elif action == "close":
                if CFG.get("run_in_background", True):
                    ui.hide_window()              # keeps listening for "Hey Jarvis"
                    ui.toast("גילי ממשיכה להקשיב ברקע")
                else:
                    os._exit(0)
        except Exception as e:
            log.error(f"window action {action}: {e}")

    def el_voices(self, key=""):
        import httpx
        k = (key or "").strip() or CFG.get("el_api_key", "")
        if not k:
            return {"ok": False, "error": "צריך להדביק מפתח"}
        try:
            r = httpx.get("https://api.elevenlabs.io/v1/voices", headers={"xi-api-key": k}, timeout=20)
            if r.status_code == 401:
                return {"ok": False, "error": "המפתח לא תקין (או שאין לו הרשאת Voices)"}
            r.raise_for_status()
            out = [{"id": v["voice_id"], "name": v.get("name", ""),
                    "female": (v.get("labels") or {}).get("gender", "") == "female",
                    "desc": " ".join(x for x in ((v.get("labels") or {}).get("accent", ""),
                                                 (v.get("labels") or {}).get("description", "")) if x)}
                   for v in r.json().get("voices", [])]
            return {"ok": True, "voices": sorted(out, key=lambda v: (not v["female"], v["name"]))}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def el_search(self, query, key=""):
        """Search ElevenLabs' public Voice Library (e.g. Hebrew voices like 'Danit')."""
        import httpx
        k = (key or "").strip() or CFG.get("el_api_key", "")
        if not k:
            return {"ok": False, "error": "צריך להדביק מפתח"}
        q = (query or "").strip()
        tries = [{"search": q, "language": "he"}, {"search": q}] if q else [{"language": "he"}]
        HE2EN = {"דנית": "Danit", "נועה": "Noa", "מאיה": "Maya", "שירה": "Shira", "יעל": "Yael", "טל": "Tal",
                 "רוני": "Roni", "מיכל": "Michal", "איתן": "Eitan", "נועם": "Noam", "דניאל": "Daniel"}
        if q in HE2EN:
            tries += [{"search": HE2EN[q], "language": "he"}, {"search": HE2EN[q]}]
        seen, out = set(), []
        try:
            for params in tries:
                r = httpx.get("https://api.elevenlabs.io/v1/shared-voices", headers={"xi-api-key": k},
                              params=dict(params, page_size=30), timeout=20)
                if r.status_code == 401:
                    return {"ok": False, "error": "למפתח אין הרשאת Voices"}
                r.raise_for_status()
                for v in r.json().get("voices", []):
                    if v["voice_id"] in seen:
                        continue
                    seen.add(v["voice_id"])
                    out.append({"id": v["voice_id"], "owner": v.get("public_owner_id", ""), "name": v.get("name", ""),
                                "female": v.get("gender") == "female", "lang": v.get("language", ""),
                                "accent": v.get("accent", ""), "desc": (v.get("description") or "")[:90],
                                "preview": v.get("preview_url", ""), "rate": v.get("rate") or 1})
                if len(out) >= 8:
                    break
            out.sort(key=lambda v: (v["lang"] != "he", not v["female"]))
            return {"ok": True, "voices": out[:30]}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def el_add(self, owner, voice_id, name, female, key=""):
        """Add a Voice Library voice to the user's account and make it JARVIS's voice."""
        import httpx
        k = (key or "").strip() or CFG.get("el_api_key", "")
        try:
            r = httpx.post(f"https://api.elevenlabs.io/v1/voices/add/{owner}/{voice_id}", headers={"xi-api-key": k},
                           json={"new_name": name}, timeout=30)
            if r.status_code >= 400 and "already" not in r.text.lower():
                return {"ok": False, "error": f"ElevenLabs: {r.text[:200]}"}
            new_id = (r.json() or {}).get("voice_id", voice_id) if r.status_code < 400 else voice_id
            if (key or "").strip():
                CFG["el_api_key"] = key.strip()
            CFG.update(el_voice_id=new_id, el_voice_name=name, el_female=bool(female), tts_engine="eleven")
            save_config()
            return {"ok": True, "id": new_id, "name": name}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def memory_list(self):
        return MEMORY.listing()["facts"]

    def memory_forget(self, id):
        MEMORY.forget([int(id)])
        return MEMORY.listing()["facts"]

    def memory_add(self, text):
        if text and text.strip():
            MEMORY.remember(text.strip())
        return MEMORY.listing()["facts"]

    def cal_test(self):
        if not CALENDAR.configured():
            return {"ok": False, "error": "צריך להדביק את הכתובת הסודית של היומן."}
        try:
            CALENDAR.cache = None
            r = calendar_events("week")
            return {"ok": True, "count": len(r["events"])}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def asc_pick_key(self):
        import webview
        try:
            r = ui.window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False,
                                             file_types=("Apple key (*.p8)", "All files (*.*)"))
            if not r:
                return {"ok": False}
            text = Path(r[0] if isinstance(r, (list, tuple)) else str(r)).read_text(encoding="utf-8").strip()
            if "BEGIN PRIVATE KEY" not in text:
                return {"ok": False, "error": "זה לא קובץ מפתח של אפל (.p8)"}
            from cryptography.hazmat.primitives import serialization
            serialization.load_pem_private_key(text.encode(), None)
            CFG["asc_key"] = text
            m = re.search(r"AuthKey_([A-Z0-9]{8,12})\.p8", str(r))
            if m and not CFG.get("asc_key_id"):
                CFG["asc_key_id"] = m.group(1)
            save_config()
            return {"ok": True, "key_id": CFG.get("asc_key_id", "")}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def asc_test(self):
        if not APPSTORE.configured():
            return {"ok": False, "error": "צריך למלא Issuer ID, Key ID, מספר ספק (Vendor) ולבחור את קובץ ה־.p8."}
        try:
            st = APPSTORE.stats(1)
            if not APPSTORE.cache.get("reviews_seeded"):
                try:
                    APPSTORE.cache["seen_reviews"] = [r["id"] for r in APPSTORE.reviews(20)]
                    APPSTORE.cache["reviews_seeded"] = True
                    APPSTORE._save()
                except Exception:
                    pass
            return {"ok": True, "downloads": st["downloads"], "purchases": st["purchases"],
                    "period": st.get("period", st.get("note", ""))}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    def pick_intro(self):
        import webview
        try:
            r = ui.window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False,
                                             file_types=("קבצי שמע (*.mp3;*.wav;*.ogg)", "All files (*.*)"))
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if not r:
            return {"ok": False}
        CFG["intro_file"] = r[0] if isinstance(r, (list, tuple)) else str(r)
        CFG["intro_mode"] = "file"
        save_config()
        return {"ok": True, "file": CFG["intro_file"]}

    def test_intro(self, mode=None):
        if mode in ("builtin", "file"):
            CFG["intro_mode"] = mode
            save_config()
        try:
            pg = assistant.speaker.pygame
            for ch in range(pg.mixer.get_num_channels()):
                pg.mixer.Channel(ch).stop()
            snd, greet_at = play_intro(pg)
            if snd:
                threading.Timer(greet_at + 4, lambda: snd.fadeout(3500)).start()
                return {"ok": True}
            return {"ok": False, "error": INTRO_ERROR[0] or "לא ידוע"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def update_now(self):
        r = do_update()
        if r.get("updated"):
            threading.Thread(target=lambda: (time.sleep(1.5), restart_jarvis(r.get("installer", False))),
                             daemon=True).start()
        return r


# ======================================================================= MAIN
def start_tray():
    """Icon next to the clock: open JARVIS / quit."""
    try:
        import pystray
        from PIL import Image
        img = Image.open(str(HERE / "assets" / "jarvis.ico"))

        def open_(icon, item):
            try:
                assistant.welcome()
            except Exception:
                ui.show_window()

        def quit_(icon, item):
            icon.stop()
            os._exit(0)
        icon = pystray.Icon("JARVIS", img, "G.I.L.I - מאזינה ל\"היי גילי\"",
                            menu=pystray.Menu(pystray.MenuItem("פתח את גילי", open_, default=True),
                                              pystray.MenuItem("יציאה", quit_)))
        icon.run_detached()
        global TRAY
        TRAY = icon
        return icon
    except Exception as e:
        log.warning(f"tray icon unavailable: {e}")
        return None


def startup_shortcut_path():
    return Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup/JARVIS.lnk"


def set_start_with_windows(enabled):
    """Create/remove the Startup shortcut that launches JARVIS hidden in the background."""
    if OS != "Windows":
        return
    lnk = startup_shortcut_path()
    try:
        if not enabled:
            if lnk.exists():
                lnk.unlink()
            return
        if FROZEN:
            target, args, workdir = sys.executable, "--background", str(Path(sys.executable).parent)
        else:
            pyw = Path(sys.executable)
            if pyw.name.lower() == "python.exe":
                pyw = pyw.with_name("pythonw.exe")
            target, args, workdir = str(pyw), f'"{HERE / "jarvis.py"}" --background', str(HERE)
        icon = str(HERE / "assets" / "jarvis.ico")
        ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
              "$s.TargetPath='{t}';$s.Arguments='{a}';$s.WorkingDirectory='{w}';"
              "$s.IconLocation='{i}';$s.Save()").format(lnk=str(lnk).replace("'", "''"), t=target.replace("'", "''"),
                                                         a=args.replace("'", "''"), w=workdir.replace("'", "''"),
                                                         i=icon.replace("'", "''"))
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=20,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        log.error(f"startup shortcut: {e}")


def already_running():
    """Only one JARVIS at a time (two would fight over the microphone)."""
    if OS != "Windows":
        return False
    import ctypes
    ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\MrSigal.JARVIS")
    return ctypes.windll.kernel32.GetLastError() == 183      # ERROR_ALREADY_EXISTS


def main():
    import webview
    if already_running():
        if not BACKGROUND_START:
            try:                                   # ask the running JARVIS to open (with the loading screen + music)
                OPEN_SIGNAL.write_text("open", encoding="utf-8")
            except Exception:
                pass
        return
    if OS == "Windows":
        try:   # own taskbar group/icon instead of Python's
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MrSigal.JARVIS")
        except Exception:
            pass
    window = webview.create_window(
        "G.I.L.I", url=str(HERE / "hud.html"), js_api=Api(),
        width=1280, height=780, min_size=(900, 600), background_color="#01060b",
        frameless=True, easy_drag=False, hidden=BACKGROUND_START,
    )
    ui.visible = not BACKGROUND_START
    start_tray()
    set_start_with_windows(bool(CFG.get("start_with_windows", True)))
    ui.window = window
    webview.start(assistant.start, debug=False)
    os._exit(0)


def selftest():
    """Used by the build server: can the packaged program load everything it needs?"""
    out = Path(os.environ.get("JARVIS_SELFTEST_OUT", str(DATA_DIR / "selftest.txt")))
    lines, ok = [], True

    def step(name, fn):
        nonlocal ok
        try:
            fn()
            lines.append(f"OK   {name}")
        except Exception as e:
            ok = False
            lines.append(f"FAIL {name}: {type(e).__name__}: {e}")
    import numpy as np
    step("hud.html", lambda: (HERE / "hud.html").read_text(encoding="utf-8")[:10])
    step("tray icon image", lambda: (HERE / "assets" / "jarvis.ico").read_bytes()[:4])
    step("startup music", lambda: make_intro_wav()[:4] == b"RIFF" or (_ for _ in ()).throw(RuntimeError("bad wav")))

    def _sound_test():
        os.environ["SDL_AUDIODRIVER"] = "dummy"
        import pygame
        pygame.mixer.init()
        snd = pygame.mixer.Sound(file=BytesIO(make_intro_wav()))
        assert snd.get_length() > 5, snd.get_length()
        assert snd.play() is not None, "no channel"
        lines.append(f"     intro sound {snd.get_length():.1f}s mixer={pygame.mixer.get_init()}")
    step("startup music plays", _sound_test)
    step("hebrew gender fix", lambda: fix_gender("אני מוכן ואני לא בטוח") == "אני מוכנה ואני לא בטוחה" or (_ for _ in ()).throw(RuntimeError(fix_gender("אני מוכן ואני לא בטוח"))))
    for mod in ("numpy", "sounddevice", "speech_recognition", "edge_tts", "pygame", "webview", "anthropic",
                "httpx", "psutil", "pyautogui", "pyperclip", "onnxruntime", "comtypes", "pystray", "PIL", "cryptography", "qrcode"):
        step(f"import {mod}", lambda m=mod: __import__(m))
    step("import ddgs", lambda: __import__("ddgs"))

    def wake():
        from openwakeword.model import Model
        Model(wakeword_models=["hey_jarvis"], inference_framework="onnx").predict(np.zeros(CHUNK, dtype=np.int16))
    step("wake word model", wake)

    def vad():
        from openwakeword.vad import VAD
        VAD().predict(np.zeros(CHUNK, dtype=np.int16), frame_size=640)
    step("voice detector", vad)

    def flac():
        import speech_recognition as sr
        sr.AudioData(np.zeros(16000, dtype=np.int16).tobytes(), SAMPLE_RATE, 2).get_flac_data()
    step("flac encoder (Google speech)", flac)
    def spk():
        e = VOICE.embed((np.random.default_rng(1).normal(0, 3000, SAMPLE_RATE * 2)).astype(np.int16))
        assert e is not None and e.shape == (256,) and abs(float(np.linalg.norm(e)) - 1) < 1e-3, getattr(e, "shape", e)
    step("voice lock model", spk)
    def docs():
        import pypdf, zipfile, tempfile
        d = Path(tempfile.mkdtemp())
        with zipfile.ZipFile(d / "t.docx", "w") as z:
            z.writestr("word/document.xml", '<w:document><w:body><w:p><w:r><w:t>שלום עולם</w:t></w:r></w:p></w:body></w:document>')
        assert read_document(str(d / "t.docx"))["text"] == "שלום עולם"
    step("read documents (pdf / docx)", docs)
    if OS == "Windows":
        def uia():
            title, items, text = uia_call(lambda: _uia_scan(root=True), 60)
            lines.append(f"     screen scan: {len(items)} clickable, {len(text)} chars of text")
        step("screen control (UI Automation)", uia)
        step("DPAPI key protection", lambda: (unprotect_secret(protect_secret("test-key-123")) == "test-key-123") or (_ for _ in ()).throw(RuntimeError("round-trip mismatch")))
    step("config dir writable", lambda: (DATA_DIR / "selftest.tmp").write_text("x") or (DATA_DIR / "selftest.tmp").unlink())
    lines.append(f"VERSION {VERSION}  FROZEN {FROZEN}  RESULT {'PASS' if ok else 'FAIL'}")
    out.write_text("\n".join(lines), encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    try:
        main()
    except Exception:
        log.exception("fatal")
        raise
