"""Generic checks run for EVERY endpoint: AUTH, ROLE, VAL (validation/robustness), ISO (tenant isolation), DUP."""
import json
import time
import uuid
import hashlib
import hmac
import base64

from engine import UNSET, _trunc
from sweeplib import Gen, wrong_value, http as _http_full

BAD_BODIES = ([], "x")  # shapes that every object-schema JSON body must reject
CLIENT_ERR = (400, 403, 404, 405, 409, 410, 413, 415, 422, 429)
REJECT = (400, 404, 409, 410, 413, 415, 422)


def is_bad5(s):
    return s in (500, 598) or (s >= 500 and s not in (501, 502, 503, 504, 599))


def is_json_body(ep):
    return bool(ep["body_schema"]) and ep["body_content_type"] == "application/json"


def is_multipart(ep):
    return ep["body_content_type"] == "multipart/form-data"


def base_request(E, ep, t):
    """(pp, query, body, files, form) for a VALID request: the one that last succeeded in the scenario, else generated."""
    if ep["id"] in E.ok_req:
        pp, query, body = E.ok_req[ep["id"]]
        return dict(pp), query, body
    body = None
    if is_json_body(ep):
        body = Gen(E.S, t.ids, t.rid).body(ep["body_schema"])
    return {}, None, body


def multipart_parts(ep, t, valid=True):
    """Minimal multipart payload for upload endpoints."""
    f = ep["path"]
    csv = b"First Name,Last Name,Company Name,email,Designation\nA,B,Acme,aa@probe-sweep.example.com,CEO\n"
    if "imports" in f:
        csv = b"Email,First Name,Company\naa@probe-sweep.example.com,A,Acme\n"
    if "attachments" in f:
        return {"file": ("probe.txt", b"probe", "text/plain")}, None
    form = None
    if f == "/imports":
        form = {"mapping": json.dumps({"Email": "contact.email"}), "options": "{}"}
    return {"file": ("probe.csv", csv, "text/csv")}, form


def skip_val(ep):
    return ep["dangerous"] or (ep["roles"] == ["PLATFORM_ADMIN"])


# ═══════════════════════════ AUTH ═══════════════════════════
def forged_expired_token(uid):
    """HS256 JWT signed with the stack's dev secret but already expired (must be rejected with 401)."""
    def b64(b):
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    head = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    pay = b64(json.dumps({"sub": uid, "exp": int(time.time()) - 3600, "type": "access"}).encode())
    sig = hmac.new(b"local-dev-only-change-me-0123456789abcdef", f"{head}.{pay}".encode(), hashlib.sha256).digest()
    return f"{head}.{pay}.{b64(sig)}"


def pmap(fn, items, workers=3):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(fn, items))


def auth_checks(E, A):
    pmap(lambda ep: _auth_one(E, A, ep), [e for e in E.inv["endpoints"] if e["auth_required"]])
    _auth_expired(E, A)


def _auth_one(E, A, ep):
    for _ in (0,):
        body = [] if is_json_body(ep) and ep["method"] in ("POST", "PUT", "PATCH") else None
        for kind, tok, hdr in (("noauth", None, None), ("garbage", "garbage.token.value", None),
                               ("wrongscheme", None, {"Authorization": "Basic dXNlcjpwYXNz"}),
                               ):
            s, b, h = E.call(ep["id"], A, token=tok, fake=True, body=body, headers=hdr, tag="auth-" + kind)
            ok = s == 401
            E.rec.rec(ep["id"], ep["method"], ep["path"], "AUTH", kind, ok, f"expected 401, got {s}: {_trunc(b, 160)}",
                      evidence={"status": s, "resp": _trunc(b, 300)}, title=f"{ep['id']} {kind} -> 401")


def _auth_expired(E, A):
    ep = E.EP["GET /api/auth/me"]
    s, b, h = E.call(ep["id"], A, token=forged_expired_token(A.uid["SUPER_ADMIN"]), tag="auth-expired")
    E.rec.rec(ep["id"], "GET", ep["path"], "AUTH", "expired", s == 401, f"expected 401, got {s}", title="expired JWT -> 401")


# ═══════════════════════════ ROLE ═══════════════════════════
def role_identities(ep):
    """[(identity, expected_allowed)] to test for an endpoint."""
    out = []
    for ident in ("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT_NOPERM"):
        exp = ep["expect"].get(ident)
        if exp is None:
            continue
        out.append((ident, bool(exp)))
    perms = ep["perms"]
    if perms and not ep["public"]:
        agent_ok = ep["roles"] is None or "AGENT" in ep["roles"]
        p = perms[0]
        out.append((f"AGENT_ONLY_{p}", bool(agent_ok and all(x == p for x in perms))))
        other = "view_analytics" if p != "view_analytics" else "manage_campaigns"
        out.append((f"AGENT_ONLY_{other}", bool(agent_ok and all(x == other for x in perms))))
    return out


def role_checks(E, A):
    pmap(lambda ep: _role_one(E, A, ep), E.inv["endpoints"])
    _role_read(E, A)


def _role_one(E, A, ep):
    for _ in (0,):
        if ep["public"]:
            continue
        for ident, allowed in role_identities(ep):
            if ident not in A.tok:
                continue
            dang = ep["dangerous"] or ep["roles"] == ["PLATFORM_ADMIN"]
            if not allowed:
                # forbidden role: realistic request with real ids/body; the guard must stop it before any effect
                pp, query, body = base_request(E, ep, A)
                if dang:
                    pp, body = {}, ([] if is_json_body(ep) else None)
                kw = {}
                if is_multipart(ep) and not dang:
                    kw["files"], kw["form"] = multipart_parts(ep, A)
                s, b, h = E.call(ep["id"], A, ident, pp=pp, query=query, body=body, fake=dang, tag="role-forbidden", **kw)
                ok = s == 403
            else:
                # allowed role: harmless probe (unknown ids + invalid body) -> must get past the guard (anything but 401/403/5xx)
                body = BAD_BODIES[0] if is_json_body(ep) and ep["method"] in ("POST", "PUT", "PATCH") else None
                real_get = ep["method"] == "GET" and not E.path_params(ep)
                s, b, h = E.call(ep["id"], A, ident, fake=not real_get, body=body, tag="role-allowed")
                ok = s not in (401, 403) and s < 500
            E.rec.rec(ep["id"], ep["method"], ep["path"], "ROLE", ident, ok,
                      f"{ident} expected {'allowed (not 403)' if allowed else '403'}, got {s}: {_trunc(b, 160)}",
                      evidence={"status": s, "resp": _trunc(b, 300), "expected_allowed": allowed},
                      title=f"{ep['id']} as {ident}: {'allowed' if allowed else 'forbidden'}")


def _role_read(E, A):
    # allowed non-owner roles can actually USE list/read endpoints (2xx), not just pass the guard
    for ep in E.inv["endpoints"]:
        if ep["method"] != "GET" or ep["public"] or E.path_params(ep) or ep["dangerous"] or ep["roles"] == ["PLATFORM_ADMIN"]:
            continue
        for ident, allowed in role_identities(ep):
            if not allowed or ident == "SUPER_ADMIN" or ident not in A.tok:
                continue
            pp, query, body = base_request(E, ep, A)
            s, b, h = E.call(ep["id"], A, ident, query=query, tag="role-read")
            ok = 200 <= s < 300 or (s in (400, 422) and ep["id"] in E.ok_req and query)
            E.rec.rec(ep["id"], ep["method"], ep["path"], "ROLE", f"{ident}-read", 200 <= s < 300,
                      f"{ident} reading list endpoint expected 2xx, got {s}: {_trunc(b, 160)}",
                      evidence={"status": s}, title=f"{ep['id']} readable by {ident}")


# ═══════════════════════════ VALIDATION ═══════════════════════════
# Endpoints whose path id is not server state (the handler never looks it up; it works on the request body only),
# so an unknown or another tenant's id cannot be rejected and there is nothing to isolate (triage.json: by-design).
# The ISO probe still fails on any leak of tenant B's data.
STATELESS_PATH_IDS = {"POST /uploads/{upload_id}/revalidations"}


def rec_val(E, ep, kind, s, b, req_desc, must_reject=False, must_not_500=True, note=""):
    bad500 = is_bad5(s)
    if must_reject:
        ok = s in REJECT and not bad500
        exp = "4xx rejection"
    else:
        ok = not bad500
        exp = "no 5xx"
    cat = None
    E.rec.rec(ep["id"], ep["method"], ep["path"], "VAL", kind, ok, f"{note} expected {exp}, got {s}: {_trunc(b, 200)} | req: {req_desc}",
              category=cat, evidence={"status": s, "resp": _trunc(b, 300), "req": req_desc},
              title=f"{ep['id']} {kind}")
    return ok


def val_checks(E, A):
    pmap(lambda ep: _val_one(E, A, ep), E.inv["endpoints"])


def _val_one(E, A, ep):
    S = E.S
    for _ in (0,):
        if skip_val(ep):
            continue
        public = ep["public"]
        who = None if public else "SUPER_ADMIN"
        pp, query, body = base_request(E, ep, A)
        pnames = E.path_params(ep)
        meth = ep["method"]
        mutating = meth in ("POST", "PUT", "PATCH", "DELETE")
        # keep rate-limited public auth endpoints to a handful of probes
        limited = ep["path"].startswith("/api/auth/") or ep["path"] == "/api/users/accept-invite" or ep["path"].startswith("/webhooks")

        def call(kind, **kw):
            kw.setdefault("tag", "val-" + kind)
            return E.call(ep["id"], A, who, **kw)

        # --- path parameters
        if pnames and not limited:
            s, b, h = call("pathbad", bad="not-a-uuid")
            rec_val(E, ep, "path-malformed-id", s, b, f"{meth} path params='not-a-uuid'", must_reject=not public)
            s, b, h = call("pathweird", bad="%27%20OR%201=1--")
            rec_val(E, ep, "path-sqli-id", s, b, "path params=\"' OR 1=1--\"")
            s, b, h = call("pathnx", fake=True, body=body if is_json_body(ep) else None,
                           **({"files": multipart_parts(ep, A)[0], "form": multipart_parts(ep, A)[1]} if is_multipart(ep) else {}))
            rec_val(E, ep, "path-nonexistent-id", s, b, f"{meth} unknown uuid",
                    must_reject=not public and ep["id"] not in STATELESS_PATH_IDS)

        if skip_body_probes(ep, public, limited):
            pass
        # --- body probes (JSON)
        if is_json_body(ep) and meth in ("POST", "PUT", "PATCH") and not (limited and not _limited_ok(ep)):
            sch = S.props_of(ep["body_schema"])
            props = sch.get("properties", {})
            req = sch.get("required", [])
            kw0 = {"pp": pp, "query": query}
            s, b, h = call("emptyobj", body={}, **kw0)
            rec_val(E, ep, "body-empty-object", s, b, "{}", must_reject=bool(req))
            s, b, h = call("array", body=[], **kw0)
            rec_val(E, ep, "body-array-instead-of-object", s, b, "[]", must_reject=(sch.get("type") == "object" or bool(props)))
            s, b, h = call("badjson", raw_body='{"a": ', ctype="application/json", **kw0)
            rec_val(E, ep, "body-malformed-json", s, b, '{"a": ', must_reject=True)
            if body is not None and isinstance(body, dict):
                for r in req[:3]:
                    nb = {k: v for k, v in body.items() if k != r}
                    s, b, h = call("missing", body=nb, **kw0)
                    rec_val(E, ep, f"body-missing-required-{r}", s, b, f"drop {r}", must_reject=True)
                nwrong = 0
                for pname, psch in props.items():
                    if nwrong >= 8:
                        break
                    kind_, ps = S.prop_kind(psch)
                    if kind_ is None and "enum" not in ps:
                        continue
                    wv = wrong_value(kind_)
                    if kind_ == "string" and ps.get("format") in ("date-time", "date", "email", "uuid"):
                        wv = {"a": 1}
                    nb = dict(body)
                    nb[pname] = wv
                    s, b, h = call("wrongtype", body=nb, **kw0)
                    rec_val(E, ep, f"body-wrong-type-{pname}", s, b, f"{pname}={wv!r}", must_reject=True)
                    nwrong += 1
                # oversized + special strings across all string properties
                for kind, val in (("oversize", "A" * 70000), ("special", "Zoë 😀 \u0000 ' \" ; -- <script>alert(1)</script> \\ %s {{x}}"),):
                    nb = dict(body)
                    strs = [p for p, ps in props.items() if S.prop_kind(ps)[0] == "string"
                            and not S.prop_kind(ps)[1].get("format") and "enum" not in S.prop_kind(ps)[1]
                            and not (p.endswith("_id") or p in ("email", "timezone", "campaign_timezone", "token", "mfa_token", "code"))]
                    if not strs:
                        continue
                    for p in strs:
                        nb[p] = val
                    s, b, h = call(kind, body=nb, **kw0)
                    bad = is_bad5(s)
                    if bad and len(strs) > 1:  # bisect: which field breaks it
                        culprit = []
                        for p in strs:
                            nb2 = dict(body)
                            nb2[p] = val
                            s2, b2, _ = call(kind + "-bisect", body=nb2, **kw0)
                            if is_bad5(s2):
                                culprit.append(p)
                        note = f"failing field(s): {culprit} "
                    else:
                        note = ""
                    rec_val(E, ep, f"body-{kind}-strings", s, b, f"{len(strs)} string field(s) set to {kind}", note=note)
                # numeric extremes
                ints = [p for p, ps in props.items() if S.prop_kind(ps)[0] in ("integer", "number")]
                for val in (-1, 10 ** 15):
                    if not ints:
                        break
                    nb = dict(body)
                    for p in ints:
                        nb[p] = val
                    s, b, h = call("intextreme", body=nb, **kw0)
                    rec_val(E, ep, f"body-ints-{val}", s, b, f"{ints} = {val}")
                # id-typed fields with garbage
                idf = [p for p in props if p.endswith("_id") or p.endswith("_ids")]
                if idf:
                    nb = dict(body)
                    for p in idf:
                        nb[p] = ["not-a-uuid"] if p.endswith("_ids") else "not-a-uuid"
                    s, b, h = call("badids", body=nb, **kw0)
                    rec_val(E, ep, "body-malformed-id-fields", s, b, f"{idf} = not-a-uuid", must_reject=False)
                    nb = dict(body)
                    for p in idf:
                        nb[p] = [str(uuid.uuid4())] if p.endswith("_ids") else str(uuid.uuid4())
                    s, b, h = call("unknownids", body=nb, **kw0)
                    rec_val(E, ep, "body-unknown-id-fields", s, b, f"{idf} = random uuid", must_reject=False)
        # --- multipart probes
        if is_multipart(ep) and meth == "POST" and not limited:
            kw0 = {"pp": pp}
            s, b, h = call("nofile", **kw0)
            rec_val(E, ep, "multipart-missing-file", s, b, "no file part", must_reject=True)
            for kind, content, fname, ctype in (
                    ("emptyfile", b"", "empty.csv", "text/csv"),
                    ("binary", bytes(range(256)) * 40, "bin.csv", "application/octet-stream"),
                    ("traversal-name", b"a,b\n1,2\n", "../../etc/passwd.csv", "text/csv"),
                    ("headeronly", b"First Name,email\n", "h.csv", "text/csv"),
                    ("bigfile", b"First Name,email\n" + b"x,y\n" * 600000, "big.csv", "text/csv"),
                    ("badencoding", "Email,Name\nzoë@x.com,\xff\xfe\n".encode("latin-1", "replace") + b"\xff\xfe\x00", "enc.csv", "text/csv"),
                    ("xlsx-garbage", b"PK\x03\x04" + b"junk" * 50, "x.xlsx", "application/vnd.ms-excel")):
                form = multipart_parts(ep, A)[1]
                s, b, h = call("mp-" + kind, files={"file": (fname, content, ctype)}, form=form, **kw0)
                rec_val(E, ep, f"multipart-{kind}", s, b, f"{fname} {len(content)} bytes")
        # --- query parameters
        qps = E.query_params(ep)
        if qps and not limited:
            ints = [p["name"] for p in qps if (p.get("schema") or {}).get("type") == "integer" or _anyof_type(p, "integer")]
            strs = [p["name"] for p in qps if (p.get("schema") or {}).get("type") == "string" or _anyof_type(p, "string")]
            bools = [p["name"] for p in qps if (p.get("schema") or {}).get("type") == "boolean" or _anyof_type(p, "boolean")]
            probes = []
            if ints:
                probes += [("int-negative", {n: -1 for n in ints}), ("int-huge", {n: 10 ** 30 for n in ints}),
                           ("int-text", {n: "abc" for n in ints})]
            if strs:
                probes += [("str-sqli", {n: "' OR '1'='1' -- " for n in strs}), ("str-long", {n: "A" * 4000 for n in strs}),
                           ("str-special", {n: "Zoë 😀 \u0000 %00 \\ ../" for n in strs})]
            if bools:
                probes.append(("bool-text", {n: "maybe" for n in bools}))
            for kind, qd in probes:
                dquery = dict(query or {})
                dquery.update(qd)
                rpp = pp if (meth == "GET" or not mutating) else {}
                s, b, h = call("q-" + kind, pp=rpp, query=dquery, fake=(mutating and bool(pnames)),
                               body=None if not is_json_body(ep) else body)
                note = ""
                if is_bad5(s) and len(qd) > 1:
                    culprit = []
                    for n, v in qd.items():
                        dq2 = dict(query or {})
                        dq2[n] = v
                        s2, b2, _ = call("q-" + kind + "-bisect", pp=rpp, query=dq2, fake=(mutating and bool(pnames)),
                                         body=None if not is_json_body(ep) else body)
                        if is_bad5(s2):
                            culprit.append(n)
                    note = f"failing param(s): {culprit} "
                rec_val(E, ep, f"query-{kind}", s, b, f"query {list(qd)[:6]}", note=note)
        # --- DELETE/GET with body-less malformed: nothing more


def _anyof_type(p, t):
    for alt in (p.get("schema") or {}).get("anyOf", []):
        if alt.get("type") == t:
            return True
    return False


def skip_body_probes(ep, public, limited):
    return False


def _limited_ok(ep):
    """Rate-limited public auth endpoints: only probe the cheap pre-handler validation ones."""
    return ep["path"] in ("/api/auth/login", "/api/auth/forgot-password", "/api/auth/reset-password", "/api/auth/magic-login",
                          "/api/auth/google/callback", "/api/auth/register", "/api/users/accept-invite", "/api/auth/mfa/verify")


# ═══════════════════════════ DUPLICATES / IDEMPOTENCY ═══════════════════════════
def dup_checks(E, A):
    """Repeat the exact valid create request: a unique violation must be 409/400, never 500; unchanged PATCH/PUT stays OK."""
    for ep in E.inv["endpoints"]:
        if ep["id"] not in E.ok_req or ep["dangerous"] or ep["public"]:
            continue
        pp, query, body = E.ok_req[ep["id"]]
        if ep["method"] == "POST" and is_json_body(ep) and not ep["path"].startswith("/api/auth") and "launch" not in ep["path"]:
            s, b, h = E.call(ep["id"], A, "SUPER_ADMIN", pp=pp, query=query, body=body, tag="dup")
            ok = s < 500 or s == 599
            E.rec.rec(ep["id"], "POST", ep["path"], "IDEM", "duplicate-create", ok,
                      f"repeat of identical create request -> {s}: {_trunc(b, 200)} (expected 2xx or 4xx, never 5xx)",
                      evidence={"status": s, "resp": _trunc(b, 300)}, title=f"{ep['id']} duplicate create")
        if ep["method"] in ("PUT", "PATCH") and is_json_body(ep):
            s, b, h = E.call(ep["id"], A, "SUPER_ADMIN", pp=pp, query=query, body=body, tag="idem-update")
            s2, b2, h2 = E.call(ep["id"], A, "SUPER_ADMIN", pp=pp, query=query, body=body, tag="idem-update")
            ok = (200 <= s < 300 and 200 <= s2 < 300) or (s == s2 and not is_bad5(s))
            E.rec.rec(ep["id"], ep["method"], ep["path"], "IDEM", "unchanged-update", ok,
                      f"same update twice -> {s}, {s2}: {_trunc(b2, 200)}", evidence={"status": [s, s2]},
                      title=f"{ep['id']} unchanged update twice OK")


# ═══════════════════════════ TENANT ISOLATION ═══════════════════════════
def leak_terms(other):
    terms = [other.tenant_id, other.ids.get("marker")]
    terms += [v for k, v in other.ids.items() if isinstance(v, str) and len(v) == 36 and k not in ("user_id", "owner_id") or
              (isinstance(v, str) and len(v) == 36 and k in ("user_id", "owner_id"))]
    return [x for x in dict.fromkeys(terms) if x]


def find_leaks(text, other):
    return [t for t in leak_terms(other) if t in text]


def iso_checks(E, A, B):
    """A -> B resources by path id and by body/query reference; list endpoints must never show the other tenant's data."""
    S = E.S
    for ep in E.inv["endpoints"]:
        if ep["public"] or ep["dangerous"] or ep["roles"] == ["PLATFORM_ADMIN"]:
            continue
        pnames = E.path_params(ep)
        meth = ep["method"]
        # ---- (1) foreign id in path
        idp = [n for n in pnames if n in B.ids or any(a[1] == n and ep["path"].startswith(a[0]) for a in __import__("engine").ALIAS)]
        if idp:
            pp, query, body = base_request(E, ep, A)
            # foreign ids for the id params, everything else as normal
            fp = {}
            for n in pnames:
                if n in idp:
                    key = n
                    for pref, pname, k in __import__("engine").ALIAS:
                        if n == pname and ep["path"].startswith(pref):
                            key = k
                    fp[n] = B.ids[key]
                elif n in pp:
                    fp[n] = pp[n]
            kw = {}
            if is_multipart(ep):
                kw["files"], kw["form"] = multipart_parts(ep, A)
            s, b, h = E.call(ep["id"], A, "SUPER_ADMIN", pp=fp, query=query, body=body, tag="iso-path", **kw)
            txt = _trunc(b, 4000) if not isinstance(b, bytes) else ""
            leaks = find_leaks(txt, B) if 200 <= s < 300 else []
            ok = s in (400, 403, 404, 409, 410, 422) and s < 500
            if ep["id"] in STATELESS_PATH_IDS:
                ok = (ok or 200 <= s < 300) and not leaks
            E.rec.rec(ep["id"], meth, ep["path"], "ISO", "path-foreign-id", ok,
                      f"tenant A used tenant B's {idp} -> {s} (expected 403/404){' LEAK: ' + str(leaks[:3]) if leaks else ''}: {_trunc(b, 200)}",
                      evidence={"status": s, "resp": _trunc(b, 400), "foreign": {n: fp[n] for n in idp}},
                      title=f"{ep['id']} with tenant B's id -> 403/404")
            # reverse direction for reads only (B token, A ids)
            if meth == "GET":
                fp2 = {n: A.ids[n if n not in ("user_id",) else "user_id"] for n in idp}
                for n in idp:
                    for pref, pname, k in __import__("engine").ALIAS:
                        if n == pname and ep["path"].startswith(pref):
                            fp2[n] = A.ids[k]
                s, b, h = E.call(ep["id"], B, "SUPER_ADMIN", pp={**fp2, **{n: pp[n] for n in pnames if n not in idp and n in pp}},
                                 query=query, tag="iso-path-rev")
                ok = s in (400, 403, 404, 409, 410, 422) and s < 500
                E.rec.rec(ep["id"], meth, ep["path"], "ISO", "path-foreign-id-reverse", ok,
                          f"tenant B used tenant A's {idp} -> {s}: {_trunc(b, 200)}", title=f"{ep['id']} B reading A's id -> 403/404")
        # ---- (2) foreign id referenced from body / query
        if is_json_body(ep) and meth in ("POST", "PUT", "PATCH") and ep["id"] != "POST /contacts/bulk":
            sch = S.props_of(ep["body_schema"])
            props = sch.get("properties", {})
            pp, query, body = base_request(E, ep, A)
            if isinstance(body, dict):
                repl = {}
                for pname in props:
                    key = pname if pname in B.ids else None
                    if pname.endswith("_ids") and (pname[:-1] in B.ids):
                        repl[pname] = [B.ids[pname[:-1]]]
                    elif pname.endswith("_ids") and pname[:-4] + "_id" in B.ids:
                        repl[pname] = [B.ids[pname[:-4] + "_id"]]
                    elif key and pname.endswith("_id") and isinstance(B.ids[key], str):
                        repl[pname] = B.ids[key]
                repl = {k: v for k, v in repl.items() if not (k == "user_id" and ep["path"] == "/sales/targets" and False)}
                for pname, fv in repl.items():
                    nb = dict(body)
                    nb[pname] = fv
                    s, b, h = E.call(ep["id"], A, "SUPER_ADMIN", pp=pp, query=query, body=nb, tag="iso-body")
                    txt = _trunc(b, 4000)
                    fv_list = fv if isinstance(fv, list) else [fv]
                    leaks = [x for x in find_leaks(txt, B) if x not in fv_list] if 200 <= s < 300 else []
                    rejected = isinstance(b, dict) and bool(b.get("rejected_count") or b.get("not_found")) and not b.get("enrolled_count")
                    ignored = 200 <= s < 300 and not leaks and (rejected or not any(x in _trunc(b, 20000) for x in fv_list))
                    ok = (s in (400, 403, 404, 409, 410, 422) and not is_bad5(s)) or ignored
                    E.rec.rec(ep["id"], meth, ep["path"], "ISO", f"body-foreign-{pname}", ok,
                              f"{pname}=<tenant B resource> -> {s} (expected 4xx or foreign id ignored/not echoed){' LEAK/LINKED: ' + str(leaks[:3] or fv_list) if (leaks or (200 <= s < 300 and not ignored)) else ''}: {_trunc(b, 200)}",
                              evidence={"status": s, "resp": _trunc(b, 400), "req": _trunc(nb, 300)},
                              title=f"{ep['id']} body field {pname} referencing tenant B -> 4xx")
        if meth == "GET":
            qn = [p["name"] for p in E.query_params(ep)]
            qrepl = {n: B.ids[n] for n in qn if n in B.ids and isinstance(B.ids[n], str) and len(B.ids[n]) == 36}
            if "owner" in qn:
                qrepl["owner"] = B.uid["SUPER_ADMIN"]
            if "member" in qn:
                qrepl["member"] = B.uid["SUPER_ADMIN"]
            if "user_id" in qn:
                qrepl["user_id"] = B.uid["SUPER_ADMIN"]
            if qrepl:
                pp, query, body = base_request(E, ep, A)
                q2 = dict(query or {})
                q2.update(qrepl)
                s, b, h = E.call(ep["id"], A, "SUPER_ADMIN", pp=pp, query=q2, tag="iso-query")
                txt = _trunc(b, 6000) if not isinstance(b, bytes) else b[:6000].decode("latin-1")
                leaks = find_leaks(txt, B) if 200 <= s < 300 else []
                ok = not is_bad5(s) and not leaks
                E.rec.rec(ep["id"], meth, ep["path"], "ISO", "query-foreign-ref", ok,
                          f"query {list(qrepl)} = tenant B ids -> {s}{' LEAK: ' + str(leaks[:3]) if leaks else ''}: {_trunc(b, 160)}",
                          evidence={"status": s, "resp": _trunc(b, 300)}, title=f"{ep['id']} query ref to tenant B leaks nothing")
    # ---- (2b) bulk contact actions that take a foreign reference
    ep = E.EP["POST /contacts/bulk"]
    for action, field, key in (("assign_owner", "owner_id", None), ("set_account", "account_id", "account_id"),
                               ("add_to_list", "list_id", "list_id"), ("enroll", "campaign_id", "campaign_id")):
        fv = B.uid["SUPER_ADMIN"] if key is None else B.ids[key]
        nb = {"prospect_ids": [A.ids["prospect_beta"]], "action": action, field: fv}
        s, b, h = E.call(ep["id"], A, "SUPER_ADMIN", body=nb, tag="iso-bulk")
        ok = (s in (400, 403, 404, 409, 422) and not is_bad5(s)) or (200 <= s < 300 and fv not in _trunc(b, 5000) and
                                                                      _bulk_unchanged(A, action, fv))
        E.rec.rec(ep["id"], "POST", ep["path"], "ISO", f"bulk-{action}-foreign-{field}", ok,
                  f"bulk {action} with tenant B's {field} -> {s}: {_trunc(b, 200)}", title=f"POST /contacts/bulk {action} with tenant B's {field}")
    # ---- (3) list/read endpoints without path ids: never contain the other tenant's records
    for ep in E.inv["endpoints"]:
        if ep["method"] != "GET" or ep["public"] or E.path_params(ep) or ep["dangerous"] or ep["roles"] == ["PLATFORM_ADMIN"]:
            continue
        for me, other in ((A, B), (B, A)):
            pp, query, body = base_request(E, ep, me)
            q2 = dict(query or {})
            for pr in E.query_params(ep):
                if pr["name"] in ("page_size", "limit", "per_column", "per_stage") and pr["name"] not in q2:
                    q2[pr["name"]] = 100
            s, b, h = E.call(ep["id"], me, "SUPER_ADMIN", query=q2, tag="iso-list")
            txt = b.decode("latin-1") if isinstance(b, bytes) else _trunc(b, 400000)
            leaks = find_leaks(txt, other) if 200 <= s < 300 else []
            E.rec.rec(ep["id"], "GET", ep["path"], "ISO", f"list-{me.label}-sees-{other.label}", not leaks and not is_bad5(s),
                      f"tenant {me.label} response contains tenant {other.label}'s data {leaks[:3]} (status {s})",
                      evidence={"status": s, "leaks": leaks[:5]}, title=f"{ep['id']} as tenant {me.label} shows nothing of tenant {other.label}")
    # ---- (4) B's resources are intact after all cross-tenant writes (nothing of B was modified/deleted)
    intact = {
        "GET /accounts/{account_id}": "account_id", "GET /contacts/{prospect_id}": "prospect_id", "GET /lists/{list_id}": "list_id",
        "GET /campaigns/{campaign_id}": "campaign_id", "GET /templates/{template_id}": "template_id", "GET /inboxes/{inbox_id}": "inbox_id",
        "GET /leads/{lead_id}": "lead_id", "GET /opportunities/{opportunity_id}": "opportunity_id", "GET /proposals/{proposal_id}/lines": "proposal_id",
        "GET /views": None, "GET /reports/custom/{report_id}": "report_id", "GET /campaign-drafts/{draft_id}": "draft_id",
        "GET /conversations/{conversation_id}": "conversation_id", "GET /api/company-profiles/{profile_id}": "profile_id",
    }
    for epid, key in intact.items():
        if epid not in E.EP or key is None:
            continue
        ep = E.EP[epid]
        s, b, h = E.call(epid, B, "SUPER_ADMIN", tag="iso-intact")
        E.rec.rec(epid, ep["method"], ep["path"], "ISO", "victim-intact", s == 200,
                  f"tenant B's own {key} still readable after cross-tenant attempts by tenant A -> {s}",
                  title=f"{epid}: tenant B's resource intact after tenant A's cross-tenant attempts")


def _bulk_unchanged(A, action, fv):
    """DB check (own tenant): the A contact must not now reference B's owner/account."""
    from sweeplib import sql
    pid = A.ids["prospect_beta"]
    col = {"assign_owner": "owner_id", "set_account": "account_id"}.get(action)
    if col:
        return sql(f"SELECT {col} FROM prospects WHERE prospect_id='{pid}'") != fv
    if action == "add_to_list":
        return sql(f"SELECT COUNT(*) FROM prospect_list_members WHERE list_id='{fv}' AND prospect_id='{pid}'") == "0"
    return sql(f"SELECT COUNT(*) FROM campaign_prospects WHERE campaign_id='{fv}' AND prospect_id='{pid}'") == "0"
