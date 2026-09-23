"""Handy-Anbindung.

Android: über ADB (USB-Debugging oder WLAN-Debugging). Die Android-Platform-Tools lädt JARVIS
auf Wunsch selbst herunter.
iPhone: Windows erlaubt ohne Jailbreak keine Fernsteuerung – JARVIS erkennt das Gerät und zeigt
den Verbindungsstatus. Weitere Funktionen folgen über die geplante JARVIS-Handy-App (siehe remote.py).
"""
import io
import re
import shutil
import subprocess
import threading
import time
import zipfile

import requests

from ..core import log, paths
from ..core.actions import action, S, N
from ..core.events import bus
from ..core.module import Module, Intent

M = "handy"
CREATE_NO_WINDOW = 0x08000000
PLATFORM_TOOLS = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"
_mod = None


def adb_path():
    p = shutil.which("adb")
    if p:
        return p
    local = paths.TOOLS_DIR / "platform-tools" / "adb.exe"
    return str(local) if local.exists() else None


def adb(*args, serial=None, timeout=10, binary=False):
    exe = adb_path()
    if not exe:
        raise RuntimeError("ADB ist nicht installiert. Richte es unter Geräte → Handy ein.")
    cmd = [exe] + (["-s", serial] if serial else []) + list(args)
    r = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW)
    if binary:
        return r.stdout
    return (r.stdout or b"").decode("utf-8", "ignore").strip()


def android_devices():
    if not adb_path():
        return []
    out = adb("devices", "-l")
    devs = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            info = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
            devs.append({"serial": parts[0], "state": parts[1], "model": info.get("model", "Android").replace("_", " "),
                         "type": "android"})
    return devs


def iphone_devices():
    try:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        wmi = win32com.client.GetObject("winmgmts:")
        rows = wmi.ExecQuery("SELECT Name, Status FROM Win32_PnPEntity WHERE Name LIKE '%iPhone%' OR Name LIKE '%Apple Mobile Device%' OR Name LIKE '%iPad%'")
        seen, out = set(), []
        for r in rows:
            name = r.Name
            if "iPhone" in name or "iPad" in name:
                if name not in seen:
                    seen.add(name)
                    out.append({"serial": name, "state": "connected" if r.Status == "OK" else r.Status, "model": name, "type": "iphone"})
        return out
    except Exception:
        return []


def _device(serial=None):
    devs = [d for d in android_devices() if d["state"] == "device"]
    if not devs:
        unauthorized = [d for d in android_devices() if d["state"] == "unauthorized"]
        if unauthorized:
            raise RuntimeError("Bitte am Handy das USB-Debugging für diesen PC erlauben.")
        raise RuntimeError("Kein Android-Handy verbunden.")
    return devs[0]["serial"]


# =============================================================== Aktionen
@action("phone_status", "Zeigt den Status des verbundenen Handys (Akku, Modell)", {}, risk=0, module=M)
def phone_status():
    lines = []
    for d in android_devices():
        if d["state"] == "device":
            bat = adb("shell", "dumpsys", "battery", serial=d["serial"])
            lvl = re.search(r"level: (\d+)", bat)
            ac = re.search(r"(AC|USB) powered: true", bat)
            lines.append(f"{d['model']}: Akku {lvl.group(1) if lvl else '?'} %{' (lädt)' if ac else ''}")
        else:
            lines.append(f"{d['model']}: {d['state']}")
    for d in iphone_devices():
        lines.append(f"{d['model']}: per USB erkannt")
    if not lines:
        return "Es ist kein Handy verbunden."
    return "; ".join(lines) + "."


@action("phone_open_url", "Öffnet eine Webseite auf dem Android-Handy", {"url": S("URL")}, risk=1, module=M)
def phone_open_url(url):
    if not url.startswith("http"):
        url = "https://" + url
    adb("shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url, serial=_device())
    return "Auf dem Handy geöffnet."


@action("phone_key", "Sendet eine Taste an das Android-Handy",
        {"key": S("play, next, prev, volume_up, volume_down, home, back, lock", ["play", "next", "prev", "volume_up", "volume_down", "home", "back", "lock"])},
        risk=1, module=M)
def phone_key(key):
    codes = {"play": 85, "next": 87, "prev": 88, "volume_up": 24, "volume_down": 25, "home": 3, "back": 4, "lock": 26}
    adb("shell", "input", "keyevent", str(codes[key]), serial=_device())
    return "Erledigt."


@action("phone_screenshot", "Macht einen Screenshot vom Android-Handy und speichert ihn am PC", {}, risk=1, module=M)
def phone_screenshot():
    data = adb("exec-out", "screencap", "-p", serial=_device(), binary=True, timeout=20)
    paths.SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    out = paths.SCREENSHOT_DIR / time.strftime("Handy_%Y-%m-%d_%H-%M-%S.png")
    out.write_bytes(data)
    return {"text": "Handy-Screenshot gespeichert.", "data": str(out)}


@action("phone_launch", "Startet eine App auf dem Android-Handy", {"app": S("App-Name oder Paketname")}, risk=1, module=M)
def phone_launch(app):
    serial = _device()
    pkgs = adb("shell", "pm", "list", "packages", "-3", serial=serial).replace("package:", "").split()
    pkgs += ["com.android.chrome", "com.google.android.youtube", "com.whatsapp", "com.spotify.music", "com.android.settings"]
    from ..core.text import similarity
    best = max(pkgs, key=lambda p: max(similarity(app, p.split(".")[-1]), similarity(app, p)), default=None)
    if not best or max(similarity(app, best.split(".")[-1]), similarity(app, best)) < 0.55:
        return {"ok": False, "text": f"Die App „{app}“ finde ich auf dem Handy nicht."}
    adb("shell", "monkey", "-p", best, "-c", "android.intent.category.LAUNCHER", "1", serial=serial)
    return f"{app} wird auf dem Handy gestartet."


@action("phone_connect_wifi", "Verbindet ein Android-Handy per WLAN-Debugging", {"address": S("IP:Port")}, risk=1, module=M)
def phone_connect_wifi(address):
    out = adb("connect", address, timeout=15)
    return out or "Verbindung wird aufgebaut."


def install_adb(progress=None):
    """Lädt die offiziellen Android-Platform-Tools von Google herunter."""
    r = requests.get(PLATFORM_TOOLS, timeout=60)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        z.extractall(paths.TOOLS_DIR)
    log.activity(M, "Android-Platform-Tools installiert")
    return adb_path()


# ================================================================== Modul
class PhoneModule(Module):
    name = "phone"
    title = "Handy"

    def __init__(self, jarvis):
        super().__init__(jarvis)
        global _mod
        _mod = self
        self.devices = []

    def start(self):
        threading.Thread(target=self._watch, daemon=True, name="phone-watch").start()

    def _watch(self):
        known = set()
        while True:
            try:
                devs = android_devices() + iphone_devices()
                keys = {(d["serial"], d["state"]) for d in devs}
                for d in devs:
                    if (d["serial"], d["state"]) not in known and d["state"] in ("device", "connected"):
                        bus.emit("device_connected", name=d["model"])
                        bus.emit("notify", title="Handy verbunden", text=f"{d['model']} ist verbunden.", speak=False)
                        log.activity(M, f"{d['model']} verbunden")
                if keys != known:
                    bus.emit("devices_changed")
                known = keys
                self.devices = devs
            except Exception:
                pass
            time.sleep(8)

    def parse(self, text, n, ctx):
        if not re.search(r"\b(handy|smartphone|telefon|iphone|android)\b", n):
            return None
        if re.search(r"(akku|status|verbunden|ist .* da)", n):
            return Intent([("phone_status", {})])
        if re.search(r"screenshot", n):
            return Intent([("phone_screenshot", {})])
        m = re.search(r"(öffne|starte) (.+?) auf (dem |meinem )?(handy|smartphone|telefon)", n)
        if m:
            target = m.group(2)
            if re.search(r"\.\w{2,3}$", target):
                return Intent([("phone_open_url", {"url": target})])
            return Intent([("phone_launch", {"app": target})])
        if re.search(r"(pause|play|musik)", n):
            return Intent([("phone_key", {"key": "play"})])
        if re.search(r"sperr", n):
            return Intent([("phone_key", {"key": "lock"})])
        if re.search(r"lauter", n):
            return Intent([("phone_key", {"key": "volume_up"})])
        if re.search(r"leiser", n):
            return Intent([("phone_key", {"key": "volume_down"})])
        return None

    def status(self):
        return {"adb": bool(adb_path()), "devices": self.devices}

    def diagnose(self):
        if not adb_path():
            return [("Handy (ADB)", True, "ADB nicht eingerichtet – optional")]
        devs = android_devices() + iphone_devices()
        return [("Handy", True, ", ".join(f"{d['model']} ({d['state']})" for d in devs) or "kein Gerät verbunden")]
