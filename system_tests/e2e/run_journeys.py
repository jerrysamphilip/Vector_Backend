#!/usr/bin/env python3
"""Run the E2E journeys against the live stack with the SES stand-in.

    python3 -m system_tests.e2e.run_journeys                 # all journeys
    python3 -m system_tests.e2e.run_journeys --only J2,J3    # a subset
    python3 -m system_tests.e2e.run_journeys --serial        # J1, then J2-J6 (default: J1 alongside J2-J6)
Writes results.md / results.json next to this file. See README.md for bringing the stand-ins up.
"""
import argparse
import os
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

INSTRUCTIONS = """
The E2E stand-ins are not ready. Bring them up (see system_tests/e2e/README.md):

  cd <Vector_Backend>
  docker build -f <sandbox Dockerfile> --build-context certs=<dir with ca.crt> -t vector-api:e2e .
  docker build -f system_tests/e2e/Dockerfile.ses-mock --build-context certs=<dir with ca.crt> -t vector-ses-mock system_tests/e2e
  FRONTEND_DIR=../Vector_Frontend docker compose -f docker-compose.yml -f <sandbox override> \\
      -f system_tests/e2e/docker-compose.e2e.yml up -d --no-build ses-mock api worker
"""


JOURNEYS = ("J1", "J2", "J3", "J4", "J5", "J6")


def selected_journeys(only_arg, cases):
    """The journeys to run: --only (default all), narrowed by a tier's case filter (TEST_CASES) to those it can select."""
    only = {x.strip().upper() for x in only_arg.split(",") if x.strip()} or set(JOURNEYS)
    if cases.active:
        only = {k for k in only if cases.wants_prefix(f"E2E-{k}-")}
        print("journeys selected by TEST_CASES:", ",".join(sorted(only)) or "none")
    return only


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="comma list of J1..J6")
    ap.add_argument("--merge", action="store_true", help="keep results of journeys not re-run from the previous results.json")
    ap.add_argument("--serial", action="store_true", help="run J1 and then J2-J6 (default: J1 in a thread next to J2-J6)")
    args = ap.parse_args()
    from system_tests.e2e import helpers as H
    problems = H.preflight()
    if problems:
        print("PRE-FLIGHT FAILED:")
        for p in problems:
            print("  -", p)
        print(INSTRUCTIONS)
        sys.exit(2)
    H.ses_verify(H.PLATFORM_SENDER)
    from system_tests.common import CASES, Results
    R = Results("E2E journeys (real worker + SES stand-in + fake IMAP)", HERE)
    only = selected_journeys(args.only, CASES)
    if args.merge and only:
        import json
        try:
            old = json.load(open(os.path.join(HERE, "results.json")))["cases"]
            R.rows = [r for r in old if not any(r["id"].startswith(f"E2E-{k}-") for k in only)]
        except Exception:  # noqa: BLE001
            pass

    def guard(prefix, fn, *a):
        t0 = time.time()
        try:
            return fn(*a)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            R.record(f"E2E-{prefix}-EXC", f"{prefix} aborted: {fn.__name__}", False, f"exception: {exc!r}", "test-issue")
        finally:  # where the journey spends its time (tier tuning, docs/TESTING.md section 2)
            print(f"timing {prefix} {fn.__name__}: {time.time() - t0:.0f}s", flush=True)

    if "J1" not in only or args.serial or not only - {"J1"}:
        _run_j1(R, guard, only)
        _run_others(R, guard, only)
    else:
        # J1 (~15 min, mostly real waits for the worker: background IMAP cadence, 120 s re-spread of step 3) runs in a
        # thread next to J2-J6 (~10 min): every journey has its own sign-up workspace, mailboxes and fake IMAP port,
        # and their job passes go through run_once (see system_tests/TIERS.json, "e2e"). --serial: one after another.
        from system_tests.e2e import j1_outreach_to_revenue  # noqa: F401  (import before the thread starts)
        t = threading.Thread(target=_run_j1, args=(R, guard, only), name="J1", daemon=True)
        t.start()
        _run_others(R, guard, only)
        t.join()
    import re as _re

    def _key(r):
        m = _re.match(r"E2E-J(\d+)-(\d+)([a-z]*)", r["id"])
        return (int(m.group(1)), int(m.group(2)), m.group(3)) if m else (99, 0, r["id"])
    R.rows.sort(key=_key)
    counts = R.write()
    print(counts)


def _run_j1(R, guard, only):
    if "J1" not in only:
        return
    from system_tests.e2e import j1_outreach_to_revenue as J1
    C = {}
    try:
        for ph in ("setup", "upload", "campaign", "send", "events", "unsub", "replies", "bg_reply", "sales", "reports",
                   "followups", "gdpr"):
            if guard("J1", getattr(J1, "phase_" + ph), R, C) is None and "w" not in C:
                break
    finally:
        if "w" in C:
            from system_tests.e2e.flows import detach_imap
            detach_imap(C["w"])


def _run_others(R, guard, only):
    for key, mod, fn in (("J2", "j2_deliverability", "run"), ("J3", "j3_accounts_access", "run"), ("J4", "j4_cross_tenant", "run"),
                         ("J5", "j5_crm_chain", "run"), ("J6", "j6_automation", "run")):
        if key not in only:
            continue
        m = __import__(f"system_tests.e2e.{mod}", fromlist=["run"])
        guard(key, getattr(m, fn), R)


if __name__ == "__main__":
    main()
