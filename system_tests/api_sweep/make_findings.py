#!/usr/bin/env python3
"""Builds ENDPOINT_COVERAGE.md and FINDINGS.md from sweep_cases.json + run_meta.json + triage.json + API log evidence.
triage.json: {"rules": [{"match": "<substring of case id>", "category": "...", "severity": "...", "title": "...", "note": "..."}]}
(only labels failures; never turns a failure into a pass)."""
import collections, json, os, re, subprocess, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import coverage  # noqa: E402

cases = json.load(open(os.path.join(HERE, "sweep_cases.json")))["cases"]
meta = json.load(open(os.path.join(HERE, "run_meta.json")))
inv = json.load(open(os.path.join(HERE, "ENDPOINTS.json")))["endpoints"]
triage = json.load(open(os.path.join(HERE, "triage.json")))["rules"] if os.path.exists(os.path.join(HERE, "triage.json")) else []


def classify(c):
    for r in triage:
        if r["match"] in c["id"] or (r.get("detail") and r["detail"] in c["detail"]):
            return r
    return None


for c in cases:
    if c["status"] == "FAIL":
        r = classify(c)
        if r:
            c["category"] = r.get("category", c["category"]); c["sev"] = r.get("severity", ""); c["group"] = r.get("title", "")
            c["note"] = r.get("note", "")
        elif "null not allowed" in c["detail"] and c["check"] == "SCHEMA":
            c["group"] = "OpenAPI response models declare non-nullable fields that are returned as null"; c["sev"] = "Low"
        elif "returned 500" in c["detail"] or "got 5" in c["detail"] or re.search(r"status 5\d\d", c["detail"]):
            c["group"] = "Unhandled 5xx"; c["sev"] = "High"

# reasons for endpoints not 2xx-covered
reasons = {}
for e in inv:
    if e["roles"] == ["PLATFORM_ADMIN"] or e["path"].startswith("/scheduler") and e["method"] != "GET":
        reasons[e["id"]] = "platform-admin-only (no operator user; only 401/403 checked)"
    elif e["path"] in ("/api/auth/google", "/api/auth/google/callback", "/connections/{provider}/start") or "oauth/microsoft/start" in e["path"]:
        reasons[e["id"]] = "environment (OAuth provider not configured)"
    elif "regenerat" in e["path"] or "generations" in e["path"] or "wizard-executions" in e["path"] or "inboxes/{inbox_id}/sync" in e["path"] or "wizard-creations" in e["path"] or "activations" in e["path"] or e["path"] == "/campaigns/lists":
        reasons[e["id"]] = "environment/defect (AI/IMAP dependency or product 500, see FINDINGS)"
    elif e["path"].startswith("/api/auth/"):
        reasons[e["id"]] = "state-changing auth flow / rate-limited; see AUTH cases"
    elif e["path"].endswith("/metrics"):
        reasons[e["id"]] = "not measurable: the metrics middleware does not count paths ending in /metrics (the sweep did get 2xx, see HAPPY case)"
    else:
        reasons[e["id"]] = "see FINDINGS"
stats = coverage.report(reasons=reasons, cases=cases)

by = collections.Counter((c["check"], c["status"]) for c in cases)
checks = sorted({c["check"] for c in cases})
fails = [c for c in cases if c["status"] == "FAIL"]
groups = collections.defaultdict(list)
for c in fails:
    groups[c.get("group") or f"{c['check']}: {c['kind'] or ''}".strip()].append(c)

def log_evidence():
    out = {}
    try:
        txt = subprocess.run(f"docker logs vector-api-1 --since {meta['started_utc']}Z 2>&1", shell=True, capture_output=True, text=True).stdout
    except Exception:
        return out
    cur = None
    for l in txt.splitlines():
        m = re.search(r'"([A-Z]+) (/[^ ?]*)[^"]*" 500 ', l)
        if m:
            cur = (m.group(1), re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27}", "{id}", m.group(2))); out.setdefault(cur, [])
        elif cur is not None:
            if re.search(r'File "/app/app/(routers|services)', l) or re.match(r"^[A-Za-z_.]+(Error|Exception)", l):
                if len(out[cur]) < 4 and l.strip() not in out[cur]:
                    out[cur].append(l.strip())
            elif l.startswith("INFO:"):
                cur = None
    return out

ev = log_evidence()
L = ["# API sweep findings", "",
     f"Run: {meta['calls']} sweep calls, {meta['requests']} HTTP requests, {meta['secs']}s. Cases: {len(cases)} "
     f"(PASS {sum(c['status']=='PASS' for c in cases)}, FAIL {len(fails)}, BLOCKED {sum(c['status']=='BLOCKED' for c in cases)}).", "",
     "## Coverage", "", f"- Sweep only: {stats['sweep_hit']}/{stats['n']} endpoints hit ({100*stats['sweep_hit']/stats['n']:.1f}%), "
     f"{stats['sweep_2xx']}/{stats['n']} with a 2xx ({100*stats['sweep_2xx']/stats['n']:.1f}%).",
     f"- Since API start (all suites): {stats['start_hit']}/{stats['n']} hit ({100*stats['start_hit']/stats['n']:.1f}%), {stats['start_2xx']} with 2xx ({100*stats['start_2xx']/stats['n']:.1f}%).",
     "- Per-endpoint table with reasons: `ENDPOINT_COVERAGE.md`.", "", "## Counts per check type", "",
     "| Check | PASS | FAIL | BLOCKED |", "|---|---|---|---|"]
for ch in checks:
    L.append(f"| {ch} | {by[(ch,'PASS')]} | {by[(ch,'FAIL')]} | {by[(ch,'BLOCKED')]} |")
L += ["", "BLOCKED = response schema not declared in OpenAPI (cannot be validated) or external dependency unavailable.", "",
      "## Failures by group (defects first)", ""]
order = sorted(groups.items(), key=lambda kv: ({"High": 0, "Critical": -1, "Medium": 1, "Low": 2}.get(kv[1][0].get("sev", ""), 3), -len(kv[1])))
for g, cs in order:
    c0 = cs[0]
    L += [f"### {g}  ({len(cs)} case(s); severity {c0.get('sev') or 'n/a'}; category {c0['category']})", ""]
    if c0.get("note"):
        L += [c0["note"], ""]
    for c in cs[:8]:
        L.append(f"- `{c['id']}`: {c['detail'][:260]}")
    if len(cs) > 8:
        L.append(f"- ... and {len(cs)-8} more")
    L.append("")
L += ["## Unhandled 500s with server-side location (from API logs)", ""]
agg = collections.OrderedDict()
for e in meta["errors"]:
    agg.setdefault(e["ep"], []).append(e)
for epid, es in agg.items():
    m, p = epid.split(" ", 1)
    e0 = es[0]
    frames = []
    for e in es:
        k = (m, re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27}", "{id}", e["path"].split("?")[0]))
        for f in ev.get(k, []):
            if f not in frames: frames.append(f)
    tags = sorted({e["tag"] for e in es})
    L.append(f"- `{epid}` x{len(es)} probes {tags[:6]} -> {e0['status']}; example body `{(e0['body'] or '')[:90]}` ; server: {'; '.join(frames[-4:])[:420]}")
open(os.path.join(HERE, "FINDINGS_generated.md"), "w").write("\n".join(L) + "\n")
print(stats, len(fails), "groups", len(groups))
