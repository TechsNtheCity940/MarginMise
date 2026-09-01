#!/usr/bin/env python3
"""Annual inventory, monthly close, usage estimation, and order planning.

This module works beside invoice_pipeline.py. It deliberately keeps physical
counts, usage estimates, order suggestions, and manager approvals separate from
invoice extraction so each restaurant workspace can evolve without corrupting
historical invoice records.
"""
from __future__ import annotations

import calendar
import csv
import json
import math
import sqlite3
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable

MONEY = Decimal("0.01")
QTY = Decimal("0.0001")


def preferred_sales_rows(
    conn: sqlite3.Connection,
    start: str | date | None = None,
    end: str | date | None = None,
) -> list[sqlite3.Row]:
    """Return ledger sales without double-counting POS materializations.

    A POS import is retained in ``sales`` as a normalized ``POS:...`` summary
    in addition to its item-level ``pos_sales_lines``.  Restaurants may also
    import a regular sales summary for the same dates.  When those periods
    overlap, prefer the manager-provided summary row; otherwise keep the POS
    summary so POS-only restaurants still report sales.
    """
    clauses = ["1=1"]
    params: list[str] = []
    if start is not None and end is not None:
        start_text = start.isoformat() if isinstance(start, date) else str(start)
        end_text = end.isoformat() if isinstance(end, date) else str(end)
        clauses.append("s.period_start<=? AND s.period_end>=?")
        params.extend([end_text, start_text])
    clauses.append(
        """(
            COALESCE(s.source_file,'') NOT LIKE 'POS:%'
            OR NOT EXISTS (
                SELECT 1 FROM sales preferred
                WHERE COALESCE(preferred.source_file,'') NOT LIKE 'POS:%'
                  AND preferred.period_start<=s.period_end
                  AND preferred.period_end>=s.period_start
            )
        )"""
    )
    return conn.execute(
        f"SELECT s.* FROM sales s WHERE {' AND '.join(clauses)} "
        "ORDER BY s.period_start,s.period_end,s.sales_id",
        tuple(params),
    ).fetchall()

PLANNING_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS inventory_counts (
    count_id INTEGER PRIMARY KEY AUTOINCREMENT,
    count_date TEXT NOT NULL,
    count_month TEXT NOT NULL,
    item_id TEXT NOT NULL REFERENCES items(item_id),
    quantity_on_hand TEXT NOT NULL,
    count_unit TEXT,
    unit_cost TEXT,
    inventory_value TEXT,
    source_file TEXT,
    notes TEXT,
    finalized INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(count_date, item_id, source_file)
);
CREATE INDEX IF NOT EXISTS idx_inventory_counts_month ON inventory_counts(count_month);
CREATE INDEX IF NOT EXISTS idx_inventory_counts_item_date ON inventory_counts(item_id, count_date);

CREATE TABLE IF NOT EXISTS monthly_item_usage (
    month TEXT NOT NULL,
    item_id TEXT NOT NULL REFERENCES items(item_id),
    item_name TEXT NOT NULL,
    vendor_name TEXT,
    vendor_sku TEXT,
    count_unit TEXT,
    opening_quantity TEXT,
    purchased_quantity TEXT NOT NULL,
    ending_quantity TEXT NOT NULL,
    estimated_usage_quantity TEXT NOT NULL,
    average_daily_usage TEXT NOT NULL,
    average_weekly_usage TEXT NOT NULL,
    usage_per_1000_sales TEXT,
    inventory_unit_cost TEXT,
    estimated_usage_cost TEXT,
    confidence TEXT NOT NULL,
    notes TEXT,
    calculated_at TEXT NOT NULL,
    PRIMARY KEY(month, item_id)
);

CREATE TABLE IF NOT EXISTS monthly_closes (
    month TEXT PRIMARY KEY,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    net_sales TEXT NOT NULL,
    invoice_purchases TEXT NOT NULL,
    product_purchases TEXT NOT NULL,
    opening_inventory_value TEXT NOT NULL,
    ending_inventory_value TEXT NOT NULL,
    estimated_cogs TEXT NOT NULL,
    estimated_product_margin TEXT NOT NULL,
    estimated_product_margin_percent TEXT NOT NULL,
    imported_operating_costs TEXT NOT NULL,
    estimated_contribution TEXT NOT NULL,
    count_status TEXT NOT NULL,
    notes TEXT,
    closed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_batches (
    batch_id TEXT PRIMARY KEY,
    as_of_date TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Draft',
    history_months INTEGER NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS order_predictions (
    prediction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES order_batches(batch_id) ON DELETE CASCADE,
    item_id TEXT NOT NULL REFERENCES items(item_id),
    vendor_name TEXT,
    vendor_sku TEXT,
    item_name TEXT NOT NULL,
    purchase_unit TEXT,
    count_unit TEXT,
    units_per_purchase_unit TEXT NOT NULL,
    estimated_on_hand TEXT,
    inventory_confidence TEXT,
    average_daily_usage TEXT NOT NULL,
    average_weekly_usage TEXT NOT NULL,
    lead_time_days TEXT NOT NULL,
    order_cycle_days TEXT NOT NULL,
    safety_stock_days TEXT NOT NULL,
    par_quantity_count_units TEXT NOT NULL,
    suggested_order_quantity TEXT NOT NULL,
    manager_order_quantity TEXT,
    order_multiple TEXT NOT NULL,
    current_price TEXT,
    estimated_order_cost TEXT,
    status TEXT NOT NULL DEFAULT 'Draft',
    notes TEXT,
    UNIQUE(batch_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_order_predictions_batch ON order_predictions(batch_id);
"""

ITEM_COLUMNS = {
    "count_unit": "TEXT",
    "units_per_purchase_unit": "TEXT NOT NULL DEFAULT '1.0000'",
    "lead_time_days": "TEXT NOT NULL DEFAULT '2.00'",
    "order_cycle_days": "TEXT NOT NULL DEFAULT '7.00'",
    "safety_stock_days": "TEXT NOT NULL DEFAULT '2.00'",
    "order_multiple": "TEXT NOT NULL DEFAULT '1.0000'",
    "minimum_order_qty": "TEXT NOT NULL DEFAULT '0.0000'",
    "par_override_count_units": "TEXT",
    "active": "INTEGER NOT NULL DEFAULT 1",
    "estimated_on_hand": "TEXT",
    "estimated_on_hand_as_of": "TEXT",
    "planning_confirmed": "INTEGER NOT NULL DEFAULT 0",
}


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def d(value: Any, default: str = "0") -> Decimal:
    if value in (None, ""):
        return Decimal(default)
    try:
        return Decimal(str(value).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid numeric value: {value!r}") from exc


def q(value: Any) -> Decimal:
    return d(value).quantize(QTY, rounding=ROUND_HALF_UP)


def m(value: Any) -> Decimal:
    return d(value).quantize(MONEY, rounding=ROUND_HALF_UP)


def parse_date(value: Any) -> date:
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y", "%m-%d-%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unsupported date: {value!r}")


def parse_month(value: Any) -> str:
    text = str(value or "").strip()
    try:
        datetime.strptime(text, "%Y-%m")
    except ValueError as exc:
        raise ValueError("Month must use YYYY-MM format") from exc
    return text


def month_bounds(month: str) -> tuple[date, date]:
    month = parse_month(month)
    year, mon = map(int, month.split("-"))
    return date(year, mon, 1), date(year, mon, calendar.monthrange(year, mon)[1])


def month_shift(month: str, amount: int) -> str:
    year, mon = map(int, parse_month(month).split("-"))
    index = year * 12 + (mon - 1) + amount
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def ceil_multiple(value: Decimal, multiple: Decimal) -> Decimal:
    if value <= 0:
        return Decimal("0").quantize(QTY)
    if multiple <= 0:
        multiple = Decimal("1")
    steps = (value / multiple).to_integral_value(rounding="ROUND_CEILING")
    return (steps * multiple).quantize(QTY)


def infer_count_conversion(description: str, purchase_unit: str = "each") -> tuple[str, Decimal]:
    """Infer a conservative count unit and units-per-purchase-unit from common pack text.

    Only clear patterns are accepted. Ambiguous descriptions remain 1:1 and stay
    visible in the Item Master for manager correction.
    """
    import re
    text = str(description or "").lower().replace("#", "")
    unit_alias = {
        "lb": "lb", "lbs": "lb", "ib": "lb", "pound": "lb", "pounds": "lb",
        "oz": "oz", "ounce": "oz", "ounces": "oz",
        "gal": "gallon", "gallon": "gallon", "gallons": "gallon",
        "qt": "quart", "quart": "quart", "quarts": "quart",
        "ct": "each", "count": "each", "ea": "each", "each": "each",
    }
    # 6 x 5 lb, 4/10 lb, 8 x 12 count.
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:x|/)\s*(\d+(?:\.\d+)?)\s*(lb|lbs|ib|pounds?|oz|ounces?|gal|gallons?|qt|quarts?|ct|count|each|ea)\b", text)
    if match:
        count = Decimal(match.group(1)) * Decimal(match.group(2))
        return unit_alias[match.group(3)], count.quantize(QTY)
    # bundle of 100, box of 50, pack of 25.
    match = re.search(r"(?:bundle|box|pack|case)\s+of\s+(\d+(?:\.\d+)?)\b", text)
    if match:
        return "each", Decimal(match.group(1)).quantize(QTY)
    # 40 lb case, 25 count bundle, 5 gallon pail.
    match = re.search(r"(\d+(?:\.\d+)?)\s*(lb|lbs|ib|pounds?|oz|ounces?|gal|gallons?|qt|quarts?|ct|count|each|ea)\b", text)
    if match:
        return unit_alias[match.group(2)], Decimal(match.group(1)).quantize(QTY)
    purchase = str(purchase_unit or "each").strip().lower()
    return ("each" if purchase in {"case", "bundle", "box", "bag", "pail"} else purchase or "each", Decimal("1.0000"))


def normalize(value: Any) -> str:
    import re, unicodedata
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9]+", " ", text).strip().upper())


@dataclass
class CountImportResult:
    imported: int = 0
    skipped: int = 0
    errors: list[str] | None = None

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


class InventoryPlanningService:
    def __init__(self, workspace: Any):
        self.workspace = workspace
        self.ensure_schema()

    def ensure_schema(self) -> None:
        self.workspace.folders.setdefault("inventory_counts", self.workspace.root / "Inventory Counts")
        self.workspace.folders.setdefault("orders", self.workspace.root / "Order Sheets")
        for path in self.workspace.folders.values():
            Path(path).mkdir(parents=True, exist_ok=True)
        with self.workspace.connect() as conn:
            conn.executescript(PLANNING_SCHEMA_SQL)
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(items)").fetchall()}
            for name, definition in ITEM_COLUMNS.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE items ADD COLUMN {name} {definition}")
            conn.execute("UPDATE items SET count_unit=COALESCE(NULLIF(count_unit,''),unit,'each')")
            self._repair_pack_conversions(conn)

    def _repair_pack_conversions(self, conn: sqlite3.Connection) -> None:
        """Repair legacy 1:1 package conversions when the purchase unit is explicit."""
        rows = conn.execute(
            "SELECT item_id,unit,count_unit,units_per_purchase_unit FROM items"
        ).fetchall()
        for row in rows:
            current = d(row["units_per_purchase_unit"], "1")
            if current != Decimal("1"):
                continue
            description = str(row["unit"] or row["count_unit"] or "")
            inferred_unit, inferred_units = infer_count_conversion(description, description)
            if inferred_units <= Decimal("1"):
                continue
            conn.execute(
                "UPDATE items SET count_unit=?, units_per_purchase_unit=? WHERE item_id=?",
                (inferred_unit, f"{inferred_units:.4f}", row["item_id"]),
            )

    def settings(self) -> dict[str, Any]:
        data = self.workspace.load_settings()
        defaults = {
            "forecast_history_months": 3,
            "default_lead_time_days": 2.0,
            "default_order_cycle_days": 7.0,
            "default_safety_stock_days": 2.0,
            "default_order_multiple": 1.0,
            "sales_adjust_order_predictions": True,
            "include_zero_order_items": True,
            "auto_generate_weekly_order_draft": True,
        }
        defaults.update(data)
        return defaults

    # ---------- item configuration ----------
    def update_item_planning(self, item_id: str, **values: Any) -> None:
        allowed = {
            "count_unit", "units_per_purchase_unit", "lead_time_days", "order_cycle_days",
            "safety_stock_days", "order_multiple", "minimum_order_qty",
            "par_override_count_units", "active", "planning_confirmed",
        }
        updates, params = [], []
        for key, value in values.items():
            if key not in allowed:
                continue
            if key in {"active", "planning_confirmed"}:
                value = int(bool(value))
            elif key != "count_unit" and value not in (None, ""):
                value = f"{q(value):.4f}"
            elif key == "par_override_count_units" and value in (None, ""):
                value = None
            updates.append(f"{key}=?")
            params.append(value)
        if not updates:
            return
        params.append(item_id)
        with self.workspace.connect() as conn:
            conn.execute(f"UPDATE items SET {', '.join(updates)} WHERE item_id=?", params)

    # ---------- count sheets and count import ----------
    def export_count_sheet_csv(self, month: str, destination: Path | None = None) -> Path:
        month = parse_month(month)
        _start, end = month_bounds(month)
        destination = destination or (self.workspace.folders["inventory_counts"] / f"Inventory_Count_{month}.csv")
        estimates = {row["item_id"]: row for row in self.estimate_inventory(end)}
        with self.workspace.connect() as conn:
            rows = conn.execute(
                """SELECT item_id,vendor_name,vendor_sku,item_name,category,unit,count_unit,
                          units_per_purchase_unit,current_price,active
                   FROM items WHERE active=1 ORDER BY category,vendor_name,item_name"""
            ).fetchall()
        headers = [
            "Count Date", "Month", "Item ID", "Vendor", "Vendor SKU", "Item Name", "Category",
            "Purchase Unit", "Count Unit", "Units Per Purchase Unit", "Current Purchase Price",
            "System Estimated On Hand", "Counted Quantity", "Notes",
        ]
        with destination.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers)
            writer.writeheader()
            for row in rows:
                estimate = estimates.get(row["item_id"], {})
                writer.writerow({
                    "Count Date": end.isoformat(), "Month": month, "Item ID": row["item_id"],
                    "Vendor": row["vendor_name"], "Vendor SKU": row["vendor_sku"] or "",
                    "Item Name": row["item_name"], "Category": row["category"],
                    "Purchase Unit": row["unit"] or "each", "Count Unit": row["count_unit"] or row["unit"] or "each",
                    "Units Per Purchase Unit": row["units_per_purchase_unit"] or "1",
                    "Current Purchase Price": row["current_price"] or "",
                    "System Estimated On Hand": estimate.get("estimated_on_hand", ""),
                    "Counted Quantity": "", "Notes": "",
                })
        return destination

    def _match_count_item(self, conn: sqlite3.Connection, row: dict[str, Any]) -> sqlite3.Row | None:
        item_id = str(row.get("Item ID") or row.get("item_id") or "").strip()
        if item_id:
            found = conn.execute("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone()
            if found:
                return found
        sku = str(row.get("Vendor SKU") or row.get("vendor_sku") or row.get("SKU") or "").strip()
        vendor = normalize(row.get("Vendor") or row.get("vendor"))
        if sku:
            if vendor:
                found = conn.execute(
                    "SELECT * FROM items WHERE vendor_key=? AND vendor_sku=?", (vendor, sku)
                ).fetchone()
            else:
                found = conn.execute("SELECT * FROM items WHERE vendor_sku=?", (sku,)).fetchone()
            if found:
                return found
        name = normalize(row.get("Item Name") or row.get("item_name") or row.get("description"))
        if name:
            if vendor:
                found = conn.execute(
                    "SELECT * FROM items WHERE vendor_key=? AND normalized_description=?", (vendor, name)
                ).fetchone()
            else:
                found = conn.execute("SELECT * FROM items WHERE normalized_description=?", (name,)).fetchone()
            return found
        return None

    def import_count_csv(self, path: Path, default_count_date: date | None = None, finalized: bool = True) -> CountImportResult:
        result = CountImportResult()
        path = path.expanduser().resolve()
        with path.open("r", encoding="utf-8-sig", newline="") as handle, self.workspace.connect() as conn:
            reader = csv.DictReader(handle)
            for index, row in enumerate(reader, 2):
                quantity_raw = (
                    row.get("Counted Quantity") or row.get("quantity_on_hand") or row.get("Quantity On Hand")
                    or row.get("Ending Quantity") or row.get("count")
                )
                if quantity_raw in (None, ""):
                    result.skipped += 1
                    continue
                try:
                    item = self._match_count_item(conn, row)
                    if not item:
                        raise ValueError("item could not be matched")
                    count_date = parse_date(row.get("Count Date") or row.get("count_date") or default_count_date)
                    month = count_date.strftime("%Y-%m")
                    quantity = q(quantity_raw)
                    if quantity < 0:
                        raise ValueError("counted quantity cannot be negative")
                    units_per = q(item["units_per_purchase_unit"] or 1)
                    purchase_price = m(item["current_price"] or 0)
                    inventory_unit_cost = (purchase_price / units_per).quantize(MONEY) if units_per else purchase_price
                    value = (quantity * inventory_unit_cost).quantize(MONEY)
                    conn.execute(
                        """INSERT INTO inventory_counts(
                               count_date,count_month,item_id,quantity_on_hand,count_unit,unit_cost,inventory_value,
                               source_file,notes,finalized,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(count_date,item_id,source_file) DO UPDATE SET
                               quantity_on_hand=excluded.quantity_on_hand,count_unit=excluded.count_unit,
                               unit_cost=excluded.unit_cost,inventory_value=excluded.inventory_value,
                               notes=excluded.notes,finalized=excluded.finalized,created_at=excluded.created_at""",
                        (
                            count_date.isoformat(), month, item["item_id"], f"{quantity:.4f}",
                            str(row.get("Count Unit") or item["count_unit"] or item["unit"] or "each"),
                            f"{inventory_unit_cost:.2f}", f"{value:.2f}", path.name,
                            str(row.get("Notes") or row.get("notes") or ""), int(finalized), now_iso(),
                        ),
                    )
                    result.imported += 1
                except Exception as exc:
                    result.errors.append(f"Row {index}: {exc}")
        target = self.workspace.folders["inventory_counts"] / path.name
        if path != target:
            import shutil
            shutil.copy2(path, target)
        return result

    def list_counts(self, month: str | None = None) -> list[sqlite3.Row]:
        query = """SELECT c.*,i.item_name,i.vendor_name,i.vendor_sku,i.category,i.unit AS purchase_unit
                   FROM inventory_counts c JOIN items i ON i.item_id=c.item_id"""
        params: tuple[Any, ...] = ()
        if month:
            query += " WHERE c.count_month=?"
            params = (parse_month(month),)
        query += " ORDER BY c.count_date DESC,i.category,i.item_name"
        with self.workspace.connect() as conn:
            return conn.execute(query, params).fetchall()

    # ---------- sales selection and month close ----------
    def _best_sales_total(self, start: date, end: date) -> Decimal:
        with self.workspace.connect() as conn:
            pos_total = conn.execute(
                "SELECT COALESCE(SUM(CAST(net_sales AS REAL)),0) AS total FROM pos_sales_lines WHERE business_date>=? AND business_date<=?",
                (start.isoformat(), end.isoformat()),
            ).fetchone()["total"]
            if float(pos_total or 0) > 0:
                return m(pos_total)
            rows = conn.execute(
                """SELECT period_start,period_end,net_sales,source_file,sales_id FROM sales
                   WHERE period_start<=? AND period_end>=? ORDER BY source_file,sales_id""",
                (end.isoformat(), start.isoformat()),
            ).fetchall()
        if not rows:
            return Decimal("0.00")
        groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            groups[str(row["source_file"] or "manual")].append(row)
        scored: list[tuple[int, int, Decimal, str]] = []
        for source, group in groups.items():
            covered: set[date] = set()
            total = Decimal("0")
            used_rows = 0
            for row in group:
                a = max(start, parse_date(row["period_start"]))
                b = min(end, parse_date(row["period_end"]))
                if a > b:
                    continue
                # Rows that span beyond this month cannot be safely prorated; exact/daily/weekly rows are preferred.
                if parse_date(row["period_start"]) < start or parse_date(row["period_end"]) > end:
                    continue
                total += m(row["net_sales"])
                used_rows += 1
                cursor = a
                while cursor <= b:
                    covered.add(cursor)
                    cursor += timedelta(days=1)
            scored.append((len(covered), used_rows, total, source))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return scored[0][2].quantize(MONEY) if scored else Decimal("0.00")

    def _theoretical_month_cogs(self, conn: sqlite3.Connection, start: date, end: date) -> Decimal:
        """Calculate ideal/theoretical COGS from recipe portions and POS sales."""
        row = conn.execute(
            """SELECT COALESCE(SUM(
                       CAST(s.quantity AS REAL) *
                       CAST(r.quantity_count_units AS REAL) /
                       CASE WHEN CAST(r.yield_percent AS REAL)>0 THEN CAST(r.yield_percent AS REAL)/100.0 ELSE 1 END *
                       CAST(COALESCE(i.current_price,'0') AS REAL) /
                       CASE WHEN CAST(COALESCE(i.units_per_purchase_unit,'1') AS REAL)>0
                            THEN CAST(COALESCE(i.units_per_purchase_unit,'1') AS REAL) ELSE 1 END
                   ),0) AS total
                FROM pos_sales_lines s
                JOIN recipe_ingredients r ON r.menu_item_id=s.menu_item_id
                JOIN items i ON i.item_id=r.item_id
                WHERE s.business_date>=? AND s.business_date<=?""",
            (start.isoformat(), end.isoformat()),
        ).fetchone()
        return m(row["total"] or 0)

    def _month_purchases(self, conn: sqlite3.Connection, start: date, end: date) -> tuple[Decimal, Decimal]:
        invoice = conn.execute(
            """SELECT COALESCE(SUM(CAST(total AS REAL)),0) AS total FROM invoices
               WHERE status='Approved' AND invoice_date>=? AND invoice_date<=?""",
            (start.isoformat(), end.isoformat()),
        ).fetchone()
        product = conn.execute(
            """SELECT COALESCE(SUM(CAST(l.line_total AS REAL)),0) AS total
               FROM invoice_lines l JOIN invoices i ON i.invoice_id=l.invoice_id
               WHERE i.status='Approved' AND i.invoice_date>=? AND i.invoice_date<=?""",
            (start.isoformat(), end.isoformat()),
        ).fetchone()
        return m(invoice["total"] or 0), m(product["total"] or 0)

    def _count_for_item(self, conn: sqlite3.Connection, item_id: str, *, before: date | None = None,
                        start: date | None = None, end: date | None = None) -> sqlite3.Row | None:
        if before is not None:
            return conn.execute(
                """SELECT * FROM inventory_counts WHERE item_id=? AND finalized=1 AND count_date<?
                   ORDER BY count_date DESC,count_id DESC LIMIT 1""",
                (item_id, before.isoformat()),
            ).fetchone()
        return conn.execute(
            """SELECT * FROM inventory_counts WHERE item_id=? AND finalized=1
               AND count_date>=? AND count_date<=? ORDER BY count_date DESC,count_id DESC LIMIT 1""",
            (item_id, start.isoformat(), end.isoformat()),
        ).fetchone()

    def _opening_count_for_item(
        self,
        conn: sqlite3.Connection,
        item_id: str,
        start: date,
    ) -> sqlite3.Row | None:
        """Return the count that represents opening inventory for a month.

        Restaurants commonly import a beginning count dated on the first day
        of the month, so that exact count is preferred. Otherwise, the latest
        finalized count before the month is used. Counts later in the month are
        never treated as opening inventory.
        """
        on_start = conn.execute(
            """SELECT * FROM inventory_counts
               WHERE item_id=? AND finalized=1 AND count_date=?
               ORDER BY count_id DESC LIMIT 1""",
            (item_id, start.isoformat()),
        ).fetchone()
        return on_start or self._count_for_item(conn, item_id, before=start)

    def _month_preview_with_conn(self, conn: sqlite3.Connection, month: str) -> dict[str, Any]:
        """Calculate an open-month inventory preview without writing close data."""
        month = parse_month(month)
        start, end = month_bounds(month)
        days = Decimal((end - start).days + 1)
        net_sales = self._best_sales_total(start, end)
        invoice_purchases, product_purchases = self._month_purchases(conn, start, end)
        operating = conn.execute(
            "SELECT COALESCE(SUM(CAST(amount AS REAL)),0) AS total "
            "FROM operating_costs WHERE cost_date>=? AND cost_date<=?",
            (start.isoformat(), end.isoformat()),
        ).fetchone()
        operating_costs = m(operating["total"] or 0)
        items = conn.execute("SELECT * FROM items WHERE active=1 ORDER BY item_name").fetchall()

        usage_rows: list[dict[str, Any]] = []
        opening_value = Decimal("0")
        ending_value = Decimal("0")
        missing_open = 0
        missing_end = 0
        for item in items:
            opening = self._opening_count_for_item(conn, item["item_id"], start)
            ending = self._count_for_item(conn, item["item_id"], start=start, end=end)
            if not opening:
                missing_open += 1
            if not ending:
                missing_end += 1
            if not opening and not ending:
                continue

            units_per = q(item["units_per_purchase_unit"] or 1)
            purchased = conn.execute(
                """SELECT COALESCE(SUM(CAST(l.quantity AS REAL)),0) AS quantity
                   FROM invoice_lines l JOIN invoices i ON i.invoice_id=l.invoice_id
                   WHERE l.item_id=? AND i.status='Approved'
                   AND i.invoice_date>=? AND i.invoice_date<=?""",
                (item["item_id"], start.isoformat(), end.isoformat()),
            ).fetchone()
            purchased_count_units = (d(purchased["quantity"] or 0) * units_per).quantize(QTY)
            transfer_row = conn.execute(
                """SELECT COALESCE(SUM(CAST(quantity_delta AS REAL)),0) AS qty
                   FROM inventory_adjustments
                   WHERE item_id=? AND adjustment_date>=? AND adjustment_date<=?""",
                (item["item_id"], start.isoformat(), end.isoformat()),
            ).fetchone()
            transfer_adjustment = q(transfer_row["qty"] or 0)
            purchase_price = m(item["current_price"] or 0)
            inventory_unit_cost = (purchase_price / units_per).quantize(MONEY) if units_per else purchase_price

            opening_qty = q(opening["quantity_on_hand"]) if opening else None
            ending_qty = q(ending["quantity_on_hand"]) if ending else None
            if opening:
                opening_item_value = m(
                    opening["inventory_value"]
                    if opening["inventory_value"] not in (None, "")
                    else q(opening["quantity_on_hand"]) * inventory_unit_cost
                )
                opening_value += opening_item_value
            if ending:
                ending_item_value = m(
                    ending["inventory_value"]
                    if ending["inventory_value"] not in (None, "")
                    else q(ending["quantity_on_hand"]) * inventory_unit_cost
                )
                ending_value += ending_item_value

            usage = None
            avg_daily = None
            avg_weekly = None
            usage_per_1000 = None
            usage_cost = None
            confidence = "Open preview"
            notes = "Read-only preview; close the month to finalize usage."
            if opening_qty is not None and ending_qty is not None:
                usage = (opening_qty + purchased_count_units + transfer_adjustment - ending_qty).quantize(QTY)
                if usage < 0:
                    notes += " Negative depletion was clamped to zero; review units or counts."
                    usage = Decimal("0").quantize(QTY)
                    confidence = "Review"
                else:
                    confidence = "Preview - complete counts"
                avg_daily = (usage / days).quantize(QTY)
                avg_weekly = (avg_daily * Decimal("7")).quantize(QTY)
                usage_per_1000 = (
                    (usage / net_sales) * Decimal("1000")
                ).quantize(QTY) if net_sales > 0 else None
                usage_cost = (usage * inventory_unit_cost).quantize(MONEY)
            elif not opening:
                confidence = "Preview - opening count missing"
            else:
                confidence = "Preview - ending count missing"

            usage_rows.append({
                "month": month,
                "item_id": item["item_id"],
                "item_name": item["item_name"],
                "vendor_name": item["vendor_name"],
                "vendor_sku": item["vendor_sku"] or "",
                "count_unit": item["count_unit"] or item["unit"] or "each",
                "opening_quantity": opening_qty,
                "purchased_quantity": purchased_count_units,
                "transfer_adjustment": transfer_adjustment,
                "ending_quantity": ending_qty,
                "estimated_usage_quantity": usage,
                "average_daily_usage": avg_daily,
                "average_weekly_usage": avg_weekly,
                "usage_per_1000_sales": usage_per_1000,
                "inventory_unit_cost": inventory_unit_cost,
                "estimated_usage_cost": usage_cost,
                "confidence": confidence,
                "notes": notes,
            })

        complete_counts = not missing_open and not missing_end and bool(items)
        theoretical_cogs = self._theoretical_month_cogs(conn, start, end)
        if complete_counts:
            estimated_cogs = (opening_value + product_purchases - ending_value).quantize(MONEY)
            cogs_source = "Physical inventory COGS"
        elif theoretical_cogs > 0:
            estimated_cogs = theoretical_cogs
            cogs_source = "Recipe-based theoretical COGS"
        else:
            estimated_cogs = product_purchases.quantize(MONEY)
            cogs_source = "Purchase-spend fallback estimate"
        waste_cost = m(conn.execute(
            "SELECT COALESCE(SUM(CAST(estimated_cost AS REAL)),0) FROM waste_events WHERE event_date>=? AND event_date<=?",
            (start.isoformat(), end.isoformat()),
        ).fetchone()[0])
        labor_row = conn.execute(
            """SELECT COALESCE(SUM(CAST(amount AS REAL)),0) AS total FROM operating_costs
               WHERE cost_date>=? AND cost_date<=? AND (LOWER(category) LIKE '%labor%' OR LOWER(category) LIKE '%payroll%' OR LOWER(category) LIKE '%wage%')""",
            (start.isoformat(), end.isoformat()),
        ).fetchone()
        actual_labor = m(labor_row["total"] or 0)
        labor_percent = d(self.workspace.load_settings().get("estimated_labor_percent", 30), "30")
        labor_cost = actual_labor if actual_labor > 0 else (net_sales * labor_percent / Decimal("100")).quantize(MONEY)
        total_estimated_costs = (estimated_cogs + waste_cost + operating_costs + labor_cost).quantize(MONEY)
        margin = (net_sales - estimated_cogs).quantize(MONEY)
        margin_pct = ((margin / net_sales) * Decimal("100")).quantize(MONEY) if net_sales else Decimal("0")
        contribution = (net_sales - total_estimated_costs).quantize(MONEY)
        if not items:
            count_status = "Open - no active inventory items"
        elif missing_open or missing_end:
            count_status = (
                f"Open preview - {missing_open} opening and {missing_end} ending count(s) missing"
            )
        else:
            count_status = "Open - count preview (not closed)"
        return {
            "month": month,
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "net_sales": f"{net_sales:.2f}",
            "invoice_purchases": f"{invoice_purchases:.2f}",
            "product_purchases": f"{product_purchases:.2f}",
            "opening_inventory_value": f"{opening_value:.2f}",
            "ending_inventory_value": f"{ending_value:.2f}",
            "estimated_cogs": f"{estimated_cogs:.2f}",
            "estimated_cogs_source": cogs_source,
            "waste_cost": f"{waste_cost:.2f}",
            "estimated_labor_cost": f"{labor_cost:.2f}",
            "estimated_labor_percent": f"{labor_percent:.2f}",
            "estimated_total_costs": f"{total_estimated_costs:.2f}",
            "estimated_product_margin": f"{margin:.2f}",
            "estimated_product_margin_percent": f"{margin_pct:.2f}",
            "imported_operating_costs": f"{operating_costs:.2f}",
            "estimated_contribution": f"{contribution:.2f}",
            "count_status": count_status,
            "status": count_status,
            "notes": (
                "Read-only preview from finalized physical counts and approved product invoice lines. "
                "Values remain provisional until Close Month is completed."
            ),
            "rows": usage_rows,
            "missing_opening_counts": missing_open,
            "missing_ending_counts": missing_end,
            "preview": True,
        }

    def preview_month(self, month: str) -> dict[str, Any]:
        """Return imported count values and provisional usage for an open month."""
        with self.workspace.connect() as conn:
            return self._month_preview_with_conn(conn, parse_month(month))

    def close_month(self, month: str) -> dict[str, Any]:
        month = parse_month(month)
        start, end = month_bounds(month)
        days = Decimal((end - start).days + 1)
        net_sales = self._best_sales_total(start, end)
        with self.workspace.connect() as conn:
            items = conn.execute("SELECT * FROM items WHERE active=1 ORDER BY item_name").fetchall()
            invoice_purchases, product_purchases = self._month_purchases(conn, start, end)
            operating = conn.execute(
                "SELECT COALESCE(SUM(CAST(amount AS REAL)),0) AS total FROM operating_costs WHERE cost_date>=? AND cost_date<=?",
                (start.isoformat(), end.isoformat()),
            ).fetchone()
            operating_costs = m(operating["total"] or 0)
            usage_rows: list[dict[str, Any]] = []
            opening_value = Decimal("0")
            ending_value = Decimal("0")
            missing_end = 0
            missing_open = 0
            for item in items:
                opening = self._opening_count_for_item(conn, item["item_id"], start)
                ending = self._count_for_item(conn, item["item_id"], start=start, end=end)
                if not ending:
                    missing_end += 1
                    continue
                units_per = q(item["units_per_purchase_unit"] or 1)
                purchased = conn.execute(
                    """SELECT COALESCE(SUM(CAST(l.quantity AS REAL)),0) AS quantity
                       FROM invoice_lines l JOIN invoices i ON i.invoice_id=l.invoice_id
                       WHERE l.item_id=? AND i.status='Approved' AND i.invoice_date>=? AND i.invoice_date<=?""",
                    (item["item_id"], start.isoformat(), end.isoformat()),
                ).fetchone()
                purchased_count_units = (d(purchased["quantity"] or 0) * units_per).quantize(QTY)
                transfer_row = conn.execute(
                    "SELECT COALESCE(SUM(CAST(quantity_delta AS REAL)),0) AS qty FROM inventory_adjustments WHERE item_id=? AND adjustment_date>=? AND adjustment_date<=?",
                    (item["item_id"], start.isoformat(), end.isoformat()),
                ).fetchone()
                transfer_adjustment = q(transfer_row["qty"] or 0)
                ending_qty = q(ending["quantity_on_hand"])
                if opening:
                    opening_qty = q(opening["quantity_on_hand"])
                    confidence = "High"
                    notes = "Usage includes sold product, spoilage, waste, theft, and count variance."
                else:
                    opening_qty = Decimal("0").quantize(QTY)
                    missing_open += 1
                    confidence = "Estimated - no opening count"
                    notes = "Opening count unavailable; usage assumes zero opening inventory and is a lower-bound estimate."
                usage = (opening_qty + purchased_count_units + transfer_adjustment - ending_qty).quantize(QTY)
                if usage < 0:
                    notes += " Negative depletion was clamped to zero; review units or counts."
                    usage = Decimal("0").quantize(QTY)
                    confidence = "Review"
                avg_daily = (usage / days).quantize(QTY)
                avg_weekly = (avg_daily * Decimal("7")).quantize(QTY)
                usage_per_1000 = ((usage / net_sales) * Decimal("1000")).quantize(QTY) if net_sales > 0 else None
                purchase_price = m(item["current_price"] or 0)
                inventory_unit_cost = (purchase_price / units_per).quantize(MONEY) if units_per else purchase_price
                opening_item_value = m(opening["inventory_value"] if opening else opening_qty * inventory_unit_cost)
                ending_item_value = m(ending["inventory_value"] if ending["inventory_value"] else ending_qty * inventory_unit_cost)
                opening_value += opening_item_value
                ending_value += ending_item_value
                usage_cost = (usage * inventory_unit_cost).quantize(MONEY)
                usage_rows.append({
                    "month": month, "item_id": item["item_id"], "item_name": item["item_name"],
                    "vendor_name": item["vendor_name"], "vendor_sku": item["vendor_sku"] or "",
                    "count_unit": item["count_unit"] or item["unit"] or "each",
                    "opening_quantity": opening_qty, "purchased_quantity": purchased_count_units,
                    "transfer_adjustment": transfer_adjustment,
                    "ending_quantity": ending_qty, "estimated_usage_quantity": usage,
                    "average_daily_usage": avg_daily, "average_weekly_usage": avg_weekly,
                    "usage_per_1000_sales": usage_per_1000, "inventory_unit_cost": inventory_unit_cost,
                    "estimated_usage_cost": usage_cost, "confidence": confidence,
                    "notes": notes + f" Net transfer adjustment: {transfer_adjustment:.4f} count units.",
                })
            if missing_end:
                raise ValueError(
                    f"Cannot close {month}: {missing_end} active item(s) have no finalized ending count. "
                    "Export the count sheet, enter counts, and import it first."
                )
            for row in usage_rows:
                conn.execute(
                    """INSERT INTO monthly_item_usage(
                           month,item_id,item_name,vendor_name,vendor_sku,count_unit,opening_quantity,
                           purchased_quantity,ending_quantity,estimated_usage_quantity,average_daily_usage,
                           average_weekly_usage,usage_per_1000_sales,inventory_unit_cost,estimated_usage_cost,
                           confidence,notes,calculated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(month,item_id) DO UPDATE SET
                           item_name=excluded.item_name,vendor_name=excluded.vendor_name,vendor_sku=excluded.vendor_sku,
                           count_unit=excluded.count_unit,opening_quantity=excluded.opening_quantity,
                           purchased_quantity=excluded.purchased_quantity,ending_quantity=excluded.ending_quantity,
                           estimated_usage_quantity=excluded.estimated_usage_quantity,
                           average_daily_usage=excluded.average_daily_usage,average_weekly_usage=excluded.average_weekly_usage,
                           usage_per_1000_sales=excluded.usage_per_1000_sales,inventory_unit_cost=excluded.inventory_unit_cost,
                           estimated_usage_cost=excluded.estimated_usage_cost,confidence=excluded.confidence,
                           notes=excluded.notes,calculated_at=excluded.calculated_at""",
                    (
                        row["month"], row["item_id"], row["item_name"], row["vendor_name"], row["vendor_sku"],
                        row["count_unit"], f"{row['opening_quantity']:.4f}", f"{row['purchased_quantity']:.4f}",
                        f"{row['ending_quantity']:.4f}", f"{row['estimated_usage_quantity']:.4f}",
                        f"{row['average_daily_usage']:.4f}", f"{row['average_weekly_usage']:.4f}",
                        None if row["usage_per_1000_sales"] is None else f"{row['usage_per_1000_sales']:.4f}",
                        f"{row['inventory_unit_cost']:.2f}", f"{row['estimated_usage_cost']:.2f}",
                        row["confidence"], row["notes"], now_iso(),
                    ),
                )
            theoretical_cogs = self._theoretical_month_cogs(conn, start, end)
            if missing_open == 0 and not missing_end:
                estimated_cogs = (opening_value + product_purchases - ending_value).quantize(MONEY)
                cogs_source = "Physical inventory COGS"
            elif theoretical_cogs > 0:
                estimated_cogs = theoretical_cogs
                cogs_source = "Recipe-based theoretical COGS"
            else:
                estimated_cogs = product_purchases.quantize(MONEY)
                cogs_source = "Purchase-spend fallback estimate"
            waste_cost = m(conn.execute(
                "SELECT COALESCE(SUM(CAST(estimated_cost AS REAL)),0) FROM waste_events WHERE event_date>=? AND event_date<=?",
                (start.isoformat(), end.isoformat()),
            ).fetchone()[0])
            actual_labor = m(conn.execute(
                """SELECT COALESCE(SUM(CAST(amount AS REAL)),0) FROM operating_costs
                   WHERE cost_date>=? AND cost_date<=? AND (LOWER(category) LIKE '%labor%' OR LOWER(category) LIKE '%payroll%' OR LOWER(category) LIKE '%wage%')""",
                (start.isoformat(), end.isoformat()),
            ).fetchone()[0])
            labor_percent = d(self.workspace.load_settings().get("estimated_labor_percent", 30), "30")
            labor_cost = actual_labor if actual_labor > 0 else (net_sales * labor_percent / Decimal("100")).quantize(MONEY)
            total_estimated_costs = (estimated_cogs + waste_cost + operating_costs + labor_cost).quantize(MONEY)
            margin = (net_sales - estimated_cogs).quantize(MONEY)
            margin_pct = ((margin / net_sales) * Decimal("100")).quantize(MONEY) if net_sales else Decimal("0")
            contribution = (net_sales - total_estimated_costs).quantize(MONEY)
            status = "Complete" if missing_open == 0 else f"Estimated ({missing_open} missing opening counts)"
            notes = (
                f"COGS source: {cogs_source}. Purchases are inventory movement, not automatically period COGS. "
                f"Estimated labor is {labor_percent:.1f}% of sales when actual labor is unavailable. "
                "Waste/spoilage and imported operating costs are included in estimated total costs."
            )
            conn.execute(
                """INSERT INTO monthly_closes(
                       month,period_start,period_end,net_sales,invoice_purchases,product_purchases,
                       opening_inventory_value,ending_inventory_value,estimated_cogs,estimated_product_margin,
                       estimated_product_margin_percent,imported_operating_costs,estimated_contribution,
                       count_status,notes,closed_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(month) DO UPDATE SET
                       period_start=excluded.period_start,period_end=excluded.period_end,net_sales=excluded.net_sales,
                       invoice_purchases=excluded.invoice_purchases,product_purchases=excluded.product_purchases,
                       opening_inventory_value=excluded.opening_inventory_value,ending_inventory_value=excluded.ending_inventory_value,
                       estimated_cogs=excluded.estimated_cogs,estimated_product_margin=excluded.estimated_product_margin,
                       estimated_product_margin_percent=excluded.estimated_product_margin_percent,
                       imported_operating_costs=excluded.imported_operating_costs,
                       estimated_contribution=excluded.estimated_contribution,count_status=excluded.count_status,
                       notes=excluded.notes,closed_at=excluded.closed_at""",
                (
                    month, start.isoformat(), end.isoformat(), f"{net_sales:.2f}", f"{invoice_purchases:.2f}",
                    f"{product_purchases:.2f}", f"{opening_value:.2f}", f"{ending_value:.2f}",
                    f"{estimated_cogs:.2f}", f"{margin:.2f}", f"{margin_pct:.2f}",
                    f"{operating_costs:.2f}", f"{contribution:.2f}", status, notes, now_iso(),
                ),
            )
        return self.month_summary(month)

    def month_summary(self, month: str) -> dict[str, Any]:
        month = parse_month(month)
        with self.workspace.connect() as conn:
            row = conn.execute("SELECT * FROM monthly_closes WHERE month=?", (month,)).fetchone()
            data = dict(row) if row else self._month_preview_with_conn(conn, month)
        data.setdefault("status", data.get("count_status"))
        return data

    def list_month_usage(self, month: str) -> list[sqlite3.Row | dict[str, Any]]:
        with self.workspace.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM monthly_item_usage WHERE month=? ORDER BY vendor_name,item_name",
                (parse_month(month),),
            ).fetchall()
            if rows:
                return rows
            return self._month_preview_with_conn(conn, parse_month(month))["rows"]

    def year_summary(self, year: int) -> list[dict[str, Any]]:
        rows = []
        with self.workspace.connect() as conn:
            closed = {row["month"]: dict(row) for row in conn.execute(
                "SELECT * FROM monthly_closes WHERE month LIKE ? ORDER BY month", (f"{int(year):04d}-%",)
            ).fetchall()}
            for mon in range(1, 13):
                month = f"{int(year):04d}-{mon:02d}"
                if month in closed:
                    rows.append(closed[month])
                    continue
                start, end = month_bounds(month)
                if start > date.today():
                    rows.append({
                        "month": month,
                        "period_start": start.isoformat(),
                        "period_end": end.isoformat(),
                        "net_sales": "0.00",
                        "invoice_purchases": "0.00",
                        "product_purchases": "0.00",
                        "opening_inventory_value": "0.00",
                        "ending_inventory_value": "0.00",
                        "estimated_cogs": "0.00",
                        "estimated_product_margin": "0.00",
                        "estimated_product_margin_percent": "0.00",
                        "imported_operating_costs": "0.00",
                        "estimated_contribution": "0.00",
                        "count_status": "Future period",
                        "status": "Future period",
                        "notes": "This month has not started.",
                        "preview": True,
                    })
                    continue
                preview = self._month_preview_with_conn(conn, month)
                preview.pop("rows", None)
                rows.append(preview)
        return rows

    def year_totals(self, year: int) -> dict[str, Any]:
        rows = self.year_summary(year)
        keys = [
            "net_sales", "invoice_purchases", "product_purchases", "opening_inventory_value",
            "ending_inventory_value", "estimated_cogs", "estimated_product_margin",
            "imported_operating_costs", "estimated_contribution",
        ]
        totals = {key: sum((m(row.get(key, 0)) for row in rows), Decimal("0")) for key in keys}
        opening_rows = [
            row for row in rows
            if m(row.get("opening_inventory_value", 0))
        ]
        ending_rows = [
            row for row in rows
            if m(row.get("ending_inventory_value", 0))
        ]
        closed_rows = [
            row for row in rows
            if str(row.get("count_status", "")).startswith(("Complete", "Estimated"))
        ]
        totals["opening_inventory_value"] = (
            m(opening_rows[0].get("opening_inventory_value", 0))
            if opening_rows else Decimal("0.00")
        )
        totals["ending_inventory_value"] = (
            m(ending_rows[-1].get("ending_inventory_value", 0))
            if ending_rows else Decimal("0.00")
        )
        totals["year"] = int(year)
        totals["closed_months"] = len(closed_rows)
        totals["product_margin_percent"] = (
            (totals["estimated_product_margin"] / totals["net_sales"] * Decimal("100")).quantize(MONEY)
            if totals["net_sales"] else Decimal("0")
        )
        return totals

    # ---------- inventory estimate and order prediction ----------
    def _usage_averages(self, conn: sqlite3.Connection, item_id: str, as_of: date, history_months: int) -> tuple[Decimal, Decimal]:
        rows = conn.execute(
            """SELECT average_daily_usage,usage_per_1000_sales FROM monthly_item_usage
               WHERE item_id=? AND month<? ORDER BY month DESC LIMIT ?""",
            (item_id, as_of.strftime("%Y-%m"), int(history_months)),
        ).fetchall()
        if not rows:
            # Fallback to recent purchasing velocity.
            since = as_of - timedelta(days=max(28, history_months * 31))
            purchased = conn.execute(
                """SELECT COALESCE(SUM(CAST(l.quantity AS REAL) * CAST(COALESCE(it.units_per_purchase_unit,'1') AS REAL)),0) AS qty
                   FROM invoice_lines l JOIN invoices i ON i.invoice_id=l.invoice_id
                   JOIN items it ON it.item_id=l.item_id
                   WHERE l.item_id=? AND i.status='Approved' AND i.invoice_date>=? AND i.invoice_date<=?""",
                (item_id, since.isoformat(), as_of.isoformat()),
            ).fetchone()
            days = Decimal(max(1, (as_of - since).days + 1))
            return (d(purchased["qty"] or 0) / days).quantize(QTY), Decimal("0")
        avg_daily = (sum((d(row["average_daily_usage"]) for row in rows), Decimal("0")) / Decimal(len(rows))).quantize(QTY)
        ratios = [d(row["usage_per_1000_sales"]) for row in rows if row["usage_per_1000_sales"] not in (None, "")]
        ratio = (sum(ratios, Decimal("0")) / Decimal(len(ratios))).quantize(QTY) if ratios else Decimal("0")
        return avg_daily, ratio

    def _sales_daily_rate(self, as_of: date, history_months: int) -> Decimal:
        rates = []
        for offset in range(1, history_months + 1):
            month = month_shift(as_of.strftime("%Y-%m"), -offset)
            start, end = month_bounds(month)
            total = self._best_sales_total(start, end)
            if total > 0:
                rates.append(total / Decimal((end - start).days + 1))
        return (sum(rates, Decimal("0")) / Decimal(len(rates))).quantize(MONEY) if rates else Decimal("0")

    def estimate_inventory(self, as_of_date: date | None = None) -> list[dict[str, Any]]:
        as_of = as_of_date or date.today()
        settings = self.settings()
        history_months = int(settings.get("forecast_history_months", 3))
        output: list[dict[str, Any]] = []
        with self.workspace.connect() as conn:
            items = conn.execute("SELECT * FROM items WHERE active=1 ORDER BY category,vendor_name,item_name").fetchall()
            for item in items:
                count = conn.execute(
                    """SELECT * FROM inventory_counts WHERE item_id=? AND finalized=1 AND count_date<=?
                       ORDER BY count_date DESC,count_id DESC LIMIT 1""",
                    (item["item_id"], as_of.isoformat()),
                ).fetchone()
                avg_daily, ratio = self._usage_averages(conn, item["item_id"], as_of, history_months)
                units_per = q(item["units_per_purchase_unit"] or 1)
                if count:
                    count_date = parse_date(count["count_date"])
                    base_qty = q(count["quantity_on_hand"])
                    purchases = conn.execute(
                        """SELECT COALESCE(SUM(CAST(l.quantity AS REAL)),0) AS qty
                           FROM invoice_lines l JOIN invoices i ON i.invoice_id=l.invoice_id
                           WHERE l.item_id=? AND i.status='Approved' AND i.invoice_date>? AND i.invoice_date<=?""",
                        (item["item_id"], count_date.isoformat(), as_of.isoformat()),
                    ).fetchone()
                    purchased_count_units = (d(purchases["qty"] or 0) * units_per).quantize(QTY)
                    adjustment = conn.execute(
                        "SELECT COALESCE(SUM(CAST(quantity_delta AS REAL)),0) AS qty FROM inventory_adjustments WHERE item_id=? AND adjustment_date>? AND adjustment_date<=?",
                        (item["item_id"], count_date.isoformat(), as_of.isoformat()),
                    ).fetchone()
                    transfer_adjustment = q(adjustment["qty"] or 0)
                    elapsed = Decimal(max(0, (as_of - count_date).days))
                    estimated = (base_qty + purchased_count_units + transfer_adjustment - avg_daily * elapsed).quantize(QTY)
                    confidence = "High" if elapsed <= 14 else "Medium" if elapsed <= 35 else "Low"
                    source_date = count_date.isoformat()
                else:
                    lookback = as_of - timedelta(days=30)
                    purchases = conn.execute(
                        """SELECT COALESCE(SUM(CAST(l.quantity AS REAL)),0) AS qty
                           FROM invoice_lines l JOIN invoices i ON i.invoice_id=l.invoice_id
                           WHERE l.item_id=? AND i.status='Approved' AND i.invoice_date>=? AND i.invoice_date<=?""",
                        (item["item_id"], lookback.isoformat(), as_of.isoformat()),
                    ).fetchone()
                    adjustment = conn.execute(
                        "SELECT COALESCE(SUM(CAST(quantity_delta AS REAL)),0) AS qty FROM inventory_adjustments WHERE item_id=? AND adjustment_date>=? AND adjustment_date<=?",
                        (item["item_id"], lookback.isoformat(), as_of.isoformat()),
                    ).fetchone()
                    transfer_adjustment = q(adjustment["qty"] or 0)
                    estimated = (d(purchases["qty"] or 0) * units_per + transfer_adjustment - avg_daily * Decimal("30")).quantize(QTY)
                    confidence = "Low - no physical count"
                    source_date = ""
                estimated = max(Decimal("0"), estimated).quantize(QTY)
                purchase_price = m(item["current_price"] or 0)
                inventory_unit_cost = (purchase_price / units_per).quantize(MONEY) if units_per else purchase_price
                inventory_value = (estimated * inventory_unit_cost).quantize(MONEY)
                conn.execute(
                    "UPDATE items SET estimated_on_hand=?,estimated_on_hand_as_of=? WHERE item_id=?",
                    (f"{estimated:.4f}", as_of.isoformat(), item["item_id"]),
                )
                output.append({
                    "item_id": item["item_id"], "vendor_name": item["vendor_name"],
                    "vendor_sku": item["vendor_sku"] or "", "item_name": item["item_name"],
                    "category": item["category"], "purchase_unit": item["unit"] or "each",
                    "count_unit": item["count_unit"] or item["unit"] or "each",
                    "units_per_purchase_unit": units_per, "current_price": purchase_price,
                    "last_count_date": source_date, "estimated_on_hand": estimated,
                    "average_daily_usage": avg_daily, "average_weekly_usage": (avg_daily * Decimal("7")).quantize(QTY),
                    "usage_per_1000_sales": ratio, "inventory_unit_cost": inventory_unit_cost,
                    "estimated_inventory_value": inventory_value, "confidence": confidence,
                })
        return output

    def generate_order_predictions(self, as_of_date: date | None = None, history_months: int | None = None) -> dict[str, Any]:
        as_of = as_of_date or date.today()
        settings = self.settings()
        history = int(history_months or settings.get("forecast_history_months", 3))
        sales_daily = self._sales_daily_rate(as_of, history)
        estimates = {row["item_id"]: row for row in self.estimate_inventory(as_of)}
        batch_id = f"ORD-{as_of.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8].upper()}"
        include_zero = bool(settings.get("include_zero_order_items", True))
        rows: list[dict[str, Any]] = []
        with self.workspace.connect() as conn:
            conn.execute(
                "INSERT INTO order_batches(batch_id,as_of_date,created_at,status,history_months,notes) VALUES(?,?,?,'Draft',?,?)",
                (batch_id, as_of.isoformat(), now_iso(), history, "Manager review is required before ordering."),
            )
            items = conn.execute("SELECT * FROM items WHERE active=1 ORDER BY vendor_name,item_name").fetchall()
            for item in items:
                estimate = estimates[item["item_id"]]
                avg_daily = d(estimate["average_daily_usage"])
                avg_weekly = d(estimate["average_weekly_usage"])
                ratio = d(estimate["usage_per_1000_sales"])
                lead = d(item["lead_time_days"] or settings.get("default_lead_time_days", 2))
                cycle = d(item["order_cycle_days"] or settings.get("default_order_cycle_days", 7))
                safety = d(item["safety_stock_days"] or settings.get("default_safety_stock_days", 2))
                demand_days = lead + cycle
                usage_demand = avg_daily * demand_days
                sales_demand = ratio * (sales_daily * demand_days) / Decimal("1000") if ratio > 0 and sales_daily > 0 else Decimal("0")
                demand = max(usage_demand, sales_demand)
                par_override = item["par_override_count_units"]
                par = q(par_override) if par_override not in (None, "") else q(demand + avg_daily * safety)
                on_hand = q(estimate["estimated_on_hand"])
                needed_count_units = max(Decimal("0"), par - on_hand)
                units_per = q(item["units_per_purchase_unit"] or 1)
                raw_order = needed_count_units / units_per if units_per else needed_count_units
                multiple = q(item["order_multiple"] or settings.get("default_order_multiple", 1))
                suggested = ceil_multiple(raw_order, multiple)
                minimum = q(item["minimum_order_qty"] or 0)
                if suggested > 0 and suggested < minimum:
                    suggested = ceil_multiple(minimum, multiple)
                if suggested <= 0 and not include_zero:
                    continue
                current_price = m(item["current_price"] or 0)
                order_cost = (suggested * current_price).quantize(MONEY)
                notes = "Sales-adjusted" if sales_demand > usage_demand else "Usage-rate forecast"
                cursor = conn.execute(
                    """INSERT INTO order_predictions(
                           batch_id,item_id,vendor_name,vendor_sku,item_name,purchase_unit,count_unit,
                           units_per_purchase_unit,estimated_on_hand,inventory_confidence,average_daily_usage,
                           average_weekly_usage,lead_time_days,order_cycle_days,safety_stock_days,
                           par_quantity_count_units,suggested_order_quantity,manager_order_quantity,
                           order_multiple,current_price,estimated_order_cost,status,notes)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'Draft',?)""",
                    (
                        batch_id, item["item_id"], item["vendor_name"], item["vendor_sku"] or "", item["item_name"],
                        item["unit"] or "each", item["count_unit"] or item["unit"] or "each", f"{units_per:.4f}",
                        f"{on_hand:.4f}", estimate["confidence"], f"{avg_daily:.4f}", f"{avg_weekly:.4f}",
                        f"{lead:.4f}", f"{cycle:.4f}", f"{safety:.4f}", f"{par:.4f}",
                        f"{suggested:.4f}", f"{suggested:.4f}", f"{multiple:.4f}",
                        f"{current_price:.2f}", f"{order_cost:.2f}", notes,
                    ),
                )
                prediction_id = cursor.lastrowid
                try:
                    from margin_memory import MarginMemoryService
                    mm = MarginMemoryService(self.workspace, self, getattr(self, '_controls', None))
                    recs = mm.recommended_adjustments_for_item(item["item_id"], as_of=as_of.isoformat())
                    if recs:
                        top = recs[0]
                        action = top.get("recommended_action") or {}
                        learned_qty = action.get("order_quantity")
                        if learned_qty is not None:
                            try:
                                learned = q(learned_qty)
                                if learned > 0:
                                    suggested = learned
                                    order_cost = (suggested * current_price).quantize(MONEY)
                                    conn.execute(
                                        """UPDATE order_predictions
                                           SET suggested_order_quantity=?,estimated_order_cost=?,notes=COALESCE(?,notes)
                                           WHERE prediction_id=?""",
                                        (f"{suggested:.4f}", f"{order_cost:.2f}", "MarginMemory learned", prediction_id),
                                    )
                            except Exception:
                                pass
                except Exception:
                    pass
                rows.append({"prediction_id": prediction_id, "item_id": item["item_id"], "suggested_order_quantity": suggested})
        return {"batch_id": batch_id, "as_of_date": as_of.isoformat(), "item_count": len(rows), "sales_daily_rate": sales_daily}

    def ensure_weekly_order_draft(self, as_of_date: date | None = None) -> dict[str, Any]:
        as_of = as_of_date or date.today()
        with self.workspace.connect() as conn:
            active_items = int(conn.execute("SELECT COUNT(*) AS n FROM items WHERE active=1").fetchone()["n"])
        if active_items == 0:
            return {"batch_id": "", "as_of_date": as_of.isoformat(), "created": False, "reason": "No active items"}
        latest = self.latest_order_batch()
        if latest:
            latest_date = parse_date(latest["as_of_date"])
            latest_rows = len(self.list_order_predictions(latest["batch_id"]))
            if latest_date.isocalendar()[:2] == as_of.isocalendar()[:2] and latest_rows > 0:
                return {"batch_id": latest["batch_id"], "as_of_date": latest["as_of_date"], "created": False}
        result = self.generate_order_predictions(as_of)
        result["created"] = True
        return result

    def month_end_reminder(self, as_of_date: date | None = None) -> str:
        as_of = as_of_date or date.today()
        month = as_of.strftime("%Y-%m")
        summary = self.month_summary(month)
        if as_of.day >= 25 and str(summary.get("count_status", "")).startswith("Open"):
            return f"Month-end inventory count is due for {month}."
        return ""

    def latest_order_batch(self) -> sqlite3.Row | None:
        with self.workspace.connect() as conn:
            return conn.execute("SELECT * FROM order_batches ORDER BY created_at DESC LIMIT 1").fetchone()

    def list_order_predictions(self, batch_id: str | None = None) -> list[sqlite3.Row]:
        if not batch_id:
            latest = self.latest_order_batch()
            batch_id = latest["batch_id"] if latest else ""
        if not batch_id:
            return []
        with self.workspace.connect() as conn:
            return conn.execute(
                "SELECT * FROM order_predictions WHERE batch_id=? ORDER BY vendor_name,item_name", (batch_id,)
            ).fetchall()

    def update_order_prediction(self, prediction_id: int, manager_qty: Any, status: str = "Reviewed", notes: str | None = None) -> None:
        quantity = q(manager_qty)
        if quantity < 0:
            raise ValueError("Order quantity cannot be negative")
        with self.workspace.connect() as conn:
            row = conn.execute("SELECT current_price FROM order_predictions WHERE prediction_id=?", (prediction_id,)).fetchone()
            if not row:
                raise ValueError("Order prediction was not found")
            cost = (quantity * m(row["current_price"] or 0)).quantize(MONEY)
            conn.execute(
                """UPDATE order_predictions SET manager_order_quantity=?,estimated_order_cost=?,status=?,notes=COALESCE(?,notes)
                   WHERE prediction_id=?""",
                (f"{quantity:.4f}", f"{cost:.2f}", status, notes, prediction_id),
            )

    def approve_order_batch(self, batch_id: str) -> None:
        with self.workspace.connect() as conn:
            conn.execute("UPDATE order_batches SET status='Approved' WHERE batch_id=?", (batch_id,))
            conn.execute("UPDATE order_predictions SET status='Approved' WHERE batch_id=?", (batch_id,))

    def export_order_sheet_csv(self, batch_id: str | None = None, destination: Path | None = None, *, document_format: str = "csv") -> Path:
        from excel_io import write_table_as, resolve_document_format
        if not batch_id:
            latest = self.latest_order_batch()
            if not latest:
                raise ValueError("No order prediction has been generated")
            batch_id = latest["batch_id"]
        rows = self.list_order_predictions(batch_id)
        if not rows:
            raise ValueError("Order batch has no items")
        base = destination or (self.workspace.folders["orders"] / f"Order_Sheet_{batch_id}.csv")
        resolved_format, resolved_ext = resolve_document_format(document_format)
        destination = base if str(base).lower().endswith(resolved_ext) else base.with_suffix(resolved_ext)
        headers = [
            "Vendor", "Vendor SKU", "Item", "Purchase Unit", "Count Unit", "Estimated On Hand",
            "Average Weekly Usage", "Par Quantity (Count Units)", "Suggested Order Quantity",
            "Manager Order Quantity", "Current Price", "Estimated Order Cost", "Status", "Notes",
        ]
        records = []
        for row in rows:
            records.append({
                "Vendor": row["vendor_name"], "Vendor SKU": row["vendor_sku"] or "", "Item": row["item_name"],
                "Purchase Unit": row["purchase_unit"], "Count Unit": row["count_unit"], "Estimated On Hand": row["estimated_on_hand"],
                "Average Weekly Usage": row["average_weekly_usage"], "Par Quantity (Count Units)": row["par_quantity_count_units"],
                "Suggested Order Quantity": row["suggested_order_quantity"], "Manager Order Quantity": row["manager_order_quantity"],
                "Current Price": row["current_price"], "Estimated Order Cost": row["estimated_order_cost"], "Status": row["status"], "Notes": row["notes"],
            })
        return write_table_as(destination, records, document_format)

    def export_full_inventory_csv(self, destination: Path | None = None, as_of_date: date | None = None, *, document_format: str = "csv") -> Path:
        from excel_io import write_table_as, resolve_document_format
        as_of = as_of_date or date.today()
        base = destination or (self.workspace.folders["exports"] / f"Full_Inventory_{as_of.isoformat()}.csv")
        _, resolved_ext = resolve_document_format(document_format)
        destination = base if str(base).lower().endswith(resolved_ext) else base.with_suffix(resolved_ext)
        estimates = self.estimate_inventory(as_of)
        latest_batch = self.latest_order_batch()
        orders = {
            row["item_id"]: row for row in self.list_order_predictions(latest_batch["batch_id"] if latest_batch else None)
        }
        headers = [
            "As Of", "Item ID", "Vendor", "Vendor SKU", "Item", "Category", "Purchase Unit", "Count Unit",
            "Units Per Purchase Unit", "Current Purchase Price", "Inventory Unit Cost", "Last Physical Count",
            "Estimated On Hand", "Estimated Inventory Value", "Average Daily Usage", "Average Weekly Usage",
            "Inventory Confidence", "Par Quantity", "Suggested Order Quantity", "Manager Order Quantity",
        ]
        records = []
        for row in estimates:
            order = orders.get(row["item_id"])
            records.append({
                "As Of": as_of.isoformat(), "Item ID": row["item_id"], "Vendor": row["vendor_name"],
                "Vendor SKU": row["vendor_sku"], "Item": row["item_name"], "Category": row["category"],
                "Purchase Unit": row["purchase_unit"], "Count Unit": row["count_unit"],
                "Units Per Purchase Unit": f"{row['units_per_purchase_unit']:.4f}",
                "Current Purchase Price": f"{row['current_price']:.2f}",
                "Inventory Unit Cost": f"{row['inventory_unit_cost']:.2f}",
                "Last Physical Count": row["last_count_date"], "Estimated On Hand": f"{row['estimated_on_hand']:.4f}",
                "Estimated Inventory Value": f"{row['estimated_inventory_value']:.2f}",
                "Average Daily Usage": f"{row['average_daily_usage']:.4f}", "Average Weekly Usage": f"{row['average_weekly_usage']:.4f}",
                "Inventory Confidence": row["confidence"],
                "Par Quantity": order["par_quantity_count_units"] if order else "",
                "Suggested Order Quantity": order["suggested_order_quantity"] if order else "",
                "Manager Order Quantity": order["manager_order_quantity"] if order else "",
            })
        return write_table_as(destination, records, document_format)

    def planning_dashboard(self, year: int | None = None) -> dict[str, Any]:
        year = int(year or date.today().year)
        totals = self.year_totals(year)
        ready_to_close = sum(
            1
            for row in self.year_summary(year)
            if row.get("count_status") == "Open - count preview (not closed)"
            and str(row.get("period_start") or "") <= date.today().isoformat()
        )
        inventory = self.estimate_inventory(date.today())
        latest = self.latest_order_batch()
        order_rows = self.list_order_predictions(latest["batch_id"] if latest else None)
        totals.update({
            "estimated_inventory_value": sum((m(row["estimated_inventory_value"]) for row in inventory), Decimal("0")),
            "items_to_order": sum(1 for row in order_rows if d(row["manager_order_quantity"] or row["suggested_order_quantity"]) > 0),
            "latest_order_batch": latest["batch_id"] if latest else "",
            "ready_to_close_months": ready_to_close,
        })
        return totals
