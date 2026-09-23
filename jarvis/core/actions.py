"""Zentrale Aktions-Registry.

Jede Fähigkeit von JARVIS ist eine Aktion mit Beschreibung, Parametern und Risikostufe.
Der lokale Sprachparser, Profile, Routinen, eigene Befehle und die KI verwenden
alle dieselben Aktionen – dadurch gibt es genau eine Stelle für Sicherheit und Protokoll.

Risikostufen:
    0 = nur lesen / harmlos
    1 = normale Aktion (Programm starten, Lautstärke ...)
    2 = heikel (Herunterfahren, Neustart, Programme hart beenden ...)
    3 = kritisch (Dateien löschen ...) – immer bestätigen
"""
import inspect
import threading
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Action:
    name: str
    func: Callable
    description: str
    params: dict = field(default_factory=dict)     # JSON-Schema properties
    required: list = field(default_factory=list)
    risk: int = 1
    module: str = ""
    confirm_text: str = ""                          # Rückfrage-Text, {arg} Platzhalter erlaubt
    ai: bool = True                                 # der KI als Werkzeug anbieten

    def schema(self):
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "strict": False,
            "parameters": {
                "type": "object",
                "properties": self.params,
                "required": self.required,
                "additionalProperties": False,
            },
        }


@dataclass
class Result:
    ok: bool
    text: str = ""
    data: object = None

    @staticmethod
    def of(value):
        if isinstance(value, Result):
            return value
        if value is None:
            return Result(True, "")
        if isinstance(value, str):
            return Result(True, value)
        if isinstance(value, dict):
            return Result(value.get("ok", True), value.get("text", ""), value.get("data"))
        return Result(True, str(value))


class Registry:
    def __init__(self):
        self._actions: dict[str, Action] = {}
        self._lock = threading.Lock()

    def register(self, name, description, params=None, required=None, risk=1, module="",
                 confirm_text="", ai=True):
        def deco(func):
            with self._lock:
                self._actions[name] = Action(name, func, description, params or {},
                                             required if required is not None else list((params or {}).keys()),
                                             risk, module, confirm_text, ai)
            return func
        return deco

    def get(self, name) -> Action | None:
        return self._actions.get(name)

    def all(self):
        return list(self._actions.values())

    def ai_tools(self):
        return [a.schema() for a in self._actions.values() if a.ai]

    def call(self, name, args=None, ctx=None) -> Result:
        """Führt eine Aktion OHNE Sicherheitsprüfung aus – nur über den Brain aufrufen."""
        act = self._actions.get(name)
        if not act:
            return Result(False, f"Unbekannte Aktion: {name}")
        args = dict(args or {})
        sig = inspect.signature(act.func)
        if "ctx" in sig.parameters:
            args["ctx"] = ctx
        # Unbekannte Argumente (z. B. von der KI) verwerfen statt abzustürzen
        accepted = {k: v for k, v in args.items() if k in sig.parameters
                    or any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values())}
        return Result.of(act.func(**accepted))


registry = Registry()
action = registry.register


def S(desc, enum=None):
    d = {"type": "string", "description": desc}
    if enum:
        d["enum"] = enum
    return d


def N(desc):
    return {"type": "number", "description": desc}


def B(desc):
    return {"type": "boolean", "description": desc}
