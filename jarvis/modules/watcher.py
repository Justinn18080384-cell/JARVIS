"""Aktiver Modus: erkennt Systemereignisse und reagiert darauf."""
import threading
import time

import psutil

from ..core import log
from ..core.config import config
from ..core.events import bus
from ..core.module import Module
from ..core.text import ascii_fold


class WatcherModule(Module):
    name = "watcher"
    title = "Aktiver Modus"

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="watcher").start()

    def _loop(self):
        bat_warned = False
        drives = self._drives()
        procs = self._procs()
        cpu_high_since = None
        cpu_warned = 0
        psutil.cpu_percent(None)
        while True:
            time.sleep(5)
            if not config.get("app.active_mode", True):
                continue
            try:
                # Akku
                b = psutil.sensors_battery()
                if b and not b.power_plugged and b.percent <= 15 and not bat_warned:
                    bat_warned = True
                    bus.emit("battery_low", percent=b.percent)
                    bus.emit("notify", title="Akku", text=f"Akku nur noch bei {b.percent:.0f} Prozent.", speak=True)
                if b and (b.power_plugged or b.percent > 20):
                    bat_warned = False

                # Laufwerke (USB-Sticks, externe Platten)
                now_drives = self._drives()
                for d in now_drives - drives:
                    bus.emit("device_connected", name=f"Laufwerk {d}")
                    bus.emit("notify", title="Laufwerk", text=f"Neues Laufwerk {d} erkannt.", speak=False)
                    log.activity("ereignis", f"Laufwerk {d} angeschlossen")
                drives = now_drives

                # Programmstarts (für ereignisgesteuerte Routinen)
                now_procs = self._procs()
                for p in now_procs - procs:
                    bus.emit("app_started", name=p)
                procs = now_procs

                # Dauerhaft hohe CPU-Last
                cpu = psutil.cpu_percent(None)
                if cpu > 95:
                    cpu_high_since = cpu_high_since or time.time()
                    if time.time() - cpu_high_since > 60 and time.time() - cpu_warned > 900:
                        cpu_warned = time.time()
                        bus.emit("notify", title="Auslastung", text="Die CPU ist seit einer Minute voll ausgelastet.", speak=False)
                else:
                    cpu_high_since = None
            except Exception as e:
                log.logger().warning("Watcher: %s", e)

    @staticmethod
    def _drives():
        try:
            return {p.mountpoint for p in psutil.disk_partitions(all=False) if "removable" in p.opts or "cdrom" not in p.opts}
        except Exception:
            return set()

    @staticmethod
    def _procs():
        out = set()
        for p in psutil.process_iter(["name"]):
            n = p.info.get("name")
            if n:
                out.add(ascii_fold(n.rsplit(".", 1)[0]))
        return out
