"""LIKE / ILIKE patterns built from user input.

A search box value is matched literally: ``%`` and ``_`` typed by the user are not wildcards, and a backslash is
not an escape. Every user-input LIKE goes through :func:`like_pattern` (or :func:`ilike`), which escapes
``\\``, ``%`` and ``_`` with ``LIKE_ESCAPE`` and adds the wildcards for the match mode."""
from typing import Any, Literal

from sqlalchemy import ColumnElement

LIKE_ESCAPE = "\\"

LikeMode = Literal["contains", "prefix", "suffix", "exact"]


def escape_like(term: str) -> str:
    """The term with LIKE metacharacters escaped (backslash first, so added escapes are not doubled)."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def like_pattern(term: Any, mode: LikeMode = "contains") -> str:
    """A LIKE pattern matching ``term`` literally; use with ``escape=LIKE_ESCAPE``.

    contains: ``%term%``; prefix: ``term%``; suffix: ``%term``; exact: ``term``."""
    escaped = escape_like(str(term))
    if mode == "contains":
        return f"%{escaped}%"
    if mode == "prefix":
        return f"{escaped}%"
    if mode == "suffix":
        return f"%{escaped}"
    if mode == "exact":
        return escaped
    raise ValueError(f"Unknown LIKE mode: {mode}")


def ilike(column: Any, term: Any, mode: LikeMode = "contains") -> ColumnElement[bool]:
    """Case-insensitive literal match of ``term`` against ``column``."""
    return column.ilike(like_pattern(term, mode), escape=LIKE_ESCAPE)


def like(column: Any, term: Any, mode: LikeMode = "contains") -> ColumnElement[bool]:
    """Case-sensitive (collation-dependent) literal match of ``term`` against ``column``."""
    return column.like(like_pattern(term, mode), escape=LIKE_ESCAPE)
