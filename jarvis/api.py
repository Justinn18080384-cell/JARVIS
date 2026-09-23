"""Schnittstelle für die Oberfläche (wird per pywebview als window.pywebview.api bereitgestellt).
Alle Einstellungen laufen hierüber – niemand muss Dateien von Hand bearbeiten."""
import os
import threading
import time
import uuid

from . import VERSION
from .core import log, paths, secrets
from .core.actions import registry
from .core.config import config
from .core.db import db
from .core.events import bus


def safe(fn):
    """Fehler als verständliche Meldung an die Oberfläche statt Traceback."""
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:
            return {"error": log.error(f"Oberfläche: {fn.__name__}", e)}
    wrapper.__name__ = fn.__name__
    return wrapper


class API:
    def __init__(self, jarvis):
        self._j = jarvis

    # ------------------------------------------------------------ Allgemein
    @safe
    def ready(self):
        self._j.ui_loaded()
        return self.state()

    @safe
    def state(self):
        cfg = config.all()
        cfg["remote"]["token"] = "••••" if cfg["remote"].get("token") else ""
        return {"version": VERSION, "config": cfg, "has_key": secrets.has("openai_api_key"),
                "has_ha_token": secrets.has("homeassistant_token"),
                "has_eleven_key": secrets.has("elevenlabs_api_key"), "status": self._j.api_status(),
                "background": self._j.background, "data_dir": str(paths.DATA)}

    def js_error(self, msg):
        log.logger().error("UI-Fehler: %s", msg)

    @safe
    def send(self, text):
        self._j.brain.submit(text, "text")
        return True

    @safe
    def toggle_mic(self):
        return self._j.modules["voice"].toggle_listen()

    @safe
    def stop_speaking(self):
        self._j.voice_stop()
        return True

    @safe
    def emergency_stop(self):
        self._j.brain.emergency_stop()
        return True

    @safe
    def resume(self):
        self._j.brain.resume()
        return True

    # ------------------------------------------------------------- Fenster
    def win_minimize(self):
        self._j.window.minimize()

    def win_toggle_max(self):
        w = self._j.window
        if getattr(self, "_maxed", False):
            w.restore()
            self._maxed = False
        else:
            w.maximize()
            self._maxed = True

    def win_fullscreen(self):
        self._j.window.toggle_fullscreen()

    def win_close(self):
        if config.get("app.close_to_tray", True) and self._j.tray:
            self._j.hide()
        else:
            self._j.quit()

    def quit(self):
        threading.Timer(0.2, self._j.quit).start()

    # ----------------------------------------------------------- Dashboard
    @safe
    def dashboard(self):
        from .modules.pc import system
        from .modules import memory, automation, smarthome, costs, focus, habits
        s = system.stats()
        return {
            "habits": habits._mod.dashboard() if habits._mod else None,
            "costs": costs.summary(),
            "missed": focus.missed_list(10),
            "system": s,
            "status": self._j.api_status(),
            "memory_count": len(memory.all_facts()),
            "automations": len(automation.load_all()),
            "rooms": len(smarthome.rooms()), "devices": len(smarthome.devices()),
            "activity": db.query("SELECT * FROM activity ORDER BY id DESC LIMIT 8"),
        }

    # ------------------------------------------------------------ Gedächtnis
    @safe
    def memory_list(self):
        from .modules import memory
        return memory.all_facts()

    @safe
    def memory_set(self, label, value, old_key=None):
        from .modules import memory
        if old_key and old_key != memory._key(label):
            db.execute("DELETE FROM facts WHERE key=?", (old_key,))
        return memory.remember(label, value)

    @safe
    def memory_delete(self, key):
        db.execute("DELETE FROM facts WHERE key=?", (key,))
        log.activity("gedächtnis", f"Eintrag gelöscht: {key}")
        return True

    # ----------------------------------------------------------- Automationen
    @safe
    def automations(self):
        from .modules import automation
        return [dict(a, description=automation.describe(a)) for a in automation.load_all()]

    @safe
    def automation_save(self, a):
        from .modules import automation
        steps_text = a.pop("steps_text", None)
        if steps_text is not None:
            lines = [l.strip() for l in steps_text.splitlines() if l.strip()]
            a["steps"] = automation.compile_steps(lines)
        a["triggers"] = [t.strip() for t in a.get("triggers", []) if t.strip()]
        if not a.get("schedule") or not a["schedule"].get("time"):
            a["schedule"] = None
        return automation.save(a)

    @safe
    def automation_delete(self, aid):
        from .modules import automation
        automation.delete(aid)
        return True

    @safe
    def automation_run(self, aid):
        from .modules import automation
        a = next((x for x in automation.load_all() if x["id"] == aid), None)
        if not a:
            return False

        def run():
            res = automation._mod.run(a)
            txt = res if isinstance(res, str) else res.get("text", "")
            self._j.brain.reply(txt, speak=False)
        threading.Thread(target=run, daemon=True).start()
        return True

    # --------------------------------------------------------- Smart Home
    @safe
    def smarthome(self):
        from .modules import smarthome
        return {"rooms": smarthome.rooms(), "devices": smarthome.devices(),
                "ha_url": config.get("smarthome.homeassistant_url"), "ha_token": secrets.has("homeassistant_token")}

    @safe
    def room_add(self, name):
        return registry.call("room_add", {"name": name}).text

    @safe
    def room_remove(self, name):
        return registry.call("room_remove", {"name": name}).text

    @safe
    def device_add(self, name, room, type, address=""):
        return registry.call("device_add", {"name": name, "room": room, "type": type, "address": address}).text

    @safe
    def device_remove(self, did):
        db.execute("DELETE FROM devices WHERE id=?", (did,))
        bus.emit("devices_changed")
        return True

    @safe
    def device_toggle(self, did):
        from .modules import smarthome
        d = db.one("SELECT * FROM devices WHERE id=?", (did,))
        smarthome._set(d, d["state"] != "on")
        return True

    @safe
    def ha_setup(self, url, token):
        config.set("smarthome.homeassistant_url", url.strip())
        if token and token != "••••":
            secrets.set("homeassistant_token", token.strip())
        from .modules.smarthome import PROVIDERS
        ok, msg = PROVIDERS["homeassistant"].test()
        return {"ok": ok, "msg": msg}

    @safe
    def ha_import(self):
        return registry.call("homeassistant_import", {}).text

    # ----------------------------------------------------------------- Handy
    @safe
    def phone(self):
        from .modules import phone, remote
        r = self._j.modules.get("remote")
        running = bool(r and r.server)
        return {"adb": bool(phone.adb_path()), "devices": phone.android_devices() + phone.iphone_devices(),
                "remote": {"enabled": config.get("remote.enabled"), "running": running, "port": config.get("remote.port"),
                           "url": remote.pairing_url().split("#")[0], "qr": remote.pairing_qr() if running else "",
                           "speak_on_pc": config.get("remote.speak_on_pc"), "clients": r.status()["clients"] if r else 0}}

    @safe
    def remote_new_token(self):
        self._j.modules["remote"].new_token()
        return True

    @safe
    def phone_install_adb(self):
        from .modules import phone
        phone.install_adb()
        return True

    @safe
    def phone_connect(self, address):
        return registry.call("phone_connect_wifi", {"address": address}).text

    @safe
    def remote_set(self, enabled):
        config.set("remote.enabled", bool(enabled))
        r = self._j.modules.get("remote")
        if enabled:
            if not r.enable():
                return {"error": "Die Handy-App konnte nicht starten – ist der Port belegt? (Protokoll prüfen)"}
        else:
            r.disable()
        return True

    # ------------------------------------------------------------ Protokoll
    @safe
    def activity(self, limit=200, only_errors=False):
        sql = "SELECT * FROM activity" + (" WHERE ok=0" if only_errors else "") + " ORDER BY id DESC LIMIT ?"
        return db.query(sql, (int(limit),))

    @safe
    def activity_clear(self):
        db.execute("DELETE FROM activity")
        return True

    @safe
    def open_logs(self):
        os.startfile(paths.LOG_DIR)
        return True

    # ---------------------------------------------------- Diagnose & Backup
    @safe
    def diagnose(self):
        return self._j.modules["maintenance"].run_diagnose()

    @safe
    def backups(self):
        from .modules import maintenance
        return maintenance.list_backups()

    @safe
    def backup_create(self):
        from .modules import maintenance
        return maintenance.create_backup().name

    @safe
    def backup_restore(self, name):
        from .modules import maintenance
        maintenance.restore_backup(name)
        threading.Timer(1.5, self._j.restart).start()     # sauber neu laden
        return True

    @safe
    def data_export(self):
        """Gedächtnis, Routinen, Geräte und Einstellungen als eine Datei in „Dokumente“ ablegen."""
        import shutil
        import subprocess
        from pathlib import Path
        from .modules import maintenance
        p = maintenance.create_backup("export")
        docs = Path.home() / "Documents"
        docs.mkdir(exist_ok=True)
        dest = docs / time.strftime("JARVIS-Export_%Y-%m-%d_%H-%M.zip")
        shutil.copy2(p, dest)
        subprocess.Popen(["explorer", "/select,", str(dest)])
        log.activity("wartung", f"Daten exportiert: {dest.name}")
        return str(dest)

    @safe
    def data_import(self):
        """JARVIS-Sicherung auswählen, einspielen und neu starten."""
        import shutil
        import zipfile
        from pathlib import Path
        import webview
        from .modules import maintenance
        res = self._j.window.create_file_dialog(webview.FileDialog.OPEN, file_types=("JARVIS-Sicherung (*.zip)",))
        if not res:
            return {"ok": False, "msg": "Abgebrochen."}
        src = Path(res[0] if isinstance(res, (list, tuple)) else res)
        try:
            with zipfile.ZipFile(src) as z:
                if "jarvis.db" not in z.namelist():
                    return {"ok": False, "msg": "Das ist keine JARVIS-Sicherung."}
        except zipfile.BadZipFile:
            return {"ok": False, "msg": "Die Datei ist beschädigt oder keine ZIP-Datei."}
        paths.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        name = time.strftime("JARVIS-Backup_%Y-%m-%d_%H-%M-%S_import.zip")
        shutil.copy2(src, paths.BACKUP_DIR / name)
        maintenance.restore_backup(name)
        log.activity("wartung", f"Daten importiert aus {src.name}")
        threading.Timer(1.5, self._j.restart).start()
        return {"ok": True, "msg": "Importiert – JARVIS startet gleich neu."}

    # ------------------------------------------------------- Einstellungen
    @safe
    def settings_set(self, values: dict):
        values = {k: v for k, v in values.items() if k != "remote.token"}
        config.update(values)
        if "app.start_with_windows" in values:
            from .app import set_autostart
            set_autostart(bool(values["app.start_with_windows"]))
        return self.state()

    @safe
    def set_api_key(self, key):
        secrets.set("openai_api_key", (key or "").strip())
        ai = self._j.modules.get("ai")
        if ai:
            ai._client = None
        if not key:
            return {"ok": True, "msg": "Schlüssel entfernt."}
        ok, msg = ai.test()
        return {"ok": ok, "msg": msg}

    # ------------------------------------------------------ Kommandozentrale
    _weather_cache = (0.0, None)

    @safe
    def home(self):
        """Alle Werte für die Widgets der Startseite in einer Abfrage (wird alle 2 s aufgerufen)."""
        import datetime
        from .modules.pc import system
        from .modules import costs, focus, automation
        s = system.stats()
        c = costs.summary()
        now = datetime.datetime.now()
        upcoming = []
        for a in automation.load_all():
            sch = a.get("schedule")
            if not a.get("enabled", True) or not sch or not sch.get("time"):
                continue
            h, m = (int(x) for x in sch["time"].split(":"))
            days = sch.get("days") or list(range(7))
            for add in range(8):
                d = now + datetime.timedelta(days=add)
                when = d.replace(hour=h, minute=m, second=0, microsecond=0)
                if d.weekday() in days and when > now:
                    label = "Heute" if add == 0 else "Morgen" if add == 1 else ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"][when.weekday()]
                    upcoming.append({"name": a["name"], "when": f"{label} {sch['time']}", "ts": when.timestamp()})
                    break
        upcoming.sort(key=lambda x: x["ts"])
        return {
            "cpu": s["cpu"], "ram": s["ram"], "ram_used": s["ram_used"], "ram_total": s["ram_total"],
            "gpu": s["gpu"], "battery": s["battery"],
            "costs": {"today": c["today"], "month": c["month"], "elevenlabs_chars": c["elevenlabs_chars"],
                      "elevenlabs_free": c["elevenlabs_free"]},
            "focus": self._j.modules["focus"].status() if "focus" in self._j.modules else {},
            "missed": focus.missed_count(),
            "upcoming": upcoming[:3],
            "automations": len(automation.load_all()),
            "weather": self._weather(),
            "ai": self._j.modules["ai"].status() if "ai" in self._j.modules else {},
            "voice": {**(self._j.modules["voice"].status() if "voice" in self._j.modules else {}),
                      "clap": bool(config.get("voice.clap_wake"))},
        }

    def _weather(self):
        """Aktuelles Wetter am Wohnort – 15 Minuten zwischengespeichert, Abruf im Hintergrund."""
        ts, data = API._weather_cache
        if time.time() - ts > 900 and not getattr(API, "_weather_loading", False):
            API._weather_loading = True

            def load():
                from .modules import briefing
                result = None
                try:
                    city = briefing._city("")
                    if city:
                        name, w = briefing.weather_data(city)
                        cur, d = w["current"], w["daily"]
                        result = {"city": name, "temp": round(cur["temperature_2m"]), "desc": briefing.WMO.get(cur["weather_code"], ""),
                                  "code": cur["weather_code"], "min": round(d["temperature_2m_min"][0]), "max": round(d["temperature_2m_max"][0]),
                                  "rain": d["precipitation_probability_max"][0]}
                    else:
                        result = {"city": ""}
                except Exception as e:
                    log.logger().warning("Wetter-Widget: %s", e)
                    result = API._weather_cache[1]
                API._weather_cache = (time.time(), result)
                API._weather_loading = False
            threading.Thread(target=load, daemon=True).start()
        return data

    @safe
    def focus_set(self, mode):
        from .modules import focus
        return focus.focus_set(mode)

    @safe
    def habits_clear(self):
        from .core.db import db
        from .modules import habits
        db.execute("DELETE FROM usage")
        db.execute("DELETE FROM launches")
        if habits._mod:
            habits._mod._buf = {}
        return True

    @safe
    def missed_clear(self):
        from .modules import focus
        focus.mark_seen()
        return True

    @safe
    def set_elevenlabs_key(self, key):
        from .modules import voice
        secrets.set("elevenlabs_api_key", (key or "").strip())
        if not key:
            if config.get("voice.tts_engine") == "elevenlabs":
                config.set("voice.tts_engine", "edge")
            return {"ok": True, "msg": "Schlüssel entfernt."}
        ok, _, msg = voice.elevenlabs_voices()
        return {"ok": ok, "msg": msg}

    @safe
    def test_openai(self):
        ok, msg = self._j.modules["ai"].test()
        return {"ok": ok, "msg": msg}

    @safe
    def models(self):
        return self._j.modules["ai"].list_models()

    @safe
    def voices(self):
        from .modules import voice
        return voice.list_voices()

    @safe
    def audio_devices(self):
        from .modules import voice
        return voice.audio_devices()

    @safe
    def test_voice(self, text=None):
        self._j.speak(text or "Guten Tag. Ich bin JARVIS, Ihr persönlicher Assistent. Alle Systeme sind bereit.")
        return True

    @safe
    def mic_level(self):
        """1,5 Sekunden Mikrofonpegel für den Einrichtungsassistenten."""
        import numpy as np
        import sounddevice as sd
        from .modules.voice import _dev_index
        rec = sd.rec(int(16000 * 1.5), samplerate=16000, channels=1, dtype="int16",
                     device=_dev_index(config.get("voice.input_device"), "input"))
        sd.wait()
        return float(np.sqrt(np.mean(rec.astype(np.float32) ** 2)))

    @safe
    def apps(self):
        from .modules.pc import index
        return [{"name": a["name"], "source": a.get("source", "")} for a in index.apps]

    @safe
    def apps_rescan(self):
        return registry.call("apps_rescan", {}).text

    @safe
    def update_check(self):
        return registry.call("update_check", {}).text

    @safe
    def update_install(self):
        return registry.call("update_install", {}).text

    @safe
    def setup_complete(self, data: dict):
        if data.get("user_name"):
            config.set("user_name", data["user_name"].strip())
            from .modules import memory
            memory.remember("Name", data["user_name"].strip())
        config.set("setup_done", True)
        log.activity("system", "Ersteinrichtung abgeschlossen")
        return True
