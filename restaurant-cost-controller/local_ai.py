#!/usr/bin/env python3
"""Pinned, on-demand local CostPilot runtime.

The llama.cpp process exists only for one manager question. MarginMise owns all
database reads, calculations, permissions, evidence validation, and navigation.
The language model receives a bounded read-only evidence packet and returns
schema-constrained JSON.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import ssl
import subprocess
import tempfile
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

LLAMA_RELEASE = "b9637"
LLAMA_ARCHIVE = f"llama-{LLAMA_RELEASE}-bin-win-cpu-x64.zip"
LLAMA_URL = (
    f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_RELEASE}/"
    f"{LLAMA_ARCHIVE}"
)
LLAMA_SHA256 = "f7783c2b8c007f95e710ac40f26a24861a80b603b0b739fc54d7c926a4716c1e"
LLAMA_LICENSE_URL = f"https://raw.githubusercontent.com/ggml-org/llama.cpp/{LLAMA_RELEASE}/LICENSE"
LLAMA_LICENSE_SHA256 = "94f29bbed6a22c35b992c5c6ebf0e7c92f13b836b90f36f461c9cf2f0f1d010d"
LLAMA_LICENSE_SIZE = 1_078

MODEL_REPO = "LiquidAI/LFM2.5-1.2B-Instruct-GGUF"
MODEL_REVISION = "047e06635fbe71469926b35ea414537245218200"
MODEL_FILE = "LFM2.5-1.2B-Instruct-Q4_K_M.gguf"
MODEL_SIZE = 730_895_168
MODEL_SHA256 = "b1b3de114215d9507409a662a501a631095a479a419584e8a2ded6304b19b4f5"
MODEL_URL = (
    f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}/"
    f"{MODEL_FILE}?download=true"
)
MODEL_ID = "lfm2.5-1.2b-instruct-q4_k_m"
MODEL_LICENSE = "LFM Open License v1.0"
MODEL_LICENSE_URL = "https://www.liquid.ai/lfm-license"
MODEL_LICENSE_FILE_URL = (
    f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}/LICENSE?download=true"
)
MODEL_LICENSE_SHA256 = "5188f2b355da20647257a3156db5834c794e5fb5e6d8dc4d4cdbb3180e75b85b"
MODEL_LICENSE_SIZE = 10_596


def ai_root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    return (Path(local) if local else Path.home() / ".local" / "share") / "MarginMise" / "AI"


def runtime_dir() -> Path:
    return ai_root() / "runtime" / f"llama-{LLAMA_RELEASE}"


def runtime_executable() -> Path:
    return runtime_dir() / ("llama-cli.exe" if os.name == "nt" else "llama-cli")


def completion_executable() -> Path:
    return runtime_dir() / ("llama-completion.exe" if os.name == "nt" else "llama-completion")


def model_path() -> Path:
    return ai_root() / "models" / MODEL_FILE


def manifest_path() -> Path:
    return ai_root() / "local_costpilot_manifest.json"


def model_license_path() -> Path:
    return ai_root() / "licenses" / "LFM_OPEN_LICENSE_1.0.txt"


def llama_license_path() -> Path:
    return ai_root() / "licenses" / "LLAMA_CPP_MIT_LICENSE.txt"


@dataclass
class LocalAIStatus:
    runtime_ready: bool = False
    runtime_executable: str = ""
    model_ready: bool = False
    model_path: str = ""
    model_size: int = 0
    licenses_ready: bool = False
    model_id: str = MODEL_ID
    license_name: str = MODEL_LICENSE
    ready: bool = False
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class LocalAIError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _ssl_context() -> ssl.SSLContext:
    try:
        from hermes_backend import ensure_windows_ca_bundle

        bundle = ensure_windows_ca_bundle()
    except Exception:
        bundle = ""
    return ssl.create_default_context(cafile=bundle or None)


def _download_verified(url: str, destination: Path, expected_sha256: str, expected_size: int = 0) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".partial")
    request = urllib.request.Request(url, headers={"User-Agent": "MarginMise-Local-CostPilot/1.0"})
    with urllib.request.urlopen(request, context=_ssl_context(), timeout=90) as response:
        with partial.open("wb") as output:
            shutil.copyfileobj(response, output, length=1024 * 1024)
    if expected_size and partial.stat().st_size != expected_size:
        raise LocalAIError(
            f"Downloaded {destination.name} has size {partial.stat().st_size}; expected {expected_size}."
        )
    actual = _sha256(partial)
    if actual.lower() != expected_sha256.lower():
        raise LocalAIError(f"Checksum verification failed for {destination.name}.")
    partial.replace(destination)


def _manifest_is_valid() -> bool:
    path = manifest_path()
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    model = model_path()
    runtime = runtime_executable()
    return bool(
        data.get("model_sha256") == MODEL_SHA256
        and data.get("llama_sha256") == LLAMA_SHA256
        and data.get("model_license_sha256") == MODEL_LICENSE_SHA256
        and data.get("llama_license_sha256") == LLAMA_LICENSE_SHA256
        and model.is_file()
        and model.stat().st_size == MODEL_SIZE
        and runtime.is_file()
        and completion_executable().is_file()
        and model_license_path().is_file()
        and llama_license_path().is_file()
        and _sha256(model_license_path()) == MODEL_LICENSE_SHA256
        and _sha256(llama_license_path()) == LLAMA_LICENSE_SHA256
    )


def status() -> LocalAIStatus:
    executable = runtime_executable()
    model = model_path()
    result = LocalAIStatus(
        runtime_ready=executable.is_file() and completion_executable().is_file(),
        runtime_executable=str(executable) if executable.is_file() else "",
        model_ready=model.is_file() and model.stat().st_size == MODEL_SIZE,
        model_path=str(model) if model.is_file() else "",
        model_size=model.stat().st_size if model.is_file() else 0,
        licenses_ready=model_license_path().is_file() and llama_license_path().is_file(),
    )
    result.ready = result.runtime_ready and result.model_ready and result.licenses_ready
    result.message = (
        "Local CostPilot is ready and will load only while answering a question."
        if result.ready
        else "Local CostPilot runtime or model is not installed."
    )
    return result


def ensure(*, auto_install: bool = True) -> LocalAIStatus:
    current = status()
    if current.ready and _manifest_is_valid():
        return current
    if not auto_install:
        return current
    if os.name != "nt":
        raise LocalAIError("Automatic local CostPilot provisioning is currently implemented for Windows.")

    root = ai_root()
    downloads = root / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)

    executable = runtime_executable()
    if not executable.is_file() or not completion_executable().is_file():
        archive = downloads / LLAMA_ARCHIVE
        if not archive.is_file() or _sha256(archive) != LLAMA_SHA256:
            _download_verified(LLAMA_URL, archive, LLAMA_SHA256)
        runtime_dir().mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as package:
            package.extractall(runtime_dir())
        if not executable.is_file():
            raise LocalAIError("llama.cpp extracted without llama-cli.exe.")

    model = model_path()
    if not model.is_file() or model.stat().st_size != MODEL_SIZE or _sha256(model) != MODEL_SHA256:
        _download_verified(MODEL_URL, model, MODEL_SHA256, MODEL_SIZE)

    if (
        not model_license_path().is_file()
        or _sha256(model_license_path()) != MODEL_LICENSE_SHA256
    ):
        _download_verified(
            MODEL_LICENSE_FILE_URL,
            model_license_path(),
            MODEL_LICENSE_SHA256,
            MODEL_LICENSE_SIZE,
        )
    if (
        not llama_license_path().is_file()
        or _sha256(llama_license_path()) != LLAMA_LICENSE_SHA256
    ):
        _download_verified(
            LLAMA_LICENSE_URL,
            llama_license_path(),
            LLAMA_LICENSE_SHA256,
            LLAMA_LICENSE_SIZE,
        )

    manifest = {
        "runtime_release": LLAMA_RELEASE,
        "llama_sha256": LLAMA_SHA256,
        "model_repo": MODEL_REPO,
        "model_revision": MODEL_REVISION,
        "model_file": MODEL_FILE,
        "model_sha256": MODEL_SHA256,
        "model_size": MODEL_SIZE,
        "model_id": MODEL_ID,
        "license": MODEL_LICENSE,
        "license_url": MODEL_LICENSE_URL,
        "model_license_sha256": MODEL_LICENSE_SHA256,
        "llama_license_sha256": LLAMA_LICENSE_SHA256,
    }
    manifest_path().write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    result = status()
    if not result.ready:
        raise LocalAIError(result.message)
    return result


def _extract_result_json(output: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    for index, character in enumerate(output or ""):
        if character != "{":
            continue
        try:
            value, _end = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and isinstance(value.get("answer"), str):
            candidates.append(value)
    if not candidates:
        raise LocalAIError(
            "The local model did not produce the required structured answer. "
            f"Output ended with: {(output or '')[-1600:]}"
        )
    return candidates[-1]


def generate_json(
    *,
    system_prompt: str,
    user_prompt: str,
    schema: dict[str, Any],
    timeout: int = 180,
    context_size: int = 8192,
    max_tokens: int = 700,
    threads: int | None = None,
) -> dict[str, Any]:
    current = ensure(auto_install=False)
    if not current.ready:
        raise LocalAIError(current.message)
    thread_count = threads or max(1, min(4, (os.cpu_count() or 2) // 2))
    with tempfile.TemporaryDirectory(prefix="marginmise-costpilot-") as temp_name:
        temp = Path(temp_name)
        prompt_file = temp / "prompt.txt"
        schema_file = temp / "schema.json"
        prompt_file.write_text(
            "<|startoftext|><|im_start|>system\n"
            + system_prompt.strip()
            + "<|im_end|>\n<|im_start|>user\n"
            + user_prompt.strip()
            + "<|im_end|>\n<|im_start|>assistant\n",
            encoding="utf-8",
        )
        schema_file.write_text(json.dumps(schema), encoding="utf-8")
        command = [
            str(completion_executable()),
            "--model",
            str(model_path()),
            "--file",
            str(prompt_file),
            "--json-schema-file",
            str(schema_file),
            "--predict",
            str(max(64, int(max_tokens))),
            "--ctx-size",
            str(max(2048, int(context_size))),
            "--threads",
            str(thread_count),
            "--threads-batch",
            str(thread_count),
            "--temp",
            "0.1",
            "--top-k",
            "50",
            "--repeat-penalty",
            "1.05",
            "--single-turn",
            "--no-conversation",
            "--no-display-prompt",
            "--no-warmup",
            "--simple-io",
            "--log-verbosity",
            "1",
            "--poll",
            "0",
            "--prio",
            "-1",
        ]
        creation_flags = 0
        if os.name == "nt":
            creation_flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
            creation_flags |= int(getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0))
        try:
            completed = subprocess.run(
                command,
                cwd=str(runtime_dir()),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(30, int(timeout)),
                creationflags=creation_flags,
            )
        except subprocess.TimeoutExpired as exc:
            raise LocalAIError(f"Local CostPilot timed out after {timeout} seconds.") from exc
        combined = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
        if completed.returncode != 0:
            raise LocalAIError(
                f"Local CostPilot exited with code {completed.returncode}: {combined[-1000:]}"
            )
        return _extract_result_json(combined)


def build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    ensure_parser = subparsers.add_parser("ensure")
    ensure_parser.add_argument("--no-install", action="store_true")
    return parser


def main() -> int:
    args = build_cli().parse_args()
    result = status() if args.command == "status" else ensure(auto_install=not args.no_install)
    print(json.dumps(result.as_dict(), indent=2))
    return 0 if result.ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
