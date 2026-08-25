---
name: restaurant-cost-control
description: Process restaurant invoices, maintain item and price history, compare purchasing costs with sales, and produce auditable management reports.
version: 1.0.0
author: Prepared for Morgan Griffin
license: Private Use
metadata:
  hermes:
    tags: [Restaurant, Invoices, Cost Control, Purchasing, Sales, Google Sheets, OCR]
    related_skills: [google-workspace, ocr-and-documents]
---

# Restaurant Cost Control

## When to use

Use this skill for restaurant delivery invoices, vendor-price histories, new-item creation, purchasing-cost analysis, sales comparisons, month-end cost reports, and duplicate or price-change audits.

## Canonical files

- Workbook: `assets/Restaurant_Cost_Tracker_Template.xlsx`
- Business config: `assets/business_config.example.yaml`
- Invoice schema: `references/invoice_extraction_schema.md`
- Validation rules: `references/validation_rules.md`
- Helper: `scripts/invoice_normalizer.py`

## Invoice procedure

1. Preserve the source.
2. Extract it to the canonical JSON schema.
3. Run:

   `python scripts/invoice_normalizer.py extracted_invoice.json --item-master item_master.csv --output-dir normalized-output`

4. Inspect `validation_report.json`.
5. Do not auto-post records marked `Needs Review`.
6. Append approved headers to Invoice Log.
7. Append lines to Item Price Log.
8. Append unmatched products to Item Master.
9. Append flags to Review Queue.
10. Recalculate the Dashboard.

## Matching

- Primary: normalized vendor + vendor SKU
- Secondary: approved alias
- Fallback: normalized vendor + normalized description
- Otherwise: deterministic new Item ID requiring review

## Calculations

- Purchases = approved invoice totals
- Purchases % of Sales = Purchases / Net Sales
- Sales Minus Purchases = Net Sales - Purchases
- Estimated Purchase Margin = Sales Minus Purchases / Net Sales
- COGS = Opening Inventory + Purchases - Closing Inventory
- Food Cost % = COGS / Net Sales
- Gross Food Margin = (Net Sales - COGS) / Net Sales

## Verification

Confirm duplicate protection, invoice arithmetic, product identity or review status, source links, and correct net-sales treatment. Never label sales minus purchases as net profit.
