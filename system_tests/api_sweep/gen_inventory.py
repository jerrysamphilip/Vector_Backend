#!/usr/bin/env python3
"""Build ENDPOINTS.json = live /openapi.json  +  guard info (allowed roles / permissions) introspected from the
router modules inside the vector-api image (require_role / require_permission closure cells).

Per endpoint it also computes `expect[role]` = True/False (access expected?) for the sweep's identities:
PLATFORM_ADMIN, SUPER_ADMIN, ADMIN, MANAGER, AGENT, AGENT_NOPERM (custom_permissions='[]').
Permission defaults come from app/domain/identity.py (ROLE_DEFAULT_PERMISSIONS, introspected) / custom_permissions.
Regenerate after any route, schema or permission change:  python3 gen_inventory.py   (stack up; reads the live API)."""
import json, os, subprocess, sys, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
API = os.environ.get("API_URL", "http://localhost:8191")

# Fallback only: introspect() reads the live defaults from app/domain/identity.py::ROLE_DEFAULT_PERMISSIONS inside the
# api image (AGENT has had manage_campaigns by default since c3ad117, 2026-10-14) and replaces this table.
DEFAULT_PERMS = {
    "PLATFORM_ADMIN": {"manage_platform"},
    "SUPER_ADMIN": {"manage_campaigns", "manage_templates", "manage_inboxes", "manage_prospects", "view_analytics", "export_data", "manage_team"},
    "ADMIN": {"manage_campaigns", "manage_templates", "manage_inboxes", "manage_prospects", "view_analytics", "export_data", "manage_team"},
    "MANAGER": {"manage_campaigns", "manage_templates", "view_analytics"},
    "AGENT": {"manage_campaigns"},
}
IDENTITIES = ["PLATFORM_ADMIN", "SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT_NOPERM"]

# Genuinely public (no bearer token required): path-prefix/exact based
PUBLIC_EXACT = {
    ("POST", "/api/auth/register"), ("POST", "/api/auth/login"), ("POST", "/api/auth/refresh"),
    ("POST", "/api/auth/forgot-password"), ("POST", "/api/auth/reset-password"), ("GET", "/health"), ("GET", "/health/ready"),
}
PUBLIC_PREFIX = ["/api/auth/magic", "/api/auth/google", "/api/auth/accept-invite", "/api/auth/mfa/verify", "/webhooks/", "/track/", "/t/", "/unsubscribe", "/connections/oauth", "/api/connections/oauth"]
# Never mutate: global state / other tenants / operator
DANGEROUS_PREFIX = ["/api/admin", "/scheduler"]
DANGEROUS_EXACT = {("POST", "/persona-blueprints")}


# Checks done INSIDE handlers (not visible in the dependency tree). Found by the first sweep run: allowed-by-decorator roles got
# 403 with these messages. Recorded here so the role matrix expects them (they are also reported as a finding: guard is not declarative).
INLINE_PERMS = {
    "POST /accounts/backfill": "manage_prospects", "GET /accounts/deleted": "manage_prospects",
    "DELETE /accounts/{account_id}": "manage_prospects", "POST /accounts/{account_id}/restore": "manage_prospects",
    "GET /contacts/deleted": "manage_prospects", "GET /contacts/duplicates": "manage_prospects",
    "DELETE /contacts/fields/{field_id}": "manage_prospects", "DELETE /contacts/{prospect_id}": "manage_prospects",
    "DELETE /lists/{list_id}": "manage_prospects",
    # import_router checks can_manage_contacts() in the handler (imports are for users who manage every contact)
    "GET /imports": "manage_prospects", "POST /imports": "manage_prospects", "POST /imports/preview": "manage_prospects",
    # added since the 2026-10-19 inventory; each handler's service calls require_manager / can_manage_contacts():
    # background imports + saved mappings (C-03/C-04, import_service.py), duplicate pairs (C-09, duplicate_service.py),
    # company backfill preview / runs / undo (C-14, account_backfill_service.py), company properties (C-16/C-17)
    "GET /imports/mappings": "manage_prospects", "POST /imports/mappings": "manage_prospects",
    "DELETE /imports/mappings/{mapping_id}": "manage_prospects", "POST /imports/uploads": "manage_prospects",
    "POST /imports/{import_id}/dry-run": "manage_prospects", "POST /imports/{import_id}/start": "manage_prospects",
    "POST /imports/{import_id}/cancel": "manage_prospects", "POST /imports/{import_id}/resume": "manage_prospects",
    "GET /contacts/duplicates/pairs": "manage_prospects", "POST /contacts/duplicates/scan": "manage_prospects",
    "POST /contacts/duplicates/{pair_id}/dismiss": "manage_prospects", "POST /contacts/duplicates/{pair_id}/reopen": "manage_prospects",
    "POST /accounts/backfill/previews": "manage_prospects", "GET /accounts/backfill/runs": "manage_prospects",
    "POST /accounts/backfill/runs": "manage_prospects", "POST /accounts/backfill/runs/{run_id}/undo": "manage_prospects",
    "DELETE /accounts/fields/{field_id}": "manage_prospects",
}
INLINE_ROLES = {"GET /contacts/export": ["ADMIN", "MANAGER", "SUPER_ADMIN"],
                "GET /accounts/export": ["ADMIN", "MANAGER", "SUPER_ADMIN"],   # "Only admins and managers can export companies"
                "GET /exports/logs": ["ADMIN", "SUPER_ADMIN"],                 # app/services/export_log.py EXPORT_LOG_ROLES
                "DELETE /contacts/{prospect_id}": ["ADMIN", "MANAGER", "SUPER_ADMIN"],   # "Agents cannot delete contacts"
                "DELETE /accounts/{account_id}": ["ADMIN", "MANAGER", "SUPER_ADMIN"]}  # "Agents cannot delete companies"  # "Only admins and managers can export contacts"


def introspect():
    sh = os.path.join(HERE, "_run_introspect.sh")
    out = subprocess.run(["bash", sh], capture_output=True, text=True).stdout
    line = [l for l in out.splitlines() if l.startswith("@@JSON@@")]
    if not line:
        sys.exit("introspection failed:\n" + out[-2000:])
    perms = [l for l in out.splitlines() if l.startswith("@@PERMS@@")]
    if perms:
        DEFAULT_PERMS.update({role: set(p) for role, p in json.loads(perms[0][len("@@PERMS@@"):]).items()})
    else:
        print("WARNING: no role defaults from the image; using the fallback table in gen_inventory.py")
    return json.loads(line[0][len("@@JSON@@"):])


def main():
    import time
    for attempt in range(30):  # other agents may restart the API container
        try:
            with urllib.request.urlopen(API + "/openapi.json", timeout=60) as r:
                spec = json.load(r)
            break
        except Exception:
            time.sleep(3)
    else:
        sys.exit("API not reachable")
    guards = {(g["method"], g["path"]): g for g in introspect()}
    eps = []
    for path, item in spec["paths"].items():
        for method, op in item.items():
            if method.upper() not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                continue
            m = method.upper()
            g = guards.get((m, path))
            params = [p for p in (item.get("parameters", []) + op.get("parameters", []))]
            roles = None
            perms = []
            auth = None
            if g:
                auth = g["auth"]
                perms = sorted(set(g["perms"]))
                if g["role_sets"]:
                    s = set(g["role_sets"][0])
                    for rs in g["role_sets"][1:]:
                        s &= set(rs)
                    roles = sorted(s)
            if f"{m} {path}" in INLINE_PERMS:
                perms = sorted(set(perms) | {INLINE_PERMS[f"{m} {path}"]})
            if f"{m} {path}" in INLINE_ROLES and roles is not None:
                roles = sorted(set(roles) & set(INLINE_ROLES[f"{m} {path}"]))
            public = (m, path) in PUBLIC_EXACT or path == "/" or g is None or any(path.startswith(p) for p in PUBLIC_PREFIX) or (g is not None and not g["auth"])
            expect = {}
            for ident in IDENTITIES:
                base = ident if ident in DEFAULT_PERMS else "AGENT"
                if public:
                    expect[ident] = True
                    continue
                ok = True
                if roles is not None and base not in roles:
                    ok = False
                if ok and perms:
                    if base == "PLATFORM_ADMIN":
                        ok = False
                    elif base == "SUPER_ADMIN":
                        ok = True
                    else:
                        eff = set() if ident == "AGENT_NOPERM" else DEFAULT_PERMS[base]
                        ok = all(p in eff for p in perms)
                if base == "PLATFORM_ADMIN" and roles is not None and "PLATFORM_ADMIN" not in roles:
                    ok = False
                if base == "PLATFORM_ADMIN" and roles is None and not perms:
                    ok = False  # get_current_user only: platform admins are allowed in principle; handler decides -> unknown
                expect[ident] = ok
            if not public and roles is None and not perms:
                expect = {k: (None if k == "PLATFORM_ADMIN" else True) for k in IDENTITIES}  # auth only, any role
            rb = op.get("requestBody")
            body_ct, body_schema = None, None
            if rb:
                cts = rb.get("content", {})
                for ct in ("application/json", "multipart/form-data", "application/x-www-form-urlencoded"):
                    if ct in cts:
                        body_ct, body_schema = ct, cts[ct].get("schema"); break
            resp = {}
            for code, rr in op.get("responses", {}).items():
                sch = None
                for ct, c in rr.get("content", {}).items():
                    if ct == "application/json":
                        sch = c.get("schema")
                resp[code] = sch
            eps.append({
                "id": f"{m} {path}", "method": m, "path": path, "operationId": op.get("operationId"), "tags": op.get("tags", []),
                "summary": op.get("summary"), "params": params, "body_required": bool(rb and rb.get("required")),
                "body_content_type": body_ct, "body_schema": body_schema, "responses": resp,
                "roles": roles, "perms": perms, "auth_required": not public, "public": public,
                "dangerous": any(path.startswith(p) for p in DANGEROUS_PREFIX) or (m, path) in DANGEROUS_EXACT,
                "platform_only": path.startswith("/platform-admin"),
                "other_deps": g["other_deps"] if g else [], "handler": g["handler"] if g else None, "in_introspection": g is not None,
                "guard": ("PUBLIC" if public else (f"roles={','.join(roles)}" if roles is not None else "any-auth-user") + (f" perm={','.join(perms)}" if perms else "")),
                "expect": expect,
            })
    eps.sort(key=lambda e: (e["path"], e["method"]))
    json.dump({"generated_from": API + "/openapi.json", "components": spec.get("components", {}), "endpoints": eps},
              open(os.path.join(HERE, "ENDPOINTS.json"), "w"), indent=1)
    print(f"{len(eps)} endpoints; unmatched in introspection: {[e['id'] for e in eps if not e['in_introspection']]}")
    print("public:", sum(e['public'] for e in eps), "dangerous:", sum(e['dangerous'] for e in eps))


if __name__ == "__main__":
    main()
