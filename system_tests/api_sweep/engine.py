"""Engine: knows the endpoint inventory, resolves path params from per-tenant id registries, performs calls (logging every
5xx), and implements the `happy` check (status + response-schema validation) used by the scenario."""
import json
import re
import uuid

from sweeplib import http, Schemas, Gen, slug

# (path prefix, path param) -> registry key, where the same param name means different resources
ALIAS = [
    ("/template-library", "template_id", "tlib_id"),
    ("/workflow/rules", "rule_id", "wf_rule_id"),
    ("/automation-rules", "rule_id", "auto_rule_id"),
    ("/contacts/activities", "activity_id", "activity_id"),
    ("/api/users/invitations", "invitation_id", "invitation_id"),
    ("/api/users", "user_id", "victim_user_id"),
    ("/campaigns/sequences", "sequence_id", "sequence_id"),
    ("/api/company-profiles", "profile_id", "profile_id"),
    ("/campaigns/prospects", "prospect_id", "prospect_id"),
]
PARAM_DEFAULTS = {"persona_type": "MARKET_ACCESS", "provider": "google", "purpose": "meeting", "step": "1",
                  "domain_name": None, "tenant_id": None}
UNSET = object()
OK2 = (200, 201, 202, 204)


class Engine:
    def __init__(self, inv, rec):
        self.inv = inv
        self.EP = {e["id"]: e for e in inv["endpoints"]}
        self.S = Schemas(inv["components"])
        self.rec = rec
        self.hits_ok = set()      # endpoints with a 2xx observed
        self.hits_any = set()
        self.ok_req = {}          # ep id -> (pp, query, body) of the last successful (2xx) happy request
        self.errors5xx = []       # every 5xx seen anywhere
        self.calls = 0
        self.slow = []
        self.recording = True

    # ------------------------------------------------------------ paths
    def fill(self, ep, t, pp=None, fake=False, bad=None, foreign=None):
        """Resolve {params} of ep's path. fake: random uuid for id params; bad: literal string for all params;
        foreign: tenant whose ids to use for id params."""
        path = ep["path"]
        pp = dict(pp or {})
        ids = (foreign or t).ids
        for name in re.findall(r"\{([^}]+)\}", path):
            if name in pp:
                val = pp[name]
            elif bad is not None:
                val = bad
            elif fake:
                val = str(uuid.uuid4()) if name.endswith("_id") or name == "id" else "zz-nonexistent"
            else:
                key = name
                for pref, pname, k in ALIAS:
                    if name == pname and ep["path"].startswith(pref):
                        key = k
                        break
                val = ids.get(key)
                if val is None:
                    val = PARAM_DEFAULTS.get(name)
                if val is None:
                    val = str(uuid.uuid4())
            path = path.replace("{" + name + "}", str(val))
        return path

    def path_params(self, ep):
        return re.findall(r"\{([^}]+)\}", ep["path"])

    def query_params(self, ep):
        return [p for p in ep["params"] if p["in"] == "query"]

    # ------------------------------------------------------------ calls
    def call(self, ep_id, t, who="SUPER_ADMIN", pp=None, query=None, body=None, fake=False, bad=None, foreign=None,
             token=UNSET, headers=None, form=None, files=None, raw_body=None, ctype=None, path=None, tag="", follow=True):
        ep = self.EP[ep_id]
        p = path or self.fill(ep, t, pp, fake, bad, foreign)
        tok = t.tok.get(who) if token is UNSET else token
        s, b, h, dt = http(ep["method"], p, body, tok, headers=headers, form=form, files=files, raw_body=raw_body,
                           ctype=ctype, query=query, follow=follow)
        self.calls += 1
        if dt > 6:
            self.slow.append({"ep": ep_id, "secs": round(dt, 1), "tag": tag, "status": s})
        self.hits_any.add(ep_id)
        if 200 <= s < 300:
            self.hits_ok.add(ep_id)
        if (s in (500, 598) or (s >= 500 and s != 599 and not isinstance(b, dict))):
            self.errors5xx.append({"ep": ep_id, "status": s, "path": p, "query": query, "body": _trunc(body, 600),
                                   "who": who, "tag": tag, "response": _trunc(b, 300)})
        return s, b, h

    # ------------------------------------------------------------ happy path
    def declared_schema(self, ep, status):
        r = ep["responses"]
        for code in (str(status), "200", "201", "202", "204"):
            if r.get(code):
                return r[code]
        return None

    def happy(self, ep_id, t, who="SUPER_ADMIN", pp=None, query=None, body=UNSET, expect=OK2, save=None, env=None,
              files=None, form=None, kind="", check="HAPPY", title=None, gen_body=True, category=None, keep=True, follow=True, query_ok=None):
        """One valid request for ep. Records HAPPY (status) and SCHEMA (response vs OpenAPI) cases.
        env: tuple of statuses that count as a clean, expected failure caused by an unavailable external dependency
        (AWS/OpenAI/OAuth/SMTP) -> HAPPY passes with env flag. save: {registry_key: dotted path in JSON response}."""
        ep = self.EP[ep_id]
        if body is UNSET:
            body = None
            if ep["body_schema"] and ep["body_content_type"] == "application/json" and gen_body:
                body = Gen(self.S, t.ids, t.rid).body(ep["body_schema"])
        s, b, h = self.call(ep_id, t, who, pp, query, body, form=form, files=files, tag="happy", follow=follow)
        ok = s in expect
        is_env = False
        detail = f"status {s}: {_trunc(b, 300)}"
        if not ok and env and s in env:
            clean = isinstance(b, dict) and any(k in b for k in ("detail", "error", "message"))
            if clean:
                ok, is_env = True, True
                detail = f"environment limitation (expected external dependency): {s} {_trunc(b, 160)}"
            else:
                detail = f"not a clean JSON error: {s} {_trunc(b, 200)}"
        if self.recording:
            self.rec.rec(ep_id, ep["method"], ep["path"], check, kind, ok, detail if not ok or is_env else "",
                         category=category, evidence={"req": _trunc(body, 400), "resp": _trunc(b, 400), "status": s},
                         title=title or f"{ep_id} valid request by {who} -> {expect[0]}xx")
            if is_env:
                self.rec.mark_env()
        if ok and not is_env:
            self.hits_ok.add(ep_id)
            if keep and who == "SUPER_ADMIN" and self.recording:
                self.ok_req[ep_id] = (dict(pp or {}), query, body)
            if save and isinstance(b, (dict, list)):
                for key, jp in save.items():
                    v = jget(b, jp)
                    if v is not None:
                        t.ids[key] = v
            if self.recording:
                self.check_schema(ep, s, b, kind, who)
        return s, b

    def check_schema(self, ep, s, b, kind="", who=""):
        sch = self.declared_schema(ep, s)
        if s == 204 or not isinstance(b, (dict, list, type(None))) and not sch:
            return
        if not sch:
            self.rec.rec(ep["id"], ep["method"], ep["path"], "SCHEMA", kind, None,
                         "OpenAPI declares no response schema for this endpoint (response_model missing): cannot validate",
                         category="not-implemented", title=f"{ep['id']} response matches declared schema")
            return
        probs = self.S.validate(b, sch)
        self.rec.rec(ep["id"], ep["method"], ep["path"], "SCHEMA", kind, not probs, "; ".join(probs[:6]),
                     evidence={"resp": _trunc(b, 500)}, title=f"{ep['id']} response matches declared schema")


def jget(obj, path):
    cur = obj
    for part in path.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except Exception:
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _trunc(v, n):
    try:
        s = json.dumps(v, default=repr) if not isinstance(v, str) else v
    except Exception:
        s = repr(v)
    return s if len(s) <= n else s[:n] + "..."


def _idem_second(self, ep_id, t, pp, kind, first_status, expect=(404, 410)):
    """Repeat a successful DELETE: the resource is gone -> 404 (never 5xx, never 2xx 'deleted again').
    A soft delete (e.g. a property is only deactivated, C-17) passes expect=(200,): repeating it is idempotent."""
    ep = self.EP[ep_id]
    if not (200 <= first_status < 300):
        return
    s, b, h = self.call(ep_id, t, "SUPER_ADMIN", pp, tag="idem")
    ok = s in expect
    want = "/".join(str(x) for x in expect)
    if self.recording:
        self.rec.rec(ep_id, ep["method"], ep["path"], "IDEM", f"second-{kind}", ok,
                     f"second {ep['method']} returned {s}: {_trunc(b, 200)} (expected {want})",
                     evidence={"status": s, "resp": _trunc(b, 300)}, title=f"{ep_id} repeated -> {want}")


Engine.idem_second = _idem_second
