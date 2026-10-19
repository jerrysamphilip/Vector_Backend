"""LIKE pattern escaping for user search input (app/utils/sql_like.py)."""
import pytest
from sqlalchemy import column
from sqlalchemy.dialects import mysql

from app.utils.sql_like import LIKE_ESCAPE, escape_like, ilike, like, like_pattern


@pytest.mark.parametrize("term,expected", [
    ("abc", "abc"), ("50%", "50\\%"), ("a_b", "a\\_b"), ("c:\\x", "c:\\\\x"), ("\\%", "\\\\\\%"), ("", ""),
])
def test_escape_like(term, expected):
    assert escape_like(term) == expected


@pytest.mark.parametrize("mode,expected", [
    ("contains", "%50\\%%"), ("prefix", "50\\%%"), ("suffix", "%50\\%"), ("exact", "50\\%"),
])
def test_like_pattern_modes(mode, expected):
    assert like_pattern("50%", mode) == expected


def test_like_pattern_default_contains_and_non_str():
    assert like_pattern(12) == "%12%"
    with pytest.raises(ValueError):
        like_pattern("x", "fuzzy")  # type: ignore[arg-type]


def test_rendered_sql_carries_the_escape_clause():
    sql = str(ilike(column("name"), "a_b").compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "ESCAPE '\\\\'" in sql and "a\\\\_b" in sql
    assert LIKE_ESCAPE == "\\"
    assert "ESCAPE" in str(like(column("name"), "x").compile(dialect=mysql.dialect()))
