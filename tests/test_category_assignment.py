from invoice_pipeline import (
    CANONICAL_INVENTORY_CATEGORIES,
    auto_assign_inventory_category,
    canonical_inventory_category,
)


def test_canonical_categories_are_exactly_the_five_upload_categories():
    assert CANONICAL_INVENTORY_CATEGORIES == (
        "Dry goods", "Beverage", "Produce", "Dairy", "alcohol"
    )


def test_common_invoice_items_are_auto_categorized():
    cases = {
        "Romaine lettuce": "Produce",
        "Fresh lemons": "Produce",
        "Cheddar cheese": "Dairy",
        "Butter unsalted": "Dairy",
        "Coca Cola syrup": "Beverage",
        "Bottled water": "Beverage",
        "Cabernet Sauvignon wine": "alcohol",
        "Bourbon whiskey": "alcohol",
        "Tomato paste canned": "Dry goods",
        "Olive oil": "Dry goods",
        "French fries frozen": "Dry goods",
        "Chicken breast": "Dry goods",
    }
    for description, expected in cases.items():
        assert auto_assign_inventory_category(description) == expected


def test_unknown_items_fall_back_to_dry_goods():
    assert auto_assign_inventory_category("Mystery pantry item") == "Dry goods"


def test_imported_aliases_normalize_to_canonical_categories():
    assert canonical_inventory_category("Dairy and Eggs", "eggs") == "Dairy"
    assert canonical_inventory_category("Beverages", "juice") == "Beverage"
    assert canonical_inventory_category("alcoholic", "spirits") == "alcohol"
    assert canonical_inventory_category("Pantry", "rice") == "Dry goods"
