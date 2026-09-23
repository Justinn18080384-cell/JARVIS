"""Wetter, Morgen-Briefing und Recherche-Agent (Ideen aus OpenJarvis).

Wetter:      Open-Meteo (kostenlos, ohne Schlüssel)
Briefing:    Begrüßung, Datum, Wetter, heutige Routinen, Notizen, PC-Hinweise, optional Nachrichten (KI + Websuche)
Recherche:   plant Teilfragen, recherchiert jede mit Websuche, schreibt einen Bericht
             (Dokumente\\JARVIS\\Recherchen) und fasst ihn mündlich zusammen
"""
import datetime
import html
import re
import threading
import time
from pathlib import Path

import requests

from ..core import log
from ..core.actions import action, S
from ..core.config import config
from ..core.events import bus
from ..core.module import Module, Intent
from . import memory

M = "briefing"
_mod = None
DAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"]
WMO = {0: "klar", 1: "überwiegend klar", 2: "teils bewölkt", 3: "bedeckt", 45: "neblig", 48: "neblig mit Reif",
       51: "leichter Nieselregen", 53: "Nieselregen", 55: "starker Nieselregen", 56: "gefrierender Niesel", 57: "gefrierender Niesel",
       61: "leichter Regen", 63: "Regen", 65: "starker Regen", 66: "gefrierender Regen", 67: "gefrierender Regen",
       71: "leichter Schneefall", 73: "Schneefall", 75: "starker Schneefall", 77: "Schneegriesel",
       80: "Regenschauer", 81: "Regenschauer", 82: "heftige Regenschauer", 85: "Schneeschauer", 86: "Schneeschauer",
       95: "Gewitter", 96: "Gewitter mit Hagel", 99: "Gewitter mit Hagel"}


def _city(city=""):
    if city:
        return city
    if config.get("briefing.city"):
        return config.get("briefing.city")
    r = memory.find("Wohnort") or memory.find("Stadt")
    return r["value"] if r else ""


def weather_data(city):
    g = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                     params={"name": city, "count": 1, "language": "de"}, timeout=8).json()
    if not g.get("results"):
        raise ValueError(f"Den Ort „{city}“ finde ich nicht.")
    loc = g["results"][0]
    w = requests.get("https://api.open-meteo.com/v1/forecast", params={
        "latitude": loc["latitude"], "longitude": loc["longitude"], "timezone": "auto", "forecast_days": 3,
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code"}, timeout=8).json()
    if "daily" not in w or "current" not in w:
        log.logger().warning("Open-Meteo-Antwort unvollständig: %s", str(w)[:300])
        raise RuntimeError("Der Wetterdienst hat keine Daten geliefert" + (f" ({w['reason']})" if w.get("reason") else "") + ".")
    return loc["name"], w


def weather_text(city, day=0):
    name, w = weather_data(city)
    d = w["daily"]
    desc = WMO.get(d["weather_code"][day], "wechselhaft")
    rain = d["precipitation_probability_max"][day]
    when = ["Heute", "Morgen", "Übermorgen"][day]
    txt = f"{when} in {name}: {desc}, {round(d['temperature_2m_min'][day])} bis {round(d['temperature_2m_max'][day])} Grad"
    if rain is not None and rain >= 30:
        txt += f", Regenwahrscheinlichkeit {rain} Prozent"
    if day == 0:
        c = w["current"]
        txt = f"In {name} ist es gerade {WMO.get(c['weather_code'], '')} bei {round(c['temperature_2m'])} Grad. " + txt
    return txt + "."


# ================================================================ Aktionen
@action("weather", "Wetter und Vorhersage für einen Ort", {"city": S("Ort, leer = Wohnort des Nutzers"),
        "day": {"type": "integer", "description": "0 = heute, 1 = morgen, 2 = übermorgen"}}, required=["city", "day"], risk=0, module=M)
def weather(city="", day=0):
    c = _city(city)
    if not c:
        return {"ok": False, "text": "Für welchen Ort? Sag zum Beispiel „Ich wohne in Berlin“, dann merke ich mir das."}
    try:
        return weather_text(c, int(day or 0))
    except ValueError as e:
        return {"ok": False, "text": str(e)}


@action("morning_briefing", "Morgen-Briefing: Datum, Wetter, heutige Routinen, Notizen, PC-Hinweise und Nachrichten", {}, risk=0, module=M)
def morning_briefing(ctx=None):
    parts = []
    now = datetime.datetime.now()
    name = config.get("user_name")
    greet = "Guten Morgen" if now.hour < 11 else "Guten Tag" if now.hour < 18 else "Guten Abend"
    parts.append(f"{greet}{', ' + name if name else ''}. Heute ist {DAYS[now.weekday()]}, der {now.day}. {MONTHS[now.month - 1]}, es ist {now.hour}:{now.minute:02d} Uhr.")
    c = _city()
    if c:
        try:
            parts.append(weather_text(c))
        except Exception as e:
            log.logger().warning("Briefing-Wetter: %s", e)
    # heutige zeitgesteuerte Routinen
    try:
        from . import automation
        todays = sorted((a["schedule"]["time"], a["name"]) for a in automation.load_all()
                        if a.get("enabled", True) and a.get("schedule") and now.weekday() in a["schedule"].get("days", range(7))
                        and a["schedule"]["time"] > now.strftime("%H:%M"))
        if todays:
            parts.append("Heute geplant: " + ", ".join(f"{n} um {t}" for t, n in todays[:4]) + ".")
    except Exception:
        pass
    # Notizen der letzten Tage
    notes = [r for r in memory.all_facts() if r["label"].startswith("Notiz") and time.time() - r["updated"] < 3 * 86400]
    if notes:
        parts.append("Deine Notizen: " + "; ".join(n["value"] for n in notes[:3]) + ".")
    # PC-Hinweise
    try:
        import psutil
        full = [p.mountpoint[:2] for p in psutil.disk_partitions() if "cdrom" not in p.opts and p.fstype
                and psutil.disk_usage(p.mountpoint).percent > 90]
        if full:
            parts.append(f"Hinweis: Laufwerk {', '.join(full)} ist fast voll.")
    except Exception:
        pass
    upd = _mod.jarvis.modules.get("updater") if _mod else None
    if upd and upd.available:
        parts.append(f"Ein JARVIS-Update auf Version {upd.available['version']} ist verfügbar.")
    # Nachrichten (optional, KI mit Websuche)
    ai = _mod.jarvis.modules.get("ai") if _mod else None
    if config.get("briefing.include_news") and ai and ai.available() and ai.provider() == "openai":
        try:
            news = ai.complete("Nenne die drei wichtigsten aktuellen Nachrichten aus Deutschland und der Welt von heute, "
                               "je in einem kurzen gesprochenen Satz, ohne Quellenangaben und ohne Aufzählungszeichen.",
                               system="Du bist ein knapper deutscher Nachrichtensprecher.", web=True)
            if news:
                parts.append("Die Nachrichten: " + re.sub(r"\s*\n+\s*", " ", re.sub(r"\[.*?\]\(.*?\)", "", news)))
        except Exception as e:
            log.logger().warning("Briefing-Nachrichten: %s", e)
    return {"text": " ".join(parts), "data": {"parts": parts}}


@action("research", "Recherche-Agent: recherchiert ein Thema gründlich im Internet, erstellt einen Bericht und fasst ihn zusammen",
        {"topic": S("Das Thema oder die Frage")}, risk=0, module=M)
def research(topic, ctx=None):
    ai = _mod.jarvis.modules.get("ai") if _mod else None
    if not ai or not ai.available():
        return {"ok": False, "text": "Für Recherchen brauche ich die KI (OpenAI-Schlüssel oder ein Ollama-Modell)."}
    threading.Thread(target=_mod.run_research, args=(topic,), daemon=True, name="research").start()
    return f"Ich recherchiere zu „{topic}“. Das dauert einen Moment – ich melde mich, sobald der Bericht fertig ist."


# ================================================================== Modul
class BriefingModule(Module):
    name = "briefing"
    title = "Briefing & Recherche"

    def __init__(self, jarvis):
        super().__init__(jarvis)
        global _mod
        _mod = self
        self.researching = 0

    def run_research(self, topic):
        ai = self.jarvis.modules["ai"]
        self.researching += 1
        bus.emit("research", topic=topic, state="start")
        try:
            web = ai.provider() == "openai"
            if web:
                plan = ai.complete(f"Thema: {topic}\nZerlege das Thema in 3 bis 5 präzise Teilfragen für eine gründliche Web-Recherche. "
                                   "Antworte nur mit den Teilfragen, eine pro Zeile, ohne Nummerierung.",
                                   system="Du bist ein sorgfältiger Rechercheplaner.")
                questions = [q.strip(" -•*0123456789.") for q in plan.splitlines() if len(q.strip()) > 8][:5] or [topic]
                findings = []
                for i, q in enumerate(questions, 1):
                    bus.emit("notify", title="Recherche", text=f"Teilfrage {i}/{len(questions)}: {q}", speak=False)
                    ans = ai.complete(f"Recherchiere mit der Websuche: {q}\nGib die wichtigsten Fakten auf Deutsch wieder, mit Zahlen und Daten, "
                                      "und nenne am Ende die Quellen als Liste von URLs.",
                                      system="Du bist ein gründlicher Rechercheur. Nutze aktuelle, verlässliche Quellen.", web=True)
                    findings.append(f"### {q}\n{ans}")
                material = "\n\n".join(findings)
            else:
                material = "(Keine Websuche verfügbar – Bericht basiert auf dem Wissen des lokalen Modells.)"
            report = ai.complete(
                f"Thema: {topic}\n\nRechercheergebnisse:\n{material}\n\n"
                "Schreibe daraus einen gut strukturierten deutschen Bericht in Markdown: # Titel, ## Kurzfassung (3–4 Sätze), "
                "## Kernpunkte (Liste), ## Details (Abschnitte), ## Quellen (Liste der URLs aus den Ergebnissen). "
                "Bleib bei den Fakten, markiere Unsicherheiten.",
                system="Du bist ein präziser Analyst und Autor.")
            path = _save_report(topic, report)
            summary = ai.complete(f"Fasse diesen Bericht in 2 bis 3 gesprochenen Sätzen zusammen, ohne Markdown:\n\n{report[:6000]}",
                                  system="Du bist JARVIS und sprichst knapp und natürlich.")
            log.activity(M, f"Recherche „{topic}“ fertig: {path.name}")
            import os
            os.startfile(path)
            self.jarvis.brain.reply(f"Die Recherche zu „{topic}“ ist fertig. {summary} Den ausführlichen Bericht habe ich geöffnet.")
        except Exception as e:
            self.jarvis.brain.reply(f"Die Recherche zu „{topic}“ ist fehlgeschlagen: {log.error('Recherche', e)}")
        finally:
            self.researching -= 1
            bus.emit("research", topic=topic, state="done")

    def parse(self, text, n, ctx):
        if re.match(r"^(guten morgen|morgen|moin)$", n) or re.search(r"(morgen[- ]?briefing|^briefing$|tagesüberblick|was steht heute an|brief(e|ing) mich|mein briefing)", n):
            return Intent([("morning_briefing", {})])
        if re.search(r"\bwetter", n):
            cm = re.search(r"\b(?:in|für) (?P<c>[\wäöüß\- ]+?)(?: (?:heute|morgen|übermorgen))?$", n)
            day = 2 if "übermorgen" in n else 1 if re.search(r"\bmorgen\b", n) else 0
            return Intent([("weather", {"city": (cm.group("c") if cm else "").strip(), "day": day})])
        if re.search(r"(wird es|regnet es|brauche ich (einen )?(schirm|regenschirm|jacke))", n):
            return Intent([("weather", {"city": "", "day": 1 if "morgen" in n else 0})])
        m = re.match(r"^(recherchiere|recherchier|mach eine recherche|erstelle eine recherche|finde alles heraus|finde heraus|informier dich)"
                     r"(?: (?:über|zu|zum thema|zur frage|dazu,?|,?))? (?P<t>.+)$", n)
        if m:
            return Intent([("research", {"topic": _orig(text, m.group("t"))})])
        return None

    def status(self):
        return {"researching": self.researching, "city": _city()}

    def diagnose(self):
        c = _city()
        if not c:
            return [("Wetter", True, "kein Ort hinterlegt (sag „Ich wohne in …“)")]
        try:
            weather_data(c)
            return [("Wetter", True, f"Open-Meteo erreichbar ({c})")]
        except Exception as e:
            return [("Wetter", False, log.friendly(e))]


def _save_report(topic, md):
    folder = Path.home() / "Documents" / "JARVIS" / "Recherchen"
    folder.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^\w\- ]", "", topic)[:60].strip().replace(" ", "_") or "Recherche"
    base = folder / f"{time.strftime('%Y-%m-%d_%H-%M')}_{slug}"
    base.with_suffix(".md").write_text(md, "utf-8")
    out = base.with_suffix(".html")
    out.write_text(_html(topic, md), "utf-8")
    return out


def _html(title, md):
    lines, out, in_list = md.splitlines(), [], False

    def inline(s):
        s = html.escape(s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"\[(.+?)\]\((https?://[^\s)]+)\)", r'<a href="\2">\1</a>', s)
        s = re.sub(r'(?<!href=")(https?://[^\s<)]+)', r'<a href="\1">\1</a>', s)
        return s
    for l in lines:
        m = re.match(r"^(#{1,4})\s+(.*)", l)
        li = re.match(r"^\s*[-*•]\s+(.*)", l) or re.match(r"^\s*\d+[.)]\s+(.*)", l)
        if li:
            if not in_list:
                out.append("<ul>"); in_list = True
            out.append(f"<li>{inline(li.group(1))}</li>")
            continue
        if in_list:
            out.append("</ul>"); in_list = False
        if m:
            n = len(m.group(1))
            out.append(f"<h{n}>{inline(m.group(2))}</h{n}>")
        elif l.strip():
            out.append(f"<p>{inline(l)}</p>")
    if in_list:
        out.append("</ul>")
    return f"""<!DOCTYPE html><html lang="de"><head><meta charset="utf-8"><title>{html.escape(title)} – JARVIS-Recherche</title>
<style>body{{background:#03070c;color:#d6ecf7;font:16px/1.65 "Segoe UI",sans-serif;max-width:860px;margin:40px auto;padding:0 20px}}
h1,h2,h3{{font-family:Bahnschrift,sans-serif;color:#7af0ff;letter-spacing:.04em}}h1{{border-bottom:1px solid #19d3ff55;padding-bottom:10px}}
a{{color:#19d3ff}}li{{margin:4px 0}}.meta{{color:#6f8ea3;font-size:13px}}</style></head><body>
<div class="meta">JARVIS-Recherche · {time.strftime('%d.%m.%Y %H:%M')}</div>{''.join(out)}</body></html>"""


def _orig(text, frag):
    i = text.lower().find(frag.lower())
    return text[i:i + len(frag)] if i >= 0 else frag
