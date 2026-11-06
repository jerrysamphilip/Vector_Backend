"""A contact's time zone from country + state, or set by a user (CM-01, BR-CM-01; app/utils/contact_timezone.py)."""
from types import SimpleNamespace

import pytest

from app.utils import contact_timezone as ct


@pytest.mark.parametrize("country,state,zone", [
    ("India", "Maharashtra", "Asia/Kolkata"), ("IN", None, "Asia/Kolkata"), ("india", "", "Asia/Kolkata"),
    ("United Kingdom", "London", "Europe/London"), ("UK", None, "Europe/London"), ("Ireland", None, "Europe/Dublin"),
    ("Germany", "Bavaria", "Europe/Berlin"), ("France", None, "Europe/Paris"), ("Netherlands", None, "Europe/Amsterdam"),
    ("Spain", None, "Europe/Madrid"), ("Italy", None, "Europe/Rome"), ("Singapore", None, "Asia/Singapore"),
    ("UAE", "Dubai", "Asia/Dubai"), ("United Arab Emirates", None, "Asia/Dubai"), ("Japan", None, "Asia/Tokyo"),
    ("USA", "CA", "America/Los_Angeles"), ("United States", "Texas", "America/Chicago"), (None, "NY", "America/New_York"),
    ("Canada", "BC", "America/Vancouver"), ("Canada", "Ontario", "America/Toronto"),
    ("Australia", "Western Australia", "Australia/Perth"), ("Brazil", "SP", "America/Sao_Paulo"),
])
def test_derive_timezone_from_country_and_state(country, state, zone):
    assert ct.derive_timezone(country, state) == zone


@pytest.mark.parametrize("country,state", [
    ("USA", None), ("USA", ""), (None, None), ("USA", "Atlantis"), ("Canada", None), ("Australia", "Nowhere"),
    ("Narnia", "X"), (None, "Maharashtra"),  # no prefix guessing: "Maharashtra" is not Massachusetts
])
def test_unknown_location_has_no_zone_instead_of_new_york(country, state):
    assert ct.derive_timezone(country, state) is None


def test_valid_timezone_accepts_only_iana_names():
    assert ct.valid_timezone(" Asia/Kolkata ") == "Asia/Kolkata"
    assert ct.valid_timezone("UTC") == "UTC"
    for bad in ("Mars/Base", "", None, "../etc/passwd", "asia/kolkata"):
        assert ct.valid_timezone(bad) is None


def test_resolve_prefers_a_valid_stored_zone():
    assert ct.resolve_timezone("Europe/Paris", "India", None) == "Europe/Paris"
    assert ct.resolve_timezone("Nope/Zone", "India", None) == "Asia/Kolkata"
    assert ct.contact_timezone_of(None) is None
    assert ct.contact_timezone_of(SimpleNamespace(timezone=None, poc_country="Japan", poc_state=None)) == "Asia/Tokyo"


def _contact(country, state, zone):
    return SimpleNamespace(poc_country=country, poc_state=state, timezone=zone)


def test_refresh_rederives_a_derived_zone_after_a_move():
    c = _contact("India", None, "Asia/Kolkata")
    c.poc_country = "Japan"
    ct.refresh_contact_timezone(c, "India", None)
    assert c.timezone == "Asia/Tokyo"


def test_refresh_fixes_the_old_new_york_default():
    c = _contact("India", "Maharashtra", "America/New_York")  # what the US-only mapping stored before CM-01
    ct.refresh_contact_timezone(c, "India", "Maharashtra")
    assert c.timezone == "Asia/Kolkata"


def test_refresh_keeps_a_zone_a_user_set():
    c = _contact("India", None, "Europe/Paris")
    c.poc_country = "Japan"
    ct.refresh_contact_timezone(c, "India", None)
    assert c.timezone == "Europe/Paris"


def test_refresh_fills_a_blank_zone():
    c = _contact("Germany", None, None)
    ct.refresh_contact_timezone(c, "Germany", None)
    assert c.timezone == "Europe/Berlin"
