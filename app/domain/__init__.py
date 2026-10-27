"""
Domain / query layer (ADR 0009, docs/adr/0009-layered-architecture.md).

May contain: typed query and filter objects (parameter objects such as ``Paging`` / ``Sort``), value
objects, enums shared across layers, and pure business rules (functions of their arguments only).

May not contain: I/O of any kind -- no SQLAlchemy, no ORM models (``app.models``), no FastAPI, no
repositories, services or routers, no network, file or clock access hidden inside a function.
Not even ``app.core.errors`` (it imports FastAPI for its handler): invalid values raise ``ValueError``.

Enforced by the ``domain-pure`` contract in ``.importlinter`` (indirect imports count too).
Unit tests: plain function / object tests, no fakes needed.
"""
