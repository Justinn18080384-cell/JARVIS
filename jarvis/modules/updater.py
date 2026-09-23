"""Update-System.

Ablauf: prüfen → herunterladen → SHA-256 prüfen → Backup der Daten → Sicherung der
Programmdateien (Rollback) → Installer still ausführen → JARVIS startet neu.
Startet die neue Version dreimal nicht sauber, stellt JARVIS die alte Version selbst wieder her.

Update-Quelle (Einstellungen → Updates):
    - GitHub-Repository-URL (https://github.com/<nutzer>/<repo>) – nutzt das neueste Release
      mit Asset „JARVIS-Setup-x.y.z.exe“ und optional „….exe.sha256“
    - oder eine eigene Manifest-URL (JSON: {"version","url","sha256","notes"})
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import requests

from .. import VERSION
from ..core import log, paths
from ..core.actions import action
from ..core.config import config
from ..core.events import bus
from ..core.module import Module, Intent

M = "update"
PENDING = paths.LOCAL / "pending_update.json"
_mod = None


def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:4])


def fetch_manifest():
    url = (config.get("update.url") or "").strip()
    if not url:
        raise RuntimeError("Es ist keine Update-Quelle eingerichtet.")
    m = re.match(r"https?://github\.com/([^/]+)/([^/#?]+)", url)
    if m:
        api = f"https://api.github.com/repos/{m.group(1)}/{m.group(2).removesuffix('.git')}/releases/latest"
        rel = requests.get(api, timeout=15, headers={"Accept": "application/vnd.github+json"}).json()
        if "tag_name" not in rel:
            raise RuntimeError("Kein Release gefunden.")
        assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
        exe = next((n for n in assets if n.lower().endswith(".exe")), None)
        if not exe:
            raise RuntimeError("Das Release enthält keinen Installer.")
        sha = ""
        if exe + ".sha256" in assets:
            sha = requests.get(assets[exe + ".sha256"], timeout=15).text.split()[0].strip()
        return {"version": rel["tag_name"].lstrip("v"), "url": assets[exe], "sha256": sha, "notes": rel.get("body") or ""}
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    return r.json()


def check():
    man = fetch_manifest()
    newer = vtuple(man["version"]) > vtuple(VERSION)
    return newer, man


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(man):
    paths.UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    target = paths.UPDATE_DIR / f"JARVIS-Setup-{man['version']}.exe"
    with requests.get(man["url"], stream=True, timeout=30) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        done = 0
        with open(target, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
                done += len(chunk)
                if total:
                    bus.emit("update_progress", percent=round(done * 100 / total), stage="Download")
    if not man.get("sha256"):
        target.unlink(missing_ok=True)
        raise RuntimeError("Das Update hat keine Prüfsumme – aus Sicherheitsgründen abgebrochen.")
    if _sha256(target).lower() != man["sha256"].lower():
        target.unlink(missing_ok=True)
        raise RuntimeError("Die Prüfsumme des Updates stimmt nicht – Update verworfen.")
    return target


def install(man, jarvis):
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Updates können nur in der installierten Version eingespielt werden.")
    bus.emit("update_progress", percent=0, stage="Download")
    setup = download(man)
    bus.emit("update_progress", percent=100, stage="Sicherung")
    from .maintenance import create_backup
    create_backup("vor-update")
    # Programmdateien für Rollback sichern
    inst = paths.install_dir()
    rb = paths.ROLLBACK_DIR / VERSION
    shutil.rmtree(rb, ignore_errors=True)
    shutil.copytree(inst, rb, ignore=shutil.ignore_patterns("unins*"))
    PENDING.write_text(json.dumps({"from": VERSION, "to": man["version"], "rollback": str(rb), "install": str(inst),
                                   "attempts": 0, "time": time.time()}), "utf-8")
    log.activity(M, f"Update {VERSION} → {man['version']} wird installiert")
    bus.emit("update_progress", percent=100, stage="Installation")
    subprocess.Popen([str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/UPDATE=1"],
                     creationflags=0x00000008)  # DETACHED_PROCESS
    threading.Timer(1.0, jarvis.quit).start()


def startup_guard():
    """Wird ganz früh beim Start aufgerufen: erkennt fehlgeschlagene Updates."""
    if not PENDING.exists():
        return
    try:
        p = json.loads(PENDING.read_text("utf-8"))
    except Exception:
        PENDING.unlink(missing_ok=True)
        return
    if vtuple(VERSION) != vtuple(p["to"]):
        # Installer lief nicht durch – alte Version läuft noch
        if time.time() - p.get("time", 0) > 600:
            PENDING.unlink(missing_ok=True)
        return
    p["attempts"] = p.get("attempts", 0) + 1
    PENDING.write_text(json.dumps(p), "utf-8")
    if p["attempts"] >= 3 and os.path.isdir(p["rollback"]):
        rollback(p)


def rollback(p):
    """Stellt die vorherige Version wieder her (nachdem sich dieser Prozess beendet hat)."""
    exe = os.path.join(p["install"], "JARVIS.exe")
    script = (f"Start-Sleep -Seconds 2; Wait-Process -Id {os.getpid()} -ErrorAction SilentlyContinue; "
              f"Copy-Item -Path '{p['rollback']}\\*' -Destination '{p['install']}' -Recurse -Force; "
              f"Start-Process '{exe}' -ArgumentList '--rolled-back'")
    PENDING.unlink(missing_ok=True)
    subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script], creationflags=0x08000000)
    log.logger().error("Update fehlgeschlagen – Rollback auf %s", p["from"])
    os._exit(1)


def mark_started_ok():
    """Neue Version läuft stabil -> Update abschließen."""
    if PENDING.exists():
        try:
            p = json.loads(PENDING.read_text("utf-8"))
            if vtuple(VERSION) == vtuple(p["to"]):
                PENDING.unlink(missing_ok=True)
                log.activity(M, f"Update auf {VERSION} erfolgreich")
                bus.emit("notify", title="Update", text=f"JARVIS wurde auf Version {VERSION} aktualisiert.", speak=False)
                # ältere Rollback-Stände aufräumen (nur den letzten behalten)
                for d in sorted(paths.ROLLBACK_DIR.glob("*"))[:-1]:
                    shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass
    for f in paths.UPDATE_DIR.glob("JARVIS-Setup-*.exe"):
        try:
            f.unlink()
        except Exception:
            pass


@action("update_check", "Prüft, ob es eine neue JARVIS-Version gibt", {}, risk=0, module=M)
def update_check():
    try:
        newer, man = check()
    except Exception as e:
        return {"ok": False, "text": log.friendly(e)}
    if newer:
        _mod.available = man
        bus.emit("update_available", **man)
        return f"Version {man['version']} ist verfügbar. Sag „installiere das Update“ oder nutze die Einstellungen."
    return f"JARVIS ist aktuell (Version {VERSION})."


@action("update_install", "Lädt das neueste Update herunter, installiert es und startet JARVIS neu", {}, risk=2, module=M,
        confirm_text="Soll ich das Update jetzt installieren? JARVIS startet dabei neu.")
def update_install():
    man = _mod.available
    if not man:
        newer, man = check()
        if not newer:
            return f"JARVIS ist bereits aktuell (Version {VERSION})."
    threading.Thread(target=_mod.install_safe, args=(man,), daemon=True).start()
    return f"Update auf Version {man['version']} wird geladen. Ich melde mich gleich zurück."


class UpdaterModule(Module):
    name = "updater"
    title = "Updates"

    def __init__(self, jarvis):
        super().__init__(jarvis)
        global _mod
        _mod = self
        self.available = None
        self.error = ""

    def start(self):
        threading.Timer(20, self._auto).start()

    def _auto(self):
        mark_started_ok()
        if config.get("update.auto_check") and config.get("update.url"):
            try:
                newer, man = check()
                if newer:
                    self.available = man
                    bus.emit("update_available", **man)
                    bus.emit("notify", title="Update verfügbar", text=f"JARVIS {man['version']} ist verfügbar.", speak=False)
            except Exception as e:
                self.error = log.friendly(e)

    def install_safe(self, man):
        try:
            install(man, self.jarvis)
        except Exception as e:
            msg = log.error("Update", e)
            bus.emit("update_progress", percent=0, stage="Fehler: " + msg)
            self.jarvis.brain.reply("Das Update ist fehlgeschlagen: " + msg + " Deine Daten sind unverändert.")

    def parse(self, text, n, ctx):
        if re.search(r"(gibt es|such|suche|prüf|prüfe).*(update|aktualisierung)|updates?$", n) and "install" not in n:
            return Intent([("update_check", {})])
        if re.search(r"(installier|installiere|spiel|mach).*(update|aktualisierung)|aktualisiere dich", n):
            return Intent([("update_install", {})])
        return None

    def status(self):
        return {"version": VERSION, "available": self.available, "source": bool(config.get("update.url")), "error": self.error}

    def diagnose(self):
        if not config.get("update.url"):
            return [("Updates", True, "keine Update-Quelle eingerichtet")]
        try:
            newer, man = check()
            return [("Update-Server", True, f"erreichbar, neueste Version {man['version']}")]
        except Exception as e:
            return [("Update-Server", False, log.friendly(e))]
