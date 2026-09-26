"""Plain-text stock report."""

from inventory.dates import is_expired
from inventory.stock import Item, compute_total


def build_report(items: list[Item], today: str) -> str:
    """One line per item, flagging expired stock, then the total value."""
    lines = []
    for item in items:
        flag = " (EXPIRED)" if is_expired(item.expiry, today) else ""
        lines.append(f"{item.name}: {item.quantity} x {item.unit_price:.2f}{flag}")
    lines.append(f"Total value: {compute_total(items):.2f}")
    return "\n".join(lines)
