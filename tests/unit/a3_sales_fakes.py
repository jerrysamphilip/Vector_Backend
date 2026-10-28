"""In-memory fakes of the sales repositories, unit of work and collaborators (A3 service tests)."""
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from app.domain.sales import OwnerScope


class FakeSettingsRepo:
    def __init__(self, stored: Optional[dict] = None):
        self.doc = stored

    def stored(self, tenant_id):
        return self.doc

    def save(self, tenant_id, settings):
        self.doc = settings


class FakeStages:
    def __init__(self, stages: List[Any]):
        self.rows = list(stages)

    def exists(self, tenant_id):
        return bool(self.rows)

    def list(self, tenant_id, include_inactive):
        rows = [s for s in self.rows if include_inactive or s.active]
        return sorted(rows, key=lambda s: (s.sort_order, s.name))

    def get(self, tenant_id, stage_id):
        return next((s for s in self.rows if s.stage_id == stage_id and s.tenant_id == tenant_id), None)

    def by_ids(self, tenant_id, ids):
        return {s.stage_id: s for s in self.rows if s.stage_id in set(ids)}

    def add(self, entity):
        self.rows.append(entity)
        return entity


class _Entities:
    """get / add / flush / claim_version over a dict keyed by the id attribute."""

    key = "id"

    def __init__(self, rows=()):
        self.rows: Dict[str, Any] = {getattr(r, self.key): r for r in rows}
        self.claimed: List[tuple] = []

    def get(self, tenant_id, entity_id):
        r = self.rows.get(entity_id)
        return r if r is not None and r.tenant_id == tenant_id else None

    def get_visible(self, tenant_id, scope: OwnerScope, entity_id):
        r = self.get(tenant_id, entity_id)
        return r if r is not None and scope.allows(r.owner_id) else None

    def add(self, entity):
        if getattr(entity, self.key, None) is None:
            setattr(entity, self.key, f"{self.key}-{len(self.rows) + 1}")
        self.rows[getattr(entity, self.key)] = entity
        return entity

    def flush(self):
        pass

    def claim_version(self, entity, expected, record):
        self.claimed.append((record, expected))
        entity.row_version = (entity.row_version or 1) + 1
        return entity.row_version


class FakeLeads(_Entities):
    key = "lead_id"

    def open_for_prospect(self, tenant_id, prospect_id):
        return next((r for r in self.rows.values() if r.prospect_id == prospect_id
                     and r.stage in ("NEW", "CONTACTED", "ENGAGED", "SQL")), None)

    def due_for_recycle(self, now):
        return [r for r in self.rows.values() if r.stage == "DISQUALIFIED" and r.recycle_at and r.recycle_at <= now]

    def other_open_lead_exists(self, prospect_id, lead_id):
        return any(r.prospect_id == prospect_id and r.lead_id != lead_id and r.stage in ("NEW", "SQL")
                   for r in self.rows.values())


class FakeOpps(_Entities):
    key = "opportunity_id"

    def __init__(self, rows=(), touches=None):
        super().__init__(rows)
        self.touches = touches or {}
        self.page_calls: List[Any] = []

    def last_activity(self, ids):
        return {i: self.touches[i] for i in ids if i in self.touches}

    def won_exists_for_account(self, tenant_id, account_id):
        return False

    def tenants_with_open_deals(self):
        return {r.tenant_id for r in self.rows.values() if r.status == "OPEN"}

    def stale_candidates(self, tenant_id, cutoff, today):
        return [r for r in self.rows.values() if r.tenant_id == tenant_id and r.status == "OPEN"]

    def page(self, tenant_id, scope, flt, query):
        from app.domain.paging import Page
        self.page_calls.append((scope, flt, query))
        rows = [r for r in self.rows.values() if scope.allows(r.owner_id)]
        return Page(rows, len(rows), query.paging), sum(float(r.amount or 0) for r in rows)


class FakeLookups:
    def __init__(self, users=None, prospects=None):
        self.users = {u.user_id: u for u in (users or [])}
        self.prospects_ = {p.prospect_id: p for p in (prospects or [])}
        self.added: List[Any] = []

    def user_names(self, ids):
        return {i: f"{self.users[i].first_name} {self.users[i].last_name}".strip()
                for i in ids if i in self.users}

    def user_exists(self, tenant_id, user_id):
        return user_id in self.users and self.users[user_id].tenant_id == tenant_id

    def user(self, tenant_id, user_id):
        u = self.users.get(user_id)
        return u if u and u.tenant_id == tenant_id else None

    def active_user_ids(self, tenant_id):
        return {u.user_id for u in self.users.values() if u.tenant_id == tenant_id}

    def prospect(self, prospect_id):
        return self.prospects_.get(prospect_id)

    def live_prospect(self, tenant_id, prospect_id):
        p = self.prospects_.get(prospect_id)
        return p if p and p.tenant_id == tenant_id else None

    tenant_prospect = live_prospect

    def prospects(self, ids):
        return {i: self.prospects_[i] for i in ids if i in self.prospects_}

    def account_names(self, ids):
        return {}

    def campaign_names(self, ids):
        return {}

    def last_campaign_emailed(self, prospect_id, before):
        return None

    def history(self, object_type, object_id, limit):
        return []

    def add(self, entity):
        self.added.append(entity)
        return entity

    def flush(self):
        pass


class FakeQuotes:
    def __init__(self, lines=None, owners=None, managers=None, admins=()):
        self.lines_ = lines or {}
        self.owners = owners or {}
        self.managers_ = managers or {}
        self.admins = list(admins)

    def line_tuples(self, proposal_id):
        return self.lines_.get(proposal_id, [])

    def deal_owner_id(self, opportunity_id):
        return self.owners.get(opportunity_id)

    def managers(self, tenant_id):
        return dict(self.managers_)

    def active_user_ids(self, tenant_id):
        return set(self.managers_) | set(self.managers_.values()) - {None} | set(self.admins)

    def active_users_with_roles(self, tenant_id, roles):
        return list(self.admins)


class FakeCollaborators:
    def __init__(self, scope: OwnerScope = OwnerScope(), team=None, managers=None):
        self.scope = scope
        self.team = team or {}
        self.managers = managers or {}
        self.changes: List[tuple] = []
        self.notifications: List[tuple] = []

    def owner_scope(self, user):
        return self.scope

    def visible_user_ids(self, user):
        return None if self.scope.owner_ids is None else set(self.scope.owner_ids)

    def team_user_ids(self, user):
        return set(self.team.get(user.user_id, {user.user_id}))

    def can_see_owner(self, user, owner_id):
        return self.scope.allows(owner_id)

    def can_access_contact(self, user, prospect):
        return self.scope.allows(prospect.owner_id)

    def sees_everything(self, user):
        return self.scope.owner_ids is None

    def record_changes(self, tenant_id, object_type, object_id, before, after, user_id, source="UI"):
        self.changes.append((object_type, object_id, before, after, source))
        return 1

    def notify(self, tenant_id, user_ids, kind, title, body=None, link=None, dedupe_key=None, dedupe_days=7):
        ids = [u for u in dict.fromkeys(user_ids) if u]
        self.notifications.append((kind, ids, title, body, link))
        return len(ids)

    def manager_of(self, user_id):
        return self.managers.get(user_id)

    def resolve_company(self, tenant_id, email, company_name):
        return None


class FakeUow:
    def __init__(self, repos):
        self.repos = repos
        self.session = None
        self.commits = 0
        self.rollbacks = 0
        self.active = False

    def __enter__(self):
        assert not self.active
        self.active = True
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.rollbacks += 1
        self.active = False

    def commit(self):
        self.commits += 1


def make_repos(**kw):
    defaults = dict(settings=FakeSettingsRepo(), stages=FakeStages([]), leads=FakeLeads(), opportunities=FakeOpps(),
                    lookups=FakeLookups(), proposals=None, quotes=FakeQuotes())
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def user(user_id="u1", role="ADMIN", tenant_id="t1", sales_level=None, **kw):
    return SimpleNamespace(user_id=user_id, role=role, tenant_id=tenant_id, sales_level=sales_level,
                           first_name=kw.get("first_name", "Ann"), last_name=kw.get("last_name", "Lee"),
                           email=f"{user_id}@x.test", full_name=kw.get("full_name"), manager_id=kw.get("manager_id"))


def sales_stage(stage_id, prob, won=False, lost=False, order=0, tenant_id="t1"):
    return SimpleNamespace(stage_id=stage_id, tenant_id=tenant_id, name=stage_id.title(), probability=prob,
                           sort_order=order, is_won=won, is_lost=lost, active=True)


def deal(opportunity_id="o1", owner_id="u2", stage_id="open", status="OPEN", **kw):
    base = dict(opportunity_id=opportunity_id, tenant_id="t1", name="Big deal", account_id=None, prospect_id=None,
                lead_id=None, owner_id=owner_id, stage_id=stage_id, amount=None, close_date=None, client_type="NEW",
                status=status, closed_reason=None, next_step=None, description=None, forecast_category=None,
                forecast_category_manual=False, campaign_id=None, closed_at=None, created_at=datetime(2026, 1, 1),
                updated_at=datetime(2026, 1, 1), row_version=1)
    base.update(kw)
    return SimpleNamespace(**base)
