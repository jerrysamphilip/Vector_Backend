"""Query-count helper for fixed-budget tests (T-5): counts the SQL statements the app sends to MySQL.

    with count_queries() as q:
        client.get("/contacts", headers=h)
    assert q.count <= 12, q.statements

A SQLAlchemy ``before_cursor_execute`` listener on the shared engine sees every statement, whichever
session (request, auth dependency, background helper) runs it.
"""
from contextlib import contextmanager
from typing import Iterator, List

from sqlalchemy import event

from app.core.database import engine


class QueryCounter:
    def __init__(self) -> None:
        self.statements: List[str] = []

    @property
    def count(self) -> int:
        return len(self.statements)


@contextmanager
def count_queries() -> Iterator[QueryCounter]:
    counter = QueryCounter()

    def _before(conn, cursor, statement, parameters, context, executemany):
        counter.statements.append(statement)

    event.listen(engine, "before_cursor_execute", _before)
    try:
        yield counter
    finally:
        event.remove(engine, "before_cursor_execute", _before)


def queries_for(call) -> QueryCounter:
    """Runs call() (e.g. a TestClient request that must succeed) and returns its statements."""
    with count_queries() as counter:
        response = call()
    assert response.status_code == 200, response.text
    return counter
