"""Stock items and totals."""

from dataclasses import dataclass


@dataclass
class Item:
    name: str
    quantity: int
    unit_price: float
    expiry: str


def compute_total(items: list[Item]) -> float:
    """Total value of all items in stock."""
    return round(sum(item.quantity * item.unit_price for item in items), 2)
