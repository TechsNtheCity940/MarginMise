#!/usr/bin/env python3
"""MarginMise runtime entry point.

The same entry point serves source runs, the packaged GUI, and the short-lived
OCR worker.  A frozen process must dispatch worker arguments before importing
the GUI; otherwise a worker invocation would start another GUI process.
"""
from __future__ import annotations

import argparse
import json
import sys
import os
from pathlib import Path

APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
FROZEN = bool(getattr(sys, "frozen", False))

if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))


def ensure_single_instance() -> bool:
    """Return only after the frozen GUI owns the Windows mutex.

    OCR workers intentionally do not call this function: they are children of
    the GUI and must be allowed to run while the GUI owns the mutex.
    """
    if not FROZEN or os.name != "nt":
        return True
    import atexit
    import ctypes

    mutex_name = "Global\\MarginMise_SingleInstance"
    kernel32 = ctypes.windll.kernel32
    mutex = kernel32.CreateMutexW(None, True, mutex_name)
    if not mutex:
        raise OSError("Windows could not create the MarginMise single-instance mutex")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(mutex)
        return False

    def release_mutex() -> None:
        kernel32.ReleaseMutex(mutex)
        kernel32.CloseHandle(mutex)

    atexit.register(release_mutex)
    return True


def log_startup(message: str) -> None:
    try:
        local_root = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))
        log_path = local_root / "MarginMise" / "Logs" / "startup.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"{message}\n")
    except Exception:
        pass


def launch_gui() -> int:
    log_startup("[MarginMise] Launching GUI...")
    try:
        import tkinter as tk
    except ImportError:
        log_startup("[MarginMise] FATAL: tkinter missing")
        print("tkinter is required. Reinstall Python with tcl/tk enabled.")
        sys.exit(1)

    try:
        from manager_first_gui import ManagerFirstRestaurantCostControllerGUI
    except Exception as exc:
        log_startup(f"[MarginMise] FATAL: GUI import failed: {exc}")
        raise

    root = tk.Tk()
    app = ManagerFirstRestaurantCostControllerGUI(root)
    def close_app() -> None:
        try:
            app.auto_upload_coordinator.stop()
            if app.pipeline:
                app.pipeline.phase2.stop_mobile_count_server()
        finally:
            root.destroy()

    root.protocol("WM_DELETE_WINDOW", close_app)
    root.mainloop()
    return 0


def startup_check() -> int:
    """Exercise frozen GUI imports without requiring an interactive session."""
    try:
        import tkinter as tk
        from manager_first_gui import ManagerFirstRestaurantCostControllerGUI

        root = tk.Tk()
        app = ManagerFirstRestaurantCostControllerGUI(root)
        root.update_idletasks()
        app.auto_upload_coordinator.stop()
        root.destroy()
        log_startup("[MarginMise] Startup check passed")
        return 0
    except Exception as exc:
        log_startup(f"[MarginMise] Startup check failed: {exc}")
        return 1


def parse_ocr_worker_args(argv: list[str]) -> tuple[Path, list[Path]]:
    """Parse the stable worker protocol used by source and frozen runs."""
    worker_args = list(argv)
    if worker_args and worker_args[0] == "--ocr-worker":
        worker_args.pop(0)
    if worker_args and worker_args[0] == "extract":
        worker_args.pop(0)
    parser = argparse.ArgumentParser(description="MarginMise OCR worker")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("images", nargs="+", type=Path)
    parsed = parser.parse_args(worker_args)
    return parsed.output, parsed.images


def handle_ocr_worker(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    output_path: Path | None = None
    try:
        output_path, image_paths = parse_ocr_worker_args(args)
        from local_ocr import extract_rapidocr
        ocr_result = extract_rapidocr(image_paths)
        result = {
            "text": ocr_result.get("text", ""),
            "average_confidence": ocr_result.get("average_confidence", 0.0),
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(result), encoding="utf-8")
        return 0
    except Exception as exc:
        if output_path is None and "--output" in args:
            try:
                output_idx = args.index("--output")
                output_path = Path(args[output_idx + 1])
            except Exception:
                output_path = None
        if output_path is not None:
            result = {"text": "", "average_confidence": 0.0, "error": str(exc)}
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(result), encoding="utf-8")
        log_startup(f"[MarginMise] OCR worker failed: {exc}")
        return 1


def main() -> int:
    log_startup(f"[MarginMise] Starting... APP_DIR={APP_DIR} FROZEN={FROZEN}")
    if "--ocr-worker" in sys.argv[1:]:
        return handle_ocr_worker()
    if "--startup-check" in sys.argv[1:]:
        return startup_check()
    if FROZEN and not ensure_single_instance():
        log_startup("[MarginMise] Another GUI instance is already running")
        return 0
    return launch_gui()


if __name__ == "__main__":
    raise SystemExit(main())
