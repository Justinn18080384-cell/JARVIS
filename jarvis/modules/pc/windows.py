"""Fensterverwaltung und Erkennung des aktiven Fensters."""
import ctypes
import os
import threading
import time

import psutil
import win32api
import win32con
import win32gui
import win32process

from ...core.text import similarity

OWN_PID = os.getpid()


def _proc_name(hwnd):
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return pid, psutil.Process(pid).name()
    except Exception:
        return 0, ""


def is_own(hwnd):
    pid, name = _proc_name(hwnd)
    if pid == OWN_PID:
        return True
    try:
        return psutil.Process(pid).ppid() == OWN_PID or "msedgewebview2" in name.lower() and \
            psutil.Process(psutil.Process(pid).ppid()).ppid() == OWN_PID
    except Exception:
        return False


def visible_windows():
    out = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title or title in ("Program Manager", "Windows Input Experience", "Microsoft Text Input Application"):
            return True
        ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        if ex & win32con.WS_EX_TOOLWINDOW:
            return True
        # unsichtbar "gecloakte" UWP-Fenster überspringen
        cloaked = ctypes.c_int(0)
        try:
            ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        except Exception:
            pass
        if cloaked.value:
            return True
        pid, name = _proc_name(hwnd)
        out.append({"hwnd": hwnd, "title": title, "pid": pid, "process": name})
        return True

    win32gui.EnumWindows(cb, None)
    return out


def info(hwnd):
    if not hwnd or not win32gui.IsWindow(hwnd):
        return None
    pid, name = _proc_name(hwnd)
    return {"hwnd": hwnd, "title": win32gui.GetWindowText(hwnd), "pid": pid, "process": name}


class ForegroundTracker:
    """Merkt sich das letzte Fenster, das NICHT JARVIS ist – für „schließ das“."""

    def __init__(self):
        self.last = None
        self.current = None
        threading.Thread(target=self._loop, daemon=True, name="fg-tracker").start()

    def _loop(self):
        while True:
            try:
                hwnd = win32gui.GetForegroundWindow()
                if hwnd and hwnd != self.current:
                    self.current = hwnd
                    if not is_own(hwnd) and win32gui.GetWindowText(hwnd):
                        self.last = hwnd
            except Exception:
                pass
            time.sleep(0.5)

    def target(self):
        if self.last and win32gui.IsWindow(self.last):
            return self.last
        return None


def find_window(query: str):
    best, score = None, 0
    for w in visible_windows():
        if is_own(w["hwnd"]):
            continue
        s = max(similarity(query, w["title"]), similarity(query, w["process"].rsplit(".", 1)[0]))
        if s > score:
            best, score = w, s
    return best if score >= 0.6 else None


def close(hwnd):
    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)


def minimize(hwnd):
    win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)


def maximize(hwnd):
    win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)


def restore(hwnd):
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    focus(hwnd)


def focus(hwnd):
    try:
        # Trick, damit SetForegroundWindow aus einem Hintergrundprozess funktioniert
        win32api.keybd_event(0x12, 0, 0, 0)
        win32gui.SetForegroundWindow(hwnd)
        win32api.keybd_event(0x12, 0, win32con.KEYEVENTF_KEYUP, 0)
    except Exception:
        pass


def minimize_all():
    import win32com.client
    win32com.client.Dispatch("Shell.Application").MinimizeAll()


def move_to_monitor(hwnd, index):
    import win32api as wa
    mons = wa.EnumDisplayMonitors()
    if index >= len(mons):
        return False
    left, top, right, bottom = mons[index][2]
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.SetWindowPos(hwnd, 0, left + 40, top + 40, (right - left) - 80, (bottom - top) - 80, 0)
    win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
    return True
