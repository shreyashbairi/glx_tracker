# PyInstaller spec for GLX Tracker (macOS .app and Windows folder build).
# Build:  pyinstaller packaging/glx_tracker.spec --noconfirm
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent
sys.path.insert(0, str(ROOT))
from glx_tracker import __version__, APP_NAME, APP_ID  # noqa: E402

hidden = []
if sys.platform == "win32":
    hidden += ["pynput.keyboard._win32", "pynput.mouse._win32", "uiautomation", "keyring.backends.Windows"]
elif sys.platform == "darwin":
    hidden += ["keyring.backends.macOS", "Quartz", "AppKit"]

a = Analysis(
    [str(ROOT / "glx_tracker" / "__main__.py")],
    pathex=[str(ROOT)],
    hiddenimports=hidden,
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.Qt3DCore"],
    noarchive=False,
)
pyz = PYZ(a.pure)
icon = str(ROOT / "packaging" / ("icon.ico" if sys.platform == "win32" else "icon.png"))
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    console=False,
    icon=icon,
)
coll = COLLECT(exe, a.binaries, a.datas, name=APP_NAME)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=icon,
        bundle_identifier=APP_ID,
        version=__version__,
        info_plist={
            "LSUIElement": True,  # menu-bar app, no Dock icon
            "CFBundleShortVersionString": __version__,
            "NSAppleEventsUsageDescription": "GLX Tracker reads the address of the active browser tab to record which website (domain only) you work on.",
            "NSHumanReadableCopyright": "Globalex Trading DMCC",
        },
    )
