#!/usr/bin/env python3
"""Validate and normalize restaurant delivery-invoice extraction JSON."""

from __future__ import annotations
import argparse, csv, hashlib, json, re, sys, unicodedata
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

MONEY = Decimal("0.01")

def money(value: Any, field: str) -> Decimal:
    try:
        return Decimal(str(value or 0)).quantize(MONEY, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid money value for {field}: {value!r}") from exc

def number(value: Any, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid numeric value for {field}: {value!r}") from exc

def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^A-Za-z0-9]+", " ", text).strip().upper()
    return re.sub(r"\s+", " ", text)

def parse_date(value: Any) -> str:
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError(f"Unsupported or missing invoice_date: {text!r}")

def deterministic_id(prefix: str, value: str, length: int = 12) -> str:
    return f"{prefix}-{hashlib.sha256(value.encode()).hexdigest()[:length].upper()}"

def load_item_master(path: Path | None):
    by_sku, by_description = {}, {}
    if not path or not path.exists():
        return by_sku, by_description
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            item_id = (row.get("Item ID") or row.get("item_id") or "").strip()
            vendor = normalize_text(row.get("Vendor") or row.get("vendor"))
            sku = normalize_text(row.get("Vendor SKU") or row.get("sku"))
            name = normalize_text(row.get("Item Name") or row.get("description"))
            if item_id and vendor and sku:
                by_sku[f"{vendor}|{sku}"] = item_id
            if item_id and vendor and name:
                by_description[f"{vendor}|{name}"] = item_id
    return by_sku, by_description

def write_csv(path: Path, headers, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("invoice_json", type=Path)
    p.add_argument("--item-master", type=Path)
    p.add_argument("--output-dir", type=Path, default=Path("normalized-output"))
    p.add_argument("--tolerance", default="0.05")
    p.add_argument("--confidence-minimum", default="0.85")
    args = p.parse_args()

    tolerance = money(args.tolerance, "tolerance")
    confidence_minimum = Decimal(str(args.confidence_minimum))
    data = json.loads(args.invoice_json.read_text(encoding="utf-8"))
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)

    vendor = str(data.get("vendor") or "").strip()
    vendor_norm = normalize_text(vendor)
    invoice_number = str(data.get("invoice_number") or "").strip()
    invoice_date = parse_date(data.get("invoice_date"))
    subtotal = money(data.get("subtotal"), "subtotal")
    fees = money(data.get("fees"), "fees")
    tax = money(data.get("tax"), "tax")
    credits = money(data.get("credits"), "credits")
    total = money(data.get("total"), "total")
    source = str(data.get("source_link") or data.get("source_file") or "")

    invoice_key = f"{vendor_norm}|{invoice_number.upper()}|{invoice_date}|{total}"
    invoice_id = deterministic_id("INV", invoice_key)
    by_sku, by_description = load_item_master(args.item_master)

    price_rows, new_items, review_rows = [], [], []
    line_sum = Decimal("0.00")
    items = data.get("items") if isinstance(data.get("items"), list) else []

    for index, item in enumerate(items, 1):
        sku = str(item.get("sku") or "").strip()
        description = str(item.get("description") or "").strip()
        quantity = number(item.get("quantity"), f"line {index} quantity")
        unit = str(item.get("unit") or "").strip()
        unit_price = money(item.get("unit_price"), f"line {index} unit_price")
        line_total = money(item.get("line_total"), f"line {index} line_total")
        confidence = Decimal(str(item.get("confidence", 0)))
        line_sum += line_total

        sku_key = f"{vendor_norm}|{normalize_text(sku)}" if sku else ""
        desc_key = f"{vendor_norm}|{normalize_text(description)}" if description else ""
        if sku_key and sku_key in by_sku:
            item_id, match_status = by_sku[sku_key], "Matched"
        elif desc_key and desc_key in by_description:
            item_id, match_status = by_description[desc_key], "Description Match - Review"
        else:
            item_id = deterministic_id("ITM", sku_key or desc_key or f"{vendor_norm}|{index}")
            match_status = "New Item"
            new_items.append({
                "Item ID": item_id, "Vendor": vendor, "Vendor SKU": sku,
                "Item Name": description, "Category": "Unclassified",
                "First Price": f"{unit_price:.2f}",
                "Current Price": f"{unit_price:.2f}",
                "Review Status": "New Item - Review Required",
            })

        issues = []
        expected = (quantity * unit_price).quantize(MONEY, rounding=ROUND_HALF_UP)
        if not description: issues.append("Missing description")
        if not sku: issues.append("Missing vendor SKU")
        if quantity <= 0: issues.append("Quantity must be positive")
        if abs(expected - line_total) > tolerance:
            issues.append(f"Line arithmetic mismatch: expected {expected}, found {line_total}")
        if confidence < confidence_minimum:
            issues.append(f"Low confidence: {confidence}")
        if match_status != "Matched":
            issues.append(match_status)

        price_rows.append({
            "Invoice ID": invoice_id, "Invoice Date": invoice_date,
            "Month": invoice_date[:7], "Vendor": vendor, "Vendor SKU": sku,
            "Item ID": item_id, "Item Description": description,
            "Category": "Unclassified", "Quantity": str(quantity),
            "Unit/Pack": unit, "Unit Price": f"{unit_price:.2f}",
            "Line Total": f"{line_total:.2f}", "Previous Price": "",
            "Price Change %": "", "Price Alert": "", "Match Status": match_status,
            "Confidence %": f"{confidence * 100:.2f}",
            "Source Link": source, "Notes": "; ".join(issues),
        })
        for issue in issues:
            review_rows.append({
                "Created Date": datetime.now().date().isoformat(),
                "Type": "Invoice Line", "Invoice ID": invoice_id,
                "Vendor": vendor, "Item ID": item_id, "Issue": issue,
                "Source Link": source, "Status": "Open", "Resolution": "",
            })

    calculated_total = (subtotal + fees + tax - credits).quantize(MONEY)
    header_issues = []
    if not vendor or not invoice_number or not items:
        header_issues.append("Missing required invoice header or items")
    if abs(line_sum - subtotal) > tolerance:
        header_issues.append(f"Line sum {line_sum} does not match subtotal {subtotal}")
    if abs(calculated_total - total) > tolerance:
        header_issues.append(f"Calculated total {calculated_total} does not match {total}")

    for issue in header_issues:
        review_rows.append({
            "Created Date": datetime.now().date().isoformat(),
            "Type": "Invoice Header", "Invoice ID": invoice_id,
            "Vendor": vendor, "Item ID": "", "Issue": issue,
            "Source Link": source, "Status": "Open", "Resolution": "",
        })

    approval = "Approved" if not review_rows else "Needs Review"
    invoice_row = {
        "Invoice ID": invoice_id, "Invoice Date": invoice_date,
        "Month": invoice_date[:7], "Vendor": vendor,
        "Invoice Number": invoice_number, "Subtotal": f"{subtotal:.2f}",
        "Fees": f"{fees:.2f}", "Tax": f"{tax:.2f}",
        "Credits": f"{credits:.2f}", "Invoice Total": f"{total:.2f}",
        "Approved?": "Yes" if approval == "Approved" else "Pending",
        "Source Link": source, "Notes": "; ".join(header_issues),
    }
    report = {
        "invoice_id": invoice_id, "approval_status": approval,
        "line_sum": f"{line_sum:.2f}", "stated_subtotal": f"{subtotal:.2f}",
        "calculated_total": f"{calculated_total:.2f}",
        "stated_total": f"{total:.2f}", "review_count": len(review_rows),
        "new_item_count": len(new_items), "duplicate_key": invoice_key,
    }

    (out / "validation_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out / "normalized_invoice.json").write_text(json.dumps({**data, "invoice_id": invoice_id}, indent=2), encoding="utf-8")
    write_csv(out / "invoice_log.csv", list(invoice_row), [invoice_row])
    write_csv(out / "item_price_log.csv", list(price_rows[0]) if price_rows else [], price_rows)
    write_csv(out / "new_items.csv", list(new_items[0]) if new_items else ["Item ID"], new_items)
    write_csv(out / "review_queue.csv", list(review_rows[0]) if review_rows else ["Issue"], review_rows)
    print(json.dumps(report, indent=2))
    return 0 if approval == "Approved" else 2

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
