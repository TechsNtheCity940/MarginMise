"""Tests for the Work-screen 'Import restaurant from folder' feature.

These tests run without pytest (call ``python tests/test_folder_import.py`` or
``uv run --with openpyxl python tests/test_folder_import.py``). They exercise
restaurant-identity discovery and the workspace-creation path used by the
button so the import flow can be validated without a Tkinter event loop.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import openpyxl  # noqa: E402

from auto_upload import discover_restaurant_identity, ensure_auto_upload_folder  # noqa: E402
from invoice_pipeline import RestaurantWorkspace  # noqa: E402

SAMPLE_DIR = ROOT / "bar-grill-month"

_failures: list[str] = []


def check(name: str, cond: bool) -> None:
    status = "PASS" if cond else "FAIL"
    print(f"{status} :: {name}")
    if not cond:
        _failures.append(name)


def test_discovery_from_readme_uses_dataset_name() -> None:
    identity = discover_restaurant_identity(SAMPLE_DIR)
    check("dataset name from README", identity["restaurant_name"] == "Barrel & Flame Bar + Grill")


def test_discovery_applies_folder_settings_over_existing_workspace(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    info = {
        "restaurant_name": "Lakeview Bistro",
        "address": "12 Harbor Rd",
        "city": "Galveston",
        "state": "TX",
        "zip": "77550",
        "latitude": "29.3013",
        "longitude": "-94.7977",
        "timezone": "America/Chicago",
        "currency": "USD",
    }
    (tmp_path / "restaurant_info.json").write_text(json.dumps(info), encoding="utf-8")
    workbook = openpyxl.Workbook()
    workbook.active.append(["Some Other Grill Weekly Sales"])
    workbook.save(tmp_path / "sales.xlsx")

    identity = discover_restaurant_identity(tmp_path)
    check("json overrides spreadsheet name", identity["restaurant_name"] == "Lakeview Bistro")
    check("address assembled", identity["address"] == "12 Harbor Rd, Galveston, TX, 77550")
    check(
        "lat/lon/timezone/currency",
        identity["latitude"] == "29.3013"
        and identity["longitude"] == "-94.7977"
        and identity["timezone"] == "America/Chicago"
        and identity["currency"] == "USD",
    )


def test_discovery_falls_back_to_folder_name(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "notes.txt").write_text("random notes", encoding="utf-8")
    identity = discover_restaurant_identity(tmp_path)
    expected = tmp_path.name.replace("-", " ").replace("_", " ").title()
    check("folder name fallback", identity["restaurant_name"] == expected)


def test_discovery_reads_coordinates_from_restaurant_config(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config = {"restaurant_name": "Coastal Tavern", "latitude": 29.7, "longitude": -95.1}
    (tmp_path / "restaurant_config.json").write_text(json.dumps(config), encoding="utf-8")
    identity = discover_restaurant_identity(tmp_path)
    check("config coords", identity["restaurant_name"] == "Coastal Tavern")
    check("config lat", identity["latitude"] == "29.7")
    check("config lon", identity["longitude"] == "-95.1")


def test_discovery_keeps_bare_address(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "restaurant_info.json").write_text(
        json.dumps({"restaurant_name": "Solo Diner", "address": "9 Main St"}), encoding="utf-8"
    )
    identity = discover_restaurant_identity(tmp_path)
    check("bare address kept as street", identity["address"] == "9 Main St")


def test_import_button_method_builds_workspace(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    info = {"restaurant_name": "Harbor Pub", "address": "5 Pier St", "city": "Dickinson", "state": "TX"}
    (tmp_path / "restaurant_info.json").write_text(json.dumps(info), encoding="utf-8")

    workspace_path = tmp_path / "MarginMise Restaurants" / "Harbor Pub"
    workspace_path.mkdir(parents=True, exist_ok=True)
    workspace = RestaurantWorkspace(workspace_path)
    identity = discover_restaurant_identity(tmp_path)
    settings = workspace.load_settings()
    settings.update({"restaurant_name": identity.get("restaurant_name") or tmp_path.name})
    for key in ("address", "latitude", "longitude", "timezone", "currency"):
        value = (identity.get(key) or "").strip()
        if value:
            settings[key] = value
    workspace.save_settings(settings)
    upload_folder = ensure_auto_upload_folder(workspace, settings["restaurant_name"])

    check("workspace restaurant name", settings["restaurant_name"] == "Harbor Pub")
    check("workspace address populated", "Dickinson, TX" in settings.get("address", ""))
    check("workspace street populated", "5 Pier St" in settings.get("address", ""))
    check("workspace upload folder created", upload_folder.exists())


def main() -> int:
    import tempfile

    base = Path(tempfile.mkdtemp(prefix="folder_import_tests_"))
    try:
        test_discovery_from_readme_uses_dataset_name()
        test_discovery_applies_folder_settings_over_existing_workspace(base / "json_ws")
        test_discovery_falls_back_to_folder_name(base / "t_folder2")
        test_discovery_reads_coordinates_from_restaurant_config(base / "config_ws")
        test_discovery_keeps_bare_address(base / "bare_addr")
        test_import_button_method_builds_workspace(base / "import_flow")
    finally:
        shutil.rmtree(base, ignore_errors=True)

    if _failures:
        print(f"\n{len(_failures)} FAILED: {_failures}")
        return 1
    print("\nALL_FOLDER_IMPORT_TESTS_PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
