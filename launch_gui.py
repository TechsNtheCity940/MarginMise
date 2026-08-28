#!/usr/bin/env python3
"""MarginMise GUI entry point.

Single entry point for both:
- normal Python: python launch_gui.py
- PyInstaller frozen EXE: MarginMise.exe
"""
import sys
import os
from pathlib import Path

APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
FROZEN = getattr(sys, "frozen", False)

if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))


def ensure_single_instance() -> None:
    if not FROZEN:
        return
    import ctypes
    kernel32 = ctypes.windll.kernel32
    mutex_name = "Global\\MarginMise_SingleInstance"
    mutex = kernel32.CreateMutexW(None, False, mutex_name)
    if mutex is None:
        return
    if kernel32.GetLastError() == 183:
        kernel32.CloseHandle(mutex)
        sys.exit(0)
    import atexit
    atexit.register(lambda: (kernel32.ReleaseMutex(mutex), kernel32.CloseHandle(mutex)) if mutex else None)


def log_startup(message: str) -> None:
    try:
        log_path = APP_DIR / "Logs" / "startup.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"{message}\n")
    except Exception:
        pass


def launch_gui() -> None:
    log_startup("[MarginMise] Launching GUI...")
    try:
        import tkinter as tk
    except ImportError:
        log_startup("[MarginMise] FATAL: tkinter missing")
        print("tkinter is required. Reinstall Python with tcl/tk enabled.")
        sys.exit(1)

    try:
        from restaurant_cost_gui import RestaurantCostGUI
    except Exception as exc:
        log_startup(f"[MarginMise] FATAL: GUI import failed: {exc}")
        raise

    root = tk.Tk()
    app = RestaurantCostGUI(root)
    root.mainloop()


def handle_ocr_worker() -> None:
    args = sys.argv[1:]
    try:
        output_idx = args.index("--output") if "--output" in args else -1
        output_path = Path(args[output_idx + 1]) if output_idx >= 0 and output_idx + 1 < len(args) else Path("rapidocr-result.json")
        image_paths = [Path(a) for a in args[output_idx + 1:] if not a.startswith("--")] if output_idx >= 0 else [Path(a) for a in args[1:] if not a.startswith("--")]

        from local_ocr import extract_rapidocr
        import json

        if not image_paths:
            result = {"text": "", "average_confidence": 0.0}
            output_path.write_text(json.dumps(result), encoding="utf-8")
            sys.exit(0)

        ocr_result = extract_rapidocr(image_paths)
        result = {
            "text": ocr_result.get("text", ""),
            "average_confidence": ocr_result.get("confidence", 0.0),
        }
        output_path.write_text(json.dumps(result), encoding="utf-8")
        sys.exit(0)
    except Exception as exc:
        if "--output" in args:
            try:
                output_idx = args.index("--output")
                output_path = Path(args[output_idx + 1])
                result = {"text": "", "average_confidence": 0.0, "error": str(exc)}
                output_path.write_text(json.dumps(result), encoding="utf-8")
            except Exception:
                pass
        sys.exit(1)


def main() -> None:
    log_startup(f"[MarginMise] Starting... APP_DIR={APP_DIR} FROZEN={FROZEN}")
    if FROZEN:
        ensure_single_instance()
        if "--ocr-worker" in sys.argv:
            handle_ocr_worker()
            return
    launch_gui()


if __name__ == "__main__":
    main()
