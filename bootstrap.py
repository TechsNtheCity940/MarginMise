#!/usr/bin/env python3
"""MarginMise Windows executable bootstrapper.

On first run, this module:
1. Extracts/copies the application to %LOCALAPPDATA%\\MarginMise
2. Creates a Python virtual environment
3. Installs all dependencies from requirements.txt
4. Silently installs Tesseract OCR via winget/Chocolatey
5. Downloads llama.cpp + LFM2.5 model
6. Launches the GUI

On subsequent runs, it skips setup and launches directly.
"""
from __future__ import annotations

import os
import sys
import subprocess
import threading
import traceback
import shutil
import zipfile
import tarfile
from pathlib import Path
from datetime import datetime

# ========== CONFIGURATION ==========
APP_NAME = "MarginMise"
LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
INSTALL_DIR = LOCALAPPDATA / APP_NAME
APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
REQUIREMENTS_FILE = "requirements.txt"

# ========== LOGGING ==========
LOG_DIR = INSTALL_DIR / "Logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
BOOTSTRAP_LOG = LOG_DIR / "bootstrap.log"
INSTALL_FLAG = INSTALL_DIR / ".installed"


def log_event(message: str) -> None:
    stamp = datetime.now().isoformat(timespec="seconds")
    try:
        with BOOTSTRAP_LOG.open("a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {message}\n")
    except Exception:
        pass


# ========== PYTHON / VENV ==========
def find_python() -> Path | None:
    """Find a usable Python 3.11+ installation."""
    candidates = []

    # Check py launcher first
    try:
        result = subprocess.run(
            ["py", "-3", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            py_path = Path(shutil.which("py") or "")
            if py_path.exists():
                candidates.append(py_path)
    except Exception:
        pass

    # Check python/python3 in PATH
    for name in ["python", "python3", "python3.11", "python3.12"]:
        path = shutil.which(name)
        if path:
            candidates.append(Path(path))

    # Check common install locations
    common_paths = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python" / "Python311" / "python.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python" / "Python312" / "python.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python" / "Python313" / "python.exe",
        Path("C:/Python311/python.exe"),
        Path("C:/Python312/python.exe"),
        Path("C:/Python313/python.exe"),
    ]
    candidates.extend(common_paths)

    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            result = subprocess.run(
                [str(candidate), "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                version = result.stdout.strip()
                log_event(f"Found Python: {candidate} ({version})")
                return candidate
        except Exception:
            continue

    return None


def install_python_winget() -> Path | None:
    """Try to install Python via winget."""
    try:
        log_event("Attempting Python install via winget...")
        result = subprocess.run(
            ["winget", "install", "--id", "Python.Python.3.12", "--exact", "--scope", "user",
             "--silent", "--disable-interactivity", "--accept-package-agreements", "--accept-source-agreements"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode == 0:
            log_event("Python installed via winget")
            # Re-check for Python
            return find_python()
    except Exception as e:
        log_event(f"winget Python install failed: {e}")
    return None


def create_venv(python: Path) -> Path:
    """Create a virtual environment in the install directory."""
    venv_dir = INSTALL_DIR / ".venv"
    venv_python = venv_dir / "Scripts" / "python.exe"

    if venv_python.exists():
        log_event("Virtual environment already exists")
        return venv_python

    log_event(f"Creating virtual environment at {venv_dir}...")
    try:
        result = subprocess.run(
            [str(python), "-m", "venv", str(venv_dir)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(f"venv creation failed: {result.stderr}")
        log_event("Virtual environment created successfully")
        return venv_python
    except Exception as e:
        log_event(f"Virtual environment creation failed: {e}")
        raise


def install_dependencies(venv_python: Path) -> None:
    """Install Python dependencies from requirements.txt."""
    log_event("Installing Python dependencies...")
    try:
        # Upgrade pip first
        subprocess.run(
            [str(venv_python), "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--upgrade", "pip"],
            capture_output=True,
            text=True,
            timeout=120,
        )

        # Install requirements
        req_file = INSTALL_DIR / REQUIREMENTS_FILE
        if not req_file.exists():
            # Try app dir if not in install dir
            req_file = Path(sys.executable).parent.parent / REQUIREMENTS_FILE

        if req_file.exists():
            result = subprocess.run(
                [str(venv_python), "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "-r", str(req_file)],
                capture_output=True,
                text=True,
                timeout=600,
            )
            if result.returncode != 0:
                log_event(f"pip install stderr: {result.stderr[:500]}")
            log_event(f"Dependencies installed (exit {result.returncode})")
        else:
            log_event(f"requirements.txt not found at {req_file}")
    except Exception as e:
        log_event(f"Dependency installation failed: {e}")
        # Non-fatal: continue with degraded functionality


# ========== TESSERACT OCR ==========
def install_tesseract() -> None:
    """Silently install Tesseract OCR using winget."""
    try:
        log_event("Installing Tesseract OCR...")
        # Try winget first
        result = subprocess.run(
            ["winget", "install", "--id", "UB-Mannheim.TesseractOCR", "--exact", "--silent",
             "--disable-interactivity", "--accept-package-agreements", "--accept-source-agreements"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode == 0:
            log_event("Tesseract OCR installed via winget")
            return

        # Try Chocolatey
        result = subprocess.run(
            ["choco", "install", "tesseract", "-y", "--no-progress"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode == 0:
            log_event("Tesseract OCR installed via Chocolatey")
            return

        log_event("Tesseract OCR installation skipped (no package manager available)")
    except Exception as e:
        log_event(f"Tesseract installation error: {e}")


# ========== LOCAL AI ==========
def install_local_ai(venv_python: Path) -> None:
    """Download and install the local AI model."""
    try:
        log_event("Installing local AI runtime...")
        result = subprocess.run(
            [str(venv_python), "-m", "local_ai", "ensure"],
            capture_output=True,
            text=True,
            timeout=600,
            cwd=str(INSTALL_DIR),
        )
        log_event(f"Local AI installation completed (exit {result.returncode})")
    except Exception as e:
        log_event(f"Local AI installation error: {e}")


# ========== MAIN BOOTSTRAP ==========
def run_bootstrap() -> None:
    """Run the full first-run setup sequence."""
    log_event("=== MarginMise Bootstrap Started ===")
    log_event(f"Install directory: {INSTALL_DIR}")
    log_event(f"App directory: {APP_DIR}")

    try:
        # 1. Find or install Python
        python = find_python()
        if not python:
            python = install_python_winget()
        if not python:
            raise RuntimeError("Python 3.11+ not found and could not be installed")

        # 2. Create virtual environment
        venv_python = create_venv(python)

        # 3. Install dependencies
        install_dependencies(venv_python)

        # 4. Install Tesseract OCR
        install_tesseract()

        # 5. Install local AI
        install_local_ai(venv_python)

        # 6. Mark as installed
        INSTALL_FLAG.write_text("1", encoding="utf-8")
        log_event("=== Bootstrap Complete ===")

    except Exception as e:
        log_event(f"Bootstrap failed: {e}\n{traceback.format_exc()}")
        # Non-fatal: continue to launch GUI anyway


def should_bootstrap() -> bool:
    """Check if first-run setup is needed."""
    # Always bootstrap if install flag is missing
    if not INSTALL_FLAG.exists():
        return True

    # Bootstrap if key components are missing
    venv_python = INSTALL_DIR / ".venv" / "Scripts" / "python.exe"
    if not venv_python.exists():
        return True

    return False


def launch_gui() -> int:
    """Launch the MarginMise GUI."""
    try:
        # Use the venv Python if available
        venv_python = INSTALL_DIR / ".venv" / "Scripts" / "python.exe"
        if venv_python.exists():
            python = venv_python
        else:
            python = Path(sys.executable)

        # Launch the GUI
        gui_script = INSTALL_DIR / "launch_gui.py"
        if not gui_script.exists():
            gui_script = Path(__file__).parent / "launch_gui.py"

        result = subprocess.run(
            [str(python), str(gui_script)],
            cwd=str(INSTALL_DIR),
        )
        return result.returncode
    except Exception as e:
        log_event(f"GUI launch failed: {e}")
        return 1


def main() -> int:
    """Main entry point for the bootstrapper."""
    log_event("MarginMise executable launched")

    if getattr(sys, "frozen", False):
        log_event("Frozen bootstrap invocation rejected; build MarginMise from launch_gui.py")
        return 2

    if should_bootstrap():
        log_event("First-run setup required")
        # Run bootstrap in a separate thread so we can show progress
        bootstrap_thread = threading.Thread(target=run_bootstrap, daemon=True)
        bootstrap_thread.start()
        bootstrap_thread.join()
    else:
        log_event("Skipping bootstrap (already installed)")

    # The source installer prepares the environment; the GUI is launched by
    # the user or by the separately packaged MarginMise executable.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
