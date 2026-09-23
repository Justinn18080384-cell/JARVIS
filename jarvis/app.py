"""JARVIS-Anwendung: verbindet Module, Oberfläche, Tray und Event-System."""
import json
import os
import re
import sys
import threading
import time

from . import VERSION, APP_NAME
from .core import log, paths, secrets
from .core.actions import action, registry, B
from .core.brain import Brain
from .core.config import config
from .core.db import db
from .core.events import bus
from .core.module import Module, Intent


# ================================================================ Kernaktionen
_app = None


@action("privacy_mode", "Schaltet den Datenschutzmodus (nur lokale Verarbeitung) ein oder aus", {"on": B("an?")}, risk=0, module="sicherheit")
def privacy_mode(on=True):
    config.set("security.privacy_mode", bool(on))
    return "Datenschutzmodus aktiv: Ich verarbeite jetzt alles lokal, ohne KI-Dienste." if on else "Datenschutzmodus beendet."


@action("wake_word", "Schaltet das Wake-Word „Jarvis“ ein oder aus", {"on": B("an?")}, risk=0, module="sprache")
def wake_word(on=True):
    config.set("voice.wake_word", bool(on))
    return "Ich höre jetzt auf „Hey Jarvis“." if on else "Wake-Word ist aus. Nutze den Mikrofon-Knopf."


@action("conversation_mode", "Dauerhaftes Zuhören für Folgefragen ein-/ausschalten", {"on": B("an?")}, risk=0, module="sprache")
def conversation_mode(on=True):
    config.set("voice.always_listen", bool(on))
    return "Gesprächsmodus an: Nach meiner Antwort höre ich direkt weiter zu." if on else "Gesprächsmodus aus."


@action("web_search_toggle", "Websuche der KI ein-/ausschalten", {"on": B("an?")}, risk=0, module="ki")
def web_search_toggle(on=True):
    config.set("ai.web_search", bool(on))
    return "Websuche aktiviert." if on else "Websuche deaktiviert."


@action("emergency_stop", "Not-Aus: stoppt Sprache und alle Automationen", {}, risk=0, module="sicherheit", ai=False)
def emergency_stop():
    _app.brain.emergency_stop(announce=False)
    return "Not-Aus ausgelöst."


class CoreModule(Module):
    name = "core"
    title = "Kern"

    def parse(self, text, n, ctx):
        def onoff():
            return not re.search(r"\b(aus|deaktivier|beend|abschalt|stopp)", n)
        if re.search(r"datenschutz ?modus|privatmodus|privater modus", n):
            return Intent([("privacy_mode", {"on": onoff()})])
        if re.search(r"wake ?word|aktivierungswort", n):
            return Intent([("wake_word", {"on": onoff()})])
        if re.search(r"gesprächsmodus|(hör|höre) (mir )?(dauerhaft|weiter|immer) zu", n):
            return Intent([("conversation_mode", {"on": onoff()})])
        if re.search(r"websuche|internetsuche", n) and re.search(r"(an|aus|aktivier|deaktivier)", n):
            return Intent([("web_search_toggle", {"on": onoff()})])
        if re.match(r"^(hallo|hi|hey|guten (tag|abend)|servus|na)$", n) or n == "":
            name = config.get("user_name")
            h = time.localtime().tm_hour
            greet = "Guten Morgen" if h < 11 else "Guten Tag" if h < 18 else "Guten Abend"
            return Intent(reply=f"{greet}{', ' + name if name else ''}. Wie kann ich helfen?")
        if re.match(r"^(danke|vielen dank|dankeschön|super|perfekt|top)$", n):
            return Intent(reply="Sehr gern.")
        if re.search(r"^(was kannst du|hilfe|was kann ich (dich )?fragen|zeig (mir )?(deine )?befehle)$", n):
            return Intent(reply="Ich kann Programme und Spiele starten und schließen, Fenster steuern, Lautstärke und Audiogerät ändern, "
                                "Screenshots machen, Dateien finden, mir Dinge über dich merken, Profile und Routinen per Sprache anlegen, "
                                "Smart-Home-Geräte schalten, dein Handy einbinden, dir ein Morgen-Briefing mit Wetter geben, "
                                "Themen recherchieren und – mit OpenAI oder Ollama – beliebige Fragen beantworten "
                                "und deinen Bildschirm verstehen. Sag zum Beispiel: „Wenn ich FiveM sage, starte Discord und FiveM.“")
        if re.match(r"^(wer bist du|wie heißt du)$", n):
            return Intent(reply=f"Ich bin JARVIS, Version {VERSION} – dein persönlicher Assistent.")
        return None


# ================================================================== Jarvis
class Jarvis:
    def __init__(self, background=False):
        global _app
        _app = self
        self.background = background
        self.window = None
        self.tray = None
        self.ui_ready = False
        self._pending_js = []
        self._last_level = 0
        self.modules: dict[str, Module] = {}
        self.brain = Brain(self)

        from .modules.automation import AutomationModule
        from .modules.memory import MemoryModule
        from .modules.voice import VoiceModule
        from .modules.pc import PCModule
        from .modules.smarthome import SmartHomeModule
        from .modules.phone import PhoneModule
        from .modules.ai import AIModule
        from .modules.maintenance import MaintenanceModule
        from .modules.updater import UpdaterModule
        from .modules.watcher import WatcherModule
        from .modules.remote import RemoteModule
        from .modules.briefing import BriefingModule
        from .modules.costs import CostsModule

        # Reihenfolge = Priorität beim Sprachverständnis
        for cls in (AutomationModule, CoreModule, MemoryModule, VoiceModule, MaintenanceModule, UpdaterModule,
                    BriefingModule, CostsModule, PCModule, SmartHomeModule, PhoneModule, AIModule, WatcherModule, RemoteModule):
            try:
                m = cls(self)
                self.modules[m.name] = m
            except Exception as e:
                log.error(f"Modul {cls.__name__} konnte nicht geladen werden", e)

        bus.on("*", self._forward)
        bus.on("notify", self._notify)

    def module_order(self):
        return list(self.modules.values())

    def start_modules(self):
        for m in self.modules.values():
            try:
                m.start()
            except Exception as e:
                log.error(f"Start von Modul {m.title}", e)
        log.activity("system", f"JARVIS {VERSION} gestartet")

    # ------------------------------------------------------------ Sprache
    def speak(self, text):
        v = self.modules.get("voice")
        if v:
            v.speak(text)

    def voice_stop(self):
        v = self.modules.get("voice")
        if v:
            v.stop_speaking()
            v.listener.cancel_record()

    def is_speaking(self):
        v = self.modules.get("voice")
        return bool(v and v.is_speaking())

    # --------------------------------------------------------- UI-Events
    def _forward(self, event, data):
        if event == "level":
            now = time.time()
            if now - self._last_level < 0.04:
                return
            self._last_level = now
        try:
            js = f"window.JARVIS && JARVIS.event({json.dumps(event)}, {json.dumps(data, default=str)})"
        except Exception:
            return
        if not self.ui_ready or not self.window:
            if event != "level":
                self._pending_js.append(js)
                self._pending_js = self._pending_js[-100:]
            return
        try:
            self.window.evaluate_js(js)
        except Exception:
            pass

    def ui_loaded(self):
        self.ui_ready = True
        pend, self._pending_js = self._pending_js, []
        for js in pend:
            try:
                self.window.evaluate_js(js)
            except Exception:
                pass

    def _notify(self, event, data):
        if not config.get("app.notifications", True):
            return
        if data.get("speak"):
            self.speak(data.get("text", ""))
        visible = False
        try:
            visible = self.window and not self.window_hidden
        except Exception:
            pass
        if self.tray and not visible:
            try:
                self.tray.notify(data.get("text", ""), data.get("title", APP_NAME))
            except Exception:
                pass

    window_hidden = False

    # ------------------------------------------------------------- Fenster
    def show(self):
        if self.window:
            self.window.show()
            self.window.restore()
            self.window_hidden = False
            try:
                from .modules.pc import windows as w
                import win32gui
                hwnd = win32gui.FindWindow(None, APP_NAME)
                if hwnd:
                    w.focus(hwnd)
            except Exception:
                pass

    def hide(self):
        if self.window:
            self.window.hide()
            self.window_hidden = True

    def quit(self):
        log.activity("system", "JARVIS beendet")
        for m in self.modules.values():
            try:
                m.stop()
            except Exception:
                pass
        try:
            if self.tray:
                self.tray.stop()
        except Exception:
            pass
        try:
            db.close()
        except Exception:
            pass
        try:
            if self.window:
                self.window.destroy()
        except Exception:
            pass
        os._exit(0)

    # --------------------------------------------------------------- Tray
    def start_tray(self):
        try:
            import pystray
            from PIL import Image
            ico = paths.ASSETS_DIR / "jarvis.ico"
            img = Image.open(ico) if ico.exists() else Image.new("RGB", (64, 64), (0, 180, 255))

            def wake_label(item):
                return "Wake-Word: an" if config.get("voice.wake_word") else "Wake-Word: aus"

            menu = pystray.Menu(
                pystray.MenuItem("JARVIS öffnen", lambda: self.show(), default=True),
                pystray.MenuItem("Zuhören", lambda: self.modules["voice"].toggle_listen()),
                pystray.MenuItem(wake_label, lambda: config.set("voice.wake_word", not config.get("voice.wake_word"))),
                pystray.MenuItem("Not-Aus", lambda: self.brain.emergency_stop()),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Beenden", lambda: self.quit()),
            )
            self.tray = pystray.Icon("JARVIS", img, f"JARVIS {VERSION}", menu)
            threading.Thread(target=self.tray.run, daemon=True, name="tray").start()
        except Exception as e:
            log.error("System-Tray", e)

    # ------------------------------------------------------------- Status
    def api_status(self):
        return {
            "version": VERSION,
            "halted": self.brain.halted,
            "busy": self.brain.busy,
            "modules": {name: self._safe_status(m) for name, m in self.modules.items()},
        }

    @staticmethod
    def _safe_status(m):
        try:
            return m.status()
        except Exception as e:
            return {"error": log.friendly(e)}


def set_autostart(on: bool):
    import winreg
    key = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
        if on and getattr(sys, "frozen", False):
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, f'"{sys.executable}" --background')
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass
