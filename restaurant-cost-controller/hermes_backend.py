#!/usr/bin/env python3
"""Hermes Agent installation, discovery, profile provisioning, and health checks.

The restaurant GUI intentionally delegates invoice text extraction to Hermes.
This module keeps that backend explicit and testable instead of pretending a
missing executable will somehow become an OCR engine through optimism.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Sequence

WINDOWS_INSTALL_URL = (
    "https://raw.githubusercontent.com/NousResearch/hermes-agent/"
    "main/scripts/install.ps1"
)
POSIX_INSTALL_URL = "https://hermes-agent.nousresearch.com/install.sh"
DEFAULT_PROFILE = "restaurant-cost-controller"
COSTPILOT_FREE_PROVIDER = "openrouter"
COSTPILOT_FREE_MODEL = "openrouter/free"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
NOUS_BASE_URL = "https://inference-api.nousresearch.com/v1"
_WINDOWS_CA_BUNDLE: str | None = None


@dataclass
class BackendStatus:
    installed: bool = False
    executable: str = ""
    version: str = ""
    profile_name: str = DEFAULT_PROFILE
    profile_installed: bool = False
    doctor_ok: bool = False
    document_tooling_supported: bool = False
    model_provider: str = ""
    model: str = ""
    free_route_configured: bool = False
    provider_authorized: bool = False
    authorization_required: bool = False
    ai_ready: bool = False
    ready: bool = False
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class HermesBackendError(RuntimeError):
    pass


def is_free_model(model: str | None) -> bool:
    normalized = str(model or "").strip().lower()
    return normalized == "openrouter/free" or normalized.endswith(":free")


def ensure_windows_ca_bundle() -> str:
    """Merge Mozilla and Windows trust roots for Python-based Hermes clients."""
    global _WINDOWS_CA_BUNDLE
    if not sys.platform.startswith("win"):
        return ""
    if _WINDOWS_CA_BUNDLE and Path(_WINDOWS_CA_BUNDLE).exists():
        return _WINDOWS_CA_BUNDLE

    local_app_data = os.environ.get("LOCALAPPDATA")
    root = Path(local_app_data) / "MarginMise" if local_app_data else Path(tempfile.gettempdir()) / "MarginMise"
    target = root / "certificates" / "windows-trust-bundle.pem"
    target.parent.mkdir(parents=True, exist_ok=True)

    pem_parts: list[str] = []
    try:
        import certifi
        pem_parts.append(Path(certifi.where()).read_text(encoding="ascii"))
    except Exception:
        default_cafile = ssl.get_default_verify_paths().cafile
        if default_cafile and Path(default_cafile).exists():
            pem_parts.append(Path(default_cafile).read_text(encoding="ascii"))

    seen: set[bytes] = set()
    for store_name in ("ROOT", "CA"):
        try:
            certificates = ssl.enum_certificates(store_name)
        except Exception:
            certificates = []
        for certificate, encoding, _trust in certificates:
            if encoding != "x509_asn" or certificate in seen:
                continue
            seen.add(certificate)
            pem_parts.append(ssl.DER_cert_to_PEM_cert(certificate))

    if not pem_parts:
        return ""
    content = "\n".join(part.rstrip() for part in pem_parts if part.strip()) + "\n"
    try:
        if not target.exists() or target.read_text(encoding="ascii") != content:
            temporary = target.with_suffix(".tmp")
            temporary.write_text(content, encoding="ascii")
            temporary.replace(target)
    except OSError:
        return ""
    _WINDOWS_CA_BUNDLE = str(target)
    return _WINDOWS_CA_BUNDLE


def hermes_subprocess_environment() -> dict[str, str]:
    """Return a subprocess environment that preserves secure Windows TLS."""
    environment = os.environ.copy()
    certificate_variables = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
    if sys.platform.startswith("win") and not any(environment.get(name) for name in certificate_variables):
        bundle = ensure_windows_ca_bundle()
        if bundle:
            for name in certificate_variables:
                environment[name] = bundle
    return environment


def hermes_failure_detail(stdout: str = "", stderr: str = "") -> str:
    """Return a concise terminal Hermes API failure despite a zero exit code."""
    output = re.sub(
        r"\x1b\[[0-9;?]*[ -/]*[@-~]",
        "",
        "\n".join(part for part in (stderr, stdout) if part),
    )
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    terminal_failure = any(
        re.search(pattern, line, re.IGNORECASE)
        for line in lines
        for pattern in (
            r"\bAPI failed after \d+ retries\b",
            r"\bNon-retryable\b.*\bAborting\b",
            r"\bNo (?:API key|runtime API key|credentials) (?:available|found)\b",
            r"\bAuthentication (?:failed|required)\b",
        )
    )
    if not terminal_failure:
        return ""
    for line in reversed(lines):
        if re.search(r"(?:Final error|Error):\s*HTTP \d{3}", line, re.IGNORECASE):
            return line.lstrip("!❌⚠️ ").strip()
    for line in reversed(lines):
        if re.search(r"\b(?:API failed after|Non-retryable|Authentication)\b", line, re.IGNORECASE):
            return line.lstrip("!❌⚠️ ").strip()
    return "Hermes reported a terminal provider failure."


def _candidate_paths() -> list[Path]:
    candidates: list[Path] = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        root = Path(local_app_data) / "hermes"
        candidates.extend([
            root / "hermes-agent" / "venv" / "Scripts" / "hermes.exe",
            root / "bin" / "hermes.cmd",
        ])
    home = Path.home()
    candidates.extend([
        home / ".local" / "bin" / "hermes",
        home / ".hermes" / "hermes-agent" / "venv" / "bin" / "hermes",
        Path("/usr/local/bin/hermes"),
    ])
    return candidates


def find_hermes_executable(configured: str | None = None) -> str:
    configured = (configured or "hermes").strip()
    if configured:
        configured_path = Path(configured).expanduser()
        if configured_path.exists():
            return str(configured_path.resolve())
        resolved = shutil.which(configured)
        if resolved:
            return resolved
    for candidate in _candidate_paths():
        if candidate.exists():
            return str(candidate.resolve())
    return ""


def command_for(executable: str, args: Sequence[str]) -> list[str]:
    """Build a cross-platform Hermes command.

    Native Windows installs may expose a ``hermes.cmd`` shim. CreateProcess
    cannot reliably execute batch wrappers directly in every Python version, so
    route those through cmd.exe explicitly. A configured Python entry point is
    launched with the active interpreter so test shims and source installs work
    on Windows, where a shebang alone is not executable.
    """
    if Path(executable).suffix.lower() == ".py":
        return [sys.executable, executable, *args]
    if sys.platform.startswith("win") and executable.lower().endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", executable, *args]
    return [executable, *args]


class HermesBackend:
    def __init__(self, app_dir: Path, configured_executable: str = "hermes"):
        self.app_dir = app_dir.expanduser().resolve()
        self.profile_distribution = self.app_dir / "hermes_profile"
        self.configured_executable = configured_executable
        self._provider_auth_cache: dict[tuple[str, str], bool] = {}

    def executable(self) -> str:
        return find_hermes_executable(self.configured_executable)

    def run(
        self,
        args: Sequence[str],
        *,
        timeout: int = 240,
        check: bool = False,
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        executable = self.executable()
        if not executable:
            raise HermesBackendError("Hermes Agent is not installed or could not be located.")
        completed = subprocess.run(
            command_for(executable, list(args)),
            capture_output=capture_output,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            env=hermes_subprocess_environment(),
        )
        if check and completed.returncode != 0:
            raise HermesBackendError(
                completed.stderr.strip() or completed.stdout.strip() or
                f"Hermes command failed with exit code {completed.returncode}."
            )
        return completed

    def status(self, profile_name: str = DEFAULT_PROFILE) -> BackendStatus:
        executable = self.executable()
        status = BackendStatus(
            installed=bool(executable),
            executable=executable,
            profile_name=profile_name,
        )
        if not executable:
            status.message = "Hermes Agent is not installed."
            return status

        version = self.run(["--version"], timeout=45)
        status.version = (version.stdout or version.stderr).strip().splitlines()[0] if (
            version.stdout or version.stderr
        ) else "installed"

        profile = self.run(["profile", "show", profile_name], timeout=60)
        status.profile_installed = profile.returncode == 0

        help_result = self.run(["chat", "--help"], timeout=60)
        help_text = (help_result.stdout or "") + "\n" + (help_result.stderr or "")
        status.document_tooling_supported = (
            help_result.returncode == 0 and "--toolsets" in help_text and "-q" in help_text and "-s" in help_text
        )

        doctor = self.run(["doctor"], timeout=120)
        status.doctor_ok = doctor.returncode == 0
        status.model_provider = self.profile_config_value(profile_name, "model.provider")
        status.model = self.profile_config_value(profile_name, "model.default")
        status.free_route_configured = is_free_model(status.model)
        status.provider_authorized = self.provider_authorized(profile_name, status.model_provider)
        status.authorization_required = status.free_route_configured and not status.provider_authorized
        status.ready = status.installed and status.profile_installed and status.document_tooling_supported
        status.ai_ready = status.ready and status.free_route_configured and status.provider_authorized
        if status.ai_ready:
            status.message = "Hermes, the MarginMise profile, and the CostPilot free AI route are ready."
        elif status.authorization_required:
            status.message = (
                "Hermes and the CostPilot free AI route are installed. "
                "One-time provider authorization is still required."
            )
        elif status.ready:
            status.message = "Hermes and the restaurant document-extraction profile are installed."
        elif not status.profile_installed:
            status.message = "Hermes is installed, but the restaurant extraction profile is missing."
        elif not status.document_tooling_supported:
            status.message = "Hermes is installed, but its chat CLI lacks required -q, -s, or --toolsets support."
        else:
            status.message = "Hermes is installed but needs setup or repair."
        return status

    def profile_config_value(self, profile_name: str, key: str) -> str:
        completed = self.run(["-p", profile_name, "config", "get", key], timeout=45)
        if completed.returncode != 0:
            return ""
        return (completed.stdout or "").strip().splitlines()[0] if (completed.stdout or "").strip() else ""

    def _profile_has_secret(self, profile_name: str, key: str) -> bool:
        value = str(os.environ.get(key) or "").strip()
        if value:
            return True
        env_path = self.profile_directory(profile_name) / ".env"
        if not env_path.exists():
            return False
        try:
            for raw_line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, raw_value = line.split("=", 1)
                if name.strip() != key:
                    continue
                return bool(raw_value.strip().strip("'\""))
        except OSError:
            return False
        return False

    def provider_authorized(self, profile_name: str, provider: str) -> bool:
        normalized = str(provider or "").strip().lower()
        if normalized == "openrouter":
            return self._profile_has_secret(profile_name, "OPENROUTER_API_KEY")
        cache_key = (profile_name, normalized)
        if cache_key in self._provider_auth_cache:
            return self._provider_auth_cache[cache_key]
        if normalized == "nous":
            completed = self.run(["-p", profile_name, "auth", "status", "nous"], timeout=60)
            output = ((completed.stdout or "") + "\n" + (completed.stderr or "")).lower()
            authorized = completed.returncode == 0 and "logged in" in output and "not logged in" not in output
        else:
            authorized = bool(normalized)
        self._provider_auth_cache[cache_key] = authorized
        return authorized

    def _recommended_nous_free_route(self, profile_name: str) -> tuple[str, set[str]]:
        """Ask the installed Hermes runtime for its current Nous free catalog.

        Free model IDs are intentionally short-lived.  Hermes already combines
        its live pricing catalog with the Nous Portal recommendations, so reuse
        that logic instead of pinning MarginMise to another model that may be
        retired later.  An empty result is a safe, non-destructive fallback for
        older Hermes releases whose internal catalog API differs.
        """
        executable = Path(self.executable())
        scripts_dir = executable.parent
        python_names = ("python.exe",) if sys.platform.startswith("win") else ("python3", "python")
        python_executable = next(
            (scripts_dir / name for name in python_names if (scripts_dir / name).exists()),
            None,
        )
        if python_executable is None:
            return "", set()

        script = r"""
import json
try:
    from hermes_cli.auth import get_provider_auth_state
    from hermes_cli.models import (
        check_nous_free_tier,
        get_curated_nous_model_ids,
        get_pricing_for_provider,
        partition_nous_models_by_tier,
        pick_silent_default_model,
        union_with_portal_free_recommendations,
    )
    ids = get_curated_nous_model_ids()
    pricing = get_pricing_for_provider("nous", force_refresh=True)
    state = get_provider_auth_state("nous") or {}
    ids, pricing = union_with_portal_free_recommendations(
        ids,
        pricing,
        state.get("portal_base_url", "") or "",
        force_refresh=True,
    )
    free_tier = check_nous_free_tier(force_fresh=True)
    selectable, _ = partition_nous_models_by_tier(
        ids,
        pricing,
        free_tier=bool(free_tier),
    )
    def is_zero_cost(model):
        row = pricing.get(model) or {}
        try:
            return (
                float(row.get("prompt", "nan")) == 0
                and float(row.get("completion", "nan")) == 0
            )
        except (TypeError, ValueError):
            return False
    selectable = [
        model for model in selectable
        if model.endswith(":free")
        or model == "openrouter/free"
        or is_zero_cost(model)
    ]
    # MarginMise document jobs need reliable multi-turn tool calling. Prefer
    # the live free families that complete that workflow, while still requiring
    # the exact model ID to be present in Hermes's current catalog.
    family_order = (
        "inclusionai/ling-",
        "stepfun/step-",
        "poolside/laguna-xs-",
        "poolside/laguna-",
    )
    selected = next(
        (
            model
            for family in family_order
            for model in selectable
            if model.startswith(family)
        ),
        "",
    )
    if not selected:
        selected = pick_silent_default_model(selectable, provider="nous")
    print(json.dumps({"model": selected, "models": selectable}))
except Exception:
    print(json.dumps({"model": "", "models": []}))
"""
        environment = hermes_subprocess_environment()
        environment["HERMES_PROFILE"] = profile_name
        try:
            completed = subprocess.run(
                [str(python_executable), "-c", script],
                capture_output=True,
                text=True,
                timeout=90,
                encoding="utf-8",
                errors="replace",
                env=environment,
            )
        except (OSError, subprocess.SubprocessError):
            return "", set()
        payload = self._json_from_output(completed.stdout)
        model = str(payload.get("model") or "").strip()
        models = {
            str(value).strip()
            for value in payload.get("models", [])
            if str(value).strip()
        }
        return (model if model in models else ""), models

    def configure_costpilot_free_route(
        self,
        profile_name: str = DEFAULT_PROFILE,
        *,
        force: bool = False,
    ) -> bool:
        """Configure a zero-cost route without storing or inventing credentials."""
        current_provider = self.profile_config_value(profile_name, "model.provider")
        current_model = self.profile_config_value(profile_name, "model.default")
        current_provider = current_provider.strip().lower()
        if (
            not force
            and current_provider
            and current_provider not in {"openrouter", "nous"}
            and self.provider_authorized(profile_name, current_provider)
        ):
            return False
        if (
            not force
            and current_provider == "openrouter"
            and is_free_model(current_model)
            and self.provider_authorized(profile_name, current_provider)
        ):
            return False

        # Nous Portal credentials can provide a genuinely keyless-to-the-app
        # free tier after the user's one-time Hermes authorization.  Resolve the
        # model from Hermes's live catalog so retired IDs repair themselves.
        if self.provider_authorized(profile_name, "nous"):
            recommended_model, free_models = self._recommended_nous_free_route(profile_name)
            if recommended_model:
                selected_model = (
                    current_model
                    if current_provider == "nous" and current_model in free_models
                    else recommended_model
                )
                if (
                    not force
                    and current_provider == "nous"
                    and current_model == selected_model
                ):
                    return False
                for key, value in (
                    ("model.provider", "nous"),
                    ("model.default", selected_model),
                    ("model.base_url", NOUS_BASE_URL),
                ):
                    self.run(
                        ["-p", profile_name, "config", "set", key, value],
                        timeout=60,
                        check=True,
                    )
                self.run(["-p", profile_name, "config", "unset", "model.api_mode"], timeout=45)
                self.run(["-p", profile_name, "config", "unset", "fallback_providers"], timeout=45)
                self.run(["-p", profile_name, "config", "unset", "fallback_model"], timeout=45)
                self._provider_auth_cache.pop((profile_name, "nous"), None)
                return True

        if (
            not force
            and is_free_model(current_model)
            and self.provider_authorized(profile_name, current_provider)
        ):
            return False
        commands = [
            ("model.provider", COSTPILOT_FREE_PROVIDER),
            ("model.default", COSTPILOT_FREE_MODEL),
            ("model.base_url", OPENROUTER_BASE_URL),
            ("model.api_mode", "chat_completions"),
        ]
        for key, value in commands:
            self.run(
                ["-p", profile_name, "config", "set", key, value],
                timeout=60,
                check=True,
            )
        # A newly provisioned free-only route must not inherit a paid fallback.
        self.run(["-p", profile_name, "config", "unset", "fallback_providers"], timeout=45)
        self.run(["-p", profile_name, "config", "unset", "fallback_model"], timeout=45)
        return True

    def install(
        self,
        *,
        skip_setup: bool = True,
        timeout: int = 1800,
        force_refresh: bool = False,
    ) -> str:
        existing = self.executable()
        if existing and not force_refresh:
            return existing

        if sys.platform.startswith("win"):
            parameter = " -SkipSetup" if skip_setup else ""
            ps_command = (
                "$ErrorActionPreference='Stop'; "
                f"$script = irm '{WINDOWS_INSTALL_URL}'; "
                f"& ([scriptblock]::Create($script)){parameter}"
            )
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_command],
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
                env=hermes_subprocess_environment(),
            )
        else:
            suffix = " -s -- --skip-browser" if skip_setup else ""
            completed = subprocess.run(
                ["bash", "-lc", f"curl -fsSL '{POSIX_INSTALL_URL}' | bash{suffix}"],
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
                env=hermes_subprocess_environment(),
            )
        if completed.returncode != 0:
            raise HermesBackendError(f"Hermes installation failed with exit code {completed.returncode}.")
        executable = self.executable()
        if not executable:
            raise HermesBackendError(
                "Hermes installation completed, but the executable could not be found. "
                "Restart the application or sign out and back in so PATH updates are visible."
            )
        return executable

    def profile_directory(self, profile_name: str) -> Path:
        configured_home = os.environ.get("HERMES_HOME")
        if configured_home:
            home = Path(configured_home).expanduser()
        elif sys.platform.startswith("win") and os.environ.get("LOCALAPPDATA"):
            home = Path(os.environ["LOCALAPPDATA"]) / "hermes"
        else:
            home = Path.home() / ".hermes"
        return home / "profiles" / profile_name

    def _sync_distribution_owned_files(self, profile_name: str) -> None:
        target = self.profile_directory(profile_name)
        target.mkdir(parents=True, exist_ok=True)
        for filename in ("SOUL.md", "distribution.yaml"):
            source = self.profile_distribution / filename
            if source.exists():
                shutil.copy2(source, target / filename)
        skills_root = self.profile_distribution / "skills"
        if skills_root.exists():
            for source_skill in skills_root.iterdir():
                if not source_skill.is_dir():
                    continue
                target_skill = target / "skills" / source_skill.name
                if target_skill.exists():
                    shutil.rmtree(target_skill)
                target_skill.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source_skill, target_skill)

    def install_profile(self, profile_name: str = DEFAULT_PROFILE, *, force: bool = False) -> bool:
        if not self.profile_distribution.exists():
            raise HermesBackendError(f"Profile distribution is missing: {self.profile_distribution}")
        current = self.run(["profile", "show", profile_name], timeout=60)
        if current.returncode == 0:
            # Patch only distribution-owned files. Config, auth, memories, sessions,
            # and user state remain untouched. Deleting an established profile just
            # to update one invoice skill would be exceptionally bad manners.
            self._sync_distribution_owned_files(profile_name)
            return False
        self.run(
            [
                "profile", "install", str(self.profile_distribution),
                "--name", profile_name, "--alias", "--yes",
            ],
            timeout=300,
            check=True,
        )
        return True

    def launch_model_setup(self, profile_name: str = DEFAULT_PROFILE) -> None:
        """Open Hermes's provider/model setup in a real console."""
        executable = self.executable()
        if not executable:
            raise HermesBackendError("Hermes must be installed before model setup can run.")
        command = command_for(executable, ["-p", profile_name, "model"])
        if sys.platform.startswith("win"):
            subprocess.Popen(
                command,
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
                env=hermes_subprocess_environment(),
            )
        elif sys.platform == "darwin":
            quoted = " ".join(subprocess.list2cmdline([part]) for part in command)
            subprocess.Popen(["osascript", "-e", f'tell application "Terminal" to do script {json.dumps(quoted)}'])
        else:
            terminal = shutil.which("x-terminal-emulator") or shutil.which("gnome-terminal") or shutil.which("konsole")
            if terminal and Path(terminal).name == "gnome-terminal":
                subprocess.Popen([terminal, "--", *command])
            elif terminal:
                subprocess.Popen([terminal, "-e", *command])
            else:
                subprocess.Popen(command)

    def launch_setup(self, profile_name: str = DEFAULT_PROFILE, *, portal: bool = True) -> None:
        executable = self.executable()
        if not executable:
            raise HermesBackendError("Hermes must be installed before setup can run.")
        args = ["-p", profile_name, "setup"] + (["--portal"] if portal else [])
        command = command_for(executable, args)
        if sys.platform.startswith("win"):
            # Setup requires browser/OAuth or provider credentials. Give it a real
            # console rather than hiding the only human-required step.
            subprocess.Popen(
                command,
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
                env=hermes_subprocess_environment(),
            )
        elif sys.platform == "darwin":
            quoted = " ".join(subprocess.list2cmdline([part]) for part in command)
            subprocess.Popen(["osascript", "-e", f'tell application "Terminal" to do script {json.dumps(quoted)}'])
        else:
            terminal = shutil.which("x-terminal-emulator") or shutil.which("gnome-terminal") or shutil.which("konsole")
            if terminal and Path(terminal).name == "gnome-terminal":
                subprocess.Popen([terminal, "--", *command])
            elif terminal:
                subprocess.Popen([terminal, "-e", *command])
            else:
                subprocess.Popen(command)

    @staticmethod
    def _json_from_output(output: str) -> dict[str, Any]:
        cleaned = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", output or "").strip()
        try:
            value = json.loads(cleaned)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
        decoder = json.JSONDecoder()
        candidates: list[dict[str, Any]] = []
        for index, char in enumerate(cleaned):
            if char != "{":
                continue
            try:
                value, _ = decoder.raw_decode(cleaned[index:])
                if isinstance(value, dict):
                    candidates.append(value)
            except json.JSONDecodeError:
                continue
        return candidates[-1] if candidates else {}

    def probe_text(self, profile_name: str = DEFAULT_PROFILE, timeout: int = 180) -> dict[str, Any]:
        completed = self.run(
            [
                "chat", "-p", profile_name, "-q",
                'Return exactly this JSON and nothing else: {"backend_ok": true}',
            ],
            timeout=timeout,
        )
        payload = self._json_from_output(completed.stdout)
        failure = hermes_failure_detail(completed.stdout, completed.stderr)
        return {
            "ok": completed.returncode == 0 and not failure and payload.get("backend_ok") is True,
            "returncode": completed.returncode,
            "payload": payload,
            "error": failure,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }

    def probe_vision(self, profile_name: str = DEFAULT_PROFILE, timeout: int = 300) -> dict[str, Any]:
        """Compatibility name: probe supported document-tool artifact execution."""
        try:
            import fitz
        except ImportError as exc:
            return {"ok": False, "error": "PyMuPDF is not installed", "exception": str(exc)}
        with tempfile.TemporaryDirectory(prefix="restaurant_hermes_document_test_") as tmp:
            root = Path(tmp)
            pdf_path, json_path, raw_path = root / "document_test.pdf", root / "document_test.json", root / "document_test.txt"
            doc = fitz.open(); page = doc.new_page(width=700, height=300)
            page.insert_text((45,70), "DOCUMENT TEST INVOICE", fontsize=24)
            page.insert_text((45,120), "Vendor: Document Test Foods", fontsize=18)
            page.insert_text((45,165), "Invoice #: TEST-100", fontsize=18)
            page.insert_text((45,210), "Invoice Total: $42.50", fontsize=18)
            doc.save(pdf_path); doc.close()
            prompt = f"""Use terminal tools to read the local PDF at {json.dumps(str(pdf_path))}.
Write all extracted text to {json.dumps(str(raw_path))}.
Write exactly this valid JSON to {json.dumps(str(json_path))} after confirming the text:
{{"backend_ok":true,"invoice_number":"TEST-100","total":"42.50"}}
Your final response must be DONE."""
            completed = self.run(["--yolo", "chat", "-p", profile_name, "--toolsets", "terminal,skills", "-s", "ocr-and-documents", "-q", prompt], timeout=timeout)
            payload = {}
            if json_path.exists():
                try: payload = json.loads(json_path.read_text(encoding="utf-8"))
                except Exception: payload = {}
            failure = hermes_failure_detail(completed.stdout, completed.stderr)
            ok = completed.returncode == 0 and not failure and payload.get("backend_ok") is True and raw_path.exists()
            return {"ok": ok, "returncode": completed.returncode, "payload": payload, "raw_text_exists": raw_path.exists(), "error": failure, "stdout": completed.stdout, "stderr": completed.stderr}

    def ensure(
        self,
        profile_name: str = DEFAULT_PROFILE,
        *,
        auto_install: bool = True,
        install_profile: bool = True,
        configure_free_route: bool = True,
    ) -> BackendStatus:
        executable = self.executable()
        if not executable:
            if not auto_install:
                return BackendStatus(profile_name=profile_name)
            self.install(skip_setup=True)
        elif auto_install:
            help_result = self.run(["chat", "--help"], timeout=60)
            help_text = (help_result.stdout or "") + "\n" + (help_result.stderr or "")
            if help_result.returncode != 0 or not all(flag in help_text for flag in ("--toolsets", "-q", "-s")):
                self.install(skip_setup=True, force_refresh=True)
        if install_profile:
            self.install_profile(profile_name)
        if configure_free_route:
            self.configure_costpilot_free_route(profile_name)
        return self.status(profile_name)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Install and verify the Hermes invoice-extraction backend.")
    parser.add_argument("--app-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--no-install", action="store_true")
    parser.add_argument("--force-profile", action="store_true")
    parser.add_argument("--setup", action="store_true")
    parser.add_argument("--no-portal", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    backend = HermesBackend(args.app_dir)
    if args.no_install:
        if backend.executable():
            backend.install_profile(args.profile, force=args.force_profile)
        status = backend.status(args.profile)
    else:
        status = backend.ensure(args.profile, auto_install=True, install_profile=True)
    if args.json:
        print(json.dumps(status.as_dict(), indent=2))
    else:
        print(status.message)
        print(f"Executable: {status.executable or 'not found'}")
        print(f"Profile: {status.profile_name} ({'installed' if status.profile_installed else 'missing'})")
    if args.setup:
        backend.launch_setup(args.profile, portal=not args.no_portal)
    return 0 if status.ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
