"""Fernzugriff: JARVIS vom Handy aus steuern (wie Alexa) – im Heimnetz, geschützt per Token.

Standardmäßig AUS. Wenn aktiviert, stellt JARVIS unter https://<PC-IP>:<Port>/ eine Handy-Web-App bereit
(zum Startbildschirm hinzufügbar) mit Text- und Spracheingabe, Statusanzeige, Geräten und Routinen.
HTTPS (selbstsigniertes Zertifikat) ist nötig, weil Handy-Browser das Mikrofon sonst nicht freigeben.
Kopplung per QR-Code: Der Token steht im URL-Fragment (#t=…) und wird nie an den Server übertragen.

API (Header „Authorization: Bearer <token>“):
    GET  /api/status                     Status von JARVIS, PC, Geräten, Routinen
    POST /api/command   {text}           Befehl ausführen -> {reply}
    POST /api/voice     (Audiodatei)     Sprachbefehl -> {transcript, reply}
    POST /api/device/<id>/toggle         Smart-Home-Gerät umschalten
    POST /api/automation/<id>/run        Profil/Routine starten
    POST /api/stop                       Not-Aus
    GET  /api/activity                   letzte Aktivitäten
    GET  /api/actions                    alle Fähigkeiten von JARVIS
    POST /api/action   {name, args}      eine Fähigkeit ausführen (gleiche Sicherheitsabfrage wie per Sprache)
    POST /api/call/<methode> {args}      dieselben Daten wie die PC-Oberfläche (Startseite, Finanzen, Gedächtnis …)
    GET  /api/screenshot                 aktuelles Bild vom PC
    POST /api/volume {value} · /api/media {key}
    POST /api/finance/import (CSV)       Kontoauszug vom Handy importieren
    GET  /api/push/key · POST /api/push/subscribe · /api/push/unsubscribe · /api/push/test
"""
import base64
import datetime
import io
import json
import ipaddress
import re
import secrets as pysecrets
import socket
import ssl
import threading
import time

from ..core import log, paths
from ..core.config import config
from ..core.db import db
from ..core.events import bus
from ..core.module import Module

CERT_DIR = paths.LOCAL / "remote"


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # es wird nichts gesendet
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def ensure_cert():
    """Erzeugt ein selbstsigniertes Zertifikat für die Handy-App (einmalig bzw. bei neuer IP)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    cert_f, key_f, ip_f = CERT_DIR / "cert.pem", CERT_DIR / "key.pem", CERT_DIR / "ip.txt"
    ip = lan_ip()
    if cert_f.exists() and key_f.exists() and ip_f.exists() and ip_f.read_text() == ip:
        return str(cert_f), str(key_f)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "JARVIS")])
    san = [x509.DNSName("localhost"), x509.DNSName(socket.gethostname()), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    try:
        san.append(x509.IPAddress(ipaddress.ip_address(ip)))
    except ValueError:
        pass
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(san), critical=False).sign(key, hashes.SHA256()))
    cert_f.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_f.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                        serialization.NoEncryption()))
    ip_f.write_text(ip)
    return str(cert_f), str(key_f)


# ------------------------------------------------------------------ Tailscale (Zugriff von unterwegs)
TAILSCALE_EXE = r"C:\Program Files\Tailscale\tailscale.exe"
_ts_cache = (0.0, None)


def tailscale_name():
    """MagicDNS-Name dieses PCs im Tailscale-Netz (z. B. jarvis-pc.tailxxxx.ts.net) – oder None."""
    global _ts_cache
    ts, name = _ts_cache
    if time.time() - ts < 300:
        return name
    name = None
    try:
        import json
        import subprocess
        import os
        if os.path.exists(TAILSCALE_EXE):
            out = subprocess.run([TAILSCALE_EXE, "status", "--json"], capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=8, creationflags=0x08000000).stdout
            st = json.loads(out)
            if st.get("BackendState") == "Running" and st.get("Self", {}).get("Online"):
                name = (st["Self"].get("DNSName") or "").rstrip(".") or None
    except Exception as e:
        log.logger().info("Tailscale nicht verfügbar: %s", e)
    _ts_cache = (time.time(), name)
    return name


def tailscale_cert():
    """Echtes Zertifikat (Let's Encrypt) von Tailscale holen bzw. erneuern – Pfade (cert, key) oder None."""
    import os
    import subprocess
    name = tailscale_name()
    if not name:
        return None
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    cert_f, key_f = CERT_DIR / "ts-cert.pem", CERT_DIR / "ts-key.pem"
    fresh = cert_f.exists() and time.time() - cert_f.stat().st_mtime < 30 * 86400
    if not fresh:
        try:
            r = subprocess.run([TAILSCALE_EXE, "cert", "--cert-file", str(cert_f), "--key-file", str(key_f), name],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, creationflags=0x08000000)
            if r.returncode != 0:
                log.logger().warning("Tailscale-Zertifikat: %s", (r.stderr or r.stdout).strip()[:200])
        except Exception as e:
            log.logger().warning("Tailscale-Zertifikat: %s", e)
    return (str(cert_f), str(key_f)) if cert_f.exists() and key_f.exists() else None


def base_url():
    """Adresse der Handy-App: über Tailscale (zu Hause und unterwegs) oder im Heimnetz."""
    port = config.get("remote.port", 8765)
    ts = tailscale_name() if config.get("remote.https", True) else None
    if ts and (CERT_DIR / "ts-cert.pem").exists():
        return f"https://{ts}:{port}"
    scheme = "https" if config.get("remote.https", True) else "http"
    return f"{scheme}://{lan_ip()}:{port}"


def pairing_url():
    return f"{base_url()}/#t={config.get('remote.token')}"


def pairing_qr():
    import qrcode
    img = qrcode.make(pairing_url(), border=2)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


# Methoden der PC-Oberfläche, die auch die Handy-App nutzen darf (keine Fenster-/Datei-Dialoge, keine Schlüssel)
BRIDGE = {"home", "dashboard", "finance", "finance_add", "finance_delete", "finance_update", "finance_budget", "finance_sub_add",
          "finance_sub_remove", "focus_set", "missed_clear", "memory_list", "memory_set", "memory_delete", "automations",
          "automation_run", "smarthome", "device_toggle", "activity", "diagnose", "update_check", "update_install", "apps",
          "backups", "backup_create"}


class RemoteModule(Module):
    name = "remote"
    title = "Fernzugriff / Handy-App"

    def __init__(self, jarvis):
        super().__init__(jarvis)
        self.server = None
        self.state = "idle"
        self.clients = {}
        bus.on("state", lambda e, d: setattr(self, "state", d.get("state", "idle")))
        bus.on("notify", self._push)

    def _push(self, event, data):
        if config.get("remote.enabled"):
            from . import push
            push.on_notify(event, data)

    def start(self):
        if not config.get("remote.token"):
            config.set("remote.token", pysecrets.token_urlsafe(24))
        if config.get("remote.enabled"):
            self.enable()

    def new_token(self):
        config.set("remote.token", pysecrets.token_urlsafe(24))
        log.activity("fernzugriff", "Neuer Zugangs-Token erstellt – alte Kopplungen sind ungültig")

    # 6-stelliger Kopplungscode (10 Minuten gültig, nach 5 Fehlversuchen verbraucht) – für die
    # Home-Bildschirm-App auf dem iPhone, die die Anmeldung aus Safari nicht übernimmt
    _pair = (None, 0.0, 0)

    def pair_code(self):
        code, until, fails = self._pair
        if not code or time.time() > until - 60:
            code = f"{pysecrets.randbelow(1_000_000):06d}"
            self._pair = (code, time.time() + 600, 0)
        return code

    def check_pair(self, code):
        want, until, fails = self._pair
        if not want or time.time() > until or fails >= 5:
            return None
        if pysecrets.compare_digest(str(code).strip(), want):
            self._pair = (None, 0.0, 0)
            log.activity("fernzugriff", "Handy per Kopplungscode angemeldet")
            return config.get("remote.token")
        self._pair = (want, until, fails + 1)
        return None

    # ------------------------------------------------------------- Befehle
    def run_command(self, text, source="remote", timeout=60, calls=None):
        replies, done = [], threading.Event()

        def cb(t):
            if t is None:
                done.set()
            else:
                replies.append(t)
        if calls:
            self.jarvis.brain.submit_calls(calls, source=source, on_reply=cb)
        else:
            self.jarvis.brain.submit(text, source=source, on_reply=cb)
        done.wait(timeout)
        return " ".join(replies) or ("(JARVIS arbeitet noch …)" if not done.is_set() else "Erledigt.")

    def reply_payload(self, reply):
        """Antwort plus offene Rückfrage (Ja/Nein-Knöpfe bzw. Auswahl am Handy)."""
        p = self.jarvis.brain.ctx.pending
        out = {"reply": reply}
        if p and p.get("type") == "confirm":
            out["confirm"] = True
        elif p and p.get("type") == "choose":
            out["choose"] = [str(o) for o in p.get("options", [])][:8]
        return out

    def status_payload(self):
        from .pc import system
        from . import smarthome, automation
        s = system.stats()
        return {
            "name": "JARVIS", "user": config.get("user_name"), "version": self.jarvis.api_status()["version"],
            "state": "halted" if self.jarvis.brain.halted else self.state,
            "pc": {"host": s["host"], "cpu": s["cpu"], "ram": s["ram"], "gpu": s["gpu"], "battery": s["battery"],
                   "volume": _safe(system.get_volume)},
            "rooms": smarthome.rooms(),
            "devices": [{k: d[k] for k in ("id", "name", "room", "type", "state")} for d in smarthome.devices()],
            "automations": [{"id": a["id"], "name": a["name"], "type": a["type"]} for a in automation.load_all() if a.get("enabled", True)],
            "ai": self.jarvis.modules["ai"].available() if "ai" in self.jarvis.modules else False,
            "focus": self.jarvis.modules["focus"].status() if "focus" in self.jarvis.modules else {},
            "busy": self.jarvis.brain.busy,
        }

    # -------------------------------------------------------------- Server
    def enable(self):
        if self.server:
            return True
        from socketserver import ThreadingMixIn
        from wsgiref.simple_server import make_server, WSGIRequestHandler, WSGIServer
        import bottle

        app = bottle.Bottle()
        mod = self
        from ..api import API
        japi = API(self.jarvis)

        def auth():
            tok = bottle.request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if not tok or not pysecrets.compare_digest(tok, config.get("remote.token") or ""):
                ip = bottle.request.remote_addr
                log.logger().warning("Fernzugriff: ungültiger Token von %s", ip)
                time.sleep(0.5)  # Brute-Force bremsen
                bottle.abort(401, "Nicht autorisiert")
            mod.clients[bottle.request.remote_addr] = time.time()

        @app.get("/")
        def index():
            # Nie zwischenspeichern – sonst läuft die Home-Bildschirm-App nach einem Update mit altem Stand weiter
            r = bottle.static_file("remote.html", root=str(paths.UI_DIR))
            r.set_header("Cache-Control", "no-store, must-revalidate")
            r.set_header("Pragma", "no-cache")
            r.set_header("Expires", "0")
            return r

        @app.get("/manifest.webmanifest")
        def manifest():
            bottle.response.content_type = "application/manifest+json"
            # Home-Bildschirm-Apps haben auf dem iPhone einen eigenen Speicher – der Zugang muss in der Start-Adresse stecken
            t = bottle.request.query.get("t", "")
            start = f"/#t={t}" if t and pysecrets.compare_digest(t, config.get("remote.token") or "") else "/"
            return {"id": "/", "name": "JARVIS", "short_name": "JARVIS", "start_url": start, "scope": "/", "display": "standalone",
                    "background_color": "#060504", "theme_color": "#060504",
                    "icons": [{"src": "/icon.png", "sizes": "512x512", "type": "image/png"}]}

        @app.get("/icon.png")
        def icon():
            return bottle.static_file("jarvis.png", root=str(paths.ASSETS_DIR))

        @app.post("/api/pair")
        def pair():
            code = re.sub(r"\D", "", str((bottle.request.json or {}).get("code", "")))
            tok = mod.check_pair(code) if len(code) == 6 else None
            if not tok:
                time.sleep(1.0)     # Raten bremsen
                return {"ok": False}
            return {"ok": True, "token": tok}

        @app.get("/api/status")
        def status():
            auth()
            return mod.status_payload()

        @app.get("/sw.js")
        def service_worker():
            r = bottle.static_file("remote-sw.js", root=str(paths.UI_DIR))
            r.set_header("Cache-Control", "no-cache")
            r.set_header("Service-Worker-Allowed", "/")
            return r

        @app.post("/api/command")
        def command():
            auth()
            text = ((bottle.request.json or {}).get("text") or "").strip()[:2000]
            return mod.reply_payload(mod.run_command(text))

        # ------------------------------------------------ alles, was JARVIS am PC kann
        @app.get("/api/actions")
        def actions():
            auth()
            from ..core.actions import registry
            return {"items": [{"name": a.name, "description": a.description, "module": a.module, "risk": a.risk,
                               "params": a.params, "required": a.required} for a in registry.all()]}

        @app.post("/api/action")
        def run_action():
            auth()
            from ..core.actions import registry
            body = bottle.request.json or {}
            name, args = body.get("name", ""), body.get("args") or {}
            act = registry.get(name)
            if not act or not isinstance(args, dict):
                bottle.abort(404, "Unbekannte Aktion")
            args = {k: v for k, v in args.items() if k in act.params and v not in (None, "")}
            missing = [k for k in act.required if k not in args]
            if missing:
                return {"reply": "Es fehlt noch: " + ", ".join(act.params.get(k, {}).get("description", k) for k in missing)}
            return mod.reply_payload(mod.run_command(None, calls=[(name, args)]))

        @app.post("/api/call/<name>")
        def api_call(name):
            auth()
            if name not in BRIDGE:
                bottle.abort(404)
            args = (bottle.request.json or {}).get("args") or []
            res = getattr(japi, name)(*args[:6])
            bottle.response.content_type = "application/json"
            return json.dumps({"result": res}, default=str, ensure_ascii=False)

        @app.get("/api/screenshot")
        def screenshot():
            auth()
            from .pc import system
            bottle.response.content_type = "image/jpeg"
            bottle.response.set_header("Cache-Control", "no-store")
            log.activity("fernzugriff", "Bildschirmfoto ans Handy gesendet")
            return system.screenshot_bytes(1600)

        @app.post("/api/volume")
        def volume():
            auth()
            from .pc import system
            body = bottle.request.json or {}
            if "mute" in body:
                system.set_mute(bool(body["mute"]))
            if "value" in body:
                system.set_volume(max(0, min(100, float(body["value"]))))
            return {"volume": _safe(system.get_volume), "muted": _safe(system.is_muted)}

        @app.post("/api/media")
        def media():
            auth()
            from .pc import system
            key = (bottle.request.json or {}).get("key")
            if key not in ("play", "next", "prev", "stop"):
                bottle.abort(400)
            system.media_key(key)
            return {"ok": True}

        @app.post("/api/finance/import")
        def finance_import():
            auth()
            from . import finance
            try:
                r = finance.import_statement(bottle.request.body.read(15 * 2**20))
            except ValueError as e:
                return {"ok": False, "msg": str(e)}
            return {"ok": True, "msg": f"{r['added']} neue Buchungen importiert" + (f", {r['skipped']} waren schon da." if r["skipped"] else ".")}

        # ------------------------------------------- Jarvis ohne PC (Solo-Modus der Handy-App)
        @app.get("/api/sync")
        def sync_pull():
            """Alles, was das Handy braucht, um ohne PC weiterzuarbeiten."""
            auth()
            from . import memory, finance
            f = finance.overview()
            bottle.response.content_type = "application/json"
            return json.dumps({
                "user": config.get("user_name"), "version": mod.jarvis.api_status()["version"],
                "model": config.get("ai.model") or "gpt-4.1-mini",
                "key_shared": bool(config.get("remote.share_ai_key")),
                "memory": [{"label": m["label"], "value": m["value"]} for m in memory.all_facts()][:300],
                "finance": {k: f[k] for k in ("month_name", "month_spent", "month_earned", "prev_spent", "by_category",
                                              "budgets", "subs", "subs_monthly", "recent", "categories")},
                "time": time.time(),
            }, default=str, ensure_ascii=False)

        @app.post("/api/sync")
        def sync_push():
            """Was das Handy ohne PC erledigt hat, nachtragen (jede Änderung nur einmal)."""
            auth()
            from . import memory, finance, costs
            done = set(db.kv_get("sync.done", []))
            ok = []
            for op in ((bottle.request.json or {}).get("ops") or [])[:500]:
                oid = str(op.get("id", ""))[:64]
                if not oid or oid in done:
                    ok.append(oid)
                    continue
                try:
                    kind = op.get("op")
                    if kind == "finance_add":
                        val = abs(float(op["amount"])) * (1 if op.get("kind") == "einnahme" else -1)
                        finance.add_tx(val, op.get("category"), op.get("note", ""), day=op.get("day"), source="handy",
                                       tx_hash=f"handy-{oid}")
                    elif kind == "memory_set":
                        memory.remember(op["label"], op["value"])
                    elif kind == "cost":
                        p_in, p_cached, p_out = costs._price(op.get("model"))
                        usd = (int(op.get("tin", 0)) * p_in + int(op.get("tout", 0)) * p_out) / 1_000_000 \
                            + int(op.get("searches", 0)) * costs.WEB_SEARCH_USD + float(op.get("seconds", 0)) / 60 * costs.TRANSCRIBE_USD_PER_MIN
                        costs.record("openai", op.get("kind") or "Handy ohne PC", op.get("model"),
                                     int(op.get("tin", 0)) + int(op.get("tout", 0)), "Tokens", usd)
                    elif kind == "chat":
                        log.activity("handy", f"Ohne PC gefragt: {str(op.get('text', ''))[:150]}")
                    done.add(oid)
                    ok.append(oid)
                except Exception as e:
                    log.error("Handy-Abgleich", e)
            db.kv_set("sync.done", list(done)[-2000:])
            if ok:
                log.logger().info("Handy-Abgleich: %d Änderungen übernommen", len(ok))
            return {"ok": ok}

        @app.get("/api/solo/key")
        def solo_key():
            auth()
            from ..core import secrets
            if not config.get("remote.share_ai_key"):
                return {"ok": False, "msg": "Am PC ist die Übergabe des Schlüssels ausgeschaltet (JARVIS → Handy)."}
            key = secrets.get("openai_api_key")
            if not key:
                return {"ok": False, "msg": "Am PC ist kein OpenAI-Schlüssel eingerichtet."}
            log.activity("fernzugriff", "OpenAI-Schlüssel an die Handy-App übergeben (für Jarvis ohne PC)")
            return {"ok": True, "key": key}

        # ------------------------------------------------------------ Push
        @app.get("/api/push/key")
        def push_key():
            auth()
            from . import push
            return {"key": push.public_key(), "enabled": bool(config.get("remote.push", True)),
                    "mode": config.get("remote.push_mode", "away"), "devices": len(push.subscriptions())}

        @app.post("/api/push/subscribe")
        def push_subscribe():
            auth()
            from . import push
            body = bottle.request.json or {}
            try:
                push.subscribe(body.get("subscription") or {}, body.get("device", ""))
            except ValueError as e:
                return {"ok": False, "msg": str(e)}
            return {"ok": True}

        @app.post("/api/push/unsubscribe")
        def push_unsubscribe():
            auth()
            from . import push
            push.unsubscribe((bottle.request.json or {}).get("endpoint", ""))
            return {"ok": True}

        @app.post("/api/push/test")
        def push_test():
            auth()
            from . import push
            ok, bad = push.send("JARVIS", "Test: Push-Nachrichten funktionieren.", tag="test")
            return {"ok": ok > 0, "sent": ok, "failed": bad}

        @app.post("/api/push/mode")
        def push_mode():
            auth()
            body = bottle.request.json or {}
            if body.get("mode") in ("away", "always"):
                config.set("remote.push_mode", body["mode"])
            if "enabled" in body:
                config.set("remote.push", bool(body["enabled"]))
            return {"ok": True}

        @app.post("/api/voice")
        def voice():
            auth()
            data = bottle.request.body.read(15 * 2**20)
            ctype = bottle.request.content_type or "audio/webm"
            ext = "mp4" if "mp4" in ctype or "aac" in ctype else "ogg" if "ogg" in ctype else "wav" if "wav" in ctype else "webm"
            try:
                text = mod.jarvis.modules["voice"].transcribe_bytes(data, f"audio.{ext}").strip()
            except Exception as e:
                return {"transcript": "", "reply": "Ich konnte die Aufnahme nicht verstehen: " + log.error("Handy-Spracheingabe", e)}
            if not text:
                return {"transcript": "", "reply": "Ich habe nichts verstanden."}
            return {"transcript": text, "reply": mod.run_command(text)}

        @app.post("/api/device/<did>/toggle")
        def toggle(did):
            auth()
            from . import smarthome
            d = db.one("SELECT * FROM devices WHERE id=?", (did,))
            if not d:
                bottle.abort(404)
            try:
                smarthome._set(d, d["state"] != "on")
            except Exception as e:
                return {"ok": False, "error": log.friendly(e)}
            return {"ok": True}

        @app.post("/api/automation/<aid>/run")
        def run(aid):
            auth()
            from . import automation
            a = next((x for x in automation.load_all() if x["id"] == aid), None)
            if not a:
                bottle.abort(404)
            res = automation._mod.run(a)
            return {"reply": res if isinstance(res, str) else res.get("text", "")}

        @app.post("/api/stop")
        def stop():
            auth()
            mod.jarvis.brain.emergency_stop()
            return {"ok": True}

        @app.post("/api/resume")
        def resume():
            auth()
            mod.jarvis.brain.resume()
            return {"ok": True}

        @app.get("/api/activity")
        def activity():
            auth()
            return {"items": db.query("SELECT * FROM activity ORDER BY id DESC LIMIT 50")}

        class Quiet(WSGIRequestHandler):
            def log_message(self, *a):
                pass

        class Server(ThreadingMixIn, WSGIServer):
            daemon_threads = True

        try:
            srv = make_server("0.0.0.0", int(config.get("remote.port", 8765)), app, server_class=Server, handler_class=Quiet)
            if config.get("remote.https", True):
                # Bevorzugt das echte Tailscale-Zertifikat (nötig für Push-Nachrichten auf dem iPhone),
                # sonst ein selbstsigniertes fürs Heimnetz
                cert, key = tailscale_cert() or ensure_cert()
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain(cert, key)
                srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
                self._ssl = ctx
            self.server = srv
            threading.Thread(target=srv.serve_forever, daemon=True, name="remote-api").start()
            threading.Thread(target=self._renew_loop, daemon=True, name="remote-cert").start()
            log.activity("fernzugriff", f"Handy-App aktiv: {pairing_url().split('#')[0]}")
            return True
        except Exception as e:
            self.server = None
            log.error("Fernzugriff", e)
            return False

    def _renew_loop(self):
        """Tailscale-Zertifikat täglich prüfen und bei Bedarf erneuern (gilt 90 Tage)."""
        while self.server:
            time.sleep(86400)
            try:
                paths_ = tailscale_cert()
                if paths_ and getattr(self, "_ssl", None):
                    self._ssl.load_cert_chain(*paths_)     # neue Verbindungen nutzen das erneuerte Zertifikat
            except Exception as e:
                log.logger().warning("Zertifikat erneuern: %s", e)

    def disable(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
            log.activity("fernzugriff", "Handy-App deaktiviert")

    def status(self):
        active = [ip for ip, t in self.clients.items() if time.time() - t < 120]
        return {"enabled": bool(self.server), "port": config.get("remote.port"), "clients": len(active)}

    def diagnose(self):
        if not config.get("remote.enabled"):
            return [("Handy-App", True, "deaktiviert")]
        return [("Handy-App", bool(self.server), pairing_url().split("#")[0] if self.server else "Server konnte nicht starten (Port belegt?)")]


def _safe(fn):
    try:
        return fn()
    except Exception:
        return None
