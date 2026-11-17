# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec: single-file NSE MT5 Bridge executable.

import sys
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries, hiddenimports = [], [], []

for pkg in ("MetaTrader5", "numpy", "pywebview", "websockets", "uvicorn", "loguru"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

hiddenimports += [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "anyio._backends._asyncio",
]
hiddenimports += collect_submodules("webview.platforms")

datas += [("nse_bridge/gui/static", "nse_bridge/gui/static")]

a = Analysis(
    ["run_bridge.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pandas", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="NSE-MT5-Bridge",
    console=True,
    disable_windowed_traceback=False,
    upx=False,
)
