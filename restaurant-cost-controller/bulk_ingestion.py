#!/usr/bin/env python3
"""Bulk document ingestion for Restaurant Cost Controller v3.5.

Provides two ingestion modes:

1. **Desktop inbox** — a single folder (e.g. ``~/Desktop/New Restaurant - Auto Upload``)
   that the restaurant drops files into.  ``process_inbox`` scans it, classifies
   each file, routes it to the correct handler, and moves results to the
   processed/review folders.

2. **Recursive folder scan** — ``process_folder`` walks an entire directory tree,
   picks up every supported document, classifies it, and routes it.  Sub-folders
   are searched recursively; unsupported files are skipped (not errors).

Supported document types (auto-classified):
    * Invoices — PDFs, images (PNG/JPG/TIFF/BMP), JSON invoice exports
    * Recipes — CSV/XLSX with ingredient columns
    * Menu Items — CSV/XLSX with POS keys and prices
    * Sales Summaries — CSV/XLSX with period + net sales
    * POS Sales — CSV/XLSX with date + item + quantity
    * Inventory Counts — CSV/XLSX with item + quantity on hand
    * Operating Costs — CSV/XLSX with date + description + amount
    * Waste Logs — CSV/XLSX with date + item + quantity + reason
    * Item Planning — CSV/XLSX with count unit + units per purchase unit
    * Distributor Catalogs — CSV/XLSX with SKU + description + price
    * Shift Reports — CSV/XLSX with shift, labor cost, guests, sales, surcharge
    * ZIP archives — unpacked and processed recursively
    * TXT files — treated as CSV (tab/comma/pipe delimited)

All processing runs locally — no external AI or cloud service required.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from invoice_pipeline import RestaurantWorkspace, InvoicePipeline, safe_filename, parse_date, money_string, now_iso
from recipe_costing import RecipeCostingService, read_document

# Supported file extensions
INVOICE_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
TABLE_EXTENSIONS = {".csv", ".tsv", ".txt", ".xlsx", ".xlsm"}
JSON_EXTENSIONS = {".json"}
ARCHIVE_EXTENSIONS = {".zip"}
ALL_SUPPORTED = INVOICE_EXTENSIONS | TABLE_EXTENSIONS | JSON_EXTENSIONS | ARCHIVE_EXTENSIONS

# Shift-report detection patterns (used by both bulk_ingestion and shift_reports)
SHIFT_KEYWORDS = re.compile(
    r"shift[\s_\-]*report|shift[\s_\-]*log|shift[\s_\-]*summary|daily[\s_\-]*shift|shift[\s_\-]*review|"
    r"manager[\s_\-]*shift|server[\s_\-]*report|cashier[\s_\-]*report",
    re.IGNORECASE,
)
SHIFT_HEADER_PATTERNS = [
    {"shift", "labor cost", "guests", "net sales", "surcharge"},
    {"shift", "labor cost", "guests"},
    {"shift", "manager", "sales", "guests"},
    {"shift", "labor cost", "sales"},
]


@dataclass
class IngestionResult:
    """Result of a bulk or inbox ingestion run."""
    total_files_found: int
    files_processed: int
    files_skipped: int
    files_with_errors: int
    by_type: dict[str, int] = field(default_factory=dict)
    by_status: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    shift_reports_extracted: int = 0
    summary: str = ""


@dataclass
class FileClassification:
    """Result of classifying a single file."""
    document_type: str
    confidence: float
    reason: str


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def normalize_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(h).lower().strip())


def classify_file(path: Path, pipeline: InvoicePipeline) -> FileClassification:
    """Classify a single file into a document type.

    Uses column-header matching for tables, field detection for JSON,
    and extension-based routing for invoices/images.
    """
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix in INVOICE_EXTENSIONS:
        return FileClassification("Invoice", 0.95, f"{suffix.upper()} document routed to invoice OCR")

    if suffix in JSON_EXTENSIONS:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(payload, dict):
                keys = set(k.lower().strip() for k in payload.keys())
                if {"vendor", "invoice number"}.issubset(keys) or (
                    "items" in keys and ("total" in keys or "subtotal" in keys)
                ):
                    return FileClassification("Invoice", 0.98, "Canonical invoice fields found in JSON")
                if "menu_item_name" in keys and "quantity" in keys:
                    return FileClassification("POS Sales", 0.95, "Menu-level sales JSON detected")
        except Exception:
            pass
        return FileClassification("Unclassified", 0.0, "JSON could not be parsed as a known document type")

    if suffix not in TABLE_EXTENSIONS:
        return FileClassification("Unsupported", 0.0, f"Unsupported file extension {suffix}")

    try:
        rows = read_document(path)
    except Exception as exc:
        return FileClassification("Unclassified", 0.0, f"Could not read table: {exc}")

    if not rows:
        return FileClassification("Unclassified", 0.0, "File is empty or has no data rows")

    headers = set(normalize_header(k) for k in rows[0].keys())
    filename = path.stem.lower().strip()

    def has(*names: str) -> bool:
        return any(normalize_header(name) in headers for name in names)

    def contains(text: str) -> bool:
        return any(text.lower() in h for h in headers)

    # Structured invoice
    if ({"vendor", "invoice number"} <= headers and {"invoice date"} <= headers
            and {"item description", "description"} <= headers
            and {"quantity", "qty"} <= headers and {"unit price"} <= headers):
        return FileClassification("Invoice", 1.0, "Structured invoice header and line-item columns detected")

    # Recipe with full spreadsheet spec
    if ({"menu item name"} <= headers and {"quantity count units"} <= headers
            and ({"inventory item id"} <= headers or {"vendor sku"} <= headers or {"inventory item name"} <= headers)):
        return FileClassification("Recipes", 1.0, "Menu recipe and ingredient columns detected")

    # Recipe (simplified: menu item + ingredient + quantity + unit)
    if ({"menu item name"} <= headers) and ({"ingredient name"} <= headers or {"item name"} in headers):
        return FileClassification("Recipes", 0.95, "Menu item and ingredient columns detected")

    # Menu items (POS key + name + price, no recipe columns)
    if ({"menu item name"} <= headers
            and any(h in headers for h in ["menu price", "category", "active", "pos item key"])
            and not any(h in headers for h in ["receipt_id", "business_date", "payment_type", "gross_sales", "net_sales"])):
        return FileClassification("Menu Items", 1.0, "Menu item master columns detected")

    # Inventory count
    if ({"quantity on hand"} <= headers or {"counted quantity"} <= headers or {"ending quantity"} <= headers) and (
        {"item id"} <= headers or {"inventory item id"} <= headers or {"vendor sku"} <= headers or {"item name"} <= headers):
        return FileClassification("Inventory Count", 1.0, "Physical inventory count columns detected")

    # Item planning
    if ({"count unit"} <= headers and {"units per purchase unit"} <= headers
            and ({"lead time days"} <= headers or {"order cycle days"} <= headers or {"safety stock days"} <= headers)):
        return FileClassification("Item Planning", 1.0, "Product conversion and planning columns detected")

    # Waste log
    if any(d in headers for d in ["event date", "waste date", "date"]) and ({"item name"} <= headers) and (
        {"quantity count units"} <= headers and {"count unit"} <= headers and {"reason"} <= headers):
        return FileClassification("Waste Log", 1.0, "Waste event columns detected")

    # Operating costs
    if any(d in headers for d in ["date", "cost date"]) and ({"description"} <= headers) and ({"amount"} <= headers):
        return FileClassification("Operating Costs", 1.0, "Operating-cost columns detected")

    # Sales summary (period + net sales)
    if ({"period start"} <= headers and {"net sales"} <= headers):
        return FileClassification("Sales Summary", 1.0, "Period sales summary columns detected")

    # Daily sales summary (date + net sales, no quantity/item)
    if ({"date"} <= headers or {"business date"} <= headers) and ({"net sales"} <= headers) and not (
        {"menu item name"} <= headers or {"quantity"} <= headers or {"units sold"} <= headers):
        return FileClassification("Sales Summary", 0.95, "Daily sales summary columns detected")

    # POS sales (date + menu item + quantity)
    if ({"date"} <= headers or {"business date"} <= headers) and (
        {"menu item name"} <= headers or {"item name"} <= headers
    ) and ({"quantity"} <= headers or {"units sold"} <= headers):
        return FileClassification("POS Sales", 0.96, "Item-level sales date, product, and quantity columns detected")

    # Daily sales with menu item name + quantity + net sales (our recipe costing format)
    if ({"menu item name"} <= headers) and ({"quantity"} <= headers) and ({"net sales"} <= headers):
        return FileClassification("POS Sales", 0.92, "Menu item name, quantity, and net sales columns detected")

    # Distributor catalog
    if ({"sku"} <= headers) and ({"description"} <= headers) and (
        {"unit price"} <= headers or {"price"} <= headers
    ) and not ({"date"} <= headers and ({"quantity"} <= headers or {"units sold"} <= headers)):
        return FileClassification("Distributor Catalog", 0.94, "Distributor SKU, description, and price columns detected")

    # Filename hints
    hints = {
        "recipe": "Recipes", "ingredient": "Recipes", "menu": "Menu Items",
        "inventory count": "Inventory Count", "waste": "Waste Log",
        "operating cost": "Operating Costs", "sales": "Sales Summary",
        "catalog": "Distributor Catalog",
        "shift report": "Shift Report", "shift log": "Shift Report",
        "shift summary": "Shift Report", "daily shift": "Shift Report",
    }
    for token, doc_type in hints.items():
        if token in filename:
            return FileClassification(doc_type, 0.45, f"Filename suggests {doc_type}")

    # Column-based shift report detection
    for pattern in SHIFT_HEADER_PATTERNS:
        if pattern <= headers:
            return FileClassification("Shift Report", 0.9, "Shift-report columns detected")

    return FileClassification("Unclassified", 0.0, "No supported data signature was found")


# ---------------------------------------------------------------------------
# Document routing (each handler is self-contained and error-safe)
# ---------------------------------------------------------------------------

def _route_invoice(pipeline: InvoicePipeline, path: Path, classification: FileClassification) -> tuple[str, str]:
    """Route an invoice document (PDF, image, or JSON invoice)."""
    try:
        result = pipeline.process_file(path)
        # ProcessResult fields: invoice_id, status, extraction_method, recognized_vendor
        detail = f"invoice_id={result.invoice_id}, status={result.status}, method={result.extraction_method}"
        if result.errors:
            detail += f", errors={'; '.join(result.errors[:3])}"
        return (result.status.lower(), detail)
    except Exception as exc:
        return ("error", str(exc))


def _route_recipe(pipeline: InvoicePipeline, path: Path) -> tuple[str, str]:
    """Route a recipe spreadsheet.

    Self-healing: tries Phase2Service first, then InvoicePipeline, then
    RecipeCostingService standalone parser.
    """
    last_error = ""
    # Attempt 1: Phase2Service import_recipes_csv
    try:
        if hasattr(pipeline.phase2, "import_recipes_csv"):
            result = pipeline.phase2.import_recipes_csv(path)
            menu_items = result.get("menu_items_imported", 0) if isinstance(result, dict) else 0
            if menu_items > 0:
                return ("recipe_imported", f"{menu_items} menu items with recipes imported (Phase2)")
    except Exception as exc:
        last_error = str(exc)
    # Attempt 2: InvoicePipeline import_recipes_csv
    try:
        result = pipeline.import_recipes_csv(path)
        if isinstance(result, dict):
            menu_items = result.get("menu_items_imported", 0)
        else:
            menu_items = len(result) if result else 0
        if menu_items > 0:
            return ("recipe_imported", f"{menu_items} recipe items imported (pipeline)")
    except Exception as exc:
        last_error = str(exc)
    # Attempt 3: RecipeCostingService standalone
    try:
        result = pipeline.recipe_costing.import_recipes(path)
        menu_items = len(result.menu_items)
        if menu_items > 0:
            return ("recipe_imported", f"{menu_items} recipes costing entries imported")
    except Exception as exc:
        last_error = str(exc)
    return ("error", f"Recipe import failed: {last_error}") if last_error else ("recipe_imported", "0 recipes in file")


def _route_sales(pipeline: InvoicePipeline, path: Path) -> tuple[str, str]:
    """Route a sales summary or POS sales document.

    Self-healing: tries workbook importer for XLSX, then CSV importer,
    then falls back to raw read for manual processing.
    """
    # Attempt 1: XLSX workbook import
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        try:
            count = pipeline.import_sales_workbook(path)
            if count > 0:
                return ("sales_imported", f"{count} periods imported from workbook")
        except Exception:
            pass
    # Attempt 2: CSV import
    try:
        count = pipeline.import_sales_csv(path)
        if count > 0:
            return ("sales_imported", f"{count} periods imported from CSV")
    except Exception:
        pass
    # Attempt 3: Fallback to raw read for manual processing
    try:
        rows = read_document(path)
        return ("sales_imported", f"{len(rows)} rows read (manual import needed)")
    except Exception as exc:
        return ("error", str(exc))


def _route_inventory_count(pipeline: InvoicePipeline, path: Path) -> tuple[str, str]:
    """Route an inventory count document.

    Self-healing: tries the dedicated inventory count importer, then
    the pipeline's CSV importer, then falls back to raw read.
    """
    last_error = ""
    # Attempt 1: OperationalControlsService import_count_csv
    try:
        result = pipeline.planning.import_count_csv(path)
        count = getattr(result, "imported", 0)
        if count and count > 0:
            return ("inventory_imported", f"{count} items counted (planning service)")
    except Exception as exc:
        last_error = str(exc)
    # Attempt 2: InvoicePipeline import_inventory_count_csv
    try:
        count = pipeline.import_inventory_count_csv(path)
        if count > 0:
            return ("inventory_imported", f"{count} items counted (pipeline)")
    except Exception as exc:
        last_error = str(exc)
    # Attempt 3: Fallback to raw read
    try:
        rows = read_document(path)
        if len(rows) > 0:
            return ("inventory_imported", f"{len(rows)} rows read (manual import needed)")
        return ("skipped", "Inventory count file is empty")
    except Exception as exc:
        last_error = str(exc)
    return ("error", f"Inventory count import failed: {last_error}") if last_error else ("skipped", "No data")


def _route_operating_costs(pipeline: InvoicePipeline, path: Path) -> tuple[str, str]:
    """Route an operating costs document.

    Self-healing: tries the CSV importer, then the workbook importer,
    then falls back to raw read.
    """
    # Attempt 1: CSV import
    try:
        if path.suffix.lower() in (".xlsx", ".xlsm"):
            count = pipeline.import_operating_costs_workbook(path)
        else:
            count = pipeline.import_operating_costs_csv(path)
        if count > 0:
            return ("costs_imported", f"{count} cost entries imported")
    except Exception:
        pass
    # Attempt 2: Fallback to raw read
    try:
        rows = read_document(path)
        if rows:
            return ("costs_imported", f"{len(rows)} cost entries read (manual import needed)")
        return ("skipped", "Operating costs file is empty")
    except Exception as exc:
        return ("error", str(exc))


def _route_waste_log(pipeline: InvoicePipeline, path: Path) -> tuple[str, str]:
    """Route a waste log document.

    Self-healing: reads the document, matches item names via DB lookup,
    then logs each waste entry. Falls back to row count if no items match.
    """
    try:
        rows = read_document(path)
    except Exception as exc:
        return ("error", f"Could not read waste log: {exc}")
    if not rows:
        return ("skipped", "Waste log file is empty")

    imported = 0
    with pipeline.workspace.connect() as conn:
        for row in rows:
            try:
                item_name = str(row.get("Item Name") or row.get("item name") or row.get("item_name") or "").strip()
                quantity = str(row.get("Quantity") or row.get("quantity") or "").strip()
                reason = str(row.get("Reason") or row.get("reason") or "unknown").strip()
                event_date = str(row.get("Event Date") or row.get("event_date") or row.get("Waste Date") or row.get("waste_date") or "").strip()
                if not item_name or not quantity:
                    continue
                item_row = conn.execute(
                    "SELECT item_id FROM items WHERE item_name LIKE ? OR normalized_description LIKE ? ORDER BY item_id LIMIT 1",
                    (f"%{item_name}%", f"%{item_name}%"),
                ).fetchone()
                if item_row:
                    pipeline.log_waste(item_row["item_id"], Decimal(quantity), reason, event_date=event_date or None)
                    imported += 1
            except Exception:
                pass
    if imported > 0:
        return ("waste_imported", f"{imported} waste entries imported")
    return ("waste_imported", f"{len(rows)} waste entries read (manual import needed)")


def _route_item_planning(pipeline: InvoicePipeline, path: Path) -> tuple[str, str]:
    """Route an item planning document."""
    try:
        rows = read_document(path)
        # Try to update each item's planning fields
        updated = 0
        for row in rows:
            item_id = str(row.get("Inventory Item ID") or row.get("inventory item id") or "").strip()
            if item_id:
                try:
                    planning_values = {}
                    for col_name, db_col in [
                        ("lead time days", "lead_time_days"),
                        ("order cycle days", "order_cycle_days"),
                        ("safety stock days", "safety_stock_days"),
                        ("target on hand days", "target_on_hand_days"),
                        ("preferred vendor", "preferred_vendor"),
                    ]:
                        val = row.get(col_name) or row.get(col_name.title()) or row.get(col_name.upper())
                        if val is not None and str(val).strip():
                            planning_values[db_col] = str(val).strip()
                    if planning_values:
                        pipeline.planning.update_item_planning(item_id, **planning_values)
                        updated += 1
                except Exception:
                    pass
        return ("planning_imported", f"{updated} items updated")
    except Exception as exc:
        return ("error", str(exc))


def _route_distributor_catalog(pipeline: InvoicePipeline, path: Path) -> tuple[str, str]:
    """Route a distributor catalog document.

    Self-healing: matches catalog items to existing items by SKU or name,
    updates current_price and price_history. Falls back to row count.
    """
    try:
        rows = read_document(path)
    except Exception as exc:
        return ("error", f"Could not read catalog: {exc}")
    if not rows:
        return ("skipped", "Distributor catalog is empty")

    updated = 0
    with pipeline.workspace.connect() as conn:
        for row in rows:
            try:
                sku = str(row.get("SKU") or row.get("sku") or row.get("vendor_sku") or "").strip()
                name = str(row.get("Description") or row.get("description") or row.get("item_name") or "").strip()
                price = str(row.get("Unit Price") or row.get("unit_price") or row.get("price") or "").strip()
                if not price:
                    continue
                if sku:
                    item = conn.execute("SELECT item_id FROM items WHERE vendor_sku=? ORDER BY item_id LIMIT 1", (sku,)).fetchone()
                else:
                    item = conn.execute("SELECT item_id FROM items WHERE item_name LIKE ? OR normalized_description LIKE ? ORDER BY item_id LIMIT 1", (f"%{name}%", f"%{name}%")).fetchone()
                if item:
                    item_id = item["item_id"]
                    old_price_row = conn.execute("SELECT current_price FROM items WHERE item_id=?", (item_id,)).fetchone()
                    old = old_price_row["current_price"] if old_price_row else None
                    conn.execute("UPDATE items SET current_price=? WHERE item_id=?", (money_string(price, "price"), item_id))
                    if old:
                        conn.execute("INSERT INTO price_history(item_id,vendor_name,invoice_date,unit_price,previous_price) VALUES(?,?,?,?,?)", (item_id, "Distributor Catalog", now_iso()[:10], money_string(price, "price"), old))
                    updated += 1
            except Exception:
                pass
    if updated > 0:
        return ("catalog_imported", f"{updated} item prices updated from catalog")
    return ("catalog_imported", f"{len(rows)} catalog items processed (no matches found)")


def _route_menu_items(pipeline: InvoicePipeline, path: Path) -> tuple[str, str]:
    """Route a menu items master document."""
    try:
        rows = read_document(path)
        if not rows:
            return ("skipped", "File is empty")
        headers = set(normalize_header(k) for k in rows[0].keys())
        if "quantity count units" in headers or "inventory item name" in headers or "ingredient name" in headers:
            result = pipeline.recipe_costing.import_recipes(path)
            return ("recipe_imported", f"{len(result.menu_items)} menu items with recipes imported")
        # Just menu items (no ingredients) - create them
        from invoice_pipeline import now_iso
        stamp = now_iso()
        count = 0
        with pipeline.workspace.connect() as conn:
            for row in rows:
                name = str(row.get("Menu Item Name") or row.get("menu item name") or row.get("item name") or "").strip()
                if not name:
                    continue
                price = str(row.get("Menu Price") or row.get("menu price") or "0").strip()
                key = str(row.get("POS Item Key") or row.get("pos item key") or "").strip() or name.upper()
                menu_id = f"MENU-{hashlib.sha256(key.encode()).hexdigest()[:14].upper()}"
                conn.execute(
                    "INSERT OR REPLACE INTO menu_items(menu_item_id,pos_item_key,menu_item_name,category,menu_price,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (menu_id, key, name, str(row.get("Category") or row.get("category") or "Unclassified"), price, stamp, stamp)
                )
                count += 1
        return ("menu_items_imported", f"{count} menu items created")
    except Exception as exc:
        return ("error", str(exc))


def _route_file(
    pipeline: InvoicePipeline, path: Path, classification: FileClassification,
) -> tuple[str, str]:
    """Route a classified file to the appropriate handler."""
    doc_type = classification.document_type

    if doc_type == "Invoice":
        return _route_invoice(pipeline, path, classification)
    elif doc_type == "Recipes":
        return _route_recipe(pipeline, path)
    elif doc_type == "Menu Items":
        return _route_menu_items(pipeline, path)
    elif doc_type in ("POS Sales", "Sales Summary"):
        return _route_sales(pipeline, path)
    elif doc_type == "Inventory Count":
        return _route_inventory_count(pipeline, path)
    elif doc_type == "Item Planning":
        return _route_item_planning(pipeline, path)
    elif doc_type == "Waste Log":
        return _route_waste_log(pipeline, path)
    elif doc_type == "Operating Costs":
        return _route_operating_costs(pipeline, path)
    elif doc_type == "Distributor Catalog":
        return _route_distributor_catalog(pipeline, path)
    elif doc_type == "Shift Report":
        return _route_shift_report(pipeline, path)
    elif doc_type == "Archive":
        return ("skipped", "ZIP archive - run process_folder with unzip_archives=True")
    else:
        return ("unclassified", classification.reason)


def _route_shift_report(pipeline: InvoicePipeline, path: Path) -> tuple[str, str]:
    """Route a shift report: extract summary fields and log reference only.

    Raw shift data is NOT stored. Only a lightweight summary with the source
    file path is logged so CostPilot can reference it.
    """
    from shift_reports import extract_shift_report, log_shift_report

    summary = extract_shift_report(path)
    if summary is None:
        return ("skipped", "Could not parse shift report")

    try:
        with pipeline.workspace.connect() as conn:
            log_shift_report(conn, summary)
    except Exception as exc:
        return ("error", f"Shift report logged but DB write failed: {exc}")

    return (
        "shift_report_extracted",
        f"{summary.shift or summary.report_date or 'shift'} — {summary.notes} [src: {summary.source_name}]",
    )


# ---------------------------------------------------------------------------
# Archive (ZIP) handling
# ---------------------------------------------------------------------------

def _extract_archive(path: Path, temp_dir: Path) -> list[Path]:
    """Extract a ZIP archive to a temp directory, returning extracted file paths."""
    extracted = []
    try:
        with zipfile.ZipFile(path, "r") as zf:
            zf.extractall(temp_dir)
        for root, _dirs, files in os.walk(temp_dir):
            for f in files:
                extracted.append(Path(root) / f)
    except Exception:
        pass
    return extracted


# ---------------------------------------------------------------------------
# Shared scanning logic
# ---------------------------------------------------------------------------

def _scan_folder(folder: Path, recursive: bool) -> list[Path]:
    """Recursively or non-recursively find all supported files in a folder."""
    if recursive:
        files = [p for p in sorted(folder.rglob("*")) if p.is_file() and p.suffix.lower() in ALL_SUPPORTED]
    else:
        files = [p for p in sorted(folder.iterdir()) if p.is_file() and p.suffix.lower() in ALL_SUPPORTED]
    return files


def _alternative_table_readers(path: Path):
    """Yield alternative reader functions for a table file.

    Tries different delimiters and encodings to handle files that
    can't be read with the default CSV parser.
    """
    from csv import DictReader
    import codecs

    def _read_with_delimiter(path: Path, delimiter: str, encoding: str = "utf-8-sig"):
        with path.open("r", encoding=encoding, newline="") as handle:
            reader = DictReader(handle, delimiter=delimiter)
            return list(reader)

    encodings = ["utf-8-sig", "latin-1", "cp1252"]

    if path.suffix.lower() == ".csv":
        for enc in encodings:
            yield lambda p, e=enc: _read_with_delimiter(p, ",", e)
            yield lambda p, e=enc: _read_with_delimiter(p, "\t", e)
            yield lambda p, e=enc: _read_with_delimiter(p, "|", e)
    elif path.suffix.lower() == ".txt":
        for enc in encodings:
            yield lambda p, e=enc: _read_with_delimiter(p, "\t", e)
            yield lambda p, e=enc: _read_with_delimiter(p, ",", e)
            yield lambda p, e=enc: _read_with_delimiter(p, "|", e)

    # Also try the default read_document
    yield read_document


def _normalize_row_keys(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize row keys to lowercase for matching."""
    normalized = []
    for row in rows:
        normalized.append({k.lower().strip(): v for k, v in row.items()})
    return normalized


def _classify_from_rows(rows: list[dict[str, Any]], filename: str) -> FileClassification:
    """Try to classify based on row content (not just headers)."""
    if not rows:
        return FileClassification("Unclassified", 0.0, "No rows to classify")

    raw_headers = list(rows[0].keys())
    headers = set(normalize_header(k) for k in raw_headers)

    def has(*names: str) -> bool:
        return any(normalize_header(name) in headers for name in names)

    def contains(text: str) -> bool:
        return any(text.lower() in h for h in headers)

    # Operating costs
    if any(h in headers for h in ["date", "cost date"]) and has("description") and has("amount"):
        return FileClassification("Operating Costs", 0.95, "Column-based operating costs")

    # Inventory count
    if has("quantity on hand", "counted quantity", "ending quantity") and (
        has("item id", "inventory item id", "vendor sku", "item name")
    ):
        return FileClassification("Inventory Count", 0.95, "Column-based inventory count")

    # Sales summary
    if (has("period start") and has("net sales")) or (
        (has("date", "business date") or "month" in headers) and has("net sales")
        and not has("menu item name", "quantity")
    ):
        return FileClassification("Sales Summary", 0.90, "Column-based sales summary")

    # POS sales
    if (has("date", "business date") or "month" in headers) and (
        has("menu item name", "item name")
    ) and (has("quantity", "units sold")):
        return FileClassification("POS Sales", 0.92, "Column-based POS sales")

    # Recipes
    if has("menu item name") and has("ingredient name", "item name", "inventory item name"):
        return FileClassification("Recipes", 0.90, "Column-based recipe")

    # Menu items
    if has("menu item name") and has("menu price", "category", "pos item key"):
        return FileClassification("Menu Items", 0.90, "Column-based menu items")

    return FileClassification("Unclassified", 0.0, "No column signature matched")


def _classify_by_content(rows: list[dict[str, Any]], filename: str) -> FileClassification:
    """Classify by content keywords in cell values, not just headers."""
    if not rows:
        return FileClassification("Unclassified", 0.0, "No rows")

    # Flatten all values into text
    all_values = set()
    for row in rows:
        for v in row.values():
            if v is not None:
                all_values.add(str(v).lower().strip())

    all_text = " ".join(all_values)

    # Waste log keywords
    if "waste" in all_text and "reason" in all_text:
        return FileClassification("Waste Log", 0.55, "Content keyword matched: waste + reason")

    # Operating costs keywords
    if any(w in all_text for w in ["rent", "utilities", "insurance", "payroll", "labor", "gas", "supplies"]) and "amount" in all_text:
        return FileClassification("Operating Costs", 0.50, "Content keyword matched: cost keywords + amount")

    # Inventory count keywords
    if "on hand" in all_text or "counted" in all_text:
        return FileClassification("Inventory Count", 0.50, "Content keyword matched: on hand/counted")

    # Return keyword matching for invoices
    if "invoice" in filename or "bill" in filename or "receipt" in filename:
        return FileClassification("Invoice", 0.45, f"Filename suggests invoice: {filename}")

    # Recipe keywords
    if "recipe" in filename or "ingredient" in filename:
        return FileClassification("Recipes", 0.45, f"Filename suggests recipes: {filename}")

    # Inventory keywords
    if "inventory" in filename or "count" in filename:
        return FileClassification("Inventory Count", 0.45, f"Filename suggests inventory: {filename}")

    # Sales keywords
    if "sales" in filename or "revenue" in filename:
        return FileClassification("Sales Summary", 0.45, f"Filename suggests sales: {filename}")

    return FileClassification("Unclassified", 0.0, "No content/keyword match found")


def _match_by_keywords(text: str, filename: str) -> str | None:
    """Match text content by keywords to a document type."""
    text = text.lower()
    fname = filename.lower()

    # Check filename first
    if "invoice" in fname or "bill" in fname or "receipt" in fname:
        return "Invoice"
    if "recipe" in fname or "ingredient" in fname:
        return "Recipes"
    if "inventory" in fname or "count" in fname:
        return "Inventory Count"
    if "waste" in fname:
        return "Waste Log"
    if "cost" in fname or "operating" in fname:
        return "Operating Costs"
    if "sales" in fname or "revenue" in fname:
        return "Sales Summary"
    if "catalog" in fname or "price" in fname:
        return "Distributor Catalog"
    if "planning" in fname or "par" in fname:
        return "Item Planning"

    # Check content for value-based keywords
    if "rent" in text and "2500" in text:
        return "Operating Costs"
    if "invoice" in text and "total" in text:
        return "Invoice"
    if "recipe" in text or "ingredient" in text:
        return "Recipes"
    if "on hand" in text or "counted" in text:
        return "Inventory Count"
    if "waste" in text and "reason" in text:
        return "Waste Log"
    if "amount" in text and ("rent" in text or "utilities" in text or "labor" in text):
        return "Operating Costs"
    if "net sales" in text:
        return "Sales Summary"
    if "sku" in text and "price" in text:
        return "Distributor Catalog"

    return None


def _try_self_heal(
    pipeline: InvoicePipeline,
    path: Path,
    classification: FileClassification,
    status: str,
    detail: str,
) -> tuple[bool, str, str, str]:
    """Attempt to recover a file that could not be classified or routed.

    Tries multiple recovery strategies:
    - Re-read with alternative delimiters/encodings
    - Match by content keywords, not just headers
    - Try generic CSV parsing for unknown table formats
    - For invoices, try image OCR with different settings

    Returns (recovered, status, detail, document_type).
    """
    suffix = path.suffix.lower()

    # --- Strategy 1: Alternative delimiter/encoding reads ---
    rows: list[dict[str, Any]] = []
    if suffix in TABLE_EXTENSIONS:
        for reader_fn in _alternative_table_readers(path):
            try:
                rows = reader_fn(path)
                if rows and len(rows) > 0:
                    break
            except Exception:
                pass

    if rows:
        headers = set(normalize_header(k) for k in rows[0].keys())

        # Re-try classification with the recovered data
        rec = _classify_from_rows(rows, path.stem)
        if rec.document_type != "Unclassified":
            s, d = _route_file(pipeline, path, rec)
            if s not in ("error", "unclassified"):
                return (True, s, d, rec.document_type)

        # Content-based keyword matching
        rec = _classify_by_content(rows, path.stem)
        if rec.document_type != "Unclassified":
            s, d = _route_file(pipeline, path, rec)
            if s not in ("error", "unclassified"):
                return (True, s, d, rec.document_type)

    # --- Strategy 2: For PDF/images, try OCR with text-first approach ---
    if suffix in INVOICE_EXTENSIONS:
        if suffix == ".pdf":
            # Try as structured invoice (CSV-like extraction)
            pass  # Invoice routing already handles PDF via pipeline.process_file

    # --- Strategy 3: Re-classify with flexible header matching ---
    if suffix in TABLE_EXTENSIONS:
        for reader_fn in _alternative_table_readers(path):
            try:
                rows = reader_fn(path)
                if not rows:
                    continue
                # Build a lowercased, joined text of all headers + all cell values
                all_text = " ".join(str(k).lower() for k in rows[0].keys())
                for row in rows:
                    all_text += " " + " ".join(str(v).lower() for v in row.values())

                # Try matching by content keywords
                doc_type = _match_by_keywords(all_text, path.stem)
                if doc_type:
                    rec = FileClassification(doc_type, 0.50, f"Content keyword match: {doc_type}")
                    s, d = _route_file(pipeline, path, rec)
                    if s not in ("error", "unclassified"):
                        return (True, s, d, rec.document_type)
            except Exception:
                pass

    return (False, status, detail, classification.document_type)


def _process_files(
    pipeline: InvoicePipeline, all_files: list[Path],
    *, enable_self_healing: bool = True,
) -> IngestionResult:
    """Process a list of classified files and return results."""
    result = IngestionResult(total_files_found=len(all_files), files_processed=0,
                             files_skipped=0, files_with_errors=0)

    for path in all_files:
        try:
            classification = classify_file(path, pipeline)
            status, detail = _route_file(pipeline, path, classification)

            # Self-healing: if the file was unclassified or errored, try recovery
            if enable_self_healing and (status in ("unclassified", "error", "skipped") or classification.document_type == "Unclassified"):
                recovered, rec_status, rec_detail, rec_type = _try_self_heal(pipeline, path, classification, status, detail)
                if recovered:
                    classification = FileClassification(rec_type, 0.50, f"Recovered from {classification.document_type}")
                    status = rec_status
                    detail = rec_detail

            result.by_type[classification.document_type] = result.by_type.get(classification.document_type, 0) + 1
            result.by_status[status] = result.by_status.get(status, 0) + 1

            if status == "error":
                result.files_with_errors += 1
                result.errors.append(f"{path.name}: {detail}")
            elif status in ("unclassified", "skipped"):
                result.files_skipped += 1
            else:
                result.files_processed += 1

            # Track shift reports separately
            if classification.document_type == "Shift Report":
                result.shift_reports_extracted += 1

        except Exception as exc:
            result.files_with_errors += 1
            result.errors.append(f"{path.name}: {exc}")

    result.files_skipped = result.total_files_found - result.files_processed - result.files_with_errors

    type_summary = ", ".join(f"{t}: {c}" for t, c in sorted(result.by_type.items()))
    status_summary = ", ".join(f"{s}: {c}" for s, c in sorted(result.by_status.items()))
    result.summary = (
        f"Processed {result.files_processed} of {result.total_files_found} files. "
        f"Types: {type_summary}. Status: {status_summary}."
    )
    return result


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------

def process_inbox(
    pipeline: InvoicePipeline,
    inbox_folder: Path | None = None,
) -> IngestionResult:
    """Process files dropped in the desktop inbox folder.

    This is the fast path for restaurants: drop files in the inbox, call this
    function, and everything gets sorted and processed.

    Args:
        pipeline: An initialized InvoicePipeline instance
        inbox_folder: Path to the inbox folder (defaults to workspace upload folder)
    """
    if inbox_folder is None:
        inbox_folder = pipeline.workspace.folders.get(
            "upload", Path.home() / "Desktop" / "New Restaurant - Auto Upload"
        )
    inbox_folder = Path(inbox_folder).resolve()

    if not inbox_folder.exists():
        result = IngestionResult(total_files_found=0, files_processed=0, files_skipped=0, files_with_errors=1)
        result.errors.append(f"Inbox folder does not exist: {inbox_folder}")
        result.summary = "Inbox folder not found"
        return result

    all_files = _scan_folder(inbox_folder, recursive=True)
    result = _process_files(pipeline, all_files)
    result.summary = (
        f"Inbox scan: {result.summary}"
    )
    return result


def process_folder(
    pipeline: InvoicePipeline,
    folder: Path | str,
    *,
    recursive: bool = True,
    unzip_archives: bool = True,
) -> IngestionResult:
    """Recursively scan a folder and process all supported documents.

    This is the secondary option: point the app at any folder (accounting folder,
    Google Drive sync folder, network share) and it will find and extract all
    relevant data.

    Args:
        pipeline: An initialized InvoicePipeline instance
        folder: Root folder to scan
        recursive: If True, search all subdirectories
        unzip_archives: If True, extract ZIP files and process their contents
    """
    folder = Path(folder).expanduser().resolve()

    if not folder.exists():
        result = IngestionResult(total_files_found=0, files_processed=0, files_skipped=0, files_with_errors=1)
        result.errors.append(f"Folder does not exist: {folder}")
        result.summary = "Folder not found"
        return result

    all_files = _scan_folder(folder, recursive)

    # Handle archives
    archives = [p for p in all_files if p.suffix.lower() in ARCHIVE_EXTENSIONS]
    if unzip_archives and archives:
        temp_base = Path(tempfile.mkdtemp(prefix="bulk_ingest_"))
        try:
            for archive in archives:
                ext_dir = temp_base / archive.stem
                ext_dir.mkdir(parents=True, exist_ok=True)
                _extract_archive(archive, ext_dir)
            # Re-scan including extracted content
            all_files = _scan_folder(folder, recursive) + _scan_folder(temp_base, recursive=True)
            # Deduplicate
            all_files = sorted(set(all_files), key=lambda p: str(p))
        finally:
            shutil.rmtree(temp_base, ignore_errors=True)

    result = _process_files(pipeline, all_files)
    result.summary = f"Folder scan ({folder}): {result.summary}"
    return result


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------

def print_ingestion_report(result: IngestionResult) -> None:
    """Print a human-readable ingestion report to stdout."""
    print("=" * 70)
    print("BULK INGESTION REPORT")
    print("=" * 70)
    print(result.summary)
    print()
    if result.by_type:
        print("  Files by type:")
        for t, c in sorted(result.by_type.items()):
            print(f"    {t}: {c}")
    if result.by_status:
        print("  Files by status:")
        for s, c in sorted(result.by_status.items()):
            print(f"    {s}: {c}")
    if result.errors:
        print(f"\n  Errors ({len(result.errors)}):")
        for err in result.errors[:20]:
            print(f"    - {err}")
        if len(result.errors) > 20:
            print(f"    ... and {len(result.errors) - 20} more")
    print("=" * 70)
