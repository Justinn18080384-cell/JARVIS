"""Backup, Wiederherstellung und Selbstdiagnose."""
import json
import shutil
import sqlite3
import threading
import time
import zipfile
from pathlib import Path

from .. import VERSION
from ..core import log, paths
from ..core.actions import action, S
from ..core.config import config
from ..core.db import db
from ..core.module import Module, Intent

M = "wartung"
KEEP = 15
_mod = None


def create_backup(reason="manuell") -> Path:
    paths.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    out = paths.BACKUP_DIR / time.strftime(f"JARVIS-Backup_%Y-%m-%d_%H-%M-%S_{reason}.zip")
    # konsistente Kopie der Datenbank (auch während JARVIS läuft)
    tmp_db = paths.CACHE_DIR / "backup.db"
    paths.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(paths.DB_FILE)
    dst = sqlite3.connect(tmp_db)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(tmp_db, "jarvis.db")
        if paths.CONFIG_FILE.exists():
            z.write(paths.CONFIG_FILE, "settings.json")
        z.writestr("meta.json", json.dumps({"version": VERSION, "time": time.time(), "reason": reason}))
    tmp_db.unlink(missing_ok=True)
    # alte Backups aufräumen
    backups = sorted(paths.BACKUP_DIR.glob("JARVIS-Backup_*.zip"))
    for old in backups[:-KEEP]:
        old.unlink(missing_ok=True)
    log.activity(M, f"Backup erstellt ({reason})")
    return out


def list_backups():
    out = []
    for p in sorted(paths.BACKUP_DIR.glob("JARVIS-Backup_*.zip"), reverse=True):
        out.append({"file": p.name, "size": p.stat().st_size, "time": p.stat().st_mtime})
    return out


def restore_backup(name: str):
    p = paths.BACKUP_DIR / Path(name).name
    if not p.exists():
        raise FileNotFoundError(name)
    create_backup("vor-wiederherstellung")
    with zipfile.ZipFile(p) as z:
        db.close()
        z.extract("jarvis.db", paths.CACHE_DIR)
        shutil.move(str(paths.CACHE_DIR / "jarvis.db"), paths.DB_FILE)
        for ext in ("-wal", "-shm"):
            Path(str(paths.DB_FILE) + ext).unlink(missing_ok=True)
        if "settings.json" in z.namelist():
            z.extract("settings.json", paths.DATA)
            config.load()
    log.activity(M, f"Backup wiederhergestellt: {p.name}")


def check_database():
    try:
        r = db.one("PRAGMA integrity_check")
        ok = list(r.values())[0] == "ok"
        facts = db.one("SELECT COUNT(*) AS n FROM facts")["n"]
        return ok, f"Datenbank {'in Ordnung' if ok else 'beschädigt'}, {facts} Einträge im Gedächtnis"
    except Exception as e:
        return False, log.friendly(e)


@action("backup_create", "Erstellt ein Backup von Gedächtnis, Profilen, Routinen und Einstellungen", {}, risk=0, module=M)
def backup_create():
    p = create_backup()
    return f"Backup erstellt: {p.name}."


@action("self_diagnose", "Führt eine Selbstdiagnose aller Module durch", {}, risk=0, module=M)
def self_diagnose():
    results = _mod.run_diagnose()
    bad = [r for r in results if not r["ok"]]
    if not bad:
        return {"text": f"Selbstdiagnose abgeschlossen: alle {len(results)} Prüfungen in Ordnung.", "data": results}
    return {"text": f"{len(bad)} von {len(results)} Prüfungen mit Problemen: " +
                    "; ".join(f"{r['name']}: {r['msg']}" for r in bad), "data": results}


class MaintenanceModule(Module):
    name = "maintenance"
    title = "Wartung"

    def __init__(self, jarvis):
        super().__init__(jarvis)
        global _mod
        _mod = self

    def start(self):
        threading.Thread(target=self._auto_backup, daemon=True, name="auto-backup").start()

    def _auto_backup(self):
        time.sleep(60)
        while True:
            try:
                if config.get("app.auto_backup", True):
                    last = db.kv_get("last_auto_backup", 0)
                    if time.time() - last > 86400:
                        create_backup("automatisch")
                        db.kv_set("last_auto_backup", time.time())
            except Exception as e:
                log.error("Automatisches Backup", e)
            time.sleep(3600)

    def run_diagnose(self):
        results = []
        ok, msg = check_database()
        results.append({"module": "Gedächtnis", "name": "Datenbank", "ok": ok, "msg": msg})
        for mod in self.jarvis.modules.values():
            if mod is self:
                continue
            try:
                for name, ok, msg in mod.diagnose():
                    results.append({"module": mod.title, "name": name, "ok": bool(ok), "msg": msg})
            except Exception as e:
                results.append({"module": mod.title, "name": mod.title, "ok": False, "msg": log.friendly(e)})
        free = shutil.disk_usage(paths.DATA).free
        results.append({"module": "System", "name": "Speicherplatz", "ok": free > 500 * 2**20, "msg": f"{free / 2**30:.1f} GB frei"})
        log.activity(M, f"Selbstdiagnose: {sum(r['ok'] for r in results)}/{len(results)} in Ordnung")
        return results

    def parse(self, text, n, ctx):
        import re
        if re.search(r"(selbstdiagnose|diagnose|systemcheck|prüf(e)? dich|funktioniert alles|check dich)", n):
            return Intent([("self_diagnose", {})])
        if re.search(r"(mach|erstelle|erstell) (ein |eine )?(backup|sicherung)|sichere (alles|meine daten)", n):
            return Intent([("backup_create", {})])
        return None
