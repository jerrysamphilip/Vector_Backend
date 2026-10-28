"""
CRM KPI measures (BRD v2.0 section 8; chunk A6): single grouped SQL queries, so they stay cheap on
100k-contact workspaces. The KPI rules (percentages, week windows, caching of the duplicate count)
are in app/services/crm_kpis.py.

``owners`` limits a measure to records owned by those users (None = all, unowned included; an empty
set matches nothing). Statements are built from fixed SQL plus bound parameters only: the owner
column names are constants of this module and the integer limits are formatted with ``int()``.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Iterable
from datetime import date, datetime
from typing import Any, Optional, Protocol

from sqlalchemy import TextClause, bindparam, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.models.email_message import EmailEvent
from app.models.user import User
from app.models.user_activity import UserActivityDay
from app.repositories.base import TenantId

logger = logging.getLogger("app.services.crm_kpis")   # the logger these warnings have always used
__all__ = ["KpiRepository", "Session", "SqlAlchemyKpiRepository"]

OwnerSet = Optional[Collection[str]]
_OWNER_COLUMNS = frozenset({"owner_id", "l.owner_id", "p.owner_id"})


def _owner_filter(owners: OwnerSet, column: str) -> tuple[str, dict]:
    if column not in _OWNER_COLUMNS:
        raise ValueError(f"not an owner column: {column}")
    if owners is None:
        return "", {}
    return f" AND {column} IN :owners", {"owners": sorted(owners) or ["-"]}


def _expanding(query: TextClause, owners: OwnerSet) -> TextClause:
    return query if owners is None else query.bindparams(bindparam("owners", expanding=True))


def _duplicate_sql(owner_sql: str, limit_ms: int) -> TextClause:
    norm = "REGEXP_REPLACE(LOWER(COALESCE({0}, '')), '[^a-z0-9]', '')"
    email = "LOWER(email)"
    local = f"SUBSTRING_INDEX(SUBSTRING_INDEX({email}, '@', 1), '+', 1)"
    domain = f"IF(LOCATE('@', {email}) = 0, '', SUBSTRING({email}, LOCATE('@', {email}) + 1))"
    personal = f"({domain} = '' OR SUBSTRING_INDEX({domain}, '.', 1) IN :labels)"
    # LIMIT keeps MySQL from merging s into every branch below (which would rerun the regexes per
    # branch): s is materialised once, then each match key is grouped once.
    return text(f"""
        WITH s AS (
            SELECT prospect_id,
                   CONCAT(IF({personal}, REPLACE({local}, '.', ''), {local}), '@', {domain}) AS ekey,
                   {norm.format('first_name')} AS f, {norm.format('last_name')} AS l,
                   COALESCE(NULLIF(account_id, ''), {norm.format('company_name')}) AS c,
                   REGEXP_REPLACE(COALESCE(phone, ''), '[^0-9]', '') AS p1,
                   REGEXP_REPLACE(COALESCE(mobile_phone, ''), '[^0-9]', '') AS p2
            FROM prospects
            WHERE tenant_id = :tenant AND deleted_at IS NULL{owner_sql}
            LIMIT 18446744073709551615
        ),
        k AS (
            SELECT prospect_id, CONCAT('e|', ekey) AS k FROM s
            UNION ALL SELECT prospect_id, CONCAT('n|', f, '|', l, '|', c) FROM s WHERE f <> '' AND l <> '' AND c <> ''
            UNION ALL SELECT prospect_id, CONCAT('i|', LEFT(f, 1), '|', l, '|', c) FROM s
                      WHERE CHAR_LENGTH(f) > 1 AND l <> '' AND c <> ''
            UNION ALL SELECT prospect_id, CONCAT('p|', RIGHT(p1, 10)) FROM s WHERE CHAR_LENGTH(p1) >= 7
            UNION ALL SELECT prospect_id, CONCAT('p|', RIGHT(p2, 10)) FROM s WHERE CHAR_LENGTH(p2) >= 7
        ),
        d AS (SELECT k FROM k GROUP BY k HAVING COUNT(DISTINCT prospect_id) > 1)
        SELECT /*+ MAX_EXECUTION_TIME({int(limit_ms)}) */
               (SELECT COUNT(DISTINCT k.prospect_id) FROM k JOIN d ON d.k = k.k), (SELECT COUNT(*) FROM s)
    """).bindparams(bindparam("labels", expanding=True))


class KpiRepository(Protocol):
    def contacts_fingerprint(self, tenant_id: TenantId, owners: OwnerSet) -> tuple: ...
    def duplicate_counts(self, tenant_id: TenantId, owners: OwnerSet, personal_labels: list[str],
                         limit_ms: int) -> Optional[tuple[int, int]]: ...
    def reply_to_lead_counts(self, tenant_id: TenantId, owners: OwnerSet, start: datetime, end: datetime,
                             source: str, window_days: int) -> tuple[int, int]: ...
    def lead_to_sql_counts(self, tenant_id: TenantId, owners: OwnerSet, start: datetime,
                           end: datetime) -> tuple[int, int]: ...
    def licensed_users(self, tenant_id: TenantId, members: OwnerSet = None) -> set[str]: ...
    def activity_days(self, tenant_id: TenantId, first: date, end: date) -> list[tuple[str, date]]: ...


class SqlAlchemyKpiRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def contacts_fingerprint(self, tenant_id: TenantId, owners: OwnerSet) -> tuple:
        """(live contacts, latest update) in scope: unchanged contacts keep a cached duplicate count."""
        owner_sql, params = _owner_filter(owners, "owner_id")
        q = _expanding(text(f"""SELECT COUNT(*), MAX(updated_at) FROM prospects
                                WHERE tenant_id = :tenant AND deleted_at IS NULL{owner_sql}"""), owners)
        return tuple(self._session.execute(q, {"tenant": tenant_id, **params}).one())

    def duplicate_counts(self, tenant_id: TenantId, owners: OwnerSet, personal_labels: list[str],
                         limit_ms: int) -> Optional[tuple[int, int]]:
        """(contacts in a duplicate group, live contacts); None when MySQL stopped the query at limit_ms."""
        owner_sql, params = _owner_filter(owners, "owner_id")
        query = _expanding(_duplicate_sql(owner_sql, limit_ms), owners)
        bind: Any = self._session.get_bind()
        try:
            # Own connection: a query stopped by the time limit must not spoil the request's session
            with bind.connect() as conn:
                row = conn.execute(query, {"tenant": tenant_id, "labels": personal_labels, **params}).one()
        except DBAPIError as exc:
            logger.warning("[KPI] Duplicate contact count for %s stopped: %s", tenant_id, exc.orig)
            return None
        return int(row[0] or 0), int(row[1] or 0)

    def reply_to_lead_counts(self, tenant_id: TenantId, owners: OwnerSet, start: datetime, end: datetime,
                             source: str, window_days: int) -> tuple[int, int]:
        """(leads created from replies in [start, end), contacts with a positive reply in [start, end)).
        Leads are scoped by lead owner, positive replies by contact owner."""
        lead_owner_sql, lp = _owner_filter(owners, "l.owner_id")
        contact_owner_sql, cp = _owner_filter(owners, "p.owner_id")
        leads = _expanding(text(f"""
            SELECT COUNT(*) FROM leads l
            WHERE l.tenant_id = :tenant AND l.created_at >= :start AND l.created_at < :end{lead_owner_sql}
              AND (l.source = :source OR EXISTS (
                    SELECT 1 FROM email_messages m JOIN email_events e ON e.message_id = m.message_id
                    WHERE m.prospect_id = l.prospect_id AND e.event_type = :positive
                      AND e.event_time <= l.created_at
                      AND e.event_time >= l.created_at - INTERVAL {int(window_days)} DAY))
        """), owners)
        positives = _expanding(text(f"""
            SELECT COUNT(DISTINCT m.prospect_id) FROM email_events e
            JOIN email_messages m ON m.message_id = e.message_id
            JOIN prospects p ON p.prospect_id = m.prospect_id
            WHERE p.tenant_id = :tenant AND e.event_type = :positive
              AND e.event_time >= :start AND e.event_time < :end{contact_owner_sql}
        """), owners)
        common = {"tenant": tenant_id, "start": start, "end": end, "positive": EmailEvent.EVENT_POSITIVE_REPLY}
        n_leads = self._session.execute(leads, {**common, "source": source, **lp}).scalar() or 0
        n_pos = self._session.execute(positives, {**common, **cp}).scalar() or 0
        return int(n_leads), int(n_pos)

    def lead_to_sql_counts(self, tenant_id: TenantId, owners: OwnerSet, start: datetime,
                           end: datetime) -> tuple[int, int]:
        """(leads created in [start, end) that reached SQL, leads created in [start, end))."""
        owner_sql, params = _owner_filter(owners, "owner_id")
        q = _expanding(text(f"""SELECT COALESCE(SUM(qualified_at IS NOT NULL), 0), COUNT(*) FROM leads
                                WHERE tenant_id = :tenant AND created_at >= :start AND created_at < :end{owner_sql}"""),
                       owners)
        row = self._session.execute(q, {"tenant": tenant_id, "start": start, "end": end, **params}).one()
        return int(row[0] or 0), int(row[1] or 0)

    def licensed_users(self, tenant_id: TenantId, members: OwnerSet = None) -> set[str]:
        """Licensed seats: the workspace's ACTIVE users (platform admins have no workspace)."""
        q = self._session.query(User.user_id).filter(User.tenant_id == tenant_id, User.status == "ACTIVE",
                                                     User.role != "PLATFORM_ADMIN")
        if members is not None:
            q = q.filter(User.user_id.in_(members or {"-"}))
        return {u for (u,) in q}

    def activity_days(self, tenant_id: TenantId, first: date, end: date) -> list[tuple[str, date]]:
        rows: Iterable[Any] = self._session.query(UserActivityDay.user_id, UserActivityDay.day).filter(
            UserActivityDay.tenant_id == tenant_id, UserActivityDay.day >= first, UserActivityDay.day < end).all()
        return [(uid, day) for uid, day in rows]
