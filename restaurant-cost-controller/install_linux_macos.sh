#!/usr/bin/env bash
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"
if ! command -v hermes >/dev/null 2>&1; then
  echo "Hermes Agent was not found. Installing the official backend..."
  curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- --skip-browser
  export PATH="$HOME/.local/bin:$PATH"
fi
HERMES_BIN="$(command -v hermes || true)"
if [[ -z "$HERMES_BIN" && -x "$HOME/.local/bin/hermes" ]]; then
  HERMES_BIN="$HOME/.local/bin/hermes"
fi
[[ -n "$HERMES_BIN" ]] || { echo "Hermes executable was not found after installation."; exit 1; }

PYTHON_BIN="$(command -v python3 || true)"
if [[ -z "$PYTHON_BIN" && -x "$HOME/.hermes/hermes-agent/venv/bin/python" ]]; then
  PYTHON_BIN="$HOME/.hermes/hermes-agent/venv/bin/python"
fi
[[ -n "$PYTHON_BIN" ]] || { echo "Python was not found."; exit 1; }

[[ -d .venv ]] || "$PYTHON_BIN" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

.venv/bin/python hermes_backend.py --profile restaurant-cost-controller

echo "Installation complete. Tesseract is not required."
echo "CostPilot is pinned to OpenRouter Free Models Router (openrouter/free)."
echo "If no OpenRouter key is configured, CostPilot requests one-time authorization on first use."
echo "Run ./run_gui.sh"
