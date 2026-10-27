"""
Paging and sorting parameter objects (ADR 0009).

Routers build these from query parameters and pass them down unchanged; services and repositories
accept them instead of loose ``page`` / ``page_size`` / ``sort_by`` / ``sort_dir`` arguments. Pure
values: no I/O, no SQLAlchemy. Invalid values raise ``ValueError`` (inside a pydantic validator it
becomes a 422; ``handle_route_error`` maps it to 400). ``app.core.errors`` is not used here because it
imports FastAPI for its handler, and the domain layer must be pure end to end.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar, Generic, TypeVar

T = TypeVar("T")


class SortDirection(str, Enum):
    ASC = "asc"
    DESC = "desc"


@dataclass(frozen=True, slots=True)
class Sort:
    """One sort key. ``field`` is a public field name; the repository maps it to a column."""

    field: str
    direction: SortDirection = SortDirection.ASC

    def __post_init__(self) -> None:
        if not self.field:
            raise ValueError("Sort field must not be empty")

    @property
    def descending(self) -> bool:
        return self.direction is SortDirection.DESC

    @classmethod
    def parse(cls, value: str, allowed: frozenset[str] | None = None) -> Sort:
        """``"name"`` -> ascending, ``"-created_at"`` -> descending; optionally checked against ``allowed``."""
        raw = value.strip()
        direction = SortDirection.DESC if raw.startswith("-") else SortDirection.ASC
        field = raw.lstrip("-+")
        if allowed is not None and field not in allowed:
            raise ValueError(f"Cannot sort by '{field}'")
        return cls(field, direction)


@dataclass(frozen=True, slots=True)
class Paging:
    """1-based page number and page size; ``offset`` / ``limit`` for the repository."""

    page: int = 1
    page_size: int = 50

    MAX_PAGE_SIZE: ClassVar[int] = 500

    def __post_init__(self) -> None:
        if self.page < 1:
            raise ValueError("page must be >= 1")
        if not 1 <= self.page_size <= self.MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {self.MAX_PAGE_SIZE}")

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        return self.page_size


@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    """One page of results plus the total count across all pages."""

    items: Sequence[T]
    total: int
    paging: Paging

    @property
    def pages(self) -> int:
        return max(1, math.ceil(self.total / self.paging.page_size))

    @property
    def has_next(self) -> bool:
        return self.paging.page < self.pages
