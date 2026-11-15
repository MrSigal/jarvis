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

VERSION = "3.1"
UPDATE_REPO = "MrSigal/jarvis"          # updates are downloaded from this GitHub repo
UPDATE_BRANCH = "main"
HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "config.json"
LOG_FILE = HERE / "jarvis.log"
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
    "claude_model": "claude-haiku-5-5",   # cheapest Claude model
    "voice": "he-IL-AvriNeural",            # free Hebrew male voice (female: he-IL-HilaNeural)
    "tts_engine": "edge",                   # edge (fast, built-in) | voicestudio (local VoiceStudio app)
    "vs_url": "http://127.0.0.1:3900",
    "vs_model": "omnivoice",
    "vs_voice": "default",
    "user_name": "אדוני",
    "wake_threshold": 0.5,
    "follow_up_seconds": 6,
    "min_confidence": 0.5,                  # speech below this confidence is ignored (non-English, noise)
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
    if not str(cfg.get("voice", "")).startswith("he-IL"):     # v3: Hebrew
        cfg["voice"] = "he-IL-AvriNeural"
    if cfg.get("user_name", "").lower() in ("", "sir"):
        cfg["user_name"] = "אדוני"
    if cfg.get("min_confidence", 0.5) > 0.5:
        cfg["min_confidence"] = 0.5
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

    # ---- VoiceStudio app running on this computer (OpenAI-compatible API)
    def _voicestudio(self, text):
        import httpx, base64
        base = CFG["vs_url"].rstrip("/")
        r = httpx.post(f"{base}/v1/audio/speech", timeout=90, json={
            "model": CFG["vs_model"], "input": text[:4000], "voice": CFG["vs_voice"] or "default",
            "language": "he", "response_format": "mp3"})
        if r.status_code != 200:
            raise RuntimeError(f"VoiceStudio {r.status_code}: {r.text[:200]}")
        ctype = r.headers.get("content-type", "")
        if not ctype.startswith("application/json"):
            return r.content                                   # raw audio
        d = r.json()                                           # some builds answer with JSON
        for k in ("audio", "audio_base64", "b64", "data"):
            if isinstance(d.get(k), str) and len(d[k]) > 100:
                return base64.b64decode(d[k].split(",")[-1])
        for k in ("url", "audio_url", "path", "file", "output"):
            v = d.get(k)
            if isinstance(v, str) and v:
                if v.startswith("http") or v.startswith("/"):
                    if v.startswith("/") and not os.path.exists(v):
                        v = base + v
                    if v.startswith("http"):
                        return httpx.get(v, timeout=60).content
                    return Path(v).read_bytes()
                return Path(v).read_bytes()
        raise RuntimeError(f"VoiceStudio answer not understood: {str(d)[:200]}")

    def synth(self, text):
        if CFG.get("tts_engine") == "voicestudio":
            try:
                return self._voicestudio(text)
            except Exception as e:
                log.error(f"VoiceStudio failed: {e}")
                ui.toast("VoiceStudio לא עונה - משתמש בקול הרגיל. האם האפליקציה פתוחה?", "warn")
        return self._edge(text)

    def say(self, text, show=True):
        if not text:
            return
        with self.lock:
            self._stop = False
            if show:
                ui.say("jarvis", text)
            try:
                audio = self.synth(text)
            except Exception as e:
                log.error(f"TTS failed: {e}")
                ui.toast("ההקראה נכשלה (אין אינטרנט?)", "warn")
                return
            path = os.path.join(tempfile.gettempdir(), f"jarvis_{int(time.time()*1000)}.mp3")
            with open(path, "wb") as f:
                f.write(audio)
            ui.state("speaking")
            try:
                m = self.pygame.mixer.music
                m.load(path)
                m.play()
                while m.get_busy() and not self._stop:
                    time.sleep(0.04)
                m.stop()
                try:
                    m.unload()
                except Exception:
                    pass
            finally:
                try:
                    os.remove(path)
                except Exception:
                    pass
                ui.state("idle")

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
        self.ww = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
        self.rec = sr.Recognizer()
        self.stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=CHUNK)
        self.stream.start()
        self._n = 0

    def read(self):
        data, _ = self.stream.read(CHUNK)
        chunk = data[:, 0].copy()
        self._n += 1
        if self._n % 2 == 0:
            rms = float(self.np.sqrt(self.np.mean(chunk.astype(self.np.float32) ** 2)))
            ui.level(min(1.0, rms / 4000))
        return chunk

    def flush(self):
        try:
            while self.stream.read_available >= CHUNK:
                self.stream.read(CHUNK)
        except Exception:
            pass
        self.ww.reset()

    def heard_wake_word(self, chunk):
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

    def listen(self, start_timeout=6.0, max_seconds=15.0, silence_end=1.0):
        np = self.np
        self.flush()
        ui.state("listening")
        noise = [np.sqrt(np.mean(self.read().astype(np.float32) ** 2)) for _ in range(3)]
        threshold = max(320.0, float(np.mean(noise)) * 2.2)
        frames, started, silent, waited, total = [], False, 0.0, 0.0, 0.0
        step = CHUNK / SAMPLE_RATE
        while True:
            c = self.read()
            rms = float(np.sqrt(np.mean(c.astype(np.float32) ** 2)))
            if not started:
                waited += step
                if rms > threshold:
                    started = True
                    frames.append(c)
                elif waited > start_timeout:
                    ui.state("idle")
                    return None
                continue
            frames.append(c)
            total += step
            silent = silent + step if rms < threshold else 0.0
            if silent >= silence_end or total >= max_seconds:
                break
        ui.state("thinking")
        pcm = np.concatenate(frames).tobytes()
        return self.transcribe(pcm)

    def transcribe(self, pcm):
        """Returns English text, or None if the speech was not clearly English."""
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


def open_website(url):
    if not url.startswith("http"):
        url = "https://" + url
    webbrowser.open(url)
    return {"ok": True, "opened": url}


def google_search(query):
    webbrowser.open("https://www.google.com/search?q=" + urllib.parse.quote(query))
    return {"ok": True}


def youtube_search(query, play_first=False):
    webbrowser.open("https://www.youtube.com/results?search_query=" + urllib.parse.quote(query))
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


class Timers:
    def __init__(self):
        self.events = queue.Queue()

    def set(self, seconds, label=""):
        seconds = float(seconds)
        if not 0 < seconds <= 86400:
            return {"ok": False, "error": "invalid duration"}
        threading.Timer(seconds, lambda: self.events.put(
            f"{CFG['user_name']}, הטיימר{(' של ' + label) if label else ''} הסתיים.")).start()
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
    _t("youtube_search", "Search YouTube in the browser (music, videos).", {"query": S}, ["query"]),
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
    _t("web_search", "Search the internet. Use for news, facts, prices, sports, people, anything current. Returns titles, snippets and URLs.", {"query": S}, ["query"]),
    _t("read_webpage", "Read the text of a web page (e.g. a result from web_search) when snippets aren't enough.", {"url": S}, ["url"]),
    _t("get_weather", "Current weather and 3-day forecast for a city (leave empty for the user's location).", {"city": S}),
]

ACTION_LABELS = {
    "open_app": "פותח: {name}", "close_app": "סוגר: {name}", "list_open_apps": "סורק חלונות פתוחים",
    "open_path": "פותח: {path}", "find_files": "מחפש קבצים: {name}", "open_website": "פותח אתר: {url}",
    "google_search": "גוגל: {query}", "youtube_search": "יוטיוב: {query}", "media_control": "מדיה: {action}",
    "set_volume": "עוצמה ← {level}%", "window_control": "חלון: {action}", "press_keys": "מקשים: {keys}",
    "type_text": "מקליד טקסט", "take_screenshot": "צילום מסך", "set_timer": "טיימר: {seconds} שניות",
    "get_datetime": "בודק שעה", "system_info": "אבחון מערכת", "power": "חשמל: {action}",
    "run_powershell": "פקודה: {command}", "web_search": "חיפוש ברשת: {query}",
    "read_webpage": "קורא דף: {url}", "get_weather": "מזג אוויר: {city}",
}


def system_prompt():
    return f"""You are J.A.R.V.I.S., the user's personal AI, running on their own computer in Israel. Personality: the AI butler from the films - calm, precise, loyal, quietly witty.

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
            "media_control": lambda: media_control(a["action"]), "set_volume": lambda: set_volume(a["level"]),
            "window_control": lambda: window_control(a["action"]), "press_keys": lambda: press_keys(a["keys"]),
            "type_text": lambda: type_text(a["text"]), "take_screenshot": take_screenshot,
            "set_timer": lambda: self.timers.set(a["seconds"], a.get("label", "")),
            "get_datetime": get_datetime, "system_info": system_info,
            "power": lambda: power(a["action"]), "run_powershell": lambda: run_powershell(a["command"]),
            "web_search": lambda: web_search(a["query"]), "read_webpage": lambda: read_webpage(a["url"]),
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


def check_update():
    try:
        import httpx
        r = httpx.get(_raw("version.txt") + f"?t={int(time.time())}", timeout=10, follow_redirects=True)
        if r.status_code != 200:
            return {"ok": False, "error": f"update server said {r.status_code}"}
        latest = r.text.strip()
        return {"ok": True, "current": VERSION, "latest": latest, "available": _vtuple(latest) > _vtuple(VERSION)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def do_update():
    """Download the new files, install new packages if needed, and restart."""
    import httpx
    info = check_update()
    if not info.get("ok"):
        return info
    if not info["available"]:
        return {"ok": True, "updated": False, "message": f"Already on the latest version ({VERSION})."}
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


def restart_jarvis():
    subprocess.Popen([sys.executable, str(HERE / "jarvis.py")], cwd=str(HERE))
    os._exit(0)


# ======================================================================= ASSISTANT LOOP
STOP_PHRASES = ("זהו", "זה הכל", "זה הכול", "תודה זהו", "זהו תודה", "לך לישון", "עזוב", "לא משנה", "תודה ג'רוויס")


class Assistant:
    def __init__(self):
        self.timers = Timers()
        self.speaker = None
        self.ears = None
        self.brain = None
        self.text_q = queue.Queue()
        self.ptt = threading.Event()
        self.busy = False

    def build_brain(self):
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
            msg = self.timers.events.get()
            if self.speaker:
                self.speaker.say(msg)

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
            self.speaker.say(f"עדכון {r['version']} הותקן. מפעיל את עצמי מחדש.")
            restart_jarvis()

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
                answer = self.brain.ask(text)
            except Exception as e:
                log.exception("brain")
                msg = str(e)
                low = msg.lower()
                ui.toast("שגיאה: " + msg[:180], "error")
                if "authentication" in low or "401" in msg or "api key" in low:
                    answer = "Claude דחה את החיבור. תבדוק את המפתח בהגדרות."
                elif "credit" in low or "billing" in low:
                    answer = "נראה שנגמר הקרדיט בחשבון של Claude."
                elif "connect" in low or "timeout" in low or "network" in low:
                    answer = "אני לא מצליח להגיע לשרתים כרגע. תבדוק את החיבור לאינטרנט."
                else:
                    answer = f"משהו השתבש אצלי. השגיאה מופיעה על המסך, {CFG['user_name']}."
        if not answer or "[IGNORE]" in answer:
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
        self.speaker.say(f"{part}, {CFG['user_name']}. כל המערכות פועלות.")
        def bg_check():
            info = check_update()
            if info.get("available"):
                ui.js("updateAvailable", info["latest"])
                ui.toast(f"יש עדכון {info['latest']} - לחץ על 'עדכון' או תגיד 'תתעדכן'.")
        threading.Thread(target=bg_check, daemon=True).start()

        while True:
            kind, payload = self.wait_trigger()
            if kind == "text":
                self.think(payload)
                continue
            self.ears.beep()
            q = self.ears.listen()      # nothing / non-English -> silently back to standby
            while q:
                if any(p in q for p in STOP_PHRASES) and len(q.split()) <= 4:
                    self.speaker.say(f"בשמחה, {CFG['user_name']}.")
                    break
                if not self.think(q):
                    break
                if not self.text_q.empty():
                    break
                self.ears.beep(1318, 0.06)
                q = self.ears.listen(start_timeout=float(CFG["follow_up_seconds"]))


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
            "voice": CFG["voice"], "tts_engine": CFG.get("tts_engine", "edge"),
            "vs_voice": CFG.get("vs_voice", "default"), "vs_model": CFG.get("vs_model", "omnivoice"),
            "user_name": CFG["user_name"], "wake_threshold": CFG["wake_threshold"],
        }

    def save_settings(self, s):
        if s.get("claude_key", "").strip():
            CFG["anthropic_api_key"] = s["claude_key"].strip()
        if s.get("voice"):
            CFG["voice"] = s["voice"]
        if s.get("tts_engine") in ("edge", "voicestudio"):
            CFG["tts_engine"] = s["tts_engine"]
        if s.get("vs_voice"):
            CFG["vs_voice"] = s["vs_voice"]
        if s.get("vs_model"):
            CFG["vs_model"] = s["vs_model"]
        if s.get("user_name", "").strip():
            CFG["user_name"] = s["user_name"].strip()
        if s.get("wake_threshold"):
            CFG["wake_threshold"] = float(s["wake_threshold"])
        save_config()
        result = {"ok": True, "claude": None}
        if CFG["anthropic_api_key"]:
            try:
                import anthropic
                anthropic.Anthropic(api_key=CFG["anthropic_api_key"]).messages.create(
                    model=CFG["claude_model"], max_tokens=5, messages=[{"role": "user", "content": "hi"}])
                result["claude"] = "ok"
            except Exception as e:
                result["claude"] = str(e)[:200]
        assistant.build_brain()
        return result

    def vs_check(self):
        """Is VoiceStudio running? Returns its voices."""
        import httpx
        base = CFG["vs_url"].rstrip("/")
        try:
            r = httpx.get(f"{base}/v1/audio/voices", timeout=8)
        except Exception:
            return {"ok": False, "error": "VoiceStudio לא פתוח. תפתח את האפליקציה ונסה שוב."}
        if r.status_code != 200:
            return {"ok": False, "error": f"VoiceStudio ענה {r.status_code}"}
        try:
            d = r.json()
        except Exception:
            d = []
        items = d if isinstance(d, list) else d.get("voices") or d.get("data") or d.get("profiles") or []
        voices = [{"id": "default", "name": "ברירת מחדל"}]
        for v in items:
            if isinstance(v, str):
                voices.append({"id": v, "name": v})
            elif isinstance(v, dict):
                vid = v.get("id") or v.get("voice_id") or v.get("profile_id") or v.get("name")
                if vid and vid != "default":
                    voices.append({"id": str(vid), "name": str(v.get("name") or vid)})
        return {"ok": True, "voices": voices}

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
        if assistant.speaker:
            threading.Thread(target=assistant.speaker.say,
                             args=(f"לשירותך, {CFG['user_name']}. איך אני נשמע?",), daemon=True).start()

    def open_link(self, url):
        webbrowser.open(url)

    def version(self):
        return VERSION

    def update_now(self):
        r = do_update()
        if r.get("updated"):
            threading.Thread(target=lambda: (time.sleep(1.5), restart_jarvis()), daemon=True).start()
        return r


# ======================================================================= MAIN
def main():
    import webview
    window = webview.create_window(
        "J.A.R.V.I.S", url=str(HERE / "hud.html"), js_api=Api(),
        width=1280, height=780, min_size=(900, 600), background_color="#01060b",
    )
    ui.window = window
    webview.start(assistant.start, debug=False)
    os._exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("fatal")
        raise
