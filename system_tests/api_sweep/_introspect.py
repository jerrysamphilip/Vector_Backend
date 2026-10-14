"""Runs INSIDE the vector-api container: imports router modules (not app.main) and prints a JSON list of
{method, path, roles, perms, auth_deps} per route by walking each route's dependant tree."""
import json, importlib, sys
from fastapi import FastAPI
from fastapi.routing import APIRoute

# Every module in app.routers (discovered, so new routers are never silently left out of the guard map)
import pkgutil, app.routers as _routers_pkg
MODS = sorted(m.name for m in pkgutil.iter_modules(_routers_pkg.__path__) if m.name.endswith("_router"))
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
