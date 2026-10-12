"""Helpers for the outreach suite (fake IMAP server, SES event builders, waits, workspace cache).

Kept separate from ../common.py, which is shared by every suite and must not be edited.
"""
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import common  # noqa: E402
from common import API, req, sql, login, Workspace  # noqa: E402,F401

STATE_FILE = os.path.join(HERE, ".state.json")
WEBHOOK_TOKEN = os.environ.get("SES_WEBHOOK_TOKEN", "local-dev-webhook-token")
# Address the containers use to reach this machine (docker bridge gateway of the stack network)
HOST_FROM_CONTAINERS = os.environ.get("HOST_FROM_CONTAINERS", "172.18.0.1")


# ───────────────────────── generic ─────────────────────────

def wait_until(fn, timeout=90, interval=3):
    """Poll fn() until it returns a truthy value or the timeout passes. Returns the last value."""
    deadline = time.time() + timeout
    val = fn()
    while not val and time.time() < deadline:
        time.sleep(interval)
        val = fn()
    return val


def q(s):
    """SQL string literal."""
    return "'" + str(s).replace("\\", "\\\\").replace("'", "''") + "'"


def rows(query):
    out = sql(query)
    return [line.split("\t") for line in out.splitlines()] if out else []


def scalar(query):
    out = sql(query)
    return out.splitlines()[0] if out else None


def upload_csv(token, filename, text):
    """POST /uploads/validations with a real text/csv part (common.req sends octet-stream)."""
    import urllib.request
    import urllib.error
    boundary = uuid.uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f'Content-Type: text/csv\r\n\r\n').encode() + text.encode() + f"\r\n--{boundary}--\r\n".encode()
    r = urllib.request.Request(API + "/uploads/validations", data=body, method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with common._opener.open(r, timeout=120) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw.decode(errors="ignore")


def business_tz(now=None):
    """
    An IANA zone where it is currently a weekday that is not a US federal holiday, so the
    scheduler's send-window guard (business days only) lets a 00:00-23:59 window send now.
    Returns None when no zone qualifies (e.g. a weekend in every zone).
    """
    now = now or datetime.now(timezone.utc)
    holidays = {(1, 1), (6, 19), (7, 4), (11, 11), (12, 25)}  # fixed-date ones; others checked below

    def floating_holiday(d):
        # MLK, Presidents, Memorial, Labor, Columbus/Indigenous, Thanksgiving
        nth = (d.day - 1) // 7 + 1
        last = (d + timedelta(days=7)).month != d.month
        wd = d.weekday()
        return ((d.month, wd, nth) in {(1, 0, 3), (2, 0, 3), (9, 0, 1), (10, 0, 2), (11, 3, 4)}
                or (d.month == 5 and wd == 0 and last))

    best = None
    for off in range(-12, 15):
        name = "UTC" if off == 0 else f"Etc/GMT{'-' if off > 0 else '+'}{abs(off)}"
        local = now.astimezone(ZoneInfo(name))
        d = local.date()
        if local.weekday() >= 5 or (d.month, d.day) in holidays or floating_holiday(d):
            continue
        # keep at least 30 minutes of the local day left so a test run fits
        minutes_left = (23 * 60 + 59) - (local.hour * 60 + local.minute)
        if minutes_left < 30:
            continue
        if best is None or minutes_left > best[1]:
            best = (name, minutes_left)
    return best[0] if best else None


# ───────────────────────── workspace cache ─────────────────────────

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_workspace(label, state):
    """
    A workspace for this suite. Self sign-up is limited to 10 per hour per client IP and every
    suite on this machine shares that IP, so workspaces are created once and re-used by later runs.
    """
    cached = state.get("workspaces", {}).get(label)
    if cached:
        ws = Workspace.__new__(Workspace)
        ws.__dict__.update(cached)
        if sql(f"SELECT COUNT(*) FROM users WHERE email={q(ws.email)}") == "1":
            ws.token = login(ws.email, ws.password)
            return ws
    try:
        ws = Workspace(label)
    except AssertionError as exc:
        if "429" not in str(exc):
            raise
        # The shared per-IP sign-up budget is used up by other suites on this machine. The API
        # takes the client IP from the last X-Forwarded-For hop (its proxy's), so present a
        # distinct test-client address for this one sign-up instead of waiting up to an hour.
        ws = _register_via_xff(label)
    state.setdefault("workspaces", {})[label] = {k: v for k, v in ws.__dict__.items() if k != "token"}
    save_state(state)
    return ws


def _register_via_xff(label):
    ws = Workspace.__new__(Workspace)
    ws.rid = common.run_id()
    ws.email = f"owner-{label}-{ws.rid}@systest.example"
    ws.password = f"Systest-{ws.rid}-pw!"
    xff = f"10.250.{int(ws.rid[:2], 16)}.{int(ws.rid[2:4], 16)}"
    s, b = req("POST", "/api/auth/register", {"first_name": "Sys", "last_name": label.title(), "email": ws.email,
                                              "password": ws.password, "tenant_name": f"Systest {label} {ws.rid}"},
               headers={"X-Forwarded-For": xff})
    assert s == 201, ("register failed", s, b)
    ws.token = b["access_token"]
    ws.tenant_id = sql(f"SELECT tenant_id FROM users WHERE email='{ws.email}'")
    ws.user_id = sql(f"SELECT user_id FROM users WHERE email='{ws.email}'")
    return ws


def get_user(ws, role, first, state, **extra):
    key = f"{ws.rid}:{first}"
    cached = state.get("users", {}).get(key)
    if cached and sql(f"SELECT COUNT(*) FROM users WHERE user_id={q(cached['user_id'])}") == "1":
        return {**cached, "token": login(cached["email"], ws.password)}
    u = ws.add_user(role, first, **extra)
    state.setdefault("users", {})[key] = {"email": u["email"], "user_id": u["user_id"]}
    save_state(state)
    return u


# ───────────────────────── SES events ─────────────────────────

def ses_post(event, token=WEBHOOK_TOKEN, headers=None, raw_body=None):
    path = "/webhooks/ses/notifications" + (f"?token={token}" if token else "")
    return req("POST", path, event, headers=headers)


def _mail(message_id=None, ses_id=None, source="sender@example.com", dest="x@example.com", tags=True):
    m = {"timestamp": datetime.utcnow().isoformat() + "Z", "messageId": ses_id or f"ses-{uuid.uuid4().hex}",
         "source": source, "destination": [dest]}
    if tags and message_id:
        m["tags"] = {"message_id": [message_id]}
    return m


def ses_bounce(message_id, email, ses_id=None, source="s@example.com", btype="Permanent", sub="General", tags=True,
               diag="smtp; 550 5.1.1 user unknown"):
    return {"notificationType": "Bounce", "eventType": "Bounce",
            "mail": _mail(message_id, ses_id, source, email, tags),
            "bounce": {"bounceType": btype, "bounceSubType": sub, "timestamp": datetime.utcnow().isoformat() + "Z",
                       "bouncedRecipients": [{"emailAddress": email, "diagnosticCode": diag}]}}


def ses_complaint(message_id, email, ses_id=None, source="s@example.com", tags=True):
    return {"notificationType": "Complaint", "eventType": "Complaint",
            "mail": _mail(message_id, ses_id, source, email, tags),
            "complaint": {"complaintFeedbackType": "abuse", "timestamp": datetime.utcnow().isoformat() + "Z",
                          "complainedRecipients": [{"emailAddress": email}]}}


def ses_delivery(message_id, email, ses_id=None, source="s@example.com", tags=True, stamp=None):
    return {"notificationType": "Delivery", "eventType": "Delivery",
            "mail": _mail(message_id, ses_id, source, email, tags),
            "delivery": {"timestamp": stamp or (datetime.utcnow().isoformat() + "Z"), "recipients": [email],
                         "smtpResponse": "250 2.0.0 OK", "processingTimeMillis": 420}}


# ───────────────────────── fake IMAP server ─────────────────────────

class FakeImap:
    """
    Minimal IMAP4rev1 server over TLS (self-signed), enough for imaplib as used by the app:
    CAPABILITY, LOGIN, SELECT/EXAMINE INBOX, UID SEARCH, UID FETCH (BODY.PEEK[]), NOOP, LOGOUT.
    Mailboxes are keyed by login user; only INBOX exists (Sent folders answer NO).
    """

    def __init__(self, workdir, port=0):
        self.mailboxes = {}   # user -> list of raw bytes (uid = index + 1)
        self.passwords = {}   # user -> password
        self.logins = []      # (user, ok)
        self.lock = threading.Lock()
        cert, key = os.path.join(workdir, "imap-cert.pem"), os.path.join(workdir, "imap-key.pem")
        if not os.path.exists(cert):
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key,
                            "-out", cert, "-days", "30", "-subj", "/CN=fake-imap.test"],
                           check=True, capture_output=True)
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(cert, key)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", port))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def add_account(self, user, password):
        with self.lock:
            self.mailboxes.setdefault(user.lower(), [])
            self.passwords[user.lower()] = password

    def deliver(self, user, raw: bytes):
        with self.lock:
            self.mailboxes.setdefault(user.lower(), []).append(raw)
            return len(self.mailboxes[user.lower()])

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, raw_conn):
        try:
            conn = self.ctx.wrap_socket(raw_conn, server_side=True)
        except Exception:
            raw_conn.close()
            return
        f = conn.makefile("rb")
        user = None
        selected = False

        def send(s):
            conn.sendall(s if isinstance(s, bytes) else s.encode())

        try:
            send("* OK [CAPABILITY IMAP4rev1 AUTH=PLAIN] fake imap ready\r\n")
            while True:
                line = f.readline()
                if not line:
                    return
                parts = line.decode(errors="ignore").strip().split(" ", 2)
                if len(parts) < 2:
                    continue
                tag, cmd = parts[0], parts[1].upper()
                args = parts[2] if len(parts) > 2 else ""
                if cmd == "CAPABILITY":
                    send(f"* CAPABILITY IMAP4rev1 AUTH=PLAIN\r\n{tag} OK done\r\n")
                elif cmd == "LOGIN":
                    toks = _imap_args(args)
                    u, p = (toks + ["", ""])[:2]
                    ok = self.passwords.get(u.lower()) == p
                    self.logins.append((u.lower(), ok))
                    if ok:
                        user = u.lower()
                        send(f"{tag} OK LOGIN completed\r\n")
                    else:
                        send(f"{tag} NO [AUTHENTICATIONFAILED] Invalid credentials\r\n")
                elif cmd in ("SELECT", "EXAMINE"):
                    box = _imap_args(args)[0] if args else ""
                    if user and box.upper() == "INBOX":
                        n = len(self.mailboxes.get(user, []))
                        selected = True
                        send(f"* {n} EXISTS\r\n* 0 RECENT\r\n* OK [UIDVALIDITY 777] ok\r\n"
                             f"* OK [UIDNEXT {n + 1}] ok\r\n{tag} OK [READ-ONLY] done\r\n")
                    else:
                        send(f"{tag} NO no such mailbox\r\n")
                elif cmd == "UID" and selected:
                    sub, _, rest = args.partition(" ")
                    msgs = self.mailboxes.get(user, [])
                    if sub.upper() == "SEARCH":
                        uids = list(range(1, len(msgs) + 1))
                        if "UID" in rest.upper() and ":" in rest:
                            lo = int(rest.upper().split("UID", 1)[1].strip(" ()").split(":")[0])
                            uids = [u for u in uids if u >= lo] or (uids[-1:] if uids else [])
                        send(f"* SEARCH {' '.join(map(str, uids))}\r\n{tag} OK SEARCH done\r\n".replace("SEARCH \r", "SEARCH\r"))
                    elif sub.upper() == "FETCH":
                        uid_s = rest.split(" ", 1)[0]
                        try:
                            uid = int(uid_s)
                            body = msgs[uid - 1]
                            send(f"* {uid} FETCH (UID {uid} BODY[] {{{len(body)}}}\r\n".encode() + body + b")\r\n")
                        except (ValueError, IndexError):
                            pass
                        send(f"{tag} OK FETCH done\r\n")
                    else:
                        send(f"{tag} BAD unsupported\r\n")
                elif cmd == "LOGOUT":
                    send(f"* BYE bye\r\n{tag} OK LOGOUT done\r\n")
                    return
                elif cmd in ("NOOP", "CLOSE", "CHECK"):
                    send(f"{tag} OK done\r\n")
                elif cmd == "LIST":
                    send(f'* LIST (\\HasNoChildren) "/" INBOX\r\n{tag} OK LIST done\r\n')
                elif cmd == "STATUS":
                    n = len(self.mailboxes.get(user, [])) if user else 0
                    send(f"* STATUS INBOX (MESSAGES {n})\r\n{tag} OK done\r\n")
                elif cmd == "SEARCH" and selected:
                    n = len(self.mailboxes.get(user, []))
                    send(f"* SEARCH {' '.join(map(str, range(1, n + 1)))}\r\n{tag} OK done\r\n")
                elif cmd == "FETCH" and selected:
                    send(f"{tag} OK done\r\n")
                else:
                    send(f"{tag} BAD unknown command\r\n")
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass


def _imap_args(s):
    """Split IMAP arguments, honouring double quotes."""
    out, cur, quoted, esc = [], "", False, False
    for ch in s:
        if esc:
            cur += ch
            esc = False
        elif ch == "\\" and quoted:
            esc = True
        elif ch == '"':
            quoted = not quoted
        elif ch == " " and not quoted:
            if cur:
                out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


def reply_mime(from_addr, to_addr, subject, body, in_reply_to=None, auto_submitted=None, msg_id=None):
    from email.utils import formatdate, make_msgid
    lines = [f"From: {from_addr}", f"To: {to_addr}", f"Subject: {subject}", f"Date: {formatdate()}",
             f"Message-ID: {msg_id or make_msgid(domain='fake-imap.test')}", "MIME-Version: 1.0",
             "Content-Type: text/plain; charset=utf-8"]
    if in_reply_to:
        lines += [f"In-Reply-To: {in_reply_to}", f"References: {in_reply_to}"]
    if auto_submitted:
        lines.append(f"Auto-Submitted: {auto_submitted}")
    return ("\r\n".join(lines) + "\r\n\r\n" + body + "\r\n").encode()
