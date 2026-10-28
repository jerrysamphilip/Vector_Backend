"""System tests for contact scoring on fit and engagement (BR-CM-43 / CM-083).

The scoring model endpoints (admins only), validation and bounds, scores on create, list
column / sort / grade and range filters, the per-contact breakdown, the engagement hook and
grade-change history, the async full recompute (scores stored without touching updated_at),
email engagement picked up by a recompute, and visibility (hierarchy and tenants).

Runs against the live stack; creates its own workspaces.  python3 run_contact_scoring.py
Writes results.json / results.md next to this file.
"""
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common import Results, Workspace, req, sql  # noqa: E402

R = Results("contact_scoring", os.path.dirname(os.path.abspath(__file__)))
W = Workspace("cs")
OWNER = W.token
AGENT = W.add_user("AGENT", "Casey")
MANAGER = W.add_user("MANAGER", "Morgan")
W2 = Workspace("cs-b")
rid = W.rid

TAG_MODEL = {
    "fit_rules": [
        {"id": "hot", "label": "Hot tag", "field": "tags", "operator": "in", "value": "hot", "points": 80},
        {"id": "warm", "label": "Warm tag", "field": "tags", "operator": "in", "value": "warm", "points": 40},
    ],
    "fit_weight": 100, "engagement_weight": 0, "half_life_days": 0, "lookback_days": 180,
    "grade_thresholds": {"A": 75, "B": 50, "C": 25},
}
ENGAGEMENT_MODEL = {
    "fit_rules": [], "fit_weight": 0, "engagement_weight": 100, "half_life_days": 0, "lookback_days": 180,
    "grade_thresholds": {"A": 75, "B": 50, "C": 10},
}


def mk_contact(tok, local, **kw):
    s, b = req("POST", "/contacts", {"email": f"{local}.{rid}@cs-{rid}.io", **kw}, tok)
    assert s == 201, (s, b)
    return b


def wait_recomputed(tok, timeout=60):
    """Poll the model until no recompute is pending (the API runs it after the response)."""
    deadline = time.time() + timeout
    body = None
    while time.time() < deadline:
        s, body = req("GET", "/contacts/scoring-model", None, tok)
        if s == 200 and body.get("recompute_requested_at") is None and body.get("last_recomputed_at"):
            return body
        time.sleep(1)
    return body


def items(path):
    s, b = req("GET", path, None, OWNER)
    return s, [c["prospect_id"] for c in (b or {}).get("items", [])] if s == 200 else b


# ── Model ─────────────────────────────────────────────────────────
s, m = req("GET", "/contacts/scoring-model", None, OWNER)
R.check("CS-001", "Admin reads the scoring model, seeded with default fit rules and 7 engagement signals",
        s == 200 and len(m["model"]["fit_rules"]) >= 5 and len(m["signals"]) == 7
        and m["model"]["fit_weight"] + m["model"]["engagement_weight"] == 100, (s, str(m)[:300]))
R.check("CS-002", "Default fit rules cover seniority, size, revenue, industry, country, stage and email type",
        s == 200 and {"designation", "employees", "annual_revenue", "industry", "country", "lifecycle_stage", "email_type"}
        <= {r["field"] for r in m["model"]["fit_rules"]}, str(m)[:300])
s1, _ = req("GET", "/contacts/scoring-model", None, AGENT["token"])
s2, _ = req("PUT", "/contacts/scoring-model", TAG_MODEL, AGENT["token"])
s3, _ = req("POST", "/contacts/scoring-model/recompute", None, MANAGER["token"])
R.check("CS-003", "Agents and managers cannot read, change or recompute the model (403)", (s1, s2, s3) == (403, 403, 403), (s1, s2, s3))
s, b = req("PUT", "/contacts/scoring-model", {"fit_weight": 70, "engagement_weight": 70}, OWNER)
R.check("CS-004", "Weights that do not add up to 100 are rejected (400)", s == 400, (s, b))
s, b = req("PUT", "/contacts/scoring-model", {"fit_rules": [{"field": "industry", "operator": "in", "value": "x", "points": 500}]}, OWNER)
R.check("CS-005", "Rule points outside -100..100 are rejected (422)", s == 422, (s, str(b)[:200]))
s, b = req("PUT", "/contacts/scoring-model", {"fit_rules": [{"field": "industry", "operator": "in", "value": "x" * 600, "points": 5}]}, OWNER)
R.check("CS-006", "Oversize rule value is rejected (422)", s == 422, (s, str(b)[:200]))
s, b = req("PUT", "/contacts/scoring-model", {"fit_rules": [{"field": "password", "operator": "equals", "value": "x", "points": 5}]}, OWNER)
R.check("CS-007", "Unknown rule property is rejected (400)", s == 400, (s, b))
s, b = req("PUT", "/contacts/scoring-model", {"grade_thresholds": {"A": 20, "B": 50, "C": 10}}, OWNER)
R.check("CS-008", "Grade thresholds that do not decrease are rejected (400)", s == 400, (s, b))
s, b = req("PUT", "/contacts/scoring-model", TAG_MODEL, OWNER)
R.check("CS-009", "Admin saves a custom model", s == 200 and [r["id"] for r in b["model"]["fit_rules"]] == ["hot", "warm"], (s, str(b)[:300]))
wait_recomputed(OWNER)

# ── Scores on contacts, list ──────────────────────────────────────
marker = f"lst{uuid.uuid4().hex[:6]}"
hot = mk_contact(OWNER, f"{marker}hot", tags=["hot"])
warm = mk_contact(OWNER, f"{marker}warm", tags=["warm"])
cold = mk_contact(OWNER, f"{marker}cold")
R.check("CS-010", "A new contact is scored straight away (score, fit, engagement, grade on the record)",
        (hot.get("score"), hot.get("fit_score"), hot.get("engagement_score"), hot.get("score_grade")) == (80, 80, 0, "A"),
        {k: hot.get(k) for k in ("score", "fit_score", "engagement_score", "score_grade")})
s, got = items(f"/contacts?q={marker}&sort_by=score&sort_order=desc")
R.check("CS-011", "Contact list sorts by score (desc)", s == 200 and got == [hot["prospect_id"], warm["prospect_id"], cold["prospect_id"]], (s, got))
s, got = items(f"/contacts?q={marker}&sort_by=score&sort_order=asc")
R.check("CS-012", "Contact list sorts by score (asc)", s == 200 and got == [cold["prospect_id"], warm["prospect_id"], hot["prospect_id"]], (s, got))
s, got = items(f"/contacts?q={marker}&grade=A")
R.check("CS-013", "Filter by grade A", s == 200 and got == [hot["prospect_id"]], (s, got))
s, got = items(f"/contacts?q={marker}&grade=A,C")
R.check("CS-014", "Filter by several grades", s == 200 and sorted(got) == sorted([hot["prospect_id"], warm["prospect_id"]]), (s, got))
s, got = items(f"/contacts?q={marker}&score_min=30&score_max=50")
R.check("CS-015", "Filter by score range", s == 200 and got == [warm["prospect_id"]], (s, got))
s1, _ = req("GET", "/contacts?grade=Z", None, OWNER)
s2, _ = req("GET", "/contacts?score_min=101", None, OWNER)
s3, _ = req("GET", "/contacts?sort_by=score;drop", None, OWNER)
R.check("CS-016", "Bad grade 400, out-of-range score 422, bad sort 422", (s1, s2, s3) == (400, 422, 422), (s1, s2, s3))
s, meta = req("GET", "/contacts/meta", None, OWNER)
R.check("CS-017", "Score is an available list / export column", s == 200 and "score" in [c["key"] for c in meta["columns"]], (s, str(meta)[:200]))

# ── Breakdown ─────────────────────────────────────────────────────
s, sc = req("GET", f"/contacts/{hot['prospect_id']}/score", None, OWNER)
R.check("CS-018", "Score breakdown shows which fit rules contributed",
        s == 200 and [r["id"] for r in sc["fit_rules"] if r["matched"]] == ["hot"] and sc["score"] == 80
        and sc["stored"]["score"] == 80 and len(sc["engagement_signals"]) == 7, (s, str(sc)[:300]))

# ── Engagement hook and grade history ─────────────────────────────
s, _ = req("PUT", "/contacts/scoring-model", ENGAGEMENT_MODEL, OWNER)
wait_recomputed(OWNER)
eng = mk_contact(OWNER, "engaged")
s, _ = req("POST", f"/contacts/{eng['prospect_id']}/activities", {"activity_type": "MEETING", "subject": "Demo"}, OWNER)
s2, d = req("GET", f"/contacts/{eng['prospect_id']}", None, OWNER)
R.check("CS-019", "Logging a meeting raises engagement and the score right away",
        s == 201 and s2 == 200 and (d.get("engagement_score"), d.get("score_grade")) == (15, "C"),
        (s, s2, d.get("engagement_score"), d.get("score_grade")))
s, h = req("GET", f"/contacts/{eng['prospect_id']}/history?field=score_grade", None, OWNER)
hist = [(i["old_value"], i["new_value"], i["source"]) for i in (h or {}).get("items", [])]
R.check("CS-020", "A grade change writes a property history row (D -> C, SYSTEM)", s == 200 and hist == [("D", "C", "SYSTEM")], (s, hist))

# Email engagement (opens / clicks / replies) is counted on recompute
mid = str(uuid.uuid4())
sql(f"INSERT INTO email_messages (message_id, prospect_id, tenant_id, direction, status, to_email, subject, sent_at) "
    f"SELECT '{mid}', prospect_id, tenant_id, 'OUTBOUND', 'SENT', 'x@example.com', 'Hi', UTC_TIMESTAMP() "
    f"FROM prospects WHERE prospect_id = '{eng['prospect_id']}'")
for etype in (2, 2, 3):
    sql(f"INSERT INTO email_events (event_id, message_id, tenant_id, event_type, event_time) "
        f"SELECT '{uuid.uuid4()}', message_id, tenant_id, {etype}, UTC_TIMESTAMP() FROM email_messages WHERE message_id = '{mid}'")
s, sc = req("POST", f"/contacts/{eng['prospect_id']}/score/recompute", None, OWNER)
sig = {x["signal"]: x["count"] for x in (sc or {}).get("engagement_signals", [])} if s == 200 else {}
R.check("CS-021", "Email opens (one per email) and clicks count towards engagement",
        s == 200 and sig.get("email_opened") == 1 and sig.get("email_clicked") == 1 and sc["engagement"] == 15 + 3 + 6, (s, sig, (sc or {}).get("engagement")))

# ── Async recompute ───────────────────────────────────────────────
before_updated = sql(f"SELECT updated_at FROM prospects WHERE prospect_id='{cold['prospect_id']}'")
sql(f"UPDATE prospects SET score=NULL, score_grade=NULL, scored_at=NULL, updated_at=updated_at WHERE prospect_id='{cold['prospect_id']}'")
s, b = req("POST", "/contacts/scoring-model/recompute", None, OWNER)
R.check("CS-022", "Recompute now is accepted asynchronously (202, queued)", s == 202 and b.get("status") == "queued" and b.get("contacts", 0) >= 4, (s, b))
done = wait_recomputed(OWNER)
scored = sql(f"SELECT score FROM prospects WHERE prospect_id='{cold['prospect_id']}'")
R.check("CS-023", "The background recompute rescores every contact", scored == "0" and (done or {}).get("stats", {}).get("scored") == (done or {}).get("stats", {}).get("contacts"),
        (scored, (done or {}).get("stats")))
after_updated = sql(f"SELECT updated_at FROM prospects WHERE prospect_id='{cold['prospect_id']}'")
R.check("CS-024", "Rescoring does not change the contact's Last updated", before_updated == after_updated, (before_updated, after_updated))

# ── Visibility ────────────────────────────────────────────────────
own = mk_contact(AGENT["token"], "agentown")
s1, _ = req("GET", f"/contacts/{own['prospect_id']}/score", None, AGENT["token"])
s2, _ = req("GET", f"/contacts/{hot['prospect_id']}/score", None, AGENT["token"])
R.check("CS-025", "An agent reads the score of their own contact but not of others' (404)", (s1, s2) == (200, 404), (s1, s2))
s1, _ = req("GET", f"/contacts/{hot['prospect_id']}/score", None, W2.token)
s2, _ = req("POST", f"/contacts/{hot['prospect_id']}/score/recompute", None, W2.token)
R.check("CS-026", "Another workspace cannot read or recompute the score (404)", (s1, s2) == (404, 404), (s1, s2))
s, m2 = req("GET", "/contacts/scoring-model", None, W2.token)
R.check("CS-027", "Each workspace has its own model", s == 200 and m2["model"]["fit_rules"] != TAG_MODEL["fit_rules"]
        and m2["model"]["fit_weight"] == 50, (s, str(m2)[:200]))
s, b = req("GET", f"/contacts?sort_by=score&page_size=200", None, AGENT["token"])
ids = [c["prospect_id"] for c in (b or {}).get("items", [])]
R.check("CS-028", "An agent's score-sorted list holds only their own contacts", s == 200 and own["prospect_id"] in ids and hot["prospect_id"] not in ids, (s, len(ids)))

R.write()
c = {k: sum(1 for r in R.rows if r["status"] == k) for k in ("PASS", "FAIL", "BLOCKED")}
print("COUNTS", c)
