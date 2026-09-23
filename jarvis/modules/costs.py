"""KI-Kosten: zählt jede kostenpflichtige Nutzung mit und rechnet sie in Euro um.

OpenAI liefert pro Anfrage die verbrauchten Tokens – daraus und aus der Preistabelle unten
ergibt sich der Betrag. Die Preise sind Richtwerte (Stand der Programmierung) und können von der
tatsächlichen Rechnung leicht abweichen; die echte Abrechnung steht auf platform.openai.com/usage.
ElevenLabs rechnet in Zeichen ab – gezählt wird gegen das monatliche Freikontingent.
"""
import datetime
import re
import time

from ..core import log
from ..core.actions import action, S
from ..core.config import config
from ..core.db import db
from ..core.module import Module, Intent

M = "kosten"

# USD pro 1 Mio. Tokens: (Eingabe, zwischengespeicherte Eingabe, Ausgabe)
TOKEN_PRICES = {
    "gpt-5-nano": (0.05, 0.005, 0.40),
    "gpt-5-mini": (0.25, 0.025, 2.00),
    "gpt-5": (1.25, 0.125, 10.00),
    "gpt-4.1-nano": (0.10, 0.025, 0.40),
    "gpt-4.1-mini": (0.40, 0.10, 1.60),
    "gpt-4.1": (2.00, 0.50, 8.00),
    "gpt-4o-mini": (0.15, 0.075, 0.60),
    "gpt-4o": (2.50, 1.25, 10.00),
    "o4-mini": (1.10, 0.275, 4.40),
    "o3": (2.00, 0.50, 8.00),
}
DEFAULT_TOKEN_PRICE = TOKEN_PRICES["gpt-4.1-mini"]
WEB_SEARCH_USD = 0.01           # pro Websuche
TRANSCRIBE_USD_PER_MIN = 0.003  # gpt-4o-mini-transcribe
OPENAI_TTS_USD_PER_MIN = 0.015  # gpt-4o-mini-tts

SERVICE_NAMES = {"openai": "OpenAI", "elevenlabs": "ElevenLabs"}

db.execute("""CREATE TABLE IF NOT EXISTS costs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, service TEXT, kind TEXT, model TEXT,
    units REAL, unit TEXT, usd REAL
)""")


def _price(model):
    model = (model or "").lower()
    # längster passender Präfix gewinnt („gpt-4.1-mini-2025…“ → gpt-4.1-mini, nicht gpt-4.1)
    for name in sorted(TOKEN_PRICES, key=len, reverse=True):
        if model.startswith(name):
            return TOKEN_PRICES[name]
    return DEFAULT_TOKEN_PRICE


def record(service, kind, model, units, unit, usd):
    try:
        db.execute("INSERT INTO costs(ts, service, kind, model, units, unit, usd) VALUES(?,?,?,?,?,?,?)",
                   (time.time(), service, kind, model or "", float(units), unit, float(usd)))
    except Exception as e:
        log.error("Kosten erfassen", e)


def track_openai_response(resp, model, kind="Chat"):
    """Kosten einer Responses-API-Antwort (Tokens + Websuchen) erfassen."""
    try:
        u = getattr(resp, "usage", None)
        if u is None:
            return
        tin = getattr(u, "input_tokens", 0) or 0
        tout = getattr(u, "output_tokens", 0) or 0
        details = getattr(u, "input_tokens_details", None)
        cached = (getattr(details, "cached_tokens", 0) or 0) if details else 0
        p_in, p_cached, p_out = _price(getattr(resp, "model", None) or model)
        usd = ((tin - cached) * p_in + cached * p_cached + tout * p_out) / 1_000_000
        searches = sum(1 for o in (getattr(resp, "output", None) or []) if getattr(o, "type", "") == "web_search_call")
        if searches:
            usd += searches * WEB_SEARCH_USD
            kind += f" + {searches}× Websuche"
        record("openai", kind, model, tin + tout, "Tokens", usd)
    except Exception as e:
        log.error("Kosten erfassen", e)


def track_transcription(seconds, model="gpt-4o-mini-transcribe"):
    record("openai", "Spracherkennung", model, seconds, "Sekunden", seconds / 60 * TRANSCRIBE_USD_PER_MIN)


def track_openai_tts(seconds, model):
    record("openai", "Stimme", model, seconds, "Sekunden", seconds / 60 * OPENAI_TTS_USD_PER_MIN)


def track_elevenlabs(chars, model):
    usd_per_1k = float(config.get("costs.elevenlabs_usd_per_1k_chars", 0) or 0)
    record("elevenlabs", "Stimme", model, chars, "Zeichen", chars / 1000 * usd_per_1k)


# ------------------------------------------------------------------ Auswertung
def _eur(usd):
    return usd * float(config.get("costs.eur_per_usd", 0.86))


def _fmt_eur(eur):
    if eur < 0.01:
        return "unter 1 Cent" if eur > 0 else "0 Cent"
    if eur < 1:
        return f"{round(eur * 100)} Cent"
    return f"{eur:.2f} Euro".replace(".", ",")


def _month_start(now=None):
    now = now or datetime.datetime.now()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


def summary():
    now = datetime.datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    month = _month_start(now)
    prev = _month_start(now.replace(day=1) - datetime.timedelta(days=1))

    def total(since, until=None):
        row = db.one("SELECT COALESCE(SUM(usd),0) AS usd, COUNT(*) AS n FROM costs WHERE ts>=? AND ts<?",
                     (since, until or time.time() + 1))
        return _eur(row["usd"]), row["n"]

    by_kind = db.query("""SELECT service, kind, COUNT(*) AS n, SUM(units) AS units, unit, SUM(usd) AS usd
                          FROM costs WHERE ts>=? GROUP BY service, kind, unit ORDER BY usd DESC, n DESC""", (month,))
    # „Chat + 2× Websuche“ und „Chat“ zusammenfassen
    merged = {}
    for r in by_kind:
        key = (r["service"], re.sub(r" \+ \d+× Websuche$", "", r["kind"]), r["unit"])
        m = merged.setdefault(key, {"service": SERVICE_NAMES.get(r["service"], r["service"]), "kind": key[1],
                                    "n": 0, "units": 0.0, "unit": r["unit"], "eur": 0.0})
        m["n"] += r["n"]
        m["units"] += r["units"] or 0
        m["eur"] += _eur(r["usd"] or 0)
    days = db.query("""SELECT date(ts, 'unixepoch', 'localtime') AS day, SUM(usd) AS usd FROM costs
                       WHERE ts>=? GROUP BY day ORDER BY day""", (time.time() - 30 * 86400,))
    el_chars = db.one("SELECT COALESCE(SUM(units),0) AS c FROM costs WHERE service='elevenlabs' AND ts>=?", (month,))["c"]
    first = db.one("SELECT MIN(ts) AS t FROM costs")["t"]
    t_eur, t_n = total(today)
    m_eur, m_n = total(month)
    p_eur, _ = total(prev, month)
    a_eur, a_n = total(0)
    return {
        "today": t_eur, "today_n": t_n, "month": m_eur, "month_n": m_n, "prev_month": p_eur,
        "all": a_eur, "all_n": a_n, "since": first,
        "by_kind": sorted(merged.values(), key=lambda x: (-x["eur"], -x["n"])),
        "days": [{"day": d["day"], "eur": _eur(d["usd"] or 0)} for d in days],
        "elevenlabs_chars": int(el_chars), "elevenlabs_free": int(config.get("costs.elevenlabs_free_chars", 10000)),
    }


def spoken_summary():
    s = summary()
    if not s["all_n"]:
        return "Bisher hast du über mich noch nichts für KI-Dienste ausgegeben."
    parts = [f"Diesen Monat hast du ungefähr {_fmt_eur(s['month'])} für KI ausgegeben, heute {_fmt_eur(s['today'])}."]
    if s["prev_month"] > 0:
        parts.append(f"Letzten Monat waren es {_fmt_eur(s['prev_month'])}.")
    top = [k for k in s["by_kind"] if k["eur"] >= 0.005][:2]
    if top:
        parts.append("Am meisten kostet " + " und ".join(f"{k['service']} {k['kind']} mit {_fmt_eur(k['eur'])}" for k in top) + ".")
    if s["elevenlabs_chars"]:
        free = s["elevenlabs_free"]
        parts.append(f"Von ElevenLabs hast du diesen Monat {s['elevenlabs_chars']:,} von {free:,} Gratis-Zeichen verbraucht.".replace(",", "."))
    parts.append(f"Insgesamt seit Beginn: {_fmt_eur(s['all'])}. Das sind Richtwerte, die genaue Abrechnung steht bei OpenAI.")
    return " ".join(parts)


@action("costs_report", "Sagt, wie viel der Nutzer für KI-Dienste (OpenAI, ElevenLabs) ausgegeben hat – heute, diesen Monat, insgesamt",
        {}, risk=0, module=M)
def costs_report():
    return spoken_summary()


class CostsModule(Module):
    name = "costs"
    title = "KI-Kosten"

    def parse(self, text, n, ctx):
        if re.search(r"(was|wie ?viel) (habe ich|hab ich|hast du|haben wir)\b.*\b(ausgegeben|verbraucht|gekostet|bezahlt)"
                     r"|\b(ki|meine|die|deine|jarvis|openai|elevenlabs)[- ]?kosten\b|kostenübersicht|was kostest du"
                     r"|wie teuer (bist du|warst du|ist die ki)", n):
            return Intent([("costs_report", {})])
        return None

    def status(self):
        s = summary()
        return {"month_eur": round(s["month"], 4), "today_eur": round(s["today"], 4)}
