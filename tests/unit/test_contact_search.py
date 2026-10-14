"""Unit tests (no database) for the contact search condition (BR-CM-21): the digits-only phone match
applies to phone-like queries only, so a mixed query does not match every record containing its digits."""
import pytest

from app.routers.contacts_router import search_condition


def _likes(q):
    sql = str(search_condition(q).compile(compile_kwargs={"literal_binds": True}))
    return sql.count("LIKE")


@pytest.mark.parametrize("q", ["737 555 0188", "737-555-0188", "+1 (737) 555-0188", "737.555.0188"])
def test_cs_u01_phone_like_query_adds_digits_match(q):
    assert _likes(q) == 2


@pytest.mark.parametrize("q", ["zorblax66034", "restoreme.66034a", "kilo1234", "acme 2024 ltd"])
def test_cs_u02_mixed_query_matches_text_only(q):
    assert _likes(q) == 1


def test_cs_u03_plain_digits_query_single_match():
    assert _likes("7375550188") == 1  # digits == term: the text match already covers it
    assert _likes("123") == 1         # fewer than 4 digits
