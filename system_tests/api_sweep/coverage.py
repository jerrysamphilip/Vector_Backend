#!/usr/bin/env python3
"""Coverage from Prometheus http_requests_total{method,route,status}: snapshot before/after the sweep.
usage: coverage.py snapshot <name>   |   coverage.py report   (writes ENDPOINT_COVERAGE.md)"""
import json, os, re, sys, urllib.request, collections
HERE = os.path.dirname(os.path.abspath(__file__))
API = os.environ.get("API_URL", "http://localhost:8191")
LINE = re.compile(r'^http_requests_total\{(.*)\}\s+([0-9.eE+]+)$')


def snapshot(name):
    txt = urllib.request.urlopen(API + "/metrics", timeout=30).read().decode()
    d = {}
    for l in txt.splitlines():
        m = LINE.match(l)
        if m:
            lab = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
            d[f'{lab.get("method")}|{lab.get("route")}|{lab.get("status")}'] = float(m.group(2))
    json.dump(d, open(os.path.join(HERE, f"metrics_{name}.json"), "w"))
    return d


def per_ep(delta):
    hit, ok = collections.Counter(), collections.Counter()
    for k, v in delta.items():
        m, r, st = k.split("|")
        if v <= 0: continue
        hit[(m, r)] += v
        if st.startswith("2"): ok[(m, r)] += v
    return hit, ok


def report(reasons=None, cases=None, extra_head=""):
    inv = json.load(open(os.path.join(HERE, "ENDPOINTS.json")))["endpoints"]
    before = json.load(open(os.path.join(HERE, "metrics_before.json")))
    after = json.load(open(os.path.join(HERE, "metrics_after.json")))
    delta = {k: v - before.get(k, 0) for k, v in after.items()}
    sh, so = per_ep(delta)
    ah, ao = per_ep(after)
    stat = collections.defaultdict(lambda: [0, 0])
    for c in cases or []:
        if c.get("ep"):
            stat[c["ep"]][0 if c["status"] == "PASS" else 1] += 1
    rows, n = [], len(inv)
    cs = sum(1 for e in inv if sh[(e["method"], e["path"])]); c2 = sum(1 for e in inv if so[(e["method"], e["path"])])
    as_, a2 = sum(1 for e in inv if ah[(e["method"], e["path"])]), sum(1 for e in inv if ao[(e["method"], e["path"])])
    for e in inv:
        k = (e["method"], e["path"])
        p, f = stat[e["id"]]
        why = "" if so[k] else (reasons or {}).get(e["id"], "UNEXPLAINED")
        rows.append(f"| {e['method']} | `{e['path']}` | {e['guard']} | {'Y' if sh[k] else 'N'} | {'Y' if so[k] else 'N'} | {p}/{f} | {'Y' if ah[k] else 'N'} | {'Y' if ao[k] else 'N'} | {why} |")
    out = ["# Endpoint coverage (from /metrics http_requests_total deltas)", "", extra_head,
           f"- Endpoints in OpenAPI: **{n}**",
           f"- Sweep only: hit **{cs}/{n} = {100*cs/n:.1f}%**; hit with 2xx **{c2}/{n} = {100*c2/n:.1f}%**",
           f"- Since API container start (all suites + sweep): hit **{as_}/{n} = {100*as_/n:.1f}%**; 2xx **{a2}/{n} = {100*a2/n:.1f}%**", "",
           "| Method | Path | Guard | Sweep hit | Sweep 2xx | Checks pass/fail | Since-start hit | Since-start 2xx | Reason if not 2xx in sweep |",
           "|---|---|---|---|---|---|---|---|---|"] + rows
    open(os.path.join(HERE, "ENDPOINT_COVERAGE.md"), "w").write("\n".join(out) + "\n")
    return dict(n=n, sweep_hit=cs, sweep_2xx=c2, start_hit=as_, start_2xx=a2)


if __name__ == "__main__":
    if sys.argv[1] == "snapshot": snapshot(sys.argv[2])
    else: print(report(cases=json.load(open(os.path.join(HERE, "sweep_cases.json")))["cases"]))
