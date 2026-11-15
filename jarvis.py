# -*- coding: utf-8 -*-
"""
J.A.R.V.I.S  -  Personal desktop AI assistant
==============================================
* Say "Hey Jarvis" (or click the core / press the mic button) and speak.
* Futuristic HUD window instead of a console.
* Brain: Claude (Anthropic API key).  Voice: ElevenLabs (falls back to a free voice).
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

VERSION = "2.2"
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
    "elevenlabs_api_key": "",
    "elevenlabs_voice_id": "",
    "elevenlabs_voice_name": "",
    "elevenlabs_model": "eleven_multilingual_v2",
    "fallback_voice": "en-GB-RyanNeural",   # free voice used without ElevenLabs
    "speech_recognition": "google",          # google (free) | elevenlabs (more accurate)
    "user_name": "sir",
    "wake_threshold": 0.5,
    "follow_up_seconds": 6,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception as e:
            log.error(f"config.json unreadable: {e}")
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

    # ---- ElevenLabs
    def _eleven(self, text):
        import httpx
        vid = CFG["elevenlabs_voice_id"]
        r = httpx.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{vid}?output_format=mp3_44100_128",
            headers={"xi-api-key": CFG["elevenlabs_api_key"], "Content-Type": "application/json"},
            json={"text": text, "model_id": CFG["elevenlabs_model"]},
            timeout=40,
        )
        if r.status_code != 200:
            raise RuntimeError(f"ElevenLabs {r.status_code}: {r.text[:200]}")
        return r.content

    # ---- free fallback (Microsoft Edge voices)
    def _edge(self, text):
        import edge_tts

        async def run():
            buf = BytesIO()
            async for chunk in edge_tts.Communicate(text, CFG["fallback_voice"], rate="+4%").stream():
                if chunk["type"] == "audio":
                    buf.write(chunk["data"])
            return buf.getvalue()
        return asyncio.run(run())

    def synth(self, text):
        if CFG["elevenlabs_api_key"] and CFG["elevenlabs_voice_id"]:
            try:
                return self._eleven(text)
            except Exception as e:
                log.error(f"ElevenLabs failed: {e}")
                ui.toast("ElevenLabs voice failed - using backup voice. Check your key / credits.", "warn")
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
                ui.toast("Voice output failed (no internet?)", "warn")
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
        ui.toast("Loading wake-word model…")
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
        if CFG["speech_recognition"] == "elevenlabs" and CFG["elevenlabs_api_key"]:
            try:
                import httpx
                buf = BytesIO()
                with wave.open(buf, "wb") as w:
                    w.setnchannels(1); w.setsampwidth(2); w.setframerate(SAMPLE_RATE); w.writeframes(pcm)
                r = httpx.post(
                    "https://api.elevenlabs.io/v1/speech-to-text",
                    headers={"xi-api-key": CFG["elevenlabs_api_key"]},
                    data={"model_id": "scribe_v1", "language_code": "en"},
                    files={"file": ("speech.wav", buf.getvalue(), "audio/wav")},
                    timeout=30,
                )
                if r.status_code == 200:
                    return (r.json().get("text") or "").strip() or None
                log.error(f"Scribe {r.status_code}: {r.text[:200]}")
            except Exception as e:
                log.error(f"Scribe failed: {e}")
        try:
            return self.rec.recognize_google(self.sr.AudioData(pcm, SAMPLE_RATE, 2), language="en-US")
        except self.sr.UnknownValueError:
            return None
        except Exception as e:
            log.error(f"Speech recognition failed: {e}")
            ui.toast("Speech recognition needs internet.", "warn")
            return None


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
            return {"ok": False, "error": f"No app named '{raw}' found on this computer"}
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
        return {"ok": False, "error": f"Nothing called '{name}' seems to be running."}
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
    return {"ok": False, "error": f"Windows refused to close {', '.join(targets)} (it may need admin rights)."}


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
            return {"ok": False, "error": "Could not reach the internet."}
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
        return {"ok": False, "error": f"'{path}' does not exist"}
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


def get_datetime():
    n = datetime.now()
    return {"time": n.strftime("%H:%M"), "date": n.strftime("%A, %d %B %Y")}


class Timers:
    def __init__(self):
        self.events = queue.Queue()

    def set(self, seconds, label=""):
        seconds = float(seconds)
        if not 0 < seconds <= 86400:
            return {"ok": False, "error": "invalid duration"}
        threading.Timer(seconds, lambda: self.events.put(
            f"{CFG['user_name'].capitalize()}, your {label + ' ' if label else ''}timer is done.")).start()
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
    "open_app": "Opening {name}", "close_app": "Closing {name}", "list_open_apps": "Scanning open windows",
    "open_path": "Opening {path}", "find_files": "Searching files: {name}", "open_website": "Opening {url}",
    "google_search": "Google: {query}", "youtube_search": "YouTube: {query}", "media_control": "Media: {action}",
    "set_volume": "Volume → {level}%", "window_control": "Window: {action}", "press_keys": "Keys: {keys}",
    "type_text": "Typing text", "take_screenshot": "Screenshot", "set_timer": "Timer: {seconds}s",
    "get_datetime": "Clock sync", "system_info": "System diagnostics", "power": "Power: {action}",
    "run_powershell": "PowerShell: {command}", "web_search": "Web: {query}",
    "read_webpage": "Reading {url}", "get_weather": "Weather: {city}",
}


def system_prompt():
    return f"""You are J.A.R.V.I.S., the personal AI of the user, running on their own computer. Speak like the AI butler from the films: calm, precise, loyal, quietly witty, very British. Call the user "{CFG['user_name']}" now and then.

Rules:
- Your replies are spoken aloud. Keep them short (usually one or two sentences). No markdown, lists, emojis, URLs or citations.
- Act ONLY on what the user explicitly asks. Never take actions on your own initiative, never "helpfully" do extra steps they didn't request. If a request is ambiguous, ask a short question instead of guessing.
- Use the tools to actually do things on the computer, then confirm briefly ("Chrome is up, {CFG['user_name']}.").
- Closing: use close_app (check list_open_apps if you're not sure of the name). Never close Jarvis itself.
- Risky actions (restart, shut down, sleep, sign out, any PowerShell) need a spoken confirmation first: say exactly what you'll do and ask "Shall I proceed?"; only after a clear yes call the tool with confirmed=true.
- You DO have internet access through web_search, read_webpage and get_weather. For anything current (news, weather, prices, sports, people, opening hours...) use them and answer in your own words. Never say you can't access the internet.
- Current local time: {datetime.now():%A %d %B %Y, %H:%M}."""


DANGEROUS = {"run_powershell"}
DANGEROUS_POWER = {"restart", "shutdown", "sleep", "sign_out"}


class Brain:
    def __init__(self, timers):
        import anthropic
        self.client = anthropic.Anthropic(api_key=CFG["anthropic_api_key"])
        self.timers = timers
        self.history = []
        self.web_search = True

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
        return self.client.messages.create(model=CFG["claude_model"], max_tokens=800,
                                           system=system_prompt(), tools=TOOLS, messages=self.history)

    def ask(self, text):
        self._trim()
        mark = len(self.history)
        self.history.append({"role": "user", "content": text})
        try:
            return self._loop()
        except Exception:
            del self.history[mark:]
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
            return " ".join(b.text for b in resp.content if b.type == "text").strip()
        return f"My apologies, {CFG['user_name']}, that got rather tangled. Could you try again?"

    def _trim(self, keep=30):
        h = self.history[-keep:]
        while h and not (h[0]["role"] == "user" and isinstance(h[0]["content"], str)):
            h = h[1:]
        self.history = h


class BasicBrain:
    """Used when no Claude key is set: simple commands only."""
    def __init__(self, timers):
        self.timers = timers

    def ask(self, t):
        s = t.lower().strip().rstrip(".!?")
        for p in ("open ", "launch ", "start "):
            if s.startswith(p):
                r = open_app(s[len(p):]); return "Right away." if r.get("ok") else r["error"]
        for p in ("close ", "quit ", "exit "):
            if s.startswith(p):
                r = close_app(s[len(p):]); return "Done." if r.get("ok") else r["error"]
        if s.startswith(("search for ", "google ")):
            google_search(s.split(" ", 2)[-1]); return "Here are the results."
        if s.startswith("play "):
            youtube_search(s[5:]); return "Searching YouTube."
        if "time" in s:
            return f"It's {get_datetime()['time']}."
        return "I'm running in basic mode. Add your Claude key in settings and I'll understand anything you say."


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
        ui.toast("Installing new components… (about a minute)")
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
STOP_PHRASES = ("that's all", "thats all", "thank you jarvis", "never mind", "nevermind", "go to sleep", "that will be all")


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
        s = text.lower().strip().rstrip(".!?")
        s = re.sub(r"^(hey |ok |okay )?jarvis[, ]*", "", s)
        s = re.sub(r"^(please |can you |could you |would you )", "", s).replace(" please", "").strip()
        if re.search(r"\b(update yourself|check for updates?|install (the )?update|upgrade yourself)\b", s):
            ui.action("Checking for updates")
            r = do_update()
            if not r.get("ok"):
                return "I couldn't reach the update server, " + CFG["user_name"] + "."
            if not r.get("updated"):
                return f"I'm already running the latest version, {CFG['user_name']}."
            self.speaker.say(f"Update {r['version']} installed. Restarting now.")
            restart_jarvis()
        m = re.match(r"^(close|quit|exit|kill|shut down|terminate)\s+(?:the\s+|my\s+)?(.+)$", s)
        if m and m.group(2) not in ("computer", "pc", "laptop", "the computer", "yourself", "jarvis"):
            app = re.sub(r"\s+(app|application|window|program)$", "", m.group(2))
            ui.action(f"Closing {app}")
            r = close_app(app)
            if r.get("ok"):
                return f"{app.title()} is closed, {CFG['user_name']}."
            ui.action("✗ " + r["error"])
            return None  # let Claude try (it can list open windows)
        m = re.match(r"^(open|launch|start|run)\s+(?:the\s+|my\s+)?(.+)$", s)
        if m and not any(w in m.group(2) for w in (" and ", " then ", "folder", "file", "website", ".com", "settings for")):
            app = m.group(2)
            ui.action(f"Opening {app}")
            r = open_app(app)
            if r.get("ok"):
                return f"Opening {app.title()}, {CFG['user_name']}."
            ui.action("✗ " + r["error"])
            return None
        return None

    def think(self, text):
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
                ui.toast("Error: " + msg[:180], "error")
                if "authentication" in low or "401" in msg or "api key" in low:
                    answer = "My connection to Claude was refused. Please check the API key in settings."
                elif "credit" in low or "billing" in low:
                    answer = "It seems the Claude account is out of credit."
                elif "connect" in low or "timeout" in low or "network" in low:
                    answer = "I can't reach my servers at the moment. Please check the internet connection."
                else:
                    answer = "Something went wrong on my end. The error is on the screen, sir."
        self.speaker.say(answer)

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
            ui.toast(f"Audio output error: {e}", "error")
            return
        self.build_brain()
        try:
            self.ears = Ears()
            ui.status({"mic": "online"})
        except Exception as e:
            log.exception("ears")
            ui.status({"mic": "offline"})
            ui.toast("Microphone / wake-word unavailable. You can still type commands.", "error")
        greet = datetime.now().hour
        part = "morning" if 5 <= greet < 12 else "afternoon" if greet < 18 else "evening"
        self.speaker.say(f"Good {part}, {CFG['user_name']}. All systems are online.")
        def bg_check():
            info = check_update()
            if info.get("available"):
                ui.js("updateAvailable", info["latest"])
                ui.toast(f"Update {info['latest']} available - click UPDATE or say 'update yourself'.")
        threading.Thread(target=bg_check, daemon=True).start()

        while True:
            kind, payload = self.wait_trigger()
            if kind == "text":
                self.think(payload)
                continue
            self.ears.beep()
            q = self.ears.listen()
            if not q:
                self.speaker.say(f"Yes, {CFG['user_name']}?")
                q = self.ears.listen()
            while q:
                if any(p in q.lower() for p in STOP_PHRASES) and len(q.split()) <= 5:
                    self.speaker.say(f"Very good, {CFG['user_name']}.")
                    break
                self.think(q)
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
            "has_eleven": bool(CFG["elevenlabs_api_key"]), "eleven_mask": _mask(CFG["elevenlabs_api_key"]),
            "voice_id": CFG["elevenlabs_voice_id"], "voice_name": CFG["elevenlabs_voice_name"],
            "user_name": CFG["user_name"], "wake_threshold": CFG["wake_threshold"],
            "speech_recognition": CFG["speech_recognition"],
        }

    def list_voices(self, key=""):
        import httpx
        key = key.strip() or CFG["elevenlabs_api_key"]
        if not key:
            return {"ok": False, "error": "Enter your ElevenLabs key first."}
        try:
            r = httpx.get("https://api.elevenlabs.io/v1/voices", headers={"xi-api-key": key}, timeout=20)
            if r.status_code != 200:
                return {"ok": False, "error": f"ElevenLabs said: {r.status_code} - check the key."}
            voices = []
            for v in r.json().get("voices", []):
                lab = v.get("labels") or {}
                desc = ", ".join(x for x in (lab.get("gender"), lab.get("accent"), lab.get("description")) if x)
                voices.append({"id": v["voice_id"], "name": v["name"], "desc": desc})
            return {"ok": True, "voices": voices}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def save_settings(self, s):
        if s.get("claude_key", "").strip():
            CFG["anthropic_api_key"] = s["claude_key"].strip()
        if s.get("eleven_key", "").strip():
            CFG["elevenlabs_api_key"] = s["eleven_key"].strip()
        if s.get("voice_id"):
            CFG["elevenlabs_voice_id"] = s["voice_id"]
            CFG["elevenlabs_voice_name"] = s.get("voice_name", "")
        if s.get("user_name", "").strip():
            CFG["user_name"] = s["user_name"].strip()
        if s.get("wake_threshold"):
            CFG["wake_threshold"] = float(s["wake_threshold"])
        if s.get("speech_recognition") in ("google", "elevenlabs"):
            CFG["speech_recognition"] = s["speech_recognition"]
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
                             args=(f"At your service, {CFG['user_name']}. How do I sound?",), daemon=True).start()

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
