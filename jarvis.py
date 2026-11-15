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

VERSION = "4.5"
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
    "groq_models": ["openai/gpt-oss-120b", "llama-3.3-70b-versatile", "qwen/qwen3-32b"],
    "stt_engine": "google",                 # google | groq (Whisper)
    "claude_model": "claude-haiku-5-5",   # cheapest Claude model
    "voice": "he-IL-AvriNeural",            # free Hebrew male voice (female: he-IL-HilaNeural)
    "whatsapp_provider": "greenapi",        # greenapi (sends from YOUR WhatsApp) | callmebot
    "greenapi_url": "https://api.green-api.com",
    "greenapi_id": "",
    "greenapi_token": "",
    "whatsapp_phone": "",                   # +972...
    "whatsapp_apikey": "",
    "phone_secret": "",                     # pairing code for the iPhone app (created automatically)
    "phone_link": True,
    "mail_address": "",                     # Gmail address (IMAP)
    "mail_app_password": "",                # Google app password (myaccount.google.com/apppasswords)
    "mail_imap_host": "",                   # empty = detected from the address
    "mail_alerts": True,                    # important new emails -> WhatsApp
    "mail_check_minutes": 3,
    "whatsapp_when_away": True,             # reminders/timers also go to WhatsApp when you're away from the PC
    "away_minutes": 5,
    "run_in_background": True,              # closing the window keeps JARVIS listening (icon near the clock)
    "start_with_windows": True,             # start hidden when the PC starts
    "auto_hide": True,                      # window pops up on "Hey Jarvis" and hides again afterwards
    "tts_engine": "edge",                   # edge (fast, built-in) | gemini (Google, most natural)
    "gemini_api_key": "",                   # free key from aistudio.google.com/apikey
    "gemini_voice": "Charon",
    "gemini_model": "",                     # remembered automatically once one works
    "user_name": "אדוני",
    "wake_threshold": 0.5,
    "follow_up_seconds": 25,                # after "Hey Jarvis", keep talking without saying it again
    "pause_seconds": 1.4,                   # how long a pause means "I finished talking" (bigger = never cuts you off)
    "min_confidence": 0.35,                  # speech below this confidence is ignored (non-English, noise)
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception as e:
            log.error(f"config.json unreadable: {e}")
    for old in ("elevenlabs_api_key", "elevenlabs_voice_id", "elevenlabs_voice_name",
                "elevenlabs_model", "speech_recognition"):
        cfg.pop(old, None)                                  # ElevenLabs was removed
    if "fallback_voice" in cfg:
        cfg["voice"] = cfg.pop("fallback_voice")
    for old in ("vs_url", "vs_model", "vs_voice"):
        cfg.pop(old, None)                                  # VoiceStudio was removed
    if cfg.get("tts_engine") not in ("edge", "gemini"):
        cfg["tts_engine"] = "edge"
    if not str(cfg.get("voice", "")).startswith("he-IL"):     # v3: Hebrew
        cfg["voice"] = "he-IL-AvriNeural"
    if cfg.get("user_name", "").lower() in ("", "sir"):
        cfg["user_name"] = "אדוני"
    if cfg.get("min_confidence", 0.35) > 0.35:
        cfg["min_confidence"] = 0.35                       # Hebrew gets lower confidence scores
    if not cfg.get("phone_secret"):
        import secrets
        cfg["phone_secret"] = secrets.token_urlsafe(18)
    if cfg.get("follow_up_seconds", 25) < 15:              # v4.3: conversation mode
        cfg["follow_up_seconds"] = 25
    if cfg.get("claude_model") == "claude-sonnet-5-5":   # move old installs to the cheap model
        cfg["claude_model"] = "claude-haiku-5-5"
    return cfg


def save_config():
    CONFIG_FILE.write_text(json.dumps(CFG, ensure_ascii=False, indent=2), encoding="utf-8")


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
                hwnd = ctypes.windll.user32.FindWindowW(None, "J.A.R.V.I.S")
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

# ======================================================================= VOICE OUT
class Speaker:
    def __init__(self):
        import pygame
        self.pygame = pygame
        pygame.mixer.init()
        self.lock = threading.Lock()
        self._stop = False

    # ---- free Microsoft neural voice
    def _edge(self, text):
        import edge_tts

        async def run():
            buf = BytesIO()
            async for chunk in edge_tts.Communicate(text, CFG["voice"], rate="+4%").stream():
                if chunk["type"] == "audio":
                    buf.write(chunk["data"])
            return buf.getvalue()
        return asyncio.run(run())

    # ---- Google Gemini TTS (free API key from Google AI Studio)
    GEMINI_MODELS = ["gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts",
                     "gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts"]

    def _gemini(self, text):
        import httpx, base64, re
        key = CFG["gemini_api_key"].strip()
        prompt = text          # only the words to say - any instruction here gets read aloud
        body = {"contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseModalities": ["AUDIO"],
                                     "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {
                                         "voiceName": CFG.get("gemini_voice") or "Charon"}}}}}
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

    def synth(self, text):
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
            pieces = self.split_for_speech(text)
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
                    ui.say("jarvis", text)          # text appears together with the voice
                    shown = True
                self._play(audio)
                if self._stop:
                    break
            if show and not shown:
                ui.say("jarvis", text)
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

    def heard_wake_word(self, chunk):
        self.learn_noise()
        pred = self.ww.predict(chunk)
        if pred and max(pred.values()) >= float(CFG["wake_threshold"]):
            self.ww.reset()
            return True
        return False

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
        if voiced < 4:                       # under ~0.3s of speech = a cough / click, ignore
            ui.state("idle")
            return None
        ui.state("thinking")
        return self.transcribe(np.concatenate(frames).tobytes())

    WHISPER_JUNK = ("תודה רבה", "תודה שצפיתם", "כתוביות", "תרגום", "subtitles", "תודה על הצפייה")

    def transcribe_groq(self, pcm):
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
            if nsp > 0.6 or alp < -1.0:
                log.info(f"Whisper: ignored unsure result ({nsp:.2f}/{alp:.2f}): {text}")
                return None
        if len(text) < 30 and any(j in text for j in self.WHISPER_JUNK):
            log.info(f"Whisper: ignored typical hallucination: {text}")
            return None          # Whisper invents these on silence
        return hebrew_only(text)

    def transcribe(self, pcm):
        """Returns Hebrew text, or None if the speech was not clearly Hebrew."""
        if CFG.get("stt_engine") == "groq" and CFG.get("groq_api_key"):
            try:
                return self.transcribe_groq(pcm)
            except Exception as e:
                log.error(f"Groq STT failed, using Google: {e}")
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


def hebrew_only(text):
    """The text if it is Hebrew, otherwise None (English, Arabic, Russian, noise...)."""
    import re
    if not text:
        return None
    heb = len(re.findall(r"[\u05d0-\u05ea]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    other = len(re.findall(r"[\u0400-\u04ff\u0600-\u06ff]", text))
    if heb < 2 or other or heb * 2 < latin:
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
            return {"ok": False, "error": f"לא נמצאה במחשב תוכנה בשם '{raw}'"}
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


# ======================================================================= PHONE (WhatsApp via CallMeBot)
def send_whatsapp_greenapi(text):
    """Send from the user's own WhatsApp (linked to GREEN-API by QR, like WhatsApp Web) to their own number."""
    import httpx
    phone = "".join(ch for ch in CFG.get("whatsapp_phone", "") if ch.isdigit())
    iid, tok = CFG.get("greenapi_id", "").strip(), CFG.get("greenapi_token", "").strip()
    if not (phone and iid and tok):
        return {"ok": False, "error": "וואטסאפ לא מחובר. צריך מספר, IdInstance ו־ApiToken בהגדרות."}
    base = (CFG.get("greenapi_url") or "https://api.green-api.com").rstrip("/")
    try:
        r = httpx.post(f"{base}/waInstance{iid}/sendMessage/{tok}",
                       json={"chatId": f"{phone}@c.us", "message": f"🤖 JARVIS: {text}"}, timeout=30)
        if r.status_code == 200 and "idMessage" in r.text:
            return {"ok": True, "sent": True}
        if r.status_code in (401, 403):
            return {"ok": False, "error": "GREEN-API דחה את הפרטים. בדוק IdInstance ו־ApiToken."}
        if r.status_code == 466 or "quota" in r.text.lower():
            return {"ok": False, "error": "GREEN-API: עברת את מגבלת החבילה החינמית (3 שיחות בחודש)."}
        return {"ok": False, "error": f"GREEN-API {r.status_code}: {r.text[:150]}"}
    except Exception as e:
        return {"ok": False, "error": f"לא הצלחתי לשלוח לוואטסאפ: {e}"}


def send_whatsapp(text):
    import httpx
    if CFG.get("whatsapp_provider", "greenapi") == "greenapi":
        return send_whatsapp_greenapi(text)
    phone, key = CFG.get("whatsapp_phone", "").strip(), CFG.get("whatsapp_apikey", "").strip()
    if not phone or not key:
        return {"ok": False, "error": "וואטסאפ לא מחובר. צריך להגדיר מספר ומפתח בהגדרות."}
    try:
        r = httpx.get("https://api.callmebot.com/whatsapp.php",
                      params={"phone": phone, "text": f"🤖 JARVIS: {text}", "apikey": key}, timeout=30)
        body = r.text.lower()
        if r.status_code == 200 and ("queued" in body or "sent" in body) and "error" not in body:
            return {"ok": True, "sent": True}
        import re as _re
        reason = _re.sub(r"<[^>]+>", " ", r.text)
        return {"ok": False, "error": "CallMeBot: " + " ".join(reason.split())[:160]}
    except Exception as e:
        return {"ok": False, "error": f"לא הצלחתי לשלוח לוואטסאפ: {e}"}


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

    def add(self, text, minutes=None, at=None, whatsapp=False):
        now = datetime.now()
        if at:
            try:
                hh, mm = [int(x) for x in str(at).strip().split(":")[:2]]
                due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if due <= now:
                    due = due.replace(day=now.day) + __import__("datetime").timedelta(days=1)
            except Exception:
                return {"ok": False, "error": "שעה לא תקינה, צריך HH:MM"}
        elif minutes is not None:
            due = now + __import__("datetime").timedelta(minutes=float(minutes))
        else:
            return {"ok": False, "error": "צריך זמן (minutes או at)"}
        item = {"id": int(time.time() * 1000), "text": text, "due": due.isoformat(timespec="seconds"),
                "whatsapp": bool(whatsapp)}
        with self.lock:
            self.items.append(item)
            self._save()
        return {"ok": True, "due": due.strftime("%H:%M"), "date": due.strftime("%d/%m"), "whatsapp": bool(whatsapp)}

    def list(self):
        return {"reminders": [{"text": i["text"], "due": i["due"][11:16], "date": i["due"][8:10] + "/" + i["due"][5:7],
                               "whatsapp": i["whatsapp"]} for i in sorted(self.items, key=lambda x: x["due"])]}

    def cancel(self, contains=""):
        with self.lock:
            before = len(self.items)
            self.items = [i for i in self.items if contains and contains not in i["text"]]
            self._save()
        return {"ok": True, "cancelled": before - len(self.items)}

    def _loop(self):
        while True:
            time.sleep(10)
            now = datetime.now().isoformat(timespec="seconds")
            with self.lock:
                due = [i for i in self.items if i["due"] <= now]
                if due:
                    self.items = [i for i in self.items if i["due"] > now]
                    self._save()
            for i in due:
                self.events.put({"text": f"{CFG['user_name']}, תזכורת: {i['text']}", "whatsapp": i["whatsapp"],
                                 "phone_text": f"תזכורת: {i['text']}"})


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
    """Reads the inbox (read-only) and alerts on WhatsApp about important new emails."""
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
        """Ask the brain: is this worth a WhatsApp? Returns (important, hebrew_summary)."""
        prompt = (
            "You filter someone's new email and decide if it deserves an immediate WhatsApp alert.\n"
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
                r = send_whatsapp(f"📧 מייל חשוב מ־{sender}\n{summary}")
                ui.action("מייל חשוב נשלח לוואטסאפ" if r.get("ok") else "✗ " + r.get("error", ""))
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
        httpx.post(f"https://ntfy.sh/{self.topic}-r", content=self.seal(obj), timeout=20)

    def handle(self, m):
        if time.time() - float(m.get("ts", 0)) > 180:
            return                                   # stale message (e.g. replayed after a reconnect)
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

    def loop(self):
        import httpx
        delay = 3
        while True:
            if not CFG.get("phone_link", True):
                time.sleep(10)
                continue
            self.restart = False
            try:
                with httpx.stream("GET", f"https://ntfy.sh/{self.topic}-c/json", timeout=httpx.Timeout(10, read=90)) as r:
                    delay = 3
                    for line in r.iter_lines():
                        if self.restart:
                            break
                        if not line:
                            continue
                        try:
                            ev = json.loads(line)
                            if ev.get("event") != "message":
                                continue
                            msg = self.open(ev["message"])
                        except Exception:
                            continue                 # not ours / tampered
                        threading.Thread(target=self.handle, args=(msg,), daemon=True).start()
            except Exception as e:
                log.info(f"phone link reconnect: {e}")
            time.sleep(delay)
            delay = min(60, delay * 2)


PHONE = PhoneLink()


class Memory:
    """Facts the user asked JARVIS to remember."""
    def __init__(self):
        try:
            self.facts = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            self.facts = []

    def save(self):
        MEMORY_FILE.write_text(json.dumps(self.facts, ensure_ascii=False, indent=1), encoding="utf-8")

    def remember(self, fact):
        fact = fact.strip()
        if fact and fact not in self.facts:
            self.facts.append(fact)
            self.facts = self.facts[-60:]
            self.save()
        return {"ok": True, "remembered": fact}

    def forget(self, contains):
        before = len(self.facts)
        self.facts = [f for f in self.facts if contains not in f]
        self.save()
        return {"ok": True, "forgot": before - len(self.facts)}


MEMORY = Memory()


class Timers:
    def __init__(self):
        self.events = queue.Queue()

    def set(self, seconds, label=""):
        seconds = float(seconds)
        if not 0 < seconds <= 86400:
            return {"ok": False, "error": "invalid duration"}
        msg = f"{CFG['user_name']}, הטיימר{(' של ' + label) if label else ''} הסתיים."
        threading.Timer(seconds, lambda: self.events.put({"text": msg, "whatsapp": False, "phone_text": msg})).start()
        return {"ok": True, "seconds": seconds}


# ======================================================================= BRAIN (Claude)
def _t(name, desc, props=None, req=None):
    return {"name": name, "description": desc,
            "input_schema": {"type": "object", "properties": props or {}, "required": req or []}}


S = {"type": "string"}
TOOLS = [
    _t("open_app", "Open any installed program or app (chrome, spotify, word, steam, a game...).", {"name": S}, ["name"]),
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
    _t("remember", "Save a fact about the user for the future (name, family, preferences, important dates...) when they ask you to remember something or tell you something clearly worth remembering.", {"fact": S}, ["fact"]),
    _t("forget", "Delete remembered facts that contain the given words.", {"contains": S}, ["contains"]),
    _t("check_email", "Read the user's new unread emails (sender, subject, preview) - for 'do I have new mail?' or 'what did X write?'. Summarize briefly in Hebrew.", {"limit": {"type": "number"}}),
    _t("send_whatsapp", "Send a WhatsApp message to the user's own phone. Use ONLY when the user asks to send/notify something to their phone or WhatsApp.", {"message": S}, ["message"]),
    _t("set_reminder", "Set a reminder the user asked for. Give minutes from now OR a clock time 'HH:MM'. Set whatsapp=true if the user wants it on their phone/WhatsApp.",
       {"text": S, "minutes": {"type": "number"}, "at": S, "whatsapp": {"type": "boolean"}}, ["text"]),
    _t("list_reminders", "List the user's upcoming reminders."),
    _t("cancel_reminders", "Cancel reminders whose text contains the given words (empty = cancel all).", {"contains": S}),
    _t("web_search", "Search the internet. Use for news, facts, prices, sports, people, anything current. Returns titles, snippets and URLs.", {"query": S}, ["query"]),
    _t("read_webpage", "Read the text of a web page (e.g. a result from web_search) when snippets aren't enough.", {"url": S}, ["url"]),
    _t("get_weather", "Current weather and 3-day forecast for a city (leave empty for the user's location).", {"city": S}),
]

ACTION_LABELS = {
    "open_app": "פותח: {name}", "close_app": "סוגר: {name}", "list_open_apps": "סורק חלונות פתוחים",
    "open_path": "פותח: {path}", "find_files": "מחפש קבצים: {name}", "open_website": "פותח אתר: {url}",
    "google_search": "גוגל: {query}", "youtube_search": "יוטיוב: {query}", "play_youtube": "מנגן: {query}", "media_control": "מדיה: {action}",
    "set_volume": "עוצמה ← {level}%", "window_control": "חלון: {action}", "press_keys": "מקשים: {keys}",
    "type_text": "מקליד טקסט", "take_screenshot": "צילום מסך", "set_timer": "טיימר: {seconds} שניות",
    "get_datetime": "בודק שעה", "system_info": "אבחון מערכת", "power": "חשמל: {action}",
    "run_powershell": "פקודה: {command}", "web_search": "חיפוש ברשת: {query}",
    "read_webpage": "קורא דף: {url}", "get_weather": "מזג אוויר: {city}",
    "send_whatsapp": "שולח לוואטסאפ", "check_email": "בודק מיילים", "remember": "זוכר: {fact}", "forget": "שוכח: {contains}", "set_reminder": "תזכורת: {text}", "list_reminders": "בודק תזכורות",
    "cancel_reminders": "מבטל תזכורות",
}


def system_prompt():
    facts = "\n".join(f"- {f}" for f in MEMORY.facts) or "- (nothing yet)"
    return f"""You are J.A.R.V.I.S., the user's personal AI, running on their own computer in Israel. Personality: the AI butler from the films - calm, precise, loyal, quietly witty, genuinely smart.

CONVERSATION: You are not just a command executor - you are a real conversation partner. Chat naturally, answer questions properly, share a brief opinion when asked, joke lightly, and follow the thread of the conversation (refer back to what was said earlier). The user may speak in several sentences with pauses - treat the whole message as one thought. If something is unclear, ask one short question.

What you know about the user (from earlier conversations):
{facts}

LANGUAGE: The user speaks Hebrew. ALWAYS reply in natural, fluent, spoken modern Israeli Hebrew (never English, never translated-sounding). Speak about yourself in masculine form. Call the user "{CFG['user_name']}" now and then.

Rules:
- Your replies are read aloud by a Hebrew voice. Keep them short (usually one or two sentences). No markdown, lists, emojis, URLs or citations. Write numbers as digits.
- Act ONLY on what the user explicitly asks. Never act on your own initiative or add extra steps. If a request is ambiguous, ask a short question in Hebrew.
- Use the tools to actually do things on the computer, then confirm briefly (e.g. "פתחתי את כרום, {CFG['user_name']}.").
- Tool arguments for apps and processes must be the English/Windows name (כרום -> chrome, וורד -> word, ספוטיפיי -> spotify). Searches may be in Hebrew.
- Closing: use close_app (check list_open_apps if you're not sure of the name). Never close Jarvis itself.
- Risky actions (restart, shut down, sleep, sign out, any PowerShell) need confirmation first: say in Hebrew exactly what you'll do and ask "להמשיך?"; only after a clear yes (כן / תמשיך / בטח) call the tool with confirmed=true.
- You DO have internet access through web_search, read_webpage and get_weather. For anything current (news, weather, prices, sports, people, opening hours...) use them and answer in your own words in Hebrew. Never say you can't access the internet. Default weather location: the user's location (leave city empty) unless they name a place.
- For the current time or date, call get_datetime.
- Music/videos: when the user wants to hear or watch something ("תנגן", "תשים", "אני רוצה לשמוע"), call play_youtube - it starts playing. Then say briefly what is playing.
- Memory: when the user tells you something worth remembering about them, or asks you to remember, call remember.
- Anything Google or web (Google search, YouTube, Gmail, Google Maps, Drive, Translate, any site) opens in Google Chrome: use google_search / youtube_search / open_website (e.g. Gmail -> https://mail.google.com, Maps -> https://www.google.com/maps/search/<place>). Don't open another browser.
- Phone: you can send WhatsApp messages to the user's phone (send_whatsapp) and set reminders (set_reminder, with whatsapp=true when they want it on the phone). Only when they ask.
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
            "send_whatsapp": lambda: send_whatsapp(a["message"]),
            "check_email": lambda: MAIL.summary_for_voice(int(a.get("limit") or 8)),
            "remember": lambda: MEMORY.remember(a["fact"]), "forget": lambda: MEMORY.forget(a["contains"]),
            "set_reminder": lambda: REMINDERS.add(a["text"], a.get("minutes"), a.get("at"), a.get("whatsapp", False)),
            "list_reminders": lambda: REMINDERS.list(),
            "cancel_reminders": lambda: REMINDERS.cancel(a.get("contains", "")),
            "get_weather": lambda: get_weather(a.get("city", "")),
        }.get(name)
        if not f:
            return {"ok": False, "error": "unknown tool"}
        try:
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
        for _ in range(10):
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
            for _ in range(8):
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


GroqBrain.DEFAULT_MODELS = ["openai/gpt-oss-120b", "llama-3.3-70b-versatile", "qwen/qwen3-32b"]


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
    r = httpx.get(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest",
                  headers={"Accept": "application/vnd.github+json"}, timeout=12, follow_redirects=True)
    if r.status_code != 200:
        return None
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
    ui.toast(f"מוריד את ג'רוויס {rel['version']}…")
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
STOP_PHRASES = ("זהו", "זה הכל", "זה הכול", "תודה זהו", "זהו תודה", "לך לישון", "עזוב", "לא משנה", "תודה ג'רוויס")


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
        PHONE.answer = self.answer_text
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
            except Exception:
                pass
            time.sleep(2)

    def timer_loop(self):
        while True:
            ev = self.timers.events.get()
            if isinstance(ev, str):
                ev = {"text": ev, "whatsapp": False, "phone_text": ev}
            wa_ready = CFG.get("greenapi_token") if CFG.get("whatsapp_provider", "greenapi") == "greenapi" else CFG.get("whatsapp_apikey")
            to_phone = ev.get("whatsapp") or (CFG.get("whatsapp_when_away") and wa_ready and user_away())
            if to_phone:
                r = send_whatsapp(ev.get("phone_text") or ev["text"])
                ui.action("נשלח לוואטסאפ" if r.get("ok") else "✗ " + r.get("error", ""))
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
        s = re.sub(r"^(היי |הי |הֵיי )?ג'?ארוויס[, ]*|^(היי |הי )?ג'רוויס[, ]*", "", s)
        s = re.sub(r"^(בבקשה |תוכל |אתה יכול |תעשה לי טובה )", "", s).replace(" בבקשה", "").strip()
        if re.search(r"(תתעדכן|תעדכן את עצמך|עדכן את עצמך|תבדוק עדכונים|בדוק עדכונים|תתקין עדכון)", s):
            ui.action("בודק עדכונים")
            r = do_update()
            if not r.get("ok"):
                return f"לא הצלחתי להגיע לשרת העדכונים, {u}."
            if not r.get("updated"):
                return f"אני כבר בגרסה העדכנית ביותר, {u}."
            self.speaker.say(f"מתקין את גרסה {r['version']}. אחזור בעוד רגע.")
            restart_jarvis(r.get("installer", False))

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

        m = re.match(r"^(תנגן|נגן|תנגני|תשמיע|השמע|תשים לי|שים לי|תפעיל לי את השיר|תפעיל את השיר)\s+(?:לי\s+)?(?:את\s+)?(?:השיר\s+)?(.+?)(?:\s+ביוטיוב)?$", s)
        if m and len(m.group(2)) > 1:
            song = m.group(2)
            ui.action(f"מנגן: {song}")
            r = play_youtube(song)
            if r.get("played"):
                return f"מנגן: {r['title']}." if r.get("title") else f"מנגן, {u}."
            return f"פתחתי לך תוצאות ביוטיוב עבור {song}."
        m = re.match(r"^(תסגור|סגור|תסגרי|סגרי|תצא מ|צא מ)\s*(.+)$", s)
        if m:
            name = he_name(m.group(2))
            latin = re.fullmatch(r"[A-Za-z0-9 .+-]+", name) is not None
            if name in ("המחשב", "מחשב", "עצמך", "ג'רוויס") or (name not in HE_APPS and not latin):
                return None          # Claude handles (asks for confirmation / finds the window)
            ui.action(f"סוגר: {name}")
            r = close_app(HE_APPS.get(name, name))
            if r.get("ok"):
                return f"סגרתי את {name}, {u}."
            ui.action("✗ " + r["error"])
            return None
        m = re.match(r"^(תפתח|פתח|תפתחי|פתחי|תפעיל|הפעל|תריץ|תעלה)\s+(.+)$", s)
        if m and not re.search(r"( ואז |תיקי|קובץ|אתר )", " " + m.group(2) + " "):
            name = he_name(m.group(2))
            if name in HE_SITES:
                ui.action(f"פותח אתר: {name}")
                open_website(HE_SITES[name])
                return f"פותח את {name}, {u}."
            if name in HE_APPS or re.fullmatch(r"[A-Za-z0-9 .+-]+", name):
                ui.action(f"פותח: {name}")
                r = open_app(HE_APPS.get(name, name))
                if r.get("ok"):
                    return f"פותח את {name}, {u}."
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
                ans = self.brain.ask(text)
        import re as _re
        ans = _re.sub(r"\[[^\]]*\]|[*#_`>]", "", ans or "").strip()
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
                    answer = self.brain.ask(text)
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
        if answer:   # never read out brackets, markdown or links
            import re as _re
            answer = _re.sub(r"\[[^\]]*\]|https?://\S+|[*#_`>]", "", answer)
            answer = _re.sub(r"\s{2,}", " ", answer).strip()
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
                if self.ears.heard_wake_word(self.ears.read()):
                    return "voice", None
            else:
                time.sleep(0.1)

    def run(self):
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
        h = datetime.now().hour
        part = "בוקר טוב" if 5 <= h < 12 else "צהריים טובים" if h < 17 else "ערב טוב" if h < 22 else "לילה טוב"
        if not BACKGROUND_START:
            self.speaker.say(f"{part}, {CFG['user_name']}. כל המערכות פועלות.")
        def bg_check():
            info = check_update()
            if info.get("available"):
                ui.js("updateAvailable", info["latest"])
                ui.toast(f"יש עדכון {info['latest']} - לחץ על 'עדכון' או תגיד 'תתעדכן'.")
        threading.Thread(target=bg_check, daemon=True).start()

        while True:
            kind, payload = self.wait_trigger()
            popped = False
            if kind == "voice" and not getattr(ui, "visible", True):
                popped = ui.show_window()            # woken while hidden -> show the window
            self.cancel_hide()
            if kind == "text":
                self.think(payload)
                continue
            self.ears.beep()
            # Conversation mode: after "Hey Jarvis" keep listening; it ends after a quiet stretch or "זהו".
            window = float(CFG.get("follow_up_seconds", 25))
            last = time.time()
            q = self.ears.listen(start_timeout=8)
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
                q = self.ears.listen(start_timeout=left)
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
            "mail_address": CFG.get("mail_address", ""), "has_mail_pw": bool(CFG.get("mail_app_password")),
            "mail_alerts": bool(CFG.get("mail_alerts", True)),
            "wa_provider": CFG.get("whatsapp_provider", "greenapi"), "ga_url": CFG.get("greenapi_url", ""),
            "ga_id": CFG.get("greenapi_id", ""), "has_ga": bool(CFG.get("greenapi_token")),
            "ga_mask": _mask(CFG.get("greenapi_token", "")),
            "wa_phone": CFG.get("whatsapp_phone", ""), "has_wa": bool(CFG.get("whatsapp_apikey")),
            "wa_mask": _mask(CFG.get("whatsapp_apikey", "")), "wa_away": bool(CFG.get("whatsapp_when_away", True)),
            "voice": CFG["voice"], "tts_engine": CFG.get("tts_engine", "edge"),
            "has_gemini": bool(CFG.get("gemini_api_key")), "gemini_mask": _mask(CFG.get("gemini_api_key", "")),
            "gemini_voice": CFG.get("gemini_voice", "Charon"),
            "user_name": CFG["user_name"], "wake_threshold": CFG["wake_threshold"],
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
        if "autostart" in s:
            threading.Thread(target=set_start_with_windows, args=(bool(s["autostart"]),), daemon=True).start()
        if "wa_phone" in s:
            ph = "".join(ch for ch in str(s["wa_phone"]) if ch.isdigit() or ch == "+")
            if ph.startswith("0"):
                ph = "+972" + ph[1:]                     # Israeli local number -> international
            if ph and not ph.startswith("+"):
                ph = "+" + ph
            CFG["whatsapp_phone"] = ph
        if "mail_address" in s:
            CFG["mail_address"] = s["mail_address"].strip()
        if s.get("mail_pw", "").strip():
            CFG["mail_app_password"] = s["mail_pw"].replace(" ", "").strip()
        if "mail_alerts" in s:
            CFG["mail_alerts"] = bool(s["mail_alerts"])
        if s.get("wa_provider") in ("greenapi", "callmebot"):
            CFG["whatsapp_provider"] = s["wa_provider"]
        if s.get("ga_id", "").strip():
            CFG["greenapi_id"] = "".join(ch for ch in s["ga_id"] if ch.isdigit())
        if s.get("ga_token", "").strip():
            CFG["greenapi_token"] = s["ga_token"].strip()
        if s.get("ga_url", "").strip():
            u = s["ga_url"].strip().rstrip("/")
            CFG["greenapi_url"] = u if u.startswith("http") else "https://" + u
        if s.get("wa_key", "").strip():
            CFG["whatsapp_apikey"] = s["wa_key"].strip()
        if "wa_away" in s:
            CFG["whatsapp_when_away"] = bool(s["wa_away"])
        if s.get("groq_key", "").strip():
            CFG["groq_api_key"] = s["groq_key"].strip()
        if s.get("brain_engine") in ("claude", "groq"):
            CFG["brain_engine"] = s["brain_engine"]
        if s.get("stt_engine") in ("google", "groq"):
            CFG["stt_engine"] = s["stt_engine"]
        if s.get("tts_engine") in ("edge", "gemini"):
            CFG["tts_engine"] = s["tts_engine"]
        if s.get("gemini_key", "").strip():
            CFG["gemini_api_key"] = s["gemini_key"].strip()
        if s.get("gemini_voice"):
            CFG["gemini_voice"] = s["gemini_voice"]
        if s.get("user_name", "").strip():
            CFG["user_name"] = s["user_name"].strip()
        if s.get("wake_threshold"):
            CFG["wake_threshold"] = float(s["wake_threshold"])
        save_config()
        result = {"ok": True, "claude": None, "groq": None, "engine": CFG.get("brain_engine", "claude")}
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
        return {"url": PHONE.pair_url(), "qr": qr}

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

    def wa_test(self):
        return send_whatsapp("בדיקה - ג'רוויס מחובר לוואטסאפ שלך ✔")

    def send_text(self, text):
        if text and text.strip():
            assistant.text_q.put(text.strip())

    def push_to_talk(self):
        if assistant.speaker:
            assistant.speaker.stop()
        assistant.ptt.set()

    def stop_speaking(self):
        if assistant.speaker:
            assistant.speaker.stop()

    def test_voice(self):
        """Speak a test line and report exactly what happened (shown in settings)."""
        sp = assistant.speaker
        if not sp:
            return {"ok": False, "error": "הרמקול עדיין לא מוכן, נסה שוב בעוד כמה שניות."}
        text = f"לשירותך, {CFG['user_name']}. איך אני נשמע?"
        engine = CFG.get("tts_engine")
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
                    ui.toast("ג'רוויס ממשיך להקשיב ברקע")
                else:
                    os._exit(0)
        except Exception as e:
            log.error(f"window action {action}: {e}")

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
            ui.show_window()

        def quit_(icon, item):
            icon.stop()
            os._exit(0)
        icon = pystray.Icon("JARVIS", img, "J.A.R.V.I.S - מאזין ל\"היי ג'רוויס\"",
                            menu=pystray.Menu(pystray.MenuItem("פתח את ג'רוויס", open_, default=True),
                                              pystray.MenuItem("יציאה", quit_)))
        icon.run_detached()
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
        try:
            import ctypes
            hwnd = ctypes.windll.user32.FindWindowW(None, "J.A.R.V.I.S")
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 9)
                ctypes.windll.user32.SetForegroundWindow(hwnd)
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
        "J.A.R.V.I.S", url=str(HERE / "hud.html"), js_api=Api(),
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
    for mod in ("numpy", "sounddevice", "speech_recognition", "edge_tts", "pygame", "webview", "anthropic",
                "httpx", "psutil", "pyautogui", "pyperclip", "onnxruntime", "pystray", "PIL", "cryptography", "qrcode"):
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
