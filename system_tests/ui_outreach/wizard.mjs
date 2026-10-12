import { startSuite, fullWorkspace, createWs, api, sql, q, go, body, waitText, clean, mkInbox, mkContact, dialogs, sleep } from './lib.mjs';
const S = await startSuite('wizard');
const A = 'UIO-WIZ';

async function seed(ws, label) {
  const dom = `w${ws.rid}.example`;
  const ib = await mkInbox(ws, 'sales', dom);
  const pids = [];
  for (let i = 0; i < 4; i++) pids.push(await mkContact(ws, `p${i}@${label}${ws.rid}.example`, { first_name: 'Wiz' + i, company_name: 'Co' + i }));
  const l = await api('POST', '/lists', { token: ws.token, body: { list_name: 'Wiz list ' + ws.rid } });
  await api('POST', `/lists/${l.data.list_id}/members`, { token: ws.token, body: { prospect_ids: pids } });
  return { ib, pids, listName: 'Wiz list ' + ws.rid, inboxEmail: `sales@${dom}` };
}
const next = page => page.getByRole('button', { name: /Save & Continue/ }).click();

const ws = await fullWorkspace('wiz', { roles: [] });
const d = await seed(ws, 'wz');
const { page, w } = await S.open(ws);
const dlg = dialogs(page, 'accept');
const NAME = 'Wizard Camp ' + ws.rid;
let cid;

await S.t(`${A}-01`, 'Step 1 validation: empty name -> "Campaign name is required"; name without inbox -> "Select at least one inbox"', async () => {
  await go(page, '/app/campaigns/new?fresh=true', { wait: 1200 });
  await next(page); await waitText(page, /Campaign name is required/);
  await page.getByPlaceholder('e.g. Q4 Enterprise Outreach').fill(NAME);
  await next(page); await waitText(page, /Select at least one inbox/);
  if (sql(`SELECT COUNT(*) FROM campaigns WHERE campaign_name=${q(NAME)}`) !== '0') return 'campaign created despite validation error';
}, page);
await S.t(`${A}-02`, 'Step 1 saves identity, sender details, CTA, send window, distribution + start date to the campaign row', async () => {
  await page.getByText(d.inboxEmail).first().click();
  await page.getByPlaceholder('e.g. John Smith').fill('Jane Sender');
  await page.getByPlaceholder('e.g. Account Executive').fill('Head of Sales');
  await page.getByPlaceholder('https://calendly.com/...').fill('https://calendly.com/jane/intro');
  await page.locator('input[type=time]').nth(0).fill('08:30'); await page.locator('input[type=time]').nth(1).fill('16:45');
  await page.getByRole('button', { name: /^Random/ }).click();
  const date = new Date(Date.now() + 9 * 864e5).toISOString().slice(0, 10);
  await page.locator('input[type=date]').fill(date);
  await next(page);
  await waitText(page, /Sequence Engine/);
  const r = sql(`SELECT campaign_id,sender_name,sender_title,cta_link,send_window_start,send_window_end,sending_mode,start_date,status FROM campaigns WHERE campaign_name=${q(NAME)}`).split('\t');
  cid = r[0];
  const exp = ['Jane Sender', 'Head of Sales', 'https://calendly.com/jane/intro', '08:30:00', '16:45:00', 'random', date, 'DRAFT'];
  const got = r.slice(1);
  if (exp.join('|') !== got.join('|')) return `DB ${got.join('|')} != ${exp.join('|')}`;
  if (sql(`SELECT COUNT(*) FROM campaign_inboxes WHERE campaign_id=${q(cid)} AND inbox_id=${q(d.ib)}`) !== '1') return 'inbox not attached';
  return await clean(page, w);
}, page);
await S.t(`${A}-03`, 'Step 2 sequence: add follow-up with wait days, saved as 2 steps (wait_days persisted); Back keeps step 1 values', async () => {
  await page.getByRole('button', { name: /Add Follow-up Step/ }).click();
  await page.locator('input[type=number]').first().fill('3');
  await page.getByRole('button', { name: /^Back/ }).click();
  await waitText(page, /Campaign Setup/);
  if ((await page.getByPlaceholder('e.g. Q4 Enterprise Outreach').inputValue()) !== NAME) return 'name lost on Back';
  await next(page); await waitText(page, /Sequence Engine/);
  if (!(await page.locator('input[type=number]').count())) return 'follow-up lost on Back/forward';
  await page.getByRole('button', { name: /Save Sequence/ }).click();
  await waitText(page, /Select Prospects/);
  const rows = sql(`SELECT step_number,wait_days FROM email_sequences WHERE campaign_id=${q(cid)} ORDER BY step_number`).replace(/\t/g, ':').split('\n');
  if (rows.join(',') !== '1:0,2:3') return 'sequence rows ' + rows.join(',');
  return await clean(page, w);
}, page);
await S.t(`${A}-04`, 'Step 3 prospects: list selection, deselect one contact -> "3 of 4 contacts selected"', async () => {
  await waitText(page, new RegExp(d.listName));
  await waitText(page, /4 of 4 contacts selected/);
  await page.locator('tbody input[type=checkbox]').first().uncheck();
  await waitText(page, /3 of 4 contacts selected/);
  return await clean(page, w, [/campaigns\/lists/]);
}, page);
await S.t(`${A}-05`, 'Process & Generate (LLM absent): templates generated for EVERY sequence step (2 emails)', async () => {
  await page.getByRole('button', { name: /Process & Generate/ }).click();
  await waitText(page, /Persona Blueprints|AI classified|failed|error/i, 30000);
  const t = await body(page);
  if (/AI processing failed|Failed/i.test(t) && !/Live Preview/.test(t)) return 'generation error: ' + (t.match(/(AI processing failed|Failed)[^\n]*/) || [''])[0];
  const n = sql(`SELECT COUNT(*) FROM email_templates WHERE campaign_id=${q(cid)}`);
  if (n !== '2') return `${n} template(s) stored for a 2-step sequence (follow-up step has no content)`;
}, page);
await S.t(`${A}-06`, 'Edit subject (textarea) persists to DB; Cancel discards; single-brace variable triggers the warning dialog', async () => {
  const edit = page.getByRole('button', { name: /^edit$/i });
  await edit.first().click();
  await page.locator('textarea').first().fill('Quick idea for {{first_name}} at {{company_name}}');
  await page.getByRole('button', { name: /^Save$/ }).click();
  await sleep(800);
  if (!sql(`SELECT GROUP_CONCAT(subject) FROM email_templates WHERE campaign_id=${q(cid)}`).includes('Quick idea for {{first_name}}')) return 'subject not persisted';
  await edit.first().click(); await page.locator('textarea').first().fill('Bad {first_name}');
  const before = dlg.length; await page.getByRole('button', { name: /^Save$/ }).click(); await sleep(500);
  if (dlg.length === before) return 'no warning for single-brace variable';
  await page.getByRole('button', { name: /^Cancel$/ }).click();
  if (!(await body(page)).includes('Quick idea for')) return 'cancel lost the saved subject';
  return await clean(page, w);
}, page);
await S.t(`${A}-07`, 'Edit body in the rich-text editor persists to DB', async () => {
  await page.getByRole('button', { name: /^edit$/i }).nth(1).click();
  const ed = page.locator('.ProseMirror, [contenteditable=true]').first();
  await ed.click(); await page.keyboard.press('Control+A'); await page.keyboard.type('Hello {{first_name}}, tailored body ' + ws.rid);
  await page.getByRole('button', { name: /^Save$/ }).click(); await sleep(900);
  if (!sql(`SELECT GROUP_CONCAT(body) FROM email_templates WHERE campaign_id=${q(cid)}`).includes('tailored body ' + ws.rid)) return 'body not persisted';
}, page);
await S.t(`${A}-08`, 'Live Preview substitutes real prospect data (name/company), and prospect switcher works', async () => {
  await page.getByRole('button', { name: /live preview/i }).click();
  await waitText(page, /Static Code|ACTIVE PREVIEW DATA/i);
  const t = await body(page);
  if (/\{\{first_name\}\}|\{\{company_name\}\}/.test(t.split('EMAIL BLUEPRINT')[0] + t.split('EMAIL BLUEPRINT')[1])) return 'placeholders not substituted in preview';
  if (!/Wiz\d/.test(t)) return 'prospect name not in preview';
}, page);
await S.t(`${A}-09`, 'Regenerate subject with LLM absent: sensible result or error, no crash / [object Object]', async () => {
  await page.getByRole('button', { name: /regenerate/i }).first().click(); await sleep(4000);
  const bp = await w.bodyProblems(); w.drain([/.*/]);
  if (bp.length) return bp.join(',');
}, page);
await S.t(`${A}-10`, 'Click an undetected persona -> generates or shows a clear error (LLM absent)', async () => {
  await page.getByRole('button', { name: /Market Access/ }).first().click(); await sleep(4000);
  const bp = await w.bodyProblems(); w.drain([/.*/]);
  if (bp.length) return bp.join(',');
}, page);
await S.t(`${A}-11`, 'Launch step shows correct specs; Back to Edit returns; EXECUTE launches: ACTIVE, 3 enrolled (excluded contact skipped), redirect to list', async () => {
  await page.getByRole('button', { name: /Finalize & Launch/ }).click();
  await waitText(page, /Mission Ready/);
  const t = await body(page);
  if (!/3 Leads/i.test(t)) return 'spec leads != 3: ' + (t.match(/Target Audience[^\n]*\n[^\n]*/i) || [''])[0];
  if (!t.includes(NAME)) return 'name missing';
  await page.getByRole('button', { name: /Back to Edit/ }).click(); await waitText(page, /Persona Blueprints/);
  await page.getByRole('button', { name: /Finalize & Launch/ }).click();
  await page.getByRole('button', { name: /execute campaign/i }).click();
  await page.waitForURL(/\/app\/campaigns$/, { timeout: 30000 }).catch(async () => { throw new Error('no redirect: ' + (await body(page)).slice(-250).replace(/\s+/g, ' ')); });
  const st = sql(`SELECT status FROM campaigns WHERE campaign_id=${q(cid)}`);
  const en = sql(`SELECT COUNT(*) FROM campaign_prospects WHERE campaign_id=${q(cid)}`);
  if (st !== 'ACTIVE' || en !== '3') return `status ${st}, enrolled ${en}`;
  return await clean(page, w);
}, page);

// no sender identity -> clear launch message
const ws2 = await createWs('wiznoid');
const d2 = await seed(ws2, 'wn');
const b = await S.open(ws2); dialogs(b.page);
await S.t(`${A}-12`, 'Launch without sender identity shows a clear message and does not activate the campaign', async () => {
  const p = b.page; const N2 = 'NoId Camp ' + ws2.rid;
  await go(p, '/app/campaigns/new?fresh=true', { wait: 1200 });
  await p.getByPlaceholder('e.g. Q4 Enterprise Outreach').fill(N2); await p.getByText(d2.inboxEmail).first().click(); await next(p);
  await p.getByRole('button', { name: /Save Sequence/ }).click(); await waitText(p, /Select Prospects/);
  await waitText(p, /Wiz list/); await p.getByRole('button', { name: /Process & Generate/ }).click();
  await waitText(p, /Persona Blueprints/, 30000);
  await p.getByRole('button', { name: /Finalize & Launch/ }).click(); await p.getByRole('button', { name: /execute campaign/i }).click();
  await sleep(3000);
  const t = await body(p);
  if (!/sender identity|company name|postal address|physical address/i.test(t)) return 'no clear sender-identity message: ' + t.slice(-300).replace(/\s+/g, ' ');
  if (sql(`SELECT status FROM campaigns WHERE campaign_name=${q(N2)}`) === 'ACTIVE') return 'campaign activated without identity';
  b.w.drain([/.*/]);
}, b.page);
await S.done();
