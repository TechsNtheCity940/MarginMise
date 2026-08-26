#!/usr/bin/env python3
"""Reliable GUI launcher with persistent startup logging.

All engines (OCR, CostPilot) are provisioned and verified locally. No external
AI provider or cloud service is required. Hermes Agent is not used.
"""
from __future__ import annotations

import sys
import traceback
import json
import os
import threading
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
LOG_DIR = APP_DIR / "Logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
STARTUP_ERROR = LOG_DIR / "startup_error.log"
LOCAL_OCR_BOOTSTRAP_LOG = LOG_DIR / "local_ocr_bootstrap.log"
LOCAL_AI_BOOTSTRAP_LOG = LOG_DIR / "local_ai_bootstrap.log"


def write_error(text: str) -> None:
    stamp = datetime.now().isoformat(timespec="seconds")
    STARTUP_ERROR.write_text(f"[{stamp}] MarginMise startup failure\n\n{text}\n", encoding="utf-8")


def show_error(text: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        root.after(100, lambda: root.destroy())
        messagebox.showerror("MarginMise", text)
        root.mainloop()
    except Exception:
        pass


def bootstrap_local_ocr() -> None:
    """Verify the lightweight OCR engines without loading their models."""
    stamp = datetime.now().isoformat(timespec="seconds")
    try:
        from local_ocr import ensure

        status = ensure(install_tesseract=True)
        payload = {"timestamp": stamp, "ok": status.ready, "status": status.as_dict()}
    except Exception:
        payload = {
            "timestamp": stamp,
            "ok": False,
            "error": traceback.format_exc(),
            "message": "Local OCR will be retried when a scanned document is processed.",
        }
    LOCAL_OCR_BOOTSTRAP_LOG.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def bootstrap_local_ai() -> None:
    """Check the local model without loading it into memory."""
    stamp = datetime.now().isoformat(timespec="seconds")
    try:
        from local_ai import status

        current = status()
        payload = {"timestamp": stamp, "ok": current.ready, "status": current.as_dict()}
    except Exception:
        payload = {
            "timestamp": stamp,
            "ok": False,
            "error": traceback.format_exc(),
            "message": "Local CostPilot will be checked again from the application.",
        }
    LOCAL_AI_BOOTSTRAP_LOG.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main() -> int:
    try:
        STARTUP_ERROR.unlink(missing_ok=True)
        threading.Thread(
            target=bootstrap_local_ocr,
            name="MarginMise-Local-OCR-Bootstrap",
            daemon=True,
        ).start()
        threading.Thread(
            target=bootstrap_local_ai,
            name="MarginMise-Local-AI-Bootstrap",
            daemon=True,
        ).start()
        import manager_first_gui
        return int(manager_first_gui.main() or 0)
    except BaseException:
        detail = traceback.format_exc()
        write_error(detail)
        print(detail, file=sys.stderr)
        show_error(detail)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
