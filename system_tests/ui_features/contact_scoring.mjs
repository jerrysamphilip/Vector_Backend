// UIF-CS: contact scoring on fit and engagement (BRD v2.0 BR-CM-43 / CM-083)
//   Workspace -> Contact scoring settings (rules table add / delete, weights, Save, Recompute now),
//   the Score column with grade chip, sort and grade filter on Contacts, the Score card with its
//   breakdown on the contact record, and admin-only access to the settings.
// Uses the ui_crm helpers (own reusable tenant "cscore", wiped on every run).  node contact_scoring.mjs
import { ui, go, settle, tc, ok, eq, must, call, standalone, isMain, Tenant } from '../ui_crm/crm_common.mjs';

const TAG_MODEL = {
  fit_rules: [
    { id: 'hot', label: 'Hot tag', field: 'tags', operator: 'in', value: 'hot', points: 80 },
    { id: 'warm', label: 'Warm tag', field: 'tags', operator: 'in', value: 'warm', points: 40 },
  ],
  fit_weight: 100, engagement_weight: 0, half_life_days: 0, lookback_days: 180,
  grade_thresholds: { A: 75, B: 50, C: 25 },
};

export async function run(rec, browser) {
  const t = await (await Tenant.get('cscore')).init();
  const agent = await t.addUser('AGENT', 'Ari');
  await must(t.token, 'PUT', '/contacts/scoring-model', TAG_MODEL);
  const marker = `cs${t.rid}`;
  const mk = (first, tags) => must(t.token, 'POST', '/contacts', { email: `${first.toLowerCase()}.${marker}@cs.example`, first_name: first, last_name: marker, tags });
  const hot = await mk('Hotty', ['hot']);
  const warm = await mk('Warmy', ['warm']);
  await mk('Coldy', []);
  const model = () => must(t.token, 'GET', '/contacts/scoring-model');

  const A = await ui(browser, t.owner); const p = A.page;
  const names = async () => (await p.locator('tbody tr').allInnerTexts()).map(s => (s.match(/(Hotty|Warmy|Coldy)/) || [])[1]).filter(Boolean);

  await tc(rec, 'UIF-CS-01', 'Contacts list: Score column shows the grade chip and score for each contact', async () => {
    await go(A, `contacts?q=${marker}`);
    await p.getByRole('button', { name: /^Score/ }).waitFor({ timeout: 8000 });
    const hotRow = p.locator('tbody tr').filter({ hasText: 'Hotty' });
    const badge = hotRow.getByTestId('score-badge');
    eq(await badge.getAttribute('data-grade'), 'A', 'hot contact grade chip');
    ok((await badge.innerText()).includes('80'), 'hot contact score 80');
    eq(await p.locator('tbody tr').filter({ hasText: 'Coldy' }).getByTestId('score-badge').getAttribute('data-grade'), 'D', 'cold contact grade D');
  }, { u: A });

  await tc(rec, 'UIF-CS-02', 'Contacts list: clicking the Score header sorts by score (asc, then desc)', async () => {
    await go(A, `contacts?q=${marker}`);
    await p.getByRole('button', { name: /^Score/ }).click(); await settle(A, 700);
    eq(await names(), ['Coldy', 'Warmy', 'Hotty'], 'ascending');
    await p.getByRole('button', { name: /^Score/ }).click(); await settle(A, 700);
    eq(await names(), ['Hotty', 'Warmy', 'Coldy'], 'descending');
    ok(p.url().includes('sort_by=score'), 'sort kept in the URL');
  }, { u: A });

  await tc(rec, 'UIF-CS-03', 'Contacts list: grade filter and minimum score narrow the list', async () => {
    await go(A, `contacts?q=${marker}`);
    await p.getByTestId('grade-filter').selectOption('A'); await settle(A, 700);
    eq(await names(), ['Hotty'], 'grade A only');
    await p.getByTestId('grade-filter').selectOption(''); await settle(A, 300);
    await p.getByTestId('score-min-filter').fill('30'); await settle(A, 900);
    eq((await names()).sort(), ['Hotty', 'Warmy'], 'score >= 30');
  }, { u: A });

  await tc(rec, 'UIF-CS-04', 'Contact record: Score card shows score, fit / engagement and the rules that contributed', async () => {
    await go(A, `contacts/${warm.prospect_id}`);
    const card = p.getByTestId('contact-score-card');
    await card.waitFor({ timeout: 8000 });
    eq(await card.getByTestId('score-badge').getAttribute('data-grade'), 'C', 'grade C');
    eq(await card.getByTestId('score-fit').innerText(), '40', 'fit 40');
    await card.getByTestId('score-breakdown-toggle').click();
    const rule = card.getByTestId('score-fit-rule');
    eq(await rule.count(), 1, 'one matched rule');
    ok((await rule.innerText()).includes('Warm tag') && (await rule.innerText()).includes('+40'), 'rule label and points');
    await card.getByTestId('score-recompute').click(); await settle(A, 700);
    ok(await card.getByText(/Saved (Today|Yesterday)/).isVisible(), 'saved time shown');
  }, { u: A });

  await tc(rec, 'UIF-CS-05', 'Settings: admin adds a fit rule (Tags is any of "vip", +50) and saves; the model and scores follow', async () => {
    await go(A, 'contact-scoring');
    const page = p.getByTestId('scoring-settings');
    await page.waitFor({ timeout: 8000 });
    eq(await p.getByTestId('scoring-rule-row').count(), 2, 'two rules loaded');
    await p.getByTestId('scoring-add-rule').click();
    const row = p.getByTestId('scoring-rule-row').last();
    await row.getByLabel('Rule label').fill('VIP tag');
    await row.getByLabel('Rule property').selectOption('tags');
    await row.getByLabel('Rule condition').selectOption('in');
    await row.getByLabel('Rule value').fill('vip');
    await row.getByLabel('Rule points').fill('50');
    await p.getByTestId('scoring-save').click(); await settle(A, 1200);
    await p.getByTestId('scoring-notice').waitFor({ timeout: 8000 });
    const m = await model();
    const vip = m.model.fit_rules.find(r => r.label === 'VIP tag');
    ok(vip && vip.field === 'tags' && vip.operator === 'in' && vip.value === 'vip' && vip.points === 50, `rule saved: ${JSON.stringify(vip)}`);
    const c = await must(t.token, 'POST', '/contacts', { email: `vip.${marker}@cs.example`, first_name: 'Vippy', tags: ['vip'] });
    eq(c.score, 50, 'new contact scored with the new rule');
  }, { u: A });

  await tc(rec, 'UIF-CS-06', 'Settings: delete a rule, change weights and thresholds; invalid weights show an error and save nothing', async () => {
    await go(A, 'contact-scoring');
    await p.getByTestId('scoring-settings').waitFor({ timeout: 8000 });
    const rows = p.getByTestId('scoring-rule-row');
    let vipRow = -1;
    for (let i = 0; i < await rows.count(); i++) if (await rows.nth(i).getByLabel('Rule label').inputValue() === 'VIP tag') vipRow = i;
    ok(vipRow >= 0, 'VIP rule listed');
    await rows.nth(vipRow).getByTestId('scoring-delete-rule').click();
    eq(await p.getByTestId('scoring-rule-row').count(), 2, 'row removed');
    await p.getByLabel('Grade A threshold').fill('70');
    await p.getByLabel('Fit weight').fill('80'); // engagement follows to 20
    eq(await p.getByLabel('Engagement weight').inputValue(), '20', 'weights add up to 100');
    await p.getByTestId('scoring-save').click(); await settle(A, 1200);
    const m = await model();
    eq([m.model.fit_rules.length, m.model.grade_thresholds.A, m.model.fit_weight, m.model.engagement_weight], [2, 70, 80, 20], 'saved');
    await p.getByLabel('Grade A threshold').fill('10'); // below B: refused
    await p.getByTestId('scoring-save').click(); await settle(A, 900);
    await p.getByText(/Grade thresholds must decrease/).waitFor({ timeout: 8000 });
    eq((await model()).model.grade_thresholds.A, 70, 'nothing saved');
  }, { u: A, allow: [/400/] });

  await tc(rec, 'UIF-CS-07', 'Settings: Recompute now queues a background recalculation that rescores every contact', async () => {
    await go(A, 'contact-scoring');
    await p.getByTestId('scoring-recompute').click(); await settle(A, 900);
    await p.getByTestId('scoring-notice').filter({ hasText: /Recalculating/ }).waitFor({ timeout: 8000 });
    let m; for (let i = 0; i < 30; i++) { m = await model(); if (!m.recompute_requested_at && m.last_recomputed_at) break; await p.waitForTimeout(1000); }
    ok(!m.recompute_requested_at && m.last_recompute_count >= 4, `recomputed: ${JSON.stringify({ r: m.recompute_requested_at, n: m.last_recompute_count })}`);
    eq(m.stats.scored, m.stats.contacts, 'every contact scored');
  }, { u: A });

  const G = await ui(browser, agent); const pg = G.page;
  await tc(rec, 'UIF-CS-08', 'Agent: no Contact scoring link in the sidebar; the page says admins only; own contacts still show a score', async () => {
    await go(G, 'dashboard');
    eq(await pg.getByRole('link', { name: 'Contact scoring' }).count(), 0, 'no sidebar link');
    await go(G, 'contact-scoring');
    await pg.getByText('Only workspace admins can change the scoring model.').waitFor({ timeout: 8000 });
    const own = await must(await t.tokenOf(agent), 'POST', '/contacts', { email: `own.${marker}@cs.example`, first_name: 'Owny', tags: ['hot'] });
    await go(G, `contacts/${own.prospect_id}`);
    await pg.getByTestId('contact-score-card').getByTestId('score-badge').waitFor({ timeout: 8000 });
    eq((await call(await t.tokenOf(agent), 'GET', `/contacts/${hot.prospect_id}/score`)).status, 404, "others' scores hidden");
  }, { u: G, allow: [/403/, /404/] });

  await A.ctx.close(); await G.ctx.close();
}
if (isMain(import.meta)) standalone('contact_scoring', run);
