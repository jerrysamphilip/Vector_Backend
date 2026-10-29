"""
g1a: campaign wizard drafts are owner-scoped (BR-SH-02). A draft looked up by id is visible to its creator
and to whoever sees the creator's records (admins); a colleague's draft is a 404 for anyone else, for read
and delete alike. Fake repository + fake unit of work, no DB.
"""
from types import SimpleNamespace

import pytest

from app.core.errors import NotFoundError
from app.services.campaign_draft_service import CampaignDraftService

TENANT = "t-1"


def _user(user_id, role="AGENT", **extra):
    values = dict(user_id=user_id, tenant_id=TENANT, role=role, sales_level=None, custom_permissions=None)
    values.update(extra)
    return SimpleNamespace(**values)


class FakeDrafts:
    def __init__(self, rows):
        self.rows = {r.draft_id: r for r in rows}
        self.deleted = []

    def get(self, tenant_id, draft_id):
        row = self.rows.get(draft_id)
        return row if row is not None and row.tenant_id == tenant_id else None

    def delete(self, draft):
        self.deleted.append(draft.draft_id)
        self.rows.pop(draft.draft_id, None)


class FakeUow:
    def __init__(self, drafts):
        self.drafts = drafts
        self.session = object()  # visibility_scope needs no session for these users
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def commit(self):
        self.commits += 1


@pytest.fixture
def service():
    drafts = FakeDrafts([
        SimpleNamespace(draft_id="d-ann", tenant_id=TENANT, user_id="ann"),
        SimpleNamespace(draft_id="d-other", tenant_id="t-2", user_id="zed"),
    ])
    return CampaignDraftService(uow=FakeUow(drafts))


def test_g1a_owner_reads_and_deletes_own_draft(service):
    ann = _user("ann")
    assert service.get(ann, "d-ann").draft_id == "d-ann"
    assert service.delete(ann, "d-ann") == {"status": "deleted", "draft_id": "d-ann"}
    assert service.uow.drafts.deleted == ["d-ann"]


@pytest.mark.parametrize("role", ["AGENT", "MANAGER"])
def test_g1a_colleague_gets_404_for_someone_elses_draft(service, role):
    bob = _user("bob", role)
    with pytest.raises(NotFoundError):
        service.get(bob, "d-ann")
    with pytest.raises(NotFoundError):
        service.delete(bob, "d-ann")
    assert service.uow.drafts.deleted == []
    assert service.uow.commits == 0


@pytest.mark.parametrize("role", ["ADMIN", "SUPER_ADMIN"])
def test_g1a_admins_read_and_delete_others_drafts(service, role):
    admin = _user("adm", role)
    assert service.get(admin, "d-ann").user_id == "ann"
    service.delete(admin, "d-ann")
    assert service.uow.drafts.deleted == ["d-ann"]


def test_g1a_other_tenant_draft_is_404_even_for_admin(service):
    with pytest.raises(NotFoundError):
        service.get(_user("adm", "SUPER_ADMIN"), "d-other")
