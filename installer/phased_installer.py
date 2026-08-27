#!/usr/bin/env python3
"""MarginMise phased installer for low-spec restaurant PCs.

Installs components in small batches with delays between phases
to avoid overwhelming limited hardware.
"""
from __future__ import annotations

import os
import sys
import time
import subprocess
import threading
import traceback
from pathlib import Path
from datetime import datetime

APP_NAME = "MarginMise"
LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
INSTALL_DIR = LOCALAPPDATA / APP_NAME
LOG_DIR = INSTALL_DIR / "Logs"
BOOTSTRAP_LOG = LOG_DIR / "phased_install.log"
INSTALL_FLAG = INSTALL_DIR / ".installed"
PHASE_FLAG = INSTALL_DIR / ".phase"


def log_event(message: str) -> None:
    stamp = datetime.now().isoformat(timespec="seconds")
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with BOOTSTRAP_LOG.open("a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {message}\n")
    except Exception:
        pass


def get_venv_python() -> Path | None:
    return INSTALL_DIR / ".venv" / "Scripts" / "python.exe"


def run_phase(name: str, func) -> bool:
    """Run a single installation phase with error handling."""
    log_event(f"=== PHASE: {name} ===")
    try:
        result = func()
        if result:
            log_event(f"Phase '{name}' completed successfully")
            PHASE_FLAG.write_text(name, encoding="utf-8")
        else:
            log_event(f"Phase '{name}' failed")
        return result
    except Exception as e:
        log_event(f"Phase '{name}' error: {e}\n{traceback.format_exc()}")
        return False


def phase_1_python_venv() -> bool:
    """Phase 1: Install Python and create virtual environment."""
    log_event("Phase 1: Python + venv")
    
    # Check if Python exists
    try:
        result = subprocess.run(
            ["python", "--version"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            log_event(f"Python found: {result.stdout.strip()}")
        else:
            log_event("Python not found, will install")
    except Exception:
        log_event("Python check failed, will install")
    
    # Find or install Python
    python = None
    for name in ["python", "python3", "python3.11", "python3.12"]:
        try:
            result = subprocess.run(
                [name, "--version"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                python = name
                break
        except Exception:
            continue
    
    if not python:
        log_event("Installing Python via winget...")
        result = subprocess.run(
            ["winget", "install", "--id", "Python.Python.3.12", "--exact",
             "--scope", "user", "--silent", "--disable-interactivity",
             "--accept-package-agreements", "--accept-source-agreements"],
            capture_output=True, text=True, timeout=300
        )
        if result.returncode != 0:
            log_event(f"Python install failed: {result.stderr}")
            return False
        python = "python"
    
    # Create virtual environment
    venv_dir = INSTALL_DIR / ".venv"
    if not (venv_dir / "Scripts" / "python.exe").exists():
        log_event(f"Creating venv at {venv_dir}")
        result = subprocess.run(
            [python, "-m", "venv", str(venv_dir)],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            log_event(f"Venv creation failed: {result.stderr}")
            return False
        log_event("Virtual environment created")
    
    # Upgrade pip slowly
    venv_python = get_venv_python()
    if venv_python and venv_python.exists():
        log_event("Upgrading pip...")
        subprocess.run(
            [str(venv_python), "-m", "pip", "install", "--upgrade", "pip"],
            capture_output=True, text=True, timeout=120
        )
    
    return True


def phase_2_core_deps() -> bool:
    """Phase 2: Install core dependencies in small batches."""
    log_event("Phase 2: Core dependencies")
    venv_python = get_venv_python()
    if not venv_python or not venv_python.exists():
        return False
    
    # Install in small batches to avoid memory spikes
    batches = [
        # Batch 1: Core data/IO
        [
            "openpyxl>=3.1,<4",
            "python-docx>=1.1,<2",
            "certifi>=2025.0.0",
        ],
        # Batch 2: OCR/PDF
        [
            "PyMuPDF>=1.24,<2",
            "rapidocr>=3.8,<4",
            "onnxruntime>=1.20,<2",
        ],
        # Batch 3: Visualization/reporting
        [
            "matplotlib>=3.8,<4",
            "reportlab>=5.0,<6",
        ],
        # Batch 4: System/runtime
        [
            "psutil>=5.9,<6",
            "pywin32>=306",
            "Pillow>=10.0,<11",
        ],
    ]
    
    for i, batch in enumerate(batches, 1):
        log_event(f"Installing batch {i}/{len(batches)}: {', '.join(batch)}")
        result = subprocess.run(
            [str(venv_python), "-m", "pip", "install", "--disable-pip-version-check", "--no-input"] + batch,
            capture_output=True, text=True, timeout=300
        )
        if result.returncode != 0:
            log_event(f"Batch {i} failed: {result.stderr}")
            # Continue with next batch rather than failing completely
        else:
            log_event(f"Batch {i} installed successfully")
        
        # Pause between batches to let the PC recover
        if i < len(batches):
            time.sleep(5)
    
    return True


def phase_3_tesseract() -> bool:
    """Phase 3: Install Tesseract OCR (can be skipped if not needed)."""
    log_event("Phase 3: Tesseract OCR")
    venv_python = get_venv_python()
    if not venv_python or not venv_python.exists():
        return False
    
    # Try to install Tesseract, but don't fail if it doesn't work
    result = subprocess.run(
        [str(venv_python), "-m", "local_ocr", "ensure", "--install-tesseract"],
        cwd=str(INSTALL_DIR),
        capture_output=True, text=True, timeout=300
    )
    log_event(f"Tesseract install result: {result.returncode}")
    return True  # Non-critical, always succeed


def phase_4_ai_model() -> bool:
    """Phase 4: Install local AI model (can be skipped if not needed)."""
    log_event("Phase 4: AI Model")
    venv_python = get_venv_python()
    if not venv_python or not venv_python.exists():
        return False
    
    # Try to install AI model, but don't fail if it doesn't work
    result = subprocess.run(
        [str(venv_python), "-m", "local_ai", "ensure"],
        cwd=str(INSTALL_DIR),
        capture_output=True, text=True, timeout=600
    )
    log_event(f"AI model install result: {result.returncode}")
    return True  # Non-critical, always succeed


def run_all_phases() -> bool:
    """Run all installation phases sequentially."""
    phases = [
        ("python_venv", phase_1_python_venv),
        ("core_deps", phase_2_core_deps),
        ("tesseract", phase_3_tesseract),
        ("ai_model", phase_4_ai_model),
    ]
    
    for name, func in phases:
        log_event(f"Starting phase: {name}")
        success = run_phase(name, func)
        if not success:
            log_event(f"Phase {name} failed, but continuing...")
        
        # Pause between phases to let the PC recover
        time.sleep(3)
    
    # Mark as installed
    INSTALL_FLAG.write_text("1", encoding="utf-8")
    PHASE_FLAG.unlink(missing_ok=True)
    log_event("=== ALL PHASES COMPLETE ===")
    return True


def launch_gui() -> int:
    """Launch the MarginMise GUI."""
    try:
        venv_python = get_venv_python()
        if not venv_python or not venv_python.exists():
            log_event("Virtual environment not found, cannot launch GUI")
            return 1
        
        gui_script = INSTALL_DIR / "launch_gui.py"
        if not gui_script.exists():
            # Fallback to main GUI
            gui_script = INSTALL_DIR / "restaurant_cost_gui.py"
        
        if not gui_script.exists():
            log_event(f"GUI script not found")
            return 1
        
        log_event(f"Launching GUI: {gui_script}")
        result = subprocess.run(
            [str(venv_python), str(gui_script)],
            cwd=str(INSTALL_DIR)
        )
        return result.returncode

    except Exception as e:
        log_event(f"GUI launch failed: {e}")
        return 1


def main() -> int:
    """Main entry point."""
    log_event("=== MarginMise Phased Installer Started ===")
    log_event(f"Install directory: {INSTALL_DIR}")
    
    # Run all phases
    success = run_all_phases()
    
    if not success:
        log_event("Some phases failed, but attempting to launch anyway")
    
    # Launch GUI
    return launch_gui()


if __name__ == "__main__":
    raise SystemExit(main())
