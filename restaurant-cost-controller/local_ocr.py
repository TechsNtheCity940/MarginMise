#!/usr/bin/env python3
"""Small, on-demand OCR runtime for MarginMise.

RapidOCR runs in this short-lived process so ONNX model memory is returned to
Windows immediately after a document finishes. Tesseract is an optional second
engine and is installed silently through WinGet when that facility is present.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median
from typing import Any, Sequence

# Keep OCR from monopolizing a low-resource restaurant workstation.
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("OMP_WAIT_POLICY", "PASSIVE")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
os.environ.setdefault("MKL_NUM_THREADS", "2")

TESSERACT_WINGET_ID = "UB-Mannheim.TesseractOCR"


@dataclass
class LocalOCRStatus:
    rapidocr_ready: bool = False
    onnxruntime_ready: bool = False
    tesseract_ready: bool = False
    tesseract_executable: str = ""
    message: str = ""

    @property
    def ready(self) -> bool:
        return self.rapidocr_ready or self.tesseract_ready

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["ready"] = self.ready
        return payload


def _creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0


def find_tesseract() -> str:
    candidates = [shutil.which("tesseract") or ""]
    if os.name == "nt":
        candidates.extend(
            [
                str(Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Tesseract-OCR" / "tesseract.exe"),
                str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Tesseract-OCR" / "tesseract.exe"),
                str(Path(os.environ.get("LOCALAPPDATA", "")) / "Tesseract-OCR" / "tesseract.exe"),
            ]
        )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    return ""


def install_tesseract_silently(timeout: int = 600) -> tuple[bool, str]:
    """Best-effort Windows install; RapidOCR remains available if this fails."""
    existing = find_tesseract()
    if existing:
        return True, existing
    if os.name != "nt":
        return False, "Automatic Tesseract installation is currently supported on Windows only."
    winget = shutil.which("winget")
    if not winget:
        return False, "WinGet is unavailable; RapidOCR will be used."
    command = [
        winget,
        "install",
        "--id",
        TESSERACT_WINGET_ID,
        "--exact",
        "--silent",
        "--disable-interactivity",
        "--accept-package-agreements",
        "--accept-source-agreements",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(60, int(timeout)),
            creationflags=_creation_flags(),
        )
    except Exception as exc:
        return False, f"Tesseract installation could not run: {exc}"
    executable = find_tesseract()
    if executable:
        return True, executable
    detail = (completed.stderr or completed.stdout or "installer returned no details").strip()
    return False, f"Tesseract installer exit code {completed.returncode}: {detail[-500:]}"


def status() -> LocalOCRStatus:
    result = LocalOCRStatus(
        rapidocr_ready=importlib.util.find_spec("rapidocr") is not None,
        onnxruntime_ready=importlib.util.find_spec("onnxruntime") is not None,
        tesseract_executable=find_tesseract(),
    )
    result.tesseract_ready = bool(result.tesseract_executable)
    if result.rapidocr_ready and result.onnxruntime_ready:
        result.message = "RapidOCR is ready for on-demand local scan extraction."
    elif result.tesseract_ready:
        result.message = "Tesseract is ready; RapidOCR dependencies need repair."
    else:
        result.message = "No local OCR engine is ready."
    return result


def ensure(*, install_tesseract: bool = False) -> LocalOCRStatus:
    result = status()
    if install_tesseract and not result.tesseract_ready:
        _installed, detail = install_tesseract_silently()
        result = status()
        if not result.tesseract_ready and detail:
            result.message = f"{result.message} {detail}"
    return result


def _line_texts(boxes: Sequence[Any], texts: Sequence[str]) -> list[str]:
    """Rebuild reading lines from OCR word/cell boxes."""
    entries: list[dict[str, Any]] = []
    for box, text in zip(boxes or (), texts or ()):
        clean = " ".join(str(text or "").split())
        if not clean:
            continue
        points = [tuple(map(float, point)) for point in box]
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        entries.append(
            {
                "text": clean,
                "left": min(xs),
                "center_y": sum(ys) / len(ys),
                "height": max(ys) - min(ys),
            }
        )
    if not entries:
        return []
    typical_height = max(8.0, median(max(1.0, entry["height"]) for entry in entries))
    tolerance = typical_height * 0.58
    rows: list[list[dict[str, Any]]] = []
    for entry in sorted(entries, key=lambda item: (item["center_y"], item["left"])):
        best: list[dict[str, Any]] | None = None
        best_distance = float("inf")
        for row in rows[-4:]:
            row_y = sum(item["center_y"] for item in row) / len(row)
            distance = abs(entry["center_y"] - row_y)
            if distance <= tolerance and distance < best_distance:
                best, best_distance = row, distance
        if best is None:
            rows.append([entry])
        else:
            best.append(entry)
    rows.sort(key=lambda row: sum(item["center_y"] for item in row) / len(row))
    return [" ".join(item["text"] for item in sorted(row, key=lambda value: value["left"])) for row in rows]


def extract_rapidocr(image_paths: Sequence[Path]) -> dict[str, Any]:
    from rapidocr import RapidOCR

    engine = RapidOCR(
        params={
            "Global.log_level": "error",
            "EngineConfig.onnxruntime.intra_op_num_threads": 2,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            "EngineConfig.onnxruntime.enable_cpu_mem_arena": False,
        }
    )
    pages: list[dict[str, Any]] = []
    all_scores: list[float] = []
    for index, image_path in enumerate(image_paths, 1):
        output = engine(image_path)
        raw_texts = getattr(output, "txts", None)
        raw_boxes = getattr(output, "boxes", None)
        raw_scores = getattr(output, "scores", None)
        texts = list(raw_texts) if raw_texts is not None else []
        boxes = list(raw_boxes) if raw_boxes is not None else []
        scores = [float(value) for value in raw_scores] if raw_scores is not None else []
        all_scores.extend(scores)
        lines = _line_texts(boxes, texts) if boxes else [" ".join(str(text).split()) for text in texts]
        pages.append(
            {
                "page": index,
                "path": str(image_path),
                "text": "\n".join(lines).strip(),
                "average_confidence": (sum(scores) / len(scores)) if scores else 0.0,
            }
        )
    return {
        "engine": "rapidocr-onnx",
        "pages": pages,
        "text": "\n\n".join(
            f"--- PAGE {page['page']} ---\n{page['text']}" for page in pages if page["text"]
        ).strip(),
        "average_confidence": (sum(all_scores) / len(all_scores)) if all_scores else 0.0,
    }


def build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    ensure_parser = subparsers.add_parser("ensure")
    ensure_parser.add_argument("--install-tesseract", action="store_true")
    extract_parser = subparsers.add_parser("extract")
    extract_parser.add_argument("--output", type=Path, required=True)
    extract_parser.add_argument("images", nargs="+", type=Path)
    subparsers.add_parser("status")
    return parser


def main() -> int:
    args = build_cli().parse_args()
    if args.command == "ensure":
        result = ensure(install_tesseract=args.install_tesseract)
        print(json.dumps(result.as_dict(), indent=2))
        return 0 if result.ready else 2
    if args.command == "status":
        result = status()
        print(json.dumps(result.as_dict(), indent=2))
        return 0 if result.ready else 2
    output = extract_rapidocr(args.images)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2), encoding="utf-8")
    return 0 if output.get("text") else 3


if __name__ == "__main__":
    raise SystemExit(main())
