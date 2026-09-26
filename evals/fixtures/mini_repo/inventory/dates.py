"""Date helpers for stock expiry."""

from datetime import date


def parse_date(text: str) -> date:
    """Parse a 'YYYY-MM-DD' string."""
    year, month, day = (int(part) for part in text.strip().split("-"))
    return date(year, month, day)


def is_expired(expiry: str, today: str) -> bool:
    """An item is expired on and after its expiry date."""
    return parse_date(today) > parse_date(expiry)
