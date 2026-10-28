"""Unit tests (no database) for the contact search FULLTEXT pre-filter (chunk D7): the BOOLEAN MODE query
built from the search terms, operator escaping, the short-term LIKE fallback and the compiled condition."""
import pytest
from sqlalchemy.dialects import mysql

from app.domain.contacts import search_terms
from app.repositories.contacts import FULLTEXT_TOKEN_SIZE, fulltext_query, search_condition


def _q(text):
    return fulltext_query(search_terms(text))


def _sql(text):
    return str(search_condition(text).compile(dialect=mysql.dialect(), compile_kwargs={"literal_binds": True}))


def test_dbb_ftq_u01_token_size_is_the_mysql_default():
    assert FULLTEXT_TOKEN_SIZE == 2


@pytest.mark.parametrize("text,expected", [
    ("smith", '+"smith"'),
    ("Maria Garcia", '+"maria" +"garcia"'),
    ("john.smith@acme.com", '+"john" +"smith" +"acme" +"com"'),
    ("li", '+"li"'),
    ("li_wei", '+"li_wei"'),
    ("zoë", '+"zoë"'),
    ("小龙", '+"小龙"'),
    ("ann ann", '+"ann"'),            # repeated fragments once
    ("a smith", '+"smith"'),          # one-character words cannot use the index, the rest still can
])
def test_dbb_ftq_u02_fragments_become_required_phrases(text, expected):
    assert _q(text) == expected


@pytest.mark.parametrize("text", ['al*x', 'ma(x)', '"quoted"', "-jones", "+news", "~tilde", "<lt>", "@at", "xx>yy"])
def test_dbb_ftq_u03_boolean_operators_never_reach_the_query(text):
    query = _q(text)
    assert query is not None
    inner = query.replace('+"', "").replace('"', "")
    assert not set(inner) & set('+-<>()~*"@'), query


@pytest.mark.parametrize("text", ["a", "j s", "-", "*", '"', "()", "@", "%", "😀", "x+y"])
def test_dbb_ftq_u04_terms_shorter_than_a_token_fall_back_to_like(text):
    assert _q(text) is None
    sql = _sql(text)
    assert "MATCH" not in sql and sql.count("LIKE") == len(search_terms(text))


def test_dbb_ftq_u05_phone_query_is_an_alternative_of_text_and_digits():
    assert _q("737-555-0188") == '(+"737" +"555" +"0188") (+"7375550188")'
    sql = _sql("737-555-0188")
    assert sql.startswith("MATCH (prospects.search_text) AGAINST (")
    assert "IN BOOLEAN MODE" in sql and sql.count("LIKE") == 2


def test_dbb_ftq_u06_any_term_without_a_fragment_disables_the_prefilter():
    assert fulltext_query(("smith", "a")) is None
    assert fulltext_query(()) is None


def test_dbb_ftq_u07_like_still_decides():
    sql = _sql("maria")
    assert "MATCH (prospects.search_text) AGAINST ('+\"maria\"' IN BOOLEAN MODE)" in sql
    assert "prospects.search_text LIKE '%%maria%%'" in sql  # the exact substring condition is kept


def test_dbb_ftq_u08_symbols_and_astral_characters_split_fragments():
    # Characters the ngram parser might not index are separators: the pre-filter only gets weaker
    assert _q("ab😀cd") == '+"ab" +"cd"'
    assert _q("ab²cd") == '+"ab" +"cd"'
