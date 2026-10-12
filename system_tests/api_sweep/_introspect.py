"""Runs INSIDE the vector-api container: imports router modules (not app.main) and prints a JSON list of
{method, path, roles, perms, auth_deps} per route by walking each route's dependant tree."""
import json, importlib, sys
from fastapi import FastAPI
from fastapi.routing import APIRoute

MODS = ["accounts_router","ai_email_router","auth_router","automation_router","campaign_draft_router","campaign_router",
        "campaign_wizard_router","company_profile_router","connections_router","contacts_router","conversation_router",
        "crm_router","custom_reports_router","deliverability_router","email_scheduler_router","import_router","inbox_router",
        "lists_router","platform_admin_router","prospect_list_router","quotes_router","reports_router","sales_admin_router",
        "sales_reports_router","sales_router","ses_webhook_router","tasks_router","template_router","tracking_router","user_router"]
ROUTERS = []
seen = set()
for m in MODS:
    mod = importlib.import_module("app.routers." + m)
    for name in ("router", "prospect_upload_router"):
        r = getattr(mod, name, None)
        if r is not None and id(r) not in seen:
            seen.add(id(r)); ROUTERS.append(r)

def cells(fn):
    out = []
    for c in (getattr(fn, "__closure__", None) or []):
        try: out.append(c.cell_contents)
        except ValueError: pass
    return out

def walk(dep, acc):
    for sub in dep.dependencies:
        call = sub.call
        q = getattr(call, "__qualname__", str(call))
        if q.startswith("require_role.") :
            roles = [v for v in cells(call) if isinstance(v, (tuple, list, set, frozenset)) and all(isinstance(x, str) for x in v)]
            acc["roles"].append(sorted(set(roles[0])) if roles else [])
        elif q.startswith("require_permission."):
            perm = [v for v in cells(call) if isinstance(v, str)]
            acc["perms"].append(perm[0] if perm else "?")
        elif q == "get_current_user":
            acc["auth"] = True
        else:
            n = getattr(call, "__name__", q)
            if any(k in n.lower() for k in ("user", "auth", "admin", "token", "tenant", "scope", "verify", "secret", "signature")):
                acc["other"].append(f"{getattr(call,'__module__','')}.{q}")
        walk(sub, acc)

rows = []
ALL = [x for rt in ROUTERS for x in rt.routes]
for r in ALL:
    if isinstance(r, APIRoute):
        acc = {"roles": [], "perms": [], "auth": False, "other": []}
        walk(r.dependant, acc)
        for meth in sorted(r.methods - {"HEAD", "OPTIONS"}):
            rows.append({"method": meth, "path": r.path, "role_sets": acc["roles"], "perms": acc["perms"],
                         "auth": acc["auth"] or bool(acc["roles"]) or bool(acc["perms"]), "other_deps": sorted(set(acc["other"])),
                         "handler": f"{r.endpoint.__module__}.{r.endpoint.__name__}"})
print("@@JSON@@" + json.dumps(rows))
