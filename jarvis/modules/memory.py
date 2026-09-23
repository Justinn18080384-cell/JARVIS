"""Dauerhaftes, lokales Gedächtnis. Verbraucht keine API-Anfragen.

Versteht u. a.:
    „Mein Lieblingsspiel ist GTA“          -> speichert
    „Merk dir, dass ich Pizza mag“          -> speichert als Notiz
    „Was ist mein Lieblingsspiel?“          -> fragt ab
    „Vergiss mein Lieblingsspiel“           -> löscht
    „Ändere mein Lieblingsspiel auf RDR2“   -> ändert
    „Was weißt du über mich?“               -> listet
"""
import re
import time

from ..core.actions import action, S
from ..core.db import db
from ..core.module import Module, Intent
from ..core.text import ascii_fold, best_match

M = "gedächtnis"

# Wörter, bei denen "mein X ist Y" KEINE Fakten-Aussage ist
NOT_FACT_SUBJECTS = {"pc", "computer", "rechner", "bildschirm", "handy", "akku", "internet", "mikrofon", "ton"}


def _key(label: str) -> str:
    return ascii_fold(label).replace(" ", "_")


def _clean(s: str) -> str:
    return s.strip(" .,!?\"'„“")


def _pronoun(label: str) -> str:
    """‚Lieblingsspiel‘ -> ‚Dein Lieblingsspiel‘ (grob nach Genus)."""
    l = label.lower()
    fem = ("farbe", "stadt", "band", "serie", "nummer", "adresse", "freundin", "frau", "mutter",
           "schwester", "musik", "marke", "sprache", "tochter", "katze", "lieblingsfarbe")
    if l.endswith(fem) or l.endswith(("ung", "heit", "keit", "e")) and not l.endswith(("name", "käse")):
        return "Deine"
    return "Dein"


def remember(label: str, value: str, category: str = "allgemein") -> str:
    label, value = _clean(label), _clean(value)
    k = _key(label)
    now = time.time()
    old = db.one("SELECT label, value FROM facts WHERE key=?", (k,))
    if old:
        label = old["label"]
    elif label:
        label = label[0].upper() + label[1:]
    db.execute("INSERT OR REPLACE INTO facts(key,label,value,category,created,updated) VALUES(?,?,?,?,COALESCE((SELECT created FROM facts WHERE key=?),?),?)",
               (k, label, value, category, k, now, now))
    if old and old["value"].lower() != value.lower():
        return f"Aktualisiert: {_pronoun(label)} {label} ist jetzt {value} (vorher {old['value']})."
    return f"Gemerkt: {_pronoun(label)} {label} ist {value}."


def find(label: str):
    rows = db.query("SELECT * FROM facts")
    if not rows:
        return None
    k = _key(label)
    for r in rows:
        if r["key"] == k:
            return r
    hit, _ = best_match(label, rows, key=lambda r: r["label"], threshold=0.72)
    return hit


def all_facts():
    return db.query("SELECT * FROM facts ORDER BY category, label")


def context_text(limit=60) -> str:
    """Kompakte Faktenliste für die KI."""
    rows = db.query("SELECT label, value FROM facts ORDER BY updated DESC LIMIT ?", (limit,))
    return "\n".join(f"- {r['label']}: {r['value']}" for r in rows)


# ------------------------------------------------------------------ Aktionen
@action("memory_remember", "Speichert eine dauerhafte Information über den Nutzer",
        {"label": S("Wofür, z. B. 'Lieblingsspiel', 'Name', 'Geburtstag'"), "value": S("Der Wert")},
        risk=0, module=M)
def a_remember(label, value):
    return remember(label, value)


@action("memory_recall", "Liest eine gespeicherte Information aus dem Gedächtnis",
        {"label": S("Wonach gesucht wird")}, risk=0, module=M)
def a_recall(label):
    r = find(label)
    if not r:
        return {"ok": False, "text": f"Dazu habe ich mir nichts gemerkt ({label})."}
    return f"{_pronoun(r['label'])} {r['label']} ist {r['value']}."


@action("memory_forget", "Löscht eine gespeicherte Information aus dem Gedächtnis",
        {"label": S("Was vergessen werden soll")}, risk=1, module=M)
def a_forget(label):
    r = find(label)
    if not r:
        return {"ok": False, "text": f"Zu „{label}“ habe ich nichts gespeichert."}
    db.execute("DELETE FROM facts WHERE key=?", (r["key"],))
    return f"Vergessen: {r['label']}."


@action("memory_list", "Listet alles, was JARVIS über den Nutzer weiß", {}, risk=0, module=M)
def a_list():
    rows = all_facts()
    if not rows:
        return "Ich habe mir bisher noch nichts über dich gemerkt."
    items = [f"{r['label']}: {r['value']}" for r in rows[:25]]
    more = f" … und {len(rows) - 25} weitere" if len(rows) > 25 else ""
    return {"text": "Das weiß ich über dich – " + "; ".join(items) + more + ".", "data": rows}


class MemoryModule(Module):
    name = "memory"
    title = "Gedächtnis"

    def parse(self, text, norm, ctx):
        n = norm

        # Auflisten
        if re.search(r"was weißt du (alles )?über mich|was hast du (dir )?(alles )?gemerkt|zeig (mir )?(dein|das) gedächtnis", n):
            return Intent([("memory_list", {})])

        # Vergessen
        m = re.match(r"^vergiss,? (dass |das )?(mein|meine|meinen) (.+?)( ist .+)?$", n)
        if m:
            return Intent([("memory_forget", {"label": m.group(3)})])
        m = re.match(r"^(vergiss|lösche aus dem gedächtnis|lösch aus dem gedächtnis) (.+)$", n)
        if m and not re.match(r"^(es|das|alles)$", m.group(2)):
            return Intent([("memory_forget", {"label": re.sub(r"^(den|die|das|mein|meine|meinen)\s+", "", m.group(2))})], confidence=0.8)

        # Ändern
        m = re.match(r"^(ändere|änder|setze|setz|korrigiere) (mein|meine|meinen) (.+?) (auf|zu|in) (.+)$", n)
        if m:
            return Intent([("memory_remember", {"label": m.group(3), "value": _orig(text, m.group(5))})])

        # Abfragen
        m = re.match(r"^(was|wer|wie|wann|wo|welche[rsn]?) (ist|sind|heißt|war) (eigentlich )?(mein|meine) (.+)$", n)
        if m:
            label = m.group(5)
            if find(label):
                return Intent([("memory_recall", {"label": label})])
            # unbekannt -> ggf. KI, aber mit niedriger Sicherheit lokal beantworten
            return Intent([("memory_recall", {"label": label})], confidence=0.5)
        if re.match(r"^wie heiße ich$|^weißt du wie ich heiße$|^kennst du meinen namen$", n):
            return Intent([("memory_recall", {"label": "Name"})])
        if re.match(r"^wo wohne ich$", n):
            return Intent([("memory_recall", {"label": "Wohnort"})])

        # Merk dir ...
        m = re.match(r"^merk(e)? dir,? (dass |das )?(.+)$", n)
        explicit = bool(m)
        body_norm = m.group(3) if m else n
        body = _orig(text, body_norm)

        fact = self._fact(body_norm, body)
        if fact:
            return Intent([("memory_remember", {"label": fact[0], "value": fact[1]})])
        if explicit:
            label = "Notiz " + time.strftime("%d.%m. %H:%M")
            return Intent([("memory_remember", {"label": label, "value": body})])
        return None

    @staticmethod
    def _fact(n, orig):
        # mein Lieblingsspiel ist GTA / meine Lieblingsfarbe ist blau / meine Hobbys sind ...
        m = re.match(r"^(mein|meine) ([\wäöüß\- ]{2,40}?) (ist|sind|lautet|heißt) (?!das |der |die |gerade |kaputt|voll|leer|aus|an\b)(.+)$", n)
        if m and m.group(2).split()[-1] not in NOT_FACT_SUBJECTS:
            label = _orig(orig, m.group(2), title=True)
            return label, _orig(orig, m.group(4))
        m = re.match(r"^ich hei(ß|ss)e (.+)$", n)
        if m:
            return "Name", _orig(orig, m.group(2))
        m = re.match(r"^ich wohne in (.+)$", n)
        if m:
            return "Wohnort", _orig(orig, m.group(1))
        m = re.match(r"^ich bin am (.+) geboren$|^mein geburtstag ist am (.+)$", n)
        if m:
            return "Geburtstag", _orig(orig, m.group(1) or m.group(2))
        return None


def _orig(text: str, fragment: str, title=False) -> str:
    """Findet die Originalschreibweise (Groß/Klein) eines normalisierten Fragments."""
    idx = text.lower().find(fragment.lower())
    out = text[idx: idx + len(fragment)] if idx >= 0 else fragment
    out = _clean(out)
    if title and out:
        out = out[0].upper() + out[1:]
    return out
