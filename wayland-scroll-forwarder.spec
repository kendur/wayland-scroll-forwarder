# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec: single self-contained executable, no host Python modules needed.
#   dist/wayland-scroll-forwarder
from PyInstaller.utils.hooks import collect_submodules

hidden = collect_submodules("evdev") + collect_submodules("Xlib")

a = Analysis(
    ["scroll_forwarder.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pydoc", "test"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="wayland-scroll-forwarder",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)
