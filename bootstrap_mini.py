#!/usr/bin/env python3
"""Lightweight MarginMise installer EXE for restaurant PCs.

This is a minimal bootstrapper that:
1. Extracts the full app to %LOCALAPPDATA%\MarginMise
2. Sets up Python environment if needed
3. Downloads prerequisites
4. Launches the GUI

The EXE is built from this file only, keeping it small.
"""
from __future__ import annotations

import os
import sys
import subprocess
import threading
import traceback
import zipfile
import tarfile
from pathlib import Path
from datetime import datetime
from urllib.request import urlretrieve

APP_NAME = "MarginMise"
LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
INSTALL_DIR = LOCALAPPDATA / APP_NAME
LOG_DIR = INSTALL_DIR / "Logs"
BOOTSTRAP_LOG = LOG_DIR / "bootstrap_mini.log"
INSTALL_FLAG = INSTALL_DIR / ".installed"

# URLs for downloading components
GITHUB_RELEASES_URL = "https://github.com/TechsNtheCity940/MarginMise/releases/latest/download"
AI_MODEL_URL = f"{GITHUB_RELEASES_URL}/ai-model.tar.gz"
TESSERACT_URL = f"{GITHUB_RELEASES_URL}/tesseract.zip"


def log_event(message: str) -> None:
    stamp = datetime.now().isoformat(timespec="seconds")
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with BOOTSTRAP_LOG.open("a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {message}\n")
    except Exception:
        pass


def download_file(url: str, destination: Path, description: str) -> bool:
    """Download a file with progress logging."""
    try:
        log_event(f"Downloading {description} from {url}")
        urlretrieve(url, destination)
        log_event(f"Downloaded {description} to {destination}")
        return True
    except Exception as e:
        log_event(f"Failed to download {description}: {e}")
        return False


def extract_zip(zip_path: Path, extract_to: Path) -> bool:
    """Extract a ZIP file."""
    try:
        log_event(f"Extracting {zip_path} to {extract_to}")
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(extract_to)
        log_event("Extraction complete")
        return True
    except Exception as e:
        log_event(f"Failed to extract ZIP: {e}")
        return False


def extract_tar(tar_path: Path, extract_to: Path) -> bool:
    """Extract a tar.gz file."""
    try:
        log_event(f"Extracting {tar_path} to {extract_to}")
        with tarfile.open(tar_path, 'r:gz') as tf:
            tf.extractall(extract_to)
        log_event("Extraction complete")
        return True
    except Exception as e:
        log_event(f"Failed to extract tar.gz: {e}")
        return False


def find_python() -> Path | None:
    """Find a usable Python installation."""
    for name in ["python", "python3", "python3.11", "python3.12"]:
        path = shutil.which(name)
        if path:
            p = Path(path)
            try:
                result = subprocess.run([str(p), "--version"], capture_output=True, text=True, timeout=10)
                if result.returncode == 0:
                    return p
            except Exception:
                continue
    return None


def install_python() -> Path | None:
    """Try to install Python using winget."""
    try:
        log_event("Installing Python via winget...")
        subprocess.run(
            ["winget", "install", "--id", "Python.Python.3.12", "--exact", "--scope", "user",
             "--silent", "--disable-interactivity", "--accept-package-agreements", "--accept-source-agreements"],
            capture_output=True, text=True, timeout=300
        )
        return find_python()
    except Exception as e:
        log_event(f"Python install failed: {e}")
        return None


def setup_venv(python: Path) -> Path:
    """Create virtual environment and install dependencies."""
    venv_dir = INSTALL_DIR / ".venv"
    venv_python = venv_dir / "Scripts" / "python.exe"

    if not venv_python.exists():
        log_event(f"Creating virtual environment at {venv_dir}")
        subprocess.run([str(python), "-m", "venv", str(venv_dir)], check=True)
        log_event("Virtual environment created")

    # Upgrade pip
    subprocess.run([str(venv_python), "-m", "pip", "install", "--upgrade", "pip"], 
                   capture_output=True, text=True)

    # Install dependencies
    req_file = INSTALL_DIR / "requirements.txt"
    if req_file.exists():
        log_event("Installing dependencies...")
        result = subprocess.run(
            [str(venv_python), "-m", "pip", "install", "-r", str(req_file)],
            capture_output=True, text=True, timeout=600
        )
        log_event(f"Dependencies installed (exit {result.returncode})")

    return venv_python


def install_tesseract(venv_python: Path) -> None:
    """Install Tesseract OCR."""
    try:
        log_event("Installing Tesseract OCR...")
        subprocess.run(
            [str(venv_python), "-m", "local_ocr", "ensure", "--install-tesseract"],
            cwd=str(INSTALL_DIR), capture_output=True, text=True, timeout=300
        )
        log_event("Tesseract installation attempted")
    except Exception as e:
        log_event(f"Tesseract install error: {e}")


def install_ai_model(venv_python: Path) -> None:
    """Install local AI model."""
    try:
        log_event("Installing local AI model...")
        subprocess.run(
            [str(venv_python), "-m", "local_ai", "ensure"],
            cwd=str(INSTALL_DIR), capture_output=True, text=True, timeout=600
        )
        log_event("AI model installation attempted")
    except Exception as e:
        log_event(f"AI model install error: {e}")


def run_bootstrap() -> None:
    """Run the full installation process."""
    log_event("=== MarginMise Bootstrap Started ===")
    log_event(f"Install directory: {INSTALL_DIR}")

    try:
        # Create install directory
        INSTALL_DIR.mkdir(parents=True, exist_ok=True)

        # Find Python
        python = find_python()
        if not python:
            python = install_python()
        if not python:
            raise RuntimeError("Python 3.11+ not found")

        # Setup venv
        venv_python = setup_venv(python)

        # Install Tesseract
        install_tesseract(venv_python)

        # Install AI model
        install_ai_model(venv_python)

        # Mark as installed
        INSTALL_FLAG.write_text("1", encoding="utf-8")
        log_event("=== Bootstrap Complete ===")

    except Exception as e:
        log_event(f"Bootstrap failed: {e}\n{traceback.format_exc()}")


def launch_gui() -> int:
    """Launch the MarginMise GUI."""
    try:
        venv_python = INSTALL_DIR / ".venv" / "Scripts" / "python.exe"
        if not venv_python.exists():
            log_event("Virtual environment not found, cannot launch GUI")
            return 1

        gui_script = INSTALL_DIR / "launch_gui.py"
        if not gui_script.exists():
            log_event(f"GUI script not found at {gui_script}")
            return 1

        log_event("Launching GUI...")
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
    log_event("MarginMise Installer EXE launched")

    # Run bootstrap if needed
    if not INSTALL_FLAG.exists():
        log_event("First run - starting bootstrap")
        bootstrap_thread = threading.Thread(target=run_bootstrap, daemon=True)
        bootstrap_thread.start()
        bootstrap_thread.join()
    else:
        log_event("Already installed, skipping bootstrap")

    # Launch GUI
    return launch_gui()


if __name__ == "__main__":
    raise SystemExit(main())
