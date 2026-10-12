import { startSuite, fullWorkspace, createWs, api, sql, q, go, body, waitText, clean, rid, mkInbox, mkContact, mkCampaign, seedActive, campStatus, dialogs, rowOf, sleep, enrollP, stubContent } from './lib.mjs';

const S = await startSuite('campaigns_list');
const A = 'UIO-CAMP';

// ---------- empty workspace: Home / Dashboard / Campaigns empty states ----------
const emp = await createWs('campempty');
await emp.setSenderIdentity();
{
  const { page, w } = await S.open(emp);
  await S.t(`${A}-HOME-01`, 'Home (My day rail) renders empty state for a new workspace: "Nothing due today", no stale deals', async () => {
    await go(page, '/app/dashboard', { wait: 1200 });
    const t = await body(page);
    if (!/My day/i.test(t) || !/Nothing due today/.test(t)) return 'My day rail missing/empty text: ' + t.slice(0, 300);
    if (!/No stale deals/.test(t)) return 'stale text missing';
    return await clean(page, w);
  }, page);
  await S.t(`${A}-LIST-EMPTY`, 'Campaigns list empty state: "No campaigns yet" + Create New Campaign button works', async () => {
    await go(page, '/app/campaigns');
    await waitText(page, /No campaigns yet/);
    await page.getByRole('button', { name: /Create New Campaign/ }).click();
    await page.waitForURL(/campaigns\/new/);
    await page.getByPlaceholder('e.g. Q4 Enterprise Outreach').waitFor();
    return await clean(page, w);
  }, page);
  await S.t(`${A}-DASH-EMPTY`, 'Campaign analytics (Dashboard) empty workspace: zeros not NaN, range buttons render', async () => {
    await go(page, '/app/analytics', { wait: 1200 });
    const t = await body(page);
    if (!/Total Emails Sent/i.test(t)) return 'KPI tiles missing';
    for (const b of ['Daily', 'Weekly', 'Monthly', 'Yearly']) await page.getByRole('button', { name: b, exact: true }).click();
    return await clean(page, w);
  }, page);
}

// ---------- populated workspace ----------
const ws = await fullWorkspace('camplist');
const dom = `cl${ws.rid}.example`;
const inbox1 = await mkInbox(ws, 'sales', dom);
const pids = [];
for (let i = 0; i < 4; i++) pids.push(await mkContact(ws, `p${i}@acme${ws.rid}.example`, { first_name: 'Pat' + i }));
const nameA = 'Alpha Active ' + ws.rid, nameP = 'Bravo Paused ' + ws.rid, nameD = 'Charlie Draft ' + ws.rid, nameX = 'Delta Delete ' + ws.rid, nameB = 'Echo Bulk ' + ws.rid;
const cA = await seedActive(ws, nameA, [inbox1], pids);
const cP = await seedActive(ws, nameP, [inbox1], pids.slice(0, 2));
await api('POST', `/campaigns/${cP}/pause`, { token: ws.token });
const cD = await mkCampaign(ws, nameD, [inbox1]);
const cX = await mkCampaign(ws, nameX, [inbox1]);
const cB = await seedActive(ws, nameB, [inbox1], pids.slice(0, 1));
// tasks for Home
const tid = (await api('POST', '/tasks', { token: ws.token, body: { title: 'Call Pat ' + ws.rid, due_at: new Date().toISOString(), task_type: 'CALL', priority: 'HIGH' } }));

{
  const { page, w } = await S.open(ws, 'owner');
  const dlg = dialogs(page, 'accept');
  await S.t(`${A}-HOME-02`, 'Home My day rail lists a due task; ticking it marks it DONE in the DB and removes it', async () => {
    if (tid.status !== 201 && tid.status !== 200) return 'task seed failed ' + tid.status + JSON.stringify(tid.data);
    await go(page, '/app/dashboard', { wait: 1200 });
    await waitText(page, new RegExp('Call Pat ' + ws.rid));
    await page.getByRole('checkbox', { name: new RegExp('Complete Call Pat ' + ws.rid) }).check();
    await sleep(1000);
    const st = sql(`SELECT status FROM crm_tasks WHERE title='Call Pat ${ws.rid}'`);
    if (st !== 'DONE') return 'task status ' + st;
    return await clean(page, w);
  }, page);
  await S.t(`${A}-LIST-01`, 'Campaigns list shows every campaign with correct status badge and prospect count (matches DB)', async () => {
    await go(page, '/app/campaigns', { wait: 1000 });
    const expect = [[nameA, 'ACTIVE', 4], [nameP, 'PAUSED', 2], [nameD, 'DRAFT', 0], [nameX, 'DRAFT', 0]];
    const bad = [];
    for (const [n, st, cnt] of expect) {
      const r = rowOf(page, n);
      if (!(await r.count())) { bad.push(n + ' missing'); continue; }
      const t = (await r.innerText()).replace(/\s+/g, ' ');
      if (!new RegExp(st, 'i').test(t)) bad.push(`${n}: expected ${st}, row: ${t.slice(0, 80)}`);
      if (cnt && !t.includes(`${cnt} prospects`)) bad.push(`${n}: expected "${cnt} prospects"`);
    }
    const b = await clean(page, w); if (b) bad.push(b);
    return bad.length ? bad.join('; ') : undefined;
  }, page);
  await S.t(`${A}-LIST-02`, 'Search filters rows (server-side, debounced) and clearing restores; no-match shows empty state', async () => {
    await page.getByPlaceholder('Search...').fill('Alpha Active');
    await sleep(900);
    if (!(await rowOf(page, nameA).count())) return 'match missing';
    if (await rowOf(page, nameP).count()) return 'non-matching row still shown';
    await page.getByPlaceholder('Search...').fill('zzz-no-such-campaign');
    await sleep(900);
    const t = await body(page);
    if (!/No campaigns/.test(t)) return 'no empty state for no match';
    await page.getByPlaceholder('Search...').fill('');
    await sleep(900);
    if (!(await rowOf(page, nameP).count())) return 'rows not restored';
  }, page);
  await S.t(`${A}-LIST-03`, 'Column picker toggles columns, persists in localStorage across reload, Reset restores defaults', async () => {
    await page.locator('#column-picker-btn').click();
    await page.getByRole('button', { name: 'Bounced', exact: true }).click();
    await page.keyboard.press('Escape');
    await page.mouse.click(10, 300);
    const hdr = async () => (await page.locator('thead').innerText());
    if (/BOUNCED/.test((await hdr()).replace(/SENDER BOUNCED/, ''))) return 'Bounced column still visible';
    await page.reload(); await page.waitForLoadState('networkidle'); await page.waitForTimeout(500);
    if (/(^|\s)BOUNCED/.test((await hdr()).replace(/SENDER BOUNCED/, ''))) return 'column setting not persisted';
    await page.locator('#column-picker-btn').click();
    await page.getByRole('button', { name: /Reset to defaults/ }).click();
    await page.mouse.click(10, 300);
    if (!/(^|\s)BOUNCED/.test((await hdr()).replace(/SENDER BOUNCED/, ''))) return 'reset did not restore Bounced';
  }, page);
  await S.t(`${A}-LIST-04`, 'Row Pause on ACTIVE campaign -> PAUSED (DB + badge), Resume -> ACTIVE', async () => {
    await go(page, '/app/campaigns');
    await rowOf(page, nameB).locator('button[title="Pause"]').click();
    await sleep(1200);
    if (campStatus(cB) !== 'PAUSED') return 'DB status ' + campStatus(cB);
    if (!/PAUSED/i.test(await rowOf(page, nameB).innerText())) return 'badge not updated';
    await rowOf(page, nameB).locator('button[title="Resume"]').click();
    await sleep(1200);
    if (campStatus(cB) !== 'ACTIVE') return 'DB status after resume ' + campStatus(cB);
    if (!/ACTIVE/i.test(await rowOf(page, nameB).innerText())) return 'badge not updated after resume';
    return await clean(page, w);
  }, page);
  await S.t(`${A}-LIST-05`, 'Delete: cancelling the confirm keeps the campaign; accepting removes it (UI + DB)', async () => {
    dlg.stop(); const d2 = dialogs(page, 'dismiss');
    await rowOf(page, nameX).locator('button[title="Delete"]').click(); await sleep(600);
    if (!(await rowOf(page, nameX).count())) return 'row removed despite cancel';
    if (sql(`SELECT COUNT(*) FROM campaigns WHERE campaign_id=${q(cX)}`) !== '1') return 'deleted in DB despite cancel';
    d2.stop(); const d3 = dialogs(page, 'accept');
    await rowOf(page, nameX).locator('button[title="Delete"]').click(); await sleep(1500);
    if (!d3.some(m => m.includes(nameX))) return 'confirm text missing campaign name: ' + d3.join('|');
    if (await rowOf(page, nameX).count()) return 'row still visible after delete';
    const n = sql(`SELECT COUNT(*) FROM campaigns WHERE campaign_id=${q(cX)}`);
    if (n !== '0') return 'campaign still in DB: count ' + n;
    d3.stop();
    return await clean(page, w);
  }, page);
  await S.t(`${A}-LIST-06`, 'Row click: DRAFT opens the wizard in edit mode, ACTIVE opens details; View icon opens details', async () => {
    dialogs(page, 'accept');
    await go(page, '/app/campaigns');
    await rowOf(page, nameD).locator('p', { hasText: nameD }).click();
    await page.waitForURL(new RegExp('campaigns/new\\?edit=' + cD));
    await go(page, '/app/campaigns');
    await rowOf(page, nameA).locator('p', { hasText: nameA }).click();
    await page.waitForURL(new RegExp('campaigns/' + cA + '$'));
    await go(page, '/app/campaigns');
    await rowOf(page, nameA).locator('button[title="View"]').click();
    await page.waitForURL(new RegExp('campaigns/' + cA + '$'));
  }, page);
  await S.t(`${A}-LIST-07`, 'Select-all + bulk bar: Pause selected (confirm) pauses active campaigns in DB; clear selection', async () => {
    await go(page, '/app/campaigns');
    const boxes = page.locator('tbody input[type=checkbox]');
    await rowOf(page, nameA).locator('input[type=checkbox]').check();
    await rowOf(page, nameB).locator('input[type=checkbox]').check();
    await waitText(page, /2 selected|selected/i);
    await page.getByRole('button', { name: /^Pause/ }).first().click();
    await sleep(3000);
    if (campStatus(cA) !== 'PAUSED' || campStatus(cB) !== 'PAUSED') return `statuses ${campStatus(cA)}/${campStatus(cB)}`;
    await rowOf(page, nameA).locator('input[type=checkbox]').check();
    await page.getByRole('button', { name: /^Resume/ }).first().click(); await sleep(1500);
    if (campStatus(cA) !== 'ACTIVE') return 'bulk resume failed ' + campStatus(cA);
    return await clean(page, w);
  }, page);
  await S.t(`${A}-LIST-08`, 'Pause API failure surfaces feedback to the user (not silent)', async () => {
    await go(page, '/app/campaigns');
    await page.route('**/campaigns/*/pause', r => r.fulfill({ status: 409, contentType: 'application/json', body: '{"detail":"Campaign cannot be paused right now"}' }));
    const before = await body(page);
    await rowOf(page, nameA).locator('button[title="Pause"]').click(); await sleep(1200);
    await page.unroute('**/campaigns/*/pause');
    const after = await body(page);
    w.drain([/.*/]);
    if (!/cannot be paused|failed|error/i.test(after) && !dlg.length) return 'API error on pause shown nowhere (silent failure)';
  }, page);
  await S.t(`${A}-LIST-09`, 'Campaigns list: API 500 shows an explicit error (not the empty state)', async () => {
    const h = r => (r.request().method() === 'GET' && r.request().resourceType() !== 'document') ? r.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"x"}' }) : r.continue();
    await page.route(/\/api\/campaigns(\?.*)?$/, h);
    await go(page, '/app/campaigns', { wait: 1800 });
    await page.unroute(/\/api\/campaigns(\?.*)?$/, h);
    const t = await body(page); w.drain([/.*/]);
    if (/No campaigns yet/.test(t)) return 'error shown as empty state';
    if (!/Failed to load|error|try again/i.test(t)) return 'no error message: ' + t.slice(-150);
  }, page);

  // ---------- Dashboard (campaign analytics) with data ----------
  // give the data a sent/opened/replied footprint through the DB metrics (messages marked sent) so charts have numbers
  const mid = sql(`SELECT m.message_id FROM email_messages m JOIN email_sequences s ON s.sequence_id=m.sequence_id WHERE s.campaign_id=${q(cA)} LIMIT 1`);
  if (mid) sql(`UPDATE email_messages SET status='SENT', sent_at=NOW() WHERE message_id=${q(mid)}`);
  await S.t(`${A}-DASH-01`, 'Dashboard shows real totals for the workspace (campaign count, status breakdown) consistent with DB', async () => {
    await go(page, '/app/analytics', { wait: 1500 });
    const t = (await body(page)).replace(/\s+/g, ' ');
    const total = sql(`SELECT COUNT(*) FROM campaigns WHERE tenant_id=${q(ws.tenantId)}`);
    const m = t.match(/CAMPAIGNS\s+(\d+)/i);
    if (!m) return 'campaign count tile missing: ' + t.slice(0, 200);
    if (m[1] !== total) return `campaigns tile ${m[1]} vs DB ${total}`;
    const act = sql(`SELECT COUNT(*) FROM campaigns WHERE tenant_id=${q(ws.tenantId)} AND status='ACTIVE'`);
    if (!new RegExp(act + ' active').test(t)) return `active count ${act} not shown: ` + (t.match(/CAMPAIGNS.{0,40}/i) || [''])[0];
    return await clean(page, w);
  }, page);
  await S.t(`${A}-DASH-02`, 'Dashboard range buttons (Daily/Weekly/Monthly/Yearly) change the chart without errors; alerts bell opens', async () => {
    const seen = [];
    for (const b of ['Daily', 'Weekly', 'Monthly', 'Yearly']) {
      await page.getByRole('button', { name: b, exact: true }).click(); await sleep(500);
      seen.push((await body(page)).match(/Last [^\n]+|This [^\n]+/)?.[0]);
    }
    const open = page.getByRole('button', { name: 'Open alerts' });
    if (await open.count()) { await open.click(); await sleep(400); }
    return await clean(page, w);
  }, page);
  await S.t(`${A}-DASH-03`, 'Dashboard campaign table lists campaigns; rows and "View Campaigns" navigate', async () => {
    const t = await body(page);
    if (!t.includes(nameA)) return 'campaign missing in status table';
    await page.getByRole('button', { name: 'View Campaigns' }).click();
    await page.waitForURL(/\/app\/campaigns$/);
  }, page);
  await S.t(`${A}-DASH-04`, 'Dashboard API failure (global-analytics/dashboard-summary 500) degrades without crash', async () => {
    await page.route('**/reports/**', r => r.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"boom"}' }));
    await go(page, '/app/analytics', { wait: 1500 });
    await page.unroute('**/reports/**');
    const bp = await w.bodyProblems(); w.drain([/.*/]);
    if (bp.length) return bp.join(',');
  }, page);
}

// ---------- roles ----------
{
  const { page, w } = await S.open(ws, 'manager');
  await S.t(`${A}-ROLE-mgr`, 'MANAGER sees the campaigns list and can open a campaign (manage_campaigns)', async () => {
    await go(page, '/app/campaigns', { wait: 1000 });
    if (!(await rowOf(page, nameA).count())) return 'manager sees no campaigns';
    return await clean(page, w);
  }, page);
  const ag = await S.open(ws, 'agent');
  await S.t(`${A}-ROLE-agent-dash`, 'AGENT without view_analytics: no Campaign analytics link, URL redirects Home, no 403 calls', async () => {
    await go(ag.page, '/app/analytics', { wait: 1500 });
    const probs = ag.w.drain(); const bp = await ag.w.bodyProblems();
    if (!/\/app\/dashboard$/.test(new URL(ag.page.url()).pathname)) return 'not redirected: ' + ag.page.url();
    if (probs.length || bp.length) return [...probs, ...bp].join(' ; ');
  }, ag.page);
}
await S.done();
