#!/usr/bin/env python3
"""Reliable Windows-friendly GUI launcher with persistent startup logging."""
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
HERMES_BOOTSTRAP_LOG = LOG_DIR / "hermes_bootstrap.log"
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
        messagebox.showerror(
            "MarginMise could not start",
            "The application encountered a startup error.\n\n"
            f"Details were saved to:\n{STARTUP_ERROR}\n\n"
            f"{text[-1200:]}",
            parent=root,
        )
        root.destroy()
    except Exception:
        pass


def bootstrap_hermes() -> None:
    """Silently provision Hermes, the app profile, TLS trust, and free routing."""
    if str(os.environ.get("MARGINMISE_SKIP_HERMES_BOOTSTRAP") or "").lower() in {"1", "true", "yes"}:
        return
    stamp = datetime.now().isoformat(timespec="seconds")
    os.environ["MARGINMISE_HERMES_BOOTSTRAP_ACTIVE"] = "1"
    try:
        from hermes_backend import DEFAULT_PROFILE, HermesBackend

        status = HermesBackend(APP_DIR).ensure(
            DEFAULT_PROFILE,
            auto_install=True,
            install_profile=True,
            configure_free_route=True,
        )
        payload = {"timestamp": stamp, "ok": True, "status": status.as_dict()}
    except Exception:
        payload = {
            "timestamp": stamp,
            "ok": False,
            "error": traceback.format_exc(),
            "message": (
                "MarginMise started in degraded mode. Hermes will be retried in the background "
                "and operational workflows remain available."
            ),
        }
    finally:
        os.environ.pop("MARGINMISE_HERMES_BOOTSTRAP_ACTIVE", None)
    HERMES_BOOTSTRAP_LOG.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def bootstrap_local_ocr() -> None:
    """Verify the lightweight OCR engines without loading their models."""
    stamp = datetime.now().isoformat(timespec="seconds")
    try:
        from local_ocr import ensure

        status = ensure(install_tesseract=False)
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
        if str(os.environ.get("MARGINMISE_ENABLE_HERMES_BOOTSTRAP") or "").lower() in {
            "1",
            "true",
            "yes",
        }:
            threading.Thread(
                target=bootstrap_hermes,
                name="MarginMise-Hermes-Bootstrap",
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
