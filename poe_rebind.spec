# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for poe_rebind.
# Build:  .venv\Scripts\python.exe -m PyInstaller poe_rebind.spec

import sys
from pathlib import Path

block_cipher = None


a = Analysis(
    ["rebind.py"],
    pathex=[str(Path(".").resolve())],
    binaries=[],
    datas=[],
    hiddenimports=[
        "nodriver",
        "websockets",
        "websockets.legacy",
        "cv2",
        "mss",
    ],
    hookspath=[],
    hooksconfig={"nodriver": {}},
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="poe_rebind",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,  # keep console visible for log output
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    name="poe_rebind",
)
