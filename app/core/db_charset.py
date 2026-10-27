"""The one MySQL character set and collation used by every table, column and connection (chunk D1).

Set explicitly rather than taken from the server or schema default: a schema created as
utf8mb4_unicode_ci (an RDS or older default) would otherwise mix collations, and string foreign keys
and joins across them fail (MySQL 3780 / 1267). Migration 0017 converts existing databases.
"""

MYSQL_ENGINE = "InnoDB"
MYSQL_CHARSET = "utf8mb4"
MYSQL_COLLATION = "utf8mb4_0900_ai_ci"
