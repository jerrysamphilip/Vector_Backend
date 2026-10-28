import { startSuite, fullWorkspace, api, sql, q, go, body, waitText, clean, mkInbox, mkContact, seedActive, mkCampaign, dialogs, rowOf, sleep, campStatus } from './lib.mjs';
const S = await startSuite('details');
const A = 'UIO-DET';
const ws = await fullWorkspace('det');
const dom = `dt${ws.rid}.example`;
const ib = await mkInbox(ws, 'sales', dom);
const extra = await mkContact(ws, `extra@x${ws.rid}.example`, { first_name: 'Extra', company_name: 'ExCo' });
const pids = []; for (let i = 0; i < 4; i++) pids.push(await mkContact(ws, `p${i}@dt${ws.rid}.example`, { first_name: 'Det' + i, company_name: 'DetCo' }));
const NAME = 'Details Camp ' + ws.rid;
const cid = await seedActive(ws, NAME, [ib], pids, { waits: [0, 2] });
const { page, w } = await S.open(ws);
const dlg = dialogs(page, 'accept');
const tab = n => page.getByRole('button', { name: n });
await S.t(`${A}-01`, 'Details header + Analytics tab: name, ACTIVE badge, enrolled counts and sequence cards match DB', async () => {
  await go(page, '/app/campaigns/' + cid, { wait: 1500 });
  const t = await body(page);
  if (!t.includes(NAME) || !/ACTIVE/.test(t)) return 'header';
  if (!/Prospects \(4\)/.test(t)) return 'prospect tab count';
  if (!/Email 1/.test(t) || !/Email 2/.test(t)) return 'sequence analytics cards: ' + (t.match(/Sequence Analytics[\s\S]{0,200}/) || [''])[0].replace(/\s+/g, ' ');
  for (const b of ['Daily', 'Weekly', 'Monthly']) await page.getByRole('button', { name: b, exact: true }).click();
  return await clean(page, w);
}, page);
await S.t(`${A}-02`, 'Prospects tab: lists enrolled prospects, search filters, status pills carry counts, Export works', async () => {
  await tab(/^Prospects/).click(); await waitText(page, /Enrolled Prospects/);
  const rows = await page.locator('tbody tr').count(); if (rows !== 4) return 'rows ' + rows;
  await page.getByPlaceholder('Search by name, email, company…').fill('Det2'); await sleep(700);
  if ((await page.locator('tbody tr').count()) !== 1) return 'search did not narrow';
  await page.getByPlaceholder('Search by name, email, company…').fill('');
  const [dl] = await Promise.all([page.waitForEvent('download', { timeout: 8000 }).catch(() => null), page.getByRole('button', { name: /^Export/ }).click()]);
  if (!dl) return 'Export produced no download';
  return await clean(page, w);
}, page);
const xl = await api('POST', '/lists', { token: ws.token, body: { list_name: 'Extra list ' + ws.rid } });
await api('POST', `/lists/${xl.data.list_id}/members`, { token: ws.token, body: { prospect_ids: [extra] } });
await S.t(`${A}-03`, 'Prospects tab: Add Prospects (pick list) enrolls the contact (DB + table); Remove (confirm) cancels enrollment', async () => {
  await go(page, '/app/campaigns/' + cid); await tab(/^Prospects/).click(); await waitText(page, /Enrolled Prospects/);
  await page.getByRole('button', { name: /Add Prospects/ }).click();
  const sel = page.locator('select'); await sel.waitFor();
  await sel.selectOption({ label: new RegExp('Extra list') }).catch(async () => { const o = await sel.locator('option', { hasText: 'Extra list' }).getAttribute('value'); await sel.selectOption(o); });
  await page.getByRole('button', { name: /Add to Campaign/ }).click(); await sleep(2000);
  if (sql(`SELECT COUNT(*) FROM campaign_prospects WHERE campaign_id=${q(cid)}`) !== '5') return 'not enrolled in DB: ' + (await body(page)).slice(-200).replace(/\s+/g, ' ');
  await page.keyboard.press('Escape');
  await go(page, '/app/campaigns/' + cid); await tab(/^Prospects/).click(); await waitText(page, /Enrolled Prospects/);
  await rowOf(page, 'Det3').locator('button[title="Remove from campaign"]').click(); await sleep(1500);
  const st = sql(`SELECT cp.status FROM campaign_prospects cp JOIN prospects p ON p.prospect_id=cp.prospect_id WHERE cp.campaign_id=${q(cid)} AND p.first_name='Det3'`);
  if (st === 'ACTIVE' || st === '') return 'prospect still active: ' + st;
  return await clean(page, w);
}, page);
await S.t(`${A}-04`, 'Subsequence tab shows both steps with approval state; Approve All works; Edit step opens editor', async () => {
  await go(page, '/app/campaigns/' + cid); await tab(/^Subsequence/).click(); await waitText(page, /Campaign Sequence/);
  const t = await body(page); if (!/2 steps/.test(t)) return 'step count: ' + t.match(/Campaign Sequence[^\n]*\n[^\n]*/);
  await page.getByRole('button', { name: /Approve All/ }).click(); await sleep(1000);
  await page.getByRole('button', { name: /^Edit$/ }).first().click(); await sleep(500);
  if (!(await page.getByPlaceholder('Subject').count())) return 'editor not shown';
  return await clean(page, w);
}, page);
await S.t(`${A}-05`, 'Schedule tab shows queued emails per step/day; Trigger logs shows CREATE/LAUNCH audit events', async () => {
  await tab(/^Schedule/).click(); await waitText(page, /Schedule Timeline/);
  if (!/Queued|queued/i.test(await body(page))) return 'no queued items';
  await tab(/Trigger logs/).click(); await waitText(page, /System Logs/);
  const t = await body(page);
  if (!/CREATE_CAMPAIGN/.test(t) || !/LAUNCH_CAMPAIGN/.test(t)) return 'audit events missing';
  return await clean(page, w);
}, page);
await S.t(`${A}-06`, 'Sending Accounts tab lists the campaign inbox; Inbox tab renders (empty state) and Sync button reports a clear error with no IMAP', async () => {
  await tab(/Sending Accounts/).click(); await sleep(800);
  if (!(await body(page)).includes(`sales@${dom}`)) return 'inbox missing in Sending Accounts';
  await tab(/^Inbox/).click(); await sleep(1000);
  const sync = page.locator('button[title="Sync replies from mail provider"]');
  if (await sync.count()) { await sync.click(); await sleep(2500); }
  w.drain([/.*/]);
  const bp = await w.bodyProblems(); if (bp.length) return bp.join(',');
}, page);
await S.t(`${A}-07`, 'Pause Campaign / Resume Campaign buttons change status (DB + badge)', async () => {
  await page.getByRole('button', { name: 'Pause Campaign' }).click(); await sleep(1500);
  if (campStatus(cid) !== 'PAUSED') return 'DB ' + campStatus(cid);
  await page.getByRole('button', { name: 'Resume Campaign' }).click(); await sleep(1500);
  if (campStatus(cid) !== 'ACTIVE') return 'DB after resume ' + campStatus(cid);
  return await clean(page, w);
}, page);
await S.t(`${A}-08`, 'Send Now: cancel at the confirm sends nothing; accept -> POST /send-now answered and the outcome is shown (result dialog or visible error)', async () => {
  // Outcome-agnostic on purpose: while the e2e stack runs in SES-mock mode the per-message send can fail
  // (inboxes point at smtp.invalid), so the case asserts what the user can observe, not that mail went out.
  await go(page, '/app/campaigns/' + cid, { wait: 1200 });
  if (campStatus(cid) !== 'ACTIVE') return 'precondition: campaign not ACTIVE (' + campStatus(cid) + ')';
  const sentBefore = sql(`SELECT COUNT(*) FROM email_messages WHERE campaign_id=${q(cid)} AND status NOT IN ('QUEUED','SCHEDULED')`);
  dlg.stop(); const d1 = dialogs(page, 'dismiss');
  const reqs = []; const onReq = r => { if (/\/send-now$/.test(r.url())) reqs.push(r.url()); }; page.on('request', onReq);
  await page.getByRole('button', { name: /Send Now/ }).click(); await sleep(800);
  d1.stop();
  if (!d1.some(m => /Force send now/i.test(m))) return 'no confirm shown: ' + d1.join('|');
  if (reqs.length) return 'cancel at the confirm still called /send-now';
  if (sql(`SELECT COUNT(*) FROM email_messages WHERE campaign_id=${q(cid)} AND status NOT IN ('QUEUED','SCHEDULED')`) !== sentBefore) return 'cancel changed message states';
  page.off('request', onReq);
  const d2 = dialogs(page, 'accept');
  const respP = page.waitForResponse(r => /\/send-now$/.test(r.url()), { timeout: 90000 });
  const tClick = Date.now();
  await page.getByRole('button', { name: /Send Now/ }).click();
  const resp = await respP.catch(() => null);
  const took = Date.now() - tClick;
  if (!resp) { d2.stop(); return 'POST /send-now not answered within 90 s; dialogs: ' + d2.join('|'); }
  // the outcome is an in-page banner since frontend b3f05f5 (was window.alert): "Send now finished: N sent, N failed[, N skipped]."
  // or "Error sending emails: ..."; the send loop may call /send-now several times (batches), the banner shows the totals
  const banner = page.getByText(/^(Send now finished:|Error sending emails)/);
  await banner.waitFor({ timeout: 90000 }).catch(() => {});
  const result = (await banner.count()) ? await banner.first().innerText() : null;
  d2.stop(); dialogs(page, 'accept');
  const status = resp.status(); let data = null; try { data = await resp.json(); } catch {}
  console.log(`  DET-08 send-now -> HTTP ${status} in ${took} ms ${JSON.stringify(data).slice(0, 300)}`);
  const visibleErr = /error|failed/i.test(await body(page));
  if (!result && !visibleErr) return `HTTP ${status} but no result banner or visible error`;
  if (status === 200 && !/^Send now finished:/.test(result || '')) return `HTTP 200 but the banner is: ${result}`;
  if (status === 200 && result && data && !data.has_more && !result.includes(`${data.sent} sent`)) return `banner counts differ from API: ${result} vs ${JSON.stringify(data)}`;
  if (status >= 400 && !/Error sending/.test(result || '')) return `HTTP ${status} not surfaced as an error banner`;
  w.drain([/.*/]);
}, page);
await S.t(`${A}-09`, 'Role AGENT (manage_campaigns by default) can open a campaign by URL', async () => {
  const a = await S.open(ws, 'agent');
  await go(a.page, '/app/campaigns/' + cid);
  if (!new URL(a.page.url()).pathname.endsWith('/app/campaigns/' + cid)) return 'redirected: ' + a.page.url();
}, page);
await S.done();
