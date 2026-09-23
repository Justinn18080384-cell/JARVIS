"""Finanzen im Blick: Ausgaben, Einnahmen, Budgets und Abos – alles nur lokal auf diesem PC.

Eingabe per Sprache („Ich habe 12,50 Euro für Essen ausgegeben“), in der Oberfläche, am Handy
oder als Kontoauszug (PDF oder CSV) aus dem Online-Banking (Datei herunterladen und importieren).
JARVIS meldet sich mit keiner Bank an, überweist nichts und gibt keine Anlageberatung.

Beträge: negativ = Ausgabe, positiv = Einnahme.
"""
import csv
import datetime
import hashlib
import io
import re
import threading
import time

from ..core import log
from ..core.actions import action, S, N
from ..core.db import db
from ..core.events import bus
from ..core.module import Module, Intent

M = "finanzen"
_mod = None
_lock = threading.RLock()

db.schema("""CREATE TABLE IF NOT EXISTS fin_tx(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT, amount REAL, category TEXT, note TEXT, source TEXT, hash TEXT UNIQUE, created REAL
)""")
db.schema("""CREATE TABLE IF NOT EXISTS fin_budget(category TEXT PRIMARY KEY, monthly REAL)""")
db.schema("""CREATE TABLE IF NOT EXISTS fin_sub(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT, amount REAL, interval TEXT, next_due TEXT, category TEXT, active INTEGER DEFAULT 1
)""")

INCOME = "Einnahmen"
OTHER = "Sonstiges"
# Kategorie: Stichwörter (Händler, Verwendungszweck, gesprochene Wörter)
CATEGORIES = {
    "Essen & Trinken": ["essen", "lebensmittel", "einkauf", "einkaufen", "rewe", "edeka", "lidl", "aldi", "netto", "penny", "kaufland",
                        "norma", "restaurant", "mcdonald", "burger", "lieferando", "wolt", "bäcker", "baecker", "döner", "doener",
                        "pizza", "kebab", "imbiss", "café", "cafe", "kaffee", "getränke", "getraenke", "trinken", "snack", "kfc",
                        "subway", "starbucks", "mensa", "kantine", "supermarkt"],
    "Auto & Tanken": ["tanken", "tankstelle", "benzin", "diesel", "sprit", "aral", "shell", "esso", "jet ", "total", "avia", "agip",
                      "werkstatt", "tüv", "tuev", "kfz", "auto", "parken", "parkhaus", "waschanlage", "reifen"],
    "Wohnen": ["miete", "strom", "gas", "stadtwerke", "nebenkosten", "wasser", "heizung", "rundfunk", "gez", "möbel", "moebel", "ikea"],
    "Abos & Streaming": ["abo", "netflix", "spotify", "disney", "prime", "youtube premium", "dazn", "sky", "apple.com", "icloud",
                         "chatgpt", "openai", "elevenlabs", "wow", "crunchyroll", "audible"],
    "Gaming": ["steam", "epic games", "playstation", "psn", "xbox", "nintendo", "fivem", "tebex", "riot", "blizzard", "ea.com",
               "ubisoft", "rockstar", "gaming", "spiel", "game", "twitch", "discord nitro", "g2a", "instant gaming", "mmoga"],
    "Shopping": ["amazon", "zalando", "otto", "ebay", "mediamarkt", "saturn", "kleidung", "klamotten", "schuhe", "h&m", "primark",
                 "about you", "shein", "temu", "dm ", "rossmann", "müller", "mueller", "shopping", "paypal"],
    "Handy & Internet": ["telekom", "vodafone", "o2", "telefonica", "1&1", "congstar", "handy", "internet", "dsl", "mobilfunk"],
    "Versicherung": ["versicherung", "allianz", "huk", "axa", "ergo", "devk", "haftpflicht", "krankenkasse"],
    "Freizeit": ["kino", "fitness", "mcfit", "fitx", "clever fit", "freizeit", "konzert", "ticket", "eventim", "bar", "club",
                 "urlaub", "hotel", "friseur", "geschenk"],
    "Gesundheit": ["apotheke", "arzt", "zahnarzt", "medikament", "optiker", "gesundheit"],
    "Unterwegs": ["bahn", "db vertrieb", "deutschlandticket", "bus", "uber", "flixbus", "taxi", "bolt", "öpnv", "oepnv", "zug"],
    INCOME: ["gehalt", "lohn", "ausbildungsvergütung", "ausbildungsverguetung", "kindergeld", "bafög", "bafoeg", "rente",
             "taschengeld", "rückerstattung", "rueckerstattung", "erstattung", "einnahme", "bekommen", "verdient"],
}
ALL_CATEGORIES = list(CATEGORIES) + [OTHER]
INTERVALS = {"monat": 1, "quartal": 3, "halbjahr": 6, "jahr": 12, "woche": 0}


# ------------------------------------------------------------------ Hilfen
def eur(x):
    """1234.5 -> „1.234,50 €“"""
    s = f"{abs(x):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return ("-" if x < 0 else "") + s + " €"


def say_eur(x):
    x = abs(x)
    euro, cent = int(x), round((x - int(x)) * 100)
    if cent == 100:
        euro, cent = euro + 1, 0
    if not euro:
        return f"{cent} Cent" if cent else "0 Euro"
    return f"{euro} Euro" + (f" {cent}" if cent else "")


def guess_category(text, income=False):
    t = " " + (text or "").lower() + " "
    if income:
        return INCOME
    for cat, words in CATEGORIES.items():
        if cat == INCOME:
            continue
        if any(w in t for w in words):
            return cat
    return OTHER


def match_category(name):
    """Freie Eingabe („essen“, „gaming“) auf eine bekannte Kategorie abbilden."""
    if not name:
        return None
    n = name.lower().strip()
    for cat in ALL_CATEGORIES:
        if n == cat.lower() or n in cat.lower().replace("&", " ").split():
            return cat
    g = guess_category(n)
    return g if g != OTHER else (OTHER if n in ("sonstiges", "sonstige", "anderes") else None)


AMOUNT = re.compile(r"(\d{1,7}(?:[.,]\d{1,2})?)\s*(?:€|euro|eur|euros)(?:\s*(?:und\s*)?(\d{1,2})(?:\s*cent)?\b)?")


def parse_amount(text):
    m = AMOUNT.search(text)
    if not m:
        return None
    val = float(m.group(1).replace(",", "."))
    if m.group(2) and not re.search(r"[.,]", m.group(1)):
        val += int(m.group(2)) / 100
    return round(val, 2)


def _today():
    return datetime.date.today()


def _month_bounds(d=None, offset=0):
    d = d or _today()
    y, m = d.year, d.month + offset
    while m < 1:
        y, m = y - 1, m + 12
    while m > 12:
        y, m = y + 1, m - 12
    start = datetime.date(y, m, 1)
    end = datetime.date(y + (m == 12), m % 12 + 1, 1)
    return start.isoformat(), end.isoformat()


MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"]


def _add_months(d, n):
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    import calendar
    return datetime.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _next_due(d, interval):
    if interval == "woche":
        return d + datetime.timedelta(days=7)
    return _add_months(d, INTERVALS.get(interval, 1))


# ------------------------------------------------------------------ Buchungen
def add_tx(amount, category=None, note="", day=None, source="manuell", tx_hash=None):
    amount = round(float(amount), 2)
    if not amount:
        raise ValueError("Betrag fehlt")
    cat = category if category in ALL_CATEGORIES else (match_category(category) or guess_category(note, amount > 0))
    if amount > 0 and cat != INCOME and source != "csv":
        cat = INCOME
    day = day or _today().isoformat()
    with _lock:
        try:
            cur = db.execute("INSERT INTO fin_tx(day, amount, category, note, source, hash, created) VALUES(?,?,?,?,?,?,?)",
                             (day, amount, cat, (note or "").strip()[:200], source, tx_hash, time.time()))
        except Exception as e:
            if "UNIQUE" in str(e):
                return None     # bereits importiert
            raise
    tid = getattr(cur, "lastrowid", None)
    bus.emit("finance_changed")
    if amount < 0:
        _check_budget(cat)
    return {"id": tid, "day": day, "amount": amount, "category": cat, "note": note}


def undo_last():
    r = db.one("SELECT * FROM fin_tx WHERE source!='csv' ORDER BY id DESC LIMIT 1")
    if not r:
        return None
    db.execute("DELETE FROM fin_tx WHERE id=?", (r["id"],))
    bus.emit("finance_changed")
    return r


def delete_tx(tid):
    db.execute("DELETE FROM fin_tx WHERE id=?", (int(tid),))
    bus.emit("finance_changed")


def update_tx(tid, category=None, note=None):
    if category in ALL_CATEGORIES:
        db.execute("UPDATE fin_tx SET category=? WHERE id=?", (category, int(tid)))
    if note is not None:
        db.execute("UPDATE fin_tx SET note=? WHERE id=?", (str(note)[:200], int(tid)))
    bus.emit("finance_changed")


def spent(start, end, category=None):
    sql = "SELECT COALESCE(SUM(amount),0) AS s FROM fin_tx WHERE day>=? AND day<? AND amount<0"
    args = [start, end]
    if category:
        sql += " AND category=?"
        args.append(category)
    return -db.one(sql, args)["s"]


def earned(start, end):
    return db.one("SELECT COALESCE(SUM(amount),0) AS s FROM fin_tx WHERE day>=? AND day<? AND amount>0", (start, end))["s"]


# ------------------------------------------------------------------ Budgets
def set_budget(category, monthly):
    cat = match_category(category) or category
    if cat not in ALL_CATEGORIES:
        cat = "Gesamt" if (category or "").lower() in ("gesamt", "insgesamt", "alles", "monat") else None
    if not cat:
        return None
    if monthly and monthly > 0:
        db.execute("INSERT OR REPLACE INTO fin_budget(category, monthly) VALUES(?,?)", (cat, round(float(monthly), 2)))
    else:
        db.execute("DELETE FROM fin_budget WHERE category=?", (cat,))
    bus.emit("finance_changed")
    return cat


def budgets():
    start, end = _month_bounds()
    out = []
    for b in db.query("SELECT * FROM fin_budget ORDER BY category"):
        used = spent(start, end, None if b["category"] == "Gesamt" else b["category"])
        out.append({"category": b["category"], "monthly": b["monthly"], "used": round(used, 2),
                    "pct": round(used / b["monthly"] * 100) if b["monthly"] else 0})
    return out


def _check_budget(cat):
    """Warnt einmal pro Monat bei 80 % und bei 100 % eines Budgets."""
    try:
        month = _today().strftime("%Y-%m")
        for b in budgets():
            if b["category"] not in (cat, "Gesamt"):
                continue
            for level in (100, 80):
                if b["pct"] >= level:
                    key = f"fin.warn.{b['category']}.{month}.{level}"
                    if not db.kv_get(key):
                        db.kv_set(key, "1")
                        name = "Dein Gesamtbudget" if b["category"] == "Gesamt" else f"Dein Budget für {b['category']}"
                        txt = (f"{name} ist aufgebraucht: {say_eur(b['used'])} von {say_eur(b['monthly'])}." if level == 100 else
                               f"{name} ist zu {b['pct']} Prozent verbraucht: {say_eur(b['used'])} von {say_eur(b['monthly'])}.")
                        bus.emit("notify", title="Finanzen", text=txt, speak=True, priority=1)
                    break
    except Exception as e:
        log.error("Budget prüfen", e)


# ------------------------------------------------------------------ Abos
def add_sub(name, amount, interval="monat", next_due=None, category=None):
    interval = interval if interval in INTERVALS else "monat"
    name = (name or "").strip()[:60]
    if not name:
        raise ValueError("Name fehlt")
    cat = match_category(category) if category else None
    cat = cat or guess_category(name)
    if cat == OTHER:
        cat = "Abos & Streaming"
    due = next_due or _next_due(_today(), interval).isoformat()
    old = _find_sub(name)
    if old:
        db.execute("UPDATE fin_sub SET amount=?, interval=?, category=?, active=1 WHERE id=?", (abs(float(amount)), interval, cat, old["id"]))
    else:
        db.execute("INSERT INTO fin_sub(name, amount, interval, next_due, category) VALUES(?,?,?,?,?)",
                   (name, abs(float(amount)), interval, due, cat))
    bus.emit("finance_changed")
    return _find_sub(name)


def _find_sub(name):
    from ..core.text import best_match
    subs = db.query("SELECT * FROM fin_sub WHERE active=1")
    hit, _ = best_match(name, subs, key=lambda s: s["name"], threshold=0.6)
    return hit


def remove_sub(name_or_id):
    s = db.one("SELECT * FROM fin_sub WHERE id=?", (name_or_id,)) if str(name_or_id).isdigit() else _find_sub(str(name_or_id))
    if not s:
        return None
    db.execute("UPDATE fin_sub SET active=0 WHERE id=?", (s["id"],))
    bus.emit("finance_changed")
    return s


def subs():
    return db.query("SELECT * FROM fin_sub WHERE active=1 ORDER BY next_due")


def sub_monthly(s):
    return {"woche": s["amount"] * 52 / 12}.get(s["interval"], s["amount"] / max(1, INTERVALS.get(s["interval"], 1)))


def process_subs():
    """Fällige Abos als Ausgabe eintragen und einen Tag vorher erinnern."""
    today = _today()
    for s in subs():
        try:
            due = datetime.date.fromisoformat(s["next_due"])
        except Exception:
            continue
        if due == today + datetime.timedelta(days=1):
            key = f"fin.subremind.{s['id']}.{s['next_due']}"
            if not db.kv_get(key):
                db.kv_set(key, "1")
                bus.emit("notify", title="Abo", text=f"Morgen wird {s['name']} mit {say_eur(s['amount'])} abgebucht.",
                         speak=False, priority=0)
        n = 0
        while due <= today and n < 24:
            add_tx(-s["amount"], s["category"], f"Abo: {s['name']}", day=due.isoformat(), source="abo",
                   tx_hash=f"abo-{s['id']}-{due.isoformat()}")
            due = _next_due(due, s["interval"])
            n += 1
        if n:
            db.execute("UPDATE fin_sub SET next_due=? WHERE id=?", (due.isoformat(), s["id"]))


# ------------------------------------------------------------------ CSV-Import
def _num(s):
    s = (s or "").strip().replace("€", "").replace("EUR", "").replace("\xa0", "").replace(" ", "")
    if not s:
        return None
    if re.search(r",\d{1,2}$", s):          # deutsch: 1.234,56
        s = s.replace(".", "").replace(",", ".")
    else:                                    # englisch: 1,234.56
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _date(s):
    s = (s or "").strip()
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.datetime.strptime(s[:10], fmt).date().isoformat()
        except ValueError:
            pass
    return None


def import_csv(data: bytes):
    """Kontoauszug im CSV-Format (Sparkasse, Volksbank, DKB, ING, N26, Comdirect, PayPal …) einlesen."""
    text = None
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    lines = text.splitlines()
    # Kopfzeile suchen (manche Banken schreiben vorher Kontoinfos)
    head_i = next((i for i, l in enumerate(lines[:40])
                   if re.search(r"(buchungstag|buchungsdatum|datum|date|valuta)", l, re.I) and re.search(r"(betrag|amount|umsatz|soll|haben)", l, re.I)), None)
    if head_i is None:
        raise ValueError("Keine Spaltenüberschriften (Datum/Betrag) gefunden – ist das ein Kontoauszug als CSV?")
    body = "\n".join(lines[head_i:])
    delim = max([";", ",", "\t"], key=lambda d: lines[head_i].count(d))
    rows = list(csv.reader(io.StringIO(body), delimiter=delim))
    head = [h.strip().lower() for h in rows[0]]

    def col(*names, avoid=()):
        for n in names:
            for i, h in enumerate(head):
                if n in h and not any(a in h for a in avoid):
                    return i
        return None

    c_date = col("buchungstag", "buchungsdatum", "datum", "date", "valuta")
    c_amount = col("betrag (€)", "betrag (eur)", "betrag", "amount", "umsatz", avoid=("ursprung", "fremd", "gebühr"))
    c_soll, c_haben = col("soll"), col("haben")
    c_who = col("beguenstigter", "begünstigter", "empfänger", "empfaenger", "zahlungspflichtige", "auftraggeber", "name", "payee", "gegenkonto name")
    c_what = col("verwendungszweck", "buchungstext", "beschreibung", "description", "zweck", "text")
    if c_date is None or (c_amount is None and c_soll is None):
        raise ValueError("Spalten für Datum oder Betrag nicht erkannt.")
    added = skipped = 0
    for r in rows[1:]:
        if len(r) <= max(x for x in (c_date, c_amount, c_soll, c_haben, c_who, c_what) if x is not None):
            continue
        day = _date(r[c_date])
        if c_amount is not None:
            amt = _num(r[c_amount])
        else:
            amt = (_num(r[c_haben]) or 0) - abs(_num(r[c_soll]) or 0)
        if not day or not amt:
            continue
        who = r[c_who].strip() if c_who is not None else ""
        what = r[c_what].strip() if c_what is not None else ""
        note = re.sub(r"\s+", " ", " – ".join(x for x in (who, what) if x))[:200]
        h = hashlib.sha1(f"{day}|{amt:.2f}|{note}".encode()).hexdigest()
        cat = INCOME if amt > 0 and re.search(r"gehalt|lohn|vergütung|verguetung|kindergeld|bafög|rente", note, re.I) else \
            (guess_category(note) if amt < 0 else INCOME)
        if add_tx(amt, cat, note, day=day, source="csv", tx_hash=h):
            added += 1
        else:
            skipped += 1
    log.activity(M, f"Kontoauszug importiert: {added} neue Buchungen, {skipped} schon vorhanden")
    return {"added": added, "skipped": skipped}


# ------------------------------------------------------------------ PDF-Import
_PDF_DATE = re.compile(r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{2}|\d{4})?\s")
_PDF_AMOUNT = re.compile(r"(?<![\d.,])([+-]?)\s?(\d{1,3}(?:\.\d{3})*,\d{2})\s*(€|EUR)?\s*([+-]|S|H)?\s*$")
_PDF_SKIP = re.compile(r"kontostand|saldo|übertrag|uebertrag|summe|zwischensumme|gesamtumsatz|dispositionskredit|zinssatz|seite \d", re.I)
_PDF_INCOME = re.compile(r"gutschrift|gehalt|lohn|eingang|zahlungseingang|überweisungseingang|rückzahlung|erstattung|bafög|kindergeld|rente", re.I)
_PDF_DATE_ONLY = re.compile(r"^\s*\d{1,2}\.\d{1,2}\.(\d{2,4})?\s*")


def pdf_text(data: bytes) -> str:
    import pypdf
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
    except Exception:
        raise ValueError("Das PDF ist beschädigt oder unvollständig – bitte nochmal herunterladen.")
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            raise ValueError("Das PDF ist mit einem Passwort geschützt. Bitte ungeschützt aus dem Online-Banking herunterladen.")
    pages = []
    for p in reader.pages:
        try:
            pages.append(p.extract_text(extraction_mode="layout") or "")
        except Exception:
            pages.append(p.extract_text() or "")
    return "\n".join(pages)


def parse_pdf_statement(text: str):
    """Buchungen aus dem Text eines Kontoauszugs: Zeilen mit Datum vorne und Betrag hinten,
    Folgezeilen ohne Datum gehören zum Verwendungszweck. Liefert [(Tag, Betrag, Text)]."""
    years = re.findall(r"\b(20\d{2})\b", text)
    default_year = int(max(set(years), key=years.count)) if years else _today().year
    out, cur = [], None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        dm = _PDF_DATE.match(line)
        am = _PDF_AMOUNT.search(line)
        if dm and am and not _PDF_SKIP.search(line):
            d, mth, y = int(dm.group(1)), int(dm.group(2)), dm.group(3)
            year = default_year if not y else (int(y) + 2000 if len(y) == 2 else int(y))
            try:
                day = datetime.date(year, mth, d).isoformat()
            except ValueError:
                cur = None
                continue
            desc = line[:am.start()]
            while _PDF_DATE_ONLY.match(desc):          # Buchungs- und Valutadatum abschneiden
                desc = _PDF_DATE_ONLY.sub("", desc, count=1)
            val = float(am.group(2).replace(".", "").replace(",", "."))
            sign = am.group(1) or am.group(4) or ""
            if sign in ("-", "S"):
                val = -val
            elif sign not in ("+", "H"):
                val = val if _PDF_INCOME.search(desc) else -val    # ohne Vorzeichen: Gutschrift erkennen, sonst Ausgabe
            cur = [day, val, re.sub(r"\s{2,}", " ", desc).strip()]
            out.append(cur)
        elif cur is not None and not dm and not am and not _PDF_SKIP.search(line) and len(cur[2]) < 160:
            extra = re.sub(r"\s{2,}", " ", line).strip()
            if extra:
                cur[2] = (cur[2] + " – " + extra).strip(" –")
        elif dm or _PDF_SKIP.search(line):
            cur = None
    return [tuple(x) for x in out]


def import_pdf(data: bytes):
    """Kontoauszug als PDF (Sparkasse, Volksbank, DKB, ING, Commerzbank, N26 …) einlesen."""
    text = pdf_text(data)
    if len(text.strip()) < 30:
        raise ValueError("In diesem PDF ist kein Text – vermutlich ein eingescanntes Bild. Bitte den Auszug direkt aus dem Online-Banking herunterladen.")
    rows = parse_pdf_statement(text)
    if not rows:
        raise ValueError("Ich habe in diesem PDF keine Buchungen gefunden. Falls möglich, lade die Umsätze als CSV herunter.")
    added = skipped = 0
    for day, amt, note in rows:
        note = note[:200]
        h = hashlib.sha1(f"{day}|{amt:.2f}|{note}".encode()).hexdigest()
        cat = INCOME if amt > 0 else guess_category(note)
        if add_tx(amt, cat, note, day=day, source="csv", tx_hash=h):
            added += 1
        else:
            skipped += 1
    log.activity(M, f"Kontoauszug (PDF) importiert: {added} neue Buchungen, {skipped} schon vorhanden")
    return {"added": added, "skipped": skipped}


def import_statement(data: bytes):
    """CSV oder PDF – am Dateiinhalt erkannt."""
    return import_pdf(data) if data[:5] == b"%PDF-" else import_csv(data)


# ------------------------------------------------------------------ Übersicht
def overview():
    today = _today()
    start, end = _month_bounds()
    p_start, p_end = _month_bounds(offset=-1)
    by_cat = db.query("""SELECT category, -SUM(amount) AS s, COUNT(*) AS n FROM fin_tx
                         WHERE day>=? AND day<? AND amount<0 GROUP BY category ORDER BY s DESC""", (start, end))
    months = []
    for off in range(-5, 1):
        a, b = _month_bounds(offset=off)
        months.append({"month": MONTHS[int(a[5:7]) - 1][:3], "spent": round(spent(a, b), 2), "earned": round(earned(a, b), 2)})
    days_in = (datetime.date.fromisoformat(end) - datetime.date.fromisoformat(start)).days
    month_spent = spent(start, end)
    ss = subs()
    return {
        "month_name": MONTHS[today.month - 1],
        "month_spent": round(month_spent, 2), "month_earned": round(earned(start, end), 2),
        "prev_spent": round(spent(p_start, p_end), 2),
        "today_spent": round(spent(today.isoformat(), (today + datetime.timedelta(days=1)).isoformat()), 2),
        "forecast": round(month_spent / today.day * days_in, 2) if today.day >= 5 else None,
        "by_category": [{"category": r["category"], "sum": round(r["s"], 2), "n": r["n"]} for r in by_cat],
        "months": months,
        "budgets": budgets(),
        "subs": [{**s, "monthly": round(sub_monthly(s), 2)} for s in ss],
        "subs_monthly": round(sum(sub_monthly(s) for s in ss), 2),
        "recent": db.query("SELECT * FROM fin_tx ORDER BY day DESC, id DESC LIMIT 40"),
        "categories": ALL_CATEGORIES,
        "count": db.one("SELECT COUNT(*) AS n FROM fin_tx")["n"],
    }


def report(period="monat", category=None):
    cat = match_category(category) if category else None
    today = _today()
    if period in ("heute", "tag"):
        a, b, label = today.isoformat(), (today + datetime.timedelta(days=1)).isoformat(), "heute"
    elif period in ("woche", "diese woche"):
        mon = today - datetime.timedelta(days=today.weekday())
        a, b, label = mon.isoformat(), (today + datetime.timedelta(days=1)).isoformat(), "diese Woche"
    elif period in ("letzter monat", "vormonat", "letzten monat"):
        a, b = _month_bounds(offset=-1)
        label = f"im {MONTHS[int(a[5:7]) - 1]}"
    elif period in ("jahr", "dieses jahr"):
        a, b, label = f"{today.year}-01-01", f"{today.year + 1}-01-01", f"dieses Jahr"
    else:
        a, b = _month_bounds()
        label = "diesen Monat"
    if not db.one("SELECT COUNT(*) AS n FROM fin_tx")["n"]:
        return ("Ich habe noch keine Ausgaben von dir gespeichert. Sag zum Beispiel „Ich habe 12 Euro für Essen ausgegeben“ "
                "oder importiere einen Kontoauszug als CSV auf der Finanzen-Seite.")
    if cat:
        s = spent(a, b, cat)
        txt = f"Für {cat} hast du {label} {say_eur(s)} ausgegeben."
        bud = next((x for x in budgets() if x["category"] == cat), None)
        if bud and period not in ("heute", "tag", "woche", "diese woche", "jahr", "dieses jahr", "letzter monat", "vormonat", "letzten monat"):
            txt += f" Dein Budget ist {say_eur(bud['monthly'])}, also noch {say_eur(max(0, bud['monthly'] - s))} übrig." if s <= bud["monthly"] \
                else f" Damit bist du {say_eur(s - bud['monthly'])} über deinem Budget."
        return txt
    s, e = spent(a, b), earned(a, b)
    txt = f"Du hast {label} {say_eur(s)} ausgegeben"
    txt += f" und {say_eur(e)} eingenommen." if e else "."
    top = db.query("""SELECT category, -SUM(amount) AS s FROM fin_tx WHERE day>=? AND day<? AND amount<0
                      GROUP BY category ORDER BY s DESC LIMIT 2""", (a, b))
    if top and s:
        txt += " Am meisten für " + " und ".join(f"{t['category']} mit {say_eur(t['s'])}" for t in top) + "."
    if label == "diesen Monat":
        over = [x for x in budgets() if x["pct"] >= 100]
        if over:
            txt += " Überschritten: " + ", ".join(x["category"] for x in over) + "."
        ss = subs()
        if ss:
            txt += f" Deine {len(ss)} Abos kosten zusammen etwa {say_eur(sum(sub_monthly(x) for x in ss))} im Monat."
    return txt


# ------------------------------------------------------------------ Aktionen
@action("finance_add", "Trägt eine Ausgabe oder Einnahme des Nutzers in sein Haushaltsbuch ein (nur lokal gespeichert)",
        {"amount": N("Betrag in Euro, immer positiv"), "kind": S("Ausgabe oder Einnahme", ["ausgabe", "einnahme"]),
         "category": S("Kategorie", ALL_CATEGORIES), "note": S("Wofür / wo (z. B. „Rewe“, „Tanken“)")},
        required=["amount"], risk=0, module=M)
def finance_add(amount, kind="ausgabe", category=None, note=""):
    val = abs(float(amount)) * (1 if kind == "einnahme" else -1)
    tx = add_tx(val, category, note, source="sprache")
    if val > 0:
        return f"Eingetragen: {say_eur(val)} Einnahme{(' – ' + note) if note else ''}."
    start, end = _month_bounds()
    month = spent(start, end, tx["category"])
    return f"Eingetragen: {say_eur(val)} für {tx['category']}. Diesen Monat damit {say_eur(month)} in dieser Kategorie."


@action("finance_report", "Sagt, wie viel der Nutzer ausgegeben/eingenommen hat (Haushaltsbuch) – optional für eine Kategorie",
        {"period": S("Zeitraum", ["heute", "woche", "monat", "letzter monat", "jahr"]), "category": S("Kategorie (optional)")},
        required=[], risk=0, module=M)
def finance_report(period="monat", category=None):
    return report(period, category)


@action("finance_budget", "Setzt ein monatliches Budget für eine Kategorie (oder „Gesamt“); Betrag 0 entfernt es",
        {"category": S("Kategorie oder „Gesamt“"), "amount": N("Monatsbudget in Euro")}, risk=0, module=M)
def finance_budget(category, amount):
    cat = set_budget(category, float(amount))
    if not cat:
        return f"Die Kategorie „{category}“ kenne ich nicht. Möglich sind: " + ", ".join(ALL_CATEGORIES) + " oder Gesamt."
    if not amount:
        return f"Das Budget für {cat} ist entfernt."
    return f"Budget gesetzt: {say_eur(float(amount))} im Monat " + ("insgesamt." if cat == "Gesamt" else f"für {cat}.") + f" Ich sage Bescheid, wenn 80 Prozent erreicht sind."


@action("finance_sub_add", "Merkt sich ein Abo/einen Vertrag mit regelmäßiger Abbuchung (z. B. Netflix 13,99 im Monat)",
        {"name": S("Name des Abos"), "amount": N("Betrag in Euro"), "interval": S("Abrechnung", list(INTERVALS))},
        required=["name", "amount"], risk=0, module=M)
def finance_sub_add(name, amount, interval="monat"):
    s = add_sub(name, amount, interval)
    return (f"Abo gespeichert: {s['name']} mit {say_eur(s['amount'])} pro {s['interval'].capitalize()}. "
            f"Nächste Abbuchung am {datetime.date.fromisoformat(s['next_due']).strftime('%d.%m.')} – ich trage sie dann automatisch ein.")


@action("finance_sub_remove", "Entfernt ein Abo aus der Übersicht (kündigt es NICHT beim Anbieter)", {"name": S("Name des Abos")},
        risk=0, module=M)
def finance_sub_remove(name):
    s = remove_sub(name)
    if not s:
        return f"Ein Abo „{name}“ habe ich nicht gespeichert."
    return f"{s['name']} ist aus deiner Abo-Liste entfernt. Kündigen musst du es aber selbst beim Anbieter."


@action("finance_subs", "Listet die gespeicherten Abos und ihre monatlichen Kosten auf", {}, risk=0, module=M)
def finance_subs():
    ss = subs()
    if not ss:
        return "Du hast noch keine Abos gespeichert. Sag zum Beispiel „Neues Abo Netflix 13,99 Euro im Monat“."
    items = [f"{s['name']} {say_eur(s['amount'])}" + ("" if s["interval"] == "monat" else f" pro {s['interval'].capitalize()}") for s in ss]
    return f"Du hast {len(ss)} Abo{'s' if len(ss) != 1 else ''}: " + ", ".join(items) + f". Zusammen etwa {say_eur(sum(sub_monthly(s) for s in ss))} im Monat."


@action("finance_undo", "Löscht die zuletzt eingetragene Ausgabe/Einnahme", {}, risk=0, module=M)
def finance_undo():
    r = undo_last()
    if not r:
        return "Es gibt keine Buchung, die ich löschen könnte."
    return f"Gelöscht: {say_eur(r['amount'])} {r['category']}" + (f" ({r['note']})" if r["note"] else "") + "."


# ------------------------------------------------------------------ Modul
PERIODS = [(r"\bheute\b", "heute"), (r"\b(diese|der) woche\b", "woche"), (r"\b(letzten|letzter|vorigen|im letzten) monat\b|\bvormonat\b", "letzter monat"),
           (r"\b(dieses|im) jahr\b", "jahr")]
AI_WORDS = re.compile(r"\b(ki|openai|elevenlabs|jarvis|für dich|fuer dich|chatgpt|api)\b")


class FinanceModule(Module):
    name = "finance"
    title = "Finanzen"

    def __init__(self, jarvis):
        super().__init__(jarvis)
        global _mod
        _mod = self
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="finance").start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        self._stop.wait(20)
        while not self._stop.is_set():
            try:
                process_subs()
            except Exception as e:
                log.error("Abos verarbeiten", e)
            self._stop.wait(3600)

    def parse(self, text, n, ctx):
        if AI_WORDS.search(n):
            return None  # KI-Kosten beantwortet das Kosten-Modul
        amount = parse_amount(n)
        period = next((p for rx, p in PERIODS if re.search(rx, n)), "monat")
        # Abo
        m = re.search(r"^(?:mein |das )?([a-zäöüß0-9+ .&-]{2,30}?)[ -]?(?:abo|abonnement)\s+(?:für |mit |kostet |zu |von )?\d", n) or             re.search(r"(?:neues|neuer|füge|fuege|trag|speicher|merk dir)?\s*(?:ein |das |den )?(?:abo|abonnement|vertrag)\s+(?:für |von )?([a-zäöüß0-9+ .&-]+?)\s+(?:für |mit |kostet |zu )?\d", n)
        if amount and m and not re.search(r"(lösch|entfern|kündig)", n):
            name = re.sub(r"\b(hinzu|mit|für|von|kostet)\b", "", m.group(1)).strip()
            interval = "jahr" if re.search(r"jähr|jaehr|im jahr|pro jahr|jahres", n) else "woche" if re.search(r"woche", n) else \
                "quartal" if "quartal" in n else "monat"
            return Intent([("finance_sub_add", {"name": name.title() if name.islower() else name, "amount": amount, "interval": interval})])
        m = re.search(r"(?:lösch|loesch|entfern|kündig|kuendig)\w*\s+(?:das |mein )?abo\s+(?:von |für )?(.+)|(?:abo|abonnement)\s+(.+?)\s+(?:lösch|loesch|entfern|gekündigt|kuendig)", n)
        if m:
            return Intent([("finance_sub_remove", {"name": (m.group(1) or m.group(2)).strip()})])
        if re.search(r"\b(welche|meine|alle) (abos|abonnements|verträge)\b|abo[- ]?(liste|übersicht)|was (zahle|bezahle) ich (für abos|monatlich)", n):
            return Intent([("finance_subs", {})])
        # Budget
        m = re.search(r"budget (?:für |fuer )?(?:(?:die |das )?kategorie )?([a-zäöüß& ]+?) (?:auf |von |ist |=)?\s*\d", n)
        if amount is not None and m and "wie" not in n.split()[:2]:
            return Intent([("finance_budget", {"category": m.group(1).strip(), "amount": amount})])
        if amount is None and re.search(r"budget (?:für |fuer )?([a-zäöüß& ]+?) (?:löschen|entfernen|weg)", n):
            cat = re.search(r"budget (?:für |fuer )?([a-zäöüß& ]+?) (?:löschen|entfernen|weg)", n).group(1)
            return Intent([("finance_budget", {"category": cat.strip(), "amount": 0})])
        # Rückgängig
        if re.search(r"(letzte|zuletzt) (ausgabe|buchung|einnahme|eintrag)\w* (lösch|loesch|entfern|rückgängig|falsch)|(lösch|entfern)\w* (die )?letzte (ausgabe|buchung|einnahme)", n):
            return Intent([("finance_undo", {})])
        # Eintragen
        if amount:
            income = re.search(r"\b(bekommen|verdient|eingenommen|erhalten|einnahme|gehalt|lohn|überwiesen bekommen|gutgeschrieben)\b", n)
            spend = re.search(r"\b(ausgegeben|bezahlt|gezahlt|gekostet|kostet|kostete|verbraten|gekauft|ausgabe|trag|eintragen|notier)", n)
            if (income or spend) and not re.search(r"\bwie ?viel\b|\bwas\b.*\?$", n):
                m = re.search(r"(?:für|fuer|bei|beim|im|in der|an der|auf|von)\s+(?:den |die |das |dem |der |mein |meine |meinen )?([a-zäöüß0-9&' .-]{2,40}?)"
                              r"(?:\s+(?:ausgegeben|bezahlt|gezahlt|gekauft|eingenommen|bekommen|verdient|eintragen|notieren|ein|eingetragen)\b|$)", n)
                note = m.group(1).strip() if m else ""
                if not note:   # „1200 Euro Gehalt bekommen“
                    am = AMOUNT.search(n)
                    m = re.match(r"\s*([a-zäöüß &-]{3,30}?)\s+(?:ausgegeben|bezahlt|gezahlt|gekauft|eingenommen|bekommen|verdient|erhalten)", n[am.end():])
                    note = m.group(1).strip() if m else ""
                if not note:
                    m = re.search(r"^(?:der |die |das |mein |meine )?([a-zäöüß &-]{2,30}?)\s+(?:hat|haben|hatte|kostet|kostete)", n)
                    note = m.group(1).strip() if m else ""
                note = re.sub(r"\b(heute|gestern|euro|€|\d+[.,]?\d*)\b", "", note).strip()
                args = {"amount": amount, "kind": "einnahme" if income and not spend else "ausgabe", "note": note.capitalize()}
                cat = match_category(note) if note else None
                if cat:
                    args["category"] = cat
                return Intent([("finance_add", args)])
        # Auswertung
        if re.search(r"(wie ?viel|was) (habe|hab) ich\b.*\b(ausgegeben|bezahlt|verbraucht|eingenommen|verdient)"
                     r"|\b(meine|mein) (finanzen|ausgaben|haushaltsbuch|geld|budget|budgets|konto)\b|finanz(übersicht|uebersicht|stand|bericht)"
                     r"|\bwie (steht|stehen|sieht) (es mit )?(meinen?|meine) (finanzen|budget|budgets|ausgaben|geld)", n):
            cat = None
            m = re.search(r"(?:für|fuer) (?:den |die |das )?([a-zäöüß& ]+?) (?:ausgegeben|bezahlt)", n)
            if m:
                cat = match_category(m.group(1).strip())
            args = {"period": period}
            if cat:
                args["category"] = cat
            return Intent([("finance_report", args)])
        return None

    def status(self):
        start, end = _month_bounds()
        return {"month_spent": round(spent(start, end), 2)}
