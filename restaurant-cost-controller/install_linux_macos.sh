#!/usr/bin/env bash
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

PYTHON_BIN="$(command -v python3 || true)"
if [[ -z "$PYTHON_BIN" ]]; then
    # Check if there's a venv Python from a previous MarginMise install
    if [[ -x "$HOME/.local/share/MarginMise/.venv/bin/python" ]]; then
        PYTHON_BIN="$HOME/.local/share/MarginMise/.venv/bin/python"
    fi
fi
if [[ -z "$PYTHON_BIN" ]]; then
    echo "Python 3 was not found." >&2
    exit 1
fi

# Build the application environment independently of Hermes. OCR and all
# operational workflows must remain usable even when an AI provider is down.
if [[ ! -d .venv ]]; then
    echo "Creating the MarginMise Python environment..."
    "$PYTHON_BIN" -m venv .venv
fi

.venv/bin/python -m pip install --disable-pip-version-check --no-input --upgrade pip
.venv/bin/python -m pip install --disable-pip-version-check --no-input -r requirements.txt

echo "Preparing on-demand local OCR..."
if ! .venv/bin/python local_ocr.py ensure --install-tesseract; then
    echo "  WARNING: Tesseract could not be installed. RapidOCR will be used for OCR."
fi

echo "Preparing local CostPilot (pinned LFM2.5 Q4 and llama.cpp runtime)..."
if ! .venv/bin/python local_ai.py ensure; then
    echo "  WARNING: Local CostPilot runtime could not be downloaded (offline or network issue)."
    echo "  The app will use deterministic computed answers until CostPilot is installed."
    echo "  Run 'python local_ai.py ensure' later to retry the download."
fi

echo ""
echo "MarginMise installation completed."
echo "RapidOCR is installed locally and runs only while a scan is being processed."
echo "Tesseract is installed silently for fallback OCR on systems without GPU acceleration."
echo "CostPilot uses the local LFM2.5 Q4 model and loads it only while answering."
echo ""
echo "Run ./run_gui.sh"
