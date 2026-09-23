"""Audio, Energie, Bildschirm, Systeminformationen, Zwischenablage, Dateien."""
import ctypes
import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path

import psutil

from ...core import paths

CREATE_NO_WINDOW = 0x08000000


# ------------------------------------------------------------------ Audio
def _com():
    import comtypes
    try:
        comtypes.CoInitialize()
    except Exception:
        pass


def _endpoint():
    _com()
    from pycaw.pycaw import AudioUtilities
    return AudioUtilities.GetSpeakers().EndpointVolume


def get_volume() -> int:
    return round(_endpoint().GetMasterVolumeLevelScalar() * 100)


def set_volume(pct: float):
    ep = _endpoint()
    ep.SetMasterVolumeLevelScalar(max(0.0, min(1.0, pct / 100)), None)
    if pct > 0:
        ep.SetMute(0, None)


def set_mute(mute: bool):
    _endpoint().SetMute(1 if mute else 0, None)


def is_muted() -> bool:
    return bool(_endpoint().GetMute())


def output_devices():
    _com()
    from pycaw.pycaw import AudioUtilities
    default = AudioUtilities.GetSpeakers().id
    devs = []
    for d in AudioUtilities.GetAllDevices(data_flow=0, device_state=1):
        if d.FriendlyName:
            devs.append({"id": d.id, "name": d.FriendlyName, "default": d.id == default})
    return devs


def input_devices():
    _com()
    from pycaw.pycaw import AudioUtilities
    return [{"id": d.id, "name": d.FriendlyName} for d in AudioUtilities.GetAllDevices(data_flow=1, device_state=1)
            if d.FriendlyName]


def set_output_device(dev_id: str):
    _com()
    from pycaw.pycaw import AudioUtilities
    from pycaw.constants import ERole
    AudioUtilities.SetDefaultDevice(dev_id, roles=[ERole.eConsole, ERole.eMultimedia, ERole.eCommunications])


def media_key(key: str):
    codes = {"play": 0xB3, "next": 0xB0, "prev": 0xB1, "stop": 0xB2}
    vk = codes[key]
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk, 0, 2, 0)


# ----------------------------------------------------------------- Energie
def lock():
    ctypes.windll.user32.LockWorkStation()


def shutdown(restart=False, delay=10):
    subprocess.run(["shutdown", "/r" if restart else "/s", "/t", str(int(delay))], creationflags=CREATE_NO_WINDOW)


def abort_shutdown():
    return subprocess.run(["shutdown", "/a"], creationflags=CREATE_NO_WINDOW).returncode == 0


def sleep():
    subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], creationflags=CREATE_NO_WINDOW)


def monitors_off():
    ctypes.windll.user32.SendMessageW(0xFFFF, 0x0112, 0xF170, 2)


# ------------------------------------------------------------ Bildschirm
def monitors():
    import mss
    with mss.MSS() if hasattr(mss, "MSS") else mss.mss() as s:
        return [{"index": i, "width": m["width"], "height": m["height"], "left": m["left"], "top": m["top"]}
                for i, m in enumerate(s.monitors[1:])]


def screenshot(monitor: int | None = None, folder: Path | None = None) -> Path:
    import mss
    import mss.tools
    folder = folder or paths.SCREENSHOT_DIR
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / time.strftime("Screenshot_%Y-%m-%d_%H-%M-%S.png")
    with (mss.MSS() if hasattr(mss, "MSS") else mss.mss()) as s:
        mon = s.monitors[0] if monitor is None else s.monitors[min(monitor + 1, len(s.monitors) - 1)]
        img = s.grab(mon)
        mss.tools.to_png(img.rgb, img.size, output=str(out))
    return out


def screenshot_bytes(max_width=1600) -> bytes:
    """Screenshot als JPEG für die KI-Analyse (verkleinert)."""
    import io
    import mss
    from PIL import Image
    with (mss.MSS() if hasattr(mss, "MSS") else mss.mss()) as s:
        img = s.grab(s.monitors[0])
        im = Image.frombytes("RGB", img.size, img.rgb)
    if im.width > max_width:
        im = im.resize((max_width, int(im.height * max_width / im.width)))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    return buf.getvalue()


# ---------------------------------------------------------- Systeminfos
_gpu_cache = {"t": 0, "v": None}


def gpu():
    if time.time() - _gpu_cache["t"] < 5:
        return _gpu_cache["v"]
    val = None
    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            out = subprocess.run([smi, "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                                  "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3,
                                 creationflags=CREATE_NO_WINDOW).stdout.strip().splitlines()[0]
            name, util, mu, mt, temp = [x.strip() for x in out.split(",")]
            val = {"name": name, "load": float(util), "mem_used": float(mu), "mem_total": float(mt), "temp": float(temp)}
        except Exception:
            pass
    _gpu_cache.update(t=time.time(), v=val)
    return val


_static = {}


def static_info():
    if not _static:
        name = platform.processor()
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                name = winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
        except Exception:
            pass
        _static.update(cpu_name=name, cores=psutil.cpu_count(), os=f"Windows {platform.release()} ({platform.version()})",
                       host=platform.node(), ram_total=psutil.virtual_memory().total)
    return _static


def stats():
    vm = psutil.virtual_memory()
    disks = []
    for p in psutil.disk_partitions(all=False):
        if "cdrom" in p.opts or not p.fstype:
            continue
        try:
            u = psutil.disk_usage(p.mountpoint)
            disks.append({"mount": p.mountpoint, "used": u.used, "total": u.total, "percent": u.percent})
        except Exception:
            continue
    bat = psutil.sensors_battery()
    net = psutil.net_io_counters()
    return {
        "cpu": psutil.cpu_percent(interval=None),
        "ram": vm.percent, "ram_used": vm.used, "ram_total": vm.total,
        "disks": disks,
        "battery": {"percent": bat.percent, "plugged": bat.power_plugged} if bat else None,
        "uptime": time.time() - psutil.boot_time(),
        "net_sent": net.bytes_sent, "net_recv": net.bytes_recv,
        "gpu": gpu(),
        "procs": len(psutil.pids()),
        **static_info(),
    }


def top_processes(n=5):
    procs = []
    for p in psutil.process_iter(["name", "memory_info"]):
        try:
            procs.append((p.info["memory_info"].rss, p.info["name"]))
        except Exception:
            continue
    procs.sort(reverse=True)
    return procs[:n]


# ----------------------------------------------------------- Zwischenablage
def clipboard_get() -> str:
    import win32clipboard
    win32clipboard.OpenClipboard()
    try:
        if win32clipboard.IsClipboardFormatAvailable(13):
            return win32clipboard.GetClipboardData(13)
        return ""
    finally:
        win32clipboard.CloseClipboard()


def clipboard_set(text: str):
    import win32clipboard
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(text, 13)
    finally:
        win32clipboard.CloseClipboard()


# ------------------------------------------------------------------ Dateien
def known_folder(name: str):
    home = Path.home()
    try:
        import win32com.shell.shell as sh
        import win32com.shell.shellcon as sc
        def kf(csidl):
            return Path(sh.SHGetFolderPath(0, csidl, None, 0))
        mapping = {
            "desktop": kf(sc.CSIDL_DESKTOP), "dokumente": kf(sc.CSIDL_PERSONAL),
            "bilder": kf(sc.CSIDL_MYPICTURES), "musik": kf(sc.CSIDL_MYMUSIC), "videos": kf(sc.CSIDL_MYVIDEO),
        }
    except Exception:
        mapping = {"desktop": home / "Desktop", "dokumente": home / "Documents", "bilder": home / "Pictures",
                   "musik": home / "Music", "videos": home / "Videos"}
    mapping.update({
        "downloads": home / "Downloads", "download": home / "Downloads",
        "benutzerordner": home, "home": home, "screenshots": paths.SCREENSHOT_DIR,
        "programme": Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")),
        "appdata": Path(os.environ.get("APPDATA", "")), "temp": Path(os.environ.get("TEMP", "")),
        "papierkorb": "shell:RecycleBinFolder", "autostart": "shell:startup",
        "dieser pc": "shell:MyComputerFolder", "arbeitsplatz": "shell:MyComputerFolder",
    })
    aliases = {"dokument": "dokumente", "documents": "dokumente", "schreibtisch": "desktop", "pictures": "bilder",
               "fotos": "bilder", "music": "musik", "video": "videos", "filme": "videos", "jarvis": None}
    key = name.lower().strip()
    key = aliases.get(key, key)
    if name.lower().strip() == "jarvis":
        return paths.DATA
    if re.fullmatch(r"[a-z]", key or ""):
        return Path(f"{key.upper()}:\\")
    return mapping.get(key)


def search_windows_index(query: str, limit=15, kind=None):
    """Schnelle Suche über den Windows-Suchindex."""
    try:
        import win32com.client
        conn = win32com.client.Dispatch("ADODB.Connection")
        conn.Open("Provider=Search.CollatorDSO;Extended Properties='Application=Windows';")
        q = query.replace("'", "''")
        where = f"CONTAINS(System.FileName, '\"*{q}*\"')"
        if kind == "folder":
            where += " AND System.ItemType = 'Directory'"
        sql = (f"SELECT TOP {limit} System.ItemPathDisplay, System.DateModified FROM SYSTEMINDEX "
               f"WHERE {where} AND SCOPE='file:{Path.home().as_posix()}' ORDER BY System.DateModified DESC")
        rs, _ = conn.Execute(sql)
        out = []
        while not rs.EOF:
            out.append(rs.Fields.Item("System.ItemPathDisplay").Value)
            rs.MoveNext()
        conn.Close()
        return out
    except Exception:
        return None


def search_files(query: str, limit=15, kind=None):
    res = search_windows_index(query, limit, kind)
    if res:
        return res
    # Fallback: gezielte Suche in Nutzerordnern
    from ...core.text import ascii_fold
    q = ascii_fold(query)
    roots = [Path.home() / d for d in ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos")]
    out, start = [], time.time()
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("node_modules", "AppData")]
            names = dirnames if kind == "folder" else filenames + dirnames
            for n in names:
                if q in ascii_fold(n):
                    out.append(str(Path(dirpath) / n))
                    if len(out) >= limit:
                        return out
            if time.time() - start > 8:
                return out
    return out


def recycle(path: str):
    """Datei in den Papierkorb verschieben (niemals endgültig löschen)."""
    from win32com.shell import shell, shellcon
    flags = shellcon.FOF_ALLOWUNDO | shellcon.FOF_NOCONFIRMATION | shellcon.FOF_SILENT | shellcon.FOF_NOERRORUI
    res = shell.SHFileOperation((0, shellcon.FO_DELETE, path, None, flags, None, None))
    return res[0] == 0
