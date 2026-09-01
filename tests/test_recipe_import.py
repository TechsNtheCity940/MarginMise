from pathlib import Path

from openpyxl import Workbook

from invoice_pipeline import InvoicePipeline, RestaurantWorkspace


def test_recipe_excel_matches_inventory_ids_and_imports_all_lines(tmp_path: Path):
    workspace = RestaurantWorkspace(tmp_path / "workspace")
    pipeline = InvoicePipeline(workspace)

    with workspace.connect() as conn:
        conn.execute(
            """INSERT INTO items(
                item_id,vendor_key,vendor_name,item_name,normalized_description,vendor_sku,current_price
            ) VALUES(?,?,?,?,?,?,?)""",
            ("ITM-BUN", "TEST", "Test Vendor", "Hamburger buns", "HAMBURGER BUNS", "DG-004", "38.00"),
        )
        conn.execute(
            """INSERT INTO items(
                item_id,vendor_key,vendor_name,item_name,normalized_description,vendor_sku,current_price
            ) VALUES(?,?,?,?,?,?,?)""",
            ("ITM-CHEESE", "TEST", "Test Vendor", "Cheddar cheese", "CHEDDAR CHEESE", "DA-001", "118.00"),
        )

    workbook_path = tmp_path / "recipes.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "Recipes"
    ws.append([
        "Menu Item Name", "POS Item Key", "Menu Category", "Menu Price",
        "Ingredient Name", "Inventory Item ID", "Quantity Count Units",
        "Count Unit", "Yield Percent", "Notes",
    ])
    ws.append(["Classic Burger", "BURGER-001", "Bar & Grill", 14.99, "Hamburger buns", "DG-004", 0.04, "each", 100, ""])
    ws.append(["Classic Burger", "BURGER-001", "Bar & Grill", 14.99, "Cheddar cheese", "DA-001", 0.08, "lb", 100, ""])
    wb.save(workbook_path)

    result = pipeline.phase2.import_recipes_csv(workbook_path)
    assert result["imported"] == 2
    assert result["skipped"] == 0
    assert result["errors"] == []

    with workspace.connect() as conn:
        rows = conn.execute("SELECT COUNT(*) AS n FROM recipe_ingredients").fetchone()
        assert rows["n"] == 2
