"""Schutz vor umgeleiteten Datenordnern (App-Container).

Wird JARVIS aus einer paketierten App heraus gestartet (z. B. einer MSIX-App wie der Claude-Desktop-App),
leitet Windows alle Schreibzugriffe auf %APPDATA% still in einen privaten Ordner um:
    %LOCALAPPDATA%\\Packages\\<Paket>\\LocalCache\\Roaming\\JARVIS
Beim nächsten normalen Start sieht JARVIS diese Daten nicht – Gedächtnis und Einstellungen wirken „weg“.

Dieses Modul
  1. erkennt die Umleitung mit einer Probedatei und startet JARVIS dann außerhalb neu (über den Explorer),
  2. holt Daten aus solchen umgeleiteten Kopien zurück, wenn sie mehr enthalten als der echte Ordner.
Nur Standardbibliothek – läuft vor dem Laden von Einstellungen und Datenbank.
"""
import glob
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import paths

RELAUNCH_MARKER = Path(os.environ.get("TEMP", str(paths.LOCAL))) / "jarvis-relaunch.txt"


def _virtual_roots(name=""):
    local = os.environ.get("LOCALAPPDATA", "")
    return [Path(p) for p in glob.glob(os.path.join(local, "Packages", "*", "LocalCache", "Roaming", name))]


def redirected() -> bool:
    """True, wenn Schreibzugriffe auf %APPDATA% gerade in einen App-Container umgeleitet werden."""
    if os.environ.get("JARVIS_DATA"):
        return False  # eigener Datenordner (Tests) – nicht eingreifen
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return False
    probe = f".jarvis-probe-{uuid.uuid4().hex[:8]}"
    try:
        Path(appdata, probe).write_text("x")
    except Exception:
        return False
    try:
        return bool(_virtual_roots(probe))
    finally:
        try:
            Path(appdata, probe).unlink()
        except Exception:
            pass


def relaunch_outside() -> bool:
    """Startet JARVIS über den Windows-Explorer neu – der läuft außerhalb des Containers.
    Gibt False zurück, wenn gerade erst neu gestartet wurde (Schutz vor Endlosschleife)."""
    try:
        if RELAUNCH_MARKER.exists() and time.time() - RELAUNCH_MARKER.stat().st_mtime < 60:
            return False
        RELAUNCH_MARKER.write_text(str(time.time()))
    except Exception:
        pass
    if getattr(sys, "frozen", False):
        target = sys.executable
    else:
        # Entwicklungsmodus: kleine Startdatei neben dem Projekt (Laufwerk wird nicht umgeleitet)
        root = paths.app_dir().parent
        pyw = Path(sys.executable).with_name("pythonw.exe")
        target = str(root / "JARVIS starten.bat")
        Path(target).write_text(f'@start "" "{pyw}" "{root / "run.py"}"\r\n', encoding="ascii")
    try:
        subprocess.Popen(["explorer.exe", target], close_fds=True)
        return True
    except Exception:
        return False


# ------------------------------------------------------------------ Datenrettung
def _richness(db_file: Path) -> int:
    """Wie viele eigene Einträge eine Datenbank enthält (Gedächtnis zählt am meisten)."""
    if not db_file.exists():
        return 0
    try:
        con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=5)
        total = 0
        for table, weight in (("facts", 50), ("automations", 20), ("devices", 10), ("costs", 1), ("activity", 1)):
            try:
                total += weight * con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                pass
        con.close()
        return total
    except sqlite3.Error:
        return 0


def recover_redirected_data():
    """Übernimmt Daten aus einer umgeleiteten Kopie, wenn sie reichhaltiger ist als der echte Ordner.
    Der bisherige Stand wird vorher gesichert, die Kopie danach umbenannt (nie gelöscht).
    Liefert eine Meldung für den Nutzer oder None."""
    if os.environ.get("JARVIS_DATA"):
        return None
    real = paths.DATA
    marker = real / "uebernommene-kopien.json"
    try:
        done = json.loads(marker.read_text("utf-8"))
    except Exception:
        done = {}
    best, best_score = None, 0
    for root in _virtual_roots("JARVIS"):
        score = _richness(root / "jarvis.db")
        if score <= done.get(str(root), -1):
            continue  # diese Kopie wurde schon übernommen und hat seitdem nichts Neues bekommen
        if score > best_score:
            best, best_score = root, score
    if not best or best_score <= _richness(real / "jarvis.db"):
        return None

    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    real.mkdir(parents=True, exist_ok=True)
    keep = real / f"vor-wiederherstellung-{stamp}"
    keep.mkdir(exist_ok=True)
    for f in ("jarvis.db", "jarvis.db-wal", "jarvis.db-shm", "settings.json"):
        if (real / f).exists():
            shutil.move(str(real / f), str(keep / f))
    # Konsistente Kopie über die SQLite-Backup-Funktion (inkl. noch nicht übernommener WAL-Daten)
    src = sqlite3.connect(str(best / "jarvis.db"), timeout=10)
    dst = sqlite3.connect(str(real / "jarvis.db"))
    src.backup(dst)
    dst.execute("PRAGMA journal_mode=WAL")
    dst.close()
    src.close()
    if (best / "settings.json").exists():
        shutil.copy2(best / "settings.json", real / "settings.json")
    (real / "backups").mkdir(exist_ok=True)
    for z in (best / "backups").glob("*.zip"):
        if not (real / "backups" / z.name).exists():
            shutil.copy2(z, real / "backups" / z.name)
    done[str(best)] = best_score
    try:
        marker.write_text(json.dumps(done, indent=1), "utf-8")
    except OSError:
        pass
    try:
        best.rename(best.with_name(f"JARVIS.uebernommen-{stamp}"))
    except OSError:
        pass  # z. B. noch geöffnet – der Merker verhindert eine doppelte Übernahme
    return ("Ich habe dein Gedächtnis, deine Automationen und Einstellungen wiederhergestellt – sie lagen "
            "in einem umgeleiteten Ordner. Der vorherige Stand ist gesichert in „" + keep.name + "“.")
