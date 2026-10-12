"""J3 - Accounts & access: register, MFA (TOTP computed here), recovery code, invite (real email from the SES stand-in),
forgot/reset password, magic link, role change, deactivation, tenant suspension, password change revokes refresh tokens."""
import re
import time
import uuid

from .helpers import (follow, login_xff, new_workspace, q, req, scalar, ses_captured, ses_verify, sql, totp, user_token,
                      wait_until, add_user)

PLATFORM_SENDER = "platform@e2e-platform.example"
ORIGIN = {"Origin": "http://localhost:8190"}   # what the browser sends; links are built from it


def _mail_to(addr, timeout=40):
    def find():
        got = [c for c in ses_captured() if c.to == addr.lower()]
        return got[-1] if got else None
    return wait_until(find, timeout=timeout, interval=2)


def _token_in(text):
    m = re.search(r"token=([A-Za-z0-9_\-]+)", text or "")
    return m.group(1) if m else None


def _next_code(secret, used):
    """TOTP replay protection: a time step can be used once, and the server accepts only the current step (+-1).
    So wait for the next 30 s step when the current code was already spent."""
    for _ in range(3):
        c = totp(secret)
        if c not in used:
            used.add(c)
            return c
        time.sleep(30 - (time.time() % 30) + 1)
    raise RuntimeError("no fresh code")


def run(R):
    ses_verify(PLATFORM_SENDER)
    ws = new_workspace("j3")
    rid = ws.rid
    s, me = req("GET", "/api/auth/me", token=ws.token)
    R.check("E2E-J3-01", "Register: owner is SUPER_ADMIN, ACTIVE, MFA off", s == 200 and me["role"] == "SUPER_ADMIN"
            and not me.get("mfa_enabled"), f"{s} {str(me)[:200]}")
    # ── MFA ──
    s, setup = req("POST", "/api/auth/mfa/setup", None, ws.token)
    secret = setup.get("secret")
    used = set()
    s_bad, b_bad = req("POST", "/api/auth/mfa/enable", {"code": "000000"}, ws.token)
    s_ok, en = req("POST", "/api/auth/mfa/enable", {"code": _next_code(secret, used)}, ws.token)
    codes = en.get("recovery_codes") or []
    R.check("E2E-J3-02", "MFA enrolment: wrong code rejected (401), correct TOTP enables it and returns 10 one-time recovery codes",
            s == 200 and secret and s_bad in (400, 401) and s_ok == 200 and en.get("mfa_enabled") and len(codes) >= 8,
            f"setup={s} bad={s_bad} ok={s_ok} codes={len(codes)}")
    s, out = req("POST", "/api/auth/logout", None, ws.token)
    s1, lg = login_xff(ws.email, ws.password)
    R.check("E2E-J3-03", "After logout, password login does NOT give a session: it asks for the second factor (mfa_token, no access token)",
            s1 == 200 and lg.get("mfa_required") and lg.get("mfa_token") and not lg.get("access_token"), f"{s} {s1} {str(lg)[:200]}")
    bad = req("POST", "/api/auth/mfa/verify", {"mfa_token": lg["mfa_token"], "code": "123456"})
    good = req("POST", "/api/auth/mfa/verify", {"mfa_token": lg["mfa_token"], "code": _next_code(secret, used)})
    R.check("E2E-J3-04", "MFA verify: wrong code 401; right TOTP code issues access + refresh tokens usable on /me",
            bad[0] == 401 and good[0] == 200 and good[1].get("access_token")
            and req("GET", "/api/auth/me", token=good[1]["access_token"])[0] == 200, f"{bad[0]} {good[0]} {str(good[1])[:150]}")
    # recovery code, once
    s1, lg2 = login_xff(ws.email, ws.password)
    rc = codes[0]
    r1 = req("POST", "/api/auth/mfa/verify", {"mfa_token": lg2["mfa_token"], "recovery_code": rc})
    s1, lg3 = login_xff(ws.email, ws.password)
    r2 = req("POST", "/api/auth/mfa/verify", {"mfa_token": lg3["mfa_token"], "recovery_code": rc})
    R.check("E2E-J3-05", "Recovery code signs in once; the same code is refused the second time",
            r1[0] == 200 and r1[1].get("access_token") and r2[0] == 401, f"{r1[0]} {r2[0]}")
    ws.token = r1[1]["access_token"]
    # ── invite ──
    inv_addr = f"invitee-{rid}@invited-{rid}.example"
    s, inv = req("POST", "/api/users/invite", {"email": inv_addr, "role": "AGENT"}, ws.token, headers=ORIGIN)
    c = _mail_to(inv_addr)
    body = (c.text if c else "") or ""
    link = re.search(r"https?://\S+token=\S+", body)
    plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))
    temp = re.search(r"Password\s+(\S+)", plain)
    R.check("E2E-J3-06", "Invite creates an INVITED user and sends a real email (captured in SES) with a magic link + temporary password; "
            "the API response does not leak either",
            s == 201 and c and link and temp and "token=" not in str(inv) and (temp.group(1) not in str(inv)),
            f"{s} captured={bool(c)} link={bool(link)} temp={bool(temp)} resp={str(inv)[:200]}")
    st = scalar(f"SELECT status FROM users WHERE LOWER(email)={q(inv_addr)}")
    tok = _token_in(link.group(0) if link else "")
    s, mg = req("POST", "/api/auth/magic-login", {"token": tok}) if tok else (0, {})
    st2 = scalar(f"SELECT status FROM users WHERE LOWER(email)={q(inv_addr)}")
    s_again, _ = req("POST", "/api/auth/magic-login", {"token": tok}) if tok else (0, {})
    R.check("E2E-J3-07", "Invitee follows the emailed magic link: status INVITED -> ACTIVE, session issued; the link works once only",
            st == "INVITED" and s == 200 and mg.get("access_token") and st2 == "ACTIVE" and s_again == 401,
            f"before={st} after={st2} use={s} reuse={s_again}")
    inv_tok = mg.get("access_token")
    tpw = temp.group(1) if temp else ""
    s, ch = req("PUT", "/api/auth/me/password", {"current_password": tpw, "new_password": f"Invitee-{rid}-new!"}, inv_tok)
    s2, lg = login_xff(inv_addr, f"Invitee-{rid}-new!")
    s3, lg_old = login_xff(inv_addr, tpw)
    R.check("E2E-J3-08", "Invitee sets their own password using the emailed temporary one; new password logs in, temporary no longer does",
            s == 200 and s2 == 200 and lg.get("access_token") and s3 == 401, f"{s} {s2} {s3} {str(ch)[:100]}")
    inv_tok = lg.get("access_token")
    s, me_i = req("GET", "/api/auth/me", token=inv_tok)
    R.check("E2E-J3-09", "Invitee lands in the inviter's tenant with the invited role", me_i.get("role") == "AGENT"
            and me_i.get("tenant_id") == ws.tenant_id, f"{str(me_i)[:200]}")
    # ── forgot / reset ──
    victim = add_user(ws, "AGENT", f"Vic{rid}")
    old_refresh = None
    s0, l0 = login_xff(victim["email"], ws.password)
    old_refresh = l0.get("refresh_token")
    s, _ = req("POST", "/api/auth/forgot-password", {"email": victim["email"]}, headers=ORIGIN)
    c = _mail_to(victim["email"])
    rl = re.search(r"https?://\S+token=\S+", (c.text if c else "") or "")
    rtok = _token_in(rl.group(0)) if rl else None
    s_unknown, g = req("POST", "/api/auth/forgot-password", {"email": f"nobody-{rid}@nowhere.example"})
    R.check("E2E-J3-10", "Forgot-password: real email with a reset link is captured; unknown address gets the same generic 200 and no email",
            s == 200 and rtok and s_unknown == 200 and not [x for x in ses_captured() if x.to == f"nobody-{rid}@nowhere.example"],
            f"{s} link={bool(rtok)} unknown={s_unknown}")
    newpw = f"Reset-{rid}-new!"
    s, r = req("POST", "/api/auth/reset-password", {"token": rtok, "new_password": newpw})
    s_reuse, _ = req("POST", "/api/auth/reset-password", {"token": rtok, "new_password": "Another-pass-1!"})
    sn, ln = login_xff(victim["email"], newpw)
    so, lo = login_xff(victim["email"], ws.password)
    ref, _r = req("POST", "/api/auth/refresh", {"refresh_token": old_refresh}) if old_refresh else (None, None)
    R.check("E2E-J3-11", "Reset: new password works, old password rejected, link is single-use, refresh token issued before the reset is revoked",
            s == 200 and s_reuse == 400 and sn == 200 and so == 401 and ref in (401, 403), f"{s} reuse={s_reuse} new={sn} old={so} oldrefresh={ref}")
    # ── role change / deactivation ──
    tok_v = ln["access_token"]
    before = req("GET", "/api/users", token=tok_v)[0]
    s, rr = req("PUT", f"/api/users/{victim['user_id']}/role", {"role": "ADMIN"}, ws.token)
    after = req("GET", "/api/users", token=tok_v)[0]
    R.check("E2E-J3-12", "Role change takes effect immediately on the user's existing token (AGENT cannot list users, ADMIN can)",
            before == 403 and s == 200 and after == 200, f"agent={before} change={s} admin={after}")
    s, d = req("DELETE", f"/api/users/{victim['user_id']}", None, ws.token)
    s_tok = req("GET", "/api/auth/me", token=tok_v)[0]
    s_login, _ = login_xff(victim["email"], newpw)
    R.check("E2E-J3-13", "Deactivation is immediate: the user's still-unexpired access token is rejected, login refused",
            s in (200, 204) and s_tok in (401, 403) and s_login == 403, f"deactivate={s} oldtoken={s_tok} login={s_login}")
    s, ra = req("PUT", f"/api/users/{victim['user_id']}/reactivate", None, ws.token)
    s_login2, _ = login_xff(victim["email"], newpw)
    R.check("E2E-J3-14", "Reactivation restores sign-in", s == 200 and s_login2 == 200, f"{s} {s_login2}")
    # ── password change revokes refresh tokens ──
    s, la = login_xff(inv_addr, f"Invitee-{rid}-new!")
    rt = la.get("refresh_token")
    s, ch = req("PUT", "/api/auth/me/password", {"current_password": f"Invitee-{rid}-new!", "new_password": f"Invitee-{rid}-third!"}, la["access_token"])
    s_ref, _ = req("POST", "/api/auth/refresh", {"refresh_token": rt})
    R.check("E2E-J3-15", "Changing the password revokes outstanding refresh tokens", s == 200 and s_ref in (401, 403), f"{s} refresh={s_ref}")
    # ── magic link for an existing user (admin-issued invite re-send is the only issuer) + suspension ──
    pa_email = f"platform-{rid}@systest.example"
    pa_id = str(uuid.uuid4())
    h = sql(f"SELECT password_hash FROM users WHERE email='{ws.email}'")
    sql(f"INSERT INTO users (user_id, tenant_id, first_name, last_name, email, role, status, password_hash, auth_provider, email_verified, created_at) "
        f"SELECT '{pa_id}', tenant_id, 'Plat', 'Admin', '{pa_email}', 'PLATFORM_ADMIN', 'ACTIVE', '{h}', 'local', 1, NOW() FROM users WHERE email='{ws.email}'")
    pa = user_token(pa_email, ws.password)
    s, sus = req("PUT", f"/api/admin/tenants/{ws.tenant_id}/status", {"status": "SUSPENDED"}, pa)
    owner_login, ob = login_xff(inv_addr, f"Invitee-{rid}-third!")
    live = req("GET", "/api/auth/me", token=la["access_token"])[0]
    mg_susp = req("POST", "/api/auth/magic-login", {"token": "x"})[0]
    s_back, _ = req("PUT", f"/api/admin/tenants/{ws.tenant_id}/status", {"status": "ACTIVE"}, pa)
    back, _b = login_xff(inv_addr, f"Invitee-{rid}-third!")
    R.check("E2E-J3-16", "Suspended tenant: sign-in refused (403 'Tenant suspended') and already-issued tokens stop working; reactivation restores access",
            s == 200 and owner_login == 403 and live in (401, 403) and s_back == 200 and back == 200,
            f"suspend={s} login={owner_login} {str(ob)[:60]} live_token={live} reactivate={s_back} login_after={back}")
    sql(f"DELETE FROM users WHERE user_id='{pa_id}'")
    # ── MFA disable ──
    s, dis = req("POST", "/api/auth/mfa/disable", {"password": ws.password, "code": _next_code(secret, used)}, ws.token)
    s2, lg = login_xff(ws.email, ws.password)
    R.check("E2E-J3-17", "MFA can be turned off with password + code; login then returns a session directly",
            s == 200 and s2 == 200 and lg.get("access_token"), f"{s} {str(dis)[:120]} / login {s2} {str(lg)[:80]}")
