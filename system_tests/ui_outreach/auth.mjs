import { startSuite, createWs, fullWorkspace, api, sql, q, sha256, go, body, waitText, clean, WEB, enableMfa, freshTotp, rid, sleep } from './lib.mjs';
import crypto from 'node:crypto';

const S = await startSuite('auth');
const { rec, browser } = S;
const A = 'UIO-AUTH';
const ws = await fullWorkspace('auth', { roles: ['agent'] });

async function anon(vp) {
  const ctx = await browser.newContext({ viewport: vp || { width: 1440, height: 900 } });
  S.ctxs.push(ctx);
  const page = await ctx.newPage(); page.setDefaultTimeout(12000);
  const { watch } = await import('./lib.mjs');
  return { ctx, page, w: watch(page) };
}
const LOGIN_ALLOW = [/auth\/login/, /auth\/mfa\/verify/, /auth\/session/, /Failed to load resource/];
const mkUser = async (label, extra = {}) => { // fresh user in the workspace with a known password
  const email = `${label}-${ws.rid}-${rid()}@uitest.example`;
  const hash = sql(`SELECT password_hash FROM users WHERE email=${q(ws.email)}`);
  const id = crypto.randomUUID();
  const cols = { user_id: id, tenant_id: ws.tenantId, first_name: label, last_name: 'Test', email, role: 'AGENT', status: 'ACTIVE', password_hash: hash, auth_provider: 'local', email_verified: 1, ...extra };
  sql(`INSERT INTO users (${Object.keys(cols).join(',')}, created_at) VALUES (${Object.values(cols).map(q).join(',')}, NOW())`);
  return { email, id, password: ws.password };
};
const newToken = () => crypto.randomBytes(24).toString('base64url');

// ---------- Login ----------
{
  const { page, w } = await anon();
  await S.t(`${A}-01`, 'Login page renders email/password fields, Forgot password link, no problems', async () => {
    await go(page, '/login');
    await page.getByPlaceholder('your@email.com').waitFor();
    await page.getByRole('link', { name: 'Forgot password?' }).waitFor();
    return await clean(page, w);
  }, page);
  await S.t(`${A}-02`, 'Empty submit shows "Email and password are required" and sends no request', async () => {
    let called = false; page.on('request', r => { if (r.url().includes('auth/login')) called = true; });
    await page.getByRole('button', { name: 'Login' }).click();
    await waitText(page, /Email and password are required/);
    if (called) return 'login request sent despite empty form';
  }, page);
  await S.t(`${A}-03`, 'Wrong password shows "Invalid email or password", stays on /login, no cookie session', async () => {
    await page.getByPlaceholder('your@email.com').fill(ws.email);
    await page.getByPlaceholder('••••••••').fill('definitely-wrong-pw');
    await page.getByRole('button', { name: 'Login' }).click();
    await waitText(page, /Invalid email or password/);
    if (!/\/login/.test(page.url())) return 'left login page: ' + page.url();
    const ck = (await page.context().cookies()).filter(c => c.name === 'access_token');
    if (ck.length) return 'access cookie set after failed login';
    const p = w.drain(LOGIN_ALLOW); if (p.length) return p.join(';');
  }, page);
  await S.t(`${A}-04`, 'Password show/hide toggle switches input type', async () => {
    const pw = page.getByPlaceholder('••••••••');
    const btn = pw.locator('xpath=following-sibling::button');
    await btn.click(); if (await pw.getAttribute('type') !== 'text') return 'did not show';
    await btn.click(); if (await pw.getAttribute('type') !== 'password') return 'did not hide';
  }, page);
  await S.t(`${A}-05`, 'Forgot password link navigates to /forgot-password', async () => {
    await page.getByRole('link', { name: 'Forgot password?' }).click();
    await page.waitForURL(/forgot-password/);
  }, page);
  await S.t(`${A}-06`, 'Suspended account: login shows "Account is suspended or inactive"', async () => {
    const u = await mkUser('susp', { status: 'SUSPENDED' });
    await go(page, '/login');
    await page.getByPlaceholder('your@email.com').fill(u.email);
    await page.getByPlaceholder('••••••••').fill(u.password);
    await page.getByRole('button', { name: 'Login' }).click();
    await waitText(page, /suspended or inactive/i);
    const p = w.drain(LOGIN_ALLOW); if (p.length) return p.join(';');
  }, page);
  await S.t(`${A}-07`, 'Locked after 10 failed attempts: UI shows "Too many failed sign-in attempts" (429), correct password also refused', async () => {
    const u = await mkUser('lock');
    for (let i = 0; i < 10; i++) await api('POST', '/api/auth/login', { headers: { 'X-Forwarded-For': ws.xff }, body: { email: u.email, password: 'bad-' + i } });
    await go(page, '/login');
    await page.getByPlaceholder('your@email.com').fill(u.email);
    await page.getByPlaceholder('••••••••').fill(u.password);
    await page.getByRole('button', { name: 'Login' }).click();
    await waitText(page, /Too many failed sign-in attempts/i);
    if (!/\/login/.test(page.url())) return 'signed in while locked';
    w.drain(LOGIN_ALLOW);
  }, page);
  await S.t(`${A}-08`, 'Login API 500 degrades to a readable message (no crash / [object Object])', async () => {
    await go(page, '/login');
    await page.route('**/auth/login', r => r.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"Internal Server Error"}' }));
    await page.getByPlaceholder('your@email.com').fill(ws.email);
    await page.getByPlaceholder('••••••••').fill('x');
    await page.getByRole('button', { name: 'Login' }).click();
    await page.waitForTimeout(800);
    const t = await body(page); await page.unroute('**/auth/login');
    if (!/Internal Server Error|Invalid email or password|went wrong/i.test(t)) return 'no error message: ' + t.slice(-120);
    w.drain([/.*/]); const bp = await w.bodyProblems(); if (bp.length) return bp.join(',');
  }, page);
  await S.t(`${A}-09`, 'Successful login lands on Home (/app/dashboard) with session cookies and stored user', async () => {
    const u = ws.users.agent;
    await go(page, '/login');
    await page.getByPlaceholder('your@email.com').fill(u.email);
    await page.getByPlaceholder('••••••••').fill(ws.password);
    await page.getByRole('button', { name: 'Login' }).click();
    await page.waitForURL(/\/app\/dashboard/);
    const names = (await page.context().cookies()).map(c => c.name);
    if (!names.includes('access_token') || !names.includes('refresh_token')) return 'missing cookies: ' + names;
    const user = await page.evaluate(() => JSON.parse(localStorage.getItem('user') || 'null'));
    if (!user || user.email !== u.email || user.role !== 'AGENT') return 'stored user wrong ' + JSON.stringify(user);
    if (await page.evaluate(() => !!(localStorage.getItem('access_token') || localStorage.getItem('refresh_token')))) return 'token in localStorage';
    const p = await clean(page, w, [/auth\/login/]); if (p) return p;
  }, page);
  await S.t(`${A}-10`, 'Authenticated user opening /login is redirected to the app (PublicRoute)', async () => {
    await page.goto(WEB + '/login'); await page.waitForURL(/\/app\/dashboard/);
  }, page);
  await S.t(`${A}-11`, 'Logout via profile menu returns to /login, clears stored user + cookies; Back does not reopen the app', async () => {
    await page.getByRole('button', { name: 'Account' }).click();
    await page.getByRole('button', { name: /sign out|log ?out/i }).first().click();
    await page.waitForURL(/\/login/);
    const user = await page.evaluate(() => localStorage.getItem('user'));
    if (user) return 'user still stored';
    const ck = (await page.context().cookies()).filter(c => c.name === 'access_token' && c.value);
    if (ck.length) return 'access cookie remains';
    await page.goBack().catch(() => {}); await page.waitForTimeout(1200);
    if (/\/app\//.test(page.url())) return 'Back reopened app: ' + page.url();
    const ses = await page.evaluate(async () => (await fetch('/vector/api/api/auth/session', { credentials: 'include' })).status);
    if (ses === 200) return 'session still valid server-side after logout';
  }, page);
}

// ---------- Forgot password ----------
{
  const { page, w } = await anon();
  const owner = ws.users.owner;
  await S.t(`${A}-20`, 'ForgotPassword renders; empty email shows Email is required and sends nothing', async () => {
    await go(page, '/forgot-password');
    const input = page.getByPlaceholder('your@email.com'); await input.waitFor();
    let called = false; page.on('request', r => { if (r.url().includes('forgot-password')) called = true; });
    await page.locator('form button[type=submit]').click();
    await waitText(page, /Email is required/);
    if (called) return 'request sent with empty email';
  }, page);
  await S.t(`${A}-21`, 'Known email: generic success message and a reset-token row is stored (hash only)', async () => {
    const before = Number(sql(`SELECT COUNT(*) FROM password_reset_tokens WHERE user_id=${q(owner.userId)}`));
    await page.getByPlaceholder('your@email.com').fill(owner.email);
    await page.locator('form button[type=submit]').click();
    await waitText(page, /If an account exists/i);
    const after = Number(sql(`SELECT COUNT(*) FROM password_reset_tokens WHERE user_id=${q(owner.userId)}`));
    if (after !== before + 1) return `token rows ${before}->${after}`;
    const p = await clean(page, w); if (p) return p;
  }, page);
  await S.t(`${A}-22`, 'Unknown email shows the same generic message (no enumeration) and stores nothing', async () => {
    await go(page, '/forgot-password');
    const email = `nobody-${rid()}@uitest.example`;
    const before = sql(`SELECT COUNT(*) FROM password_reset_tokens`);
    await page.getByPlaceholder('your@email.com').fill(email);
    await page.locator('form button[type=submit]').click();
    await waitText(page, /If an account exists/i);
    if (sql(`SELECT COUNT(*) FROM password_reset_tokens`) !== before) return 'token created for unknown email';
  }, page);
  await S.t(`${A}-23`, 'Forgot password 500 -> readable error, no crash', async () => {
    await go(page, '/forgot-password');
    await page.route('**/auth/forgot-password', r => r.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"boom"}' }));
    await page.getByPlaceholder('your@email.com').fill(owner.email);
    await page.locator('form button[type=submit]').click();
    await page.waitForTimeout(900); await page.unroute('**/auth/forgot-password');
    const t = await body(page);
    if (/\[object Object\]|undefined/.test(t)) return 'bad text';
    if (!/boom|failed|error|try again|went wrong/i.test(t)) return 'no error shown: ' + t.slice(-150);
    w.drain([/.*/]);
  }, page);
}

// ---------- Reset password ----------
{
  const { page, w } = await anon();
  const u = await mkUser('reset');
  const addTok = (uid, { expired = false } = {}) => {
    const raw = newToken();
    sql(`INSERT INTO password_reset_tokens (token_id,user_id,token_hash,expires_at) VALUES (UUID(),${q(uid)},${q(sha256(raw))}, ${expired ? 'NOW() - INTERVAL 1 HOUR' : 'NOW() + INTERVAL 1 HOUR'})`);
    return raw;
  };
  const fill = async (a, b) => { await page.getByPlaceholder('Minimum 8 characters').fill(a); await page.getByPlaceholder('Re-enter password').fill(b); await page.locator('form button[type=submit]').click(); };
  await S.t(`${A}-30`, 'ResetPassword without token: submitting shows "Invalid or missing reset token"', async () => {
    await go(page, '/reset-password'); await fill('Newpass-1234', 'Newpass-1234'); await waitText(page, /Invalid or missing reset token/);
  }, page);
  const tok = addTok(u.id);
  await S.t(`${A}-31`, 'Short password rejected client-side (<8)', async () => {
    await go(page, '/reset-password?token=' + tok); await fill('short', 'short'); await waitText(page, /at least 8 characters/);
  }, page);
  await S.t(`${A}-32`, 'Mismatched confirmation rejected', async () => {
    await fill('Newpass-1234', 'Newpass-9999'); await waitText(page, /do not match/i);
  }, page);
  await S.t(`${A}-33`, 'Valid token: success message, redirect to /login, token marked used, new password works / old does not', async () => {
    await fill('Newpass-1234', 'Newpass-1234');
    await waitText(page, /Password reset successful/i);
    await page.waitForURL(/\/login/, { timeout: 6000 });
    const used = sql(`SELECT used_at IS NOT NULL FROM password_reset_tokens WHERE token_hash=${q(sha256(tok))}`);
    if (used !== '1') return 'token not marked used';
    const ok = await api('POST', '/api/auth/login', { headers: { 'X-Forwarded-For': ws.xff }, body: { email: u.email, password: 'Newpass-1234' } });
    const old = await api('POST', '/api/auth/login', { headers: { 'X-Forwarded-For': ws.xff }, body: { email: u.email, password: ws.password } });
    if (ok.status !== 200) return 'new password rejected ' + ok.status;
    if (old.status !== 401) return 'old password still works ' + old.status;
    w.drain([/reset-password/]);
  }, page);
  await S.t(`${A}-34`, 'Reusing the token shows "Invalid or expired reset token"', async () => {
    await go(page, '/reset-password?token=' + tok); await fill('Another-1234', 'Another-1234'); await waitText(page, /Invalid or expired reset token/);
    w.drain([/reset-password/]);
  }, page);
  await S.t(`${A}-35`, 'Expired token shows "Reset token has expired"', async () => {
    const t2 = addTok(u.id, { expired: true });
    await go(page, '/reset-password?token=' + t2); await fill('Another-1234', 'Another-1234'); await waitText(page, /expired/i);
    w.drain([/reset-password/]);
  }, page);
}

// ---------- Accept invite ----------
{
  const { page, w } = await anon();
  const mkInv = ({ expired = false, status = 'PENDING', email } = {}) => {
    const raw = newToken(); const em = email || `invitee-${rid()}@uitest.example`;
    sql(`INSERT INTO invitations (invitation_id,tenant_id,invited_by,email,role,token,status,expires_at,created_at) VALUES (UUID(),${q(ws.tenantId)},${q(ws.userId)},${q(em)},'MANAGER',${q(sha256(raw))},${q(status)}, ${expired ? 'NOW() - INTERVAL 1 DAY' : 'NOW() + INTERVAL 2 DAY'}, NOW())`);
    return { raw, email: em };
  };
  const fillInv = async (f, l, p, c) => {
    await page.getByPlaceholder('First name').fill(f); await page.getByPlaceholder('Last name').fill(l);
    await page.getByPlaceholder('Minimum 8 characters').fill(p); await page.getByPlaceholder('Re-enter password').fill(c);
    await page.locator('form button[type=submit]').click();
  };
  const inv = mkInv();
  await S.t(`${A}-40`, 'AcceptInvite without token: "Invalid or missing invitation token"', async () => {
    await go(page, '/accept-invite'); await fillInv('A', 'B', 'Password-123', 'Password-123'); await waitText(page, /Invalid or missing invitation token/);
  }, page);
  await S.t(`${A}-41`, 'Validation: names required, password >= 8, confirmation matches', async () => {
    await go(page, '/accept-invite?token=' + inv.raw);
    await page.getByPlaceholder('First name').fill('X'); await page.getByPlaceholder('Last name').fill(' ');
    await page.getByPlaceholder('Minimum 8 characters').fill('Password-123'); await page.getByPlaceholder('Re-enter password').fill('Password-123');
    await page.locator('form button[type=submit]').click();
    await waitText(page, /First name and last name are required/);
    await fillInv('Ina', 'Vitee', 'short', 'short'); await waitText(page, /at least 8 characters/);
    await fillInv('Ina', 'Vitee', 'Password-123', 'Password-999'); await waitText(page, /do not match/i);
  }, page);
  await S.t(`${A}-42`, 'Invalid token -> server message shown (404 "Invalid or expired invitation")', async () => {
    await go(page, '/accept-invite?token=' + newToken()); await fillInv('Ina', 'Vitee', 'Password-123', 'Password-123');
    await waitText(page, /Invalid or expired invitation/); w.drain([/accept-invite/]);
  }, page);
  await S.t(`${A}-43`, 'Expired invitation -> "Invitation has expired" and invitation row EXPIRED', async () => {
    const e = mkInv({ expired: true });
    await go(page, '/accept-invite?token=' + e.raw); await fillInv('Ina', 'Vitee', 'Password-123', 'Password-123');
    await waitText(page, /expired/i);
    if (sql(`SELECT status FROM invitations WHERE token=${q(sha256(e.raw))}`) !== 'EXPIRED') return 'status not EXPIRED';
    w.drain([/accept-invite/]);
  }, page);
  await S.t(`${A}-44`, 'Valid invitation: account created (role/tenant from invite), redirect to /login, can then log in', async () => {
    await go(page, '/accept-invite?token=' + inv.raw); await fillInv('Ina', 'Vitee', 'Password-123', 'Password-123');
    await waitText(page, /created|success/i);
    await page.waitForURL(/\/login/, { timeout: 6000 });
    const row = sql(`SELECT role,first_name,last_name,status,tenant_id FROM users WHERE email=${q(inv.email)}`).split('\t');
    if (row[0] !== 'MANAGER' || row[1] !== 'Ina' || row[4] !== ws.tenantId) return 'user row ' + row.join('|');
    if (sql(`SELECT status FROM invitations WHERE token=${q(sha256(inv.raw))}`) !== 'ACCEPTED') return 'invitation not ACCEPTED';
    const lr = await api('POST', '/api/auth/login', { headers: { 'X-Forwarded-For': ws.xff }, body: { email: inv.email, password: 'Password-123' } });
    if (lr.status !== 200) return 'cannot login ' + lr.status + ' ' + JSON.stringify(lr.data);
    w.drain([/accept-invite/]);
  }, page);
  await S.t(`${A}-45`, 'Re-using the accepted invitation shows an error, no duplicate user', async () => {
    await go(page, '/accept-invite?token=' + inv.raw); await fillInv('Ina', 'Vitee', 'Password-123', 'Password-123');
    await waitText(page, /Invalid or expired|already exists/i);
    if (sql(`SELECT COUNT(*) FROM users WHERE email=${q(inv.email)}`) !== '1') return 'duplicate users';
    w.drain([/accept-invite/]);
  }, page);
}

// ---------- Magic login + set password ----------
{
  const mkMagic = (uid, { expired = false } = {}) => {
    const raw = newToken();
    sql(`INSERT INTO magic_login_tokens (token_id,user_id,token_hash,expires_at) VALUES (UUID(),${q(uid)},${q(sha256(raw))}, ${expired ? 'NOW() - INTERVAL 1 HOUR' : 'NOW() + INTERVAL 1 HOUR'})`);
    return raw;
  };
  // I-13: an invited account has no password; it joins with the emailed link and chooses one
  const inv = await mkUser('magic', { status: 'INVITED', password_hash: null });
  const tok = mkMagic(inv.id);
  let page2, w2;
  {
    const a = await anon(); page2 = a.page; w2 = a.w;
    await S.t(`${A}-50`, 'MagicLogin without token: "Missing magic login token"', async () => {
      await go(page2, '/magic-login'); await waitText(page2, /Missing magic login token/);
    }, page2);
    await S.t(`${A}-51`, 'MagicLogin with unknown token shows error (not blank)', async () => {
      await go(page2, '/magic-login?token=' + newToken()); await waitText(page2, /Invalid or expired|failed/i); w2.drain([/magic-login/]);
    }, page2);
    await S.t(`${A}-52`, 'Expired magic token shows "has expired"', async () => {
      await go(page2, '/magic-login?token=' + mkMagic(inv.id, { expired: true })); await waitText(page2, /expired/i); w2.drain([/magic-login/]);
    }, page2);
    await S.t(`${A}-53`, 'Valid magic link on INVITED user: signs in, activates user, goes to /set-password; token single use', async () => {
      await go(page2, '/magic-login?token=' + tok, { wait: 0 });
      await page2.waitForURL(/set-password/, { timeout: 10000 });
      if (sql(`SELECT status FROM users WHERE user_id=${q(inv.id)}`) !== 'ACTIVE') return 'user not activated';
      if (sql(`SELECT used_at IS NOT NULL FROM magic_login_tokens WHERE token_hash=${q(sha256(tok))}`) !== '1') return 'token not consumed';
      const p = await clean(page2, w2); if (p) return p;
    }, page2);
    await S.t(`${A}-54`, 'SetPassword validation (short, mismatch); no temporary/current password is asked', async () => {
      if (await page2.getByPlaceholder('Enter the password from your invite email').count()) return 'asks for a temporary password';
      if (await page2.getByText(/temporary password/i).count()) return 'mentions a temporary password';
      if (await api('POST', '/api/auth/login', { headers: { 'X-Forwarded-For': ws.xff }, body: { email: inv.email, password: ws.password } })
        .then(r => r.status === 200)) return 'invited account signed in with a password';
      await page2.getByPlaceholder('At least 8 characters').fill('short'); await page2.getByPlaceholder('Repeat new password').fill('short');
      await page2.locator('form button[type=submit]').click(); await waitText(page2, /at least 8 characters/);
      await page2.getByPlaceholder('At least 8 characters').fill('Brandnew-123'); await page2.getByPlaceholder('Repeat new password').fill('Brandnew-124');
      await page2.locator('form button[type=submit]').click(); await waitText(page2, /do not match/i);
      await page2.getByPlaceholder('Repeat new password').fill('Brandnew-123');
    }, page2);
    await S.t(`${A}-55`, 'SetPassword success: confirmation then Home; new password works', async () => {
      await page2.locator('form button[type=submit]').click();
      await page2.waitForURL(/\/app\/dashboard/, { timeout: 8000 });
      const lr = await api('POST', '/api/auth/login', { headers: { 'X-Forwarded-For': ws.xff }, body: { email: inv.email, password: 'Brandnew-123' } });
      if (lr.status !== 200) return 'new pw login ' + lr.status;
    }, page2);
    await S.t(`${A}-56`, 'Used magic token cannot be replayed', async () => {
      const b = await anon();
      await go(b.page, '/magic-login?token=' + tok); await waitText(b.page, /Invalid or expired|expired/i);
    }, page2);
  }
}

// ---------- MFA second step ----------
{
  const u = await mkUser('mfa');
  const tok = (await api('POST', '/api/auth/login', { headers: { 'X-Forwarded-For': ws.xff }, body: { email: u.email, password: u.password } })).data.access_token;
  const m = await enableMfa(tok);
  const { page, w } = await anon();
  const doLogin = async () => {
    await go(page, '/login');
    await page.getByPlaceholder('your@email.com').fill(u.email);
    await page.getByPlaceholder('••••••••').fill(u.password);
    await page.getByRole('button', { name: 'Login' }).click();
  };
  await S.t(`${A}-60`, 'MFA user: correct password shows the second step (no session yet)', async () => {
    await doLogin(); await page.getByLabel(/code/i).first().waitFor();
    const ck = (await page.context().cookies()).filter(c => c.name === 'access_token' && c.value);
    if (ck.length) return 'session issued before second factor';
    if (page.url().includes('/app/')) return 'in app before MFA';
  }, page);
  await S.t(`${A}-61`, 'Short code rejected client-side; wrong code shows server message', async () => {
    const inp = page.getByLabel(/code/i).first();
    await inp.fill('123'); await page.locator('form button[type=submit]').click(); await waitText(page, /6-digit code/);
    await inp.fill('000000'); await page.locator('form button[type=submit]').click(); await waitText(page, /isn't valid|Invalid/i);
    w.drain(LOGIN_ALLOW);
  }, page);
  await S.t(`${A}-62`, 'Correct TOTP completes sign-in to Home', async () => {
    const inp = page.getByLabel(/code/i).first();
    await inp.fill(await freshTotp(m.secret)); await page.locator('form button[type=submit]').click();
    await page.waitForURL(/\/app\/dashboard/);
    const p = await clean(page, w, LOGIN_ALLOW); if (p) return p;
  }, page);
  await S.t(`${A}-63`, 'Recovery code path signs in once; the same recovery code is then refused', async () => {
    const c = await api('POST', '/api/auth/login', { headers: { 'X-Forwarded-For': ws.xff }, body: { email: u.email, password: u.password } });
    // fresh anonymous browser context for the recovery path
    const b = await anon();
    await go(b.page, '/login');
    await b.page.getByPlaceholder('your@email.com').fill(u.email); await b.page.getByPlaceholder('••••••••').fill(u.password);
    await b.page.getByRole('button', { name: 'Login' }).click();
    await b.page.getByRole('button', { name: /recovery code/i }).click();
    await b.page.getByLabel(/recovery/i).first().fill(m.recovery[0]); await b.page.locator('form button[type=submit]').click();
    await b.page.waitForURL(/\/app\/dashboard/);
    const b2 = await anon();
    await go(b2.page, '/login');
    await b2.page.getByPlaceholder('your@email.com').fill(u.email); await b2.page.getByPlaceholder('••••••••').fill(u.password);
    await b2.page.getByRole('button', { name: 'Login' }).click();
    await b2.page.getByRole('button', { name: /recovery code/i }).click();
    await b2.page.getByLabel(/recovery/i).first().fill(m.recovery[0]); await b2.page.locator('form button[type=submit]').click();
    await waitText(b2.page, /isn't valid|already used/i);
  }, page);
  await S.t(`${A}-64`, 'Cancel on the second step returns to the email/password form', async () => {
    const b = await anon();
    await go(b.page, '/login');
    await b.page.getByPlaceholder('your@email.com').fill(u.email); await b.page.getByPlaceholder('••••••••').fill(u.password);
    await b.page.getByRole('button', { name: 'Login' }).click();
    await b.page.getByLabel(/code/i).first().waitFor();
    await b.page.locator('button[type=button]', { hasText: /back|cancel|sign in/i }).first().click();
    await b.page.getByPlaceholder('your@email.com').waitFor();
  }, page);
  await S.t(`${A}-65`, 'MFA user via magic link also gets the second step', async () => {
    const raw = newToken();
    sql(`INSERT INTO magic_login_tokens (token_id,user_id,token_hash,expires_at) VALUES (UUID(),${q(u.id)},${q(sha256(raw))}, NOW() + INTERVAL 1 HOUR)`);
    const b = await anon(); await go(b.page, '/magic-login?token=' + raw);
    await b.page.getByLabel(/code/i).first().waitFor();
    await b.page.getByLabel(/code/i).first().fill(await freshTotp(m.secret)); await b.page.locator('form button[type=submit]').click();
    await b.page.waitForURL(/\/app\/(dashboard)|set-password/);
  }, page);
}

// ---------- Session expiry ----------
{
  const { page, w } = await S.open(ws, 'agent');
  await S.t(`${A}-70`, 'Deleting only the access cookie: next navigation silently refreshes (stays signed in)', async () => {
    const ctx = page.context();
    const all = await ctx.cookies();
    await ctx.clearCookies({ name: 'access_token' });
    await go(page, '/app/tasks', { wait: 1200 });
    if (/\/login/.test(page.url())) return 'redirected to login instead of silent refresh';
    const ck = (await ctx.cookies()).some(c => c.name === 'access_token' && c.value);
    if (!ck) return 'no new access cookie after refresh';
    w.drain([/.*/]);
  }, page);
  await S.t(`${A}-71`, 'Deleting all session cookies redirects to /login and clears stored user', async () => {
    const ctx = page.context();
    await ctx.clearCookies();
    await go(page, '/app/tasks', { wait: 1500 });
    await page.waitForURL(/\/login/, { timeout: 8000 });
    if (await page.evaluate(() => localStorage.getItem('user'))) return 'stored user kept';
    w.drain([/.*/]);
  }, page);
  await S.t(`${A}-72`, 'Unauthenticated deep link /app/campaigns redirects to /login, then back to the target after login', async () => {
    const b = await anon();
    await go(b.page, '/app/campaigns'); await b.page.waitForURL(/\/login/);
    await b.page.getByPlaceholder('your@email.com').fill(ws.users.admin?.email || ws.email);
    await b.page.getByPlaceholder('••••••••').fill(ws.password);
    await b.page.getByRole('button', { name: 'Login' }).click();
    await b.page.waitForURL(/\/app\/campaigns/, { timeout: 12000 });
  }, page);
}

await S.done();
