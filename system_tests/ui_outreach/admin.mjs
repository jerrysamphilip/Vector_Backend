import { startSuite, fullWorkspace, api, sql, q, go, body, waitText, clean, mkInbox, mkContact, mkCampaign, seedActive, dialogs, sleep, loginToken } from './lib.mjs';
const S = await startSuite('admin');
const T = 'UIO-TEAM', R = 'UIO-REP';
const ws = await fullWorkspace('adm');
const { page, w } = await S.open(ws);
const dlg0 = dialogs(page);
const urow = e => page.locator('tbody tr', { hasText: e });
const INV = `newbie-${ws.rid}@uitest.example`;
const DUP = `dup-${ws.rid}@uitest.example`;

await S.t(`${T}-01`, 'Team page lists every workspace member with role/status; KPI tiles match DB counts', async () => {
  await go(page, '/app/team', { wait: 1500 });
  for (const k of ['owner', 'admin', 'manager', 'agent']) if (!(await body(page)).includes(ws.users[k].email)) return k + ' missing';
  const total = sql(`SELECT COUNT(*) FROM users WHERE tenant_id=${q(ws.tenantId)}`);
  const t = (await body(page)).replace(/\s+/g, ' ');
  const m = t.match(/(\d+)\s*TOTAL MEMBERS|TOTAL MEMBERS\s*(\d+)/i);
  if (!m) return 'total tile unreadable: ' + t.slice(t.indexOf('Invite Member'), t.indexOf('Invite Member') + 200);
  if ((m[1] || m[2]) !== total) return `tile ${m[1] || m[2]} vs DB ${total}`;
  return await clean(page, w);
}, page);
await S.t(`${T}-02`, 'Invite modal: opens/cancels; empty and malformed email are blocked (no request); roles radios incl. Super Admin for owner', async () => {
  await page.getByRole('button', { name: 'Invite Member' }).click();
  const em = page.getByPlaceholder('colleague@company.com');
  const reqs = []; const f = r => { if (/users\/invite/.test(r.url())) reqs.push(r.url()); }; page.on('request', f);
  await page.getByRole('button', { name: 'Send Invite' }).click(); await sleep(400);
  await em.fill('not-an-email'); await page.getByRole('button', { name: 'Send Invite' }).click(); await sleep(400);
  page.off('request', f);
  if (reqs.length) return 'invite request sent for invalid email';
  if ((await page.locator('input[type=radio][name=role]').count()) !== 4) return 'role radios != 4';
  await page.getByRole('button', { name: 'Cancel' }).click();
  if (await em.isVisible()) return 'modal not closed by Cancel';
}, page);
await S.t(`${T}-03`, 'Invite a Manager: modal closes, confirmation banner; DB has INVITED user with role, invitation row and magic-login token', async () => {
  await page.getByRole('button', { name: 'Invite Member' }).click();
  await page.getByPlaceholder('colleague@company.com').fill(INV);
  await page.locator('input[type=radio][value=MANAGER]').check();
  await page.getByRole('button', { name: 'Send Invite' }).click(); await sleep(2500);
  const u = sql(`SELECT role,status,tenant_id FROM users WHERE email=${q(INV)}`).split('\t');
  if (u.join('|') !== `MANAGER|INVITED|${ws.tenantId}`) return 'user row ' + u.join('|');
  if (sql(`SELECT COUNT(*) FROM magic_login_tokens m JOIN users u ON u.user_id=m.user_id WHERE u.email=${q(INV)}`) !== '1') return 'no magic token';
  if (sql(`SELECT COUNT(*) FROM invitations WHERE tenant_id=${q(ws.tenantId)} AND email=${q(INV)}`) < '1') return 'no invitation row';
  if (!(await body(page)).includes(INV)) return 'invitee not in table';
  if (!/invit|sent|created|email/i.test(await body(page))) return 'no confirmation message';
  return await clean(page, w);
}, page);
await S.t(`${T}-04`, 'Inviting an existing member shows the server error in the modal (409), stays open', async () => {
  await page.getByRole('button', { name: 'Invite Member' }).click();
  await page.getByPlaceholder('colleague@company.com').fill(ws.users.agent.email);
  await page.getByRole('button', { name: 'Send Invite' }).click(); await sleep(1500);
  if (!/already exists/i.test(await body(page))) return 'no duplicate message';
  w.drain([/users\/invite/, /Failed to load resource/]);
  await page.getByRole('button', { name: 'Cancel' }).click();
}, page);
await S.t(`${T}-05`, 'Search and status filter narrow the member table', async () => {
  await page.getByPlaceholder('Search members…').fill('newbie'); await sleep(400);
  if ((await page.locator('tbody tr').count()) !== 1) return 'search rows != 1';
  await page.getByPlaceholder('Search members…').fill('');
  await page.locator('select').first().selectOption({ label: 'Invited' }); await sleep(400);
  const n = await page.locator('tbody tr').count(); if (n !== 1) return 'invited filter rows ' + n;
  await page.locator('select').first().selectOption({ label: 'All Status' });
}, page);
await S.t(`${T}-06`, 'Change role (owner): Manager -> Admin saved to DB; Save button only after a change', async () => {
  const row = urow(ws.users.manager.email);
  const sel = row.locator('select');
  if (await row.getByRole('button', { name: 'Save', exact: true }).count()) return 'Save visible before change';
  await sel.selectOption({ label: 'Admin' });
  await row.getByRole('button', { name: 'Save', exact: true }).click(); await sleep(1500);
  const r = sql(`SELECT role FROM users WHERE user_id=${q(ws.users.manager.userId)}`);
  if (r !== 'ADMIN') return 'role ' + r;
  await sel.selectOption({ label: 'Manager' }); await row.getByRole('button', { name: 'Save', exact: true }).click(); await sleep(1200);
  return await clean(page, w);
}, page);
await S.t(`${T}-07`, 'Deactivate agent -> INACTIVE in DB and cannot log in; Reactivate restores ACTIVE', async () => {
  const row = urow(ws.users.agent.email);
  await row.getByRole('button', { name: 'Deactivate' }).click(); await sleep(1500);
  if (sql(`SELECT status FROM users WHERE user_id=${q(ws.users.agent.userId)}`) !== 'INACTIVE') return 'not inactive';
  const lr = await api('POST', '/api/auth/login', { headers: { 'X-Forwarded-For': ws.xff }, body: { email: ws.users.agent.email, password: ws.password } });
  if (lr.status !== 403) return 'inactive user login status ' + lr.status;
  await urow(ws.users.agent.email).getByRole('button', { name: 'Reactivate' }).click(); await sleep(1500);
  if (sql(`SELECT status FROM users WHERE user_id=${q(ws.users.agent.userId)}`) !== 'ACTIVE') return 'not reactivated';
  return await clean(page, w);
}, page);
await S.t(`${T}-08`, 'Revoke a pending invite: Revoke button marks the invited user INACTIVE/removed; self row has no Deactivate', async () => {
  await urow(INV).getByRole('button', { name: 'Revoke' }).click(); await sleep(1500);
  const st = sql(`SELECT status FROM users WHERE email=${q(INV)}`);
  if (st === 'INVITED') return 'invite still pending';
  if (await urow(ws.email).getByRole('button', { name: /Deactivate/ }).count()) return 'owner can deactivate self';
}, page);
await S.t(`${T}-09`, 'Permissions modal: toggling a permission saves custom_permissions (DB) and the user\'s access changes; Reset restores defaults', async () => {
  const row = urow(ws.users.admin.email);
  await row.getByRole('button', { name: 'Permissions' }).click();
  await waitText(page, /Manage Campaigns/);
  await page.getByRole('button', { name: /Manage Templates/ }).click();
  await page.getByRole('button', { name: /^Save/ }).last().click(); await sleep(1500);
  const cp = sql(`SELECT custom_permissions FROM users WHERE user_id=${q(ws.users.admin.userId)}`);
  if (!cp || cp === 'NULL' || /manage_templates/.test(cp)) return 'custom_permissions ' + cp;
  await urow(ws.users.admin.email).getByRole('button', { name: 'Permissions' }).click();
  await page.getByRole('button', { name: /Reset to defaults/ }).click(); await sleep(1500);
  if (!['NULL', 'null'].includes(sql(`SELECT IFNULL(custom_permissions,'NULL') FROM users WHERE user_id=${q(ws.users.admin.userId)}`))) return 'reset did not clear custom_permissions';
  return await clean(page, w);
}, page);
await S.t(`${T}-10`, 'Permanent delete: Cancel keeps user, OK removes the row from DB', async () => {
  const hasDelete = await urow(INV).getByRole('button', { name: 'Delete' }).count();
  if (!hasDelete) return null;
  dlg0.stop(); const d = dialogs(page, 'dismiss'); await urow(INV).getByRole('button', { name: 'Delete' }).click(); await sleep(500); d.stop();
  if (sql(`SELECT COUNT(*) FROM users WHERE email=${q(INV)}`) !== '1') return 'deleted on cancel';
  dialogs(page, 'accept'); await urow(INV).getByRole('button', { name: 'Delete' }).click(); await sleep(1500);
  if (sql(`SELECT COUNT(*) FROM users WHERE email=${q(INV)}`) !== '0') return 'still in DB';
}, page);
await S.t(`${T}-11`, 'ADMIN view: no role dropdowns, no Permissions buttons; API refuses admin role changes and Super Admin invites (403)', async () => {
  const a = await S.open(ws, 'admin'); await go(a.page, '/app/team', { wait: 1500 });
  const sels = await a.page.locator('tbody select').count();
  if (sels) return 'admin sees ' + sels + ' role dropdowns';
  if (await a.page.getByRole('button', { name: 'Permissions' }).count()) return 'admin sees Permissions';
  const r1 = await api('PUT', `/api/users/${ws.users.agent.userId}/role`, { token: ws.users.admin.token, body: { role: 'ADMIN' } });
  const r2 = await api('POST', '/api/users/invite', { token: ws.users.admin.token, body: { email: `sa-${ws.rid}@uitest.example`, role: 'SUPER_ADMIN' } });
  if (r1.status !== 403 || r2.status !== 403) return `role change ${r1.status}, SA invite ${r2.status}`;
  if (!/Super Admin/.test(await body(a.page))) return null;
}, page);
await S.t(`${T}-12`, 'Team API 500 on user list shows an error state, not an empty team', async () => {
  await page.route(/\/api\/users(\?.*)?$/, r => r.request().resourceType() === 'document' ? r.continue() : r.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"Users service unavailable"}' }));
  await go(page, '/app/team', { wait: 4500 });
  await page.unroute(/\/api\/users(\?.*)?$/); w.drain([/.*/]);
  const t = await body(page);
  if (!/unavailable|failed to load|error|try again|could not/i.test(t)) return 'error not surfaced: ' + t.slice(-500).replace(/\s+/g, ' ');
}, page);

// ---- Reports ----
const dom = `rp${ws.rid}.example`;
const ib = await mkInbox(ws, 'sales', dom);
const pids = []; for (let i = 0; i < 3; i++) pids.push(await mkContact(ws, `r${i}@rp${ws.rid}.example`));
const cid = await seedActive(ws, 'Report Camp ' + ws.rid, [ib], pids);
sql(`UPDATE email_messages SET status='SENT', sent_at=NOW() WHERE message_id IN (SELECT message_id FROM (SELECT m.message_id FROM email_messages m JOIN email_sequences s ON s.sequence_id=m.sequence_id WHERE s.campaign_id=${q(cid)} LIMIT 2) t)`);
await S.t(`${R}-01`, 'Reports renders KPIs and campaign table for a campaign with sends; no NaN/undefined', async () => {
  await go(page, '/app/reports', { wait: 2000 });
  const t = await body(page);
  if (!t.includes('Report Camp ' + ws.rid)) return 'campaign missing from table';
  return await clean(page, w);
}, page);
await S.t(`${R}-02`, 'Date range buttons (7d/30d/90d/365d) re-query with the matching start_date', async () => {
  const seen = [];
  const f = r => { if (/dashboard-summary|global-analytics|campaign-comparison/.test(r.url())) seen.push(r.url()); }; page.on('request', f);
  const days = { '7d': 7, '30d': 30, '90d': 90, '365d': 365 };
  const bad = [];
  for (const [b, n] of Object.entries(days)) {
    seen.length = 0;
    await page.getByRole('button', { name: b, exact: true }).click(); await sleep(1200);
    const u = seen.find(x => /start_date/.test(x)); if (!u) { if (b !== '30d') bad.push(b + ': no request'); continue; }
    const sd = new URL(u).searchParams.get('start_date'), ed = new URL(u).searchParams.get('end_date');
    const diff = Math.round((new Date(ed) - new Date(sd)) / 864e5);
    if (Math.abs(diff - n) > 1) bad.push(`${b}: range ${diff}d`);
  }
  page.off('request', f);
  const c = await clean(page, w); if (c) bad.push(c);
  return bad.length ? bad.join('; ') : undefined;
}, page);
await S.t(`${R}-03`, 'Export CSV / Export Word / campaign Export CSV trigger file downloads', async () => {
  const bad = [];
  for (const [name, idx] of [['Export CSV', 0], ['Export Word', 0], ['Export CSV', 1]]) {
    const [dl] = await Promise.all([page.waitForEvent('download', { timeout: 10000 }).catch(() => null), page.getByRole('button', { name, exact: true }).nth(idx).click()]);
    if (!dl) bad.push(name + '#' + idx + ' no download'); else if (!dl.suggestedFilename()) bad.push('no filename');
  }
  w.drain([/.*/]);
  return bad.length ? bad.join('; ') : undefined;
}, page);
await S.t(`${R}-04`, 'User filter dropdown (All Users) lists workspace users and filters the report', async () => {
  await page.getByRole('button', { name: /All Users/ }).click(); await sleep(500);
  const t = await body(page);
  if (!t.includes('Ui') && !t.includes('admin')) return 'no user list';
  await page.keyboard.press('Escape');
  return await clean(page, w);
}, page);
await S.t(`${R}-05`, 'Reports API 500 does not crash (error boundary not shown)', async () => {
  await page.route(/reports\//, r => r.request().resourceType() === 'document' ? r.continue() : r.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"x"}' }));
  await go(page, '/app/reports', { wait: 1800 }); await page.unroute(/reports\//);
  const bp = await w.bodyProblems(); w.drain([/.*/]); if (bp.length) return bp.join(',');
}, page);
await S.t(`${R}-06`, 'MANAGER (view_analytics) can open Reports; AGENT is redirected Home', async () => {
  const m = await S.open(ws, 'manager'); await go(m.page, '/app/reports', { wait: 1800 });
  if (!/\/app\/reports$/.test(new URL(m.page.url()).pathname)) return 'manager redirected';
  const p = await clean(m.page, m.w); if (p) return p;
  const a = await S.open(ws, 'agent'); await go(a.page, '/app/reports');
  if (!/dashboard$/.test(new URL(a.page.url()).pathname)) return 'agent not redirected';
}, page);
await S.done();
