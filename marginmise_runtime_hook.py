# Runtime hook: ensure the native binaries for numpy / onnxruntime / cv2 are
# discoverable on Windows. PyInstaller's onedir bootloader normally adds the
# _internal directory to the DLL search path, but adding it explicitly here
# eliminates any load-order edge case where `import cv2` fails with
# "OpenCV bindings requires numpy package" even though numpy is bundled.
import os
import sys


def _add_dll_dirs() -> None:
    if sys.platform != "win32":
        return
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return
    # PyInstaller 4.4+ exposes sys._MEIPASS; the compiled packages live as
    # top-level folders (numpy, onnxruntime, cv2) directly under it.
    for name in ("numpy", "numpy.libs", "onnxruntime", "onnxruntime/capi", "cv2"):
        path = os.path.join(base, name)
        if os.path.isdir(path):
            try:
                os.add_dll_directory(path)  # type: ignore[attr-defined]
            except (AttributeError, OSError, FileNotFoundError):
                pass


_add_dll_dirs()
