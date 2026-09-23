"""Basisklasse für alle JARVIS-Module.

Neue Funktionen werden als eigenes Modul ergänzt: Aktionen registrieren,
optional einen lokalen Sprachparser (`parse`) sowie Status und Selbstdiagnose liefern.
"""
from dataclasses import dataclass, field


@dataclass
class Intent:
    """Ergebnis des lokalen Parsers: eine oder mehrere Aktionen oder eine direkte Antwort."""
    calls: list = field(default_factory=list)      # [(action_name, args), ...]
    reply: str | None = None
    confidence: float = 1.0


class Module:
    name = "module"
    title = "Modul"

    def __init__(self, jarvis):
        self.jarvis = jarvis

    def start(self):
        pass

    def stop(self):
        pass

    def parse(self, text: str, norm: str, ctx) -> Intent | None:
        """Lokales Sprachverständnis. `norm` ist kleingeschrieben und bereinigt."""
        return None

    def status(self) -> dict:
        return {}

    def diagnose(self) -> list:
        """Liste von (Prüfung, ok, Meldung)."""
        return []
