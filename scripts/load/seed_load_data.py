"""Seed a dedicated load-test database (chunk V2, wave 6 evidence).

    MYSQL_HOST=127.0.0.1 MYSQL_PORT=3316 MYSQL_USER=root MYSQL_PASSWORD=root MYSQL_DATABASE=vtest_load \\
    OPENAI_API_KEY= python -m app.db.migrate
    ... same env ... python scripts/load/seed_load_data.py            # 100k contacts, ~200k messages
    ... same env ... python scripts/load/seed_load_data.py --contacts 5000 --messages 10000   # smoke size

One tenant, 10 users across roles and two sales teams, contacts with realistic names / domains / companies,
~5 % fuzzy duplicates (same name at the same company under a second address, or a shared phone), custom
fields, accounts, static and active lists, campaigns with sequences and enrolments, email_messages in
mixed states with engagement events, conversations (unified inbox), leads and opportunities.

Safety: the database name must start with ``vtest_load`` or ``load_`` (never ``outreach_ai``), the schema
must be migrated (alembic_version present) and the database must hold no tenants or contacts yet; otherwise
the script refuses and changes nothing. Deterministic: every value comes from ``random.Random(--seed)``;
only timestamps are relative to the day the seed runs so "last 30 days" dashboards have data.

Writes the sign-in manifest (user emails, roles, the shared password) to reports/load/<db>_seed.json,
which run_load.py reads.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB_NAME_RE = re.compile(r"^(vtest_load|load_)[A-Za-z0-9_]*$")
FORBIDDEN_DBS = {"outreach_ai"}
DEFAULT_PASSWORD = "LoadTest!2026-v2"  # test-only credential for a throw-away database
TENANT_NAME = "Load Test Co"

FIRST_NAMES = (
    "James Mary Robert Patricia John Jennifer Michael Linda David Elizabeth William Barbara Richard Susan Joseph "
    "Jessica Thomas Sarah Charles Karen Christopher Lisa Daniel Nancy Matthew Betty Anthony Sandra Mark Margaret "
    "Donald Ashley Steven Kimberly Andrew Emily Paul Donna Joshua Michelle Kenneth Carol Kevin Amanda Brian Melissa "
    "George Deborah Timothy Stephanie Ronald Rebecca Jason Sharon Edward Laura Jeffrey Cynthia Ryan Dorothy Jacob "
    "Amy Gary Kathleen Nicholas Angela Eric Shirley Jonathan Emma Stephen Brenda Larry Pamela Justin Nicole Scott "
    "Anna Brandon Samantha Benjamin Katherine Samuel Christine Gregory Debra Alexander Rachel Patrick Carolyn Frank "
    "Janet Raymond Maria Jack Olivia Dennis Heather Jerry Helen Tyler Catherine Aaron Diane Jose Julie Adam Victoria "
    "Nathan Joyce Henry Lauren Zachary Kelly Douglas Christina Peter Ruth Kyle Joan Noah Virginia Ethan Judith "
    "Priya Arjun Wei Mei Hiroshi Yuki Ahmed Fatima Carlos Sofia Lukas Anja Mateo Lucia Omar Leila Raj Ananya Chen "
    "Ines Pierre Camille Sven Ingrid Kwame Amara Dmitri Olga Tariq Zara Javier Elena Hannah Felix Nora Oscar Freya"
).split()
LAST_NAMES = (
    "Smith Johnson Williams Brown Jones Garcia Miller Davis Rodriguez Martinez Hernandez Lopez Gonzalez Wilson "
    "Anderson Thomas Taylor Moore Jackson Martin Lee Perez Thompson White Harris Sanchez Clark Ramirez Lewis "
    "Robinson Walker Young Allen King Wright Scott Torres Nguyen Hill Flores Green Adams Nelson Baker Hall Rivera "
    "Campbell Mitchell Carter Roberts Gomez Phillips Evans Turner Diaz Parker Cruz Edwards Collins Reyes Stewart "
    "Morris Morales Murphy Cook Rogers Gutierrez Ortiz Morgan Cooper Peterson Bailey Reed Kelly Howard Ramos Kim "
    "Cox Ward Richardson Watson Brooks Chavez Wood James Bennett Gray Mendoza Ruiz Hughes Price Alvarez Castillo "
    "Sanders Patel Myers Long Ross Foster Jimenez Powell Jenkins Perry Russell Sullivan Bell Coleman Butler "
    "Henderson Barnes Fisher Vasquez Simmons Romero Jordan Patterson Alexander Hamilton Graham Reynolds Griffin "
    "Wallace Moreno West Cole Hayes Bryant Herrera Gibson Ellis Tran Medina Aguilar Stevens Murray Ford Castro "
    "Marshall Owens Harrison Fernandez McDonald Woods Washington Kennedy Wells Vargas Henry Chen Freeman Webb "
    "Tucker Guzman Burns Crawford Olson Simpson Porter Hunter Gordon Mendez Silva Shaw Snyder Mason Dixon Munoz "
    "Hunt Hicks Holmes Palmer Wagner Black Robertson Boyd Rose Stone Salazar Fox Warren Mills Meyer Rice Schmidt "
    "Sharma Gupta Singh Kumar Tanaka Suzuki Yamamoto Wang Zhang Liu Novak Kowalski Muller Schneider Rossi Bianchi "
    "Dubois Moreau Larsen Hansen Okafor Mensah Ivanov Petrov Haddad Khan Ali Costa Santos Oliveira Berg Lindqvist"
).split()
COMPANY_WORDS = (
    "Apex Blue Bright Cedar Cobalt Crest Delta Echo Ember Falcon Fern Granite Harbor Helix Horizon Iron Juniper "
    "Keystone Lumen Maple Meridian Nimbus North Nova Oak Onyx Orbit Peak Pine Pioneer Prism Quartz Quantum Raven "
    "Ridge River Sable Sage Sierra Silver Slate Spark Spruce Summit Terra Tidal Titan Trident Vector Vertex Willow "
    "Zenith Atlas Beacon Bridge Canyon Citadel Compass Copper Coral Crown Drift Element Forge Frontier Glacier"
).split()
COMPANY_NOUNS = (
    "Systems Labs Analytics Logistics Health Capital Robotics Software Networks Foods Energy Media Partners "
    "Dynamics Solutions Works Industries Bio Cloud Data Security Retail Motors Studios Consulting Ventures"
).split()
COMPANY_SUFFIXES = ("Inc", "LLC", "Ltd", "GmbH", "Group", "Corp", "AG", "BV", "PLC", "Co")
TLDS = ("com", "io", "co", "net", "ai", "de", "co.uk", "fr", "in", "com.au")
PERSONAL_DOMAINS = ("gmail.com", "outlook.com", "yahoo.com", "icloud.com", "proton.me")
INDUSTRIES = ("Software", "Healthcare", "Financial Services", "Manufacturing", "Retail", "Logistics",
              "Education", "Energy", "Media", "Telecommunications", "Real Estate", "Consulting")
EMP_BANDS = ("1-10", "11-50", "51-200", "201-500", "501-1000", "1001-5000", "5001-10000", "10000+")
TITLES = ("CEO", "CTO", "CFO", "COO", "VP Sales", "VP Marketing", "Head of Operations", "Sales Manager",
          "Marketing Manager", "Product Manager", "Engineering Manager", "Procurement Lead", "IT Director",
          "Account Executive", "Business Analyst", "Data Scientist", "HR Director", "Founder")
LOCATIONS = (
    ("New York", "NY", "United States", "America/New_York"), ("San Francisco", "CA", "United States", "America/Los_Angeles"),
    ("Austin", "TX", "United States", "America/Chicago"), ("Chicago", "IL", "United States", "America/Chicago"),
    ("London", None, "United Kingdom", "Europe/London"), ("Manchester", None, "United Kingdom", "Europe/London"),
    ("Berlin", None, "Germany", "Europe/Berlin"), ("Munich", None, "Germany", "Europe/Berlin"),
    ("Paris", None, "France", "Europe/Paris"), ("Amsterdam", None, "Netherlands", "Europe/Amsterdam"),
    ("Bangalore", "KA", "India", "Asia/Kolkata"), ("Mumbai", "MH", "India", "Asia/Kolkata"),
    ("Sydney", "NSW", "Australia", "Australia/Sydney"), ("Toronto", "ON", "Canada", "America/Toronto"),
    ("Singapore", None, "Singapore", "Asia/Singapore"), ("Dubai", None, "United Arab Emirates", "Asia/Dubai"),
)
LIFECYCLE = (("SUBSCRIBER", 15), ("LEAD", 40), ("MQL", 15), ("SQL", 10), ("OPPORTUNITY", 8), ("CUSTOMER", 10),
             ("EVANGELIST", 2))
LEAD_STATUSES = (("NEW", 30), ("OPEN", 20), ("IN_PROGRESS", 15), ("ATTEMPTED_TO_CONTACT", 15), ("CONNECTED", 10),
                 ("OPEN_DEAL", 4), ("BAD_TIMING", 3), ("UNQUALIFIED", 3))
LEAD_SOURCES = ("Website", "Referral", "Event", "Cold outbound", "LinkedIn", "Partner", "Webinar", "Import")
TAGS = ("vip", "newsletter", "webinar-2026", "trial", "partner", "churn-risk", "expansion", "event-lead")
TIERS = ("Bronze", "Silver", "Gold", "Platinum")
REGIONS = ("NA", "EMEA", "APAC", "LATAM")

# 10 users: role, sales_level, team. Managers report to the owner; agents to their team's manager.
USER_SPECS = (
    ("owner", "SUPER_ADMIN", None, None), ("admin", "ADMIN", None, None),
    ("head", "MANAGER", 1, None),
    ("mgr.north", "MANAGER", 2, "north"), ("mgr.south", "MANAGER", 2, "south"),
    ("agent.n1", "AGENT", 3, "north"), ("agent.n2", "AGENT", 3, "north"), ("agent.n3", "AGENT", 3, "north"),
    ("agent.s1", "AGENT", 3, "south"), ("agent.s2", "AGENT", 3, "south"),
)
USER_DOMAIN = "vector-load.example"


@dataclass
class Config:
    contacts: int = 100_000
    accounts: int = 2_000
    lists: int = 50
    campaigns: int = 20
    messages: int = 200_000
    batch: int = 5_000
    seed: int = 20261027
    password: str = DEFAULT_PASSWORD
    dup_rate: float = 0.05


@dataclass
class World:
    """Everything generated so far that later tables refer to."""
    tenant_id: str = ""
    users: List[Dict[str, Any]] = field(default_factory=list)
    inboxes: List[str] = field(default_factory=list)
    accounts: List[Dict[str, Any]] = field(default_factory=list)
    contacts: List[Dict[str, Any]] = field(default_factory=list)
    stages: List[Dict[str, Any]] = field(default_factory=list)
    campaigns: List[Dict[str, Any]] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=dict)


def weighted(rng: random.Random, pairs: Sequence[tuple]) -> Any:
    values, weights = zip(*pairs)
    return rng.choices(values, weights=weights, k=1)[0]


def new_id(rng: random.Random) -> str:
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def check_name(db_name: str) -> Optional[str]:
    """Why this database name is off limits, or None when it is a load-test database."""
    if db_name in FORBIDDEN_DBS:
        return f"refusing: {db_name} is a real application database"
    if not DB_NAME_RE.match(db_name or ""):
        return f"refusing: database name {db_name!r} must start with vtest_load or load_"
    return None


def check_target(db_name: str, table_names: Iterable[str], counts: Dict[str, int]) -> Optional[str]:
    """Why the seed must not run on this database, or None when it may."""
    bad_name = check_name(db_name)
    if bad_name:
        return bad_name
    names = set(table_names)
    if "alembic_version" not in names or "prospects" not in names:
        return "refusing: schema not migrated; run `python -m app.db.migrate` first"
    busy = {t: n for t, n in counts.items() if n}
    if busy:
        return f"refusing: database is not empty ({busy}); drop and recreate it to reseed"
    return None


# ── Generators (pure: rng + world in, rows out) ─────────────────────────────

def gen_users(rng: random.Random, world: World, password_hash: str, now: datetime) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    by_key: Dict[str, str] = {}
    for key, role, level, team in USER_SPECS:
        uid = new_id(rng)
        by_key[key] = uid
        manager = None
        if role == "MANAGER" and level == 2:
            manager = by_key["head"]
        elif role == "AGENT":
            manager = by_key[f"mgr.{team}"]
        first = key.split(".")[-1].capitalize()
        rows.append({
            "user_id": uid, "tenant_id": world.tenant_id, "first_name": first, "last_name": "Load",
            "email": f"{key}@{USER_DOMAIN}", "role": role, "status": "ACTIVE", "password_hash": password_hash,
            "auth_provider": "local", "email_verified": True, "sales_level": level, "manager_id": manager,
            "notify_email": False, "mfa_enabled": False, "created_at": now - timedelta(days=400),
            "_team": team,
        })
    return rows


def gen_accounts(rng: random.Random, world: World, n: int, now: datetime) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    names: set = set()
    domains: set = set()
    owners = [u["user_id"] for u in world.users]
    while len(rows) < n:
        name = f"{rng.choice(COMPANY_WORDS)} {rng.choice(COMPANY_NOUNS)}"
        if name in names or rng.random() < 0.3:
            name = f"{name} {rng.choice(COMPANY_SUFFIXES)}"
        if name in names:
            name = f"{name} {len(rows)}"
        slug = re.sub(r"[^a-z0-9]", "", name.lower())
        domain = f"{slug}.{rng.choice(TLDS)}"
        if domain in domains:
            domain = f"{slug}{len(rows)}.{rng.choice(TLDS)}"
        names.add(name)
        domains.add(domain)
        city, state, country, _tz = rng.choice(LOCATIONS)
        created = now - timedelta(days=rng.randint(30, 720), seconds=rng.randint(0, 86399))
        rows.append({
            "account_id": new_id(rng), "tenant_id": world.tenant_id, "name": name, "domain": domain,
            "website": f"https://www.{domain}", "industry": rng.choice(INDUSTRIES), "emp_band": rng.choice(EMP_BANDS),
            "city": city, "state": state, "country": country,
            "annual_revenue": rng.randint(5, 5000) * 100_000, "lifecycle_stage": weighted(rng, LIFECYCLE),
            "owner_id": rng.choice(owners), "created_at": created, "updated_at": created, "row_version": 1,
        })
    return rows


def _phone(rng: random.Random) -> str:
    return f"+1 {rng.randint(200, 989)}-{rng.randint(200, 999)}-{rng.randint(1000, 9999)}"


def _contact_owner(rng: random.Random, world: World) -> Optional[str]:
    if rng.random() < 0.05:
        return None  # unassigned, can be picked up
    agents = [u["user_id"] for u in world.users if u["role"] == "AGENT"]
    others = [u["user_id"] for u in world.users if u["role"] != "AGENT"]
    return rng.choice(agents) if rng.random() < 0.8 else rng.choice(others)


def _custom_fields(rng: random.Random, now: datetime) -> Dict[str, Any]:
    values: Dict[str, Any] = {"region": rng.choice(REGIONS), "tier": rng.choice(TIERS)}
    if rng.random() < 0.6:
        values["arr"] = rng.randint(1, 500) * 1000
    if rng.random() < 0.4:
        values["renewal_date"] = (now.date() + timedelta(days=rng.randint(-60, 365))).isoformat()
    return values


def _base_contact(rng: random.Random, world: World, now: datetime) -> Dict[str, Any]:
    first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
    account = rng.choice(world.accounts) if rng.random() < 0.9 else None
    city, state, country, tz = rng.choice(LOCATIONS)
    created = now - timedelta(days=rng.randint(0, 365), seconds=rng.randint(0, 86399))
    scored = rng.random() < 0.7
    fit, eng = rng.randint(0, 100), rng.randint(0, 100)
    total = (fit + eng) // 2
    return {
        "first_name": first, "last_name": last,
        "phone": _phone(rng) if rng.random() < 0.6 else None,
        "mobile_phone": _phone(rng) if rng.random() < 0.3 else None,
        "owner_id": _contact_owner(rng, world),
        "account_id": account["account_id"] if account else None,
        "company_name": account["name"] if account else None,
        "_domain": account["domain"] if account else None,
        "designation": rng.choice(TITLES),
        "industry": account["industry"] if account else rng.choice(INDUSTRIES),
        "emp_band": account["emp_band"] if account else None,
        "lifecycle_stage": weighted(rng, LIFECYCLE), "lead_status": weighted(rng, LEAD_STATUSES),
        "lead_source": rng.choice(LEAD_SOURCES), "legal_basis": rng.choice(("CONSENT", "LEGITIMATE_INTEREST")),
        "poc_city": city, "poc_state": state, "poc_country": country, "timezone": tz,
        "consent_status": "UNSUBSCRIBED" if rng.random() < 0.02 else "OPT_IN",
        "is_valid_email": rng.random() > 0.03, "is_anonymized": False,
        "tags": rng.sample(TAGS, rng.randint(0, 3)) or None,
        "custom_fields": _custom_fields(rng, now),
        "score": total if scored else None, "fit_score": fit if scored else None,
        "engagement_score": eng if scored else None,
        "score_grade": ("A" if total >= 75 else "B" if total >= 50 else "C" if total >= 25 else "D") if scored else None,
        "scored_at": now - timedelta(days=rng.randint(0, 30)) if scored else None,
        "created_at": created, "updated_at": created + timedelta(days=rng.randint(0, 30)), "row_version": 1,
    }


def _unique_email(local: str, domain: str, used: set, n: int) -> str:
    email = f"{local}@{domain}".lower()
    if email in used:
        email = f"{local}{n}@{domain}".lower()
    used.add(email)
    return email


def _duplicate_of(rng: random.Random, original: Dict[str, Any], used: set, n: int) -> Dict[str, Any]:
    """A likely duplicate: same name at the same company under another address, or a shared phone."""
    dup = dict(original)
    domain = original["_domain"] or rng.choice(PERSONAL_DOMAINS)
    local = f"{original['first_name'][0]}{original['last_name']}"
    dup["email"] = _unique_email(re.sub(r"[^a-z0-9.]", "", local.lower()), domain, used, n)
    if rng.random() < 0.3:
        dup["phone"] = original["phone"] or _phone(rng)
    dup["custom_fields"] = dict(original["custom_fields"])
    return dup


def gen_contacts(rng: random.Random, world: World, n: int, dup_rate: float, now: datetime) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    used: set = set()
    while len(rows) < n:
        i = len(rows)
        if rows and rng.random() < dup_rate:
            row = _duplicate_of(rng, rng.choice(rows), used, i)
        else:
            row = _base_contact(rng, world, now)
            domain = row["_domain"] if row["_domain"] and rng.random() < 0.9 else rng.choice(PERSONAL_DOMAINS)
            local = re.sub(r"[^a-z0-9.]", "", f"{row['first_name']}.{row['last_name']}".lower())
            row["email"] = _unique_email(local, domain, used, i)
        row["prospect_id"] = new_id(rng)
        row["tenant_id"] = world.tenant_id
        row["email_type"] = "PERSONAL" if row["email"].split("@")[1] in PERSONAL_DOMAINS else "BUSINESS"
        rows.append(row)
    return rows


def gen_lists(rng: random.Random, world: World, n: int, now: datetime) -> tuple:
    lists: List[Dict[str, Any]] = []
    members: List[Dict[str, Any]] = []
    uploaders = [u["user_id"] for u in world.users if u["role"] != "AGENT"]
    ids = [c["prospect_id"] for c in world.contacts]
    for i in range(n):
        active = i % 5 == 4  # one in five is an active (filter-defined) list
        list_id = new_id(rng)
        spec = None
        if active:
            spec = {"op": "AND", "conditions": [
                {"field": "lifecycle_stage", "operator": "is", "value": weighted(rng, LIFECYCLE)},
                {"field": "custom.region", "operator": "is", "value": rng.choice(REGIONS)},
            ]}
        lists.append({
            "list_id": list_id, "tenant_id": world.tenant_id, "list_name": f"Load list {i + 1:02d}",
            "source_type": "MANUAL" if active else rng.choice(("CSV", "IMPORT", "MANUAL")),
            "list_type": "ACTIVE" if active else "STATIC", "filters": spec,
            "description": "Seeded for the V2 load test", "uploaded_by": rng.choice(uploaders),
            "uploaded_at": now - timedelta(days=rng.randint(1, 200)),
        })
        if not active and ids:
            for pid in rng.sample(ids, min(len(ids), rng.randint(len(ids) // 200 + 1, len(ids) // 50 + 2))):
                members.append({"id": new_id(rng), "list_id": list_id, "prospect_id": pid,
                                "tenant_id": world.tenant_id, "is_new_prospect": False})
    return lists, members


def gen_stages(rng: random.Random, world: World) -> List[Dict[str, Any]]:
    from app.models.sales import DEFAULT_SALES_STAGES
    return [{"stage_id": new_id(rng), "tenant_id": world.tenant_id, "name": name, "probability": prob,
             "sort_order": i, "is_won": won, "is_lost": lost, "active": True}
            for i, (name, prob, won, lost) in enumerate(DEFAULT_SALES_STAGES)]


@dataclass
class CampaignRows:
    campaigns: List[Dict[str, Any]] = field(default_factory=list)
    sequences: List[Dict[str, Any]] = field(default_factory=list)
    enrolments: List[Dict[str, Any]] = field(default_factory=list)
    messages: List[Dict[str, Any]] = field(default_factory=list)
    events: List[Dict[str, Any]] = field(default_factory=list)
    conversations: List[Dict[str, Any]] = field(default_factory=list)
    metrics: List[Dict[str, Any]] = field(default_factory=list)
    campaign_inboxes: List[Dict[str, Any]] = field(default_factory=list)
    conversation_index: Dict[tuple, Dict[str, Any]] = field(default_factory=dict)


CAMPAIGN_STATUSES = ("ACTIVE", "ACTIVE", "ACTIVE", "ACTIVE", "PAUSED", "COMPLETED", "COMPLETED", "DRAFT")


def _message(rng: random.Random, camp: Dict[str, Any], seq: Dict[str, Any], contact: Dict[str, Any],
             inbox: str, status: str, when: datetime) -> Dict[str, Any]:
    sent = status in ("SENT", "BOUNCED", "FAILED")
    return {
        "message_id": new_id(rng), "campaign_id": camp["campaign_id"], "tenant_id": camp["tenant_id"],
        "prospect_id": contact["prospect_id"], "sequence_id": seq["sequence_id"], "inbox_id": inbox, "direction": "OUTBOUND", "status": status,
        "scheduled_at": when, "sent_at": when if status in ("SENT", "BOUNCED") else None,
        "delivered_at": when + timedelta(seconds=20) if status == "SENT" and rng.random() < 0.9 else None,
        "subject": f"{camp['campaign_name']}: step {seq['step_number']} for {contact['first_name']}",
        "body_text": f"Hi {contact['first_name']}, following up on {camp['campaign_name']}.",
        "to_email": contact["email"], "from_email": f"sales@{USER_DOMAIN}",
        "final_status": {"SENT": "DELIVERED", "BOUNCED": "BOUNCED", "FAILED": "FAILED"}.get(status) if sent else None,
        "failure_reason": "550 mailbox unavailable" if status in ("BOUNCED", "FAILED") else None,
        "conversation_id": None, "retry_count": 0, "deferred_count": 0,
    }


def _enrol_one(rng: random.Random, world: World, out: CampaignRows, camp: Dict[str, Any],
               seqs: List[Dict[str, Any]], contact: Dict[str, Any], now: datetime, tally: Dict[str, int]) -> int:
    """Messages for one contact's journey through one campaign; returns how many were written."""
    inbox = rng.choice(world.inboxes)
    done = rng.randint(1, len(seqs)) if camp["status"] != "COMPLETED" else len(seqs)
    when = now - timedelta(days=rng.randint(5, 80), seconds=rng.randint(0, 86399))
    status_cp, written, last_sent = "ACTIVE", 0, None
    for seq in seqs[:done]:
        status = "BOUNCED" if rng.random() < 0.025 else ("FAILED" if rng.random() < 0.005 else "SENT")
        msg = _message(rng, camp, seq, contact, inbox, status, when)
        out.messages.append(msg)
        written += 1
        if status == "BOUNCED":
            out.events.append({"event_id": new_id(rng), "message_id": msg["message_id"],
                               "tenant_id": msg["tenant_id"], "event_type": 5,
                               "event_time": when + timedelta(minutes=1)})
            tally["bounced"] += 1
            status_cp = "BOUNCED"
            break
        if status == "SENT":
            tally["sent"] += 1
            last_sent = msg
            _engagement(rng, out, msg, when, tally)
        when += timedelta(days=seq["wait_days"] or 2)
    if status_cp == "ACTIVE" and last_sent is not None and rng.random() < 0.04:
        written += _reply(rng, world, out, camp, last_sent, contact, inbox, tally)
        status_cp = "REPLIED"
    elif status_cp == "ACTIVE" and done < len(seqs):
        nxt = seqs[done]
        queued = "QUEUED" if camp["status"] == "ACTIVE" else "CANCELLED"
        out.messages.append(_message(rng, camp, nxt, contact, inbox, queued,
                                     now + timedelta(hours=rng.randint(1, 240))))
        written += 1
    elif status_cp == "ACTIVE":
        status_cp = "COMPLETED"
    out.enrolments.append({
        "id": new_id(rng), "campaign_id": camp["campaign_id"], "tenant_id": camp["tenant_id"],
        "prospect_id": contact["prospect_id"], "current_step": min(done + (status_cp == "ACTIVE"), len(seqs)), "status": status_cp,
        "enrolled_at": now - timedelta(days=85),
    })
    return written


def _engagement(rng: random.Random, out: CampaignRows, msg: Dict[str, Any], when: datetime,
                tally: Dict[str, int]) -> None:
    """Open (35 %) and click (15 % of opens) events for a delivered message."""
    if rng.random() < 0.35:
        out.events.append({"event_id": new_id(rng), "message_id": msg["message_id"],
                           "tenant_id": msg["tenant_id"], "event_type": 2,
                           "event_time": when + timedelta(hours=rng.randint(1, 48))})
        tally["opened"] += 1
        if rng.random() < 0.15:
            out.events.append({"event_id": new_id(rng), "message_id": msg["message_id"],
                               "tenant_id": msg["tenant_id"], "event_type": 3,
                               "event_time": when + timedelta(hours=rng.randint(2, 72))})


def _reply(rng: random.Random, world: World, out: CampaignRows, camp: Dict[str, Any], sent: Dict[str, Any],
           contact: Dict[str, Any], inbox: str, tally: Dict[str, int]) -> int:
    reply_at = sent["sent_at"] + timedelta(hours=rng.randint(1, 72))
    key = (contact["prospect_id"], inbox)  # one conversation per (contact, inbox): uq_conversations_prospect_inbox
    conv = out.conversation_index.get(key)
    if conv is None:
        conv = {
            "id": new_id(rng), "tenant_id": world.tenant_id, "prospect_id": contact["prospect_id"],
            "inbox_id": inbox, "subject": f"Re: {sent['subject']}"[:255],
            "status": rng.choice(("OPEN", "OPEN", "CLOSED", "SNOOZED")), "is_unread": rng.random() < 0.5,
            "last_message_at": reply_at, "created_at": sent["sent_at"], "updated_at": reply_at,
        }
        out.conversation_index[key] = conv
        out.conversations.append(conv)
    else:
        conv["last_message_at"] = conv["updated_at"] = max(conv["last_message_at"], reply_at)
    conv_id = conv["id"]
    sent["conversation_id"] = conv_id
    inbound = dict(_message(rng, camp, {"sequence_id": None, "step_number": 0}, contact, inbox, "SENT", reply_at))
    inbound.update({"direction": "INBOUND", "sequence_id": None, "conversation_id": conv_id,
                    "subject": f"Re: {sent['subject']}", "to_email": sent["from_email"],
                    "from_email": contact["email"], "final_status": None, "delivered_at": None,
                    "body_text": rng.choice(("Thanks, interested. Can we talk next week?",
                                             "Not right now, maybe next quarter.",
                                             "Please remove me from your list.",
                                             "I'm out of office until Monday."))})
    out.messages.append(inbound)
    out.events.append({"event_id": new_id(rng), "message_id": sent["message_id"],
                       "tenant_id": sent["tenant_id"], "event_type": 4,
                       "event_time": reply_at})
    tally["replied"] += 1
    return 1


def _campaign_row(rng: random.Random, world: World, i: int, now: datetime) -> Dict[str, Any]:
    creators = [u["user_id"] for u in world.users if u["role"] in ("SUPER_ADMIN", "ADMIN", "MANAGER")]
    status = CAMPAIGN_STATUSES[i % len(CAMPAIGN_STATUSES)]
    created = now - timedelta(days=rng.randint(90, 200))
    return {
        "campaign_id": new_id(rng), "tenant_id": world.tenant_id,
        "campaign_name": f"{rng.choice(COMPANY_WORDS)} {rng.choice(('Outreach', 'Nurture', 'Launch', 'Renewal', 'Webinar'))} {i + 1:02d}",
        "campaign_description": "Seeded for the V2 load test", "status": status,
        "paused_reason": "Paused by manager" if status == "PAUSED" else None,
        "paused_at": now - timedelta(days=3) if status == "PAUSED" else None, "auto_paused": False,
        "created_by": rng.choice(creators), "sender_name": "Load Sender", "unsubscribe_mode": "plain",
        "respect_timezone": True, "campaign_timezone": "UTC", "sending_mode": "spread", "min_gap_minutes": 2,
        "batch_gap_minutes": 30, "start_date": (now - timedelta(days=85)).date(),
        "end_date": (now + timedelta(days=60)).date(), "created_at": created, "updated_at": created,
        "row_version": 1,
    }


def gen_campaigns(rng: random.Random, world: World, n: int, target_messages: int, now: datetime) -> CampaignRows:
    out = CampaignRows()
    eligible = [c for c in world.contacts if c["consent_status"] == "OPT_IN" and c["is_valid_email"]]
    camps = [_campaign_row(rng, world, i, now) for i in range(n)]
    running = [c for c in camps if c["status"] != "DRAFT"]
    quota = target_messages // max(1, len(running))
    for camp in camps:
        out.campaigns.append(camp)
        seqs = [{"sequence_id": new_id(rng), "campaign_id": camp["campaign_id"], "tenant_id": camp["tenant_id"],
                 "step_number": s,
                 "wait_days": 0 if s == 1 else rng.choice((2, 3, 4, 7)), "stop_on_reply": True,
                 "stop_on_bounce": True, "send_start_hour": 9, "send_end_hour": 17, "row_version": 1}
                for s in range(1, rng.randint(3, 5) + 1)]
        out.sequences.extend(seqs)
        for inbox in rng.sample(world.inboxes, 2):
            out.campaign_inboxes.append({"campaign_id": camp["campaign_id"], "inbox_id": inbox})
        tally = {"sent": 0, "opened": 0, "replied": 0, "bounced": 0}
        if camp["status"] != "DRAFT" and eligible:
            written = 0
            pool = rng.sample(eligible, min(len(eligible), quota))  # upper bound; usually far fewer used
            for contact in pool:
                if written >= quota:
                    break
                written += _enrol_one(rng, world, out, camp, seqs, contact, now, tally)
        out.metrics.append({"campaign_id": camp["campaign_id"], "sent_count": tally["sent"],
                            "opened_count": tally["opened"], "replied_count": tally["replied"],
                            "bounced_count": tally["bounced"]})
    return out


LEAD_STAGE_MIX = (("NEW", 30), ("CONTACTED", 25), ("ENGAGED", 15), ("SQL", 10), ("CONVERTED", 12),
                  ("DISQUALIFIED", 8))


def gen_sales(rng: random.Random, world: World, now: datetime) -> tuple:
    leads: List[Dict[str, Any]] = []
    opps: List[Dict[str, Any]] = []
    users = [u["user_id"] for u in world.users]
    open_stages = [s for s in world.stages if not s["is_won"] and not s["is_lost"]]
    sample = rng.sample(world.contacts, max(1, len(world.contacts) * 3 // 100))
    for c in sample:
        stage = weighted(rng, LEAD_STAGE_MIX)
        created = c["created_at"] + timedelta(days=rng.randint(0, 20))
        lead = {
            "lead_id": new_id(rng), "tenant_id": world.tenant_id, "prospect_id": c["prospect_id"],
            "account_id": c["account_id"], "owner_id": c["owner_id"] or rng.choice(users),
            "source": c["lead_source"], "stage": stage, "stage_changed_at": created + timedelta(days=3),
            "qualified_at": created + timedelta(days=5) if stage in ("SQL", "CONVERTED") else None,
            "converted_at": created + timedelta(days=9) if stage == "CONVERTED" else None,
            "disqualified_reason": "No budget" if stage == "DISQUALIFIED" else None,
            "created_at": created, "updated_at": created, "row_version": 1, "opportunity_id": None,
        }
        leads.append(lead)
        if stage == "CONVERTED" or (stage == "SQL" and rng.random() < 0.3):
            st = rng.choice(world.stages if stage == "CONVERTED" else open_stages)
            status = "WON" if st["is_won"] else "LOST" if st["is_lost"] else "OPEN"
            opp = {
                "opportunity_id": new_id(rng), "tenant_id": world.tenant_id,
                "name": f"{c['company_name'] or c['last_name']} - {rng.choice(('Platform', 'Pilot', 'Expansion', 'Renewal'))}",
                "account_id": c["account_id"], "prospect_id": c["prospect_id"], "lead_id": lead["lead_id"],
                "owner_id": lead["owner_id"], "stage_id": st["stage_id"], "amount": rng.randint(2, 400) * 1000,
                "close_date": (now + timedelta(days=rng.randint(-90, 180))).date(),
                "client_type": rng.choice(("NEW", "NEW", "EXISTING")), "status": status,
                "forecast_category": "CLOSED" if st["is_won"] else "OMITTED" if st["is_lost"] else "PIPELINE",
                "forecast_category_manual": False,
                "closed_at": now - timedelta(days=rng.randint(1, 60)) if status != "OPEN" else None,
                "created_at": created + timedelta(days=9), "updated_at": created + timedelta(days=9),
                "row_version": 1,
            }
            lead["opportunity_id"] = opp["opportunity_id"]
            opps.append(opp)
    return leads, opps


# ── Database writes ─────────────────────────────────────────────────────────

def chunks(rows: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


def strip_private(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]


def normalise_keys(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """executemany needs every row to carry the same keys."""
    keys = sorted({k for r in rows for k in r})
    return [{k: r.get(k) for k in keys} for r in rows]


def missing_tenant(table_name: str, rows: Iterable[Dict[str, Any]]) -> int:
    """Rows of a child table (alembic 0028: tenant_id NOT NULL) that carry no tenant_id. The ORM's before_flush
    listener (app/models/tenant_fill.py) never sees a Core insert, so every generator sets it itself."""
    from app.models.tenant_fill import TENANT_PARENTS
    if table_name not in TENANT_PARENTS:
        return 0
    return sum(1 for r in rows if not r.get("tenant_id"))


def bulk_insert(conn, table, rows: List[Dict[str, Any]], batch: int, log: Callable[[str], None]) -> int:
    if not rows:
        return 0
    missing = missing_tenant(table.name, rows)
    if missing:
        raise ValueError(f"{table.name}: {missing} rows without tenant_id (Core inserts bypass tenant_fill)")
    started = time.perf_counter()
    clean = normalise_keys(strip_private(rows))
    for part in chunks(clean, batch):
        conn.execute(table.insert(), list(part))
    log(f"  {table.name:<30} {len(clean):>8} rows  {time.perf_counter() - started:6.1f}s")
    return len(clean)


def custom_field_rows(rng: random.Random, world: World) -> List[Dict[str, Any]]:
    specs = (("region", "Region", "SELECT", list(REGIONS)), ("tier", "Customer tier", "SELECT", list(TIERS)),
             ("arr", "ARR (USD)", "NUMBER", None), ("renewal_date", "Renewal date", "DATE", None))
    return [{"field_id": new_id(rng), "tenant_id": world.tenant_id, "field_key": key, "label": label,
             "field_type": ftype, "options": opts, "group_name": "Load test", "required": False, "sort_order": i,
             "is_active": True, "is_system": False, "max_length": None}  # 0031_property_safety columns
            for i, (key, label, ftype, opts) in enumerate(specs)]


def contact_company_rows(rng: random.Random, world: World) -> List[Dict[str, Any]]:
    return [{"link_id": new_id(rng), "tenant_id": world.tenant_id, "prospect_id": c["prospect_id"],
             "account_id": c["account_id"], "is_primary": True, "created_at": c["created_at"]}
            for c in world.contacts if c["account_id"]]


def inbox_rows(rng: random.Random, world: World) -> List[Dict[str, Any]]:
    rows = []
    for name in ("sales", "outreach", "hello"):
        iid = new_id(rng)
        world.inboxes.append(iid)
        rows.append({"inbox_id": iid, "tenant_id": world.tenant_id, "email_address": f"{name}@{USER_DOMAIN}",
                     "provider": "ses", "status": "ACTIVE", "daily_limit": 500, "warmup_enabled": False})
    return rows


def seed(conn, cfg: Config, log: Callable[[str], None]) -> World:
    from app.core.clock import utcnow
    from app.core.security import hash_password
    from app.models import (Account, Campaign, CampaignProspect, ContactCompany, ContactFieldDefinition,
                            Conversation, EmailEvent, EmailMessage, EmailSequence, Lead, Opportunity, Prospect,
                            ProspectList, ProspectListMember, SalesStage, SendingInbox, Tenant, User)
    from app.models.join_tables import campaign_inboxes
    from app.models.metrics import CampaignMetricsRealtime

    rng = random.Random(cfg.seed)
    now = utcnow().replace(microsecond=0)
    world = World(tenant_id=new_id(rng))
    ins = lambda model, rows: bulk_insert(conn, getattr(model, "__table__", model), rows, cfg.batch, log)  # noqa: E731

    world.counts["tenants"] = ins(Tenant, [{"tenant_id": world.tenant_id, "tenant_name": TENANT_NAME,
                                            "status": "ACTIVE", "require_mfa": False}])
    world.users = gen_users(rng, world, hash_password(cfg.password), now)
    world.counts["users"] = ins(User, world.users)
    world.counts["sending_inboxes"] = ins(SendingInbox, inbox_rows(rng, world))
    world.counts["contact_field_definitions"] = ins(ContactFieldDefinition, custom_field_rows(rng, world))
    world.accounts = gen_accounts(rng, world, cfg.accounts, now)
    world.counts["accounts"] = ins(Account, world.accounts)
    world.contacts = gen_contacts(rng, world, cfg.contacts, cfg.dup_rate, now)
    world.counts["prospects"] = ins(Prospect, world.contacts)
    world.counts["contact_companies"] = ins(ContactCompany, contact_company_rows(rng, world))
    lists, members = gen_lists(rng, world, cfg.lists, now)
    world.counts["prospect_lists"] = ins(ProspectList, lists)
    world.counts["prospect_list_members"] = ins(ProspectListMember, members)
    camp = gen_campaigns(rng, world, cfg.campaigns, cfg.messages, now)
    world.campaigns = camp.campaigns
    world.counts["campaigns"] = ins(Campaign, camp.campaigns)
    world.counts["campaign_inboxes"] = ins(campaign_inboxes, camp.campaign_inboxes)
    world.counts["email_sequences"] = ins(EmailSequence, camp.sequences)
    world.counts["campaign_prospects"] = ins(CampaignProspect, camp.enrolments)
    world.counts["conversations"] = ins(Conversation, camp.conversations)
    world.counts["email_messages"] = ins(EmailMessage, camp.messages)
    world.counts["email_events"] = ins(EmailEvent, camp.events)
    world.counts["campaign_metrics_realtime"] = ins(CampaignMetricsRealtime, camp.metrics)
    world.stages = gen_stages(rng, world)
    world.counts["sales_stages"] = ins(SalesStage, world.stages)
    leads, opps = gen_sales(rng, world, now)
    world.counts["leads"] = ins(Lead, leads)  # leads.opportunity_id carries no FK, so order does not matter
    world.counts["opportunities"] = ins(Opportunity, opps)
    return world


def manifest(world: World, cfg: Config, db_name: str) -> Dict[str, Any]:
    return {
        "database": db_name, "seed": cfg.seed, "tenant_id": world.tenant_id, "password": cfg.password,
        "users": [{"email": u["email"], "role": u["role"], "sales_level": u["sales_level"], "team": u["_team"]}
                  for u in world.users],
        "counts": world.counts,
        "duplicates_target_rate": cfg.dup_rate,
    }


def parse_args(argv: Optional[List[str]] = None) -> Config:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    d = Config()
    p.add_argument("--contacts", type=int, default=d.contacts)
    p.add_argument("--accounts", type=int, default=None, help="default: contacts / 50, at least 20")
    p.add_argument("--lists", type=int, default=d.lists)
    p.add_argument("--campaigns", type=int, default=d.campaigns)
    p.add_argument("--messages", type=int, default=None, help="target email_messages (default: 2 x contacts)")
    p.add_argument("--batch", type=int, default=d.batch, help="rows per INSERT batch")
    p.add_argument("--seed", type=int, default=d.seed)
    p.add_argument("--password", default=d.password, help="shared password of the seeded users")
    p.add_argument("--dup-rate", type=float, default=d.dup_rate)
    a = p.parse_args(argv)
    return Config(contacts=a.contacts, accounts=a.accounts or max(20, a.contacts // 50), lists=a.lists,
                  campaigns=a.campaigns, messages=a.messages if a.messages is not None else 2 * a.contacts,
                  batch=a.batch, seed=a.seed, password=a.password, dup_rate=a.dup_rate)


def main(argv: Optional[List[str]] = None) -> int:
    cfg = parse_args(argv)
    from sqlalchemy import inspect, text

    from app.core.config import settings
    from app.core.database import engine

    db_name = engine.url.database or ""
    bad_name = check_name(db_name)  # before connecting at all
    if bad_name:
        print(bad_name, file=sys.stderr)
        return 2
    with engine.connect() as conn:
        tables = inspect(conn).get_table_names()
        counts = {}
        for t in ("tenants", "users", "prospects"):
            if t in tables:
                counts[t] = conn.execute(text(f"SELECT COUNT(*) FROM `{t}`")).scalar() or 0  # nosec B608 - fixed names
    reason = check_target(db_name, tables, counts)
    if reason:
        print(reason, file=sys.stderr)
        return 2
    print(f"Seeding {db_name} on {settings.MYSQL_HOST}:{settings.MYSQL_PORT}: {cfg}".replace(cfg.password, "***"))
    started = time.perf_counter()
    with engine.begin() as conn:
        world = seed(conn, cfg, print)
    elapsed = time.perf_counter() - started
    out = ROOT / "reports" / "load" / f"{db_name}_seed.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    data = manifest(world, cfg, db_name)
    data["elapsed_seconds"] = round(elapsed, 1)
    out.write_text(json.dumps(data, indent=2, default=str))
    print(f"Done in {elapsed:.1f}s. Manifest: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
