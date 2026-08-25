#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  ./install_linux_macos.sh
fi
exec .venv/bin/python launch_gui.py
