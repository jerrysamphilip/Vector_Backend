"""Unit tests (no database) for the contact search condition after 0034_drop_contact_fulltext: a plain LIKE
on prospects.search_text, one per search term, user metacharacters escaped, no MATCH."""
import pytest
from sqlalchemy.dialects import mysql

from app.domain.contacts import search_terms
from app.repositories import contacts as contacts_repo
from app.repositories.contacts import search_condition


def _sql(text):
    return str(search_condition(text).compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True}))


@pytest.mark.parametrize("text", ["maria", "Maria Garcia", "li", "a", "al*x", '"quoted"', "+news", "小龙", "😀"])
def test_final_csc_u01_one_like_per_term_and_no_match(text):
    sql = _sql(text)
    assert "MATCH" not in sql and "AGAINST" not in sql
    assert sql.count("prospects.search_text LIKE") == len(search_terms(text)) == 1


def test_final_csc_u02_phone_query_is_text_or_digits():
    sql = _sql("737-555-0188")
    assert sql.count("prospects.search_text LIKE") == 2 and " OR " in sql
    assert "'%%737-555-0188%%'" in sql and "'%%7375550188%%'" in sql


@pytest.mark.parametrize("text,pattern", [("%", "%%\\\\%%%%"), ("_", "%%\\\\_%%"), ("\\", "%%\\\\\\\\%%")])
def test_final_csc_u03_like_metacharacters_are_escaped(text, pattern):
    sql = _sql(text)
    assert f"LIKE '{pattern}' ESCAPE" in sql, sql


def test_final_csc_u04_fulltext_helpers_are_gone():
    assert not hasattr(contacts_repo, "fulltext_query")
    assert not hasattr(contacts_repo, "FULLTEXT_TOKEN_SIZE")
