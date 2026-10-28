# ADR 0003: Contact search strategy

Status: Accepted (2026-10)

## Context

The contact list searches name, e-mail, company, title and phone in one box. Searching several columns
with `OR ... LIKE` cannot use indexes and gets slower with each column, and phone numbers are stored with
arbitrary formatting.

## Decision

- `prospects.search_text` is a STORED generated column (`SEARCH_TEXT_SQL` in `app/models/prospect.py`,
  `VARCHAR(1100)`): the lower-cased searchable fields plus the phone digits, maintained by MySQL on every
  write.
- `contact_query_service.search_condition` matches `search_text LIKE '%term%'` (with `%`, `_` and `\`
  escaped) and, when the query looks like a phone number, also the digits-only form.
- The query is always combined with the tenant / hierarchy filter (`visible_contacts`), which uses
  `ix_prospects_tenant_live_*` indexes, so the scan is bounded by one workspace's live contacts.

## Consequences

- Simple and exact (substring match, any position, any language) with one column to scan; good for
  workspaces up to tens of thousands of contacts.
- A leading-wildcard LIKE cannot use a B-tree index: cost grows linearly with the workspace's contact
  count. **Planned at about 100k contacts per workspace**: add a `FULLTEXT INDEX ... (search_text) WITH
  PARSER ngram` (`ngram_token_size=2`, which also suits names and CJK text) and switch
  `search_condition` to `MATCH(search_text) AGAINST(:q IN BOOLEAN MODE)`, keeping the LIKE path for
  1-character queries and phone digits. This is a migration plus one function change; callers are
  unaffected. *Tried and reverted (2026-10):* 0030 added that index as a pre-filter in front of the LIKE;
  under concurrent inserts from another workspace some committed contacts never matched it, so 0034 dropped
  it and the LIKE scan stays. A replacement must be proven against that case
  (`tests/test_final_contact_search.py`) first.
- Changing the searchable fields means changing `SEARCH_TEXT_SQL` in a migration (generated column
  rebuild).
