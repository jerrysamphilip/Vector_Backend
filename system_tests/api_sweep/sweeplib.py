"""Library for the API sweep: throttled HTTP client, OpenAPI schema validator + valid-value generator,
case recorder. Standard library only (+ ../common.py)."""
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import common  # noqa: E402
from common import API, sql, login, run_id, Workspace, Results  # noqa: E402,F401


# Values that must never be written to committed result files (they are credentials of throwaway test
# users, but the secret scan rightly treats them as secrets): JSON fields like "secret"/"access_token",
# otpauth "secret=" query values and bare JWTs are replaced by a placeholder before a case is recorded.
_SENSITIVE_FIELD = re.compile(
    r'((?:\\?["\'])(?:secret|access_token|refresh_token|mfa_token|token|password|new_password|current_password|'
    r'temp_password|temporary_password|recovery_codes?|backup_codes?|api_key|client_secret)(?:\\?["\'])\s*:\s*(?:\\?["\']))'
    r'([^"\'\\]{4,})', re.I)
_OTP_SECRET = re.compile(r'(secret=)[A-Za-z0-9%]{8,}', re.I)
_JWT = re.compile(r'eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}')


def redact(text):
    """Strip credentials from a recorded detail/evidence string (keeps the rest readable)."""
    if not isinstance(text, str):
        return text
    text = _JWT.sub("<jwt>", text)
    text = _SENSITIVE_FIELD.sub(lambda m: m.group(1) + "<redacted>", text)
    return _OTP_SECRET.sub(lambda m: m.group(1) + "<redacted>", text)


# ───────────────────────── HTTP ─────────────────────────
# The API has a per-client-IP ceiling of 1200 req/min shared by every suite on this machine; stay well below it.
MIN_INTERVAL = float(os.environ.get("SWEEP_MIN_INTERVAL", "0.055"))
_last = [0.0]
_lock = threading.Lock()
COUNT = {"requests": 0, "429": 0}


def _hd(h):
    return {k.lower(): v for k, v in h.items()}


def http(method, path, body=None, token=None, headers=None, form=None, files=None, raw_body=None, ctype=None,
         query=None, timeout=40, follow=True):
    """-> (status, parsed_body, response_headers_dict, seconds). Never raises on HTTP errors; connection errors
    are retried (the API container may be restarted by other agents)."""
    h = {"Authorization": f"Bearer {token}"} if token else {}
    h.update(headers or {})
    data = None
    if query:
        path += ("&" if "?" in path else "?") + urllib.parse.urlencode(query, doseq=True)
    if files is not None:
        boundary = uuid.uuid4().hex
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
                 for k, v in (form or {}).items()]
        for k, (fname, content, *ct) in files.items():
            c = ct[0] if ct else "application/octet-stream"
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{fname}"\r\n'
                         f'Content-Type: {c}\r\n\r\n'.encode() + content + b"\r\n")
        data = b"".join(parts) + f"--{boundary}--\r\n".encode()
        h["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    elif raw_body is not None:
        data = raw_body if isinstance(raw_body, bytes) else raw_body.encode()
        h["Content-Type"] = ctype or "application/json"
    elif body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    for attempt in range(8):
        with _lock:
            wait = _last[0] + MIN_INTERVAL - time.time()
            if wait > 0:
                time.sleep(wait)
            _last[0] = time.time()
        t0 = time.time()
        r = urllib.request.Request(API + path, method=method, data=data, headers=h)
        try:
            try:
                with (common._opener if follow else common._raw_opener).open(r, timeout=timeout) as resp:
                    content, st, hd = resp.read(), resp.status, _hd(resp.headers)
            except urllib.error.HTTPError as e:
                content, st, hd = e.read(), e.code, _hd(e.headers)
        except (TimeoutError, __import__("socket").timeout):
            return 598, "client timeout after %ss" % timeout, {}, time.time() - t0
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            if "timed out" in str(exc):
                return 598, "client timeout after %ss" % timeout, {}, time.time() - t0
            time.sleep(4)
            continue
        COUNT["requests"] += 1
        if st == 429 and "login" not in path and "register" not in path and "forgot" not in path:
            COUNT["429"] += 1
            time.sleep(min(int(hd.get("retry-after", "5")), 30) + 1)
            continue
        ct = hd.get("content-type", "")
        parsed = None
        if "json" in ct:
            try:
                parsed = json.loads(content or b"null")
            except Exception:
                parsed = content.decode(errors="ignore")
        else:
            parsed = content if len(content) < 2_000_000 else content[:2_000_000]
            if "text" in ct or "csv" in ct or "html" in ct:
                parsed = content.decode(errors="ignore")
        return st, parsed, hd, time.time() - t0
    return 599, "connection failed", {}, 0.0


# ───────────────────────── schemas ─────────────────────────
class Schemas:
    def __init__(self, components):
        self.c = components.get("schemas", {})

    def deref(self, s):
        seen = 0
        while isinstance(s, dict) and "$ref" in s and seen < 30:
            s = self.c.get(s["$ref"].split("/")[-1], {})
            seen += 1
        return s or {}

    # ---- validation: returns list of problem strings
    def validate(self, inst, schema, path="$", depth=0):
        probs = []
        s = self.deref(schema)
        if not s or depth > 12:
            return probs
        for key in ("anyOf", "oneOf"):
            if key in s:
                best = None
                for alt in s[key]:
                    p = self.validate(inst, alt, path, depth + 1)
                    if not p:
                        return []
                    if best is None or len(p) < len(best):
                        best = p
                return best or []
        if "allOf" in s:
            for alt in s["allOf"]:
                probs += self.validate(inst, alt, path, depth + 1)
            return probs
        t = s.get("type")
        if inst is None:
            if t is None or t == "null" or s.get("nullable"):
                return []
            return [f"{path}: null not allowed (declared {t})"]
        if "enum" in s and inst not in s["enum"]:
            probs.append(f"{path}: {inst!r} not in enum {s['enum']}")
        if t == "object" or "properties" in s:
            if not isinstance(inst, dict):
                return [f"{path}: expected object got {type(inst).__name__}"]
            props = s.get("properties", {})
            for r in s.get("required", []):
                if r not in inst:
                    probs.append(f"{path}.{r}: required field missing")
            ap = s.get("additionalProperties", None)
            for k, v in inst.items():
                if k in props:
                    probs += self.validate(v, props[k], f"{path}.{k}", depth + 1)
                elif props and ap in (None, False):
                    probs.append(f"{path}.{k}: undeclared (extra) field")
                elif isinstance(ap, dict):
                    probs += self.validate(v, ap, f"{path}.{k}", depth + 1)
        elif t == "array":
            if not isinstance(inst, list):
                return [f"{path}: expected array got {type(inst).__name__}"]
            for i, v in enumerate(inst[:25]):
                probs += self.validate(v, s.get("items", {}), f"{path}[{i}]", depth + 1)
        elif t == "string":
            if not isinstance(inst, str):
                return [f"{path}: expected string got {type(inst).__name__} ({str(inst)[:30]})"]
        elif t == "integer":
            if isinstance(inst, bool) or not isinstance(inst, int):
                if not (isinstance(inst, float) and inst.is_integer()):
                    return [f"{path}: expected integer got {type(inst).__name__} ({str(inst)[:30]})"]
        elif t == "number":
            if isinstance(inst, bool) or not isinstance(inst, (int, float)):
                # money serialised as string is a common FastAPI Decimal pattern: accept numeric strings
                if not (isinstance(inst, str) and re.fullmatch(r"-?\d+(\.\d+)?", inst) and s.get("format") != "float"):
                    return [f"{path}: expected number got {type(inst).__name__} ({str(inst)[:30]})"]
        elif t == "boolean":
            if not isinstance(inst, bool):
                return [f"{path}: expected boolean got {type(inst).__name__}"]
        return probs

    def props_of(self, schema):
        s = self.deref(schema)
        for key in ("anyOf", "oneOf"):
            if key in s:
                for alt in s[key]:
                    a = self.deref(alt)
                    if a.get("type") == "object" or "properties" in a:
                        return a
        if "allOf" in s:
            for alt in s["allOf"]:
                a = self.deref(alt)
                if "properties" in a:
                    return a
        return s

    def prop_kind(self, schema):
        """Resolve a property schema to a (type, schema) ignoring null alternatives."""
        s = self.deref(schema)
        for key in ("anyOf", "oneOf"):
            if key in s:
                for alt in s[key]:
                    a = self.deref(alt)
                    if a.get("type") != "null" and a:
                        return self.prop_kind(a)
        return s.get("type") or ("object" if "properties" in s else None), s


# ───────────────────────── value generation ─────────────────────────
class Gen:
    """Generate valid-looking JSON values from OpenAPI schemas. `ids` maps registry names (list_id, ...) to
    real resource ids of the tenant being exercised."""

    def __init__(self, schemas: Schemas, ids: dict, rid: str):
        self.S, self.ids, self.rid, self.n = schemas, ids, rid, 0

    def uniq(self):
        self.n += 1
        return f"{self.rid}{self.n}"

    def scalar_by_name(self, name, s):
        n = (name or "").lower()
        f = s.get("format")
        if f == "email" or "email" in n and "id" not in n.split("_"):
            return f"sw{self.uniq()}@systest.example"
        if f == "date-time":
            return "2031-03-15T10:00:00Z"
        if f == "date":
            return "2031-03-15"
        if f == "time":
            return "09:00:00"
        if f == "uuid":
            return self.ids.get(n) or str(uuid.uuid4())
        if n in self.ids and isinstance(self.ids[n], str):
            return self.ids[n]
        if n.endswith("_id") or n == "id":
            return self.ids.get(n) or str(uuid.uuid4())
        if "password" in n:
            return "Sweep-Passw0rd-9x!"
        if "url" in n or "link" in n or n == "website":
            return "https://example.com/sweep"
        if "phone" in n:
            return "+15125550123"
        if "domain" in n:
            return f"sweep-{self.rid}.example.com"
        if n in ("timezone", "campaign_timezone"):
            return "UTC"
        if "name" in n or n in ("title", "label", "subject"):
            return f"Sweep {n} {self.uniq()}"
        if "body" in n or "description" in n or "notes" in n or n in ("text", "message"):
            return f"Sweep text {self.uniq()}"
        return f"sweep-{self.uniq()}"

    def value(self, schema, name=None, depth=0):
        S = self.S
        s = S.deref(schema)
        if depth > 6:
            return None
        if "default" in s and s["default"] is not None and not s.get("anyOf"):
            pass
        t, s2 = S.prop_kind(schema)
        s = s2
        if "enum" in s:
            return s["enum"][0]
        if "const" in s:
            return s["const"]
        if t == "object":
            return self.obj(s, depth)
        if t == "array":
            n = (name or "").lower()
            if n.endswith("ids") or n.endswith("_ids"):
                key = n[:-3].rstrip("_") + "_id" if n.endswith("ids") else n
                v = self.ids.get(n) or ([self.ids[key]] if key in self.ids else None)
                if isinstance(v, list):
                    return v
                return [v] if v else [str(uuid.uuid4())]
            item = self.value(s.get("items", {}), (name or "")[:-1] if (name or "").endswith("s") else name, depth + 1)
            return [item] * max(1, s.get("minItems", 0)) if item is not None else []
        if t == "integer":
            lo = s.get("minimum", s.get("exclusiveMinimum", 0) + 1 if "exclusiveMinimum" in s else 1)
            hi = s.get("maximum")
            v = max(1, lo)
            return min(v, hi) if hi is not None else v
        if t == "number":
            return max(1.0, s.get("minimum", 1.0))
        if t == "boolean":
            return False
        if t == "string":
            v = self.scalar_by_name(name, s)
            ml = s.get("maxLength")
            return v[:ml] if ml else v
        return f"sweep-{self.uniq()}"

    def obj(self, s, depth=0, only_required=True):
        props = s.get("properties", {})
        req = s.get("required", [])
        out = {}
        for k in (req if only_required else props):
            if k in props:
                out[k] = self.value(props[k], k, depth + 1)
        return out

    def body(self, schema):
        S = self.S
        s = S.props_of(schema)
        if s.get("type") == "array":
            return [self.value(s.get("items", {}))]
        return self.obj(s)


# wrong-typed values per property type
def wrong_value(kind):
    return {"string": {"a": 1}, "integer": "abc", "number": "abc", "boolean": "maybe", "array": "x", "object": "x"}.get(kind, {"a": 1})


# ───────────────────────── recorder ─────────────────────────
def slug(method, path):
    p = re.sub(r"\{([^}]+)\}", r"\1", path).strip("/")
    p = re.sub(r"[^A-Za-z0-9]+", "-", p).strip("-").lower() or "root"
    return f"{method}-{p}"


class Recorder:
    """Wraps common.Results; keeps structured metadata for FINDINGS/coverage."""

    def __init__(self, out_dir):
        self.R = Results("API sweep", out_dir)
        self.meta = {}
        self.out_dir = out_dir
        self.seen_ids = set()
        self._lk = threading.Lock()

    def rec(self, ep_id, method, path, check, kind, ok, detail="", category=None, evidence=None, title=None):
        """check: AUTH|ROLE|HAPPY|VAL|ISO|HIER|IDEM|SCHEMA ; kind: sub-id suffix"""
        cid = f"API-{slug(method, path)}-{check}" + (f"-{kind}" if kind else "")
        with self._lk:
            base, i = cid, 2
            while cid in self.seen_ids:
                cid = f"{base}~{i}"
                i += 1
            self.seen_ids.add(cid)
        if ok is True:
            category = ""
        elif ok is False and not category:
            category = "defect"
        detail = redact(str(detail))
        evidence = redact(evidence) if isinstance(evidence, str) else evidence
        self.R.record(cid, title or f"{method} {path} {check} {kind or ''}".strip(), ok, detail, category)
        row = self.R.rows[-1]
        row["detail"] = detail[:900]
        self.last_id = cid
        self.meta[cid] = {"ep": ep_id, "check": check, "kind": kind, "status": row["status"], "category": category or "",
                          "evidence": evidence}
        return ok

    def write(self):
        counts = self.R.write()
        with open(os.path.join(self.out_dir, "sweep_cases.json"), "w") as f:
            json.dump({"counts": counts, "cases": [{**r, **self.meta.get(r["id"], {})} for r in self.R.rows]}, f, indent=1)
        return counts
