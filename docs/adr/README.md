# Architecture decision records

Short records of decisions that shape the backend: context, decision, consequences. Add a new file
(`NNNN-title.md`) for a new decision; supersede an old one by adding a record that links to it rather
than rewriting history. Overview: [../ARCHITECTURE.md](../ARCHITECTURE.md).

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-layering-and-domain-errors.md) | Layering (thin routers, services own logic and DB) and domain errors | Accepted |
| [0002](0002-single-process-api-in-memory-rate-limiter.md) | Single API replica with an in-memory rate limiter; Redis when scaling out | Accepted |
| [0003](0003-contact-search-strategy.md) | Contact search: LIKE on a generated `search_text` column; FULLTEXT ngram at 100k+ | Accepted |
| [0004](0004-background-jobs-named-locks.md) | Background loops coordinated by MySQL named locks | Accepted |
| [0005](0005-webhook-outbox.md) | Outbound webhooks through a transactional outbox | Accepted |
| [0006](0006-test-framework-layers.md) | Four test layers behind one runner | Accepted |
| [0007](0007-session-timeouts.md) | Session idle timeout, absolute cap and refresh-token reuse detection | Accepted |
| [0008](0008-internal-transport.md) | Internal transport: NetworkPolicies now, MySQL over verified TLS as the target | Proposed |
| [0009](0009-layered-architecture.md) | Layered architecture: route / service / domain / repository / adapter / UI, service-owned unit of work, import contracts with a shrinking baseline | Accepted |
