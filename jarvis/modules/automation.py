"""Profile, Routinen und eigene Befehle – komplett per Sprache/Chat erstellbar.

Eine Automation besteht aus:
    type      profile | routine | command
    name      Anzeigename
    triggers  Sätze, die sie auslösen („FiveM“, „Gaming-Modus starten“)
    steps     [{"text": "starte Discord", "calls": [[action, args], ...], "delay": 3}, ...]
    schedule  {"time": "22:30", "days": [0..6]}  (optional)
    event     {"type": "startup"|"app_started"|"battery_low"|"device_connected", "value": "..."} (optional)
"""
import datetime
import json
import re
import threading
import time
import uuid

from ..core import log
from ..core.actions import action, registry, S, N, B, Result
from ..core.db import db
from ..core.events import bus
from ..core.module import Module, Intent
from ..core.text import normalize, similarity, split_list, find_number, ascii_fold

M = "automation"
TYPE_NAMES = {"profile": "Profil", "routine": "Routine", "command": "Befehl"}
DAYS = {"montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3, "freitag": 4, "samstag": 5, "sonntag": 6}
VERBS = r"(starte|start|öffne|schließe|schließ|beende|mach|setze|stell|stelle|schalte|spiele|sag|sage|sperre|wechsle|minimiere|google|such|suche|aktiviere|deaktiviere|warte)"

_mod = None  # Instanz, damit Aktionen auf den Brain zugreifen können


# ------------------------------------------------------------------ Speicher
def load_all(kind=None):
    rows = db.query("SELECT * FROM automations" + (" WHERE type=?" if kind else "") + " ORDER BY name", (kind,) if kind else ())
    out = []
    for r in rows:
        d = json.loads(r["data"])
        d.update(id=r["id"], type=r["type"], name=r["name"], enabled=bool(r["enabled"]))
        out.append(d)
    return out


def save(a: dict):
    a.setdefault("id", uuid.uuid4().hex[:10])
    data = {k: v for k, v in a.items() if k not in ("id", "type", "name", "enabled")}
    db.execute("INSERT OR REPLACE INTO automations(id,type,name,data,enabled,updated) VALUES(?,?,?,?,?,?)",
               (a["id"], a["type"], a["name"], json.dumps(data, ensure_ascii=False), int(a.get("enabled", True)), time.time()))
    bus.emit("automations_changed")
    return a


def delete(aid):
    db.execute("DELETE FROM automations WHERE id=?", (aid,))
    bus.emit("automations_changed")


def find(name, kind=None, threshold=0.72):
    best, score = None, 0
    for a in load_all(kind):
        for cand in [a["name"]] + a.get("triggers", []):
            s = similarity(name, cand)
            if s > score:
                best, score = a, s
    return best if score >= threshold else None


# ---------------------------------------------------------------- Kompilieren
def compile_steps(texts, default_delay=0):
    """Wandelt natürliche Sätze in Aktionsschritte um (lokal, ohne KI)."""
    steps = []
    last_verb = None
    for raw in texts:
        t = raw.strip()
        if not t:
            continue
        n = normalize(t)
        wm = re.match(r"^(warte|pause|danach|dann)? ?(warte )?(\d+|\w+) sekunden?( warten| pause)?$", n)
        if wm and find_number(n) is not None:
            steps.append({"text": t, "wait": find_number(n)})
            continue
        vm = re.match(VERBS + r"\b", n)
        if vm:
            last_verb = vm.group(1)
        elif last_verb:
            t = f"{last_verb} {t}"
        calls = _mod.compile_text(t) if _mod else None
        if not calls and not vm and not last_verb and _mod:
            # „Steam“ allein in einem Profil bedeutet „starte Steam“
            calls = _mod.compile_text(f"starte {t}")
            if calls:
                t = f"starte {t}"
        step = {"text": t, "calls": calls or []}
        if default_delay and steps:
            step["delay"] = default_delay
        steps.append(step)
    return steps


def parse_schedule(n):
    """‚jeden Tag um 22:30‘, ‚werktags um 7 Uhr‘, ‚am Samstag um 10‘ -> dict"""
    m = re.search(r"um (\d{1,2})(?:[:.](\d{2})| uhr (\d{1,2})| uhr)?", n)
    if not m:
        return None
    h = int(m.group(1))
    mi = int(m.group(2) or m.group(3) or 0)
    if h > 23 or mi > 59:
        return None
    days = list(range(7))
    if re.search(r"werktags|wochentags|unter der woche", n):
        days = [0, 1, 2, 3, 4]
    elif re.search(r"wochenende", n):
        days = [5, 6]
    else:
        found = [d for w, d in DAYS.items() if w in n]
        if found:
            days = found
    return {"time": f"{h:02d}:{mi:02d}", "days": days}


def describe(a):
    parts = []
    for s in a.get("steps", []):
        if "wait" in s:
            parts.append(f"warte {int(s['wait'])} s")
        else:
            parts.append((f"(+{int(s['delay'])} s) " if s.get("delay") else "") + s["text"])
    extra = ""
    if a.get("schedule"):
        d = a["schedule"]["days"]
        dn = "täglich" if len(d) == 7 else "werktags" if d == [0, 1, 2, 3, 4] else ", ".join(k.title()[:2] for k, v in DAYS.items() if v in d)
        extra = f" – {dn} um {a['schedule']['time']}"
    if a.get("event"):
        extra += f" – bei Ereignis {a['event']['type']}" + (f" ({a['event'].get('value')})" if a["event"].get("value") else "")
    return f"{TYPE_NAMES[a['type']]} „{a['name']}“: " + " → ".join(parts) + extra


# ------------------------------------------------------------------ Aktionen
@action("automation_create",
        "Erstellt ein Profil (mehrere Programme starten), eine Routine (mehrere Aktionen, optional zeitgesteuert) "
        "oder einen eigenen Sprachbefehl. Schritte sind natürliche deutsche Befehle.",
        {"type": S("profile, routine oder command", ["profile", "routine", "command"]),
         "name": S("Name, z. B. 'FiveM' oder 'Gaming-Modus'"),
         "triggers": {"type": "array", "items": {"type": "string"}, "description": "Auslösesätze, z. B. ['FiveM', 'starte FiveM']"},
         "steps": {"type": "array", "items": {"type": "string"}, "description": "Schritte als deutsche Befehle, z. B. ['starte Discord', 'warte 5 Sekunden', 'starte FiveM mit Argument +connect 1.2.3.4']"},
         "delay_seconds": N("Pause zwischen den Schritten in Sekunden (0 = keine)"),
         "schedule_time": S("Uhrzeit HH:MM für zeitgesteuerte Routinen, sonst leer"),
         "schedule_days": {"type": "array", "items": {"type": "integer"}, "description": "Wochentage 0=Mo..6=So, leer = täglich"}},
        required=["type", "name", "triggers", "steps", "delay_seconds", "schedule_time", "schedule_days"], risk=1, module=M)
def automation_create(type, name, triggers, steps, delay_seconds=0, schedule_time="", schedule_days=None):
    existing = find(name, threshold=0.95)
    a = existing or {"type": type, "name": name}
    a["type"] = type
    a["triggers"] = list(dict.fromkeys([name] + [t for t in (triggers or []) if t]))
    a["steps"] = compile_steps(steps, float(delay_seconds or 0))
    if schedule_time:
        a["schedule"] = {"time": schedule_time, "days": schedule_days or list(range(7))}
    save(a)
    verb = "aktualisiert" if existing else "erstellt"
    return {"text": f"{TYPE_NAMES[type]} „{name}“ {verb}. {describe(a)}", "data": a}


@action("automation_delete", "Löscht ein Profil, eine Routine oder einen eigenen Befehl", {"name": S("Name")}, risk=1, module=M,
        confirm_text="Soll ich „{name}“ wirklich löschen?")
def automation_delete(name):
    a = find(name)
    if not a:
        return {"ok": False, "text": f"Ich finde nichts namens „{name}“."}
    delete(a["id"])
    return f"{TYPE_NAMES[a['type']]} „{a['name']}“ gelöscht."


@action("automation_list", "Listet Profile, Routinen und eigene Befehle", {"type": S("profile, routine, command oder all", ["profile", "routine", "command", "all"])}, risk=0, module=M)
def automation_list(type="all"):
    items = load_all(None if type in (None, "", "all") else type)
    if not items:
        return "Es sind noch keine " + {"profile": "Profile", "routine": "Routinen", "command": "eigenen Befehle"}.get(type, "Automationen") + " angelegt."
    return {"text": " | ".join(describe(a) for a in items[:15]), "data": items}


@action("automation_run", "Führt ein Profil, eine Routine oder einen eigenen Befehl aus", {"name": S("Name")}, risk=1, module=M)
def automation_run(name, ctx=None):
    a = find(name)
    if not a:
        return {"ok": False, "text": f"Ich finde kein Profil und keine Routine „{name}“."}
    return _mod.run(a)


@action("automation_add_step", "Fügt einem Profil/einer Routine einen Schritt hinzu",
        {"name": S("Name der Automation"), "step": S("Neuer Schritt als Befehl"), "position": N("Position ab 1, 0 = am Ende")},
        required=["name", "step", "position"], risk=1, module=M)
def automation_add_step(name, step, position=0):
    a = find(name)
    if not a:
        return {"ok": False, "text": f"„{name}“ gibt es nicht."}
    new = compile_steps([step])
    pos = int(position or 0)
    if pos > 0:
        a["steps"][pos - 1:pos - 1] = new
    else:
        a["steps"].extend(new)
    save(a)
    return f"Zu „{a['name']}“ hinzugefügt: {step}."


@action("automation_remove_step", "Entfernt einen Schritt aus einem Profil/einer Routine",
        {"name": S("Name der Automation"), "step": S("Schritt oder Programmname, der entfernt werden soll")}, risk=1, module=M)
def automation_remove_step(name, step):
    a = find(name)
    if not a:
        return {"ok": False, "text": f"„{name}“ gibt es nicht."}
    scored = sorted(((similarity(step, s.get("text", "")), i) for i, s in enumerate(a["steps"])), reverse=True)
    if not scored or scored[0][0] < 0.4:
        return {"ok": False, "text": f"Den Schritt „{step}“ finde ich in „{a['name']}“ nicht."}
    removed = a["steps"].pop(scored[0][1])
    save(a)
    return f"Aus „{a['name']}“ entfernt: {removed.get('text')}."


@action("automation_enable", "Aktiviert oder deaktiviert eine Routine", {"name": S("Name"), "enabled": B("aktiv?")}, risk=1, module=M)
def automation_enable(name, enabled=True):
    a = find(name)
    if not a:
        return {"ok": False, "text": f"„{name}“ gibt es nicht."}
    a["enabled"] = bool(enabled)
    save(a)
    return f"„{a['name']}“ ist jetzt {'aktiv' if enabled else 'deaktiviert'}."


@action("automation_set_event", "Lässt eine Routine bei einem Ereignis automatisch laufen",
        {"name": S("Name der Routine"),
         "event": S("startup (JARVIS-Start), app_started (Programm gestartet), battery_low, device_connected", ["startup", "app_started", "battery_low", "device_connected", "none"]),
         "value": S("z. B. Programmname bei app_started, sonst leer")}, risk=1, module=M)
def automation_set_event(name, event, value=""):
    a = find(name)
    if not a:
        return {"ok": False, "text": f"„{name}“ gibt es nicht."}
    a["event"] = None if event == "none" else {"type": event, "value": value}
    save(a)
    return describe(a)


# -------------------------------------------------------------------- Modul
class AutomationModule(Module):
    name = "automation"
    title = "Profile & Routinen"

    def __init__(self, jarvis):
        super().__init__(jarvis)
        global _mod
        _mod = self
        self._ran = {}
        self._running = 0

    def start(self):
        if not db.kv_get("automation_defaults"):
            self._defaults()
            db.kv_set("automation_defaults", True)
        threading.Thread(target=self._scheduler, daemon=True, name="scheduler").start()
        bus.on("app_started", lambda e, d: self._event("app_started", d.get("name", "")))
        bus.on("battery_low", lambda e, d: self._event("battery_low", ""))
        bus.on("device_connected", lambda e, d: self._event("device_connected", d.get("name", "")))
        threading.Timer(8, lambda: self._event("startup", "")).start()

    def _defaults(self):
        defaults = [
            ("routine", "Gaming-Modus", ["Gaming-Modus", "Gaming Modus starten", "Zeit zum Zocken"],
             ["Lautstärke auf 60 Prozent", "starte Steam", "starte Discord"]),
            ("routine", "Ich gehe schlafen", ["Ich gehe schlafen", "Gute Nacht", "Schlafmodus"],
             ["Ton aus", "alle Fenster minimieren", "sperre den PC"]),
            ("routine", "Film-Modus", ["Film-Modus", "Filmabend", "Kino-Modus"],
             ["Lautstärke auf 70 Prozent", "alle Fenster minimieren"]),
        ]
        for t, name, trig, steps in defaults:
            save({"type": t, "name": name, "triggers": trig, "steps": [{"text": s, "calls": []} for s in steps]})

    # ------------------------------------------------------------- Ausführung
    def compile_text(self, text):
        n = normalize(text)
        intent = None
        for mod in self.jarvis.module_order():
            if mod is self:
                continue
            try:
                i = mod.parse(text, n, self.jarvis.brain.ctx)
            except Exception:
                i = None
            if i and i.calls and (intent is None or i.confidence > intent.confidence):
                intent = i
        return [[c, a] for c, a in intent.calls] if intent else None

    def run(self, a, announce=True, source="manual"):
        brain = self.jarvis.brain
        if brain.halted:
            return {"ok": False, "text": "Automationen sind wegen Not-Aus pausiert. Sag „fortsetzen“ zum Freigeben."}
        if not any(s.get("delay") or s.get("wait") for s in a["steps"]) or source == "sync":
            return self._run_steps(a)
        threading.Thread(target=self._run_steps, args=(a, announce), daemon=True).start()
        return f"{TYPE_NAMES[a['type']]} „{a['name']}“ wird ausgeführt."

    def _run_steps(self, a, announce_end=False):
        brain = self.jarvis.brain
        ctx = brain.ctx
        self._running += 1
        texts, failed = [], 0
        log.activity(M, f"{TYPE_NAMES[a['type']]} „{a['name']}“ gestartet")
        # Live-Fortschritt für die Oberfläche
        tid = uuid.uuid4().hex[:8]
        labels = [f"{int(s['wait'])} s warten" if "wait" in s else s["text"] for s in a["steps"]]
        bus.emit("task", id=tid, name=a["name"], steps=labels, status="start")
        ok_end = False
        try:
            for i, s in enumerate(a["steps"]):
                bus.emit("task", id=tid, i=i, status="running")
                wait = float(s.get("wait") or s.get("delay") or 0)
                if wait and ctx.cancel.wait(wait):
                    return {"ok": False, "text": "Abgebrochen."}
                if ctx.cancel.is_set() or brain.halted:
                    return {"ok": False, "text": "Abgebrochen."}
                if "wait" in s:
                    bus.emit("task", id=tid, i=i, status="ok")
                    continue
                before = failed
                calls = s.get("calls") or self.compile_text(s["text"])
                if calls:
                    for name, args in calls:
                        act = registry.get(name)
                        if act and act.risk >= 3:
                            texts.append(f"Übersprungen (braucht Bestätigung): {s['text']}")
                            continue
                        try:
                            res = registry.call(name, args, ctx)
                        except Exception as e:
                            res = Result(False, log.error(f"Schritt {s['text']}", e))
                        if not res.ok:
                            failed += 1
                            texts.append(res.text)
                else:
                    # nicht lokal verständlich -> KI (wenn verfügbar)
                    ai = self.jarvis.modules.get("ai")
                    if ai and ai.available():
                        ai.chat(s["text"], ctx, silent=True)
                    else:
                        failed += 1
                        texts.append(f"Nicht verstanden: {s['text']}")
                bus.emit("task", id=tid, i=i, status="fail" if failed > before else "ok")
            ok_end = True
        finally:
            self._running -= 1
            bus.emit("task", id=tid, status="done", ok=ok_end and failed == 0)
        msg = f"{a['name']} ausgeführt." + (f" Probleme: {' '.join(texts)}" if failed else "")
        log.activity(M, msg, ok=failed == 0)
        if announce_end:
            brain.reply(msg, priority=1 if failed else 0)
        return {"ok": failed == 0, "text": msg}

    # --------------------------------------------------------------- Zeitplan
    def _scheduler(self):
        while True:
            try:
                now = datetime.datetime.now()
                hm = now.strftime("%H:%M")
                for a in load_all():
                    sch = a.get("schedule")
                    if not a.get("enabled", True) or not sch:
                        continue
                    if sch["time"] == hm and now.weekday() in sch.get("days", range(7)):
                        key = (a["id"], now.strftime("%Y-%m-%d %H:%M"))
                        if key not in self._ran and not self.jarvis.brain.halted:
                            self._ran[key] = True
                            bus.emit("notify", title="Routine", text=f"Zeitgesteuert: {a['name']}", speak=False)
                            threading.Thread(target=self._run_steps, args=(a, True), daemon=True).start()
            except Exception as e:
                log.error("Zeitplan", e)
            time.sleep(15)

    def _event(self, etype, value):
        if self.jarvis.brain.halted:
            return
        for a in load_all():
            ev = a.get("event")
            if not a.get("enabled", True) or not ev or ev.get("type") != etype:
                continue
            if ev.get("value") and value and similarity(ev["value"], value) < 0.7:
                continue
            log.activity(M, f"Ereignis {etype} löst „{a['name']}“ aus")
            threading.Thread(target=self._run_steps, args=(a, True), daemon=True).start()

    # ---------------------------------------------------------------- Parser
    def parse(self, text, n, ctx):
        # 1) Eigener Befehl / Profil / Routine direkt über Auslösesatz
        stripped = re.sub(r"^(starte|start|aktiviere|führe|mach|öffne|los|lade) (das |den |die |mein |meine |meinen )?(profil |routine |modus |befehl )?", "", n)
        stripped = re.sub(r" (starten|aktivieren|ausführen|an|los|laden)$", "", stripped)
        best, score = None, 0
        for a in load_all():
            if not a.get("enabled", True):
                continue
            for trig in a.get("triggers", []) + [a["name"]]:
                tn = normalize(trig)
                s = 1.0 if n == tn or stripped == tn else max(similarity(n, tn), similarity(stripped, tn))
                if s > score:
                    best, score = a, s
        if best and score >= 0.88:
            return Intent([("automation_run", {"name": best["name"]})], confidence=1.0)

        # 2) Verwaltung
        m = re.match(r"^(zeig|zeige|liste|welche) (mir )?(alle |meine )?(profile|routinen|befehle|automationen)( gibt es| habe ich| hast du)?$", n)
        if m:
            t = {"profile": "profile", "routinen": "routine", "befehle": "command"}.get(m.group(4), "all")
            return Intent([("automation_list", {"type": t})])
        m = re.match(r"^(lösch|lösche|entferne) (das |die |den |mein |meine |meinen )?(profil|routine|befehl|automation) (.+)$", n)
        if m:
            return Intent([("automation_delete", {"name": m.group(4)})])
        m = re.match(r"^(deaktivier|deaktiviere|pausier|pausiere) (die |das )?(routine|profil|befehl) (.+)$", n)
        if m:
            return Intent([("automation_enable", {"name": m.group(4), "enabled": False})])
        m = re.match(r"^(aktivier|aktiviere) (die |das )?(routine|profil|befehl) (.+) (wieder)?$", n)
        if m:
            return Intent([("automation_enable", {"name": m.group(4), "enabled": True})])
        m = re.match(r"^(füg|füge) (zu|zum|zur) (profil |routine )?(.+?) (noch )?(.+) hinzu$", n)
        if m:
            return Intent([("automation_add_step", {"name": m.group(4), "step": _orig(text, m.group(6)), "position": 0})])
        m = re.match(r"^(entfern|entferne|nimm) (.+?) aus (dem |der )?(profil |routine )?(.+?)( raus)?$", n)
        if m and find(m.group(5)):
            return Intent([("automation_remove_step", {"name": m.group(5), "step": m.group(2)})])

        # 3) Erstellen: „Wenn ich FiveM sage, starte FiveM, Discord und TeamSpeak“
        m = re.match(r"^wenn ich (?:sage )?[\"„]?(.+?)[\"“]? (?:sage|sag|schreibe|eingebe),? (?:dann )?(.+)$", n)
        if m:
            return self._create_intent(text, "command", m.group(1), m.group(2))
        m = re.match(r"^(?:erstelle|erstell|mach|lege|leg|neues|neue|neuer)(?: mir)? (?:ein |eine |einen |ein neues |eine neue |einen neuen )?(profil|routine|befehl)(?: an)?(?: namens| mit dem namen| für)? [\"„]?(.+?)[\"“]?(?::| mit| das| die| der| welche[rs]?)\s(.+)$", n)
        if m:
            kind = {"profil": "profile", "routine": "routine", "befehl": "command"}[m.group(1)]
            return self._create_intent(text, kind, m.group(2), m.group(3))
        m = re.match(r"^(?:jeden tag|täglich|werktags|wochentags|am wochenende|jeden \w+tag|montags|dienstags|mittwochs|donnerstags|freitags|samstags|sonntags) um ([\d:. ]+(?:uhr)?(?: \d+)?),? (.+)$", n)
        if m:
            return self._create_intent(text, "routine", "Zeitplan " + m.group(1).strip(), m.group(2), schedule_text=n)
        return None

    def _create_intent(self, text, kind, name, body, schedule_text=None):
        delay = 0
        dm = re.search(r",? mit (je(weils)? )?(\d+|\w+) sekunden? (pause|abstand|verzögerung|dazwischen)( dazwischen)?", body)
        if dm:
            delay = find_number(dm.group(3)) or 0
            body = body[:dm.start()] + body[dm.end():]
        sched = parse_schedule(schedule_text or body)
        if sched:
            body = re.sub(r",? ?(jeden tag|täglich|werktags|wochentags|am wochenende)? ?um [\d:. ]+(uhr)?( \d+)?,?", " ", body).strip()
            kind = "routine" if kind != "command" else kind
        parts = split_list(_orig(text, body))
        name_o = _orig(text, name).strip(" \"„“")
        triggers = [name_o]
        if kind == "profile":
            triggers.append(f"{name_o} starten")
        return Intent([("automation_create", {
            "type": kind, "name": name_o[:1].upper() + name_o[1:], "triggers": triggers, "steps": parts,
            "delay_seconds": delay, "schedule_time": sched["time"] if sched else "",
            "schedule_days": sched["days"] if sched else []})], confidence=1.0)

    def status(self):
        items = load_all()
        return {"profiles": sum(a["type"] == "profile" for a in items), "routines": sum(a["type"] == "routine" for a in items),
                "commands": sum(a["type"] == "command" for a in items), "running": self._running}

    def diagnose(self):
        out = []
        for a in load_all():
            bad = [s["text"] for s in a["steps"] if "wait" not in s and not (s.get("calls") or self.compile_text(s["text"]))]
            out.append((f"{TYPE_NAMES[a['type']]} {a['name']}", not bad, "ok" if not bad else "Unklare Schritte (nutzt KI): " + ", ".join(bad)))
        if not out:
            out.append(("Automationen", True, "keine angelegt"))
        return out


def _orig(text, frag):
    i = text.lower().find(frag.lower())
    return text[i:i + len(frag)] if i >= 0 else frag
