#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
if [ ! -x ".venv/bin/python" ]; then
  ./install_linux_macos.sh
fi
exec .venv/bin/python auto_upload.py
