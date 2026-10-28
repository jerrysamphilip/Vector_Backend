"""System tests for A/B testing of email subject lines (BRD v2.0 BR-SF-18 / RP-104).

Runs against the live stack (API_URL, MySQL via docker exec - see ../common.py) and creates
its own two workspaces. The campaign is launched with a start date 20 days out, so nothing is
actually sent; emails are then marked SENT and opened / clicked / replied with SQL inside our
own tenant, as the scheduler and the tracking endpoints would.

    python3 run_ab_testing.py      (writes results.json / results.md here)
"""
import os
import sys
from collections import Counter
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common import Results, Workspace, req, sql  # noqa: E402

R = Results("ab_testing", os.path.dirname(os.path.abspath(__file__)))
W = Workspace("ab")
OWNER = W.token
MANAGER = W.add_user("MANAGER", "Abby")
# AGENT holds manage_campaigns by default; this one has it revoked to exercise the permission check
AGENT = W.add_user("AGENT", "Abe", custom_permissions="[]")
W2 = Workspace("ab-b")
rid = W.rid
FUTURE = (date.today() + timedelta(days=20)).isoformat()
MIN_SENDS = 30
N_PROSPECTS = 64  # 32 per variant: 30 get "sent", 2 stay scheduled


def q(s):
    return "'" + str(s).replace("\\", "\\\\").replace("'", "''") + "'"


def rows(query):
    out = sql(query)
    return [line.split("\t") for line in out.splitlines()] if out else []


s, b = W.set_sender_identity()
assert s == 200, ("sender identity", s, b)

# ── fixture: a one-step campaign with N_PROSPECTS enrolled ─────────
s, c = req("POST", "/campaigns", {"campaign_name": f"AB System {rid}", "start_date": FUTURE,
                                  "respect_timezone": False, "campaign_timezone": "UTC"}, OWNER)
assert s == 201, ("campaign", s, c)
CID = c["campaign_id"]
s, st = req("POST", f"/campaigns/{CID}/sequences", {"step_number": 1, "wait_days": 0}, OWNER)
assert s == 201, ("step", s, st)
SEQ = st["sequence_id"]
s, t = req("POST", "/templates", {"campaign_id": CID, "sequence_id": SEQ,
                                  "subject": "Template subject {{first_name}}", "body": "Hello from the template"}, OWNER)
assert s == 201, ("template", s, t)
pids = []
for i in range(N_PROSPECTS):
    s, p = req("POST", "/contacts", {"email": f"ab{i}.{rid}@ab-{rid}.io", "first_name": f"Pro{i}",
                                     "company_name": "Acme"}, OWNER)
    assert s == 201, ("contact", s, p)
    pids.append(p["prospect_id"])
s, e = req("POST", f"/campaigns/{CID}/prospects", {"prospect_ids": pids}, OWNER)
assert s == 200 and e.get("enrolled_count") == N_PROSPECTS, ("enroll", s, e)
URL = f"/campaigns/{CID}/sequences/1/subject-variants"


def step_messages():
    return rows(f"SELECT message_id, IFNULL(subject_variant,''), status, subject FROM email_messages "
                f"WHERE sequence_id={q(SEQ)} ORDER BY prospect_id")


# ── AB-S001..: API surface and variant CRUD ─────────────────────────
s, spec = req("GET", "/openapi.json")
paths = list((spec or {}).get("paths", {})) if s == 200 else []
R.check("AB-S001", "OpenAPI lists the subject-variant endpoints (CRUD, stats, promote)",
        any(p.endswith("/subject-variants") for p in paths) and any(p.endswith("/subject-variants/stats") for p in paths)
        and any(p.endswith("/promote") and "subject-variants" in p for p in paths), [p for p in paths if "variant" in p])

s, a = req("POST", URL, {"subject": "Quick question, {{first_name}}"}, OWNER)
s2, bb = req("POST", URL, {"subject": "Idea for Acme, {{first_name}}"}, OWNER)
R.check("AB-S002", "Owner adds variants A and B to step 1", s == 201 and s2 == 201
        and a.get("label") == "A" and bb.get("label") == "B", (s, a, s2, bb))
s, lst = req("GET", URL, None, OWNER)
R.check("AB-S003", "Listing shows two variants and the test as active", s == 200 and lst.get("ab_active") is True
        and [v["label"] for v in lst.get("variants", [])] == ["A", "B"], (s, lst))

s1, _ = req("POST", URL, {"subject": "x", "weight": 0}, OWNER)
s2, _ = req("POST", URL, {"subject": "x", "weight": 101}, OWNER)
s3, _ = req("POST", URL, {"subject": "y" * 501}, OWNER)
s4, _ = req("POST", URL, {"subject": ""}, OWNER)
R.check("AB-S004", "Out-of-bounds weight / subject length / empty subject are 422",
        (s1, s2, s3, s4) == (422, 422, 422, 422), (s1, s2, s3, s4))
s, d = req("POST", URL, {"subject": "You are a winner, claim your prize"}, OWNER)
R.check("AB-S005", "A subject with a disallowed phrase fails the compliance check (400)", s == 400, (s, d))
s, d = req("POST", URL, {"subject": "Line one\nBcc: someone@x.io"}, OWNER)
R.check("AB-S006", "A multi-line subject (header injection) is refused (422)", s == 422, (s, d))
sc, _ = req("POST", URL, {"subject": "Third angle"}, OWNER)
sd, _ = req("POST", URL, {"subject": "Fourth angle"}, OWNER)
se, de = req("POST", URL, {"subject": "Fifth angle"}, OWNER)
R.check("AB-S007", "At most four variants per step (fifth is 409)", sc == 201 and sd == 201 and se == 409, (sc, sd, se, de))
s1, _ = req("DELETE", URL + "/C", None, OWNER)
s2, _ = req("DELETE", URL + "/D", None, OWNER)
R.check("AB-S008", "Unsent variants can be deleted (204)", s1 == 204 and s2 == 204, (s1, s2))

# ── AB-S010..: tenancy and permissions ──────────────────────────────
s, d = req("POST", URL, {"subject": "Agent subject"}, AGENT["token"])
R.check("AB-S010", "An agent without manage_campaigns cannot change variants (403)", s == 403, (s, d))
s, d = req("GET", URL + "/stats", None, AGENT["token"])
R.check("AB-S011", "An agent can read the variant stats (200)", s == 200, (s, d))
s1, _ = req("GET", URL, None, W2.token)
s2, _ = req("GET", URL + "/stats", None, W2.token)
s3, _ = req("POST", URL, {"subject": "Foreign"}, W2.token)
R.check("AB-S012", "Another workspace gets 404 on list / stats / create", (s1, s2, s3) == (404, 404, 404), (s1, s2, s3))

# ── AB-S020..: launch assigns variants evenly ───────────────────────
s, d = req("POST", f"/campaigns/{CID}/launch", None, OWNER)
R.check("AB-S020", "Campaign launches (start date in the future)", s == 200, (s, d))
msgs = step_messages()
split = Counter(m[1] for m in msgs)
R.check("AB-S021", "Launch assigns every email a variant, split evenly (32 / 32)",
        len(msgs) == N_PROSPECTS and split == {"A": N_PROSPECTS // 2, "B": N_PROSPECTS // 2}, split)
ok = all(m[3].startswith("Quick question, Pro") if m[1] == "A" else m[3].startswith("Idea for Acme, Pro") for m in msgs)
R.check("AB-S022", "Each email carries its variant's subject, personalised", ok, msgs[:3])
s, sched = req("GET", f"/campaigns/{CID}/schedule", None, OWNER)
labels = {p.get("subject_variant") for it in (sched or {}).get("schedule", []) for p in it.get("prospects", [])}
R.check("AB-S023", "Schedule view shows the variant per email", s == 200 and bool(labels & {"A", "B"}), (s, labels))

# ── AB-S030..: stats, sample size, winner ───────────────────────────
a_ids = [m[0] for m in msgs if m[1] == "A"]
b_ids = [m[0] for m in msgs if m[1] == "B"]


def mark_sent(ids):
    sql(f"UPDATE email_messages SET status='SENT', sent_at=UTC_TIMESTAMP() WHERE message_id IN ({','.join(q(i) for i in ids)})")


def events(ids, etype):
    values = ",".join(f"(UUID(), {q(i)}, (SELECT tenant_id FROM email_messages WHERE message_id={q(i)}), {etype}, "
                      f"UTC_TIMESTAMP())" for i in ids)
    sql(f"INSERT INTO email_events (event_id, message_id, tenant_id, event_type, event_time) VALUES {values}")


mark_sent(a_ids[:MIN_SENDS - 1] + b_ids[:MIN_SENDS - 1])
events(a_ids[:12], 2)
events(a_ids[:3], 2)          # repeat opens by the same prospects count once
events(b_ids[:6], 2)
events(a_ids[:3], 3)          # clicks
events(b_ids[:2], 4)          # replies
s, st = req("GET", URL + "/stats", None, OWNER)
rowsv = {v["label"]: v for v in (st or {}).get("variants", [])}
R.check("AB-S030", "Below 30 sends per variant no winner is named (insufficient_data)",
        s == 200 and st["winner"]["status"] == "insufficient_data" and st["winner"]["label"] is None, (s, st and st.get("winner")))
s, d = req("POST", URL + "/A/promote", None, OWNER)
R.check("AB-S031", "Promotion is refused before the sample size is reached (409)", s == 409, (s, d))

mark_sent([a_ids[MIN_SENDS - 1], b_ids[MIN_SENDS - 1]])
s, st = req("GET", URL + "/stats", None, OWNER)
rowsv = {v["label"]: v for v in (st or {}).get("variants", [])}
A_, B_ = rowsv.get("A", {}), rowsv.get("B", {})
R.check("AB-S032", "Variant A stats: sent 30, unique opened 12 (40%), clicked 3, scheduled 2",
        (A_.get("sent"), A_.get("opened"), A_.get("open_rate"), A_.get("clicked"), A_.get("click_rate"), A_.get("scheduled"))
        == (30, 12, 40.0, 3, 25.0, 2), A_)
R.check("AB-S033", "Variant B stats: sent 30, opened 6 (20%), replied 2 (6.67%)",
        (B_.get("sent"), B_.get("opened"), B_.get("open_rate"), B_.get("replied"), B_.get("reply_rate"))
        == (30, 6, 20.0, 2, 6.67), B_)
R.check("AB-S034", "Winner by open rate is A once both variants have 30 sends",
        st.get("winner", {}).get("status") == "winner" and st["winner"]["label"] == "A", st.get("winner"))

# ── AB-S040..: sent emails are frozen ───────────────────────────────
s, d = req("PATCH", URL + "/A", {"subject": "Changed after sending"}, OWNER)
R.check("AB-S040", "A variant that was sent cannot change its subject (409)", s == 409, (s, d))
s, d = req("DELETE", URL + "/B", None, OWNER)
R.check("AB-S041", "A variant that was sent cannot be deleted (409)", s == 409, (s, d))
before = {m[0]: m[3] for m in step_messages() if m[2] == "SENT"}

# ── AB-S050..: promote the winner ───────────────────────────────────
s, d = req("POST", URL + "/A/promote", None, MANAGER["token"])
R.check("AB-S050", "A manager who does not own the campaign cannot promote (403)", s == 403, (s, d))
s, d = req("POST", URL + "/B/promote", None, OWNER)
R.check("AB-S051", "Promoting a variant that is not the winner is 409", s == 409, (s, d))
s, d = req("POST", URL + "/A/promote", None, W2.token)
R.check("AB-S052", "Another workspace cannot promote (404)", s == 404, (s, d))
s, d = req("POST", URL + "/A/promote", None, OWNER)
R.check("AB-S053", "The owner promotes the winner; the two unsent B emails switch",
        s == 200 and (d or {}).get("promoted_label") == "A" and d.get("updated_messages") == 2, (s, d))
after = step_messages()
pending = [m for m in after if m[2] != "SENT"]
R.check("AB-S054", "All remaining unsent emails now use the winner's subject",
        len(pending) == 4 and all(m[1] == "A" and m[3].startswith("Quick question, Pro") for m in pending), pending)
R.check("AB-S055", "Already-sent emails (sent history) are unchanged",
        all({m[0]: m[3] for m in after if m[2] == "SENT"}.get(k) == v for k, v in before.items()) and len(before) == 60, len(before))
s, st = req("GET", URL + "/stats", None, OWNER)
R.check("AB-S056", "Stats report the promoted winner", s == 200 and st["winner"]["status"] == "promoted"
        and st["winner"]["label"] == "A", (s, st and st.get("winner")))
s, d = req("POST", URL, {"subject": "Too late"}, OWNER)
R.check("AB-S057", "After promotion the test is closed: adding a variant is 409", s == 409, (s, d))

# ── AB-S060..: sent history with the variant ────────────────────────
s, h = req("GET", URL + "/messages?label=B&limit=200", None, OWNER)
R.check("AB-S060", "Email list filtered to variant B shows its 30 sent emails with the B subject",
        s == 200 and h.get("total") == 30 and all(i["subject_variant"] == "B" and i["sent_at"] for i in h.get("items", [])),
        (s, h and h.get("total")))
s, _ = req("GET", URL + "/messages?limit=500", None, OWNER)
R.check("AB-S061", "Email list page size is bounded (limit > 200 is 422)", s == 422, s)
sent_pid = sql(f"SELECT prospect_id FROM email_messages WHERE sequence_id={q(SEQ)} AND status='SENT' LIMIT 1")
s, tl = req("GET", f"/contacts/{sent_pid}/timeline", None, OWNER)
if s == 200:
    sent_items = [i.get("email") or {} for i in (tl or {}).get("items", []) if i.get("kind") == "EMAIL_SENT"]
    R.check("AB-S062", "Contact email history carries the subject variant",
            any(e.get("subject_variant") in ("A", "B") for e in sent_items), sent_items[:2])
else:
    R.record("AB-S062", "Contact email history carries the subject variant", None,
             f"timeline endpoint returned {s}", "test-issue")

counts = R.write()
print(counts)
sys.exit(1 if counts["FAIL"] else 0)
