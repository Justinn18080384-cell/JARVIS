"""Zentrale Pfade. Persönliche Daten liegen getrennt von den Programmdateien,
damit Updates und Neuinstallationen sie nie überschreiben."""
import os
import sys
from pathlib import Path


def app_dir() -> Path:
    """Verzeichnis der Programmdateien (bei PyInstaller: _internal bzw. exe-Ordner)."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def exe_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    return Path(sys.executable)


def install_dir() -> Path:
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else app_dir().parent


DATA = Path(os.environ.get("JARVIS_DATA") or Path(os.environ.get("APPDATA", Path.home())) / "JARVIS")
LOCAL = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "JARVIS"

CONFIG_FILE = DATA / "settings.json"
DB_FILE = DATA / "jarvis.db"
LOG_DIR = DATA / "logs"
BACKUP_DIR = DATA / "backups"
CACHE_DIR = LOCAL / "cache"
MODELS_DIR = LOCAL / "models"
TOOLS_DIR = LOCAL / "tools"
ROLLBACK_DIR = LOCAL / "rollback"
UPDATE_DIR = LOCAL / "updates"
SCREENSHOT_DIR = Path.home() / "Pictures" / "JARVIS"

UI_DIR = app_dir() / "ui"
ASSETS_DIR = app_dir() / "assets"


def ensure():
    for p in (DATA, LOG_DIR, BACKUP_DIR, CACHE_DIR, MODELS_DIR, TOOLS_DIR, UPDATE_DIR):
        p.mkdir(parents=True, exist_ok=True)
