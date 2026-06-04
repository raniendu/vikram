from datetime import datetime

from vikram.context import agent_identity, current_datetime


def test_current_datetime_includes_iso_date_and_tz():
    result = current_datetime()
    now = datetime.now().astimezone()

    assert result.startswith("Current date and time: ")
    assert now.date().isoformat() in result
    assert f"({now.tzname()})" in result


def test_agent_identity_uses_provided_name():
    assert agent_identity("alfred") == "Your name is alfred."
