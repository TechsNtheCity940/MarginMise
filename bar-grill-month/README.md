# Barrel & Flame Bar + Grill — MarginMise Test Data

Fictional one-month test dataset for July 2026. All records are synthetic.

## Files
- `invoices/`: 35 image-only PDF invoice scans across five vendors, delivered on a realistic cadence throughout the month. Each scan is an image-only PDF (no text layer) for OCR testing.
- `recipe_guide.xlsx`: bar-and-grill recipes using the inventory item IDs.
- `sales_detail_july_2026.csv`: combined daily item-level sales for July (684 line items).
- `daily_sales_reports/`: 31 individual daily sales report CSVs, one per business day (2026-07-01 through 2026-07-31), each item-level with the same columns as the combined file.
- `daily_sales_july_2026.csv`: daily net-sales series for dashboard charts.
- `sales_summary_july_2026.csv`: month summary.
- `inventory_counts.xlsx`: Beginning Inventory on July 1 and Ending Inventory on July 31.

Categories represented in inventory, invoices, recipes, and/or sales inputs: Dry Goods, Frozen Goods, Dairy, Produce, Beverage, Liquor, and Misc.
