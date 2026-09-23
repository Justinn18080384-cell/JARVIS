"""PC-Helfer: Downloads, Netzwerkdiagnose, Aufräumen, Zwischenablage-Verlauf, Desktop-Szenen, Absturz-Assistent."""
import ctypes
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import psutil

from ..core import log
from ..core.actions import action, S, N
from ..core.config import config
from ..core.db import db
from ..core.events import bus
from ..core.module import Module, Intent

M = "pc-helfer"
_mod = None
DOWNLOADS = Path.home() / "Downloads"
PARTIAL = (".crdownload", ".part", ".partial", ".tmp", ".download", ".opdownload")
NO_WINDOW = 0x08000000


def _mb(b):
    if b >= 1024 ** 3:
        return f"{b / 1024 ** 3:.1f} GB".replace(".", ",")
    if b >= 1024 ** 2:
        return f"{round(b / 1024 ** 2)} MB"
    return f"{max(1, round(b / 1024))} KB"


# ================================================================== Netzwerk
def _ping(host, count=3):
    """Durchschnittliche Antwortzeit in ms oder None."""
    try:
        # ping antwortet im DOS-Zeichensatz (Umlaute in „Zeitüberschreitung“) → "oem" statt Standard
        out = subprocess.run(["ping", "-n", str(count), "-w", "1500", host], capture_output=True, text=True,
                             encoding="oem", errors="replace", timeout=count * 2 + 3, creationflags=NO_WINDOW).stdout or ""
    except Exception:
        return None
    times = [int(t) for t in re.findall(r"(?:Zeit|time)[=<](\d+)\s*ms", out, re.I)]
    return round(sum(times) / len(times)) if times else None


def _gateway():
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "(Get-NetRoute -DestinationPrefix 0.0.0.0/0 | Sort-Object RouteMetric | Select-Object -First 1).NextHop"],
                             capture_output=True, text=True, encoding="oem", errors="replace", timeout=8,
                             creationflags=NO_WINDOW).stdout.strip()
        return out or None
    except Exception:
        return None


@action("network_check", "Netzwerkdiagnose: prüft Router, Internet, DNS und Ping und erklärt das Ergebnis", {}, risk=0, module=M)
def network_check():
    gw = _gateway()
    router = _ping(gw) if gw else None
    net = _ping("1.1.1.1")
    t = time.time()
    try:
        socket.getaddrinfo("www.google.com", 443)
        dns = round((time.time() - t) * 1000)
    except Exception:
        dns = None
    if router is None and gw:
        return "Dein Router antwortet nicht. Prüfe das Netzwerkkabel oder starte den Router neu."
    if net is None:
        return "Dein Router ist erreichbar, aber das Internet nicht. Vermutlich liegt eine Störung beim Anbieter vor – ein Router-Neustart hilft oft."
    if dns is None:
        return f"Das Internet ist erreichbar (Ping {net} ms), aber Webadressen werden nicht aufgelöst. Das ist ein DNS-Problem – ein Router-Neustart hilft meistens."
    verdict = "sehr gut" if net < 25 else "gut" if net < 50 else "etwas langsam" if net < 100 else "schlecht"
    tip = " Für Online-Spiele ist das zu hoch – schließe Downloads und Streams." if net >= 80 else ""
    return (f"Alles in Ordnung: Router {router if router is not None else '–'} ms, Internet-Ping {net} ms, "
            f"Namensauflösung {dns} ms. Deine Verbindung ist {verdict}.{tip}")


# ================================================================== Aufräumen
def _dir_size(p, older_than=0):
    total, now = 0, time.time()
    for root, _, files in os.walk(p, onerror=lambda e: None):
        for f in files:
            try:
                st = os.stat(os.path.join(root, f))
                if not older_than or now - st.st_mtime > older_than:
                    total += st.st_size
            except OSError:
                pass
    return total


class _SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("i64Size", ctypes.c_int64), ("i64NumItems", ctypes.c_int64)]


def _recycle_bin():
    info = _SHQUERYRBINFO(ctypes.sizeof(_SHQUERYRBINFO), 0, 0)
    try:
        ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info))
    except Exception:
        return 0, 0
    return info.i64Size, info.i64NumItems


@action("cleanup_scan", "Zeigt, wie viel Speicherplatz sich freimachen lässt (Temp-Dateien, Papierkorb, alte Downloads)", {}, risk=0, module=M)
def cleanup_scan():
    temp = _dir_size(os.environ.get("TEMP", ""), older_than=2 * 86400)
    rb, rb_n = _recycle_bin()
    old_dl = _dir_size(DOWNLOADS, older_than=60 * 86400) if DOWNLOADS.exists() else 0
    free = shutil.disk_usage("C:\\").free
    parts = [f"Auf Laufwerk C sind {_mb(free)} frei."]
    if temp > 50 * 1024 ** 2:
        parts.append(f"Alte temporäre Dateien: {_mb(temp)} – die kann ich gefahrlos löschen, sag „Räum die temporären Dateien auf“.")
    if rb > 0:
        parts.append(f"Im Papierkorb liegen {rb_n} Dateien mit {_mb(rb)}.")
    if old_dl > 500 * 1024 ** 2:
        parts.append(f"Downloads, die älter als zwei Monate sind: {_mb(old_dl)} – die schau dir am besten selbst an.")
    if len(parts) == 1:
        parts.append("Es gibt gerade nichts Nennenswertes aufzuräumen.")
    return " ".join(parts)


@action("cleanup_temp", "Löscht temporäre Dateien, die älter als zwei Tage sind", {}, risk=2, module=M,
        confirm_text="Soll ich die alten temporären Dateien löschen? Programme brauchen sie nicht mehr.")
def cleanup_temp():
    temp, freed, now = os.environ.get("TEMP", ""), 0, time.time()
    if not temp or not os.path.isdir(temp):
        return {"ok": False, "text": "Den Temp-Ordner finde ich nicht."}
    for root, dirs, files in os.walk(temp, topdown=False, onerror=lambda e: None):
        for f in files:
            p = os.path.join(root, f)
            try:
                st = os.stat(p)
                if now - st.st_mtime > 2 * 86400:
                    os.remove(p)
                    freed += st.st_size
            except OSError:
                pass                                   # in Benutzung – überspringen
        for d in dirs:
            try:
                os.rmdir(os.path.join(root, d))        # nur leere Ordner
            except OSError:
                pass
    log.activity(M, f"Temp-Dateien aufgeräumt: {_mb(freed)}")
    return f"Fertig, ich habe {_mb(freed)} freigemacht."


@action("recycle_bin_empty", "Leert den Papierkorb endgültig", {}, risk=3, module=M,
        confirm_text="Soll ich den Papierkorb wirklich leeren? Die Dateien sind danach endgültig weg.")
def recycle_bin_empty():
    size, n = _recycle_bin()
    if not n:
        return "Der Papierkorb ist schon leer."
    ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 0x7)    # ohne Nachfrage/Ton/Fortschritt
    return f"Papierkorb geleert – {_mb(size)} frei."


# ================================================================== Downloads
@action("download_open_latest", "Öffnet den zuletzt heruntergeladenen Download oder nennt ihn", {"open": {"type": "boolean"}}, required=[], risk=1, module=M)
def download_open_latest(open=True):
    files = [f for f in DOWNLOADS.glob("*") if f.is_file() and not f.name.lower().endswith(PARTIAL)] if DOWNLOADS.exists() else []
    if not files:
        return "In deinem Download-Ordner liegt nichts."
    f = max(files, key=lambda p: p.stat().st_mtime)
    if open:
        os.startfile(str(f))
        return f"Ich öffne {f.name}."
    return f"Zuletzt heruntergeladen: {f.name} ({_mb(f.stat().st_size)})."


# ================================================================== Zwischenablage
@action("clipboard_history", "Nennt die zuletzt kopierten Texte (Verlauf nur im Arbeitsspeicher)", {}, risk=0, module=M)
def clipboard_history():
    h = _mod.clip if _mod else []
    if not h:
        return "Seit dem Start habe ich noch keinen kopierten Text gesehen." if config.get("pchelp.clipboard_history", True) else "Der Zwischenablage-Verlauf ist ausgeschaltet."
    items = [f"{i}. {(t[:70] + '…') if len(t) > 70 else t}" for i, t in enumerate(h[:3], 1)]
    return "Zuletzt kopiert: " + " ".join(items)


@action("clipboard_restore", "Legt einen früher kopierten Text wieder in die Zwischenablage (1 = zuletzt, 2 = davor …)",
        {"index": N("1 = zuletzt kopiert, 2 = davor …")}, risk=1, module=M)
def clipboard_restore(index=2):
    h = _mod.clip if _mod else []
    i = int(index or 2) - 1
    if not 0 <= i < len(h):
        return {"ok": False, "text": "So weit reicht mein Verlauf nicht zurück."}
    _mod.set_clip(h[i])
    return "Liegt wieder in der Zwischenablage – du kannst es mit Strg+V einfügen."


# ================================================================== Desktop-Szenen
def _layout():
    import win32con
    import win32gui
    from .pc import windows
    out = []
    for w in windows.visible_windows():
        h = w["hwnd"]
        if windows.is_own(h) or w["process"].lower() in ("explorer.exe", "claude.exe"):
            continue
        try:
            exe = psutil.Process(w["pid"]).exe()
            placement = win32gui.GetWindowPlacement(h)
            out.append({"exe": exe, "process": w["process"], "title": w["title"][:80],
                        "rect": list(placement[4]), "max": placement[1] == win32con.SW_SHOWMAXIMIZED})
        except Exception:
            continue
    return out


def _apply(entry):
    import win32con
    import win32gui
    from .pc import windows
    for w in windows.visible_windows():
        if w["process"].lower() == entry["process"].lower():
            h = w["hwnd"]
            l, t, r, b = entry["rect"]
            win32gui.ShowWindow(h, win32con.SW_RESTORE)
            win32gui.MoveWindow(h, l, t, r - l, b - t, True)
            if entry.get("max"):
                win32gui.ShowWindow(h, win32con.SW_MAXIMIZE)
            return True
    return False


@action("scene_save", "Speichert die aktuelle Fensteranordnung als Desktop-Szene", {"name": S("Name der Szene, z. B. Arbeit")}, risk=1, module=M)
def scene_save(name):
    layout = _layout()
    if not layout:
        return {"ok": False, "text": "Ich sehe gerade keine Fenster, die ich speichern könnte."}
    scenes = db.kv_get("scenes", {})
    scenes[name.lower()] = {"name": name, "windows": layout, "ts": time.time()}
    db.kv_set("scenes", scenes)
    return f"Szene „{name}“ gespeichert mit {len(layout)} Fenstern. Sag „Stelle {name} wieder her“, um sie zurückzuholen."


@action("scene_restore", "Stellt eine gespeicherte Desktop-Szene wieder her (startet fehlende Programme, ordnet Fenster an)",
        {"name": S("Name der Szene")}, risk=1, module=M)
def scene_restore(name):
    from ..core.text import best_match
    scenes = db.kv_get("scenes", {})
    if not scenes:
        return {"ok": False, "text": "Du hast noch keine Szene gespeichert. Sag zum Beispiel: Speichere mein Fensterlayout als Arbeit."}
    key, _ = best_match(name.lower(), list(scenes), threshold=0.5)
    if not key:
        return {"ok": False, "text": f"Eine Szene „{name}“ kenne ich nicht. Gespeichert sind: {', '.join(s['name'] for s in scenes.values())}."}
    sc = scenes[key]
    running = {p.info["name"].lower() for p in psutil.process_iter(["name"]) if p.info["name"]}
    started = []
    for e in sc["windows"]:
        if e["process"].lower() not in running and os.path.exists(e["exe"]):
            try:
                subprocess.Popen([e["exe"]], cwd=os.path.dirname(e["exe"]))
                started.append(Path(e["exe"]).stem)
                running.add(e["process"].lower())
            except Exception:
                pass

    def arrange():
        for attempt in range(4):
            time.sleep(0 if attempt == 0 and not started else 4)
            missing = [e for e in sc["windows"] if not _apply(e)]
            if not missing:
                break
    threading.Thread(target=arrange, daemon=True).start()
    return f"Szene „{sc['name']}“ wird wiederhergestellt" + (f" – ich starte {', '.join(started)}." if started else ".")


@action("scene_list", "Listet gespeicherte Desktop-Szenen", {}, risk=0, module=M)
def scene_list():
    scenes = db.kv_get("scenes", {})
    return ("Gespeicherte Szenen: " + ", ".join(s["name"] for s in scenes.values()) + ".") if scenes else "Du hast noch keine Szene gespeichert."


# ================================================================== Modul
class PCHelpModule(Module):
    name = "pchelp"
    title = "PC-Helfer"

    def __init__(self, jarvis):
        super().__init__(jarvis)
        global _mod
        _mod = self
        self.clip = []                     # nur im Arbeitsspeicher, nie auf der Festplatte
        self._clip_seq = 0
        self._dl_seen = {}
        self._crash_last = time.time()
        self._crash_notified = {}

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="pchelp").start()

    def set_clip(self, text):
        import win32clipboard
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
        self._clip_seq = ctypes.windll.user32.GetClipboardSequenceNumber()

    def _loop(self):
        self._scan_downloads(initial=True)
        n = 0
        while True:
            time.sleep(1)
            n += 1
            try:
                if config.get("pchelp.clipboard_history", True):
                    self._check_clipboard()
                if n % 5 == 0 and config.get("pchelp.download_watch", True):
                    self._scan_downloads()
                if n % 30 == 0 and config.get("pchelp.crash_assistant", True):
                    self._check_crashes()
            except Exception as e:
                log.logger().warning("PC-Helfer: %s", e)

    def _check_clipboard(self):
        seq = ctypes.windll.user32.GetClipboardSequenceNumber()
        if seq == self._clip_seq:
            return
        self._clip_seq = seq
        import win32clipboard
        try:
            win32clipboard.OpenClipboard()
            try:
                if not win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                    return
                text = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            return
        text = (text or "").strip()
        if not text or len(text) > 5000:
            return
        self.clip = [text] + [t for t in self.clip if t != text][:19]

    def _scan_downloads(self, initial=False):
        if not DOWNLOADS.exists():
            return
        now = time.time()
        for f in DOWNLOADS.iterdir():
            try:
                if not f.is_file() or f.name.lower().endswith(PARTIAL) or f.name.startswith("~"):
                    continue
                st = f.stat()
            except OSError:
                continue
            key = str(f)
            if initial:
                self._dl_seen[key] = ("done", st.st_size)
                continue
            prev = self._dl_seen.get(key)
            if prev is None:
                self._dl_seen[key] = ("new", st.st_size)       # neu – erst melden, wenn die Größe stabil ist
            elif prev[0] == "new" and prev[1] == st.st_size and now - st.st_mtime > 3:
                self._dl_seen[key] = ("done", st.st_size)
                bus.emit("download_done", name=f.name)
                bus.emit("notify", title="Download fertig", text=f"{f.name} ({_mb(st.st_size)}) ist fertig heruntergeladen.",
                         speak=False, priority=0)
            elif prev[0] == "new":
                self._dl_seen[key] = ("new", st.st_size)

    def _check_crashes(self):
        """Neue Absturzmeldungen (Ereignis 1000/1002) aus dem Windows-Anwendungsprotokoll."""
        import win32evtlog
        since, newest = self._crash_last, self._crash_last
        h = win32evtlog.OpenEventLog(None, "Application")
        try:
            flags = win32evtlog.EVENTLOG_BACKWARDS_READ | win32evtlog.EVENTLOG_SEQUENTIAL_READ
            done = False
            while not done:
                events = win32evtlog.ReadEventLog(h, flags, 0)
                if not events:
                    break
                for ev in events:
                    ts = ev.TimeGenerated.timestamp()
                    if ts <= since:
                        done = True
                        break
                    newest = max(newest, ts)
                    if ev.EventID & 0xFFFF in (1000, 1002) and ev.SourceName in ("Application Error", "Application Hang") and ev.StringInserts:
                        self._on_crash(ev.StringInserts[0], hang=(ev.EventID & 0xFFFF) == 1002)
        finally:
            win32evtlog.CloseEventLog(h)
        self._crash_last = newest

    def _on_crash(self, exe_name, hang=False):
        stem = Path(exe_name).stem
        if stem.lower() in ("jarvis", "python", "pythonw", "msedgewebview2", "searchhost", "explorer"):
            return
        from . import habits, focus
        app = None
        if habits._mod:
            hit = habits._mod.app_for(exe_name, "")
            app = hit[0] if hit else None
        verb = "reagiert nicht mehr und wurde beendet" if hang else "ist abgestürzt"
        # Nur echte Programme und Spiele melden – Hintergrunddienste (z. B. Audio-Treiber) stürzen teils ständig ab
        if not app and stem.lower() not in focus.GAMES:
            log.logger().info("Absturz eines Hintergrundprogramms: %s", exe_name)
            return
        if time.time() - self._crash_notified.get(stem, 0) < 3600:
            return
        self._crash_notified[stem] = time.time()
        what = app or stem
        log.activity(M, f"{what} {verb}", ok=False)
        text = f"{what} {verb}."
        brain = self.jarvis.brain
        if app and not brain.ctx.pending:
            text += " Soll ich es neu starten?"
            brain.ctx.pending = {"type": "confirm", "calls": [("app_open", {"name": app})], "time": time.time()}
            bus.emit("confirm", text=text)
        bus.emit("notify", title="Absturz", text=text, speak=True, priority=2)

    def parse(self, text, n, ctx):
        if re.search(r"(ist|wie ist) (mein|das) (internet|netzwerk|wlan|ping)|internet (langsam|kaputt|weg|geht nicht)|netzwerk ?(diagnose|test|prüf)|prüf (mein|das) (internet|netzwerk)|wie ist mein ping|habe ich (internet|lag)", n):
            return Intent([("network_check", {})])
        if re.search(r"(wie viel|wieviel) (platz|speicher)(platz)? (kann ich|lässt sich) (frei|aufräum)|speicher ?(aufräumen|analysieren|prüfen)|was kann ich (löschen|aufräumen)|pc aufräumen", n):
            return Intent([("cleanup_scan", {})])
        if re.search(r"(räum|lösch)\w* (die )?(temporären|temp|tmp)", n):
            return Intent([("cleanup_temp", {})])
        if re.search(r"papierkorb (leeren|ausleeren)|leer\w* (den )?papierkorb", n):
            return Intent([("recycle_bin_empty", {})])
        if re.search(r"(öffne|zeig) (mir )?(den )?(letzten|neuesten) download", n):
            return Intent([("download_open_latest", {"open": True})])
        if re.search(r"was (habe|hab) ich (zuletzt|als letztes) (heruntergeladen|gedownloadet)", n):
            return Intent([("download_open_latest", {"open": False})])
        if re.search(r"was (habe|hab) ich (vorhin|zuletzt|eben|gerade) kopiert|zwischenablage ?verlauf|verlauf der zwischenablage", n):
            return Intent([("clipboard_history", {})])
        m = re.search(r"(kopier|hol)\w* (mir )?(den |das )?(vorletzte|zweite|dritte|vorvorletzte)n? (eintrag|text)?", n)
        if m:
            idx = {"vorletzte": 2, "zweite": 2, "dritte": 3, "vorvorletzte": 3}[m.group(4)]
            return Intent([("clipboard_restore", {"index": idx})])
        m = re.search(r"speicher\w* (mein |das )?(fenster ?layout|fensteranordnung|layout|desktop|szene) als (?P<name>.+)$", n)
        if m:
            return Intent([("scene_save", {"name": m.group("name").strip().title()})])
        m = re.search(r"(stell\w*|lade|hol\w*) (das |mein |die )?(layout |szene |fensterlayout )?(?P<name>.+?) wieder( her)?$", n)
        if m:
            from ..core.text import best_match
            key, _ = best_match(m.group("name"), list(db.kv_get("scenes", {})), threshold=0.7)
            if key:   # nur wenn es eine Szene mit dem Namen gibt – sonst ist ein Fenster gemeint
                return Intent([("scene_restore", {"name": key})])
        if re.search(r"welche (szenen|layouts)", n):
            return Intent([("scene_list", {})])
        return None

    def status(self):
        return {"clipboard_items": len(self.clip)}
