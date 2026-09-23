"""PC-Steuerung: Programme, Fenster, Audio, Energie, Screenshots, Dateien, Web."""
import datetime
import os
import re
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

from ...core import log
from ...core.actions import action, S, N, B
from ...core.events import bus
from ...core.module import Module, Intent
from ...core.text import find_number, strip_articles, similarity, split_list
from . import apps as appsmod
from . import system as sysm
from . import windows as win

M = "pc"
index = appsmod.AppIndex()
tracker: win.ForegroundTracker | None = None

SITES = {
    "youtube": "https://www.youtube.com", "google": "https://www.google.de", "netflix": "https://www.netflix.com",
    "twitch": "https://www.twitch.tv", "amazon": "https://www.amazon.de", "reddit": "https://www.reddit.com",
    "github": "https://github.com", "gmail": "https://mail.google.com", "wikipedia": "https://de.wikipedia.org",
    "chatgpt": "https://chatgpt.com", "whatsapp web": "https://web.whatsapp.com", "instagram": "https://www.instagram.com",
    "twitter": "https://x.com", "x": "https://x.com", "spotify web": "https://open.spotify.com",
    "disney plus": "https://www.disneyplus.com", "prime video": "https://www.primevideo.com", "ebay": "https://www.ebay.de",
    "wetter": "https://www.google.de/search?q=wetter", "maps": "https://maps.google.de", "google maps": "https://maps.google.de",
    "fivem server": "https://servers.fivem.net",
}

THAT = re.compile(r"^(das|es|dies|dieses|dieses fenster|das fenster|das hier|das programm|das aktuelle fenster|das aktive fenster|aktuelle fenster|jetzige fenster)$")


def _target_window(name, ctx):
    """'das' -> letztes Fremdfenster, sonst Fenster/Programm per Name."""
    if not name or THAT.match(name.strip().lower()):
        hwnd = tracker.target() if tracker else None
        return win.info(hwnd) if hwnd else None
    return win.find_window(name)


# ================================================================ Programme
@action("app_open", "Startet ein Programm, Spiel oder eine App",
        {"name": S("Name des Programms, z. B. 'Discord', 'GTA V'"), "args": S("Optionale Startargumente")},
        required=["name"], risk=1, module=M)
def app_open(name, args="", exact=False, ctx=None):
    name = strip_articles(name.strip())
    app = index.get(name) if exact else None
    if not app:
        hits = index.find(name)
        if not hits:
            if name.lower() in SITES:
                return open_url(SITES[name.lower()])
            # Nachscannen – vielleicht frisch installiert
            index.scan()
            hits = index.find(name)
        if not hits:
            return {"ok": False, "text": f"Ich habe kein Programm namens „{name}“ gefunden."}
        top_s, top = hits[0]
        close = [a for s, a in hits[1:4] if top_s - s < 0.06 and a["name"] != top["name"]]
        if top_s < 0.9 and close:
            options = [top["name"]] + [a["name"] for a in close]
            return {"ok": True, "text": f"Meinst du {', '.join(options[:-1])} oder {options[-1]}?",
                    "data": {"choose": options, "param": "name"}}
        app = top
    try:
        appsmod.launch(app, args or "")
    except Exception as e:
        return {"ok": False, "text": f"{app['name']} konnte nicht gestartet werden: {log.error('Programmstart', e)}"}
    if ctx:
        ctx.last_app = app
    bus.emit("app_started", name=app["name"])
    return f"{app['name']} wird gestartet."


@action("app_close", "Schließt ein laufendes Programm oder Fenster",
        {"name": S("Programmname oder 'das' für das aktive Fenster")}, risk=1, module=M)
def app_close(name, ctx=None):
    w = _target_window(name, ctx)
    app = None
    if not THAT.match((name or "").strip().lower()):
        hits = index.find(name)
        app = hits[0][1] if hits and hits[0][0] >= 0.75 else None
    if app:
        procs = appsmod.processes_for(app)
        if procs:
            pids = {p.pid for p in procs}
            n = 0
            for wi in win.visible_windows():
                if wi["pid"] in pids:
                    win.close(wi["hwnd"])
                    n += 1
            if n == 0:
                for p in procs:
                    try:
                        p.terminate()
                    except Exception:
                        pass
            return f"{app['name']} wird geschlossen."
    if w:
        win.close(w["hwnd"])
        return f"{_short(w['title'])} wird geschlossen."
    return {"ok": False, "text": f"„{name}“ läuft gerade nicht."}


@action("app_kill", "Beendet ein Programm hart (ungespeicherte Daten gehen verloren)",
        {"name": S("Programmname")}, risk=2, module=M, confirm_text="Soll ich {name} wirklich hart beenden? Ungespeicherte Daten gehen verloren.")
def app_kill(name):
    hits = index.find(name)
    procs = appsmod.processes_for(hits[0][1]) if hits else []
    if not procs:
        import psutil
        procs = [p for p in psutil.process_iter(["name"]) if similarity(name, (p.info["name"] or "").rsplit(".", 1)[0]) > 0.85]
    for p in procs:
        try:
            p.kill()
        except Exception:
            pass
    return f"{len(procs)} Prozess(e) beendet." if procs else {"ok": False, "text": f"{name} läuft nicht."}


@action("app_search", "Sucht installierte Programme", {"query": S("Suchbegriff")}, risk=0, module=M)
def app_search(query):
    hits = index.find(query, limit=8)
    if not hits:
        return {"ok": False, "text": f"Kein Programm passend zu „{query}“ gefunden."}
    return {"text": "Gefunden: " + ", ".join(a["name"] for _, a in hits) + ".", "data": [a for _, a in hits]}


@action("apps_rescan", "Sucht nach neu installierten Programmen", {}, risk=0, module=M)
def apps_rescan():
    new = index.scan()
    if new:
        return f"{len(index.apps)} Programme bekannt. Neu: {', '.join(new[:10])}."
    return f"Programmliste aktualisiert – {len(index.apps)} Programme bekannt, nichts Neues."


# ================================================================== Fenster
def _win_action(name, ctx, fn, verb):
    w = _target_window(name, ctx)
    if not w:
        return {"ok": False, "text": "Ich finde das Fenster nicht."}
    fn(w["hwnd"])
    return f"{_short(w['title'])} {verb}."


@action("window_minimize", "Minimiert ein Fenster", {"name": S("Fenster/Programm oder 'das'")}, risk=1, module=M)
def window_minimize(name="das", ctx=None):
    return _win_action(name, ctx, win.minimize, "minimiert")


@action("window_maximize", "Maximiert ein Fenster", {"name": S("Fenster/Programm oder 'das'")}, risk=1, module=M)
def window_maximize(name="das", ctx=None):
    return _win_action(name, ctx, win.maximize, "maximiert")


@action("window_restore", "Stellt ein Fenster wieder her und holt es nach vorne", {"name": S("Fenster/Programm oder 'das'")}, risk=1, module=M)
def window_restore(name="das", ctx=None):
    return _win_action(name, ctx, win.restore, "wiederhergestellt")


@action("windows_minimize_all", "Minimiert alle Fenster (Desktop anzeigen)", {}, risk=1, module=M)
def windows_minimize_all():
    win.minimize_all()
    return "Alle Fenster minimiert."


@action("window_to_monitor", "Verschiebt ein Fenster auf einen anderen Monitor",
        {"name": S("Fenster oder 'das'"), "monitor": N("Monitor-Nummer ab 1")}, risk=1, module=M)
def window_to_monitor(name="das", monitor=2, ctx=None):
    w = _target_window(name, ctx)
    if not w:
        return {"ok": False, "text": "Ich finde das Fenster nicht."}
    if not win.move_to_monitor(w["hwnd"], int(monitor) - 1):
        return {"ok": False, "text": f"Monitor {int(monitor)} gibt es nicht."}
    return f"{_short(w['title'])} ist jetzt auf Monitor {int(monitor)}."


@action("windows_list", "Listet offene Fenster", {}, risk=0, module=M)
def windows_list():
    ws = [w for w in win.visible_windows() if not win.is_own(w["hwnd"])]
    if not ws:
        return "Es sind keine Fenster offen."
    return {"text": "Offen sind: " + ", ".join(_short(w["title"]) for w in ws[:12]) + ".", "data": ws}


def _short(title):
    parts = re.split(r" [-–—|] ", title)
    return (parts[-1] if len(parts) > 1 and len(parts[-1]) < 30 else parts[0])[:50]


# ==================================================================== Audio
@action("volume_set", "Setzt die Systemlautstärke", {"level": N("0 bis 100")}, risk=1, module=M)
def volume_set(level):
    sysm.set_volume(float(level))
    return f"Lautstärke auf {int(float(level))} Prozent."


@action("volume_change", "Ändert die Lautstärke relativ", {"delta": N("z. B. 10 oder -10")}, risk=1, module=M)
def volume_change(delta):
    new = max(0, min(100, sysm.get_volume() + float(delta)))
    sysm.set_volume(new)
    return f"Lautstärke {'erhöht' if float(delta) > 0 else 'verringert'} auf {int(new)} Prozent."


@action("volume_get", "Nennt die aktuelle Lautstärke", {}, risk=0, module=M)
def volume_get():
    return f"Die Lautstärke steht auf {sysm.get_volume()} Prozent{', stummgeschaltet' if sysm.is_muted() else ''}."


@action("mute", "Schaltet den Ton stumm oder wieder an", {"on": B("true = stumm")}, risk=1, module=M)
def mute(on=True):
    sysm.set_mute(bool(on))
    return "Ton stummgeschaltet." if on else "Ton ist wieder an."


@action("audio_output", "Wechselt das Audio-Ausgabegerät", {"name": S("Name des Geräts, z. B. 'Kopfhörer'")}, risk=1, module=M)
def audio_output(name, exact=False):
    devs = sysm.output_devices()
    if exact:
        dev = next((d for d in devs if d["name"] == name), None)
    else:
        scored = sorted(((max(similarity(name, d["name"]), similarity(name, d["name"].split("(")[0])), d) for d in devs),
                        key=lambda x: -x[0])
        dev = scored[0][1] if scored and scored[0][0] >= 0.5 else None
        if dev is None and len(devs) > 1:
            return {"text": "Welches Gerät? " + ", ".join(d["name"] for d in devs) + ".",
                    "data": {"choose": [d["name"] for d in devs], "param": "name"}}
    if not dev:
        return {"ok": False, "text": f"Kein Audiogerät „{name}“ gefunden."}
    sysm.set_output_device(dev["id"])
    return f"Ton läuft jetzt über {dev['name']}."


@action("audio_devices", "Listet Audio-Ausgabegeräte", {}, risk=0, module=M)
def audio_devices():
    devs = sysm.output_devices()
    return {"text": "Ausgabegeräte: " + ", ".join(d["name"] + (" (aktiv)" if d["default"] else "") for d in devs) + ".",
            "data": devs}


@action("media", "Mediensteuerung", {"key": S("play, next, prev oder stop", ["play", "next", "prev", "stop"])}, risk=1, module=M)
def media(key):
    sysm.media_key(key)
    return {"play": "Wiedergabe umgeschaltet.", "next": "Nächster Titel.", "prev": "Vorheriger Titel.", "stop": "Gestoppt."}[key]


# ================================================================== Energie
@action("pc_lock", "Sperrt den PC", {}, risk=1, module=M)
def pc_lock():
    threading.Timer(1.5, sysm.lock).start()
    return "PC wird gesperrt."


@action("pc_shutdown", "Fährt den PC herunter", {}, risk=2, module=M,
        confirm_text="Soll ich den PC wirklich herunterfahren?")
def pc_shutdown():
    sysm.shutdown(False, 10)
    return "Der PC fährt in 10 Sekunden herunter. Sag „abbrechen“, um das zu stoppen."


@action("pc_restart", "Startet den PC neu", {}, risk=2, module=M, confirm_text="Soll ich den PC wirklich neu starten?")
def pc_restart():
    sysm.shutdown(True, 10)
    return "Der PC startet in 10 Sekunden neu. Sag „abbrechen“, um das zu stoppen."


@action("pc_sleep", "Versetzt den PC in den Energiesparmodus", {}, risk=2, module=M,
        confirm_text="Soll der PC in den Energiesparmodus?")
def pc_sleep():
    threading.Timer(2, sysm.sleep).start()
    return "Gute Nacht – Energiesparmodus wird aktiviert."


@action("pc_abort_shutdown", "Bricht ein geplantes Herunterfahren ab", {}, risk=0, module=M)
def pc_abort_shutdown():
    return "Herunterfahren abgebrochen." if sysm.abort_shutdown() else "Es war kein Herunterfahren geplant."


@action("monitors_off", "Schaltet die Bildschirme aus", {}, risk=1, module=M)
def monitors_off():
    threading.Timer(1, sysm.monitors_off).start()
    return "Bildschirme gehen aus."


# ============================================================== Bildschirm
@action("screenshot", "Erstellt einen Screenshot", {"monitor": N("Monitor-Nummer ab 1, leer = alle")}, required=[], risk=1, module=M)
def screenshot(monitor=None, ctx=None):
    p = sysm.screenshot(None if monitor in (None, "", 0) else int(monitor) - 1)
    if ctx:
        ctx.last_file = str(p)
    bus.emit("screenshot", path=str(p))
    return {"text": f"Screenshot gespeichert unter Bilder/JARVIS.", "data": str(p)}


@action("active_window", "Nennt das aktive Fenster", {}, risk=0, module=M)
def active_window(ctx=None):
    w = _target_window("das", ctx)
    if not w:
        return "Gerade ist kein anderes Fenster aktiv."
    return {"text": f"Aktiv ist {w['title']} ({w['process']}).", "data": w}


@action("monitors_info", "Listet die angeschlossenen Monitore", {}, risk=0, module=M)
def monitors_info():
    ms = sysm.monitors()
    return {"text": f"{len(ms)} Monitor(e): " + ", ".join(f"Monitor {m['index'] + 1} mit {m['width']}×{m['height']}" for m in ms) + ".",
            "data": ms}


# ============================================================ Systeminfos
@action("system_info", "Zeigt Systeminformationen und Auslastung", {}, risk=0, module=M)
def system_info():
    s = sysm.stats()
    parts = [f"CPU {s['cpu']:.0f} %", f"Arbeitsspeicher {s['ram']:.0f} % ({s['ram_used'] / 2**30:.1f} von {s['ram_total'] / 2**30:.0f} GB)"]
    if s["gpu"]:
        parts.append(f"Grafikkarte {s['gpu']['load']:.0f} % bei {s['gpu']['temp']:.0f} °C")
    for d in s["disks"][:3]:
        parts.append(f"Laufwerk {d['mount'][:2]} {d['percent']:.0f} % belegt")
    if s["battery"]:
        parts.append(f"Akku {s['battery']['percent']:.0f} %")
    return {"text": "Auslastung: " + ", ".join(parts) + ".", "data": s}


@action("top_processes", "Zeigt die Programme mit dem höchsten Speicherverbrauch", {}, risk=0, module=M)
def top_processes():
    tp = sysm.top_processes()
    return "Den meisten Speicher brauchen: " + ", ".join(f"{n} ({r / 2**20:.0f} MB)" for r, n in tp) + "."


# =========================================================== Zwischenablage
@action("clipboard_read", "Liest den Text in der Zwischenablage", {}, risk=0, module=M)
def clipboard_read():
    t = sysm.clipboard_get()
    return {"text": f"In der Zwischenablage steht: {t[:500]}" if t else "Die Zwischenablage ist leer.", "data": t}


@action("clipboard_write", "Kopiert Text in die Zwischenablage", {"text": S("Der Text")}, risk=1, module=M)
def clipboard_write(text):
    sysm.clipboard_set(text)
    return "In die Zwischenablage kopiert."


# ====================================================================== Web
@action("open_url", "Öffnet eine Webseite im Browser", {"url": S("URL oder Domain")}, risk=1, module=M)
def open_url(url):
    if not re.match(r"^https?://", url):
        url = "https://" + url.strip()
    webbrowser.open(url)
    return f"{urllib.parse.urlparse(url).netloc.replace('www.', '')} wird geöffnet."


@action("web_search_open", "Öffnet eine Websuche im Browser",
        {"query": S("Suchbegriff"), "site": S("google oder youtube", ["google", "youtube"])}, required=["query"], risk=1, module=M)
def web_search_open(query, site="google"):
    q = urllib.parse.quote_plus(query)
    url = f"https://www.youtube.com/results?search_query={q}" if site == "youtube" else f"https://www.google.de/search?q={q}"
    webbrowser.open(url)
    return f"Ich suche {'auf YouTube' if site == 'youtube' else 'im Netz'} nach {query}."


# ================================================================== Dateien
@action("open_folder", "Öffnet einen Ordner (z. B. Downloads, Dokumente, Desktop oder Pfad)", {"name": S("Ordnername oder Pfad")}, risk=1, module=M)
def open_folder(name, ctx=None):
    kf = sysm.known_folder(name)
    if kf:
        os.startfile(str(kf))
        return f"Ordner {name.title()} geöffnet."
    if os.path.isdir(name):
        os.startfile(name)
        return "Ordner geöffnet."
    hits = sysm.search_files(name, limit=5, kind="folder")
    if hits:
        os.startfile(hits[0])
        return f"Ordner {Path(hits[0]).name} geöffnet."
    return {"ok": False, "text": f"Keinen Ordner „{name}“ gefunden."}


@action("find_files", "Sucht Dateien nach Namen", {"query": S("Teil des Dateinamens")}, risk=0, module=M)
def find_files(query):
    hits = sysm.search_files(query)
    if not hits:
        return {"ok": False, "text": f"Keine Dateien zu „{query}“ gefunden."}
    bus.emit("files_found", files=hits)
    return {"text": f"{len(hits)} Treffer, zum Beispiel: " + ", ".join(Path(h).name for h in hits[:5]) + ".", "data": hits}


@action("open_file", "Öffnet eine Datei (Suche nach Namen)", {"query": S("Dateiname oder Pfad")}, risk=1, module=M)
def open_file(query, exact=False, ctx=None):
    if os.path.exists(query):
        os.startfile(query)
        return f"{Path(query).name} geöffnet."
    hits = sysm.search_files(query, limit=6)
    if not hits:
        return {"ok": False, "text": f"Keine Datei „{query}“ gefunden."}
    exact_hits = [h for h in hits if Path(h).stem.lower() == query.lower()]
    if len(hits) > 1 and not exact_hits:
        names = [h for h in hits[:4]]
        return {"text": "Welche meinst du? " + "; ".join(f"{i + 1}. {Path(h).name}" for i, h in enumerate(names)),
                "data": {"choose": names, "param": "query"}}
    path = (exact_hits or hits)[0]
    os.startfile(path)
    if ctx:
        ctx.last_file = path
    return f"{Path(path).name} geöffnet."


@action("delete_file", "Verschiebt eine Datei in den Papierkorb", {"path": S("Vollständiger Pfad")}, risk=3, module=M,
        confirm_text="Soll ich {path} wirklich in den Papierkorb verschieben?")
def delete_file(path):
    if not os.path.exists(path):
        return {"ok": False, "text": "Die Datei existiert nicht."}
    return "In den Papierkorb verschoben." if sysm.recycle(path) else {"ok": False, "text": "Konnte nicht gelöscht werden."}


# ============================================================ Zeit & Timer
@action("time_now", "Nennt Uhrzeit und Datum", {}, risk=0, module=M)
def time_now():
    now = datetime.datetime.now()
    days = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
    months = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"]
    return f"Es ist {now.hour}:{now.minute:02d} Uhr, {days[now.weekday()]}, der {now.day}. {months[now.month - 1]} {now.year}."


_timers = []


@action("timer", "Stellt einen Timer/Erinnerung", {"seconds": N("Dauer in Sekunden"), "label": S("Wofür")}, required=["seconds"], risk=1, module=M)
def timer(seconds, label=""):
    seconds = float(seconds)

    def fire():
        bus.emit("notify", title="Timer", text=f"Timer abgelaufen{': ' + label if label else ''}.", speak=True, priority=2)
    t = threading.Timer(seconds, fire)
    t.daemon = True
    t.start()
    _timers.append(t)
    m, s = divmod(int(seconds), 60)
    dur = (f"{m} Minute{'n' if m != 1 else ''}" if m else "") + (f" {s} Sekunden" if s else "")
    return f"Timer für {dur.strip()} gestellt."


# ================================================================== Modul
class PCModule(Module):
    name = "pc"
    title = "PC-Steuerung"

    def start(self):
        global tracker
        tracker = win.ForegroundTracker()
        threading.Thread(target=self._scanner, daemon=True, name="app-scan").start()

    def _scanner(self):
        first = True
        while True:
            try:
                new = index.scan()
                if new and not first:
                    log.activity("pc", f"Neue Programme erkannt: {', '.join(new[:5])}")
                    bus.emit("notify", title="Neue Programme", text="Neu erkannt: " + ", ".join(new[:5]), speak=False)
            except Exception as e:
                log.error("Programmscan", e)
            first = False
            time.sleep(600)

    def status(self):
        return {"apps": len(index.apps), "last_scan": index.last_scan}

    def diagnose(self):
        out = []
        out.append(("Programmindex", len(index.apps) > 0, f"{len(index.apps)} Programme bekannt"))
        try:
            out.append(("Audio-Steuerung", True, f"Lautstärke {sysm.get_volume()} %"))
        except Exception as e:
            out.append(("Audio-Steuerung", False, log.friendly(e)))
        try:
            out.append(("Monitore", True, f"{len(sysm.monitors())} erkannt"))
        except Exception as e:
            out.append(("Monitore", False, log.friendly(e)))
        return out

    # ------------------------------------------------------------ parser
    def parse(self, text, n, ctx):
        I = Intent

        # --- Zeit
        if re.search(r"\b(wie spät|wie viel uhr|wieviel uhr|uhrzeit|welcher tag|welches datum|den wievielten)\b", n):
            return I([("time_now", {})])

        # --- Timer
        m = re.search(r"(timer|wecker|erinnere mich)\b.*?\b(in|auf|für)?\s*([\w,.]+)\s*(sekunden?|minuten?|stunden?)\b(.*)$", n)
        if m and re.match(r"^(stell|setz|starte|mach|erinnere|timer)", n):
            num = find_number(m.group(3)) or 1
            mult = {"s": 1, "m": 60, "h": 3600}[{"sek": "s", "min": "m", "stu": "h"}[m.group(4)[:3]]]
            label = re.sub(r"^\s*(an|daran|dass|zu|mit)\s+", "", m.group(5)).strip()
            return I([("timer", {"seconds": num * mult, "label": label})])

        # --- Not-Aus Herunterfahren
        if re.search(r"(herunterfahren|neustart|shutdown) (abbrechen|stoppen)|^abbrechen$|nicht herunterfahren", n):
            return I([("pc_abort_shutdown", {})], confidence=0.9)

        # --- Energie
        if re.search(r"(fahr|fahre) (den |meinen )?(pc|computer|rechner|laptop)? ?(herunter|runter)|^herunterfahren$|^(pc|computer|rechner) (aus|ausschalten|herunterfahren)$|schalte? (den )?(pc|computer|rechner) aus", n):
            return I([("pc_shutdown", {})])
        if re.search(r"^neustart$|(starte?|start) (den |meinen )?(pc|computer|rechner) neu|(pc|computer|rechner) neu ?starten", n):
            return I([("pc_restart", {})])
        if re.search(r"(sperr|sperre) (den |meinen )?(pc|computer|rechner|bildschirm)|(pc|computer|bildschirm) sperren|^sperren$", n):
            return I([("pc_lock", {})])
        if re.search(r"energiesparmodus|standby|ruhezustand|schlafmodus", n):
            return I([("pc_sleep", {})])
        if re.search(r"(bildschirm|monitor)e? aus", n):
            return I([("monitors_off", {})])

        # --- Audio
        if re.search(r"(^|\b)(ton|sound|lautsprecher|pc) (aus|stumm)|stummschalten|^stumm$|mute", n) and "aufheben" not in n:
            return I([("mute", {"on": True})])
        if re.search(r"(ton|sound) (wieder )?an|stummschaltung aufheben|unmute|laut ?schalten", n):
            return I([("mute", {"on": False})])
        m = re.search(r"lautstärke (auf |zu )?([\w,.]+)( prozent|%)?$", n)
        if m and find_number(m.group(2)) is not None:
            return I([("volume_set", {"level": find_number(m.group(2))})])
        if re.search(r"(wie laut|welche lautstärke|lautstärke\?*$)", n):
            return I([("volume_get", {})])
        m = re.search(r"\b(viel |deutlich |etwas |ein bisschen |bisschen |ein wenig )?(lauter|leiser)\b", n)
        if m and not re.search(r"(sprich|rede|stimme)", n):
            step = 25 if m.group(1) in ("viel ", "deutlich ") else 5 if m.group(1) in ("etwas ", "ein bisschen ", "bisschen ", "ein wenig ") else 10
            num = find_number(n)
            if num and num <= 100:
                step = num
            return I([("volume_change", {"delta": step if m.group(2) == "lauter" else -step})])
        m = re.search(r"(wechsel|wechsle|schalte?|stell|stelle)? ?(den |das )?(ton|audio|sound|ausgabe|audiogerät|audio ausgabe|ausgabegerät)( auf| zu| über)? (.+?)( um)?$", n)
        if m and re.search(r"(wechsel|schalt|stell|über|auf )", n):
            return I([("audio_output", {"name": m.group(5)})], confidence=0.9)
        if re.search(r"^(auf |zu )?(kopfhörer|headset|lautsprecher|boxen)( umschalten| wechseln)$", n):
            return I([("audio_output", {"name": re.search(r"(kopfhörer|headset|lautsprecher|boxen)", n).group(1)})])
        if re.search(r"welche (audio|ausgabe|sound)geräte|audiogeräte", n):
            return I([("audio_devices", {})])
        if re.search(r"^(pause|musik pausieren|pausiere|musik (an|weiter)|play|wiedergabe)$", n):
            return I([("media", {"key": "play"})])
        if re.search(r"(nächste[rsn]? (lied|song|titel)|skip|überspringen)", n):
            return I([("media", {"key": "next"})])
        if re.search(r"(vorherige[rsn]? (lied|song|titel)|letzte[rsn]? (lied|song|titel))", n):
            return I([("media", {"key": "prev"})])

        # --- Screenshot
        if re.search(r"screenshot|bildschirmfoto|bildschirm ?aufnahme", n) and not re.search(r"analys|was siehst", n):
            mon = re.search(r"monitor (\w+)", n)
            return I([("screenshot", {"monitor": find_number(mon.group(1)) if mon else None})])

        # --- Systeminfo
        if re.search(r"(systeminfo|system ?status|auslastung|wie geht es (dem|meinem) (pc|computer|rechner)|cpu|arbeitsspeicher|ram\b|festplatte|speicherplatz|temperatur|grafikkarte|gpu|akku)", n) \
                and not re.search(r"(öffne|starte|handy|smartphone|telefon|iphone)", n):
            if re.search(r"(was|welche).*(verbraucht|frisst|braucht).*(speicher|ram)", n):
                return I([("top_processes", {})])
            return I([("system_info", {})])
        if re.search(r"welche monitore|wie viele (monitore|bildschirme)", n):
            return I([("monitors_info", {})])

        # --- Zwischenablage
        if re.search(r"(was (ist|steht) in der zwischenablage|zwischenablage (vorlesen|lesen|anzeigen)|lies (mir )?die zwischenablage)", n):
            return I([("clipboard_read", {})])
        m = re.match(r"^kopier(e)? (.+) in die zwischenablage$", n)
        if m:
            return I([("clipboard_write", {"text": _orig(text, m.group(2))})])

        # --- Fenster
        if re.search(r"(alle fenster minimier|alles minimier|zeig (mir )?den desktop|desktop anzeigen)", n):
            return I([("windows_minimize_all", {})])
        if re.search(r"welche fenster|was ist (gerade )?offen", n):
            return I([("windows_list", {})])
        if re.search(r"welches fenster ist (aktiv|offen)|was ist das (für ein )?(fenster|programm)|was ist gerade aktiv", n):
            return I([("active_window", {})])
        m = re.match(r"^(?:minimiere|minimier|verkleinere|verkleiner)\b\s*(?P<n>.*)$", n) or re.match(r"^(?P<n>.+) minimieren$", n)
        if m:
            return I([("window_minimize", {"name": m.group("n") or "das"})])
        m = re.match(r"^(?:maximiere|maximier|vergrößere|vergrößer)\b\s*(?P<n>.*)$", n) or re.match(r"^(?P<n>.+) (?:maximieren|in den vollbild|auf vollbild)$", n)
        if m:
            return I([("window_maximize", {"name": m.group("n") or "das"})])
        m = re.match(r"^(stell|stelle|hol|hole) (.+?) (wieder her|nach vorne|in den vordergrund|zurück)$", n)
        if m:
            return I([("window_restore", {"name": m.group(2)})])
        m = re.match(r"^(verschieb|verschiebe|schieb|schiebe) (.+?) auf (den )?(monitor|bildschirm) (\w+)$", n)
        if m:
            return I([("window_to_monitor", {"name": m.group(2), "monitor": find_number(m.group(5)) or 2})])

        # --- Schließen
        m = re.match(r"^(schließ|schließe|schliess|schliesse|beende|beend|mach) (.+?)( zu| aus)?$", n)
        if m and (m.group(1) != "mach" or m.group(3)):
            target = strip_articles(m.group(2)) if not THAT.match(m.group(2)) else m.group(2)
            if re.search(r"^(hart|sofort|zwangsweise) ", target):
                return I([("app_kill", {"name": target.split(" ", 1)[1]})])
            names = split_list(target) if not THAT.match(target) else [target]
            return I([("app_close", {"name": t}) for t in names], confidence=0.9 if m.group(1) != "mach" else 0.6)

        # --- Web
        m = re.match(r"^(?:google nach|google|googel|such|suche) ?(?:im internet|im netz|online|bei google)?(?: nach)? (?P<q>.+)$", n)
        if m and (n.startswith(("google", "googel")) or re.search(r"(im internet|im netz|online|bei google)", n)):
            return I([("web_search_open", {"query": m.group("q"), "site": "google"})])
        m = re.match(r"^(?:such|suche|zeig|zeig mir|spiel|spiele) (?:auf|bei) youtube(?: nach)? (?P<q>.+)$", n) \
            or re.match(r"^(?P<q>.+) auf youtube (?:suchen|abspielen|zeigen)$", n)
        if m:
            return I([("web_search_open", {"query": m.group("q"), "site": "youtube"})])

        # --- Dateien / Ordner
        m = re.match(r"^(?:öffne|zeig|zeige|mach)(?: mir)? (?:den |meinen |meine |die )?(?:ordner|verzeichnis) (?P<n>.+?)(?: auf)?$", n) \
            or re.match(r"^(?:öffne|zeig|zeige|mach)(?: mir)? (?:den |meinen |meine |die )?(?P<n>downloads?|dokumente|desktop|bilder|musik|videos|screenshots|papierkorb|autostart|dieser pc|arbeitsplatz)(?: ordner)?(?: auf)?$", n)
        if m:
            return I([("open_folder", {"name": m.group("n").strip()})])
        m = re.match(r"^(such|suche|finde|find) (mir )?(die |alle |meine )?(datei(en)?|dokument(e)?|bilder|fotos)? ?(namens |mit dem namen |nach )?(.+)$", n)
        if m and (m.group(4) or re.search(r"\.(pdf|docx?|xlsx?|png|jpe?g|txt|mp[34])\b", n)):
            return I([("find_files", {"query": m.group(8)})])
        m = re.match(r"^(öffne|zeig) (mir )?(die )?(datei|dokument|pdf|bild) (.+)$", n)
        if m:
            return I([("open_file", {"query": m.group(5)})])

        # --- Programme suchen
        if re.search(r"(such|suche|scanne) (nach )?neue(n)? programme|programme (neu )?(scannen|suchen|aktualisieren)", n):
            return I([("apps_rescan", {})])
        m = re.match(r"^(ist|habe ich) (.+) installiert$", n) or re.match(r"^such(e)? (das )?programm (.+)$", n)
        if m:
            return I([("app_search", {"query": m.group(m.lastindex)})])

        # --- Öffnen / Starten
        m = re.match(r"^(öffne|öffnen|starte|start|starten|führe|lauf|launch|zock|zocke|spiel|spiele|mach) (mir )?(.+?)( auf| an| aus| starten)?$", n)
        if m and (m.group(1) != "mach" or (m.group(4) or "").strip() in ("auf", "an")) and \
                not (m.group(1) in ("spiel", "spiele") and not m.group(3)):
            target = m.group(3)
            if THAT.match(target):
                return I([("window_restore", {"name": "das"})], confidence=0.8)
            if re.match(r"^[\w\-]+\.(de|com|net|org|io|tv|gg|eu|at|ch)(/\S*)?$", target):
                return I([("open_url", {"url": target})])
            if target in SITES and not index.find(target):
                return I([("open_url", {"url": SITES[target]})])
            if target in SITES and index.find(target) and index.find(target)[0][0] < 0.9:
                return I([("open_url", {"url": SITES[target]})])
            args = ""
            am = re.search(r"\s+mit (den )?(argument(en)?|parameter[n]?|startoptionen?) (.+)$", target)
            if am:
                args, target = _orig(text, am.group(4)), target[:am.start()]
            names = split_list(target)
            if len(names) > 1 and all(index.find(x) for x in names):
                return I([("app_open", {"name": x}) for x in names], confidence=0.85)
            return I([("app_open", {"name": _orig(text, target), "args": args})], confidence=0.85)
        return None


def _orig(text, frag):
    i = text.lower().find(frag.lower())
    return text[i:i + len(frag)] if i >= 0 else frag
