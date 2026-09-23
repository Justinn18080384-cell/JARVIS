"""Geheimnisse (API-Keys, Tokens) liegen in der Windows-Anmeldeinformationsverwaltung,
nicht im Klartext in einer Datei."""
import keyring

SERVICE = "JARVIS"


def get(name: str) -> str:
    try:
        return keyring.get_password(SERVICE, name) or ""
    except Exception:
        return ""


def set(name: str, value: str):
    if value:
        keyring.set_password(SERVICE, name, value)
    else:
        delete(name)


def delete(name: str):
    try:
        keyring.delete_password(SERVICE, name)
    except Exception:
        pass


def has(name: str) -> bool:
    return bool(get(name))
