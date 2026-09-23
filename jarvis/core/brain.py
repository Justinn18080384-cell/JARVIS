"""Der Brain entscheidet, wie eine Eingabe verarbeitet wird.

Reihenfolge (lokal zuerst – KI nur, wenn nötig):
    1. Not-Aus, Bestätigungen und offene Rückfragen
    2. „nochmal“ / Wiederholen
    3. Eigene Befehle des Nutzers
    4. Lokale Parser der Module (PC, Gedächtnis, Smart Home ...)
    5. OpenAI (mit denselben Aktionen als Werkzeuge)
"""
import queue
import re
import threading
import time

from . import log
from .actions import registry, Result
from .config import config
from .events import bus
from .module import Intent
from .text import normalize, best_match

YES = re.compile(r"^(ja|jo|jep|yes|klar|okay|ok|mach( das| es)?|bestätigt?|genau|richtig|sicher|jawohl|tu es|los)\b")
NO = re.compile(r"^(nein|nee|no|nö|abbrechen|abbruch|stopp?|lass es|lieber nicht|doch nicht|vergiss es)\b")
KILL = re.compile(r"^(not ?aus|stopp? alles|alles stopp|halt stopp|notstopp|sei still|ruhe|halt die klappe|stopp?|halt)$")
REPEAT = re.compile(r"^(mach (das|es) (noch ?mal|nochmals|erneut)|noch ?mal|nochmals|wiederhole? (das|es)?|wiederholen|das gleiche nochmal)$")


class Context:
    def __init__(self):
        self.source = "text"
        self.trusted = False
        self.last_calls = []
        self.last_app = None          # zuletzt geöffnetes Programm {name, exe}
        self.last_window = None       # letztes Fremdfenster (hwnd)
        self.last_file = None
        self.history = []             # KI-Gesprächsverlauf
        self.pending = None           # {"type": "confirm"|"choose", ...}
        self.cancel = threading.Event()


class Brain:
    def __init__(self, jarvis):
        self.jarvis = jarvis
        self.ctx = Context()
        self.q = queue.Queue()
        self.busy = False
        self.halted = False           # Not-Aus aktiv: Automationen pausiert
        threading.Thread(target=self._worker, daemon=True, name="brain").start()

    # ------------------------------------------------------------------ API
    def submit(self, text: str, source: str = "text", on_reply=None):
        """on_reply(text) erhält die Antworten dieser Anfrage, on_reply(None) signalisiert das Ende."""
        text = (text or "").strip()
        if not text:
            if on_reply:
                on_reply(None)
            return
        # Not-Aus sofort, nicht erst in der Warteschlange
        if KILL.match(normalize(text)):
            self.emergency_stop()
            if on_reply:
                on_reply("Alles gestoppt. Automationen sind pausiert.")
                on_reply(None)
            return
        if source == "remote":
            bus.emit("reply", text=text, kind="user remote")
        self.q.put((text, source, on_reply))

    def emergency_stop(self, announce=True):
        self.ctx.cancel.set()
        self.jarvis.voice_stop()
        self.halted = True
        self.ctx.pending = None
        with self.q.mutex:
            self.q.queue.clear()
        log.activity("sicherheit", "Not-Aus ausgelöst – Sprache und Automationen gestoppt")
        bus.emit("halted", halted=True)
        if announce:
            self.reply("Alles gestoppt. Automationen sind pausiert, bis du sie wieder freigibst.", speak=False)

    def resume(self):
        self.halted = False
        self.ctx.cancel.clear()
        bus.emit("halted", halted=False)
        log.activity("sicherheit", "Automationen wieder freigegeben")

    _reply_cb = None

    def reply(self, text, speak=True, kind="jarvis", priority=None):
        """priority=None: direkte Antwort an den Nutzer – wird immer gesprochen.
        Sonst (z. B. zeitgesteuerte Routine): nur, wenn der aktive Modus es erlaubt."""
        if not text:
            return
        bus.emit("reply", text=text, kind=kind)
        cb = self._reply_cb
        if cb:
            try:
                cb(text)
            except Exception:
                pass
        if speak and self.ctx.source == "remote" and cb and not config.get("remote.speak_on_pc", False):
            speak = False
        if speak and priority is not None:
            from ..modules import focus
            speak = focus.may_speak(priority)
        if speak:
            self.jarvis.speak(text)

    # --------------------------------------------------------------- worker
    def _worker(self):
        while True:
            text, source, cb = self.q.get()
            self.busy = True
            self._reply_cb = cb
            self.ctx.cancel.clear()
            try:
                bus.emit("state", state="thinking")
                answer = self.handle(text, source)
                if answer:
                    self.reply(answer)
            except Exception as e:  # nie einen Traceback beim Nutzer zeigen
                self.reply("Da ist etwas schiefgelaufen: " + log.error("Verarbeitung", e))
            finally:
                self._reply_cb = None
                if cb:
                    try:
                        cb(None)
                    except Exception:
                        pass
                self.busy = False
                if not self.jarvis.is_speaking():
                    bus.emit("state", state="idle")

    def handle(self, text: str, source: str) -> str:
        ctx = self.ctx
        ctx.source = source
        ctx.trusted = False
        norm = normalize(text)
        log.logger().info("Eingabe (%s): %s", source, text)

        if not norm:
            return "Ja? Wie kann ich helfen?"

        # 1. Offene Rückfrage / Bestätigung
        if ctx.pending:
            r = self._resolve_pending(norm)
            if r is not None:
                return r

        if norm in ("weiter", "fortsetzen", "automationen fortsetzen", "entsperren", "freigeben") and self.halted:
            self.resume()
            return "Automationen sind wieder aktiv."

        # 2. Wiederholen
        if REPEAT.match(norm):
            if not ctx.last_calls:
                return "Ich habe noch nichts getan, das ich wiederholen könnte."
            return self.execute(ctx.last_calls, remember=False)

        # 3./4. Lokale Verarbeitung
        intent = self._local(text, norm)
        if intent:
            if intent.reply and not intent.calls:
                return intent.reply
            out = self.execute(intent.calls)
            return (intent.reply + " " + out).strip() if intent.reply else out

        # 5. KI
        ai = self.jarvis.modules.get("ai")
        if ai and ai.available():
            return ai.chat(text, ctx)
        if config.get("security.privacy_mode"):
            return "Das kann ich lokal nicht beantworten, und der Datenschutzmodus verhindert eine KI-Anfrage."
        return ("Das habe ich leider nicht verstanden. Für freie Fragen richte bitte "
                "einen OpenAI-Schlüssel in den Einstellungen ein.")

    def _local(self, text, norm) -> Intent | None:
        best = None
        for mod in self.jarvis.module_order():
            try:
                intent = mod.parse(text, norm, self.ctx)
            except Exception as e:
                log.error(f"Parser {mod.name}", e)
                continue
            if intent and (best is None or intent.confidence > best.confidence):
                best = intent
                if intent.confidence >= 1.0:
                    break
        return best

    # ------------------------------------------------------------ execution
    def execute(self, calls, remember=True, trusted=False) -> str:
        """Führt Aktionen mit Sicherheitsprüfung aus."""
        ctx = self.ctx
        level = int(config.get("security.confirm_level", 2))
        for name, args in calls:
            act = registry.get(name)
            if not act:
                continue
            needs = act.risk >= level and not (trusted and act.risk < 3)
            if needs:
                txt = act.confirm_text or f"Soll ich „{act.description}“ wirklich ausführen?"
                try:
                    txt = txt.format(**{k: v for k, v in (args or {}).items()})
                except Exception:
                    pass
                ctx.pending = {"type": "confirm", "calls": calls, "time": time.time()}
                bus.emit("confirm", text=txt)
                return txt
        return self._run(calls, remember)

    def _run(self, calls, remember=True) -> str:
        ctx = self.ctx
        bus.emit("state", state="executing")
        texts = []
        for name, args in calls:
            if ctx.cancel.is_set():
                texts.append("Abgebrochen.")
                break
            act = registry.get(name)
            try:
                res = registry.call(name, args, ctx)
            except Exception as e:
                res = Result(False, log.error(f"Aktion {name}", e))
            desc = act.description if act else name
            log.activity(act.module if act else "aktion", f"{desc} {self._fmt_args(args)} → {res.text or ('ok' if res.ok else 'fehlgeschlagen')}", res.ok)
            if isinstance(res.data, dict) and res.data.get("choose"):
                # Aktion braucht eine Rückfrage (mehrdeutig)
                ctx.pending = {"type": "choose", "action": name, "args": args,
                               "param": res.data["param"], "options": res.data["choose"], "time": time.time()}
                return res.text
            if res.text:
                texts.append(res.text)
        if remember and calls and all(registry.get(n) and registry.get(n).risk > 0 for n, _ in calls):
            ctx.last_calls = calls
        return " ".join(texts)

    @staticmethod
    def _fmt_args(args):
        if not args:
            return ""
        return "(" + ", ".join(f"{k}={v}" for k, v in args.items() if k != "ctx") + ")"

    def _resolve_pending(self, norm):
        p = self.ctx.pending
        if time.time() - p.get("time", 0) > 120:
            self.ctx.pending = None
            return None
        if p["type"] == "confirm":
            if YES.match(norm):
                self.ctx.pending = None
                return self._run(p["calls"])
            if NO.match(norm):
                self.ctx.pending = None
                return "In Ordnung, abgebrochen."
            self.ctx.pending = None
            return None
        if p["type"] == "choose":
            if NO.match(norm):
                self.ctx.pending = None
                return "Okay, dann nicht."
            opts = p["options"]
            pick = None
            ordinals = {"erste": 0, "ersten": 0, "1": 0, "zweite": 1, "zweiten": 1, "2": 1,
                        "dritte": 2, "dritten": 2, "3": 2, "vierte": 3, "4": 3, "letzte": -1, "letzten": -1}
            for w, i in ordinals.items():
                if re.search(rf"\b{w}\b", norm):
                    try:
                        pick = opts[i]
                    except IndexError:
                        pass
                    break
            if pick is None:
                pick, _ = best_match(norm, opts, threshold=0.45)
            if pick is None:
                self.ctx.pending = None
                return None
            self.ctx.pending = None
            args = dict(p["args"])
            args[p["param"]] = pick
            args["exact"] = True
            return self.execute([(p["action"], args)])
        return None
