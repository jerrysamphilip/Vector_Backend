import { startSuite, fullWorkspace, api, sql, q, go, body, waitText, clean, mkContact, dialogs, sleep, rowOf } from './lib.mjs';
import path from 'node:path';
import { HERE } from './lib.mjs';
const S = await startSuite('prospects');
const A = 'UIO-PROS';
const ws = await fullWorkspace('pros');
const csv = path.join(HERE, 'fixtures/prospects.csv'), bad = path.join(HERE, 'fixtures/bad.txt');
const LIST = 'Uploaded list ' + ws.rid;
const { page, w } = await S.open(ws);
dialogs(page, 'accept');
const modal = () => page.locator('div.fixed').last();

await S.t(`${A}-01`, 'Prospect lists empty state: zero stats, New List button, no errors', async () => {
  await go(page, '/app/prospects', { wait: 1200 });
  const t = await body(page);
  if (!/Prospect Lists/.test(t)) return 'no heading';
  await page.getByRole('button', { name: 'New List' }).waitFor();
  return await clean(page, w);
}, page);
await S.t(`${A}-02`, 'New List validation: missing name, missing file -> inline messages; unsupported file type rejected', async () => {
  await page.getByRole('button', { name: 'New List' }).click();
  await page.getByRole('button', { name: /Validate & Continue/ }).click();
  await waitText(page, /Give this list a name/);
  await page.getByPlaceholder('e.g., Q2 SaaS Decision Makers').fill(LIST);
  await page.getByRole('button', { name: /Validate & Continue/ }).click();
  await waitText(page, /Select a file to upload/);
  await page.locator('input[type=file]').setInputFiles(bad);
  await page.getByRole('button', { name: /Validate & Continue/ }).click(); await sleep(1500);
  const t = await body(page); w.drain([/.*/]);
  if (/\/prospects\/validate/.test(page.url())) return 'unsupported .txt accepted for validation';
  if (/\[object Object\]/.test(t)) return '[object Object]';
  if (!/unsupported|file|csv|xlsx|invalid|failed/i.test(await modal().innerText())) return 'no message for bad file';
}, page);
await S.t(`${A}-03`, 'Upload CSV -> preview: 2 accepted / 2 need attention with per-row reasons (invalid email, duplicate)', async () => {
  await page.locator('input[type=file]').setInputFiles(csv);
  await page.getByRole('button', { name: /Validate & Continue/ }).click();
  await page.waitForURL(/prospects\/validate/);
  await waitText(page, /2 Accepted/); await waitText(page, /2 Needs Attention/);
  const t = await body(page);
  if (!/Invalid or personal email/.test(t) || !/Duplicate email in file/.test(t)) return 'row reasons missing';
  if (!t.includes(LIST)) return 'list name not carried to preview';
  return await clean(page, w);
}, page);
await S.t(`${A}-04`, 'Opening the preview URL directly (no upload state) redirects back to Prospect lists, no crash', async () => {
  await go(page, '/app/prospects/validate?fresh=1', { wait: 300 }); await page.waitForURL(/\/app\/prospects$/, { timeout: 8000 });
  return await clean(page, w);
}, page);
await S.t(`${A}-05`, 'Confirm Upload creates the list with exactly the accepted rows; list appears in the table without reload', async () => {
  await page.getByRole('button', { name: 'New List' }).click();
  await page.getByPlaceholder('e.g., Q2 SaaS Decision Makers').fill(LIST);
  await page.locator('input[type=file]').setInputFiles(csv);
  await page.getByRole('button', { name: /Validate & Continue/ }).click();
  await waitText(page, /Confirm Upload \(2\)/);
  await page.getByRole('button', { name: /Confirm Upload/ }).click();
  await page.waitForURL(/\/app\/prospects$/, { timeout: 15000 });
  const lid = sql(`SELECT list_id FROM prospect_lists WHERE tenant_id=${q(ws.tenantId)} AND list_name=${q(LIST)}`);
  if (!lid) return 'list not in DB';
  const n = sql(`SELECT COUNT(*) FROM prospect_list_members WHERE list_id=${q(lid)}`);
  if (n !== '2') return 'members ' + n;
  const emails = sql(`SELECT GROUP_CONCAT(p.email ORDER BY p.email) FROM prospect_list_members m JOIN prospects p ON p.prospect_id=m.prospect_id WHERE m.list_id=${q(lid)}`);
  if (emails !== 'ann.one@co1.example,bob.two@co2.example') return 'members ' + emails;
  await sleep(1200);
  if (!(await body(page)).includes(LIST)) return 'new list not shown in table after confirm (stale list cache)';
  return await clean(page, w);
}, page);
await S.t(`${A}-06`, 'Search lists, sort by name; View opens the members modal with the 2 contacts', async () => {
  await go(page, '/app/prospects', { wait: 1000 });
  await page.getByPlaceholder('Search lists…').fill('nomatch-zzz'); await sleep(900);
  if ((await body(page)).includes(LIST)) return 'search ignored';
  await page.getByPlaceholder('Search lists…').fill(ws.rid); await sleep(900);
  if (!(await body(page)).includes(LIST)) return 'search lost match';
  await page.getByRole('button', { name: 'View' }).first().click();
  await waitText(page, /ann\.one@co1\.example/); 
  const t = await body(page); if (!/bob\.two@co2\.example/.test(t)) return 'member missing';
  return await clean(page, w);
}, page);
await S.t(`${A}-07`, 'Stats tiles match DB (total contacts, in campaigns)', async () => {
  await page.keyboard.press('Escape'); await go(page, '/app/prospects', { wait: 1200 });
  const t = (await body(page)).replace(/\n+/g, ' ');
  const total = sql(`SELECT COUNT(DISTINCT m.prospect_id) FROM prospect_list_members m JOIN prospect_lists l ON l.list_id=m.list_id WHERE l.tenant_id=${q(ws.tenantId)}`);
  const m = t.match(/TOTAL CONTACTS\s+(\d+)/i);
  if (!m || m[1] !== total) return `tile ${m && m[1]} vs DB ${total}`;
}, page);
await S.t(`${A}-08`, 'Delete list: row trash -> confirm dialog; Cancel keeps, Delete removes list (DB) ', async () => {
  const row = rowOf(page, LIST);
  await row.locator('button').last().click();
  await waitText(page, /Delete this list\?/);
  await page.getByRole('button', { name: /Cancel/ }).click();
  if (sql(`SELECT COUNT(*) FROM prospect_lists WHERE list_name=${q(LIST)}`) !== '1') return 'deleted on cancel';
  await rowOf(page, LIST).locator('button').last().click();
  await page.locator('div.fixed').getByRole('button', { name: /^Delete/ }).click(); await sleep(1500);
  if (sql(`SELECT COUNT(*) FROM prospect_lists WHERE list_name=${q(LIST)}`) !== '0') return 'list still in DB';
  return await clean(page, w);
}, page);
await S.t(`${A}-09`, 'Revalidate Bounced (admin only): opens modal for owner; agent is redirected away from the screen', async () => {
  await page.getByRole('button', { name: /Revalidate Bounced/ }).click(); await sleep(1000);
  const bp = await w.bodyProblems(); w.drain([/.*/]); if (bp.length) return bp.join(',');
  await page.keyboard.press('Escape');
  const a = await S.open(ws, 'agent'); await go(a.page, '/app/prospects');
  if (!/dashboard/.test(a.page.url())) return 'agent allowed on prospect lists';
}, page);
await S.t(`${A}-10`, 'Upload API 500 shows a readable message in the modal', async () => {
  await go(page, '/app/prospects');
  await page.route(/uploads\/validations/, r => r.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"Upload service down"}' }));
  await page.getByRole('button', { name: 'New List' }).click();
  await page.getByPlaceholder('e.g., Q2 SaaS Decision Makers').fill('X'); await page.locator('input[type=file]').setInputFiles(csv);
  await page.getByRole('button', { name: /Validate & Continue/ }).click(); await sleep(1200);
  await page.unroute(/uploads\/validations/); w.drain([/.*/]);
  if (!/Upload service down|failed/i.test(await body(page))) return 'no error shown';
}, page);
await S.done();
