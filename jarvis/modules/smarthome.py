"""Zimmer- und Gerätesteuerung.

Anbieter (Provider) sind austauschbar. Aktuell:
    virtual        – Testgeräte ohne Hardware (zum Ausprobieren)
    homeassistant  – Home Assistant über REST-API (deckt Hue, Shelly, Tuya, Sonos, TVs … ab)
Weitere Anbieter (z. B. Hue direkt, Tuya, FRITZ!DECT) werden als neue Provider-Klasse ergänzt.
"""
import json
import re
import threading
import uuid

import requests

from ..core import log, secrets
from ..core.actions import action, S, N
from ..core.config import config
from ..core.db import db
from ..core.events import bus
from ..core.module import Module, Intent
from ..core.text import similarity, ascii_fold, find_number

M = "smarthome"
TYPES = {"licht": "light", "lampe": "light", "lampen": "light", "leuchte": "light", "steckdose": "plug", "stecker": "plug",
         "fernseher": "tv", "tv": "tv", "lautsprecher": "speaker", "box": "speaker", "boxen": "speaker", "ventilator": "fan",
         "heizung": "climate", "rollladen": "cover", "rollo": "cover"}
TYPE_DE = {"light": "Licht", "plug": "Steckdose", "tv": "Fernseher", "speaker": "Lautsprecher", "fan": "Ventilator",
           "climate": "Heizung", "cover": "Rollladen", "other": "Gerät"}


# ================================================================ Provider
class VirtualProvider:
    name = "virtual"

    def set(self, dev, on, brightness=None):
        return True

    def test(self):
        return True, "virtuelle Geräte"


class HomeAssistantProvider:
    name = "homeassistant"

    def _base(self):
        url = (config.get("smarthome.homeassistant_url") or "").rstrip("/")
        token = secrets.get("homeassistant_token")
        if not url or not token:
            raise RuntimeError("Home Assistant ist nicht eingerichtet (URL und Token in den Einstellungen).")
        return url, {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def set(self, dev, on, brightness=None):
        url, h = self._base()
        entity = dev["address"]
        domain = entity.split(".")[0]
        if domain == "cover":
            service = "open_cover" if on else "close_cover"
        else:
            service = "turn_on" if on else "turn_off"
        body = {"entity_id": entity}
        if on and brightness is not None and domain == "light":
            body["brightness_pct"] = int(brightness)
        r = requests.post(f"{url}/api/services/{domain}/{service}", headers=h, json=body, timeout=8)
        r.raise_for_status()
        return True

    def entities(self):
        url, h = self._base()
        tpl = ("{% set ns = namespace(o=[]) %}{% for s in states %}{% if s.domain in ['light','switch','media_player','fan','cover','climate'] %}"
               "{% set ns.o = ns.o + [[s.entity_id, s.name, area_name(s.entity_id) or '', s.state]] %}{% endif %}{% endfor %}{{ ns.o | tojson }}")
        r = requests.post(f"{url}/api/template", headers=h, json={"template": tpl}, timeout=10)
        r.raise_for_status()
        return json.loads(r.text)

    def test(self):
        try:
            url, h = self._base()
            r = requests.get(f"{url}/api/", headers=h, timeout=5)
            return r.ok, "verbunden" if r.ok else f"HTTP {r.status_code}"
        except Exception as e:
            return False, log.friendly(e)


PROVIDERS = {"virtual": VirtualProvider(), "homeassistant": HomeAssistantProvider()}


# ================================================================== Daten
def rooms():
    return [r["name"] for r in db.query("SELECT name FROM rooms ORDER BY name")]


def devices(room=None):
    rows = db.query("SELECT * FROM devices" + (" WHERE room=?" if room else "") + " ORDER BY room, name", (room,) if room else ())
    return rows


def find_room(name):
    best, score = None, 0
    for r in rooms():
        s = similarity(name, r)
        if s > score:
            best, score = r, s
    return best if score >= 0.7 else None


def find_device(name, room=None):
    best, score = None, 0
    for d in devices(room):
        s = max(similarity(name, d["name"]), similarity(name, f"{TYPE_DE.get(d['type'], '')} {d['room']}"))
        if s > score:
            best, score = d, s
    return best if score >= 0.7 else None


def _set(dev, on, brightness=None):
    prov = PROVIDERS.get(dev["provider"] or "virtual", PROVIDERS["virtual"])
    prov.set(dev, on, brightness)
    state = "on" if on else "off"
    db.execute("UPDATE devices SET state=? WHERE id=?", (state, dev["id"]))
    bus.emit("devices_changed")


# ================================================================ Aktionen
@action("room_add", "Legt ein Zimmer an", {"name": S("Name des Zimmers")}, risk=0, module=M)
def room_add(name):
    name = name.strip().title()
    db.execute("INSERT OR IGNORE INTO rooms(name) VALUES(?)", (name,))
    bus.emit("devices_changed")
    return f"Zimmer {name} angelegt."


@action("room_remove", "Löscht ein Zimmer (Geräte bleiben ohne Zimmer erhalten)", {"name": S("Zimmer")}, risk=1, module=M)
def room_remove(name):
    r = find_room(name)
    if not r:
        return {"ok": False, "text": f"Zimmer „{name}“ gibt es nicht."}
    db.execute("DELETE FROM rooms WHERE name=?", (r,))
    db.execute("UPDATE devices SET room=NULL WHERE room=?", (r,))
    bus.emit("devices_changed")
    return f"Zimmer {r} gelöscht."


@action("device_add", "Fügt ein Smart-Home-Gerät hinzu und ordnet es einem Zimmer zu",
        {"name": S("Gerätename, z. B. 'Deckenlicht'"), "room": S("Zimmer"),
         "type": S("light, plug, tv, speaker, fan, climate, cover, other", list(TYPE_DE)),
         "address": S("Home-Assistant entity_id, falls bekannt, sonst leer")}, required=["name", "room", "type", "address"], risk=0, module=M)
def device_add(name, room, type="light", address=""):
    room_name = find_room(room) or room.strip().title()
    db.execute("INSERT OR IGNORE INTO rooms(name) VALUES(?)", (room_name,))
    provider = "homeassistant" if address and "." in address else "virtual"
    if provider == "virtual" and config.get("smarthome.homeassistant_url") and secrets.has("homeassistant_token"):
        # passendes Home-Assistant-Gerät suchen
        try:
            ents = PROVIDERS["homeassistant"].entities()
            best = max(ents, key=lambda e: similarity(name, e[1]), default=None)
            if best and similarity(name, best[1]) > 0.75:
                provider, address = "homeassistant", best[0]
        except Exception:
            pass
    db.execute("INSERT INTO devices(id,name,room,type,provider,address,state) VALUES(?,?,?,?,?,?,?)",
               (uuid.uuid4().hex[:10], name.strip(), room_name, type, provider, address, "off"))
    bus.emit("devices_changed")
    hint = "" if provider != "virtual" else " (vorerst virtuell – verbinde es später über Home Assistant)"
    return f"{TYPE_DE.get(type, 'Gerät')} „{name}“ im Zimmer {room_name} hinzugefügt{hint}."


@action("device_remove", "Entfernt ein Smart-Home-Gerät", {"name": S("Gerätename")}, risk=1, module=M)
def device_remove(name):
    d = find_device(name)
    if not d:
        return {"ok": False, "text": f"Gerät „{name}“ nicht gefunden."}
    db.execute("DELETE FROM devices WHERE id=?", (d["id"],))
    bus.emit("devices_changed")
    return f"„{d['name']}“ entfernt."


@action("device_move", "Ordnet ein Gerät einem anderen Zimmer zu", {"name": S("Gerät"), "room": S("Zimmer")}, risk=0, module=M)
def device_move(name, room):
    d = find_device(name)
    if not d:
        return {"ok": False, "text": f"Gerät „{name}“ nicht gefunden."}
    r = find_room(room) or room.title()
    db.execute("INSERT OR IGNORE INTO rooms(name) VALUES(?)", (r,))
    db.execute("UPDATE devices SET room=? WHERE id=?", (r, d["id"]))
    bus.emit("devices_changed")
    return f"„{d['name']}“ ist jetzt im Zimmer {r}."


@action("device_set", "Schaltet Smart-Home-Geräte ein/aus. Entweder ein Gerät per Name oder alle Geräte eines Typs/Zimmers.",
        {"name": S("Gerätename oder leer"), "room": S("Zimmer oder leer"), "type": S("Gerätetyp (light, plug, tv, …) oder 'all'"),
         "on": {"type": "boolean"}, "brightness": N("Helligkeit 0-100 oder leer")},
        required=["name", "room", "type", "on", "brightness"], risk=1, module=M)
def device_set(name="", room="", type="", on=True, brightness=None):
    targets = []
    if name:
        d = find_device(name, find_room(room) if room else None) or find_device(name)
        if d:
            targets = [d]
    else:
        r = find_room(room) if room else None
        if room and not r:
            return {"ok": False, "text": f"Zimmer „{room}“ kenne ich nicht."}
        targets = [d for d in devices(r) if type in ("", "all", None) or d["type"] == type]
        if not room and not r and type in ("", "all", None):
            return {"ok": False, "text": "Welches Zimmer meinst du?"}
    if not targets:
        rs = rooms()
        if not rs:
            return {"ok": False, "text": "Es sind noch keine Smart-Home-Geräte eingerichtet. Sag zum Beispiel: „Füge im Wohnzimmer eine Lampe namens Deckenlicht hinzu.“"}
        return {"ok": False, "text": "Dazu habe ich kein passendes Gerät gefunden."}
    errors = []
    for d in targets:
        try:
            _set(d, bool(on), brightness if brightness not in ("", None) else None)
        except Exception as e:
            errors.append(f"{d['name']}: {log.friendly(e)}")
    what = targets[0]["name"] if len(targets) == 1 else f"{len(targets)} Geräte" + (f" im {targets[0]['room']}" if room else "")
    if errors:
        return {"ok": False, "text": f"Nicht alles hat geklappt – {'; '.join(errors)}"}
    extra = f" auf {int(brightness)} Prozent" if brightness not in ("", None) and on else ""
    return f"{what} {'eingeschaltet' if on else 'ausgeschaltet'}{extra}."


@action("devices_list", "Listet Zimmer und Smart-Home-Geräte", {}, risk=0, module=M)
def devices_list():
    ds = devices()
    if not ds:
        return "Es sind noch keine Geräte eingerichtet."
    by = {}
    for d in ds:
        by.setdefault(d["room"] or "ohne Zimmer", []).append(f"{d['name']} ({'an' if d['state'] == 'on' else 'aus'})")
    return {"text": " | ".join(f"{r}: {', '.join(v)}" for r, v in by.items()), "data": ds}


@action("homeassistant_import", "Importiert alle Geräte aus Home Assistant mit ihren Zimmern", {}, risk=0, module=M)
def homeassistant_import():
    ents = PROVIDERS["homeassistant"].entities()
    dom_type = {"light": "light", "switch": "plug", "media_player": "tv", "fan": "fan", "cover": "cover", "climate": "climate"}
    known = {d["address"] for d in devices()}
    n = 0
    for eid, name, area, state in ents:
        if eid in known:
            continue
        room = area or "Unsortiert"
        db.execute("INSERT OR IGNORE INTO rooms(name) VALUES(?)", (room,))
        db.execute("INSERT INTO devices(id,name,room,type,provider,address,state) VALUES(?,?,?,?,?,?,?)",
                   (uuid.uuid4().hex[:10], name, room, dom_type.get(eid.split(".")[0], "other"), "homeassistant", eid,
                    "on" if state in ("on", "playing", "open") else "off"))
        n += 1
    bus.emit("devices_changed")
    return f"{n} Geräte aus Home Assistant übernommen."


# ================================================================== Modul
class SmartHomeModule(Module):
    name = "smarthome"
    title = "Smart Home"

    def parse(self, text, n, ctx):
        # Konfiguration: „Füge im Wohnzimmer eine Lampe namens Deckenlicht hinzu“
        m = re.match(r"^(füg|füge|leg|lege) (im |in der |in die |ins )?(?:zimmer )?([\wäöüß]+) (ein |eine |einen )?(\w+)( namens| mit dem namen)? ?(.*?) (hinzu|an)$", n)
        if m and m.group(5) in TYPES:
            name = m.group(7) or f"{m.group(5).title()} {m.group(3).title()}"
            return Intent([("device_add", {"name": _orig(text, name).strip() or name, "room": m.group(3), "type": TYPES[m.group(5)], "address": ""})])
        m = re.match(r"^(leg|lege|erstelle) (ein )?(neues )?zimmer (namens )?(.+?)( an)?$", n)
        if m:
            return Intent([("room_add", {"name": m.group(5)})])
        m = re.match(r"^(lösch|lösche|entferne) (das )?zimmer (.+)$", n)
        if m:
            return Intent([("room_remove", {"name": m.group(3)})])
        m = re.match(r"^(verschieb|verschiebe|pack|packe|ordne) (das gerät |die lampe |das licht |die steckdose )?(.+?) (ins|in das|in die|zum|zu) (zimmer )?(.+?)( zu)?$", n)
        if m and find_device(m.group(3)):
            return Intent([("device_move", {"name": m.group(3), "room": m.group(6)})])
        if re.search(r"(welche|zeig|liste).*(geräte|zimmer|lampen)|smart ?home status", n):
            return Intent([("devices_list", {})])
        if re.search(r"home ?assistant.*(importier|übernehm|synchronisier)", n):
            return Intent([("homeassistant_import", {})])

        # Schalten
        on = None
        if re.search(r"\b(an|ein|einschalten|anschalten|anmachen|hell)\b$", n) or re.match(r"^(schalte|schalt|mach|mache) .* (an|ein)$", n):
            on = True
        if re.search(r"\b(aus|ausschalten|abschalten|ausmachen|dunkel)\b$", n):
            on = False
        m_dim = re.search(r"(dimm|dimme|stell|stelle|setze?) (das |die )?(licht|lampe[n]?|.+?) (im |in der |in )?([\wäöüß]+)? ?auf ([\w,.]+) ?(prozent|%)", n)
        if m_dim:
            room = find_room(m_dim.group(5) or "") if m_dim.group(5) else None
            dev = find_device(m_dim.group(3), room) if m_dim.group(3) not in ("licht", "lampe", "lampen") else None
            b = find_number(m_dim.group(6))
            if room or dev:
                return Intent([("device_set", {"name": dev["name"] if dev else "", "room": room or "", "type": "light",
                                               "on": True, "brightness": b})])
        if on is None:
            return None
        room = None
        rm = re.search(r"\b(im|in der|in dem|in|vom|von der|ganze[nm]?) ([\wäöüß]+)", n)
        if rm:
            room = find_room(rm.group(2))
        body = re.sub(r"^(schalte|schalt|mach|mache|dreh|drehe)\s+", "", n)
        body = re.sub(r"\s+(an|ein|aus|einschalten|ausschalten|anmachen|ausmachen)$", "", body)
        body_wo_room = re.sub(r"\b(im|in der|in dem|in|vom|von der) [\wäöüß]+", "", body).strip()
        if re.match(r"^(alles|alle geräte|überall alles)$", body_wo_room):
            if not room and rm:
                return Intent(reply=f"Ein Zimmer „{rm.group(2).title()}“ kenne ich noch nicht. "
                                    f"Sag zum Beispiel: „Füge im Zimmer {rm.group(2).title()} eine Lampe namens Deckenlicht hinzu.“")
            return Intent([("device_set", {"name": "", "room": room or "", "type": "all", "on": on, "brightness": None})]) if room else None
        tm = re.search(r"\b(das )?(licht|lampen?|steckdosen?|fernseher|tv|lautsprecher|boxen?|ventilator|heizung|rollladen|rollo)\b", body_wo_room)
        dev = find_device(re.sub(r"^(das|die|den|der) ", "", body_wo_room), room) if body_wo_room else None
        if dev:
            return Intent([("device_set", {"name": dev["name"], "room": dev["room"] or "", "type": "", "on": on, "brightness": None})])
        if tm:
            t = TYPES.get(tm.group(2).rstrip("n") if tm.group(2) not in TYPES else tm.group(2), "light")
            if not room:
                rs = rooms()
                if len(rs) == 1:
                    room = rs[0]
                elif not rs:
                    return Intent([("device_set", {"name": "", "room": "", "type": t, "on": on, "brightness": None})], confidence=0.7)
                else:
                    return Intent(reply=f"In welchem Zimmer? Ich kenne {', '.join(rs)}.", confidence=0.7)
            return Intent([("device_set", {"name": "", "room": room, "type": t, "on": on, "brightness": None})])
        return None

    def status(self):
        ds = devices()
        return {"rooms": len(rooms()), "devices": len(ds), "on": sum(d["state"] == "on" for d in ds),
                "homeassistant": bool(config.get("smarthome.homeassistant_url"))}

    def diagnose(self):
        out = [("Smart-Home-Geräte", True, f"{len(devices())} Geräte in {len(rooms())} Zimmern")]
        if config.get("smarthome.homeassistant_url"):
            ok, msg = PROVIDERS["homeassistant"].test()
            out.append(("Home Assistant", ok, msg))
        return out


def _orig(text, frag):
    i = text.lower().find(frag.lower())
    return text[i:i + len(frag)] if i >= 0 else frag
