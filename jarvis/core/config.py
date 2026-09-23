"""Einstellungen – werden ausschließlich über die JARVIS-Oberfläche oder per Sprache geändert."""
import copy
import json
import threading

from . import paths

DEFAULTS = {
    "setup_done": False,
    "user_name": "",
    "language": "de",
    # KI
    "ai": {
        "enabled": True,
        "provider": "openai",          # openai | ollama (lokal)
        "ollama_url": "http://localhost:11434",
        "ollama_model": "",
        "model": "gpt-4.1-mini",
        "vision_model": "gpt-4.1-mini",
        "web_search": False,
        "max_history": 12,
    },
    # Sprache
    "voice": {
        "tts_enabled": True,
        "tts_engine": "edge",          # edge | openai | system | off
        "edge_voice": "de-DE-ConradNeural",
        "openai_voice": "onyx",
        "openai_tts_model": "gpt-4o-mini-tts",
        "volume": 90,                   # 0-100
        "rate": 0,                      # -50 .. +50 (%)
        "stt_engine": "openai",         # openai | local
        "local_stt_model": "small",
        "wake_word": True,
        "wake_threshold": 0.5,
        "always_listen": False,         # Folgefragen ohne erneutes Wake-Word
        "follow_up_seconds": 6,
        "input_device": None,
        "output_device": None,
        "chime": True,
    },
    # Sicherheit / Datenschutz
    "security": {
        "confirm_level": 2,             # Aktionen ab dieser Stufe brauchen Bestätigung
        "privacy_mode": False,          # nur lokale Verarbeitung
        "screen_ai": True,
    },
    # Verhalten
    "app": {
        "start_with_windows": False,
        "start_minimized": False,
        "close_to_tray": True,
        "boot_animation": True,
        "notifications": True,
        "active_mode": True,
        "auto_backup": True,
    },
    "update": {
        "url": "https://github.com/Justinn18080384-cell/JARVIS",
        "auto_check": True,
        "channel": "stable",
    },
    "remote": {
        "enabled": False,
        "port": 8765,
        "token": "",
        "https": True,                  # nötig, damit Handy-Browser das Mikrofon freigeben
        "speak_on_pc": False,           # Antworten zusätzlich am PC vorlesen
    },
    "briefing": {
        "city": "",                     # leer = Wohnort aus dem Gedächtnis
        "include_news": True,
    },
    "smarthome": {
        "homeassistant_url": "",
    },
}


def _merge(base, extra):
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self):
        self._lock = threading.RLock()
        self._data = copy.deepcopy(DEFAULTS)
        self._listeners = []
        self.load()

    def load(self):
        with self._lock:
            try:
                if paths.CONFIG_FILE.exists():
                    self._data = _merge(DEFAULTS, json.loads(paths.CONFIG_FILE.read_text("utf-8-sig")))
            except Exception:
                # Kaputte Datei nicht verlieren, sondern beiseitelegen
                try:
                    paths.CONFIG_FILE.replace(paths.CONFIG_FILE.with_suffix(".broken.json"))
                except Exception:
                    pass
                self._data = copy.deepcopy(DEFAULTS)

    def save(self):
        with self._lock:
            paths.DATA.mkdir(parents=True, exist_ok=True)
            tmp = paths.CONFIG_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), "utf-8")
            tmp.replace(paths.CONFIG_FILE)

    def get(self, key, default=None):
        with self._lock:
            node = self._data
            for part in key.split("."):
                if not isinstance(node, dict) or part not in node:
                    return default
                node = node[part]
            return copy.deepcopy(node)

    def set(self, key, value, save=True):
        with self._lock:
            parts = key.split(".")
            node = self._data
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value
            if save:
                self.save()
        for cb in list(self._listeners):
            try:
                cb(key, value)
            except Exception:
                pass

    def update(self, values: dict):
        for k, v in values.items():
            self.set(k, v, save=False)
        self.save()

    def all(self):
        with self._lock:
            return copy.deepcopy(self._data)

    def on_change(self, cb):
        self._listeners.append(cb)


config = Config()
