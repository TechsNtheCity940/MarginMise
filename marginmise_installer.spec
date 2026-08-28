#!/usr/bin/env python3
"""Lightweight installer EXE spec for MarginMise.

Builds a small installer EXE that installs the full app to
%LOCALAPPDATA%\MarginMise\ and sets up prerequisites.
"""
import sys
from pathlib import Path

hidden_imports = [
    'zipfile', 'tarfile', 'urllib.request', 'threading',
    'pathlib', 'datetime', 'subprocess', 'shutil', 'os', 'sys',
]

a = Analysis(
    ['launch_gui.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['hermes_agent', 'hermes_backend', 'hermes', 'tkinter', 'matplotlib'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='MarginMise-Install',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # Show console for installation progress
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/app_icon_256.png',
)
