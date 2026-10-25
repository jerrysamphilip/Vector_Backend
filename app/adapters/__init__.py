"""
Adapter layer (ADR 0009, docs/adr/0009-layered-architecture.md).

May contain: clients for external systems -- SES/SNS, IMAP/SMTP, OpenAI, file storage, Microsoft 365
-- each behind a ``typing.Protocol`` that services depend on, plus the FastAPI / worker providers that
hand out the real implementation.

May not contain: business rules, or imports of ``app.services`` / ``app.routers`` (services call
adapters, never the other way round).

Enforced by the ``adapters-no-upper`` contract in ``.importlinter``.
Unit tests: services get fake adapters (via ``dependency_overrides`` or constructor arguments, not
module patching); the real adapters are exercised in the functional / live layers.
"""
