# Layer pattern (reference implementation: contacts)

How an aggregate looks once it follows [ADR 0009](adr/0009-layered-architecture.md). Contacts (chunk L1) is the
reference; sales (A3) and reports (A6) follow it. Copy it for every remaining aggregate in wave 3.

## Layout per aggregate `<agg>`

| File | Holds | Rules |
|---|---|---|
| `app/domain/<agg>.py` | Frozen parameter objects: `XFilter` (raw request params), `XCriteria` (validated, resolved for one user, incl. `VisibilityScope` and a parsed filter tree), `Sort`/`Paging` from `app/domain/paging.py`, pure rules | No I/O. Errors are `ValueError` subclasses |
| `app/repositories/<agg>.py` | `XRepository` (Protocol) + `SqlAlchemyXRepository`; `XUnitOfWork` (Protocol: `.x`, `.session`, `commit`, `rollback`, `savepoint`) + `SqlAlchemyXUnitOfWork.for_session(db)`; `borrow(db)` for access inside a caller's transaction | All SQL. `tenant_id` on every tenant entry point. Takes domain objects, returns ORM objects or `Page[T]`. Never commits |
| `app/services/<agg>_*.py` | `XService(uow)` with `for_session(db)`; one public method per route, body `with self.uow as uow: ... uow.commit()`; guards (403) before the `with` | No sqlalchemy / fastapi imports; raise `app.core.errors`. Legacy `fn(db, ...)` helpers used by other aggregates stay as thin wrappers over `borrow(db)` |
| `app/routers/<agg>_router.py` | Provider `def x_service(db=Depends(get_db)) -> XService`; handler parses params → domain objects → ONE service call → dict/model | No sqlalchemy, `app.models` or `app.repositories` imports. Keep `Query(...)` declarations so OpenAPI is unchanged |
| `app/schemas/<agg>_schema.py` | Request/response models (moved out of routers) | |
| `app/models/<model>.py` | `Mapped[T] = mapped_column(..., nullable=<explicit>)` | Prove identical DDL with a `CreateTable(mysql)` snapshot test taken before the change |

Naming: `XFilter` / `XCriteria` / `X_SORT_FIELDS`; `XRepository` / `SqlAlchemyXRepository`; `XUnitOfWork`;
`XService.for_session`; providers `x_service`.

Transactions: the service owns the unit of work (commit inside the `with`; an exception rolls back);
repositories stage and flush only; bridge to not-yet-migrated code through `uow.session`.

## Tests per layer

| File | What |
|---|---|
| `tests/unit/test_<tag>_domain_*.py` | Pure value and function tests, no fakes |
| `tests/unit/test_<tag>_*_services.py` | Fake repository (dict-backed, only the methods used) + fake unit of work (counts enter / commit / rollback) |
| `tests/unit/test_<tag>_*_routers.py` | `FastAPI()` + domain error handler + the router; `dependency_overrides` for the user and providers; assert status, body and the domain objects passed down |
| `tests/test_<tag>_*_repository.py` | MySQL: filters, scope, paging, sort, tenant isolation, writes |

A refactor chunk snapshots `app.openapi()` for the paths it touches before and after (must be identical), and a
report or query-heavy chunk adds a golden-output test first (e.g. `tests/test_a6_report_outputs.py`).
