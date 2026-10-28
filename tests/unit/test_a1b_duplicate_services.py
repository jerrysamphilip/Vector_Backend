"""
a1b service layer (C-07..C-11, C-19, F-21): duplicate scan storage rules, dismiss / reopen, merge preview
helpers, merge safety (locks, refusal, log, identities) and the export log, against in-memory fakes.
"""
from contextlib import nullcontext
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.core.errors import BadRequestError, ConflictError, NotFoundError, PermissionDenied
from app.domain.contacts import ContactFilter
from app.domain.duplicates import Candidate
from app.domain.paging import Page, Paging
from app.services import contact_query_service as query
from app.services import contact_service as rules
from app.services import duplicate_scan
from app.services.duplicate_service import DuplicateService, merge_warnings
from app.services.export_log import ExportLogService, record_export


class FakeStore:
    """Just enough of DuplicateRepository."""

    def __init__(self, candidates=(), pairs=()):
        self._candidates, self._pairs = list(candidates), {p.pair_id: p for p in pairs}
        self.locked, self.logs, self.carried, self.resolved, self.deleted = [], [], [], [], []
        self.lock_result = None

    def candidates(self, tenant_id):
        return self._candidates

    def pairs(self, tenant_id):
        return [p for p in self._pairs.values() if p.tenant_id == tenant_id]

    def has_pairs(self, tenant_id):
        return bool(self.pairs(tenant_id))

    def add_pair(self, pair):
        pair.pair_id = pair.pair_id or f"new{len(self._pairs)}"
        self._pairs[pair.pair_id] = pair

    def delete_pairs(self, ids):
        self.deleted.extend(ids)
        for i in ids:
            self._pairs.pop(i, None)
        return len(ids)

    def pair(self, tenant_id, pair_id):
        p = self._pairs.get(pair_id)
        return p if p is not None and p.tenant_id == tenant_id else None

    def lock_contacts(self, tenant_id, ids):
        self.locked.append(list(ids))
        return self.lock_result if self.lock_result is not None else []

    def carry_identities(self, tenant_id, survivor_id, duplicates, keep_email):
        self.carried.append((survivor_id, list(duplicates), keep_email))

    def add_merge_log(self, log):
        self.logs.append(log)

    def resolve_pairs(self, tenant_id, ids, user_id, when):
        self.resolved.append((list(ids), user_id))
        return 0


def pair(pid, a, b, status="OPEN", tenant_id="t1", score=90):
    return SimpleNamespace(pair_id=pid, tenant_id=tenant_id, contact_a_id=a, contact_b_id=b, status=status,
                           score=score, confidence="HIGH", reasons=["x"], last_seen_at=None, resolved_at=None,
                           resolved_by=None, created_at=None)


def user(role="ADMIN", **kw):
    base = dict(role=role, user_id="u1", tenant_id="t1", custom_permissions=None, sales_level=None,
                first_name="Ann", last_name="Lee")
    base.update(kw)
    return SimpleNamespace(**base)


# ── scan storage ───────────────────────────────────────────────────

def test_scan_adds_refreshes_removes_and_never_resuggests_dismissed():
    people = [Candidate("a", "Ann", "Lee", "ann@acme.com", "Acme"), Candidate("b", "Ann", "Lee", "a.lee@acme.com", "Acme"),
              Candidate("c", "Bo", "Ek", "bo@x.io"), Candidate("d", "Bo", "Ek", "BO@x.io"),
              Candidate("e", "Cy", "Fu", "cy@y.io"), Candidate("f", "Cy", "Fu", "cy@y.io")]
    store = FakeStore(people, [pair("p-ab", "a", "b", score=1), pair("p-cd", "c", "d", status="DISMISSED"),
                               pair("p-old", "x", "y"), pair("p-merged", "e", "f", status="MERGED")])
    now = datetime(2026, 10, 27)
    stats = duplicate_scan.scan_tenant(store, "t1", now)
    assert stats["pairs_found"] == 3 and stats["updated"] == 1 and stats["kept_dismissed"] == 1
    assert stats["new"] == 0 and stats["removed"] == 1 and store.deleted == ["p-old"]
    refreshed = store.pair("t1", "p-ab")
    assert refreshed.score > 1 and refreshed.last_seen_at == now and refreshed.status == "OPEN"
    assert store.pair("t1", "p-cd").status == "DISMISSED" and store.pair("t1", "p-merged").status == "MERGED"
    store2 = FakeStore(people[:2])
    assert duplicate_scan.scan_tenant(store2, "t1", now)["new"] == 1
    new = store2.pairs("t1")[0]
    assert (new.contact_a_id, new.contact_b_id, new.status, new.confidence) == ("a", "b", "OPEN", "HIGH")


def test_pair_group_orders_contacts_oldest_first():
    group = duplicate_scan.pair_group(pair("p", "a", "b"), {
        "a": {"prospect_id": "a", "created_at": datetime(2026, 2, 1)},
        "b": {"prospect_id": "b", "created_at": datetime(2026, 1, 1)}})
    assert [c["prospect_id"] for c in group["contacts"]] == ["b", "a"]
    assert group["pair_id"] == "p" and group["confidence"] == "HIGH" and group["reasons"] == ["x"]


def test_scan_job_isolates_failures():
    class Db:
        def __init__(self):
            self.commits = self.rollbacks = 0

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

    class Repo(FakeStore):
        def candidates(self, tenant_id):
            if tenant_id == "bad":
                raise RuntimeError("boom")
            return []

        def tenants_with_contacts(self):
            return ["t1", "bad", "t2"]

    totals = duplicate_scan.run_scan_job(Db, lambda db: Repo())
    assert totals == {"tenants": 2, "new": 0, "removed": 0, "failed": 1}


# ── dismiss / reopen ───────────────────────────────────────────────

class FakeContacts:
    def __init__(self, store, contacts):
        self.duplicates, self.contacts = store, contacts

    def get_live(self, tenant_id, pid):
        c = self.contacts.get(pid)
        return c if c is not None and c.tenant_id == tenant_id and c.deleted_at is None else None


class FakeUow:
    def __init__(self, contacts):
        self.contacts, self.session = contacts, None
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def commit(self):
        self.commits += 1

    def savepoint(self):
        return nullcontext()


def contact(pid, **kw):
    base = dict(prospect_id=pid, tenant_id="t1", deleted_at=None, merged_into_id=None, owner_id="u1",
                first_name="A", last_name=pid, email=f"{pid}@x.io", consent_status="OPT_IN", account_id=None,
                full_name=f"A {pid}")
    base.update(kw)
    return SimpleNamespace(**base)


def test_dismiss_and_reopen():
    store = FakeStore(pairs=[pair("p1", "a", "b"), pair("p2", "a", "gone")])
    uow = FakeUow(FakeContacts(store, {"a": contact("a"), "b": contact("b")}))
    svc = DuplicateService(uow)
    with pytest.raises(PermissionDenied):
        svc.dismiss(user("AGENT", custom_permissions=[]), "p1")
    out = svc.dismiss(user(), "p1")
    assert out == {"pair_id": "p1", "status": "DISMISSED"} and uow.commits == 1
    assert store.pair("t1", "p1").resolved_by == "u1"
    with pytest.raises(ConflictError, match="dismissed, not open"):
        svc.dismiss(user(), "p1")
    assert svc.reopen(user(), "p1")["status"] == "OPEN" and store.pair("t1", "p1").resolved_by is None
    with pytest.raises(NotFoundError, match="Contact not found"):
        svc.dismiss(user(), "p2")  # a contact the caller cannot see (or that is gone)
    with pytest.raises(NotFoundError, match="suggestion not found"):
        svc.dismiss(user(tenant_id="t2"), "p1")
    with pytest.raises(BadRequestError, match="status must be"):
        svc.pairs(user(), "WHATEVER", None, Paging())


# ── merge safety ───────────────────────────────────────────────────

def test_merge_refusal_and_warnings():
    a, b = contact("a"), contact("b", deleted_at=datetime(2026, 1, 1))
    assert rules.merge_refusal([a, contact("b")], ["a", "b"]) is None
    assert "A b <b@x.io> was deleted or merged" in rules.merge_refusal([a, b], ["a", "b"])
    assert rules.merge_refusal([a], ["a", "b"]).startswith("A contact was deleted")
    assert "cannot be merged" in rules.merge_refusal([a, b], ["a", "b"], rules.MERGE_REFUSED)
    warnings = merge_warnings([contact("a"), contact("b", owner_id="u2", consent_status="UNSUBSCRIBED")])
    assert any("unsubscribed" in w for w in warnings) and any("different owners" in w for w in warnings)
    assert any("e-mail history" in w for w in warnings)


def test_record_snapshot_is_json_safe():
    snap = rules.record_snapshot(SimpleNamespace(prospect_id="a", created_at=datetime(2026, 1, 2, 3, 4), email="a@x"))
    assert snap["prospect_id"] == "a" and snap["created_at"] == "2026-01-02T03:04:00" and snap["email"] == "a@x"
    assert snap["first_name"] is None and "custom_fields" in snap


class MergeRepo:
    """A contact repository with the identity store (IdentityAware), for merge_into."""

    def __init__(self, store):
        self.duplicates = store
        self.staged, self.deleted_ids, self.flushes = [], [], 0

    def memberships(self, pid):
        return []

    def enrollment_rows(self, pid):
        return []

    def persona(self, pid):
        return None

    def reassign_children(self, a, b):
        return 0, 0

    def repoint_merged(self, a, b):
        pass

    def links_of(self, pid):
        return []

    def linked_account_ids(self, pid, except_account=None):
        return []

    def flush(self):
        self.flushes += 1

    def delete_rows(self, ids):
        self.deleted_ids.extend(ids)

    def stage(self, entity):
        self.staged.append(entity)
        return entity

    def remove(self, entity):
        pass


FILL = ("phone", "mobile_phone", "designation", "industry", "emp_band", "linkedin_url", "poc_city", "poc_state",
        "poc_country", "timezone", "lifecycle_stage", "lead_status", "lead_source", "legal_basis", "company_name")


def full(pid, **kw):
    base = {f: None for f in FILL}
    base.update(prospect_id=pid, tenant_id="t1", deleted_at=None, merged_into_id=None, owner_id="u1", first_name=None,
                last_name=None, email=f"{pid}@x.io", tags=None, custom_fields=None, consent_status="OPT_IN",
                account_id=None, full_name=pid)
    base.update(kw)
    return SimpleNamespace(**base)


def test_merge_into_locks_logs_and_carries_identities():
    store = FakeStore()
    keep, dup = full("keep"), full("dup", first_name="Dee")
    store.lock_result = [keep, dup]
    repo = MergeRepo(store)
    moved = rules.merge_into(repo, keep, [dup], "u1", {"email": "dup", "bogus": "dup"})
    assert store.locked == [["keep", "dup"]]
    assert store.carried == [("keep", [("dup", "dup@x.io")], "dup@x.io")]
    assert keep.email == "dup@x.io" and keep.first_name == "Dee" and repo.deleted_ids == ["dup"]
    (log,) = store.logs
    assert log.survivor_id == "keep" and log.merged_ids == ["dup"] and log.choices == {"email": "dup"}
    assert log.snapshot["survivor"]["email"] == "keep@x.io" and log.snapshot["merged"][0]["first_name"] == "Dee"
    assert log.moved == moved and store.resolved == [(["dup"], "u1")]


def test_merge_into_refuses_when_a_contact_was_merged_meanwhile():
    store = FakeStore()
    keep, dup = full("keep"), full("dup")
    store.lock_result = [keep]  # dup is gone (merged by someone else)
    repo = MergeRepo(store)
    with pytest.raises(ConflictError, match="Reload and try again"):
        rules.merge_into(repo, keep, [dup], "u1")
    assert not repo.deleted_ids and not store.logs


# ── export log ─────────────────────────────────────────────────────

class FakeLogs:
    def __init__(self):
        self.rows = []

    def add(self, log):
        self.rows.append(log)

    def page(self, tenant_id, object_type, user_id, paging):
        self.calls = (tenant_id, object_type, user_id, paging)
        rows = [r for r in self.rows if r.tenant_id == tenant_id]
        return Page(items=rows, total=len(rows), paging=paging)

    def user_names(self, ids):
        return {i: "Ann Lee" for i in ids}


def test_record_export_and_admin_only_log():
    logs = FakeLogs()
    log = record_export(logs, "t1", "u1", "contact", "q=x" * 2000, 12, "CSV")
    assert (log.object_type, log.row_count, log.format, len(log.filter_summary)) == ("CONTACT", 12, "csv", 2000)
    svc = ExportLogService(FakeUow(None), logs=lambda session: logs)
    with pytest.raises(PermissionDenied, match="Only admins"):
        svc.page(user("MANAGER"), None, None, Paging())
    out = svc.page(user(), "list", None, Paging(1, 10))
    assert out["total"] == 1 and out["items"][0]["user_name"] == "Ann Lee" and logs.calls[1] == "LIST"


def test_export_summary_and_size_cap():
    summary = query.export_summary(ContactFilter(q="ann", list_id="L1", lifecycle_stage="LEAD"), ["name", "email"], "VIP")
    assert summary == "q=ann; list=VIP; lifecycle_stage=LEAD | columns: name, email"
    assert query.export_summary(ContactFilter(), ["email"]).startswith("all contacts")
    query.check_export_size(query.EXPORT_ROW_LIMIT)
    with pytest.raises(BadRequestError, match="100,001 contacts, more than the 100,000"):
        query.check_export_size(query.EXPORT_ROW_LIMIT + 1)
