"""Einstiegspunkt von JARVIS."""
import os
import sys
import threading


def _single_instance():
    """Nur eine JARVIS-Instanz. Eine zweite holt das vorhandene Fenster nach vorne."""
    import win32api
    import win32event
    import winerror
    mutex = win32event.CreateMutex(None, False, "Local\\JARVIS_SingleInstance")
    show_evt = win32event.CreateEvent(None, False, False, "Local\\JARVIS_Show")
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS or \
            win32event.WaitForSingleObject(mutex, 0) == win32event.WAIT_TIMEOUT:
        win32event.SetEvent(show_evt)
        sys.exit(0)
    return mutex, show_evt


def _window_geometry(width=1320, height=840, margin=24):
    """Fenstergröße an den Arbeitsbereich des Hauptmonitors anpassen.

    pywebview rechnet in logischen Pixeln (Windows-Skalierung). Arbeitsbereich und DPI
    werden aus derselben Quelle gelesen, damit das Verhältnis unabhängig vom
    DPI-Modus des Prozesses stimmt – sonst ragt das rahmenlose Fenster bei 150 %
    Skalierung über den Bildschirm und die Titelleiste ist unerreichbar.
    """
    try:
        import ctypes
        import win32api
        mon = win32api.MonitorFromPoint((0, 0), 1)  # MONITOR_DEFAULTTOPRIMARY
        left, top, right, bottom = win32api.GetMonitorInfo(mon)["Work"]
        dpi_x, dpi_y = ctypes.c_uint(), ctypes.c_uint()
        ctypes.windll.shcore.GetDpiForMonitor(int(mon), 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y))
        scale = (dpi_x.value or 96) / 96
        wa_x, wa_y = left / scale, top / scale
        wa_w, wa_h = (right - left) / scale, (bottom - top) / scale
    except Exception:
        return {"width": width, "height": height, "min_size": (960, 640)}
    w = int(min(width, wa_w - 2 * margin))
    h = int(min(height, wa_h - 2 * margin))
    return {
        "width": w, "height": h,
        "x": int(wa_x + (wa_w - w) / 2), "y": int(wa_y + (wa_h - h) / 2),
        "min_size": (min(960, w), min(640, h)),
    }


def main():
    background = "--background" in sys.argv
    # Frühester Punkt: fehlgeschlagene Updates erkennen (Rollback)
    from jarvis.core import paths
    paths.ensure()
    from jarvis.modules import updater
    updater.startup_guard()

    handles = _single_instance()

    import webview
    from jarvis import APP_NAME, VERSION
    from jarvis.core import log
    from jarvis.core.config import config
    from jarvis.app import Jarvis
    from jarvis.api import API

    log.logger().info("JARVIS %s startet (%s)", VERSION, "Hintergrund" if background else "normal")
    jarvis = Jarvis(background=background)
    api = API(jarvis)
    hidden = background or config.get("app.start_minimized", False)

    window = webview.create_window(
        APP_NAME, url=str(paths.UI_DIR / "index.html"), js_api=api,
        **_window_geometry(), frameless=True, easy_drag=False,
        background_color="#03070c", hidden=hidden, text_select=True,
    )
    jarvis.window = jarvis_window = window
    jarvis.window_hidden = hidden

    def on_closing():
        if config.get("app.close_to_tray", True) and jarvis.tray:
            jarvis.hide()
            return False
        jarvis.quit()

    window.events.closing += on_closing

    def watch_show():
        import win32event
        while True:
            win32event.WaitForSingleObject(handles[1], win32event.INFINITE)
            jarvis.show()

    def started():
        threading.Thread(target=watch_show, daemon=True).start()
        jarvis.start_tray()
        jarvis.start_modules()

    webview.start(started, gui="edgechromium", debug="--debug" in sys.argv,
                  storage_path=str(paths.LOCAL / "webview"), private_mode=False)
    jarvis.quit()


if __name__ == "__main__":
    main()
