"""Modi (Nicht stören) und Prioritätssystem für Meldungen.

Modi:      normal | gaming | film | schlafen | arbeit
Prioritäten jeder Meldung:
    0 = Info       (z. B. neues Laufwerk, Update verfügbar)
    1 = Wichtig    (z. B. Mikrofonproblem)
    2 = Dringend   (z. B. Timer abgelaufen, Akku fast leer)
    3 = Kritisch   (immer sprechen)
Je nach Modus wird eine Meldung vorgelesen, nur angezeigt oder für später gesammelt
(„Was habe ich verpasst?“). Direkte Antworten auf eigene Fragen spricht JARVIS immer.

Automatik: Ein Spiel im Vollbild schaltet in den Gaming-Modus, ein Video im Vollbild in den
Film-Modus – danach zurück. Ein von Hand gewählter Modus hat immer Vorrang.
"""
import ctypes
import datetime
import re
import threading
import time

from ..core import log
from ..core.actions import action, S, B
from ..core.config import config
from ..core.db import db
from ..core.events import bus
from ..core.module import Module, Intent

M = "modus"
_mod = None

MODES = {
    # name: (Anzeigename, ab Priorität sprechen, ab Priorität anzeigen)
    "normal": ("Normal", 0, 0),
    "gaming": ("Gaming", 2, 1),
    "film": ("Film", 3, 2),
    "schlafen": ("Schlafen", 3, 3),
    "arbeit": ("Arbeit", 2, 1),
}
PRIO_NAMES = ["Info", "Wichtig", "Dringend", "Kritisch"]

# Bekannte Spiele (Prozessname ohne .exe, klein) – zusätzlich zur Vollbild-Erkennung
GAMES = {"fivem", "gta5", "gta5_enhanced", "playgtav", "cs2", "csgo", "valorant-win64-shipping", "r5apex", "fortniteclient-win64-shipping",
         "rocketleague", "league of legends", "overwatch", "eldenring", "cod", "modernwarfare", "rainbowsix", "destiny2",
         "pubg", "tslgame", "minecraft", "robloxplayerbeta", "dota2", "witcher3", "cyberpunk2077", "rdr2", "forzahorizon5",
         "eurotrucks2", "ets2", "fc25", "fc26", "ea sports fc", "bf2042", "bf6", "helldivers2", "palworld-win64-shipping"}
VIDEO_APPS = {"vlc", "mpc-hc", "mpc-hc64", "mpc-be64", "potplayermini64", "potplayer", "wmplayer", "video.ui", "netflix",
              "disneyplus", "primevideo", "applicationframehost", "kodi", "plex", "jellyfin", "mpv"}
BROWSERS = {"chrome", "msedge", "firefox", "opera", "opera_gx", "brave", "vivaldi"}
NEVER = {"explorer", "searchhost", "shellexperiencehost", "startmenuexperiencehost", "lockapp", "textinputhost"}

db.schema("""CREATE TABLE IF NOT EXISTS missed(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, mode TEXT, priority INTEGER, title TEXT, text TEXT, seen INTEGER DEFAULT 0
)""")


def current():
    return _mod.mode if _mod else "normal"


def rules(mode=None):
    mode = mode or current()
    return MODES.get(mode, MODES["normal"])


def may_speak(priority):
    """Darf eine Meldung dieser Priorität im aktuellen Modus vorgelesen werden?"""
    return priority >= 3 or priority >= rules()[1]


def may_show(priority):
    return priority >= 3 or priority >= rules()[2]


def add_missed(priority, title, text):
    db.execute("INSERT INTO missed(ts, mode, priority, title, text) VALUES(?,?,?,?,?)",
               (time.time(), current(), int(priority), title or "", text or ""))
    bus.emit("missed_changed", count=missed_count())


def missed_count():
    return db.one("SELECT COUNT(*) AS n FROM missed WHERE seen=0")["n"]


def missed_list(limit=30):
    return db.query("SELECT * FROM missed WHERE seen=0 ORDER BY id DESC LIMIT ?", (limit,))


def mark_seen():
    db.execute("UPDATE missed SET seen=1 WHERE seen=0")
    bus.emit("missed_changed", count=0)


def missed_summary(mark=True):
    rows = missed_list(50)
    if not rows:
        return "Du hast nichts verpasst."
    rows.reverse()
    items = [r["text"].rstrip(".") for r in rows[:5]]
    more = f" und {len(rows) - 5} weitere" if len(rows) > 5 else ""
    text = (f"Du hast {len(rows)} Meldung{'en' if len(rows) != 1 else ''} verpasst: " + "; ".join(items) + more + ".")
    if mark:
        mark_seen()
    return text


# ------------------------------------------------------------------ Erkennung
def _foreground_situation():
    """'gaming' | 'film' | None anhand des Vordergrundfensters."""
    try:
        import win32api
        import win32gui
        from .pc import windows
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd or windows.is_own(hwnd):
            return None
        cls = win32gui.GetClassName(hwnd)
        if cls in ("Progman", "WorkerW", "Shell_TrayWnd"):
            return None
        info = windows.info(hwnd) or {}
        proc = (info.get("process") or "").lower().rsplit(".", 1)[0]
        if proc in NEVER:
            return None
        # Vollbild: Fenster deckt den ganzen Monitor ab (auch randloses Fenster)
        mon = win32api.MonitorFromWindow(hwnd, 2)
        ml, mt, mr, mb = win32api.GetMonitorInfo(mon)["Monitor"]
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        import win32con
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
        # Ein normal maximiertes Fenster mit Titelleiste ist kein Vollbild (z. B. bei ausgeblendeter Taskleiste)
        maximized_window = win32gui.IsZoomed(hwnd) and style & win32con.WS_CAPTION == win32con.WS_CAPTION
        fullscreen = l <= ml and t <= mt and r >= mr and b >= mb and not maximized_window
        # Windows meldet exklusives Direct3D-Vollbild zusätzlich selbst
        state = ctypes.c_int(0)
        try:
            ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state))
        except Exception:
            pass
        d3d = state.value == 3  # QUNS_RUNNING_D3D_FULL_SCREEN
        if proc in GAMES or any(g in proc for g in ("fivem", "gta5")):
            return "gaming"
        if fullscreen or d3d:
            if proc in VIDEO_APPS or proc in BROWSERS:
                return "film"
            return "gaming"
    except Exception:
        return None
    return None


class FocusModule(Module):
    name = "focus"
    title = "Modi"

    def __init__(self, jarvis):
        super().__init__(jarvis)
        global _mod
        _mod = self
        self.mode = config.get("focus.mode", "normal")
        if self.mode not in MODES:
            self.mode = "normal"
        self.manual = self.mode != "normal"   # beim Start gesetzter Modus zählt als manuell
        self.since = time.time()

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="focus").start()

    # ------------------------------------------------------------ Wechsel
    def set_mode(self, mode, manual=True, announce=True):
        mode = mode if mode in MODES else "normal"
        old = self.mode
        self.manual = manual and mode != "normal"
        if mode == old:
            return None
        self.mode, self.since = mode, time.time()
        config.set("focus.mode", mode if manual else "normal")
        log.activity(M, f"Modus: {MODES[old][0]} → {MODES[mode][0]}" + ("" if manual else " (automatisch)"))
        bus.emit("focus", mode=mode, label=MODES[mode][0], manual=self.manual)
        # Beim Verlassen eines ruhigen Modus: kurz zusammenfassen, was liegen geblieben ist
        if mode == "normal" and missed_count():
            n = missed_count()
            text = f"{MODES[old][0]}-Modus beendet. In der Zwischenzeit {'ist eine Meldung' if n == 1 else f'sind {n} Meldungen'} eingegangen – sag „Was habe ich verpasst?“."
            if not manual:
                bus.emit("notify", title="Modus", text=text, speak=True, priority=1)
                return None
            return text
        return None

    def _loop(self):
        seen, since = None, 0.0
        while True:
            time.sleep(3)
            try:
                if self.jarvis.brain.halted:
                    continue
                self._sleep_schedule()
                if not config.get("focus.auto", True) or self.manual:
                    continue
                sit = _foreground_situation()
                if sit != seen:
                    seen, since = sit, time.time()
                    continue
                stable = time.time() - since
                if sit and self.mode != sit and stable >= 8:
                    self.set_mode(sit, manual=False)
                elif not sit and self.mode in ("gaming", "film") and stable >= 30:
                    self.set_mode("normal", manual=False)
            except Exception as e:
                log.logger().warning("Modus-Erkennung: %s", e)

    def _sleep_schedule(self):
        sch = config.get("focus.sleep") or {}
        if not sch.get("enabled"):
            return
        now = datetime.datetime.now().strftime("%H:%M")
        start, end = sch.get("from", "23:00"), sch.get("to", "07:00")
        inside = (start <= now or now < end) if start > end else (start <= now < end)
        if inside and self.mode != "schlafen" and not (self.manual and self.mode != "normal"):
            self.set_mode("schlafen", manual=False)
        elif not inside and self.mode == "schlafen" and not self.manual:
            self.set_mode("normal", manual=False)

    # ------------------------------------------------------------ Sprache
    def parse(self, text, n, ctx):
        off = re.search(r"\b(aus|beenden|beende|deaktivier\w*|stopp|vorbei|ende)\b", n)
        if re.search(r"(was|welche meldungen) (habe|hab) ich verpasst|verpasste meldungen|was ist (in der zwischenzeit|inzwischen) passiert", n):
            return Intent([("focus_missed", {})])
        if re.search(r"(welcher|in welchem) modus", n):
            return Intent([("focus_status", {})])
        if re.search(r"nicht stören", n):
            return Intent([("focus_set", {"mode": "normal" if off else "arbeit"})])
        for key, pat in (("gaming", r"gaming[- ]?modus|spiele?[- ]?modus|zock(er)?[- ]?modus"),
                         ("film", r"film[- ]?modus|kino[- ]?modus|serien[- ]?modus"),
                         ("schlafen", r"schlaf[- ]?modus|nacht[- ]?modus|ruhe[- ]?modus"),
                         ("arbeit", r"arbeits?[- ]?modus|fokus[- ]?modus|konzentrations[- ]?modus")):
            if re.search(pat, n):
                return Intent([("focus_set", {"mode": "normal" if off else key})])
        if re.search(r"(normal(en|er)? modus|modus (beenden|aus|normal))", n):
            return Intent([("focus_set", {"mode": "normal"})])
        return None

    def status(self):
        return {"mode": self.mode, "label": MODES[self.mode][0], "manual": self.manual,
                "auto": config.get("focus.auto", True), "missed": missed_count()}


# ------------------------------------------------------------------ Aktionen
@action("focus_set", "Schaltet einen Modus: normal, gaming, film, schlafen (nur Kritisches), arbeit (nicht stören)",
        {"mode": S("Modus", list(MODES))}, risk=0, module=M)
def focus_set(mode="normal"):
    if not _mod:
        return {"ok": False, "text": "Modi sind nicht verfügbar."}
    was = _mod.mode
    extra = _mod.set_mode(mode, manual=True)
    label = MODES.get(mode, MODES["normal"])[0]
    if mode == "normal":
        base = "Normaler Modus." if was == "normal" else f"{MODES[was][0]}-Modus beendet."
        return base if not extra else extra
    hints = {"gaming": "Ich melde mich nur noch bei Dringendem.", "film": "Ich bleibe still, außer es ist kritisch.",
             "schlafen": "Gute Nacht. Ich melde mich nur bei Kritischem.", "arbeit": "Ich halte dir den Rücken frei und sammle Unwichtiges."}
    return f"{label}-Modus aktiv. {hints.get(mode, '')}"


@action("focus_status", "Sagt, welcher Modus aktiv ist und wie viele Meldungen warten", {}, risk=0, module=M)
def focus_status():
    if not _mod:
        return "Normaler Modus."
    n = missed_count()
    how = "von dir eingeschaltet" if _mod.manual else "automatisch erkannt" if _mod.mode != "normal" else ""
    return (f"Aktiv ist der {MODES[_mod.mode][0]}-Modus" + (f", {how}" if how else "") + "."
            + (f" {n} verpasste Meldung{'en' if n != 1 else ''} warten." if n else ""))


@action("focus_missed", "Liest verpasste Meldungen vor, die während eines Modus gesammelt wurden", {}, risk=0, module=M)
def focus_missed():
    return missed_summary()


@action("focus_auto", "Automatische Modus-Erkennung (Spiel/Film im Vollbild) ein- oder ausschalten", {"on": B("an?")}, risk=0, module=M)
def focus_auto(on=True):
    config.set("focus.auto", bool(on))
    return "Ich erkenne Spiele und Filme jetzt automatisch." if on else "Automatische Modus-Erkennung ist aus."
