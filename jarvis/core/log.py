"""Aktivitätsprotokoll (was hat JARVIS getan) und Fehlerprotokoll."""
import logging
import logging.handlers
import time
import traceback

from . import paths
from .db import db
from .events import bus

_logger = None


def logger():
    global _logger
    if _logger is None:
        paths.LOG_DIR.mkdir(parents=True, exist_ok=True)
        lg = logging.getLogger("jarvis")
        lg.setLevel(logging.INFO)
        h = logging.handlers.RotatingFileHandler(paths.LOG_DIR / "jarvis.log", maxBytes=2_000_000,
                                                 backupCount=3, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        lg.addHandler(h)
        _logger = lg
    return _logger


def activity(kind: str, text: str, ok: bool = True):
    """Für den Nutzer sichtbarer Eintrag im Aktivitätsprotokoll."""
    ts = time.time()
    try:
        db.execute("INSERT INTO activity(ts, kind, text, ok) VALUES(?,?,?,?)", (ts, kind, text, int(ok)))
    except Exception:
        pass
    logger().info("%s: %s", kind, text)
    bus.emit("activity", ts=ts, kind=kind, text=text, ok=ok)


def error(context: str, exc: BaseException | None = None) -> str:
    """Protokolliert einen Fehler vollständig und liefert eine verständliche Meldung."""
    detail = "".join(traceback.format_exception(exc)) if exc else ""
    logger().error("%s\n%s", context, detail)
    msg = friendly(exc) if exc else context
    activity("fehler", f"{context}: {msg}", ok=False)
    return msg


def friendly(exc: BaseException) -> str:
    name = type(exc).__name__
    text = str(exc)
    if "AuthenticationError" in name or "invalid_api_key" in text or "Incorrect API key" in text:
        return "Der OpenAI-API-Schlüssel ist ungültig. Bitte in den Einstellungen prüfen."
    if "RateLimit" in name or "insufficient_quota" in text:
        return "Das OpenAI-Kontingent ist aufgebraucht oder das Limit wurde erreicht."
    if "APIConnectionError" in name or "ConnectError" in name or "getaddrinfo" in text:
        return "Keine Verbindung zum Internet oder zum Dienst."
    if "Timeout" in name:
        return "Der Dienst hat nicht rechtzeitig geantwortet."
    if isinstance(exc, FileNotFoundError):
        return "Datei oder Programm wurde nicht gefunden."
    if isinstance(exc, PermissionError):
        return "Dafür fehlen die Berechtigungen."
    if "PortAudio" in text or "sounddevice" in name.lower():
        return "Problem mit dem Audiogerät (Mikrofon/Lautsprecher)."
    return text[:200] if text else name
