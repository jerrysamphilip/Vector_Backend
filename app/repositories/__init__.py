"""
Repository layer (ADR 0009, docs/adr/0009-layered-architecture.md).

May contain: all SQL and ORM mapping, one module per aggregate (``contacts.py``, ``campaigns.py``, ...).
Every read and write is scoped to a tenant. Repositories return ORM objects or domain values and
accept domain parameter objects (``app.domain``) for filters, sorting and paging.

May not contain: business rules, permission decisions, FastAPI types, or calls into ``app.services``
/ ``app.routers``. Repositories never commit: the service's unit of work
(``app.repositories.base.UnitOfWork``) owns the transaction.

Enforced by the ``repositories-no-upper`` contract in ``.importlinter``.
Unit tests: services use fake repositories; the repositories themselves are tested against MySQL in
the functional layer.
"""
