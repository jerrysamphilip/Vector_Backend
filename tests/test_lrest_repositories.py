"""
lrest repositories against MySQL: the queries moved out of the SES webhook, tracking, company profile,
connection, AI email and legacy services, each with tenant isolation where the entry point takes a tenant.
"""
import uuid
from datetime import timedelta

import pytest

from app.core.clock import utcnow
from app.domain.contacts import VisibilityScope
from app.models.audit import AuditLog
from app.models.company_profile import CompanyProfile
from app.models.email_message import EmailEvent, EmailMessage
from app.models.prospect import GlobalUnsubscribe, Prospect
from app.models.sales_extra import UserConnection
from app.repositories.audit import SqlAlchemyAuditRepository
from app.repositories.company_profiles import SqlAlchemyCompanyProfilesUnitOfWork, company_profiles_repo
from app.repositories.connections import SqlAlchemyConnectionsUnitOfWork, connections_repo
from app.repositories.daily_limit import daily_limit_repo
from app.repositories.email_events import SqlAlchemyEmailEventsUnitOfWork
from app.repositories.enrollment import SqlAlchemyEnrollmentRepository
from app.repositories.mentions import mentions_repo
from app.repositories.personas import SqlAlchemyPersonaCampaignRepository, SqlAlchemyPersonasUnitOfWork
from app.repositories.platform_flags import PlatformFlagRepository
from app.repositories.revalidation import SqlAlchemyRevalidationRepository
from app.repositories.suppression import suppression_repo
from app.repositories.uploads import SqlAlchemyUploadRepository, UploadConflict, UploadRowRefused
from app.repositories.workflow import workflow_repo
from tests.conftest import RUN, SessionLocal, register_tenant


@pytest.fixture(scope="module")
def ws(client):
    return register_tenant(client, "lresta")


@pytest.fixture(scope="module")
def other(client):
    return register_tenant(client, "lrestb")


def _user_id(tenant):
    with SessionLocal() as db:
        from app.models.user import User
        return db.query(User.user_id).filter(User.email == tenant["email"]).scalar()


def _contact(db, tenant_id, email=None, **kw):
    p = Prospect(tenant_id=tenant_id, email=email or f"c.{uuid.uuid4().hex[:8]}@lrest{RUN}.example.com",
                 first_name="C", consent_status="OPT_IN", **kw)
    db.add(p)
    db.flush()
    return p


def test_email_events_repository(ws):
    with SessionLocal() as db:
        p = _contact(db, ws["tenant_id"], email=f"Mixed.{uuid.uuid4().hex[:6]}@LREST{RUN}.example.com")
        ses = f"ses-{uuid.uuid4().hex}"
        m = EmailMessage(prospect_id=p.prospect_id, direction="OUTBOUND", status="SENT", to_email=p.email,
                         ses_message_id=ses, provider_message_id=f"prov-{ses}")
        db.add(m)
        db.commit()
        with SqlAlchemyEmailEventsUnitOfWork.for_session(db) as uow:
            repo = uow.events
            assert repo.prospect_tenant(p.prospect_id) == ws["tenant_id"]
            assert repo.tenants_with_email(f"  {p.email.upper()} ") == [ws["tenant_id"]]
            assert repo.message_id_for_provider_id(ses) == m.message_id
            assert repo.message_id_for_provider_id(f"prov-{ses}") == m.message_id
            assert repo.message_id_for_provider_id("nope") is None
            repo.add_event(m.message_id, EmailEvent.EVENT_OPEN, {"dedupe_key": "k1"})
            note = repo.add_message(prospect_id=p.prospect_id, direction="INBOUND", status="SENT", to_email="x",
                                    from_email="y")
            uow.commit()
            assert repo.has_event_with_key(m.message_id, "k1") and not repo.has_event_with_key(m.message_id, "k2")
            assert repo.message(m.message_id) is m and repo.message(note.message_id) is not None
            assert repo.prospect(p.prospect_id) is p and repo.conversation("nope") is None


def test_company_profiles_repository(ws, other):
    with SessionLocal() as db:
        with SqlAlchemyCompanyProfilesUnitOfWork.for_session(db) as uow:
            repo = uow.profiles
            a = repo.add(CompanyProfile(profile_id=str(uuid.uuid4()), tenant_id=ws["tenant_id"], profile_name="B",
                                        company_name="Acme", postal_address="1 Road", is_default=True))
            b = repo.add(CompanyProfile(profile_id=str(uuid.uuid4()), tenant_id=ws["tenant_id"], profile_name="A",
                                        company_name="Acme 2", is_default=False))
            uow.commit()
            assert [p.profile_id for p in repo.list(ws["tenant_id"])][:2] == [a.profile_id, b.profile_id]
            assert repo.get(other["tenant_id"], a.profile_id) is None
            assert repo.default(ws["tenant_id"]).profile_id == a.profile_id
            assert repo.identity_fields(ws["tenant_id"]) == ("Acme", "1 Road")
            assert repo.tenant_name(ws["tenant_id"]).startswith("Tenant lresta")
            repo.clear_default(ws["tenant_id"], keep_profile_id=b.profile_id)
            uow.commit()
            repo.refresh(a)
            assert a.is_default is False
            repo.clear_default(ws["tenant_id"])
            uow.commit()
            assert company_profiles_repo(db).user_name_parts(_user_id(ws)) == ("LRESTA", "Owner")
            assert company_profiles_repo(db).user_name_parts("nobody") is None
            repo.delete(b)
            uow.commit()
            assert repo.get(ws["tenant_id"], b.profile_id) is None


def test_connections_repository(ws, other):
    uid = _user_id(ws)
    with SessionLocal() as db:
        with SqlAlchemyConnectionsUnitOfWork.for_session(db) as uow:
            repo = uow.connections
            c = repo.add(UserConnection(tenant_id=ws["tenant_id"], user_id=uid, provider="GOOGLE",
                                        sync_calendar=True))
            mine = _contact(db, ws["tenant_id"], owner_id=uid)
            theirs = _contact(db, other["tenant_id"], email=mine.email)
            uow.commit()
            assert repo.for_user(uid) == [c] and repo.owned(uid, c.connection_id) is c
            assert repo.owned("someone-else", c.connection_id) is None
            assert repo.by_provider(uid, "GOOGLE") is c and repo.by_id(c.connection_id) is c
            assert repo.calendar_connection(uid) is c and c in repo.every()
            assert repo.user(uid).email == ws["email"] and repo.prospect(mine.prospect_id) is mine
            everyone = repo.contacts_by_emails(ws["tenant_id"], VisibilityScope(), [mine.email.lower()])
            assert everyone == [mine] and theirs not in everyone
            assert repo.contacts_by_emails(ws["tenant_id"], VisibilityScope(owner_ids=frozenset({"x"})),
                                           [mine.email.lower()]) == []
            assert repo.activity_logged("missing") is False
            repo.delete(c)
            uow.commit()
            assert connections_repo(db).for_user(uid) == []


def test_personas_repositories(ws, other):
    with SessionLocal() as db:
        p = _contact(db, ws["tenant_id"])
        db.commit()
        with SqlAlchemyPersonasUnitOfWork.for_session(db) as uow:
            repo = uow.personas
            assert repo.contact(ws["tenant_id"], p.prospect_id) is p
            assert repo.contact(other["tenant_id"], p.prospect_id) is None
            other_bp = repo.active_blueprint("OTHER")
            assert other_bp is not None and repo.blueprint(other_bp.blueprint_id) is other_bp
            assert repo.blueprint_for_type("OTHER") is other_bp
            assert other_bp in repo.blueprints(True) and len(repo.blueprints(False)) >= len(repo.blueprints(True))
            assert repo.persona_of(p.prospect_id) is None
        campaigns = SqlAlchemyPersonaCampaignRepository(db)
        assert campaigns.list_contacts(ws["tenant_id"], "no-such-list") == []
        assert campaigns.personas_for([p.prospect_id]) == []
        assert campaigns.campaign_tenant("nope") is None and campaigns.campaign("nope") is None
        assert campaigns.sequences("nope") == []
        detached = campaigns.detached_active_blueprint("OTHER")
        assert detached is not None and detached not in db
        campaigns.delete_campaign_templates("nope")


def test_suppression_and_revalidation_repositories(ws, other):
    now = utcnow()
    with SessionLocal() as db:
        addr = f"supp.{uuid.uuid4().hex[:6]}@lrest{RUN}.example.com"
        p = _contact(db, ws["tenant_id"], email=addr)
        repo = suppression_repo(db)
        repo.add(GlobalUnsubscribe(tenant_id=ws["tenant_id"], email=addr, unsubscribed_at=now,
                                   reason="Hard bounce: General"))
        expired = f"old.{uuid.uuid4().hex[:6]}@lrest{RUN}.example.com"
        repo.add(GlobalUnsubscribe(tenant_id=ws["tenant_id"], email=expired, unsubscribed_at=now,
                                   suppression_expires_at=now - timedelta(days=1), reason="Unsubscribed"))
        db.add(EmailMessage(prospect_id=p.prospect_id, direction="OUTBOUND", status="QUEUED", to_email=addr))
        db.commit()
        assert repo.active_emails(ws["tenant_id"], [addr, expired], now) == [addr]
        assert repo.active_emails(other["tenant_id"], [addr], now) == []
        assert repo.active(ws["tenant_id"], addr.upper(), now) is not None
        assert repo.active(ws["tenant_id"], expired, now) is None and repo.entry(ws["tenant_id"], expired)
        assert repo.contacts_with_email(ws["tenant_id"], addr) == [p]
        assert repo.contacts_with_email(other["tenant_id"], addr) == []
        assert repo.cancel_pending([p.prospect_id], ("QUEUED", "SCHEDULED"), "Suppressed") == 1
        assert repo.enrolments([p.prospect_id]) == []
        revalidation = SqlAlchemyRevalidationRepository(db)
        assert revalidation.suppression(ws["tenant_id"], addr).reason.startswith("Hard bounce")
        assert len(revalidation.suppressions(ws["tenant_id"])) >= 2
        assert revalidation.contact(other["tenant_id"], p.prospect_id) is None
        assert revalidation.contacts_with_emails(ws["tenant_id"], {addr}) == [p]
        assert revalidation.other_contact_with_email(ws["tenant_id"], addr, p.prospect_id) is None
        assert revalidation.bounced_enrolments(p.prospect_id) == []
        assert revalidation.count_bounced_enrolments(p.prospect_id) == 0
        repo.remove(ws["tenant_id"], addr)
        db.commit()
        assert repo.entry(ws["tenant_id"], addr) is None


def test_upload_repository(ws, other):
    with SessionLocal() as db:
        addr = f"up.{uuid.uuid4().hex[:6]}@lrest{RUN}.example.com"
        p = _contact(db, ws["tenant_id"], email=addr)
        db.commit()
        repo = SqlAlchemyUploadRepository(db)
        assert repo.contacts_with_emails(ws["tenant_id"], [addr]) == [p]
        assert repo.contacts_with_emails(other["tenant_id"], [addr]) == [] and repo.contacts_with_emails("t", []) == []
        assert repo.list_exists(ws["tenant_id"], "nope") is False and repo.member_counts("nope") == (0, 0)
        assert isinstance(repo.suppression_reasons(ws["tenant_id"]), list)
        with pytest.raises(UploadRowRefused) as refused:
            repo.in_savepoint(lambda: repo.add(Prospect(tenant_id=ws["tenant_id"], email=addr, first_name="Dup")))
        assert refused.value.duplicate is True
        with pytest.raises(UploadRowRefused) as too_long:
            repo.in_savepoint(lambda: repo.add(Prospect(tenant_id=ws["tenant_id"], email="x" * 400 + "@a.io")))
        assert too_long.value.duplicate is False
        assert repo.in_savepoint(lambda: 42) == 42
        repo.add(Prospect(tenant_id=ws["tenant_id"], email=addr, first_name="Dup"))
        with pytest.raises(UploadConflict):
            repo.commit()


def test_platform_flag_repository():
    with SessionLocal() as db:
        flags = PlatformFlagRepository(db)
        name = f"lrest:{uuid.uuid4().hex[:8]}"
        flags.ensure_counter(name)
        assert flags.add_within(name, 5, 10) and not flags.add_within(name, 6, 10)
        assert flags.increment_below(name, 7) and not flags.increment_below(name, 6)
        flags.adjust(name, -100)
        assert flags.get(name) == "0"
        flags.put(name, "x")
        assert flags.get(name) == "x"
        flags.remove(name)
        assert flags.get(name) is None
        db.commit()


def test_audit_and_mention_repositories(ws, other):
    uid = _user_id(ws)
    with SessionLocal() as db:
        audit = SqlAlchemyAuditRepository(db)
        log = audit.add(AuditLog(log_id=str(uuid.uuid4()), tenant_id=ws["tenant_id"], user_id=uid,
                                 action="UPDATE_CAMPAIGN", entity_type="campaign", entity_id="c-lrest"))
        assert audit.trail(ws["tenant_id"], "campaign", "c-lrest", uid, 5) == [log]
        assert log not in audit.trail(other["tenant_id"], None, None, None, 50)
        assert audit.entity_history("campaign", "c-lrest", 5) == [log]

        mentions = mentions_repo(db)
        assert [u.user_id for u in mentions.mentionable(ws["tenant_id"], "lresta", 5)] == [uid]
        assert mentions.mentionable(other["tenant_id"], "lresta", 5) == []
        assert [u.user_id for u in mentions.active_users(ws["tenant_id"], [uid])] == [uid]
        assert mentions.of_activity("nope") == [] and mentions.with_users(["nope"]) == []
        db.rollback()


def test_limit_workflow_and_enrollment_repositories(ws, other):
    uid = _user_id(ws)
    with SessionLocal() as db:
        limits = daily_limit_repo(db)
        start = utcnow() - timedelta(days=1)
        assert limits.first_steps_sent(uid, start, utcnow()) == 0 and limits.held(uid, "X") == 0
        assert limits.step_number("nope") is None and limits.campaign_creator("nope") is None
        assert limits.owners([]) == [] and limits.user_name_parts(uid) == ("LRESTA", "Owner")
        assert limits.user_name_parts("nobody") == ()

        p = _contact(db, ws["tenant_id"])
        flow = workflow_repo(db)
        assert flow.record(ws["tenant_id"], "CONTACT", p.prospect_id) is p
        assert flow.record(other["tenant_id"], "CONTACT", p.prospect_id) is None
        assert flow.prospect(other["tenant_id"], p.prospect_id) is None
        assert flow.active_rules(ws["tenant_id"], "LEAD") == []

        enrol = SqlAlchemyEnrollmentRepository(db)
        assert enrol.tenant_contacts(ws["tenant_id"], [p.prospect_id]) == [p]
        assert enrol.tenant_contacts(other["tenant_id"], [p.prospect_id]) == []
        assert enrol.sent_since([p.prospect_id], start) == set()
        assert enrol.enrolled_in_live_campaigns_since([p.prospect_id], start) == set()
        assert enrol.enrolled("nope", [p.prospect_id]) == set()
        assert enrol.lists_with_owners(ws["tenant_id"], ["nope"]) == []
        db.rollback()
