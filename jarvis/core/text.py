"""Hilfsfunktionen für deutsches Sprachverständnis."""
import difflib
import re
import unicodedata

FILLER = [
    r"\bjarvis\b", r"\bhey\b", r"\bhallo\b", r"\bbitte\b", r"\bmal\b", r"\bdoch\b", r"\bkurz\b",
    r"\bkannst du\b", r"\bkönntest du\b", r"\bwürdest du\b", r"\bdanke\b", r"\beinfach\b",
]

NUMBERS = {
    "null": 0, "eins": 1, "ein": 1, "eine": 1, "einen": 1, "zwei": 2, "drei": 3, "vier": 4,
    "fünf": 5, "sechs": 6, "sieben": 7, "acht": 8, "neun": 9, "zehn": 10, "elf": 11, "zwölf": 12,
    "dreizehn": 13, "vierzehn": 14, "fünfzehn": 15, "sechzehn": 16, "siebzehn": 17, "achtzehn": 18,
    "neunzehn": 19, "zwanzig": 20, "dreißig": 30, "dreissig": 30, "vierzig": 40, "fünfzig": 50,
    "sechzig": 60, "siebzig": 70, "achtzig": 80, "neunzig": 90, "hundert": 100, "halb": 50,
}


def normalize(text: str) -> str:
    t = text.lower().strip()
    t = t.replace("’", "'").replace("„", "").replace("“", "").replace('"', "")
    for f in FILLER:
        t = re.sub(f, " ", t)
    t = re.sub(r"[!?.;]+$", "", t)
    t = re.sub(r"^[,\s]+|[,\s]+$", "", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def ascii_fold(s: str) -> str:
    s = s.lower().replace("ß", "ss").replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def words_to_number(s: str):
    s = s.strip().lower()
    if re.fullmatch(r"\d+([.,]\d+)?", s):
        return float(s.replace(",", "."))
    if s in NUMBERS:
        return NUMBERS[s]
    m = re.fullmatch(r"(\w+)und(\w+)", s)  # einundzwanzig
    if m and m.group(1) in NUMBERS and m.group(2) in NUMBERS:
        return NUMBERS[m.group(1)] + NUMBERS[m.group(2)]
    return None


def find_number(s: str):
    m = re.search(r"(\d+(?:[.,]\d+)?)", s)
    if m:
        return float(m.group(1).replace(",", "."))
    for w in re.findall(r"[a-zäöüß]+", s.lower()):
        n = words_to_number(w)
        if n is not None and w not in ("ein", "eine", "einen"):
            return n
    return None


def split_list(s: str) -> list[str]:
    """'A, B und C' -> ['A', 'B', 'C']"""
    parts = re.split(r"\s*,\s*|\s+und\s+|\s+sowie\s+|\s+dann\s+|\s+danach\s+", s)
    return [p.strip(" .") for p in parts if p.strip(" .")]


def similarity(a: str, b: str) -> float:
    a, b = ascii_fold(a), ascii_fold(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    # Teilwort-Treffer ("chrome" in "google chrome")
    aw, bw = set(a.split()), set(b.split())
    if aw and aw <= bw:
        ratio = max(ratio, 0.85 - 0.02 * (len(bw) - len(aw)))
    elif a.replace(" ", "") in b.replace(" ", ""):
        ratio = max(ratio, 0.75)
    return ratio


def best_match(query: str, candidates, key=lambda x: x, threshold=0.6):
    best, score = None, 0.0
    for c in candidates:
        s = similarity(query, key(c))
        if s > score:
            best, score = c, s
    return (best, score) if score >= threshold else (None, score)


def strip_articles(s: str) -> str:
    return re.sub(r"^(den|die|das|dem|der|ein|eine|einen|mein|meine|meinen)\s+", "", s.strip())
