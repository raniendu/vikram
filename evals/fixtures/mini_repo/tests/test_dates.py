from datetime import date

from inventory.dates import is_expired, parse_date


def test_parse_date():
    assert parse_date("2024-03-09") == date(2024, 3, 9)


def test_is_expired_after_expiry_day():
    assert is_expired("2024-03-09", "2024-03-10")


def test_is_expired_on_expiry_day():
    assert is_expired("2024-03-09", "2024-03-09")


def test_not_expired_before_expiry_day():
    assert not is_expired("2024-03-09", "2024-03-08")
