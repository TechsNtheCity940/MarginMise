from decimal import Decimal

from dashboard_service import DashboardService
from inventory_planning import infer_count_conversion


def test_pack_conversion_recognizes_ocr_ib_typo():
    unit, count = infer_count_conversion("40 Ib case", "40 Ib case")
    assert unit == "lb"
    assert count == Decimal("40.0000")


def test_dashboard_prefers_theoretical_cogs_over_purchase_spend(tmp_path):
    class FakePhase2:
        def list_menu_costs(self, start, end):
            return [{"category": "Food", "theoretical_food_cost": "125.00"}]

    class FakePipeline:
        def __init__(self, workspace):
            self.workspace = workspace
            self.phase2 = FakePhase2()

    from invoice_pipeline import RestaurantWorkspace
    from inventory_planning import InventoryPlanningService
    workspace = RestaurantWorkspace(tmp_path / "restaurant")
    InventoryPlanningService(workspace)
    service = DashboardService(FakePipeline(workspace), cache_seconds=0)
    cost, source, _ = service._estimated_product_cost(
        __import__("datetime").date(2026, 7, 1),
        __import__("datetime").date(2026, 7, 31),
        purchase_rows=[{"date": "2026-07-01", "value": 190000.0}],
    )
    assert cost == 125.0
    assert "theoretical" in source.lower()
