#!/bin/bash
# ============================================================
# MarginMise macOS/Linux Build Script
# ============================================================
# Prerequisites: python3
# This script builds a standalone executable in dist/
# ============================================================

set -e
cd "$(dirname "$0")"

echo "========================================"
echo "MarginMise Build (macOS/Linux)"
echo "========================================"
echo

# Create virtual environment
echo "[1/5] Creating virtual environment..."
python3 -m venv .buildvenv
source .buildvenv/bin/activate

# Install dependencies
echo "[2/5] Installing dependencies..."
pip install --upgrade pip
pip install -r requirements.txt
pip install pyinstaller pillow

# Build with PyInstaller
echo "[3/5] Building executable with PyInstaller..."
pyinstaller marginmise.spec

# Test the executable
echo "[4/5] Build complete."
if [ -f "dist/MarginMise" ]; then
    echo
    echo "========================================"
    echo "BUILD SUCCESSFUL!"
    echo "========================================"
    echo "Output: dist/MarginMise"
    echo
    echo "To run: ./dist/MarginMise"
    echo "On first run, it will download Tesseract OCR and the local AI model."
else
    echo
    echo "========================================"
    echo "BUILD FAILED!"
    echo "========================================"
    echo "Check the PyInstaller output above for errors."
    exit 1
fi

# Cleanup
echo "[5/5] Cleaning up build venv..."
deactivate
rm -rf .buildvenv

echo
echo "Done."
