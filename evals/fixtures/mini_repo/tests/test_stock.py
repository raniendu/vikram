from inventory.report import build_report
from inventory.stock import Item, compute_total

ITEMS = [
    Item("milk", 3, 1.25, "2024-03-09"),
    Item("rice", 2, 4.10, "2025-01-01"),
]


def test_compute_total():
    assert compute_total(ITEMS) == 11.95


def test_build_report_flags_expired():
    report = build_report(ITEMS, "2024-03-10")
    assert "milk: 3 x 1.25 (EXPIRED)" in report
    assert "Total value: 11.95" in report
