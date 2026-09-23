"""Einfacher Event-Bus zwischen Modulen und Oberfläche."""
import threading
from collections import defaultdict


class EventBus:
    def __init__(self):
        self._subs = defaultdict(list)
        self._lock = threading.Lock()

    def on(self, event, cb):
        with self._lock:
            self._subs[event].append(cb)

    def emit(self, event, **data):
        with self._lock:
            subs = list(self._subs.get(event, ())) + list(self._subs.get("*", ()))
        for cb in subs:
            try:
                cb(event, data)
            except Exception:
                pass


bus = EventBus()
