"""Web-Push: Meldungen von JARVIS als echte Push-Nachricht aufs Handy – auch wenn die App geschlossen ist.

Standard-Web-Push (RFC 8030/8291/8292) über die Push-Dienste von Apple bzw. Google.
Der Inhalt ist Ende-zu-Ende verschlüsselt (nur das Handy kann ihn lesen), angemeldet wird
mit einem eigenen VAPID-Schlüssel, der nur auf diesem PC liegt.
iPhone: ab iOS 16.4, und nur wenn JARVIS zum Home-Bildschirm hinzugefügt wurde.
"""
import base64
import ctypes
import json
import os
import struct
import threading
import time
from urllib.parse import urlparse

from ..core import log
from ..core.config import config
from ..core.db import db

db.schema("""CREATE TABLE IF NOT EXISTS push_subs(
    endpoint TEXT PRIMARY KEY, p256dh TEXT, auth TEXT, device TEXT, created REAL, last_ok REAL, fails INTEGER DEFAULT 0
)""")

_key_lock = threading.Lock()
_recent = {}


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _vapid_key():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    with _key_lock:
        pem = db.kv_get("push.vapid_key")
        if pem:
            return serialization.load_pem_private_key(pem.encode(), None)
        key = ec.generate_private_key(ec.SECP256R1())
        db.kv_set("push.vapid_key", key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                      serialization.NoEncryption()).decode())
        return key


def _raw_pub(key) -> bytes:
    from cryptography.hazmat.primitives import serialization
    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def public_key() -> str:
    """Öffentlicher VAPID-Schlüssel für pushManager.subscribe (applicationServerKey)."""
    return _b64u(_raw_pub(_vapid_key()))


def encrypt(p256dh: str, auth: str, payload: bytes, salt: bytes | None = None, server_key=None) -> bytes:
    """Nachricht für ein Abo verschlüsseln (aes128gcm, RFC 8291)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    ua_pub = _b64u_dec(p256dh)
    auth_secret = _b64u_dec(auth)
    as_key = server_key or ec.generate_private_key(ec.SECP256R1())
    as_pub = _raw_pub(as_key)
    shared = as_key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub))
    ikm = HKDF(hashes.SHA256(), 32, auth_secret, b"WebPush: info\x00" + ua_pub + as_pub).derive(shared)
    salt = salt or os.urandom(16)
    cek = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
    ct = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + struct.pack("!I", 4096) + bytes([len(as_pub)]) + as_pub + ct


def _vapid_auth(endpoint: str) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    key = _vapid_key()
    u = urlparse(endpoint)
    # Apple lehnt „mailto:…@localhost“ ab – daher die Projektseite als Kontakt
    sub = config.get("update.url") or "https://github.com"
    claims = {"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + 12 * 3600, "sub": sub}
    head = _b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    body = _b64u(json.dumps(claims, separators=(",", ":")).encode())
    r, s = decode_dss_signature(key.sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256())))
    sig = _b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={head}.{body}.{sig}, k={_b64u(_raw_pub(key))}"


# ------------------------------------------------------------------ Abos der Handys
def subscribe(sub: dict, device=""):
    keys = sub.get("keys") or {}
    ep = sub.get("endpoint", "")
    if not ep.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
        raise ValueError("Ungültiges Push-Abo")
    db.execute("INSERT OR REPLACE INTO push_subs(endpoint, p256dh, auth, device, created, fails) VALUES(?,?,?,?,?,0)",
               (ep, keys["p256dh"], keys["auth"], (device or "")[:80], time.time()))
    log.activity("fernzugriff", f"Push-Nachrichten aktiviert ({device or 'Handy'})")


def unsubscribe(endpoint):
    db.execute("DELETE FROM push_subs WHERE endpoint=?", (endpoint,))


def subscriptions():
    return db.query("SELECT endpoint, device, created, last_ok, fails FROM push_subs")


def send(title, body, tag=None, urgent=False, url="/"):
    """An alle registrierten Handys senden. Gibt (erfolgreich, fehlgeschlagen) zurück."""
    import requests
    subs = db.query("SELECT * FROM push_subs")
    if not subs:
        return 0, 0
    payload = json.dumps({"title": title or "JARVIS", "body": (body or "")[:400], "tag": tag, "url": url},
                         ensure_ascii=False).encode()
    ok = bad = 0
    for s in subs:
        try:
            data = encrypt(s["p256dh"], s["auth"], payload)
            r = requests.post(s["endpoint"], data=data, timeout=15, headers={
                "Authorization": _vapid_auth(s["endpoint"]), "TTL": "86400", "Content-Encoding": "aes128gcm",
                "Content-Type": "application/octet-stream", "Urgency": "high" if urgent else "normal"})
            if r.status_code in (200, 201, 202):
                ok += 1
                db.execute("UPDATE push_subs SET last_ok=?, fails=0 WHERE endpoint=?", (time.time(), s["endpoint"]))
            elif r.status_code in (404, 410):
                bad += 1
                unsubscribe(s["endpoint"])   # Handy hat das Abo beendet
                log.logger().info("Push-Abo abgelaufen und entfernt (%s)", s["device"])
            else:
                bad += 1
                log.logger().warning("Push fehlgeschlagen (%s): %s %s", s["device"], r.status_code, r.text[:200])
                db.execute("UPDATE push_subs SET fails=fails+1 WHERE endpoint=?", (s["endpoint"],))
        except Exception as e:
            bad += 1
            log.logger().warning("Push fehlgeschlagen (%s): %s", s["device"], e)
    return ok, bad


def idle_seconds():
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
    li = LASTINPUTINFO()
    li.cbSize = ctypes.sizeof(li)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li)):
        return 0
    tick = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
    return ((tick - li.dwTime) & 0xFFFFFFFF) / 1000   # Zähler läuft nach 49 Tagen über


def user_away():
    """Nicht am PC: seit 5 Minuten keine Eingabe (gilt auch, wenn der PC gesperrt ist)."""
    try:
        return idle_seconds() > 300
    except Exception:
        return False


def on_notify(event, data):
    """Angezeigte Meldungen (nach der Modus-Prüfung) zusätzlich als Push senden."""
    if not data.get("_routed") or not config.get("remote.push", True):
        return
    prio = int(data.get("priority", 0))
    if prio < int(config.get("remote.push_min_priority", 0)):
        return
    if config.get("remote.push_mode", "away") == "away" and prio < 2 and not user_away():
        return
    text = data.get("text", "")
    key = (data.get("title"), text)
    now = time.time()
    if now - _recent.get(key, 0) < 60:
        return   # dieselbe Meldung nicht mehrfach
    _recent[key] = now
    for k in [k for k, t in _recent.items() if now - t > 600]:
        _recent.pop(k, None)
    if not db.one("SELECT COUNT(*) AS n FROM push_subs")["n"]:
        return
    threading.Thread(target=send, args=(data.get("title") or "JARVIS", text), kwargs={"urgent": prio >= 2},
                     daemon=True, name="push").start()
