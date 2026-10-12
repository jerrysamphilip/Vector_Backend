#!/usr/bin/env python3
"""Exhaustive API sweep (see FINDINGS.md). Usage: run_api_sweep.py [--fresh] [--phases a,b,c]"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from sweeplib import Recorder, COUNT  # noqa: E402
import coverage  # noqa: E402
import fixtures  # noqa: E402
from engine import Engine  # noqa: E402
import scenario, generic, hier  # noqa: E402


def main():
    t0 = time.time()
    fresh = "--fresh" in sys.argv
    only = set(sys.argv[sys.argv.index('--phases') + 1].split(',')) if '--phases' in sys.argv else None
    inv = json.load(open(os.path.join(HERE, "ENDPOINTS.json")))
    rec = Recorder(HERE)
    coverage.snapshot('before')
    started = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime())
    E = Engine(inv, rec)
    A, B, new = fixtures.get_tenants(fresh)
    print("tenants", "new" if new else "cached", A.tenant_id, B.tenant_id)
    scenario.create_phase(E, A)
    E.recording = False
    scenario.create_phase(E, B)
    E.recording = True
    for name, fn in (("AUTH", lambda: generic.auth_checks(E, A)), ("ROLE", lambda: generic.role_checks(E, A)),
                     ("HIER", lambda: hier.hier_checks(E, A)), ("VAL", lambda: generic.val_checks(E, A)),
                     ("DUP", lambda: generic.dup_checks(E, A)), ("ISO", lambda: generic.iso_checks(E, A, B))):
        if only and name not in only:
            continue
        t1 = time.time(); fn(); print(f"phase {name} {time.time()-t1:.0f}s", flush=True)
    for fn in (scenario.tail_phase, scenario.auth_phase):
        try:
            fn(E, A)
        except Exception as exc:  # keep results of everything done so far
            import traceback; traceback.print_exc()
            rec.rec("-", "-", fn.__name__, "SETUP", "", False, f"exception {exc!r}", category="test-issue")
    counts = rec.write()
    coverage.snapshot('after')
    json.dump({'started_utc': started, 'errors': E.errors5xx, 'slow': E.slow, 'calls': E.calls, 'requests': COUNT['requests'], 'secs': round(time.time()-t0), 'hits_ok': sorted(E.hits_ok)}, open(os.path.join(HERE, 'run_meta.json'), 'w'), indent=1)
    print(counts, "requests", COUNT, "secs", round(time.time() - t0))
    print("5xx:", len(E.errors5xx))
    un = [e for e in inv["endpoints"] if e["id"] not in E.hits_ok]
    print("NOT 2xx-covered:", len(un))
    for e in un:
        print("   ", e["id"], e["guard"][:60])
    for e in E.errors5xx[:40]:
        print("  ", e["status"], e["ep"], e["tag"], e["path"], e["response"][:120])


if __name__ == "__main__":
    main()
