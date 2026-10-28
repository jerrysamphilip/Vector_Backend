"""Drop the FULLTEXT ngram index on prospects.search_text (D7 reverted)

Revision ID: 0034_drop_contact_fulltext
Revises: 0033_prospect_merged_into_index
Create Date: 2026-10-28

0030_contact_fulltext added ``ft_prospects_search_text`` (FULLTEXT WITH PARSER ngram) and the contact list /
global search pre-filtered with ``MATCH (search_text) AGAINST (...)`` before the exact LIKE. While another
workspace was inserting contacts at the same time, some committed contacts never matched the MATCH and so
never became searchable (live regression; reproduced locally, not fixable from the query side). The search is the plain ``search_text LIKE '%term%'`` scan again
(app/repositories/contacts.py), which is always correct, and this revision drops the index.

Metadata-quick: dropping a FULLTEXT index does not rebuild the table (the hidden FTS_DOC_ID column InnoDB
added with the first FULLTEXT index stays until the next table rebuild). No data changes.

Idempotent: the index is dropped only when information_schema lists it (a fresh build made by create_all
from the models never has it).

Downgrade recreates the index exactly as 0030 did: ``ADD FULLTEXT INDEX ... WITH PARSER ngram`` with
``innodb_ft_enable_stopword = OFF`` in that session (the code of this revision no longer reads it; the
0030 code does). It rebuilds prospects and blocks writes meanwhile (about 7 s at 100k contacts).
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0034_drop_contact_fulltext"
down_revision: Union[str, Sequence[str], None] = "0033_prospect_merged_into_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "prospects"
COLUMN = "search_text"
INDEX = "ft_prospects_search_text"


def _has_index() -> bool:
    return bool(op.get_bind().execute(text(
        "SELECT COUNT(*) FROM information_schema.statistics "
        "WHERE table_schema = DATABASE() AND table_name = :t AND index_name = :i"),
        {"t": TABLE, "i": INDEX}).scalar())


def upgrade() -> None:
    if not _has_index():
        return
    op.execute(f"DROP INDEX {INDEX} ON {TABLE}")


def downgrade() -> None:
    if _has_index():
        return
    op.get_bind().execute(text("SET SESSION innodb_ft_enable_stopword = OFF"))
    op.execute(f"ALTER TABLE {TABLE} ADD FULLTEXT INDEX {INDEX} ({COLUMN}) WITH PARSER ngram")
