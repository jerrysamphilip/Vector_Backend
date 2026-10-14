"""Frontend -> backend contract check.

Extracts every API call the React app makes (method + path template) and verifies the backend
(OpenAPI from the running API, or a saved copy) has a matching route with the same method.
Usage: python3 check_contract.py [--frontend ../../../Vector_Frontend/src] [--openapi URL|file] [--json out.json]
Exit code 1 if any call has no matching route.
"""
import argparse, json, os, re, sys, urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("--frontend", default=os.path.join(os.path.dirname(__file__), "../../../Vector_Frontend/src"))
ap.add_argument("--openapi", default="http://localhost:8191/openapi.json")
ap.add_argument("--json", default=None)
a = ap.parse_args()

if a.openapi.startswith("http"):
    spec = json.load(urllib.request.build_opener(urllib.request.ProxyHandler({})).open(a.openapi))
else:
    spec = json.load(open(a.openapi))
routes = {}  # (method, compiled regex) -> path
for path, ops in spec["paths"].items():
    core = path.rstrip("/") or "/"
    rx = re.compile("^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(core)) + "/?$")
    for m in ops:
        if m in ("get", "post", "put", "patch", "delete"):
            routes[(m, rx)] = path

CALL = re.compile(r"\b(\w+)\.(get|post|put|patch|delete)\(\s*(`[^`]*`|'[^']*'|\"[^\"]*\"|[A-Za-z_][\w.]*)", re.S)
# clients whose base is "/api" (apiV1Client) vs the root API
V1 = {"apiV1Client"}
CLIENTS = {"api", "apiClient", "apiV1Client", "client", "http", "axios", "apiv1", "authClient", "request"}

# Local wrapper helpers in src/api/*.js: `const get = (path) => api.get(path)`; callers use get('/x'), del(...)
WRAP = re.compile(r"(?<![\w.])(get|post|patch|put|del)\(\s*(`[^`]*`|'[^']*'|\"[^\"]*\")", re.S)
WRAP_METHOD = {"del": "delete"}

# Vitest/Jest files mock the client with fake paths: not real app calls
TEST_FILE = re.compile(r"\.(test|spec)\.[jt]sx?$")
calls, dynamic = [], []
for root, _, files in os.walk(a.frontend):
    for f in files:
        if not f.endswith((".js", ".jsx", ".ts", ".tsx")) or TEST_FILE.search(f):
            continue
        p = os.path.join(root, f)
        src = open(p, encoding="utf-8", errors="ignore").read()
        for m in CALL.finditer(src):
            client, method, arg = m.groups()
            if client not in CLIENTS:
                continue
            line = src.count("\n", 0, m.start()) + 1
            if arg[0] in "`'\"":
                tpl = arg[1:-1]
                tpl = re.sub(r"\$\{[^}]*\}", "{id}", tpl).split("?")[0]
                if not tpl.startswith("/"):
                    dynamic.append((os.path.relpath(p, a.frontend), line, method, arg)); continue
                path = ("/api" if client in V1 else "") + tpl
                calls.append((os.path.relpath(p, a.frontend), line, method, path))
            else:
                dynamic.append((os.path.relpath(p, a.frontend), line, method, arg))

for root, _, files in os.walk(os.path.join(a.frontend, "api")):
    for f in files:
        if not f.endswith(".js") or TEST_FILE.search(f):
            continue
        p = os.path.join(root, f)
        src = open(p, encoding="utf-8", errors="ignore").read()
        if not re.search(r"const (get|post|patch|put|del) = ", src):
            continue
        for m in WRAP.finditer(src):
            name, arg = m.groups()
            line = src.count("\n", 0, m.start()) + 1
            tpl = re.sub(r"\$\{[^}]*\}", "{id}", arg[1:-1]).split("?")[0]
            if tpl.startswith("/"):
                calls.append((os.path.relpath(p, a.frontend), line, WRAP_METHOD.get(name, name), tpl))

# resolve simple constants like `const API_BASE = '/x'` + `${API_BASE}/y`? keep unresolved as dynamic
missing, ok = [], 0
for f, line, method, path in calls:
    p = re.sub(r"\{id\}", "x1", path)
    hit = any(m == method and rx.match(p) for (m, rx) in routes)
    if hit:
        ok += 1
    else:
        alt = [rp for (m, rx), rp in routes.items() if rx.match(p)]
        missing.append({"file": f, "line": line, "method": method.upper(), "path": path,
                        "note": f"path exists with other method(s)" if alt else "no such path"})
print(f"API calls found: {len(calls)}  matched: {ok}  unmatched: {len(missing)}  unresolved(dynamic): {len(dynamic)}")
for m in missing:
    print(f"MISSING  {m['method']:6} {m['path']}   ({m['file']}:{m['line']}) - {m['note']}")
if a.json:
    json.dump({"calls": len(calls), "matched": ok, "missing": missing,
               "dynamic": [{"file": f, "line": l, "method": mt, "arg": ar} for f, l, mt, ar in dynamic]}, open(a.json, "w"), indent=2)
sys.exit(1 if missing else 0)
