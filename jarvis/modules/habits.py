"""Gewohnheiten, Bildschirmzeit, Spielzeit, Vormachen und Tageszusammenfassung – alles lokal.

Erfasst (nur auf diesem PC, in der JARVIS-Datenbank):
    usage     wie lange welches Programm im Vordergrund war (pro Tag), Spiele markiert
    launches  wann welches bekannte Programm gestartet wurde
Daraus:
    Vorschläge   „Du startest um diese Zeit meistens Discord und FiveM – soll ich?“ (≥3 Tage in 14 Tagen, ±20 min)
    Vormachen    „Schau mir zu“ → Programme starten → „Speichere das als Gaming-Setup“ = eigener Befehl
    Statistik    Spielzeit, Bildschirmzeit, „Wie war mein Tag?“
"""
import datetime
import re
import threading
import time
from pathlib import Path

import psutil

from ..core import log
from ..core.actions import action, S
from ..core.config import config
from ..core.db import db
from ..core.events import bus
from ..core.module import Module, Intent
from ..core.text import ascii_fold

M = "gewohnheiten"
_mod = None
TICK = 5                    # Sekunden zwischen zwei Messungen des Vordergrundfensters
IGNORE = {"explorer", "searchhost", "shellexperiencehost", "startmenuexperiencehost", "lockapp", "textinputhost",
          "applicationframehost", "jarvis", "pythonw", "python", "msedgewebview2", "systemsettings", "taskmgr",
          "claude", "rundll32", "dllhost", "conhost", "cmd", "powershell", "pwsh", "windowsterminal", "openconsole",
          "onedrive", "lghub", "lghub_agent", "lghub_updater", "ollama", "ollama app", "winget", "git", "gh", "code"}

db.schema("""CREATE TABLE IF NOT EXISTS usage(
    day TEXT, app TEXT, seconds REAL, game INTEGER DEFAULT 0, PRIMARY KEY(day, app))""")
db.schema("""CREATE TABLE IF NOT EXISTS launches(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, app TEXT, weekday INTEGER, minute INTEGER)""")


def fmt_duration(sec):
    sec = int(sec or 0)
    h, m = sec // 3600, (sec % 3600) // 60
    if h and m:
        return f"{h} Std. {m} Min."
    if h:
        return f"{h} Std."
    return f"{max(1, m)} Min." if sec >= 30 else "unter einer Minute"


def _spoken_duration(sec):
    sec = int(sec or 0)
    h, m = sec // 3600, (sec % 3600) // 60
    parts = []
    if h:
        parts.append(f"{h} Stunde" + ("n" if h != 1 else ""))
    if m or not h:
        parts.append(f"{max(1, m)} Minute" + ("n" if m != 1 else ""))
    return " und ".join(parts)


def _today():
    return datetime.date.today().isoformat()


class HabitsModule(Module):
    name = "habits"
    title = "Gewohnheiten"

    def __init__(self, jarvis):
        super().__init__(jarvis)
        global _mod
        _mod = self
        self._buf = {}                    # app -> [Sekunden, Spiel?] (wird minütlich gespeichert)
        self._pids = {}                   # pid -> App-Name der bekannten laufenden Programme
        self._recent = {}                 # App -> letzter Start (gegen doppelte Zählung)
        self._exe_map, self._dir_map, self._map_ts = {}, [], 0
        self.recording = None             # {"start": ts, "apps": [(sekunden_seit_start, App)]}
        self.snooze_until = 0
        self.suggested_today = {}         # App -> Datum

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="habits").start()

    def stop(self):
        try:
            self._flush()        # Messwerte der letzten Minute nicht verlieren (Beenden, Update)
        except Exception:
            pass

    # ------------------------------------------------------ Zuordnung Prozess → Programm
    def _refresh_map(self):
        from . import pc
        if time.time() - self._map_ts < 300 and self._exe_map:
            return
        exe, dirs = {}, []
        for a in list(pc.index.apps):
            if a.get("exe"):
                exe.setdefault(a["exe"].lower(), a)
            if a.get("dir") and len(a["dir"]) > 12:
                dirs.append((a["dir"].lower(), a))
        dirs.sort(key=lambda x: -len(x[0]))       # genauester Ordner zuerst
        self._exe_map, self._dir_map, self._map_ts = exe, dirs, time.time()

    def app_for(self, pname, pexe):
        """Bekanntes Programm (Name, Quelle) zu einem Prozess – oder None für System-/Hilfsprozesse."""
        pn, pe = (pname or "").lower(), (pexe or "").lower()
        a = self._exe_map.get(pn)
        if not a and pe:
            a = next((app for d, app in self._dir_map if pe.startswith(d)), None)
        if a:
            return a["name"], a.get("source", "")
        return None

    def _is_game(self, app, source, stem):
        from . import focus
        return source == "steam" or stem in focus.GAMES or any(g in stem for g in ("fivem", "gta5")) or focus.current() == "gaming"

    # ------------------------------------------------------------------ Schleife
    def _loop(self):
        self._refresh_map()
        self._pids = self._snapshot()           # Grundstand: schon laufende Programme zählen nicht als „gestartet“
        n = 0
        while True:
            time.sleep(TICK)
            n += 1
            try:
                if config.get("habits.tracking", True):
                    self._sample_foreground()
                    if n % 2 == 0:
                        self._check_launches()
                    if n % 12 == 0:
                        self._flush()
                if n % 12 == 6:
                    self._maybe_suggest()
                if self.recording and time.time() - self.recording["start"] > 900:
                    self.recording = None
                    bus.emit("notify", title="Vormachen", text="Ich habe nach 15 Minuten aufgehört zuzuschauen.", speak=False, priority=0)
            except Exception as e:
                log.logger().warning("Gewohnheiten: %s", e)

    def _snapshot(self):
        self._refresh_map()
        out = {}
        for p in psutil.process_iter(["pid", "name", "exe"]):
            try:
                if ascii_fold((p.info["name"] or "").rsplit(".", 1)[0]) in IGNORE:
                    continue
                hit = self.app_for(p.info["name"], p.info["exe"])
                if hit:
                    out[p.info["pid"]] = hit[0]
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return out

    def _sample_foreground(self):
        from . import pc
        from .pc import windows
        hwnd = pc.tracker.current if pc.tracker else None
        info = windows.info(hwnd) if hwnd else None
        if not info or windows.is_own(hwnd):
            return
        pname = info.get("process") or ""
        stem = ascii_fold(pname.rsplit(".", 1)[0])
        if not stem or stem in IGNORE:
            return
        try:
            pexe = psutil.Process(info["pid"]).exe()
        except Exception:
            pexe = ""
        hit = self.app_for(pname, pexe)
        app, source = hit if hit else (Path(pname).stem, "")
        b = self._buf.setdefault(app, [0.0, False])
        b[0] += TICK
        b[1] = b[1] or self._is_game(app, source, stem)

    def _flush(self):
        day, buf, self._buf = _today(), self._buf, {}
        for app, (sec, game) in buf.items():
            db.execute("""INSERT INTO usage(day, app, seconds, game) VALUES(?,?,?,?)
                          ON CONFLICT(day, app) DO UPDATE SET seconds = seconds + excluded.seconds, game = MAX(game, excluded.game)""",
                       (day, app, sec, int(game)))

    def _check_launches(self):
        now_pids = self._snapshot()
        started = {app for pid, app in now_pids.items() if pid not in self._pids}
        self._pids = now_pids
        now = datetime.datetime.now()
        for app in started:
            if time.time() - self._recent.get(app, 0) < 600:
                continue
            self._recent[app] = time.time()
            db.execute("INSERT INTO launches(ts, app, weekday, minute) VALUES(?,?,?,?)",
                       (time.time(), app, now.weekday(), now.hour * 60 + now.minute))
            if self.recording is not None:
                self.recording["apps"].append((time.time() - self.recording["start"], app))
                log.activity(M, f"Vormachen: {app} gestartet")

    def running_apps(self):
        return set(self._pids.values())

    # ------------------------------------------------------------------ Vorschläge
    def habits_now(self, window=20, min_days=3, days_back=14):
        """Programme, die der Nutzer um diese Uhrzeit an mindestens min_days Tagen gestartet hat."""
        now = datetime.datetime.now()
        minute = now.hour * 60 + now.minute
        rows = db.query("SELECT ts, app, minute FROM launches WHERE ts > ?", (time.time() - days_back * 86400,))
        days = {}
        for r in rows:
            diff = abs(r["minute"] - minute)
            if min(diff, 1440 - diff) <= window:
                days.setdefault(r["app"], set()).add(datetime.date.fromtimestamp(r["ts"]))
        return sorted((app for app, d in days.items() if len(d) >= min_days), key=lambda a: -len(days[a]))

    def _maybe_suggest(self):
        from . import focus
        brain = self.jarvis.brain
        if (not config.get("habits.suggestions", True) or time.time() < self.snooze_until or focus.current() != "normal"
                or brain.halted or brain.busy or brain.ctx.pending):
            return
        today = _today()
        running = self.running_apps()
        apps = [a for a in self.habits_now() if a not in running and self.suggested_today.get(a) != today][:3]
        if not apps:
            return
        for a in apps:
            self.suggested_today[a] = today
        names = " und ".join([", ".join(apps[:-1]), apps[-1]] if len(apps) > 1 else apps)
        text = f"Du startest um diese Zeit meistens {names}. Soll ich {'sie' if len(apps) > 1 else 'es'} starten?"
        brain.ctx.pending = {"type": "confirm", "calls": [("app_open", {"name": a}) for a in apps], "time": time.time()}
        log.activity(M, "Vorschlag: " + names)
        # Nach dem Vorlesen kurz zuhören, damit „Ja“ oder „Nicht jetzt“ ohne „Hey Jarvis“ reicht
        v = self.jarvis.modules.get("voice")
        if v and config.get("voice.conversation", True):
            v.conversation, v.awaiting_follow_up = True, True
        bus.emit("confirm", text=text)
        bus.emit("notify", title="Vorschlag", text=text, speak=True, priority=1)

    # ------------------------------------------------------------------ Statistik
    def usage(self, since_day, games_only=False):
        self._flush()
        q = "SELECT app, SUM(seconds) AS s, MAX(game) AS g FROM usage WHERE day >= ?" + (" AND game = 1" if games_only else "")
        return db.query(q + " GROUP BY app ORDER BY s DESC", (since_day,))

    def dashboard(self):
        today = _today()
        week = (datetime.date.today() - datetime.timedelta(days=6)).isoformat()
        apps_today = self.usage(today)
        games_today = [r for r in apps_today if r["g"]]
        games_week = self.usage(week, games_only=True)
        return {
            "screen_today": sum(r["s"] for r in apps_today),
            "top_today": [{"app": r["app"], "sec": r["s"], "game": bool(r["g"])} for r in apps_today[:6]],
            "games_today": sum(r["s"] for r in games_today),
            "games_week": sum(r["s"] for r in games_week),
            "top_games_week": [{"app": r["app"], "sec": r["s"]} for r in games_week[:5]],
            "recording": bool(self.recording),
        }

    # ------------------------------------------------------------------ Sprache
    def parse(self, text, n, ctx):
        if re.search(r"^(nicht jetzt|jetzt nicht|gerade nicht|später vielleicht|vielleicht später)\b", n):
            return Intent([("habits_snooze", {})])
        if re.search(r"(schau|guck) mir (mal )?zu|merk dir,? was ich (jetzt )?(mache|starte|öffne)|starte die aufnahme|nimm (meinen|den) ablauf auf|lern(e)? (von mir|meinen ablauf)", n):
            return Intent([("demo_start", {})])
        m = re.search(r"(speicher[en]?|merk dir|nenn[en]?|leg)( das| es| den ablauf| diesen ablauf| das ganze)?( jetzt)? (als|unter( dem namen)?) (?P<name>.+?)( ab| an)?$", n)
        if m and (self.recording or re.search(r"ablauf|das ganze", n)):
            return Intent([("demo_save", {"name": _orig(text, m.group("name"))})])
        if self.recording and re.search(r"^(fertig|stopp|stop|aufnahme (beenden|stoppen)|hör auf zuzuschauen|das wars)$", n):
            return Intent([("demo_save", {"name": ""})])
        if re.search(r"(wie lange|wieviel|wie viel)( zeit)? (habe|hab) ich (heute |diese woche |gestern )?(gespielt|gezockt|am pc|am computer)", n) \
                or re.search(r"spielzeit|gaming[- ]?statistik|bildschirmzeit", n):
            return Intent([("playtime", {"week": bool(re.search(r"woche", n))})])
        if re.search(r"wie war mein tag|tageszusammenfassung|tagesbericht|was habe ich heute (so )?gemacht|fasse? (meinen|den) tag zusammen", n):
            return Intent([("day_summary", {})])
        return None

    def status(self):
        return {"recording": bool(self.recording), "tracking": config.get("habits.tracking", True),
                "suggestions": config.get("habits.suggestions", True)}


def _orig(text, frag):
    """Namen in Originalschreibweise aus dem Satz holen (normalize() macht alles klein)."""
    i = text.lower().find(frag.lower())
    return (text[i:i + len(frag)] if i >= 0 else frag).strip(" .!?\"'„“")


# ------------------------------------------------------------------ Aktionen
@action("habits_snooze", "Vorschläge für eine Stunde pausieren („Nicht jetzt“)", {}, risk=0, module=M, ai=False)
def habits_snooze(ctx=None):
    if _mod:
        _mod.snooze_until = time.time() + 3600
    if ctx is not None:
        ctx.pending = None
    return "Alles klar, ich frage später nochmal."


@action("demo_start", "Lernen durch Vormachen: JARVIS schaut zu, welche Programme der Nutzer nacheinander startet", {}, risk=0, module=M)
def demo_start():
    if not _mod:
        return {"ok": False, "text": "Das geht gerade nicht."}
    _mod.recording = {"start": time.time(), "apps": []}
    return ("Ich schaue dir zu. Starte jetzt die Programme in der gewünschten Reihenfolge. "
            "Wenn du fertig bist, sag zum Beispiel: Speichere das als Gaming-Setup.")


@action("demo_save", "Speichert den vorgemachten Ablauf als eigenen Sprachbefehl", {"name": S("Name des Befehls")}, risk=1, module=M)
def demo_save(name=""):
    from . import automation
    if not _mod:
        return {"ok": False, "text": "Das geht gerade nicht."}
    rec = _mod.recording
    if rec is None:
        # „Merk dir diesen Ablauf als X“ im Nachhinein: Programmstarts der letzten 10 Minuten
        rows = db.query("SELECT ts, app FROM launches WHERE ts > ? ORDER BY ts", (time.time() - 600,))
        rec = {"start": rows[0]["ts"] if rows else time.time(), "apps": [(r["ts"] - rows[0]["ts"], r["app"]) for r in rows]}
    if not rec["apps"]:
        return {"ok": False, "text": "Ich habe noch kein Programm gesehen, das du gestartet hast. Starte die Programme und sag dann nochmal Bescheid."}
    if not name:
        return {"ok": False, "text": "Wie soll der Befehl heißen? Sag zum Beispiel: Speichere das als Gaming-Setup."}
    steps, last = [], 0.0
    for t, app in rec["apps"]:
        gap = t - last
        if steps and gap >= 3:
            steps.append(f"warte {min(30, int(round(gap)))} Sekunden")
        steps.append(f"starte {app}")
        last = t
    _mod.recording = None
    res = automation.automation_create("command", name, [name, f"starte {name}"], steps)
    apps = ", ".join(a for _, a in rec["apps"])
    return {"ok": True, "text": f"Gespeichert! Sag einfach „{name}“, dann starte ich {apps}.", "data": res.get("data")}


@action("playtime", "Sagt, wie lange der Nutzer heute bzw. diese Woche gespielt hat und am PC war",
        {"week": {"type": "boolean", "description": "true = diese Woche statt heute"}}, required=[], risk=0, module=M)
def playtime(week=False):
    if not _mod:
        return "Dazu habe ich noch keine Daten."
    d = _mod.dashboard()
    if week:
        top = d["top_games_week"]
        if not d["games_week"]:
            return "Diese Woche habe ich noch keine Spielzeit erfasst."
        best = ", ".join(f"{g['app']} {_spoken_duration(g['sec'])}" for g in top[:3])
        return f"Diese Woche hast du {_spoken_duration(d['games_week'])} gespielt. Am meisten: {best}."
    parts = [f"Heute warst du {_spoken_duration(d['screen_today'])} am PC"]
    parts.append(f"davon {_spoken_duration(d['games_today'])} am Zocken" if d["games_today"] else "gespielt hast du noch nicht")
    return ", ".join(parts) + "."


@action("day_summary", "Tageszusammenfassung: Bildschirmzeit, meistgenutzte Programme, Spielzeit, Befehle, KI-Kosten", {}, risk=0, module=M)
def day_summary():
    from . import costs, focus
    if not _mod:
        return "Dazu habe ich noch keine Daten."
    d = _mod.dashboard()
    boot = datetime.datetime.fromtimestamp(psutil.boot_time())
    parts = [f"Dein PC läuft seit {boot.strftime('%H:%M')} Uhr."]
    if d["screen_today"]:
        parts.append(f"Aktiv warst du {_spoken_duration(d['screen_today'])}.")
        top = [t for t in d["top_today"] if t["sec"] >= 120][:3]
        if top:
            parts.append("Am meisten genutzt: " + ", ".join(f"{t['app']} mit {_spoken_duration(t['sec'])}" for t in top) + ".")
    if d["games_today"]:
        parts.append(f"Gezockt hast du {_spoken_duration(d['games_today'])}.")
    start = datetime.datetime.combine(datetime.date.today(), datetime.time()).timestamp()
    cmds = db.one("SELECT COUNT(*) AS n FROM activity WHERE ts >= ? AND kind NOT IN ('system', 'fehler', 'wartung')", (start,))["n"]
    if cmds:
        parts.append("Ich habe heute eine Aufgabe für dich erledigt." if cmds == 1 else f"Ich habe heute {cmds} Aufgaben für dich erledigt.")
    c = costs.summary()
    if c["today"] >= 0.005:
        parts.append(f"Die KI hat heute etwa {round(c['today'] * 100)} Cent gekostet.")
    missed = focus.missed_count()
    if missed:
        parts.append("Eine Meldung wartet noch auf dich." if missed == 1 else f"{missed} Meldungen warten noch auf dich.")
    return " ".join(parts)
