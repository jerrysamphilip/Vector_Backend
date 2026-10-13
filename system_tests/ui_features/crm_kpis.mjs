// UIF-KPI: the BRD v2.0 section 8 CRM KPIs in the UI
//   Sales reports -> "CRM KPIs" tab (six tiles with value, numerator / denominator, the < 1% duplicate target,
//   definitions behind the info button, the 9-week adoption bars), the Forecast tab's forecast-accuracy card on the
//   new formula (actual / committed), the Sales head dashboard card, workspace KPIs hidden from agents, the help article.
// Uses the ui_crm helpers (own reusable tenant "crmkpi", wiped on every run).  node crm_kpis.mjs
import { ui, go, settle, tc, ok, eq, must, sql, standalone, isMain, Tenant } from '../ui_crm/crm_common.mjs';

const pad = n => String(n).padStart(2, '0');
const iso = d => `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
const addMonths = (d, n) => new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + n, 1));

export async function run(rec, browser) {
  const t = await (await Tenant.get('crmkpi')).init();
  const agent = await t.addUser('AGENT', 'Kip', { sales_level: 3 });
  const tok = t.token;
  const tid = t.tenantId;

  // Dataset: 2 of 4 contacts are duplicates (same name at the same company) -> 50 %
  const mk = (local, body) => must(tok, 'POST', '/contacts', { email: `${local}.${t.rid}@kpi-ui.example`, ...body });
  await mk('dana1', { first_name: 'Dana', last_name: `Dup${t.rid}`, company_name: 'Dupe Co' });
  await mk('dana2', { first_name: 'Dana', last_name: `Dup${t.rid}`, company_name: 'Dupe Co' });
  await mk('solo1', { first_name: 'Solo', last_name: 'One' });
  await mk('solo2', { first_name: 'Solo', last_name: 'Two' });
  // Forecast: 1000 committed at the start of the last finished quarter, 800 won in it -> 80 %
  const stages = await must(tok, 'GET', '/sales/stages');
  const won = stages.find(s => s.is_won);
  const { fiscal_year_start_month: fyStart } = await must(tok, 'GET', '/sales-reports/forecast');
  const now = new Date();
  const m0 = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1);
  const qIndex = Math.floor(((now.getUTCMonth() + 1 - fyStart + 12) % 12) / 3);
  const fyYear = now.getUTCMonth() + 1 >= fyStart ? now.getUTCFullYear() : now.getUTCFullYear() - 1;
  const curQ = addMonths(new Date(Date.UTC(fyYear, fyStart - 1, 1)), 3 * qIndex);
  const lastQ = addMonths(curQ, -3);
  ok(m0 >= curQ.getTime(), 'quarter maths');
  sql(`INSERT INTO forecast_snapshots (snapshot_id, tenant_id, period_type, period_start, period_end, owner_id, won, weighted, forecast, open_count, commit_open, committed, source, taken_at)
       VALUES (UUID(), '${tid}', 'QUARTER', '${iso(lastQ)}', '${iso(curQ)}', '${t.owner.userId}', 0, 500, 500, 1, 1000, 1000, 'SCHEDULED', '${iso(lastQ)} 00:00:00')`);
  const closeDay = new Date(lastQ.getTime() + 10 * 86400000);
  await must(tok, 'POST', '/opportunities', { name: `Won ${t.rid}`, owner_id: t.owner.userId, stage_id: won.stage_id, amount: '800', close_date: iso(closeDay), closed_reason: 'Price' });
  // Adoption: the agent was active last week
  const monday = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - ((now.getUTCDay() + 6) % 7)));
  sql(`INSERT IGNORE INTO user_activity_days (tenant_id, user_id, day) VALUES ('${tid}', '${agent.userId}', '${iso(new Date(monday.getTime() - 7 * 86400000))}')`);

  const A = await ui(browser, t.owner); const p = A.page;
  const tile = key => p.getByTestId(`kpi-${key}`);
  const api = () => must(tok, 'GET', '/sales-reports/kpis');

  await tc(rec, 'UIF-KPI-01', 'Sales reports -> CRM KPIs: six tiles in BRD order; duplicate rate 50% of 4 contacts, target < 1%, off target', async () => {
    await go(A, 'sales-reports?tab=kpis');
    await p.getByTestId('crm-kpis').waitFor({ timeout: 10000 });
    await tile('duplicate_contact_rate').getByTestId('kpi-value').waitFor({ timeout: 10000 });
    const keys = await p.locator('[data-testid^="kpi-"][data-available]').evaluateAll(els => els.map(e => e.dataset.testid.slice(4)));
    eq(keys, ['duplicate_contact_rate', 'reply_to_lead', 'lead_to_sql', 'sql_to_win', 'forecast_accuracy', 'crm_adoption'], 'tiles');
    const d = tile('duplicate_contact_rate');
    eq(await d.getByTestId('kpi-value').innerText(), '50%', 'duplicate value');
    ok((await d.getByTestId('kpi-sub').innerText()).startsWith('2 duplicate contacts ÷ 4 live contacts'), 'numerator / denominator');
    const target = await d.getByTestId('kpi-target').innerText();
    ok(target.includes('Target < 1%') && target.includes('off target'), `target: ${target}`);
  }, { u: A });

  await tc(rec, 'UIF-KPI-02', 'The info button shows each KPI\'s formula and definition', async () => {
    const f = tile('forecast_accuracy');
    eq(await f.getByTestId('kpi-definition').count(), 0, 'hidden at first');
    ok((await f.getByTestId('kpi-info').getAttribute('title')).includes('committed forecast'), 'tooltip text');
    await f.getByTestId('kpi-info').click();
    const def = await f.getByTestId('kpi-definition').innerText();
    ok(def.includes('actual revenue ÷ committed forecast × 100, per quarter') && def.includes('Commit forecast category'), def);
    eq(await f.getByTestId('kpi-info').getAttribute('aria-expanded'), 'true', 'aria-expanded');
  }, { u: A });

  await tc(rec, 'UIF-KPI-03', 'Forecast accuracy 80% (800 won / 1000 committed); adoption matches the API with 9 weekly bars; values equal the API', async () => {
    await go(A, 'sales-reports?tab=kpis');
    await tile('forecast_accuracy').getByTestId('kpi-value').waitFor({ timeout: 10000 });
    eq(await tile('forecast_accuracy').getByTestId('kpi-value').innerText(), '80%', 'forecast accuracy');
    ok((await tile('forecast_accuracy').getByTestId('kpi-sub').innerText()).includes('revenue won'), 'forecast sub');
    const k = Object.fromEntries((await api()).kpis.map(x => [x.key, x]));
    eq(k.forecast_accuracy.value, 80, 'api forecast');
    for (const key of Object.keys(k)) {
      const shown = await tile(key).getByTestId('kpi-value').innerText();
      eq(shown, k[key].value == null ? '—' : `${k[key].value}%`, `${key} shown`);
    }
    const seats = Number(sql(`SELECT COUNT(*) FROM users WHERE tenant_id='${tid}' AND status='ACTIVE'`));
    const active = Number(sql(`SELECT COUNT(DISTINCT user_id) FROM user_activity_days WHERE tenant_id='${tid}' AND day >= '${iso(monday)}'`));
    eq([k.crm_adoption.numerator, k.crm_adoption.denominator], [active, seats], 'adoption from SQL');
    eq(await tile('crm_adoption').getByTestId('kpi-adoption-trend').locator('span[title]').count(), 9, 'nine weekly bars');
    ok((await tile('crm_adoption').getByTestId('kpi-adoption-trend').locator('span[title]').nth(7).getAttribute('title')).includes('1 of'), 'last week: the agent');
  }, { u: A });

  await tc(rec, 'UIF-KPI-04', 'Forecast tab: the forecast-accuracy card explains actual ÷ committed and shows 80% for the last quarter', async () => {
    await go(A, 'sales-reports?tab=forecast');
    const card = p.getByTestId('forecast-accuracy');
    await card.waitFor({ timeout: 10000 });
    ok((await card.getByTestId('forecast-accuracy-help').innerText()).includes('actual ÷ committed × 100'), 'help text');
    const row = card.getByTestId('forecast-accuracy-row').filter({ hasText: iso(lastQ) });
    await row.waitFor({ timeout: 8000 });
    eq(await row.getByTestId('forecast-accuracy-value').innerText(), '80%', 'row accuracy');
    ok((await row.innerText()).includes('1,000'), 'committed shown');
  }, { u: A });

  await tc(rec, 'UIF-KPI-05', 'Sales head dashboard shows the CRM KPIs card', async () => {
    await go(A, 'sales');
    await p.getByTestId('crm-kpis').waitFor({ timeout: 10000 });
    await tile('duplicate_contact_rate').getByTestId('kpi-value').waitFor({ timeout: 10000 });
    eq(await tile('duplicate_contact_rate').getByTestId('kpi-value').innerText(), '50%', 'dashboard duplicate rate');
  }, { u: A });

  await tc(rec, 'UIF-KPI-06', 'Help: "CRM KPIs explained" defines all six KPIs', async () => {
    await go(A, 'help/crm-kpis');
    await p.getByRole('heading', { name: 'CRM KPIs explained' }).first().waitFor({ timeout: 8000 });
    const body = await p.locator('article').innerText();
    for (const term of ['Duplicate contact rate', 'Reply-to-lead conversion', 'Lead-to-SQL conversion', 'SQL-to-win rate', 'Forecast accuracy', 'CRM adoption', 'below 1%'])
      ok(body.includes(term), `help mentions ${term}`);
  }, { u: A });

  const G = await ui(browser, agent); const pg = G.page;
  await tc(rec, 'UIF-KPI-07', 'Agent: duplicate rate and adoption are workspace-level (shown as —, with a note); team KPIs are shown', async () => {
    await go(G, 'sales-reports?tab=kpis');
    await pg.getByTestId('crm-kpis').waitFor({ timeout: 10000 });
    const d = pg.getByTestId('kpi-duplicate_contact_rate');
    await d.getByTestId('kpi-value').waitFor({ timeout: 10000 });
    eq(await d.getAttribute('data-available'), 'false', 'duplicate unavailable');
    eq(await d.getByTestId('kpi-value').innerText(), '—', 'no value');
    ok((await d.getByTestId('kpi-note').innerText()).includes('Workspace-level'), 'note');
    eq(await pg.getByTestId('kpi-crm_adoption').getAttribute('data-available'), 'false', 'adoption unavailable');
    eq(await pg.getByTestId('kpi-lead_to_sql').getAttribute('data-available'), 'true', 'lead to SQL available');
  }, { u: G });

  await A.ctx.close(); await G.ctx.close();
}
if (isMain(import.meta)) standalone('crm_kpis', run);
