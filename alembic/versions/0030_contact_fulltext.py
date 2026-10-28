"""Contact search on a FULLTEXT ngram index over prospects.search_text (D7)

Revision ID: 0030_contact_fulltext
Revises: 0029_status_checks
Create Date: 2026-10-28

The contact list / global search matched ``search_text LIKE '%term%'`` (the STORED generated column that
holds the lower-cased name, email, company, title and phone digits), which reads every live contact of the
tenant. This revision adds ``ft_prospects_search_text``, a FULLTEXT index on that column WITH PARSER ngram.
app/repositories/contacts.py now pre-filters with ``MATCH (search_text) AGAINST ('+"frag" ...' IN BOOLEAN
MODE)`` and keeps the LIKE as the exact condition, so results are unchanged; terms shorter than one token
still use the LIKE alone.

ngram_token_size: the server default 2 is assumed (a read-only server variable, set in my.cnf or the RDS
parameter group; 2 lets two-letter terms such as "li" use the index). A different value only logs a warning
here: with a larger size, two-character fragments would find nothing, so keep it at 2 or change
FULLTEXT_TOKEN_SIZE in the repository and rebuild the index.

Stopwords: the index is built with ``innodb_ft_enable_stopword = OFF`` (this session, and every Alembic /
app session via app.core.database.MYSQL_SESSION_INIT_COMMAND). With the default InnoDB stopword list the
ngram parser drops every token containing "a", "i", "to", "in", ..., and "maria" would match nothing. The
setting is read when an index is (re)built, so a manual ``OPTIMIZE TABLE prospects`` or a table-rebuilding
ALTER run from a plain mysql client must set it too (``SET SESSION innodb_ft_enable_stopword = OFF``).

Idempotent: the index is created only when information_schema does not list it (a fresh build made by
create_all from the models already has it). Adding the first FULLTEXT index to a table rebuilds it (InnoDB
adds the hidden FTS_DOC_ID column) and blocks writes to prospects meanwhile (about 7 s at 100k contacts
locally); run it in a quiet period on a large database. No data changes.

Downgrade drops the index (the hidden FTS_DOC_ID column stays until the next table rebuild).
"""
import logging
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "0030_contact_fulltext"
down_revision: Union[str, Sequence[str], None] = "0029_status_checks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

TABLE = "prospects"
COLUMN = "search_text"
INDEX = "ft_prospects_search_text"
TOKEN_SIZE = 2  # literal copy of app/repositories/contacts.py FULLTEXT_TOKEN_SIZE


def _has_index() -> bool:
    return bool(op.get_bind().execute(text(
        "SELECT COUNT(*) FROM information_schema.statistics "
        "WHERE table_schema = DATABASE() AND table_name = :t AND index_name = :i"),
        {"t": TABLE, "i": INDEX}).scalar())


def _has_column() -> bool:
    return bool(op.get_bind().execute(text(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"),
        {"t": TABLE, "c": COLUMN}).scalar())


def upgrade() -> None:
    if _has_index():
        return
    if not _has_column():
        logger.warning("0030_contact_fulltext: %s.%s is missing; the FULLTEXT index was NOT created and "
                       "contact search will fail until it is. Check earlier revisions.", TABLE, COLUMN)
        return
    bind = op.get_bind()
    token_size = int(bind.execute(text("SELECT @@ngram_token_size")).scalar() or TOKEN_SIZE)
    if token_size != TOKEN_SIZE:
        logger.warning("0030_contact_fulltext: ngram_token_size is %d, the contact search assumes %d; "
                       "searches for terms shorter than %d characters per word may miss contacts. Set "
                       "ngram_token_size=%d on the server and rebuild %s.", token_size, TOKEN_SIZE,
                       token_size, TOKEN_SIZE, INDEX)
    bind.execute(text("SET SESSION innodb_ft_enable_stopword = OFF"))
    op.execute(f"ALTER TABLE {TABLE} ADD FULLTEXT INDEX {INDEX} ({COLUMN}) WITH PARSER ngram")


def downgrade() -> None:
    if _has_index():
        op.execute(f"DROP INDEX {INDEX} ON {TABLE}")
