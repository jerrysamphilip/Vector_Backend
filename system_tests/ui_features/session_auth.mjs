// UIF-SESS: signed-out access, cookie-session expiry, CSRF, sign-out (BRD security / session handling).
//   01-08  signed-out visits to protected routes end on /login and show no workspace data (data-driven over ROUTES)
//   11-23  return-to-page after sign-in, API without credentials, silent refresh when the access cookie expires mid-use
//          (navigation and a form submit: retried with the rotated X-CSRF-Token), one refresh for parallel 401s, CSRF
//          enforcement, fully expired session -> login (and back to the page), sign-out clears state and revokes the
//          refresh token, back button after sign-out, sign-out in another tab, signed-in user on /login.
// The app keeps the session in httpOnly cookies (access_token, refresh_token, csrf_token; src/api/http.js refreshes on 401).
// "Expiry" is simulated by deleting the access_token cookie (and revoking refresh tokens in the DB for a dead session).
// Every browser session here is a fresh form login (never the shared cookie cache), so cookie surgery cannot leak into
// other suites. Uses the ui_crm helpers (own reusable tenant "session", wiped on every run).   node session_auth.mjs
import { settle, tc, ok, eq, must, call, sql, sq, standalone, isMain, Tenant, WEB } from '../ui_crm/crm_common.mjs';
import { anon, freshLogin, fillLogin, loginPaced, cookieNames, dropCookies, sidebarLink } from './_ui_extra.mjs';

const pad = n => String(n).padStart(2, '0');
const APP_PATH = new URL(WEB).pathname.replace(/\/$/, '');          // e.g. /vector
const API_PATH = `${APP_PATH}/api`;                                   // the SPA's API base (root-mounted routes)
const ALLOW = [/http 401/, /status of 401/];

export async function run(rec, browser) {
  const t = await (await Tenant.get('session')).init();
  const owner = t.owner;
  const contact = await must(t.token, 'POST', '/contacts', { email: `sess.${t.rid}@sess.example`, first_name: 'Sessa', last_name: `Private${t.rid}` });
  const SECRET = `Sessa Private${t.rid}`;
  const go = async (u, route, wait = 900) => { await u.page.goto(`${WEB}/${route.replace(/^\//, '')}`); await settle(u, wait); };
  const opened = [];
  const fresh = async (opts) => { const u = await freshLogin(browser, owner, opts); opened.push(u); return u; };

  // ---------- signed out ----------
  const ROUTES = ['app/dashboard', 'app/contacts', `app/contacts/${contact.prospect_id}`, 'app/deals', 'app/templates', 'app/settings', 'app/webhooks', 'admin'];
  const X = await anon(browser); opened.push(X);
  for (const [i, r] of ROUTES.entries()) {
    await tc(rec, `UIF-SESS-${pad(1 + i)}`, `Signed out: /${r} redirects to /login and shows no workspace data`, async () => {
      const dataCalls = [];
      const onResp = resp => { const u = new URL(resp.url()); if (u.pathname.startsWith(API_PATH) && !/\/auth\//.test(u.pathname) && resp.status() < 300) dataCalls.push(u.pathname); };
      X.page.on('response', onResp);
      try {
        await go(X, r, 700);
        ok(/\/login(\?|$)/.test(new URL(X.page.url()).pathname + new URL(X.page.url()).search), `ended on ${X.page.url()}`);
        await X.page.locator('input[type=email]').waitFor({ timeout: 5000 });
        ok(!(await X.page.locator('body').innerText()).includes(SECRET), 'contact name visible');
        eq(dataCalls, [], 'no successful data API calls while signed out');
      } finally { X.page.off('response', onResp); }
    }, { u: X, allow: ALLOW });
  }

  await tc(rec, 'UIF-SESS-11', 'Signing in after the redirect returns to the page that was requested (/app/deals)', async () => {
    const Y = await anon(browser); opened.push(Y);
    await go(Y, 'app/deals', 500);
    await Y.page.waitForURL(/\/login/, { timeout: 8000 });
    await loginPaced();
    await fillLogin(Y.page, owner.email, owner.password);
    await Y.page.waitForURL(/\/app\//, { timeout: 15000 });
    ok(/\/app\/deals$/.test(new URL(Y.page.url()).pathname), `landed on ${Y.page.url()}`);
  });

  await tc(rec, 'UIF-SESS-12', 'API without credentials -> 401 (no cookie, garbage bearer, tampered token)', async () => {
    eq((await call(null, 'GET', '/contacts')).status, 401, 'no credentials');
    eq((await call('not-a-jwt', 'GET', '/contacts')).status, 401, 'garbage bearer');
    const parts = t.token.split('.'); const tampered = `${parts[0]}.${parts[1]}.${parts[2].slice(0, -4)}AAAA`;
    eq((await call(tampered, 'GET', '/contacts')).status, 401, 'tampered signature');
  });

  // ---------- expiry mid-use ----------
  const S = await fresh();
  await tc(rec, 'UIF-SESS-13', 'Access cookie expires mid-use: navigating to Contacts refreshes silently (one POST /auth/refresh 200), page stays and shows data', async () => {
    const p = S.page;
    await go(S, 'app/dashboard');
    await dropCookies(S.ctx, ['access_token']);
    ok(!(await cookieNames(S.ctx)).includes('access_token'), 'cookie dropped');
    const refresh = p.waitForResponse(r => r.url().includes('/auth/refresh'), { timeout: 10000 });
    await (await sidebarLink(p, 'Contacts', 'CRM')).click();
    const rr = await refresh;
    eq(rr.status(), 200, 'refresh status');
    await p.getByText(SECRET).first().waitFor({ timeout: 10000 });
    ok(/\/app\/contacts/.test(p.url()), `still on ${p.url()}`);
    ok((await cookieNames(S.ctx)).includes('access_token'), 'new access cookie set');
  }, { u: S, allow: ALLOW });

  await tc(rec, 'UIF-SESS-14', 'Access cookie expires while a form is open: submit is retried after the refresh with the new X-CSRF-Token; contact saved, no login page', async () => {
    const p = S.page;
    await go(S, 'app/contacts');
    await p.getByRole('button', { name: /Add contact/ }).first().click();
    const m = p.locator('div.fixed.inset-0').last();
    const email = `retry.${t.rid}.${Date.now()}@sess.example`;
    await m.getByLabel('Email *').fill(email);
    await m.getByLabel('First name').fill('Retry'); await m.getByLabel('Last name').fill('Kept');
    const csrfBefore = (await S.ctx.cookies()).find(c => c.name === 'csrf_token')?.value;
    await dropCookies(S.ctx, ['access_token']);
    const posts = [];
    const onReq = r => { if (r.method() === 'POST' && new URL(r.url()).pathname === `${API_PATH}/contacts`) posts.push(r.headers()['x-csrf-token'] || ''); };
    p.on('request', onReq);
    try {
      await m.getByRole('button', { name: /Add contact/ }).click();
      await p.waitForURL(/\/app\/contacts\/[0-9a-f-]{36}/, { timeout: 15000 });
    } finally { p.off('request', onReq); }
    eq(sql(`SELECT CONCAT(first_name,' ',last_name) FROM prospects WHERE tenant_id=${sq(t.tenantId)} AND email=${sq(email)} AND deleted_at IS NULL`), 'Retry Kept', 'DB: contact created once with the typed values');
    eq(posts.length, 2, `POST attempts (original + retry): ${posts.length}`);
    const csrfAfter = (await S.ctx.cookies()).find(c => c.name === 'csrf_token')?.value;
    eq(posts[1], csrfAfter, 'retry carries the rotated CSRF token');
    ok(csrfBefore !== undefined, 'csrf cookie existed');
  }, { u: S, allow: ALLOW });

  await tc(rec, 'UIF-SESS-15', 'Several parallel 401s (full reload of a contact record without the access cookie) share ONE refresh; the record renders', async () => {
    const p = S.page;
    await dropCookies(S.ctx, ['access_token']);
    let refreshes = 0;
    const onReq = r => { if (r.url().includes('/auth/refresh')) refreshes++; };
    p.on('request', onReq);
    try {
      await go(S, `app/contacts/${contact.prospect_id}`, 1500);
      await p.getByRole('heading', { name: SECRET }).waitFor({ timeout: 10000 });
    } finally { p.off('request', onReq); }
    eq(refreshes, 1, 'refresh requests');
  }, { u: S, allow: ALLOW });

  await tc(rec, 'UIF-SESS-16', 'CSRF: a cookie-authenticated POST without / with a wrong X-CSRF-Token -> 403, nothing created; GET needs no token', async () => {
    const p = S.page;
    await go(S, 'app/dashboard', 500);
    const email = `csrf.${t.rid}.${Date.now()}@sess.example`;
    const res = await p.evaluate(async ({ base, email }) => {
      const body = JSON.stringify({ email, first_name: 'Csrf' });
      const h = { 'Content-Type': 'application/json' };
      const a = await fetch(`${base}/contacts`, { method: 'POST', credentials: 'include', headers: h, body });
      const b = await fetch(`${base}/contacts`, { method: 'POST', credentials: 'include', headers: { ...h, 'X-CSRF-Token': 'wrong' }, body });
      const c = await fetch(`${base}/contacts?page_size=1`, { credentials: 'include' });
      return [a.status, b.status, c.status, await a.text()];
    }, { base: API_PATH, email });
    eq(res.slice(0, 3), [403, 403, 200], `statuses (${res[3].slice(0, 80)})`);
    ok(/CSRF/i.test(res[3]), 'detail mentions CSRF');
    eq(sql(`SELECT COUNT(*) FROM prospects WHERE email=${sq(email)}`), '0', 'nothing created');
  }, { u: S, allow: [/http 403/, /status of 403/] });

  await tc(rec, 'UIF-SESS-17', 'Signed-in user opening /login is sent to /app/dashboard', async () => {
    await go(S, 'login', 800);
    ok(/\/app\/dashboard/.test(S.page.url()), `ended on ${S.page.url()}`);
  }, { u: S });

  // ---------- session fully expired ----------
  const E = await fresh();
  let deadUrl = '';
  await tc(rec, 'UIF-SESS-18', 'Session fully expired (access cookie gone, refresh token revoked): next action ends on /login with the stored user cleared', async () => {
    const p = E.page;
    await go(E, `app/deals?view=list`);
    deadUrl = new URL(p.url()).pathname + new URL(p.url()).search;
    await dropCookies(E.ctx, ['access_token']);
    sql(`UPDATE refresh_tokens SET revoked_at=UTC_TIMESTAMP() WHERE user_id=${sq(owner.userId)} AND revoked_at IS NULL`);
    await (await sidebarLink(p, 'Contacts', 'CRM')).click();
    await p.waitForURL(/\/login/, { timeout: 10000 });
    eq(await p.evaluate(() => localStorage.getItem('user')), null, 'stored user cleared');
    ok(!(await p.locator('body').innerText()).includes(SECRET), 'no contact data on the login page');
  }, { u: E, allow: ALLOW, noBody: true });

  await tc(rec, 'UIF-SESS-19', 'After a session expiry, signing in again returns to the page the user was on (not the dashboard)', async () => {
    const p = E.page;
    ok(/\/login/.test(p.url()), 'precondition: on the login page');
    await loginPaced();
    await fillLogin(p, owner.email, owner.password);
    await p.waitForURL(/\/app\//, { timeout: 15000 });
    const now = new URL(p.url()).pathname;
    ok(now === `${APP_PATH}/app/contacts` || now === deadUrl.split('?')[0], `landed on ${now}; the page in use was ${deadUrl} (http.js redirectToLogin() drops the location)`);
  }, { u: E });

  // ---------- sign out ----------
  const O = await fresh();
  let oldCookies = [];
  await tc(rec, 'UIF-SESS-20', 'Sign out (profile menu): /login, stored user cleared, auth cookies removed, the refresh token is revoked', async () => {
    const p = O.page;
    await go(O, `app/contacts/${contact.prospect_id}`, 1200);
    await p.getByRole('heading', { name: SECRET }).waitFor();
    oldCookies = await O.ctx.cookies();
    const rt = oldCookies.find(c => c.name === 'refresh_token')?.value;
    ok(rt, 'refresh cookie present before sign-out');
    await p.getByRole('button', { name: 'Account' }).click();
    await p.getByRole('button', { name: /Sign out/ }).click();
    await p.waitForURL(/\/login/, { timeout: 8000 });
    eq(await p.evaluate(() => localStorage.getItem('user')), null, 'stored user');
    const left = (await cookieNames(O.ctx)).filter(n => ['access_token', 'refresh_token'].includes(n));
    eq(left, [], 'auth cookies left in the browser');
    const r = await call(null, 'POST', '/api/auth/refresh', { refresh_token: rt });
    eq(r.status, 401, 'old refresh token refused');
  }, { u: O });

  await tc(rec, 'UIF-SESS-21', 'Back button after sign-out does not show the contact record (stays on / returns to /login)', async () => {
    const p = O.page;
    await p.goBack().catch(() => {}); await settle(O, 1200);
    ok(/\/login/.test(p.url()), `after Back: ${p.url()}`);
    ok(!(await p.locator('body').innerText()).includes(SECRET), 'contact name visible after Back');
    await p.goBack().catch(() => {}); await settle(O, 1000);
    ok(!(await p.locator('body').innerText()).includes(SECRET), 'contact name visible after a second Back');
  }, { u: O, allow: ALLOW, noBody: true });

  await tc(rec, 'UIF-SESS-22', 'The access token captured before sign-out no longer works afterwards', async () => {
    const at = oldCookies.find(c => c.name === 'access_token')?.value;
    ok(at, 'access cookie captured');
    const r = await call(at, 'GET', '/api/auth/me');
    ok(r.status === 401, `old access token still accepted after sign-out: GET /api/auth/me -> ${r.status} (access JWTs are not revoked on logout; valid until expiry)`);
  });

  await tc(rec, 'UIF-SESS-23', 'Sign out in one tab: the other tab of the same browser ends on /login at its next action and shows no data', async () => {
    const T = await fresh(); const p1 = T.page;
    const p2 = await T.ctx.newPage(); p2.setDefaultTimeout(15000);
    await p2.goto(`${WEB}/app/contacts`); await p2.waitForLoadState('networkidle').catch(() => {});
    await p2.getByText(SECRET).first().waitFor({ timeout: 10000 });
    await go(T, 'app/dashboard', 600);
    await p1.getByRole('button', { name: 'Account' }).click();
    await p1.getByRole('button', { name: /Sign out/ }).click();
    await p1.waitForURL(/\/login/, { timeout: 8000 });
    await p2.waitForTimeout(500);
    if (!/\/login/.test(p2.url())) await (await sidebarLink(p2, 'Deals', 'Sales')).click().catch(() => {});
    await p2.waitForURL(/\/login/, { timeout: 10000 });
    ok(!(await p2.locator('body').innerText()).includes(SECRET), 'contact data still visible in the other tab');
  });

  for (const u of opened) await u.ctx.close().catch(() => {});
}
if (isMain(import.meta)) standalone('session_auth', run);
