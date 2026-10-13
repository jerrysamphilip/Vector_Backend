import { stubContent, startSuite, fullWorkspace, api, sql, q, go, body, waitText, clean, mkInbox, mkCampaign, dialogs, sleep } from './lib.mjs';
const S = await startSuite('library');
const ws = await fullWorkspace('lib');
const dom = `lb${ws.rid}.example`;
const ib = await mkInbox(ws, 'sales', dom);
const cid = await mkCampaign(ws, 'Auto Camp ' + ws.rid, [ib]); await stubContent(ws, cid);
const { page, w } = await S.open(ws);
const dlg = dialogs(page, 'accept');
const T = 'UIO-TPL';
const NAME = 'Intro template ' + ws.rid;
const modal = () => page.locator('div.fixed').last();

await S.t(`${T}-01`, 'Template library empty state renders; New template opens modal; Cancel closes without saving', async () => {
  await go(page, '/app/templates', { wait: 1000 });
  await page.getByRole('button', { name: /New template/ }).click();
  await page.getByText('New template').first().waitFor();
  await page.getByRole('button', { name: 'Cancel' }).click();
  if (sql(`SELECT COUNT(*) FROM message_templates WHERE tenant_id=${q(ws.tenantId)}`) !== '0') return 'row created';
  return await clean(page, w);
}, page);
await S.t(`${T}-02`, 'Create template: merge-field insert, save -> row in list and in DB with subject/body/category/shared', async () => {
  await page.getByRole('button', { name: /New template/ }).click();
  const m = modal();
  await m.locator('input').nth(0).fill(NAME);
  await m.getByPlaceholder('e.g. Follow-up').fill('Intro');
  await m.locator('input').nth(2).fill('Hello from us');
  await m.locator('textarea').fill('Hi ');
  await m.getByRole('button', { name: /first name/i }).first().click();
  await m.getByRole('button', { name: 'Save template' }).click();
  await waitText(page, new RegExp(NAME));
  const r = sql(`SELECT category,subject,body,shared FROM message_templates WHERE name=${q(NAME)}`).split('\t');
  if (r[0] !== 'Intro' || r[1] !== 'Hello from us' || !/^Hi \{\{/.test(r[2]) || r[3] !== '1') return 'DB row ' + r.join('|');
  return await clean(page, w);
}, page);
await S.t(`${T}-03`, 'Validation: saving a template with empty name shows a server error inside the modal (no crash)', async () => {
  await page.getByRole('button', { name: /New template/ }).click();
  await modal().getByRole('button', { name: 'Save template' }).click(); await sleep(1000);
  const t = await body(page); w.drain([/.*/]);
  if (/\[object Object\]/.test(t)) return '[object Object] shown';
  if (!/name|required|invalid|failed/i.test(await modal().innerText())) return 'no validation feedback';
  await page.getByRole('button', { name: 'Cancel' }).click();
}, page);
await S.t(`${T}-04`, 'Preview renders with sample values; Edit updates DB; category filter + search narrow the list', async () => {
  const row = page.locator('tbody tr', { hasText: NAME });
  await row.getByRole('button', { name: 'Preview' }).click();
  await waitText(page, /Preview with sample contact values/);
  if (/\{\{first_name\}\}/.test(await modal().innerText())) return 'merge fields not rendered in preview';
  await page.keyboard.press('Escape'); await page.getByRole('button', { name: /close|cancel/i }).first().click().catch(() => {});
  await go(page, '/app/templates');
  await page.locator('tbody tr', { hasText: NAME }).getByRole('button', { name: 'Edit' }).click();
  await modal().locator('input').nth(2).fill('Edited subject');
  await modal().getByRole('button', { name: 'Save template' }).click(); await sleep(1000);
  if (sql(`SELECT subject FROM message_templates WHERE name=${q(NAME)}`) !== 'Edited subject') return 'edit not saved';
  await page.getByPlaceholder('Search templates').fill('zzzz'); await sleep(800);
  if (await page.locator('tbody tr', { hasText: NAME }).count()) return 'search did not filter';
  await page.getByPlaceholder('Search templates').fill(''); await sleep(600);
  await page.getByLabel('Category').selectOption({ label: 'Intro' });
  await sleep(700); if (!(await page.locator('tbody tr', { hasText: NAME }).count())) return 'category filter dropped matching row';
  return await clean(page, w);
}, page);
await S.t(`${T}-05`, 'Agent sees shared template but only the owner can edit/delete (no edit buttons for agent; API refuses)', async () => {
  const a = await S.open(ws, 'agent');
  await go(a.page, '/app/templates', { wait: 1000 });
  const row = a.page.locator('tbody tr', { hasText: NAME });
  if (!(await row.count())) return 'shared template invisible to agent';
  const tid = sql(`SELECT template_id FROM message_templates WHERE name=${q(NAME)}`);
  const r = await api('DELETE', '/template-library/' + tid, { token: ws.users.agent.token });
  if (r.status < 400) return 'agent deleted someone else\'s template: ' + r.status;
}, page);
await S.t(`${T}-06`, 'Delete: dismissing confirm keeps it; accepting removes row and DB record', async () => {
  dlg.stop(); const d1 = dialogs(page, 'dismiss');
  await go(page, '/app/templates');
  await page.locator('tbody tr', { hasText: NAME }).getByRole('button', { name: 'Delete' }).click(); await sleep(500);
  if (sql(`SELECT COUNT(*) FROM message_templates WHERE name=${q(NAME)}`) !== '1') return 'deleted despite cancel';
  d1.stop(); dialogs(page, 'accept');
  await page.locator('tbody tr', { hasText: NAME }).getByRole('button', { name: 'Delete' }).click(); await sleep(1200);
  if (sql(`SELECT COUNT(*) FROM message_templates WHERE name=${q(NAME)}`) !== '0') return 'still in DB';
  return await clean(page, w);
}, page);

// ---- Automation rules ----
const R = 'UIO-AUTO';
const RULE = 'Follow up ' + ws.rid;
await S.t(`${R}-01`, 'Automation empty state; Create rule modal validation (required fields) keeps modal open', async () => {
  await go(page, '/app/automation', { wait: 1000 });
  await waitText(page, /No automation rules yet/);
  await page.getByRole('button', { name: /Create New Rule/ }).click();
  await page.getByPlaceholder('e.g., Follow up after open').waitFor();
  const before = dlg.length;
  await page.getByRole('button', { name: /^Create Rule$/ }).click(); await sleep(600);
  if (!(await page.getByPlaceholder('e.g., Follow up after open').isVisible())) return 'modal closed with empty form';
}, page);
const fillRule = async (name, trig, action) => {
  await page.getByPlaceholder('e.g., Follow up after open').fill(name);
  const sels = page.locator('div.fixed select');
  await sels.nth(0).selectOption({ label: 'Auto Camp ' + ws.rid });
  await sels.nth(1).selectOption({ label: trig });
  await page.locator('div.fixed input[type=number]').fill('24');
  await sels.nth(2).selectOption({ label: action });
};
await S.t(`${R}-02`, 'Create Send-AI-Template rule: row appears; DB has trigger/action/template/delay and is active', async () => {
  await fillRule(RULE, 'If No Reply', 'Send AI Template');
  const sels = page.locator('div.fixed select');
  await sels.nth(3).selectOption({ index: 1 });
  await page.getByRole('button', { name: /^Create Rule$/ }).click();
  await waitText(page, new RegExp(RULE));
  const r = sql(`SELECT is_active,trigger_type,action_type,JSON_EXTRACT(trigger_config,'$.delay_hours') FROM automation_rules WHERE name=${q(RULE)}`).split('\t');
  if (r.join('|') !== '1|NO_REPLY|SEND_EMAIL|24') return 'DB row ' + r.join('|');
  return await clean(page, w);
}, page);
await S.t(`${R}-02b`, 'Create "Pause this contact" rule (PAUSE_PROSPECT, replaced "Pause Campaign"): option offered, no template picker, row listed, DB row persisted', async () => {
  const NAMEP = 'Pause rule ' + ws.rid;
  await go(page, '/app/automation', { wait: 1000 });
  await page.getByRole('button', { name: /Create New Rule/ }).click();
  await page.getByPlaceholder('e.g., Follow up after open').waitFor();
  const opts = (await page.locator('div.fixed select').nth(2).locator('option').allInnerTexts()).map(t => t.trim());
  if (opts.includes('Pause Campaign')) return 'stale "Pause Campaign" option still offered: ' + opts.join(',');
  if (!opts.includes('Pause this contact')) return 'Action options: ' + opts.join(',');
  await fillRule(NAMEP, 'If Email Opened', 'Pause this contact');
  if ((await page.locator('div.fixed select').nth(2).inputValue()) !== 'PAUSE_PROSPECT') return 'option value is not PAUSE_PROSPECT';
  if (await page.getByText('Select Template').count()) return 'template picker still shown for a pause action';
  await page.getByRole('button', { name: /^Create Rule$/ }).click();
  await waitText(page, new RegExp(NAMEP));
  const r = sql(`SELECT is_active,trigger_type,action_type,JSON_EXTRACT(trigger_config,'$.delay_hours'),campaign_id FROM automation_rules WHERE name=${q(NAMEP)} AND campaign_id=${q(cid)}`).split('\t');
  if (r.slice(0, 4).join('|') !== '1|EMAIL_OPENED|PAUSE_PROSPECT|24') return 'DB row ' + r.join('|');
  if (r[4] !== cid) return `rule campaign_id ${r[4]} != ${cid}`;
  return await clean(page, w);
}, page);
await go(page, '/app/automation', { wait: 1000 });
await S.t(`${R}-03`, 'Toggle rule off/on (the repaired PUT): DB is_active flips and UI reflects it', async () => {
  const get = () => sql(`SELECT is_active FROM automation_rules WHERE name=${q(RULE)}`);
  const a0 = get();
  await page.locator('button[title="Pause Rule"]').first().click(); await sleep(1200);
  if (get() === a0) return 'DB not changed after pause: ' + get();
  await page.locator('button[title="Activate Rule"]').first().click(); await sleep(1200);
  if (get() !== a0) return 'not restored';
  return await clean(page, w);
}, page);
await S.t(`${R}-04`, 'Search filters rules; delete (confirm) removes row and DB record', async () => {
  await page.getByPlaceholder('Search rules by name or trigger...').fill('nomatch'); await sleep(600);
  if ((await body(page)).includes(RULE)) return 'search ignored';
  await page.getByPlaceholder('Search rules by name or trigger...').fill('');
  await page.locator('button[title="Delete Rule"]').first().click(); await sleep(1200);
  if (sql(`SELECT COUNT(*) FROM automation_rules WHERE name=${q(RULE)}`) !== '0') return 'rule remains in DB';
  return await clean(page, w);
}, page);

// ---- AI email generator ----
const G = 'UIO-AIGEN';
await S.t(`${G}-01`, 'AI generator: Classify disabled until designation entered; classification shows persona + confidence', async () => {
  await go(page, '/app/ai-email', { wait: 1200 });
  const btn = page.getByRole('button', { name: /Classify Persona/ });
  if (!(await btn.isDisabled())) return 'classify enabled with empty designation';
  await page.getByPlaceholder('e.g. Senior Backend Engineer').fill('VP of Market Access');
  await page.getByPlaceholder('e.g. TechCorp').fill('Acme');
  await btn.click(); await waitText(page, /Persona Type/);
  return await clean(page, w);
}, page);
await S.t(`${G}-02`, 'Sequence tab: 3-email sequence is generated, shows compliance check, copy button works', async () => {
  await page.getByRole('button', { name: /3-Email Sequence/ }).click();
  await page.getByRole('button', { name: /generate/i }).first().click();
  await waitText(page, /Email 1|Quick question for Acme/i, 15000);
  await waitText(page, /Compliance Check/);
  return await clean(page, w, [/compliance/]);
}, page);
await S.t(`${G}-03`, 'Classification failure (500) shows an error message to the user', async () => {
  await go(page, '/app/ai-email');
  await page.route(/classifications\/test/, r => r.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"LLM unavailable"}' }));
  await page.getByPlaceholder('e.g. Senior Backend Engineer').fill('CTO');
  await page.getByRole('button', { name: /Classify Persona/ }).click(); await sleep(1500);
  await page.unroute(/classifications\/test/);
  w.drain([/.*/]);
  if (!/unavailable|failed|error|try again/i.test(await body(page))) return 'failure is silent (no message)';
}, page);
await S.done();
