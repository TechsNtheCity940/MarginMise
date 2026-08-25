# Restaurant Cost Controller Project Rules

## Default folders

- Upload Invoices
- Processed Invoices
- Needs Review
- Monthly Sales
- Original Documents
- Exports

Do not move a source invoice to Processed Invoices until validation succeeds.

## Workbook

Use `assets/Restaurant_Cost_Tracker_Template.xlsx`.

Required sheets:

- START HERE
- Dashboard
- Invoice Log
- Item Price Log
- Item Master
- Sales Summary
- Review Queue
- Settings

## Invoice ingestion

1. Preserve the source file and its link or path.
2. Extract the invoice into the canonical JSON schema.
3. Run the included `invoice_normalizer.py`.
4. Review its validation report.
5. Post approved invoice and line-item rows.
6. Add unmatched products to Item Master as new items requiring review.
7. Add uncertainty to Review Queue.
8. Recalculate the Dashboard.
9. Move the source only after successful posting.

## Product matching priority

1. Vendor + vendor SKU
2. Manager-approved alias
3. Vendor + normalized description
4. New Item ID requiring review

Do not merge products merely because descriptions appear similar.

## Sales

Accept weekly or monthly sales. Preserve supplied net sales and do not subtract tax twice.

## Month-end

Confirm invoices and sales, disclose open review items, generate purchase-to-sales metrics, calculate COGS when inventory values exist, export a manager report, and back up source records.
