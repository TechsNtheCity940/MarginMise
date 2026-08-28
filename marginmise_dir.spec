#!/usr/bin/env python3
"""PyInstaller spec for MarginMise — folder-based Windows build (memory-safe)."""
import sys
import os
from pathlib import Path

hidden_imports = [
    'PIL',
    'rapidocr',
    'onnxruntime',
    'matplotlib.backends.backend_tkagg',
    'matplotlib.figure',
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
]

datas = [(str(p), str(p.parent.relative_to(Path('.'))) if p.parent != Path('.') else '.') for p in Path("assets").rglob("*") if p.is_file()]

a = Analysis(
    ['launch_gui.py'],
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

# Folder-based build uses less memory than onefile
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='MarginMise',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # Disable UPX to reduce memory pressure
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/favicon.ico',
)

# Create a distributable folder
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='MarginMise',
)
