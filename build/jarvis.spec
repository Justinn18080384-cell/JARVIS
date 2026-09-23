# PyInstaller-Spezifikation für JARVIS (onedir – schneller Start, saubere Updates)
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, collect_dynamic_libs

ROOT = Path(SPECPATH).parent
import openwakeword
OWW = Path(openwakeword.__file__).parent / "resources" / "models"

datas = [
    (str(ROOT / "jarvis" / "ui"), "ui"),
    (str(ROOT / "jarvis" / "assets"), "assets"),
]
for f in ("hey_jarvis_v0.1.onnx", "melspectrogram.onnx", "embedding_model.onnx"):
    datas.append((str(OWW / f), "openwakeword/resources/models"))
datas += collect_data_files("faster_whisper")
datas += collect_data_files("webview")
datas += collect_data_files("certifi")

binaries = collect_dynamic_libs("ctranslate2") + collect_dynamic_libs("onnxruntime")

hiddenimports = (
    collect_submodules("jarvis")
    + collect_submodules("webview.platforms")
    + ["clr", "pystray._win32", "win32timezone", "comtypes.stream", "keyring.backends.Windows",
       "edge_tts", "miniaudio", "sounddevice", "faster_whisper", "ctranslate2", "onnxruntime",
       "pythoncom", "pywintypes", "win32com.shell.shell", "win32com.shell.shellcon", "bottle", "wsgiref.simple_server",
       "cryptography", "qrcode", "qrcode.image.pil", "faster_whisper.audio", "av", "pypdf"]
    + collect_submodules("qrcode")
)

a = Analysis(
    [str(ROOT / "run.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "sklearn", "scipy", "pandas", "IPython", "pytest", "torch", "tensorflow", "tflite_runtime"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="JARVIS",
    icon=str(ROOT / "jarvis" / "assets" / "jarvis.ico"),
    console=False,
    version=str(ROOT / "build" / "version_info.txt"),
    uac_admin=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="JARVIS")
