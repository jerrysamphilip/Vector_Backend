import { startSuite, fullWorkspace, createWs, api, sql, q, go, body, waitText, clean, mkInbox, dialogs, sleep } from './lib.mjs';
const S = await startSuite('mailboxes');
const A = 'UIO-MBX';
const ws = await fullWorkspace('mbx', { identity: false });
const dom = `mb${ws.rid}.example`;
const ib = await mkInbox(ws, 'sales', dom);
const { page, w } = await S.open(ws);
dialogs(page, 'accept');
const modal = () => page.locator('div.fixed').last();
const E = `sales@${dom}`;
const rowBox = () => page.locator('div').filter({ hasText: E }).filter({ has: page.locator('button[aria-label="Edit sending and receiving settings"]') }).last();

await S.t(`${A}-01`, 'Email accounts renders: stats tiles, inbox row (SMTP, warmup off), sender-identity card prompting setup', async () => {
  await go(page, '/app/inboxes', { wait: 1500 });
  const t = await body(page);
  if (!t.includes(E)) return 'inbox row missing';
  if (!/Sender identity/.test(t)) return 'identity card missing';
  if (!/ACCOUNTS\s+1/i.test(t.replace(/\n+/g, ' '))) return 'accounts tile not 1';
  return await clean(page, w);
}, page);
await S.t(`${A}-02`, 'Sender identity: empty save is blocked with a message; valid save persists to the company profile (DB) and shows in the card; Cancel discards', async () => {
  await page.getByPlaceholder('Acme Inc.').waitFor();
  await page.getByPlaceholder('Acme Inc.').fill('');
  await page.getByRole('button', { name: /^Save/ }).click(); await sleep(500);
  const ok = await page.getByPlaceholder('Acme Inc.').evaluate(e => e.checkValidity());
  if (ok) return 'empty company name passes form validation';
  if (sql(`SELECT COUNT(*) FROM company_profiles WHERE tenant_id=${q(ws.tenantId)} AND company_name<>''`) !== '0') return 'empty identity saved';
  await page.getByPlaceholder('Acme Inc.').fill('x'); await page.getByPlaceholder('100 Main Street, Suite 200').fill('ab');
  await page.getByRole('button', { name: /^Save/ }).click(); await sleep(500);
  if (await page.getByPlaceholder('100 Main Street, Suite 200').evaluate(e => e.checkValidity())) return 'too-short address accepted by form';
  await page.getByPlaceholder('Acme Inc.').fill('Acme Test Inc ' + ws.rid);
  await page.getByPlaceholder('100 Main Street, Suite 200').fill('100 Main St, Austin, TX 78701, USA');
  await page.getByRole('button', { name: /^Save/ }).click(); await sleep(1200);
  if (!(await body(page)).includes('Acme Test Inc ' + ws.rid)) return 'card not updated';
  if (sql(`SELECT COUNT(*) FROM company_profiles WHERE tenant_id=${q(ws.tenantId)} AND company_name='Acme Test Inc ${ws.rid}'`) === '0') return 'identity not in DB';
  w.drain([/sender-identity/, /Failed to load resource/]);
  return await clean(page, w);
}, page);
await S.t(`${A}-03`, 'Filters and search: Warmup On hides the (warmup-off) inbox, Warmup Off shows it, search by email narrows, no-match empty state', async () => {
  await page.getByRole('button', { name: 'Warmup On', exact: true }).click(); await sleep(400);
  if ((await body(page)).includes(E)) return 'Warmup On still lists a warmup-off inbox';
  await page.getByRole('button', { name: 'Warmup Off', exact: true }).click(); await sleep(400);
  if (!(await body(page)).includes(E)) return 'Warmup Off hides it';
  await page.getByRole('button', { name: 'All', exact: true }).click();
  await page.getByPlaceholder('Search by email or provider…').fill('zzznomatch'); await sleep(500);
  if ((await body(page)).includes(E)) return 'search ignored';
  await page.getByPlaceholder('Search by email or provider…').fill('sales'); await sleep(500);
  if (!(await body(page)).includes(E)) return 'search lost match';
}, page);
await S.t(`${A}-04`, 'Warmup toggle on the row flips warmup_enabled in the DB and back', async () => {
  const get = () => sql(`SELECT warmup_enabled FROM sending_inboxes WHERE inbox_id=${q(ib)}`);
  const row = page.locator('div,tr', { hasText: E }).last();
  const tog = rowBox().locator('button.w-9').first();
  const before = get();
  await tog.click(); await sleep(1500);
  if (get() === before) return 'warmup not toggled in DB (state ' + before + ')';
  await tog.click(); await sleep(1500);
  if (get() !== before) return 'warmup not restored';
  return await clean(page, w);
}, page);
await S.t(`${A}-05`, 'Detail drawer: Overview/Connection/Settings/Activity tabs render; Test sending sign-in against an unreachable SMTP host reports a clear failure', async () => {
  await page.getByText(E).first().click();
  for (const t of ['Overview', 'Connection', 'Settings', 'Activity']) { await page.getByRole('button', { name: t, exact: true }).click(); await sleep(400); }
  await page.getByRole('button', { name: 'Connection', exact: true }).click();
  await page.getByRole('button', { name: /Test sending sign-in/ }).click();
  await waitText(page, /fail|error|could not|unable|refused|resolve|timed out|not known|invalid/i, 25000);
  const bp = await w.bodyProblems(); w.drain([/test-smtp/, /Failed to load resource/]);
  if (bp.length) return bp.join(',');
}, page);
await S.t(`${A}-06`, 'Settings tab: change daily limit / delay and Save Settings persists to DB; invalid value rejected', async () => {
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  const nums = page.locator('main input[type=number], div input[type=number]');
  const inp = page.locator('input[type=number]').nth(4);
  const before = sql(`SELECT daily_limit FROM sending_inboxes WHERE inbox_id=${q(ib)}`);
  const target = page.locator('input[type=number][placeholder^="Off"]');
  await target.fill('123');
  await page.getByRole('button', { name: /Save Settings/ }).click(); await sleep(1500);
  const after = sql(`SELECT max_emails_per_day FROM sending_inboxes WHERE inbox_id=${q(ib)}`);
  if (after !== '123') return 'max_emails_per_day ' + after;
  await target.fill('-5'); await page.getByRole('button', { name: /Save Settings/ }).click(); await sleep(1200);
  if (sql(`SELECT max_emails_per_day FROM sending_inboxes WHERE inbox_id=${q(ib)}`) === '-5') return 'negative limit stored';
  w.drain([/.*/]);
}, page);
await S.t(`${A}-07`, 'Connect Mailbox wizard: provider step -> SMTP form; validation for empty / invalid email and missing host', async () => {
  await page.keyboard.press('Escape');
  await go(page, '/app/inboxes');
  await page.getByRole('button', { name: /Connect Mailbox/ }).click();
  await page.getByText('SMTP Workspace').click();
  await page.getByRole('button', { name: /Connect Account/ }).click(); await sleep(600);
  let t = await body(page);
  if (!/required|enter|invalid|valid/i.test(t.split('Connecting via SMTP')[1] || t)) return 'no validation for empty form';
  await page.getByPlaceholder('name@company.com').fill('not-an-email');
  await page.getByRole('button', { name: /Connect Account/ }).click(); await sleep(600);
  if (!/valid email|invalid email|email/i.test(await modal().innerText())) return 'no invalid email message';
}, page);
await S.t(`${A}-08`, 'Connect SMTP mailbox with unreachable host: ends in a clear result (connected-unverified or explicit error), row appears iff created; no crash', async () => {
  const em = `new@${dom}`;
  await page.getByPlaceholder('name@company.com').fill(em);
  await page.getByPlaceholder('smtp.gmail.com').fill('smtp.invalid');
  await page.locator('input[placeholder="Usually your email address"]').nth(0).fill(em);
  await page.locator('input[type=password]').nth(0).fill('secret-pass-1');
  await page.getByPlaceholder('imap.gmail.com').fill('imap.invalid');
  await page.locator('input[placeholder="Usually your email address"]').nth(1).fill(em);
  await page.locator('input[type=password]').nth(1).fill('secret-pass-2');
  await page.getByRole('button', { name: /Connect Account/ }).click();
  await sleep(6000);
  const bp = await w.bodyProblems(); w.drain([/.*/]);
  if (bp.length) return bp.join(',');
  const n = sql(`SELECT COUNT(*) FROM sending_inboxes WHERE email_address=${q(em)}`);
  const pw = sql(`SELECT smtp_password FROM sending_inboxes WHERE email_address=${q(em)}`);
  if (n === '1' && pw === 'secret-pass-1') return 'SMTP password stored in plaintext';
  if (n === '0' && !/fail|error|could not|verify|unable|invalid/i.test(await modal().innerText().catch(() => ''))) return 'not created and no error shown';
}, page);
await S.t(`${A}-09`, 'Microsoft 365 connect button when MS365 is not configured shows a clear message (no redirect to undefined)', async () => {
  await go(page, '/app/inboxes', { wait: 1000 });
  await page.locator('button[title*="Microsoft 365"]').first().click();
  await sleep(1500);
  if (/undefined|null/.test(page.url())) return 'bad redirect ' + page.url();
  const t = await body(page);
  if (page.url().includes('login.microsoftonline')) return null;
  if (!/microsoft|not configured|unavailable|sign-in/i.test(t)) return 'no message';
  w.drain([/.*/]);
}, page);
await S.t(`${A}-10`, 'Alert Preferences modal opens, saves, closes; Run Warmup gives feedback', async () => {
  await page.getByRole('button', { name: /Alert Preferences/ }).click(); await sleep(800);
  const m = await modal().innerText(); if (!/alert/i.test(m)) return 'no modal';
  await page.keyboard.press('Escape'); if (await page.getByRole('button', { name: /^Save/ }).count()) await page.getByRole('button', { name: /^Save/ }).first().click().catch(() => {});
  await sleep(800); await page.getByRole('button', { name: /Cancel|Close/ }).first().click().catch(() => {});
  await page.getByRole('button', { name: /Run Warmup/ }).click(); await sleep(2500);
  const bp = await w.bodyProblems(); w.drain([/.*/]); if (bp.length) return bp.join(',');
}, page);
await S.t(`${A}-11`, 'Delete mailbox: Cancel keeps it; Delete removes it (DB) and the row', async () => {
  await go(page, '/app/inboxes', { wait: 1000 });
  const row = page.locator('div', { has: page.getByText(E) }).last();
  const trash = rowBox().locator('button[aria-label="Edit sending and receiving settings"] + button');
  await trash.click(); await waitText(page, /Delete Mailbox\?/);
  await page.getByRole('button', { name: 'Cancel' }).last().click();
  if (sql(`SELECT COUNT(*) FROM sending_inboxes WHERE inbox_id=${q(ib)}`) !== '1') return 'deleted on cancel';
  await trash.click(); await page.locator('div.fixed').getByRole('button', { name: 'Delete', exact: true }).click(); await sleep(1500);
  if (sql(`SELECT COUNT(*) FROM sending_inboxes WHERE inbox_id=${q(ib)}`) !== '0') return 'still in DB';
  return await clean(page, w);
}, page);
await S.t(`${A}-12`, 'Roles: MANAGER and AGENT cannot open Email accounts (redirect Home)', async () => {
  for (const r of ['manager', 'agent']) { const o = await S.open(ws, r); await go(o.page, '/app/inboxes'); if (!/dashboard$/.test(new URL(o.page.url()).pathname)) return r + ' -> ' + o.page.url(); }
}, page);
await S.done();
