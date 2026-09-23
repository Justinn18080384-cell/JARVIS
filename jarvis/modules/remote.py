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
"""
import base64
import datetime
import io
import ipaddress
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
    ts = tailscale_name()
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


class RemoteModule(Module):
    name = "remote"
    title = "Fernzugriff / Handy-App"

    def __init__(self, jarvis):
        super().__init__(jarvis)
        self.server = None
        self.state = "idle"
        self.clients = {}
        bus.on("state", lambda e, d: setattr(self, "state", d.get("state", "idle")))

    def start(self):
        if not config.get("remote.token"):
            config.set("remote.token", pysecrets.token_urlsafe(24))
        if config.get("remote.enabled"):
            self.enable()

    def new_token(self):
        config.set("remote.token", pysecrets.token_urlsafe(24))
        log.activity("fernzugriff", "Neuer Zugangs-Token erstellt – alte Kopplungen sind ungültig")

    # ------------------------------------------------------------- Befehle
    def run_command(self, text, source="remote", timeout=60):
        replies, done = [], threading.Event()

        def cb(t):
            if t is None:
                done.set()
            else:
                replies.append(t)
        self.jarvis.brain.submit(text, source=source, on_reply=cb)
        done.wait(timeout)
        return " ".join(replies) or ("(JARVIS arbeitet noch …)" if not done.is_set() else "Erledigt.")

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
            return bottle.static_file("remote.html", root=str(paths.UI_DIR))

        @app.get("/manifest.webmanifest")
        def manifest():
            bottle.response.content_type = "application/manifest+json"
            return {"name": "JARVIS", "short_name": "JARVIS", "start_url": "/", "display": "standalone",
                    "background_color": "#03070c", "theme_color": "#03070c",
                    "icons": [{"src": "/icon.png", "sizes": "512x512", "type": "image/png"}]}

        @app.get("/icon.png")
        def icon():
            return bottle.static_file("jarvis.png", root=str(paths.ASSETS_DIR))

        @app.get("/api/status")
        def status():
            auth()
            return mod.status_payload()

        @app.post("/api/command")
        def command():
            auth()
            text = ((bottle.request.json or {}).get("text") or "").strip()[:2000]
            return {"reply": mod.run_command(text)}

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
