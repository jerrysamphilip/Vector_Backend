// UIF-PA: Platform admin console (/admin, src/pages/PlatformAdmin.jsx + components/layout/AdminLayout.jsx; API /api/admin/*,
// /scheduler/*). A PLATFORM_ADMIN user is created by SQL for the run (tenant_id NULL, as app/models/user.py documents) and
// removed at the end. Covers sign-in landing, tenant roster + search, tenant detail, suspend / reactivate a throwaway tenant
// (login blocked while suspended, live session behaviour), create tenant (+ validation / duplicates), stats tiles, route guards
// both ways, and the scheduler pause / resume switch (console card UIF-PA-14 with confirmation; API UIF-PA-15/16),
// /admin/tenants and /admin/users views (UIF-PA-18). Suspend / Reactivate / scheduler changes ask window.confirm (auto-accepted).
// The scheduler flag (platform_flags.scheduler_paused) is captured first and restored EXACTLY in a finally block.
// Tenants: "padmin" (a normal tenant whose admin must be kept out) and "pa_victim" (suspended / reactivated), both reusable.
//   node platform_admin.mjs
import crypto from 'node:crypto';
import { ui, go, settle, tc, ok, eq, call, sql, sq, standalone, isMain, modalLoc, Tenant, PASSWORD, WEB } from '../ui_crm/crm_common.mjs';
import { loginToken } from '../ui/ui_common.mjs';
import { anon, freshLogin, fillLogin, loginPaced } from './_ui_extra.mjs';

const FLAG = 'scheduler_paused';
const readFlag = () => { const [n, v] = sql(`SELECT CONCAT(COUNT(*),'|',IFNULL(MAX(value),'')) FROM platform_flags WHERE name='${FLAG}'`).split('|'); return n === '1' ? v : null; };
function writeFlag(v) {
  if (v === null) sql(`DELETE FROM platform_flags WHERE name='${FLAG}'`);
  else sql(`INSERT INTO platform_flags (name, value) VALUES ('${FLAG}', ${sq(v)}) ON DUPLICATE KEY UPDATE value=VALUES(value)`);
}

export async function run(rec, browser) {
  const origFlag = readFlag();              // restored in finally, whatever happens
  const t = await (await Tenant.get('padmin')).init();
  const agent = await t.addUser('AGENT', 'Pat');
  const v = await Tenant.get('pa_victim');
  sql(`UPDATE tenants SET status='ACTIVE' WHERE tenant_id=${sq(v.tenantId)}`);   // a crashed earlier run may have left it suspended
  await v.init();
  const vName = sql(`SELECT tenant_name FROM tenants WHERE tenant_id=${sq(v.tenantId)}`);
  const vStatus = () => sql(`SELECT status FROM tenants WHERE tenant_id=${sq(v.tenantId)}`);
  const vUsers = sql(`SELECT COUNT(*) FROM users WHERE tenant_id=${sq(v.tenantId)} AND role<>'PLATFORM_ADMIN'`);

  const paId = crypto.randomUUID();
  const pa = { email: `platform-${t.rid}-${Date.now().toString(36)}@uitest.example`, password: PASSWORD, userId: paId };
  sql(`INSERT INTO users (user_id, tenant_id, first_name, last_name, email, role, status, password_hash, auth_provider, email_verified, created_at) VALUES (${[paId, null, 'Plat', 'Admin', pa.email, 'PLATFORM_ADMIN', 'ACTIVE', t.hash, 'local', 1].map(sq).join(',')}, NOW())`);
  const NEW_TENANT = `Ui pa created ${t.rid} ${Date.now().toString(36)}`;
  const NEW_EMAIL = `newadmin-${t.rid}-${Date.now().toString(36)}@uitest.example`;
  const opened = [];

  try {
    const P = await freshLogin(browser, pa); opened.push(P); const p = P.page;
    const paToken = await loginToken(pa.email, pa.password);   // for the scheduler API (Bearer: no CSRF needed)
    const row = (name) => p.locator('tbody tr').filter({ hasText: name });

    await tc(rec, 'UIF-PA-01', 'Platform admin signs in through the login form and lands on /admin (Platform Administration, no tenant navigation)', async () => {
      ok(/\/admin(\/|$|\?)/.test(p.url()), `landed on ${p.url()}`);
      await p.getByRole('heading', { name: 'Platform Administration' }).waitFor();
      ok(await p.getByText('Platform Admin', { exact: true }).first().isVisible(), 'Platform Admin label in the sidebar');
      eq(await p.getByRole('link', { name: 'Contacts', exact: true }).count(), 0, 'no tenant nav');
      await p.getByText('Tenant Roster').waitFor();
    }, { u: P });

    await tc(rec, 'UIF-PA-02', 'Tenant roster: search narrows to the tenant; status ACTIVE, user / campaign counts match the DB', async () => {
      await p.getByPlaceholder('Search tenants...').fill(vName); await settle(P, 900);
      eq(await p.locator('tbody tr').count(), 1, 'one row');
      const r = row(vName);
      const cells = await r.locator('td').allInnerTexts();
      ok(cells[0].includes(vName) && cells[0].includes(v.tenantId.slice(-8)), `tenant cell: ${cells[0]}`);   // short id = last 8 characters (UUIDv7 ids share their first 8, app/core/ids.py short_id)
      eq(cells[1].trim(), 'ACTIVE', 'status badge');
      eq(cells[2].trim(), vUsers, 'user count');
      eq(cells[3].trim(), sql(`SELECT COUNT(*) FROM campaigns WHERE tenant_id=${sq(v.tenantId)}`), 'campaign count');
      ok(await r.getByRole('button', { name: /Suspend/ }).isVisible(), 'Suspend action');
    }, { u: P });

    await tc(rec, 'UIF-PA-03', 'Search with no match shows "No tenants found for the current search."', async () => {
      await p.getByPlaceholder('Search tenants...').fill(`zz-no-tenant-${t.rid}`); await settle(P, 900);
      await p.getByText('No tenants found for the current search.').waitFor();
      await p.getByPlaceholder('Search tenants...').fill(vName); await settle(P, 900);
    }, { u: P });

    await tc(rec, 'UIF-PA-04', 'Tenant detail: View opens the modal with the tenant name, user count and the team (owner email, SUPER ADMIN)', async () => {
      await row(vName).getByRole('button', { name: /View/ }).click(); await settle(P, 900);
      const m = modalLoc(p);
      await m.getByRole('heading', { name: vName }).waitFor();
      await m.getByText('Tenant Team').waitFor();
      const teamRow = m.locator('tbody tr').filter({ hasText: v.owner.email });
      await teamRow.waitFor();
      ok((await teamRow.innerText()).includes('SUPER ADMIN'), 'role badge');
      ok((await m.innerText()).includes(`${vUsers} team members`), 'team count');
      await p.mouse.click(5, 5); await settle(P, 500);   // backdrop closes it
      eq(await p.getByText('Tenant Team').count(), 0, 'modal closed');
    }, { u: P });

    // the victim's owner is signed in BEFORE the suspension (live-session case)
    const V = await freshLogin(browser, v.owner); opened.push(V);

    await tc(rec, 'UIF-PA-05', 'Suspend from the roster: badge SUSPENDED, Activate offered, DB status SUSPENDED', async () => {
      await row(vName).getByRole('button', { name: /Suspend/ }).click(); await settle(P, 1200);
      await row(vName).getByText('SUSPENDED', { exact: true }).waitFor({ timeout: 8000 });
      ok(await row(vName).getByRole('button', { name: /Activate/ }).isVisible(), 'Activate button');
      eq(vStatus(), 'SUSPENDED', 'DB');
    }, { u: P });

    await tc(rec, 'UIF-PA-06', 'Suspended tenant: API login -> 403 "Tenant suspended"; the login form shows the error and stays on /login', async () => {
      await loginPaced();
      const r = await call(null, 'POST', '/api/auth/login', { email: v.owner.email, password: v.owner.password });
      eq(r.status, 403, `API login status (${JSON.stringify(r.data).slice(0, 80)})`);
      ok(/suspend/i.test(JSON.stringify(r.data)), 'detail mentions suspended');
      const X = await anon(browser); opened.push(X);
      await X.page.goto(`${WEB}/login`);
      await loginPaced();
      await fillLogin(X.page, v.owner.email, v.owner.password);
      await X.page.getByText(/suspend/i).first().waitFor({ timeout: 8000 });
      ok(/\/login/.test(X.page.url()), `still on login: ${X.page.url()}`);
      eq(await X.page.evaluate(() => localStorage.getItem('user')), null, 'no user stored');
    });

    await tc(rec, 'UIF-PA-07', 'A user signed in before the suspension is signed out or told the workspace is suspended on the next screen (no broken page)', async () => {
      await go(V, 'contacts', { wait: 2500 });
      const onLogin = /\/login/.test(V.page.url());
      const told = await V.page.getByText(/suspended/i).count();
      ok(onLogin || told > 0, `still inside the app at ${V.page.url()} with no suspension notice (API answers 403 "Tenant suspended", the UI has no handling for it)`);
    }, { u: V, allow: [/http 40[13]/, /status of 40[13]/], noBody: true });

    await tc(rec, 'UIF-PA-08', 'Reactivate: Activate -> ACTIVE badge, DB ACTIVE, the tenant owner can sign in again', async () => {
      await row(vName).getByRole('button', { name: /Activate/ }).click(); await settle(P, 1200);
      await row(vName).getByText('ACTIVE', { exact: true }).waitFor({ timeout: 8000 });
      eq(vStatus(), 'ACTIVE', 'DB');
      await loginPaced();
      const r = await call(null, 'POST', '/api/auth/login', { email: v.owner.email, password: v.owner.password });
      eq(r.status, 200, 'login works again');
    }, { u: P });

    await tc(rec, 'UIF-PA-09', 'Add Tenant: required fields block an empty submit (no API call); valid form creates tenant + INVITED SUPER_ADMIN + magic link', async () => {
      await p.getByPlaceholder('Search tenants...').fill(''); await settle(P, 600);
      let posts = 0;
      const onReq = r => { if (r.method() === 'POST' && /\/api\/admin\/tenants$/.test(new URL(r.url()).pathname)) posts++; };
      p.on('request', onReq);
      try {
        await p.getByRole('button', { name: /Add Tenant/ }).click();
        const m = modalLoc(p);
        await m.getByRole('heading', { name: 'Create Tenant' }).waitFor();
        await m.getByRole('button', { name: 'Create Tenant' }).click(); await settle(P, 400);
        eq(posts, 0, 'empty form not submitted');
        eq(await m.locator('form').evaluate(f => f.checkValidity()), false, 'form invalid');
        await m.getByPlaceholder('e.g. Acme Corp').fill(NEW_TENANT);
        await m.getByPlaceholder('John').fill('Nina');
        await m.getByPlaceholder('Doe').fill('Newadmin');
        await m.getByPlaceholder('admin@acme.com').fill(NEW_EMAIL);
        await m.getByRole('button', { name: 'Create Tenant' }).click(); await settle(P, 1500);
        eq(posts, 1, 'one create request');
        // invite email sent -> modal closes; email failed -> activation link panel with Done
        if (await p.getByText('Tenant created successfully').count()) {
          ok(/magic-login\?token=|token=/.test(await m.innerText()), 'activation link shown');
          await m.getByRole('button', { name: 'Done' }).click(); await settle(P, 600);
        }
        eq(await p.getByRole('heading', { name: 'Create Tenant' }).count(), 0, 'modal closed');
      } finally { p.off('request', onReq); }
      await p.getByPlaceholder('Search tenants...').fill(NEW_TENANT); await settle(P, 900);
      await row(NEW_TENANT).waitFor({ timeout: 8000 });
      eq((await row(NEW_TENANT).locator('td').nth(1).innerText()).trim(), 'ACTIVE', 'new tenant ACTIVE');
      eq(sql(`SELECT CONCAT(u.role,'|',u.status) FROM users u JOIN tenants tt ON tt.tenant_id=u.tenant_id WHERE tt.tenant_name=${sq(NEW_TENANT)} AND u.email=${sq(NEW_EMAIL)}`), 'SUPER_ADMIN|INVITED', 'admin user');
      eq(sql(`SELECT COUNT(*) FROM magic_login_tokens m JOIN users u ON u.user_id=m.user_id WHERE u.email=${sq(NEW_EMAIL)}`), '1', 'magic login token');
    }, { u: P });

    await tc(rec, 'UIF-PA-10', 'Add Tenant duplicates: same tenant name -> "A tenant with this name already exists"; same admin email -> "A user with this email already exists"', async () => {
      await p.getByRole('button', { name: /Add Tenant/ }).click();
      const m = modalLoc(p);
      await m.getByPlaceholder('e.g. Acme Corp').fill(NEW_TENANT.toUpperCase());
      await m.getByPlaceholder('John').fill('Dup');
      await m.getByPlaceholder('Doe').fill('Name');
      await m.getByPlaceholder('admin@acme.com').fill(`dup-${NEW_EMAIL}`);
      await m.getByRole('button', { name: 'Create Tenant' }).click(); await settle(P, 900);
      await m.getByText('A tenant with this name already exists').waitFor({ timeout: 8000 });
      await m.getByPlaceholder('e.g. Acme Corp').fill(`${NEW_TENANT} two`);
      await m.getByPlaceholder('admin@acme.com').fill(NEW_EMAIL);
      await m.getByRole('button', { name: 'Create Tenant' }).click(); await settle(P, 900);
      await m.getByText('A user with this email already exists').waitFor({ timeout: 8000 });
      eq(sql(`SELECT COUNT(*) FROM tenants WHERE tenant_name LIKE ${sq(NEW_TENANT + '%')}`), '1', 'no extra tenant');
      await m.getByRole('button', { name: 'Cancel', exact: true }).click();
    }, { u: P, allow: [/http 409/, /status of 409/] });

    await tc(rec, 'UIF-PA-11', 'Stats tiles: Total Tenants / Suspended match /api/admin/stats (read around the page load)', async () => {
      const before = await call(paToken, 'GET', '/api/admin/stats');
      await P.page.goto(`${WEB}/admin`); await settle(P, 1200);
      const after = await call(paToken, 'GET', '/api/admin/stats');
      const tile = async (label) => Number((await p.getByText(label, { exact: true }).locator('xpath=following-sibling::p[1]').innerText()).trim());
      const total = await tile('Total Tenants'); const susp = await tile('Suspended');
      ok(total >= Math.min(before.data.total_tenants, after.data.total_tenants) && total <= Math.max(before.data.total_tenants, after.data.total_tenants), `Total Tenants ${total} vs API ${before.data.total_tenants}..${after.data.total_tenants}`);
      ok(susp >= Math.min(before.data.suspended_tenants, after.data.suspended_tenants) && susp <= Math.max(before.data.suspended_tenants, after.data.suspended_tenants), `Suspended ${susp} vs API`);
    }, { u: P });

    const A = await ui(browser, t.owner); opened.push(A);
    await tc(rec, 'UIF-PA-12', 'Tenant SUPER_ADMIN cannot reach the console: /admin and /admin/tenants redirect to /app/dashboard; /api/admin/* -> 403', async () => {
      for (const r of ['admin', 'admin/tenants', 'admin/users']) {
        await A.page.goto(`${WEB}/${r}`); await settle(A, 800);
        ok(/\/app\/dashboard/.test(A.page.url()), `/${r} -> ${A.page.url()}`);
        eq(await A.page.getByRole('heading', { name: 'Platform Administration' }).count(), 0, 'console not rendered');
      }
      eq((await call(t.token, 'GET', '/api/admin/tenants')).status, 403, 'list tenants');
      eq((await call(t.token, 'GET', '/api/admin/stats')).status, 403, 'stats');
      eq((await call(t.token, 'PUT', `/api/admin/tenants/${v.tenantId}/status`, { status: 'SUSPENDED' })).status, 403, 'suspend another tenant');
      eq(vStatus(), 'ACTIVE', 'victim untouched');
      const ag = await t.tokenOf(agent);
      eq((await call(ag, 'GET', '/api/admin/users')).status, 403, 'agent: users');
    }, { u: A });

    await tc(rec, 'UIF-PA-13', 'Platform admin is kept out of tenant screens: /app/contacts redirects to /admin; tenant API -> 403', async () => {
      await P.page.goto(`${WEB}/app/contacts`); await settle(P, 900);
      ok(/\/admin(\/|$|\?)/.test(P.page.url()), `redirected to ${P.page.url()}`);
      const r = await call(paToken, 'GET', '/contacts');
      eq(r.status, 403, 'GET /contacts');
      ok(/Platform admins cannot access tenant data/.test(JSON.stringify(r.data)), 'detail');
    }, { u: P });

    await tc(rec, 'UIF-PA-14', 'Scheduler card on /admin: current state shown; Pause / Resume ask for confirmation and flip the flag; original state restored', async () => {
      try {
        await P.page.goto(`${WEB}/admin`); await settle(P, 1000);
        const status = p.getByTestId('scheduler-status');
        const toggle = p.getByTestId('scheduler-toggle');
        await toggle.waitFor({ timeout: 8000 });
        ok(new RegExp(origFlag === null ? 'running' : 'paused', 'i').test(await status.innerText()), `state shown: ${await status.innerText()}`);
        if (origFlag !== null) {              // paused by someone: resume first
          P.lastDialog = '';
          await toggle.click(); await settle(P, 800);
          ok(/Resume the email scheduler/.test(P.lastDialog || ''), `resume confirmation: ${P.lastDialog}`);
          await status.getByText(/^running$/i).waitFor({ timeout: 8000 });
          eq(readFlag(), null, 'flag cleared');
        }
        P.lastDialog = '';
        await toggle.click(); await settle(P, 800);
        ok(/Pause the email scheduler/.test(P.lastDialog || ''), `pause confirmation: ${P.lastDialog}`);
        await status.getByText(/^paused$/i).waitFor({ timeout: 8000 });
        eq(readFlag(), pa.email, 'flag holds the admin email');
        ok((await status.innerText()).includes(pa.email), 'paused by shown');
        ok(/Resume scheduler/.test(await toggle.innerText()), 'button offers Resume');
        P.lastDialog = '';
        await toggle.click(); await settle(P, 800);
        ok(/Resume the email scheduler/.test(P.lastDialog || ''), `resume confirmation: ${P.lastDialog}`);
        await status.getByText(/^running$/i).waitFor({ timeout: 8000 });
        eq(readFlag(), null, 'flag cleared');
      } finally { writeFlag(origFlag); }
    }, { u: P });

    await tc(rec, 'UIF-PA-18', '/admin/tenants shows the tenant list (no stats tiles); /admin/users lists tenant users with search', async () => {
      await P.page.goto(`${WEB}/admin/tenants`); await settle(P, 1000);
      await p.getByRole('heading', { level: 1, name: 'Tenants' }).waitFor();
      eq(await p.getByText('Total Tenants', { exact: true }).count(), 0, 'no stats tiles on the tenant list');
      await p.getByPlaceholder('Search tenants...').fill(vName); await settle(P, 900);
      await row(vName).waitFor({ timeout: 8000 });
      await P.page.goto(`${WEB}/admin/users`); await settle(P, 1000);
      await p.getByRole('heading', { level: 1, name: 'Users' }).waitFor();
      await p.getByPlaceholder('Search users...').fill(v.owner.email); await settle(P, 900);
      const r = p.locator('tbody tr').filter({ hasText: v.owner.email });
      await r.waitFor({ timeout: 8000 });
      ok((await r.innerText()).includes(vName), 'tenant name in the row');
      ok(/SUPER ADMIN/.test(await r.innerText()), 'role badge');
      eq(await p.getByRole('button', { name: /Suspend|Delete|Edit/ }).count(), 0, 'read-only');
    }, { u: P });

    await tc(rec, 'UIF-PA-15', 'Scheduler switch (API, platform admin): stop -> paused + flag holds the admin email; start -> running; original state restored', async () => {
      const st = async () => (await call(paToken, 'GET', '/scheduler/status')).data.running;
      eq(await st(), origFlag === null, 'status matches the flag before the test');
      if (origFlag !== null) {            // currently paused by someone: resume, then pause again
        eq((await call(paToken, 'POST', '/scheduler/start')).data.status, 'started', 'start');
        eq(await st(), true, 'running');
        eq(readFlag(), null, 'flag cleared');
      }
      eq((await call(paToken, 'POST', '/scheduler/stop')).data.status, 'stopped', 'stop');
      eq(await st(), false, 'paused');
      eq(readFlag(), pa.email, 'flag value is the admin email');
      eq((await call(paToken, 'POST', '/scheduler/stop')).data.status, 'not_running', 'stop again is idempotent');
      if (origFlag === null) {
        eq((await call(paToken, 'POST', '/scheduler/start')).data.status, 'started', 'start');
        eq(await st(), true, 'running again');
        eq((await call(paToken, 'POST', '/scheduler/start')).data.status, 'already_running', 'start again is idempotent');
      }
      writeFlag(origFlag);
      eq(readFlag(), origFlag, 'restored');
    });

    await tc(rec, 'UIF-PA-16', 'Tenant admin / agent cannot pause or run the scheduler (403), flag unchanged; status is readable', async () => {
      const before = readFlag();
      eq((await call(t.token, 'POST', '/scheduler/stop')).status, 403, 'admin stop');
      eq((await call(t.token, 'POST', '/scheduler/start')).status, 403, 'admin start');
      eq((await call(t.token, 'POST', '/scheduler/process')).status, 403, 'admin process');
      eq((await call(await t.tokenOf(agent), 'POST', '/scheduler/stop')).status, 403, 'agent stop');
      eq(readFlag(), before, 'flag unchanged');
      eq((await call(t.token, 'GET', '/scheduler/status')).status, 200, 'status readable');
    });

    await tc(rec, 'UIF-PA-17', 'Platform admin signs out from the profile popup: /login, and /admin then redirects to /login', async () => {
      await P.page.goto(`${WEB}/admin`); await settle(P, 800);
      await p.getByRole('button', { name: /Plat Admin/ }).click();
      await p.getByRole('button', { name: /Sign out/ }).click();
      await p.waitForURL(/\/login/, { timeout: 8000 });
      await p.goto(`${WEB}/admin`); await settle(P, 800);
      ok(/\/login/.test(p.url()), `after sign-out /admin -> ${p.url()}`);
    }, { u: P });
  } finally {
    writeFlag(origFlag);
    if (readFlag() !== origFlag) console.error('!! scheduler flag NOT restored');
    for (const u of opened) await u.ctx.close().catch(() => {});
    try { sql(`UPDATE tenants SET status='ACTIVE' WHERE tenant_id=${sq(v.tenantId)}`); } catch (e) { console.error('victim reactivate failed', e.message); }
    try {   // the tenant created through the console
      const tid = sql(`SELECT tenant_id FROM tenants WHERE tenant_name=${sq(NEW_TENANT)}`);
      if (tid) sql(`SET FOREIGN_KEY_CHECKS=0; DELETE m FROM magic_login_tokens m JOIN users u ON u.user_id=m.user_id WHERE u.tenant_id=${sq(tid)}; DELETE FROM refresh_tokens WHERE user_id IN (SELECT user_id FROM users WHERE tenant_id=${sq(tid)}); DELETE FROM users WHERE tenant_id=${sq(tid)}; DELETE FROM tenants WHERE tenant_id=${sq(tid)}; SET FOREIGN_KEY_CHECKS=1;`);
    } catch (e) { console.error('created-tenant cleanup failed', e.message); }
    try { sql(`DELETE FROM refresh_tokens WHERE user_id=${sq(paId)}; DELETE FROM users WHERE user_id=${sq(paId)}`); }
    catch { try { sql(`UPDATE users SET status='INACTIVE' WHERE user_id=${sq(paId)}`); } catch (e) { console.error('platform admin cleanup failed', e.message); } }
  }
}
if (isMain(import.meta)) standalone('platform_admin', run);
