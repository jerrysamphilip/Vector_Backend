"""Content-Disposition values for downloads (app/utils/http_files.py, W-12)."""
import re
from email.message import Message
from urllib.parse import unquote

from app.utils.http_files import MAX_FILENAME_CHARS, ascii_fallback, content_disposition


def _params(value: str) -> dict:
    """The two filename parameters; `filename` is what an RFC 6266 client shows (filename* wins)."""
    msg = Message()
    msg["Content-Disposition"] = value
    star = re.search(r"filename\*=UTF-8''([^;\s]+)", value).group(1)
    return {"type": msg.get_content_disposition(), "filename": unquote(star, encoding="utf-8"),
            "fallback": re.search(r'filename="((?:[^"\\]|\\.)*)"', value).group(1),
            "star": re.search(r"filename\*=UTF-8''([^;\s]+)", value).group(1)}


def _latin1_safe(value: str) -> None:
    value.encode("latin-1")  # Starlette encodes header values as latin-1
    assert value.isascii() and "\r" not in value and "\n" not in value


def test_plain_ascii_name():
    v = content_disposition("report.csv")
    assert v == "attachment; filename=\"report.csv\"; filename*=UTF-8''report.csv"
    _latin1_safe(v)


def test_inline():
    assert content_disposition("q.pdf", inline=True).startswith('inline; filename="q.pdf"')


def test_accented_name_is_transliterated_in_the_fallback():
    v = content_disposition("Café Menü.pdf")
    _latin1_safe(v)
    p = _params(v)
    assert p["fallback"] == "Cafe Menu.pdf"
    assert unquote(p["star"]) == "Café Menü.pdf"
    assert p["filename"] == "Café Menü.pdf"


def test_non_latin_name():
    v = content_disposition("日本語の見積書.pdf")
    _latin1_safe(v)
    p = _params(v)
    assert p["fallback"] == "download.pdf"
    assert p["star"] == "%E6%97%A5%E6%9C%AC%E8%AA%9E%E3%81%AE%E8%A6%8B%E7%A9%8D%E6%9B%B8.pdf"
    assert p["filename"] == "日本語の見積書.pdf"
    assert ascii_fallback("Отчёт 2026.csv") == "_ 2026.csv"


def test_quotes_and_backslashes_are_escaped():
    v = content_disposition('say "hi"\\there.txt')
    _latin1_safe(v)
    p = _params(v)
    assert p["fallback"] == 'say \\"hi\\"_there.txt'  # backslash is a path separator: replaced
    assert unquote(p["star"]) == 'say "hi"_there.txt'
    assert '"' not in p["star"]


def test_semicolons_and_commas_stay_inside_the_value():
    v = content_disposition("a; b, c=d.csv")
    p = _params(v)
    assert p["fallback"] == "a; b, c=d.csv"
    assert p["star"] == "a%3B%20b%2C%20c%3Dd.csv"
    assert p["filename"] == "a; b, c=d.csv"


def test_header_injection_and_percent():
    v = content_disposition("evil\r\nSet-Cookie: x=1%41.csv")
    _latin1_safe(v)
    p = _params(v)
    assert "%" not in p["fallback"]  # no client percent-decodes the fallback
    assert unquote(p["star"]) == "evilSet-Cookie: x=1%41.csv"


def test_very_long_name_keeps_extension():
    v = content_disposition("é" * 1000 + ".xlsx")
    _latin1_safe(v)
    p = _params(v)
    assert len(p["filename"]) == MAX_FILENAME_CHARS and p["filename"].endswith(".xlsx")
    assert p["fallback"].endswith(".xlsx") and len(p["fallback"]) <= MAX_FILENAME_CHARS


def test_empty_or_dots_only():
    assert _params(content_disposition(""))["filename"] == "download"
    assert _params(content_disposition("..."))["filename"] == "download"
