"""Sales-hierarchy visibility spot checks (BR-SH-02): MANAGER(l2)<-AGENT(l3) in one branch, MGR2(l2)<-AGENT_OTHER(l3) in another."""
from sweeplib import http as _h
from engine import _trunc

USERS = ["AGENT", "AGENT_OTHER", "MANAGER", "MGR2"]
VISIBLE = {  # who sees whose records
    "SUPER_ADMIN": set(USERS), "ADMIN": set(USERS), "MANAGER": {"MANAGER", "AGENT"}, "MGR2": {"MGR2", "AGENT_OTHER"},
    "AGENT": {"AGENT"}, "AGENT_OTHER": {"AGENT_OTHER"}, "AGENT_NOPERM": set(),
}


def H(m, p, tok, body=None, query=None):
    s, b, h, d = _h(m, p, body, tok, query=query)
    return s, b


def hier_checks(E, A):
    mk = "hr" + A.ids["marker"][-6:]
    recs = {k: {} for k in ("contacts", "accounts", "leads", "opportunities", "tasks")}
    for u in USERS:
        tk = A.tok[u]
        s, b = H("POST", "/contacts", tk, {"email": f"{u.lower()}@{mk}{u.lower().replace('_','')}.example.com", "first_name": u, "last_name": mk, "company_name": f"{mk} {u}"})
        if s != 201:
            E.rec.rec("POST /contacts", "POST", "/contacts", "HIER", f"setup-{u}", False, f"{s} {_trunc(b,200)}", category="test-issue")
            continue
        recs["contacts"][u] = b["prospect_id"]
        recs["accounts"][u] = b.get("account_id")
        s, b = H("POST", "/leads", tk, {"prospect_id": recs["contacts"][u], "source": "WEB"})
        recs["leads"][u] = b.get("lead_id") if s == 201 else None
        s, b = H("POST", "/opportunities", tk, {"name": f"{mk} opp {u}", "prospect_id": recs["contacts"][u], "amount": 10})
        recs["opportunities"][u] = b.get("opportunity_id") if s == 201 else None
        s, b = H("POST", "/tasks", tk, {"title": f"{mk} task {u}"})
        recs["tasks"][u] = b.get("task_id") if s == 201 else None
    cfg = {"contacts": ("/contacts", "prospect_id", "GET /contacts", "GET /contacts/{prospect_id}"),
           "accounts": ("/accounts", "account_id", "GET /accounts", "GET /accounts/{account_id}"),
           "leads": ("/leads", "lead_id", "GET /leads", "GET /leads/{lead_id}"),
           "opportunities": ("/opportunities", "opportunity_id", "GET /opportunities", "GET /opportunities/{opportunity_id}"),
           "tasks": ("/tasks", "task_id", "GET /tasks", None)}
    viewers = ["SUPER_ADMIN", "ADMIN", "MANAGER", "MGR2", "AGENT", "AGENT_OTHER"]
    for kind, (path, idkey, lep, gep) in cfg.items():
        for v in viewers:
            exp = {recs[kind][u] for u in VISIBLE[v] if recs[kind].get(u)}
            allids = {x for x in recs[kind].values() if x}
            q = {"page_size": 100}
            if kind == "tasks": q["scope"] = "all"
            if kind == "contacts": q["q"] = mk
            s, b = H("GET", path, A.tok[v], query=q)
            items = (b.get("items") if isinstance(b, dict) else b) or []
            seen = {i.get(idkey) for i in items if isinstance(i, dict)} & allids
            ok = s == 200 and seen == exp
            E.rec.rec(lep, "GET", path, "HIER", f"list-{v}", ok,
                      f"{v} sees {len(seen)} of the 4 seeded {kind}, expected {len(exp)}; unexpected={len(seen-exp)} missing={len(exp-seen)} status={s}",
                      evidence={"seen": sorted(seen), "expected": sorted(exp)}, title=f"{lep} hierarchy visibility for {v}")
            if gep:
                for u in USERS:
                    rid = recs[kind].get(u)
                    if not rid: continue
                    s, b = H("GET", path + "/" + rid, A.tok[v])
                    shouldsee = u in VISIBLE[v]
                    ok = (s == 200) if shouldsee else (s in (403, 404))
                    E.rec.rec(gep, "GET", path + "/{id}", "HIER", f"get-{v}-of-{u}", ok,
                              f"{v} GET {kind} owned by {u} -> {s}, expected {'200' if shouldsee else '403/404'}", title=f"{gep} {v} -> {u}'s record")
    # reports
    ep = "GET /sales-reports/leaderboard"
    for v, member, exp in (("AGENT", "AGENT_OTHER", (403, 404)), ("MANAGER", "AGENT_OTHER", (403, 404)), ("MANAGER", "AGENT", (200,)),
                           ("AGENT", "MANAGER", (403, 404))):
        s, b = H("GET", "/sales-reports/leaderboard", A.tok[v], query={"member": A.uid[member]})
        E.rec.rec(ep, "GET", "/sales-reports/leaderboard", "HIER", f"member-{v}-{member}", s in exp, f"{v} member={member} -> {s}, expected {exp}", title=f"{ep} {v} requesting {member}")
    for v, member, exp in (("MANAGER", "AGENT_OTHER", (403,)), ("MANAGER", "AGENT", (200,)), ("ADMIN", "AGENT_OTHER", (200,))):
        s, b = H("GET", "/reports/dashboard-summary", A.tok[v], query={"user_id": A.uid[member]})
        E.rec.rec("GET /reports/dashboard-summary", "GET", "/reports/dashboard-summary", "HIER", f"user-{v}-{member}", s in exp, f"{v} user_id={member} -> {s}, expected {exp}", title=f"dashboard-summary scope {v}->{member}")
