#!/usr/bin/env python3
"""PyInstaller spec for MarginMise — standalone Windows .exe build."""
#
# To build: pyinstaller marginmise.spec
#
# This creates a one-file, windowed executable with all Python modules
# and assets bundled. On first run, the exe will:
#   1. Extract itself to %LOCALAPPDATA%\MarginMise
#   2. Download Tesseract OCR (silent)
#   3. Download llama.cpp + LFM2.5 model (silent, ~250MB)
# No internet required after initial setup.

import sys
import os
from pathlib import Path

# All internal modules must be explicitly listed (no hidden imports)
hidden_imports = [
    'invoice_pipeline',
    'bulk_ingestion',
    'recipe_costing',
    'margin_memory',
    'manager_chat',
    'local_ai',
    'local_ocr',
    'inventory_planning',
    'phase2_features',
    'phase3_features',
    'operational_controls',
    'excel_io',
    'auto_upload',
    'dashboard_service',
    'dashboard_widgets',
    'review_copilot',
    'launch_gui',
    'restaurant_cost_gui',
    'manager_first_gui',
    'events',
    'shift_reports',
    'weekly_invoice_log',
    'src.theme',
    'bootstrap',
]

# Collect all data files from assets directory
datas = [(str(p), str(p.parent if p.is_file() else p)) for p in Path('assets').rglob('*') if p.is_file()]

a = Analysis(
    ['bootstrap.py'],
    pathex=['.'],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['hermes_agent', 'hermes_backend', 'hermes'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='MarginMise',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # Windowed — no console popup
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/app_icon_256.png',
)

