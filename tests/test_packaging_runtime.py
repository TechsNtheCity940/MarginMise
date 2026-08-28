from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_production_specs_use_gui_entrypoint() -> None:
    for name in ("marginmise.spec", "marginmise_dir.spec"):
        tree = ast.parse(read(name), filename=name)
        analyses = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Analysis"]
        assert len(analyses) == 1
        entrypoint_node = analyses[0].args[0]
        assert isinstance(entrypoint_node, ast.List)
        assert isinstance(entrypoint_node.elts[0], ast.Constant)
        assert entrypoint_node.elts[0].value == "launch_gui.py"


def test_production_specs_do_not_use_bootstrap_as_entrypoint() -> None:
    for name in ("marginmise.spec", "marginmise_dir.spec"):
        source = read(name)
        assert "['bootstrap.py']" not in source
        assert "bootstrap.py" not in source.split("Analysis(", 1)[1].split(")", 1)[0]


def test_frozen_ocr_protocol_does_not_pass_a_script_to_the_exe() -> None:
    source = read("invoice_pipeline.py")
    section = source[source.index("runner = ("):source.index("completed = subprocess.run", source.index("runner = ("))]
    assert '[str(sys.executable), "--ocr-worker", "extract"]' in section
    assert "local_ocr.py" in section
    assert "str(worker)" not in section


def test_runtime_dispatches_worker_before_gui_import() -> None:
    source = read("launch_gui.py")
    assert source.index('if "--ocr-worker" in sys.argv[1:]:') < source.index("return launch_gui()")
    assert "ManagerFirstRestaurantCostControllerGUI" in source
    assert "--startup-check" in source


def test_worker_parser_accepts_source_and_frozen_forms() -> None:
    import sys
    sys.path.insert(0, str(ROOT))
    import launch_gui

    expected = (Path("/tmp/result.json"), [Path("/tmp/page.png")])
    assert launch_gui.parse_ocr_worker_args(
        ["--ocr-worker", "extract", "--output", "/tmp/result.json", "/tmp/page.png"]
    ) == expected
    assert launch_gui.parse_ocr_worker_args(
        ["extract", "--output", "/tmp/result.json", "/tmp/page.png"]
    ) == expected


def test_source_bootstrap_never_launches_from_frozen_mode() -> None:
    source = read("bootstrap.py")
    assert 'if getattr(sys, "frozen", False):' in source
    assert "Frozen bootstrap invocation rejected" in source
    assert "return launch_gui()" not in source


def test_gui_defers_matplotlib_import_until_rendering() -> None:
    for name in ("restaurant_cost_gui.py", "dashboard_widgets.py"):
        source = read(name)
        assert "def _load_matplotlib()" in source
        assert source.index("def _load_matplotlib()") < source.index("import matplotlib")
        assert "_load_matplotlib()" in source


def test_ci_smoke_test_checks_for_exactly_one_process() -> None:
    workflow = read(".github/workflows/build-windows-exe.yml")
    assert "Smoke test executable process behavior" in workflow
    assert "$copies.Count -ne 1" in workflow
    assert "Start-Process" in workflow
    assert "path: dist/MarginMise" in workflow


def test_launch_gui_source_protocol_is_parseable() -> None:
    source = read("launch_gui.py")
    ast.parse(source, filename="launch_gui.py")


def test_build_scripts_do_not_build_bootstrap() -> None:
    for name in ("build_exe_small.bat", "build_exe_fast.bat"):
        assert "bootstrap.py" not in read(name)
        assert "marginmise_dir.spec" in read(name)


def test_low_end_safeguards_are_configured() -> None:
    pipeline = read("invoice_pipeline.py")
    assert '"max_source_file_mb": 50' in pipeline
    assert "Source file is" in pipeline
    assert 'context_size=2048' in read("manager_chat.py")
    assert "max_files_per_cycle" in read("auto_upload.py")
    assert '"auto_upload_max_files_per_cycle": 2' in pipeline


def test_costpilot_does_not_install_model_during_startup_check() -> None:
    source = read("restaurant_cost_gui.py")
    startup = source[source.index("def _check_costpilot_first_run"):source.index("def _check_local_costpilot")]
    assert "ensure_local_ai(auto_install=True)" not in startup
    assert "local_ai_status()" in startup


def test_low_end_build_is_onedir_and_installer_has_dependencies() -> None:
    onedir = read("marginmise_dir.spec")
    assert "exclude_binaries=True" in onedir
    assert "COLLECT(" in onedir
    installer = read("marginmise_installer.spec")
    assert "requirements.txt" in installer
    assert "assets" in installer


def test_windows_ci_builds_and_uploads_onedir_output() -> None:
    workflow = read(".github/workflows/build-windows-exe.yml")
    assert "marginmise_dir.spec" in workflow
    assert "dist\\MarginMise\\MarginMise.exe" in workflow
    assert "path: dist/MarginMise" in workflow
    assert "--startup-check" in workflow


def test_installer_is_per_user_and_build_installs_runtime_dependencies() -> None:
    assert "HKCU" in read("installer/marginmise.nsi")
    build = read("build_installer.bat")
    assert "-r requirements.txt" in build
    assert "if exist \"%DIST_FOLDER%\" rmdir /s /q \"%DIST_FOLDER%\"" in build


def test_legacy_costpilot_check_has_no_recurring_timer() -> None:
    source = read("restaurant_cost_gui.py")
    legacy = source[source.index("def _check_local_costpilot"):source.index("def install_repair_local_costpilot")]
    assert "after(" not in legacy
    assert "Thread(" not in legacy


def test_automation_defaults_to_posting_clean_records() -> None:
    source = read("invoice_pipeline.py")
    assert "needs_review = bool(errors) or unrecognized_requires_review or recovered_requires_review" in source
    assert '"require_review_for_unrecognized_vendors": False' in source
    assert '"auto_learn_validated_vendors": True' in source
