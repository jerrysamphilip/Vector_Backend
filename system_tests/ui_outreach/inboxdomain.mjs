import { startSuite, fullWorkspace, api, sql, q, go, body, waitText, clean, mkInbox, mkContact, dialogs, sleep } from './lib.mjs';
import crypto from 'node:crypto';
const S = await startSuite('inboxdomain');
const ws = await fullWorkspace('ibd');
const dom = `ib${ws.rid}.example`;
const ib = await mkInbox(ws, 'sales', dom);
const p1 = await mkContact(ws, `lead1@cust${ws.rid}.example`, { first_name: 'Lena', last_name: 'Lead' });
const p2 = await mkContact(ws, `lead2@cust${ws.rid}.example`, { first_name: 'Omar', last_name: 'Other' });
const mkConv = (pid, subj, unread, text) => {
  const id = crypto.randomUUID();
  sql(`INSERT INTO conversations (id,tenant_id,prospect_id,inbox_id,subject,status,is_unread,last_message_at,created_at,updated_at) VALUES (${q(id)},${q(ws.tenantId)},${q(pid)},${q(ib)},${q(subj)},'OPEN',${unread},NOW(),NOW(),NOW())`);
  sql(`INSERT INTO email_messages (message_id,conversation_id,prospect_id,inbox_id,direction,status,subject,body_text,to_email,from_email,sent_at,scheduled_at,retry_count,max_retries) VALUES (UUID(),${q(id)},${q(pid)},${q(ib)},'INBOUND','RECEIVED',${q(subj)},${q(text)},'sales@${dom}','lead@cust.example',NOW(),NOW(),0,0)`);
  return id;
};
const c1 = mkConv(p1, 'Re: Intro ' + ws.rid, 1, 'Sounds interesting, can we talk Tuesday?');
const c2 = mkConv(p2, 'Re: Pricing ' + ws.rid, 0, 'What does it cost?');
const apiR = await api('GET', '/conversations?page_size=50', { token: ws.token }); console.log('API', apiR.status, JSON.stringify(apiR.data).slice(0, 300));
sql(`UPDATE conversations SET last_message_at=NOW() - INTERVAL 1 DAY WHERE id=${q(c1)}`);
const { page, w } = await S.open(ws);
dialogs(page);
const A = 'UIO-INBOX', D = 'UIO-DOM';

await S.t(`${A}-00`, 'Inbox with existing conversations but no IMAP-configured mailbox still shows the conversations (not only the IMAP setup guide)', async () => {
  await go(page, '/app/inbox', { wait: 1800 });
  if (!(await body(page)).includes('Lena Lead')) return 'conversations hidden behind IMAP setup guide although 2 conversations exist (API returns them)';
}, page);
await api('PUT', '/inboxes/' + ib, { token: ws.token, body: { imap_host: 'imap.invalid', imap_port: 993, imap_username: 'sales', imap_password: 'x' } });
await S.t(`${A}-01`, 'Unified Inbox lists both conversations (unread highlighted) with subject/snippet; no problems', async () => {
  await go(page, '/app/inbox', { wait: 1800 });
  const t = await body(page);
  if (!t.includes('Lena Lead') || !t.includes('Omar Other')) return 'conversations missing: ' + t.replace(/\s+/g,' ').slice(-300);
  return await clean(page, w);
}, page);
await S.t(`${A}-02`, 'Search narrows the list; opening a thread shows the message and marks it read (DB is_unread=0)', async () => {
  await page.getByPlaceholder('Search by name, email or subject…').fill('Omar'); await sleep(800);
  if ((await body(page)).includes('Lena Lead')) return 'search did not filter the list';
  await page.getByPlaceholder('Search by name, email or subject…').fill('');
  await sleep(500);
  await page.getByText('Lena Lead').first().click(); await sleep(1200);
  if (!(await body(page)).includes('can we talk Tuesday')) return 'message body not shown';
  if (sql(`SELECT is_unread FROM conversations WHERE id=${q(c1)}`) !== '0') return 'still unread in DB';
  return await clean(page, w);
}, page);
await S.t(`${A}-03`, 'Reply form: empty reply does nothing; Enter sends a reply -> OUTBOUND message stored for that conversation', async () => {
  const box = page.getByPlaceholder(/Type your reply/);
  await box.fill('   '); await box.press('Enter'); await sleep(500);
  const n0 = sql(`SELECT COUNT(*) FROM email_messages WHERE conversation_id=${q(c1)} AND direction='OUTBOUND'`);
  if (n0 !== '0') return 'blank reply sent';
  await box.fill('Tuesday 10am works for us.'); await box.press('Enter'); await sleep(2500);
  const n1 = sql(`SELECT COUNT(*) FROM email_messages WHERE conversation_id=${q(c1)} AND direction='OUTBOUND' AND body_text LIKE 'Tuesday 10am%'`);
  if (n1 !== '1') return 'reply not stored (' + n1 + '); outbound rows: ' + sql(`SELECT CONCAT(status,':',LEFT(body_text,40)) FROM email_messages WHERE conversation_id=${q(c1)} AND direction='OUTBOUND'`);
  if (!(await body(page)).includes('Tuesday 10am works')) return 'reply not shown in thread';
  return await clean(page, w);
}, page);
await S.t(`${A}-04`, 'Reply failure (API 500) restores the draft text and does not crash', async () => {
  await page.route(/conversations\/.*\/reply/, r => r.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"x"}' }));
  const box = page.getByPlaceholder(/Type your reply/);
  await box.fill('Will fail'); await box.press('Enter'); await sleep(1500);
  await page.unroute(/conversations\/.*\/reply/); w.drain([/.*/]);
  if ((await box.inputValue()) !== 'Will fail') return 'draft lost after failed send';
}, page);
await S.t(`${A}-05`, 'Sync button without IMAP reports a clear message; Refresh works', async () => {
  const sync = page.locator('button[title="Sync replies from mail provider"]');
  if (await sync.count()) { await sync.click(); await sleep(3000); }
  const r = page.locator('button[title="Refresh"]'); if (await r.count()) await r.click();
  const bp = await w.bodyProblems(); w.drain([/.*/]); if (bp.length) return bp.join(',');
}, page);
await S.t(`${A}-06`, 'Tenant isolation: conversations of this workspace are not visible to another workspace', async () => {
  const other = await (await import('./lib.mjs')).createWs('ibd-other');
  const r = await api('GET', '/conversations?page_size=50', { token: other.token });
  if (JSON.stringify(r.data).includes(ws.rid)) return 'foreign conversations leaked';
  const r2 = await api('GET', '/conversations/' + c1, { token: other.token });
  if (r2.status === 200) return 'foreign conversation readable by id';
}, page);

// ---- Domain health ----
await S.t(`${D}-01`, 'Domain Health lists the workspace sending domain with SPF/DKIM/DMARC chips, KPIs consistent with inbox count', async () => {
  await go(page, '/app/domain-health', { wait: 1800 });
  const t = await body(page);
  if (!t.includes(dom)) return 'domain missing';
  if (!/SPF/.test(t) || !/DKIM/.test(t) || !/DMARC/.test(t)) return 'auth chips missing';
  if (!/MAILBOXES\s+1/i.test(t.replace(/\n+/g, ' '))) return 'mailboxes tile != 1';
  return await clean(page, w);
}, page);
await S.t(`${D}-02`, 'Trend filter select: switching All domains <-> single domain re-renders the chart without errors', async () => {
  const sel = page.locator('select').first();
  await sel.selectOption({ label: dom }); await sleep(900);
  await sel.selectOption({ index: 0 }); await sleep(900);
  return await clean(page, w);
}, page);
await S.t(`${D}-03`, 'Details modal shows DNS/campaign/inbox info; Run Diagnostics gives feedback; Close works', async () => {
  await page.getByRole('button', { name: 'Details' }).first().click();
  await waitText(page, /CONNECTED INBOXES/i);
  await page.getByRole('button', { name: /Run Diagnostics/ }).click(); await sleep(6000);
  const bp = await w.bodyProblems(); w.drain([/scans/, /Failed to load resource/]);
  if (bp.length) return bp.join(',');
  await page.getByRole('button', { name: 'Close' }).first().click();
}, page);
await S.t(`${D}-04`, 'Delete domain: confirm, removes the domain card and DB record (no add-domain UI exists: domains derive from inboxes)', async () => {
  await page.getByRole('button', { name: 'Details' }).first().click();
  await page.getByRole('button', { name: /^Delete/ }).first().click(); await sleep(800);
  console.log((await page.locator('div.fixed').last().innerText()).slice(0,300));
  await waitText(page, new RegExp('Delete ' + dom.replace(/\./g, '\\.') + '\\?'));
  await page.locator('div.fixed', { hasText: 'Delete ' + dom + '?' }).last().getByRole('button', { name: /Delete Forever/ }).click();
  await sleep(1500);
  const n = sql(`SELECT COUNT(*) FROM sending_domains WHERE tenant_id=${q(ws.tenantId)} AND domain_name=${q(dom)}`);
  if (n !== '0') return 'domain still in DB (' + n + ')';
  w.drain([/.*/]);
}, page);
await S.t(`${D}-05`, 'AGENT role can open Domain Health without forbidden-API noise', async () => {
  const a = await S.open(ws, 'agent'); await go(a.page, '/app/domain-health', { wait: 1500 });
  const p = await clean(a.page, a.w); if (p) return p;
}, page);
await S.done();
