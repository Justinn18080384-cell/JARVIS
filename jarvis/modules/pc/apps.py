"""Programmindex: Startmenü, Store-Apps, Steam-Spiele, Desktop.
Wird regelmäßig neu aufgebaut, damit neu installierte Programme automatisch erkannt werden."""
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

import psutil

from ...core import paths, log
from ...core.text import similarity, ascii_fold

INDEX_FILE = paths.CACHE_DIR / "apps.json"
CREATE_NO_WINDOW = 0x08000000

SKIP = re.compile(r"(uninstall|deinstall|entfernen|readme|hilfe|help|documentation|dokumentation|"
                  r"license|lizenz|release notes|website|support|manual|handbuch|changelog)", re.I)

# Bekannte Sprachvarianten -> Suchbegriff
ALIASES = {
    "browser": "chrome", "internet": "edge", "rechner": "rechner", "taschenrechner": "rechner",
    "editor": "editor", "notizblock": "editor", "explorer": "explorer", "datei explorer": "explorer",
    "einstellungen": "einstellungen", "task manager": "task-manager", "taskmanager": "task-manager",
    "terminal": "terminal", "eingabeaufforderung": "eingabeaufforderung", "ts": "teamspeak",
    "teamspeak": "teamspeak", "vs code": "visual studio code", "vscode": "visual studio code",
    "word": "word", "excel": "excel", "gta 5": "grand theft auto v", "gta v": "grand theft auto v",
    "gta": "grand theft auto v", "cs2": "counter-strike 2", "counter strike": "counter-strike 2",
}

BUILTIN = [
    {"name": "Explorer", "kind": "exe", "target": "explorer.exe", "exe": "explorer.exe"},
    {"name": "Task-Manager", "kind": "exe", "target": "taskmgr.exe", "exe": "taskmgr.exe"},
    {"name": "Einstellungen", "kind": "uri", "target": "ms-settings:", "exe": "systemsettings.exe"},
    {"name": "Rechner", "kind": "exe", "target": "calc.exe", "exe": "calculatorapp.exe"},
    {"name": "Editor", "kind": "exe", "target": "notepad.exe", "exe": "notepad.exe"},
    {"name": "Eingabeaufforderung", "kind": "exe", "target": "cmd.exe", "exe": "cmd.exe"},
    {"name": "Systemsteuerung", "kind": "exe", "target": "control.exe", "exe": "control.exe"},
    {"name": "Paint", "kind": "exe", "target": "mspaint.exe", "exe": "mspaint.exe"},
]


class AppIndex:
    def __init__(self):
        self.apps: list[dict] = []
        self.lock = threading.Lock()
        self.last_scan = 0
        self.new_apps: list[str] = []
        self._load()

    def _load(self):
        try:
            data = json.loads(INDEX_FILE.read_text("utf-8"))
            self.apps = data["apps"]
            self.last_scan = data.get("ts", 0)
        except Exception:
            self.apps = []

    def _save(self):
        INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
        INDEX_FILE.write_text(json.dumps({"ts": self.last_scan, "apps": self.apps}, ensure_ascii=False), "utf-8")

    # ---------------------------------------------------------------- scan
    def scan(self):
        found: dict[str, dict] = {}

        def add(app):
            key = ascii_fold(app["name"])
            if not key or SKIP.search(app["name"]):
                return
            if key not in found or (found[key]["kind"] == "appid" and app["kind"] != "appid"):
                found[key] = {**found.get(key, {}), **app}

        for b in BUILTIN:
            add(dict(b, source="system"))
        for app in self._scan_shortcuts():
            add(app)
        for app in self._scan_start_apps():
            key = ascii_fold(app["name"])
            if key in found:
                found[key].setdefault("appid", app["target"])
            else:
                add(app)
        for app in self._scan_steam():
            add(app)

        apps = sorted(found.values(), key=lambda a: a["name"].lower())
        with self.lock:
            old = {ascii_fold(a["name"]) for a in self.apps}
            new = [a["name"] for a in apps if ascii_fold(a["name"]) not in old]
            if self.apps and new:
                self.new_apps = new
            self.apps = apps
            self.last_scan = time.time()
            self._save()
        return new if old else []

    def _scan_shortcuts(self):
        dirs = [
            Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / r"Microsoft\Windows\Start Menu\Programs",
            Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs",
            Path.home() / "Desktop",
            Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop",
        ]
        try:
            import win32com.client
            shell = win32com.client.Dispatch("WScript.Shell")
        except Exception:
            shell = None
        for d in dirs:
            if not d.exists():
                continue
            for p in d.rglob("*"):
                if p.suffix.lower() not in (".lnk", ".url", ".appref-ms"):
                    continue
                name = p.stem
                target, args, exe = str(p), "", ""
                if p.suffix.lower() == ".lnk" and shell:
                    try:
                        sc = shell.CreateShortcut(str(p))
                        tp = sc.TargetPath or ""
                        if tp.lower().endswith(".exe"):
                            exe = Path(tp).name.lower()
                    except Exception:
                        pass
                elif p.suffix.lower() == ".url":
                    try:
                        txt = p.read_text("utf-8", errors="ignore")
                        m = re.search(r"URL=steam://rungameid/(\d+)", txt)
                        if m:
                            yield {"name": name, "kind": "steam", "target": m.group(1), "exe": "", "source": "steam"}
                            continue
                    except Exception:
                        pass
                yield {"name": name, "kind": "shortcut", "target": target, "args": args, "exe": exe,
                       "source": "startmenü" if "Start Menu" in str(p) else "desktop"}

    def _scan_start_apps(self):
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress"],
                capture_output=True, text=True, timeout=30, creationflags=CREATE_NO_WINDOW,
                encoding="utf-8", errors="ignore")
            data = json.loads(out.stdout or "[]")
            if isinstance(data, dict):
                data = [data]
            for a in data:
                if a.get("Name") and a.get("AppID"):
                    yield {"name": a["Name"], "kind": "appid", "target": a["AppID"], "exe": "", "source": "store"}
        except Exception as e:
            log.logger().warning("Get-StartApps fehlgeschlagen: %s", e)

    def _scan_steam(self):
        for lib in steam_libraries():
            steamapps = Path(lib) / "steamapps"
            for acf in steamapps.glob("appmanifest_*.acf"):
                try:
                    txt = acf.read_text("utf-8", errors="ignore")
                    appid = re.search(r'"appid"\s+"(\d+)"', txt).group(1)
                    name = re.search(r'"name"\s+"([^"]+)"', txt).group(1)
                    inst = re.search(r'"installdir"\s+"([^"]+)"', txt).group(1)
                    if re.search(r"redistributable|steamworks|proton|runtime", name, re.I):
                        continue
                    yield {"name": name, "kind": "steam", "target": appid, "exe": "",
                           "dir": str(steamapps / "common" / inst), "source": "steam"}
                except Exception:
                    continue

    # --------------------------------------------------------------- query
    def find(self, query: str, limit=5):
        q = query.strip().lower()
        q = ALIASES.get(q, q)
        with self.lock:
            scored = []
            for a in self.apps:
                s = similarity(q, a["name"])
                if a.get("exe") and ascii_fold(q) == ascii_fold(Path(a["exe"]).stem):
                    s = max(s, 0.95)
                scored.append((s, a))
        scored.sort(key=lambda x: (-x[0], len(x[1]["name"])))
        return [(s, a) for s, a in scored[:limit] if s > 0.5]

    def get(self, name: str):
        key = ascii_fold(name)
        with self.lock:
            for a in self.apps:
                if ascii_fold(a["name"]) == key:
                    return a
        return None


def steam_path():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            return winreg.QueryValueEx(k, "SteamPath")[0]
    except Exception:
        p = Path(r"C:\Program Files (x86)\Steam")
        return str(p) if p.exists() else None


def steam_libraries():
    sp = steam_path()
    libs = []
    if sp:
        libs.append(sp)
        vdf = Path(sp) / "steamapps" / "libraryfolders.vdf"
        try:
            for m in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text("utf-8", errors="ignore")):
                libs.append(m.group(1).replace("\\\\", "\\"))
        except Exception:
            pass
    return list(dict.fromkeys(libs))


def launch(app: dict, args: str = ""):
    kind, target = app["kind"], app["target"]
    if kind == "steam":
        os.startfile(f"steam://rungameid/{target}")
    elif kind == "appid":
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{target}"], creationflags=CREATE_NO_WINDOW)
    elif kind == "uri":
        os.startfile(target)
    elif kind == "path":
        if args:
            subprocess.Popen(f'"{target}" {args}', cwd=str(Path(target).parent), shell=False)
        else:
            os.startfile(target)
    elif kind == "exe":
        subprocess.Popen(f"{target} {args}".strip(), shell=True, creationflags=CREATE_NO_WINDOW)
    else:  # shortcut
        if args:
            # Argumente an eine Verknüpfung: Ziel auflösen
            try:
                import win32com.client
                sc = win32com.client.Dispatch("WScript.Shell").CreateShortcut(target)
                subprocess.Popen(f'"{sc.TargetPath}" {sc.Arguments} {args}', cwd=sc.WorkingDirectory or None)
                return
            except Exception:
                pass
        os.startfile(target)


def processes_for(app: dict):
    """Findet laufende Prozesse eines Programms."""
    exe = (app.get("exe") or "").lower()
    d = (app.get("dir") or "").lower()
    name = ascii_fold(app["name"])
    procs = []
    for p in psutil.process_iter(["name", "exe"]):
        try:
            pn = (p.info["name"] or "").lower()
            pe = (p.info["exe"] or "").lower()
            if exe and pn == exe:
                procs.append(p)
            elif d and pe.startswith(d):
                procs.append(p)
            elif not exe and not d and pn and similarity(name, Path(pn).stem) > 0.85:
                procs.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return procs
