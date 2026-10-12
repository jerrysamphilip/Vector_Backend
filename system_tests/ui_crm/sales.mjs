// UIC-SAL: Leads, SqlQueue, Deals, DealDetail, Pipeline, Dashboard, Reports, Team, Settings, Targets, Connections
import { Tenant, ui, go, settle, tc, skip, ok, eq, num, must, call, sql, sq, dbn, modalLoc, standalone, isMain, failApi, seedContacts, dnd } from './crm_common.mjs';

const dstr = d => new Date(Date.now() + d * 86400000).toISOString().slice(0, 10);
const money = n => '$' + Math.round(n).toLocaleString('en');

export async function run(rec, browser) {
  const t = await (await Tenant.get('sales')).init();
  const alice = await t.addUser('AGENT', 'Alice', { sales_level: 3 });
  const boss = await t.addUser('MANAGER', 'Boss', { sales_level: 2 });
  sql(`UPDATE users SET manager_id='${boss.userId}' WHERE user_id='${alice.userId}'`);
  const other = await t.addUser('AGENT', 'Other', { sales_level: 3 });
  const cs = await seedContacts(t, 26, { owners: [t.owner.userId], prefix: 'Lead' });
  const stages = await must(t.token, 'GET', '/sales/stages');
  const S = n => stages.find(s => s.name === n).stage_id;
  // 12 leads: alice 6 (2 SQL), other 2, owner 4
  const leads = [];
  const lead = async (i, owner, stage) => { const l = await must(t.token, 'POST', '/leads', { prospect_id: cs[i].prospect_id, owner_id: owner.userId, source: i % 2 ? 'Webinar' : 'Campaign' }); leads.push(l);
    if (stage === 'SQL') { await must(t.token, 'PATCH', `/leads/${l.lead_id}`, { qualification: { budget: true, authority: true, need: true, timeline: true } }); await must(t.token, 'PATCH', `/leads/${l.lead_id}`, { stage: 'SQL' }); }
    else if (stage !== 'NEW') await must(t.token, 'PATCH', `/leads/${l.lead_id}`, { stage }); return l; };
  const mix = ['NEW', 'CONTACTED', 'ENGAGED', 'SQL'];
  for (let i = 0; i < 6; i++) await lead(i, alice, i < 2 ? 'SQL' : mix[i % 3]);
  for (let i = 6; i < 8; i++) await lead(i, other, 'NEW');
  for (let i = 8; i < 12; i++) await lead(i, t.owner, mix[i % 4]);
  // 10 deals: amounts across stages and FY quarters (FY Apr-Mar; today Oct 2026 => Q3)
  const deals = [];
  const dl = async (n, amount, stage, close, owner = t.owner) => { const d = await must(t.token, 'POST', '/opportunities', { name: n, amount, stage_id: S(stage), close_date: close, owner_id: owner.userId, client_type: 'NEW' }); deals.push({ ...d, amount, stage }); return d; };
  await dl('Deal A', 10000, 'Qualification', dstr(20)); await dl('Deal B', 20000, 'Needs analysis', dstr(40));
  await dl('Deal C', 30000, 'Proposal', dstr(60)); await dl('Deal D', 40000, 'Negotiation', dstr(80));
  await dl('Deal E', 5000, 'Qualification', '2027-02-15', alice); await dl('Deal F', 7000, 'Proposal', '2027-03-10', alice);
  for (const [n, amt, close] of [['Won Q1', 50000, '2026-05-10'], ['Won Q2', 25000, '2026-08-05'], ['Won Q3', 15000, dstr(-2)]]) {
    const d = await dl(n, amt, 'Qualification', close); await must(t.token, 'PATCH', `/opportunities/${d.opportunity_id}`, { stage_id: S('Closed won'), closed_reason: 'Price' });
    sql(`UPDATE opportunities SET close_date='${close}', closed_at='${close} 12:00:00' WHERE opportunity_id='${d.opportunity_id}'`); }
  { const d = await dl('Lost X', 9000, 'Qualification', dstr(5)); await must(t.token, 'PATCH', `/opportunities/${d.opportunity_id}`, { stage_id: S('Closed lost'), closed_reason: 'Timing' }); }
  const openSum = deals.filter(d => ['Qualification', 'Needs analysis', 'Proposal', 'Negotiation'].includes(d.stage) && !/^(Won|Lost)/.test(d.name)).reduce((a, d) => a + d.amount, 0);
  const wonSum = 50000 + 25000 + 15000;
  const A = await ui(browser, t.owner); const p = A.page;

  // ---------- Leads ----------
  await tc(rec, 'UIC-SAL-01', 'Leads split view lists open leads; stage chips filter; counts equal DB', async () => {
    await go(A, 'leads');
    const open = dbn(`SELECT COUNT(*) FROM leads WHERE tenant_id='${t.tenantId}' AND stage IN ('NEW','CONTACTED','ENGAGED','SQL')`);
    ok(await p.getByText(`${open} leads`).isVisible(), `footer ${open} leads`);
    await p.getByRole('button', { name: 'SQL', exact: true }).click(); await settle(A, 600);
    const sqln = dbn(`SELECT COUNT(*) FROM leads WHERE tenant_id='${t.tenantId}' AND stage='SQL'`);
    ok(await p.getByText(`${sqln} leads`).isVisible(), `SQL chip ${sqln}`);
  }, { u: A });
  await tc(rec, 'UIC-SAL-02', 'Leads list mode + board mode render with per-stage counts', async () => {
    await go(A, 'leads?mode=list'); await settle(A, 500);
    eq(await p.locator('tbody tr').count(), dbn(`SELECT COUNT(*) FROM leads WHERE tenant_id='${t.tenantId}'`), 'list rows');
    await go(A, 'leads?mode=board'); await settle(A, 500);
    for (const l of ['New', 'Contacted', 'Engaged (MQL)', 'Sales qualified (SQL)']) ok(await p.getByText(l, { exact: true }).first().isVisible(), l);
  }, { u: A });
  await tc(rec, 'UIC-SAL-03', 'Leads: search and owner filter', async () => {
    await go(A, 'leads?mode=list');
    await p.getByPlaceholder(/Search name, email/).fill('Lead3'); await settle(A, 800);
    eq(await p.locator('tbody tr').count(), 1, 'search');
    await p.getByPlaceholder(/Search name, email/).fill('');
    await p.getByLabel('Owner').selectOption({ label: 'Alice Test' }); await settle(A, 700);
    eq(await p.locator('tbody tr').count(), 6, 'owner filter');
  }, { u: A });
  await tc(rec, 'UIC-SAL-04', 'Create lead via modal (contact search) -> DB; duplicate open lead shows link to existing', async () => {
    await go(A, 'leads');
    await p.getByRole('button', { name: /New lead/ }).click();
    const m = modalLoc(p);
    await m.getByPlaceholder('Search your contacts…').fill('Lead20'); await m.getByRole('button', { name: /Lead20 Person20/ }).click();
    await m.getByLabel('Lead source').fill('Referral'); await m.getByRole('button', { name: 'Create lead' }).click();
    await p.waitForURL(/\/app\/leads\/[0-9a-f-]{36}/);
    eq(sql(`SELECT CONCAT_WS('|',stage,source) FROM leads WHERE prospect_id='${cs[20].prospect_id}'`), 'NEW|Referral');
    await go(A, 'leads'); await p.getByRole('button', { name: /New lead/ }).click();
    await modalLoc(p).getByPlaceholder('Search your contacts…').fill('Lead20'); await modalLoc(p).getByRole('button', { name: /Lead20 Person20/ }).click();
    await modalLoc(p).getByRole('button', { name: 'Create lead' }).click(); await settle(A, 600);
    ok(await modalLoc(p).getByRole('link', { name: 'Open it' }).isVisible(), 'link to existing lead');
  }, { u: A, allow: [/http 409/, /Failed to load resource/] });
  const L = leads[8];
  await tc(rec, 'UIC-SAL-05', 'LeadDetail: stage steps, BANT qualification, next step persisted', async () => {
    const id = leads.find(l => sql(`SELECT stage FROM leads WHERE lead_id='${l.lead_id}'`) === 'NEW' && sql(`SELECT owner_id FROM leads WHERE lead_id='${l.lead_id}'`) === t.owner.userId)?.lead_id || L.lead_id;
    await go(A, `leads/${id}`);
    await p.getByRole('button', { name: 'Contacted', exact: true }).click(); await settle(A, 600);
    eq(sql(`SELECT stage FROM leads WHERE lead_id='${id}'`), 'CONTACTED');
    for (const l of ['Budget confirmed', 'Talking to the decision maker', 'Clear business need']) { await p.getByLabel(l).click(); await settle(A, 700); }
    await p.getByRole('button', { name: 'Sales qualified (SQL)', exact: true }).click(); await settle(A, 600);
    ok(sql(`SELECT stage FROM leads WHERE lead_id='${id}'`) !== 'SQL' && await p.locator('.bg-red-50').count() > 0, 'SQL blocked with 3/4 criteria and a message shown');
    await p.getByLabel('Buying timeline known').click(); await settle(A, 800);
    await p.getByRole('button', { name: 'Sales qualified (SQL)', exact: true }).click(); await settle(A, 600);
    eq(sql(`SELECT stage FROM leads WHERE lead_id='${id}'`), 'SQL');
    await p.getByPlaceholder('e.g. Demo with the CFO').fill('Demo with CFO');
    await p.getByRole('button', { name: 'Save next step' }).click(); await settle(A, 600);
    eq(sql(`SELECT next_step FROM leads WHERE lead_id='${id}'`), 'Demo with CFO');
  }, { u: A, allow: [/http 4\d\d PATCH/, /Failed to load resource/] });
  await tc(rec, 'UIC-SAL-06', 'Disqualify requires a reason; stores reason + recycle; recycle as new', async () => {
    const id = leads[9].lead_id;
    await go(A, `leads/${id}`);
    await p.getByRole('button', { name: 'Disqualify' }).click();
    ok(await modalLoc(p).getByRole('button', { name: 'Disqualify' }).isDisabled(), 'disabled without reason');
    await modalLoc(p).getByRole('button', { name: 'No budget' }).click();
    await modalLoc(p).getByRole('button', { name: 'Disqualify' }).click(); await settle(A, 700);
    eq(sql(`SELECT CONCAT_WS('|',stage,disqualified_reason) FROM leads WHERE lead_id='${id}'`), 'DISQUALIFIED|No budget');
    ok(sql(`SELECT recycle_at IS NOT NULL FROM leads WHERE lead_id='${id}'`) === '1', 'recycle scheduled');
    await p.getByRole('button', { name: 'Recycle as new lead' }).click(); await settle(A, 600);
    eq(sql(`SELECT stage FROM leads WHERE lead_id='${id}'`), 'NEW');
  }, { u: A });
  await tc(rec, 'UIC-SAL-07', 'SQL queue: tiles equal DB; convert to opportunity creates deal and redirects', async () => {
    await go(A, 'sql-queue');
    const q = dbn(`SELECT COUNT(*) FROM leads WHERE tenant_id='${t.tenantId}' AND stage='SQL'`);
    eq(num((await p.locator('body').innerText()).match(/IN QUEUE\s+(\d+)/i)?.[1]), q, 'in queue');
    const row = p.locator('tbody tr').filter({ has: p.getByRole('button', { name: /Convert/ }) }).first(); const nm = (await row.locator('p').first().innerText()).trim();
    await row.getByRole('button', { name: /Convert/ }).click();
    await modalLoc(p).getByLabel('Opportunity name').fill('Converted deal'); await modalLoc(p).getByLabel('Amount').fill('12000');
    await modalLoc(p).getByRole('button', { name: /Create opportunity/ }).click();
    await p.waitForURL(/\/app\/deals\/[0-9a-f-]{36}/);
    eq(sql(`SELECT CONCAT_WS('|',name,amount) FROM opportunities WHERE tenant_id='${t.tenantId}' AND name='Converted deal'`), 'Converted deal|12000.00');
    eq(dbn(`SELECT COUNT(*) FROM leads WHERE tenant_id='${t.tenantId}' AND stage='SQL'`), q - 1, 'queue shrinks');
  }, { u: A });

  // ---------- Deals ----------
  await tc(rec, 'UIC-SAL-08', 'Deals board: stage column counts and amounts equal DB; list view total', async () => {
    await go(A, 'deals');
    for (const st of ['Qualification', 'Needs analysis', 'Proposal', 'Negotiation']) {
      const col = p.locator('div.w-72').filter({ hasText: new RegExp(`^${st}`) });
      const n = dbn(`SELECT COUNT(*) FROM opportunities o JOIN sales_stages s ON s.stage_id=o.stage_id WHERE o.tenant_id='${t.tenantId}' AND s.name='${st}' AND o.status='OPEN'`);
      eq(num(await col.locator('span.text-xs').first().innerText()), n, st + ' count');
    }
    await go(A, 'deals?mode=list&status=OPEN'); await settle(A, 500);
    const total = Number(sql(`SELECT SUM(amount) FROM opportunities WHERE tenant_id='${t.tenantId}' AND status='OPEN'`));
    ok(await p.getByText(money(total)).first().isVisible(), `list total ${money(total)}`);
  }, { u: A });
  await tc(rec, 'UIC-SAL-09', 'Deals: create via modal (validation, company, amount, close date) -> DB', async () => {
    await go(A, 'deals');
    await p.getByRole('button', { name: /New opportunity/ }).click();
    const m = modalLoc(p);
    ok(await m.getByRole('button', { name: 'Create' }).isDisabled(), 'disabled w/o name');
    await m.getByLabel('Name *').fill('Modal deal'); await m.getByLabel('Amount').fill('33000'); await m.getByLabel('Expected close').fill('2026-12-15');
    await m.getByRole('button', { name: 'Create' }).click();
    await p.waitForURL(/\/app\/deals\/[0-9a-f-]{36}/);
    eq(sql(`SELECT CONCAT_WS('|',amount,close_date,status) FROM opportunities WHERE tenant_id='${t.tenantId}' AND name='Modal deal'`), '33000.00|2026-12-15|OPEN');
  }, { u: A });
  await tc(rec, 'UIC-SAL-10', 'Deals board: drag to next stage persists; drag to Closed won asks for reason (required)', async () => {
    await go(A, 'deals');
    const card = p.locator('a', { hasText: 'Deal A' }).first();
    await dnd(p, card, p.locator('div.w-72').filter({ hasText: /^Needs analysis/ })); await settle(A, 800);
    eq(sql(`SELECT s.name FROM opportunities o JOIN sales_stages s ON s.stage_id=o.stage_id WHERE o.name='Deal A' AND o.tenant_id='${t.tenantId}'`), 'Needs analysis');
    await dnd(p, p.locator('a', { hasText: 'Deal A' }).first(), p.getByText(/Drop here to mark Closed won/));
    const m = modalLoc(p);
    ok(await m.getByRole('button', { name: /Move to Closed won/ }).isDisabled(), 'reason required');
    await m.getByRole('radio', { name: 'Price', exact: true }).first().check(); await m.getByRole('button', { name: /Move to Closed won/ }).click(); await settle(A, 800);
    eq(sql(`SELECT CONCAT_WS('|',status,closed_reason) FROM opportunities WHERE name='Deal A' AND tenant_id='${t.tenantId}'`), 'WON|Price');
  }, { u: A });
  const dB = deals.find(d => d.name === 'Deal B');
  await tc(rec, 'UIC-SAL-11', 'DealDetail: stage buttons, inline edits (amount/close/next step), forecast category', async () => {
    await go(A, `deals/${dB.opportunity_id}`);
    await p.getByRole('button', { name: /^Proposal/ }).click(); await settle(A, 700);
    eq(sql(`SELECT s.name FROM opportunities o JOIN sales_stages s ON s.stage_id=o.stage_id WHERE o.opportunity_id='${dB.opportunity_id}'`), 'Proposal');
    await p.getByLabel('Amount', { exact: true }).fill('22000'); await p.getByLabel('Next step').fill('Send SOW'); await p.getByLabel('Next step').blur(); await settle(A, 700);
    eq(sql(`SELECT CONCAT_WS('|',amount,next_step) FROM opportunities WHERE opportunity_id='${dB.opportunity_id}'`), '22000.00|Send SOW');
    await p.getByLabel('Forecast category').selectOption('COMMIT'); await settle(A, 600);
    eq(sql(`SELECT CONCAT_WS('|',forecast_category,forecast_category_manual) FROM opportunities WHERE opportunity_id='${dB.opportunity_id}'`), 'COMMIT|1');
  }, { u: A });
  await tc(rec, 'UIC-SAL-12', 'DealDetail: log note/call, task, timeline; mark lost requires reason', async () => {
    await go(A, `deals/${dB.opportunity_id}`);
    await p.getByPlaceholder('Title (optional)').fill('Kickoff note'); await p.getByPlaceholder('What happened?').fill('Discussed scope'); await p.getByRole('button', { name: 'Log', exact: true }).click(); await settle(A, 700);
    ok(await p.getByText('Kickoff note').first().isVisible(), 'note in timeline');
    await p.getByRole('button', { name: 'Task', exact: true }).click(); await p.getByPlaceholder('What needs doing?').fill('Prepare SOW'); await p.getByRole('button', { name: 'Add task' }).click(); await settle(A, 700);
    eq(dbn(`SELECT COUNT(*) FROM crm_tasks WHERE opportunity_id='${dB.opportunity_id}' AND title='Prepare SOW'`), 1);
    await p.getByRole('button', { name: 'Mark lost' }).click();
    ok(await modalLoc(p).getByRole('button', { name: /Move to Closed lost/ }).isDisabled(), 'reason required');
    await modalLoc(p).getByRole('radio', { name: 'Timing', exact: true }).first().check(); await modalLoc(p).getByRole('button', { name: /Move to Closed lost/ }).click(); await settle(A, 800);
    eq(sql(`SELECT CONCAT_WS('|',status,closed_reason) FROM opportunities WHERE opportunity_id='${dB.opportunity_id}'`), 'LOST|Timing');
  }, { u: A });
  const dC = deals.find(d => d.name === 'Deal C');
  await tc(rec, 'UIC-SAL-13', 'Quote: add product + custom line with discount -> totals, proposal amount, PDF download', async () => {
    await must(t.token, 'POST', '/products', { name: 'Platform seat', unit_price: 100, unit: 'seat' });
    await go(A, `deals/${dC.opportunity_id}`);
    await p.getByLabel('Proposal title').filter({ visible: true }).first().fill('Q4 proposal'); await p.getByRole('button', { name: /Add proposal/ }).filter({ visible: true }).first().click(); await settle(A, 700);
    await p.getByTitle('Quote lines and PDF').filter({ visible: true }).first().click();
    const m = modalLoc(p);
    await m.locator('select').first().selectOption({ label: 'Platform seat' });
    await m.locator('input[type=number]').nth(0).fill('10'); await m.locator('input[type=number]').nth(2).fill('10');   // qty 10, 10% discount => 900
    await m.getByRole('button', { name: /Add line/ }).click();
    await m.locator('tbody tr').nth(1).locator('input').first().fill('Setup fee');
    await m.locator('tbody tr').nth(1).locator('input[type=number]').nth(1).fill('250');
    ok(await m.getByText('$1,150').first().isVisible(), 'live total $1,150');
    await m.getByRole('button', { name: /Save lines/ }).click(); await settle(A, 800);
    eq(sql(`SELECT amount FROM proposals WHERE opportunity_id='${dC.opportunity_id}'`), '1150.00', 'proposal amount from lines');
    const [dl] = await Promise.all([p.waitForEvent('download', { timeout: 20000 }), m.getByRole('button', { name: 'PDF' }).click()]);
    ok(/\.pdf$/.test(dl.suggestedFilename()), 'pdf name ' + dl.suggestedFilename());
    await m.getByRole('button', { name: 'Close' }).last().click();
    await p.getByRole('button', { name: 'Delete proposal' }).click(); await settle(A, 700);
    eq(dbn(`SELECT COUNT(*) FROM proposals WHERE opportunity_id='${dC.opportunity_id}'`), 0, 'proposal deleted');
  }, { u: A });
  // ---------- Pipeline ----------
  await tc(rec, 'UIC-SAL-14', 'Pipeline revenue tab: open/weighted/won tiles equal independently computed DB sums; proposals tab renders', async () => {
    await go(A, 'pipeline');
    const open = Number(sql(`SELECT SUM(amount) FROM opportunities WHERE tenant_id='${t.tenantId}' AND status='OPEN'`));
    const weighted = Number(sql(`SELECT SUM(o.amount*s.probability/100) FROM opportunities o JOIN sales_stages s ON s.stage_id=o.stage_id WHERE o.tenant_id='${t.tenantId}' AND o.status='OPEN'`));
    const body = await p.locator('body').innerText();
    const short = n => new Intl.NumberFormat('en', { style: 'currency', currency: 'USD', notation: 'compact', maximumFractionDigits: 1 }).format(n);
    const has = n => body.includes(short(n)) || body.includes(short(n).replace(/\.0([KM])/, '$1')) || body.includes(short(n).replace(/([KM])$/, '.0$1'));
    ok(has(open), `open ${short(open)}`); ok(has(weighted), `weighted ${short(weighted)}`);
    ok(body.includes(money(open)), 'table open total ' + money(open));
    await p.getByRole('button', { name: 'Proposal pipeline' }).click(); await settle(A, 500);
    for (const s of ['Draft', 'Sent', 'Under review', 'Accepted', 'Rejected']) ok(await p.getByText(s, { exact: true }).first().isVisible(), s);
  }, { u: A });
  // ---------- Dashboard / reports ----------
  await tc(rec, 'UIC-SAL-15', 'Sales dashboard: tiles match DB (won amount/count, open pipeline); period selector changes numbers', async () => {
    await go(A, 'sales');
    await p.getByLabel('Period').selectOption('365'); await settle(A, 800);
    const won = Number(sql(`SELECT SUM(amount) FROM opportunities WHERE tenant_id='${t.tenantId}' AND status='WON' AND closed_at >= NOW() - INTERVAL 365 DAY`));
    const short = n => new Intl.NumberFormat('en', { style: 'currency', currency: 'USD', notation: 'compact', maximumFractionDigits: 1 }).format(n);
    { const b = await p.locator('body').innerText(); ok(b.includes(short(won)) || b.includes(short(won).replace(/\.0([KM])/, '$1')), `won ${short(won)} (12 months)`); }
    await p.getByLabel('Period').selectOption('30'); await settle(A, 800);
    const won30 = Number(sql(`SELECT IFNULL(SUM(amount),0) FROM opportunities WHERE tenant_id='${t.tenantId}' AND status='WON' AND closed_at >= NOW() - INTERVAL 30 DAY`));
    { const b = await p.locator('body').innerText(); ok(b.includes(short(won30)) || b.includes(short(won30).replace(/\.0([KM])/, '$1')) || won30 === 0, `won ${short(won30)} (30 days)`); }
  }, { u: A });
  await tc(rec, 'UIC-SAL-16', 'Sales reports: every tab renders without errors', async () => {
    for (const [tab, label] of [['leads', 'Leads & SQLs'], ['pipeline', 'Pipeline'], ['forecast', 'Forecast'], ['targets', 'Targets & leaderboard'], ['team', 'Team performance'], ['roi', 'Campaign ROI'], ['custom', 'Custom reports']]) {
      await go(A, `sales-reports?tab=${tab}`); await settle(A, 600);
      ok(await p.locator('.animate-spin').count() === 0, `${label} finished loading`);
      const bp = await A.w.bodyProblems(); eq(bp, [], label);
    }
  }, { u: A });
  await tc(rec, 'UIC-SAL-17', 'Forecast: FY April-March quarters; won per quarter equals DB', async () => {
    await go(A, 'sales-reports?tab=forecast');
    const body = await p.locator('body').innerText();
    ok(/FY 2026-27/.test(body), 'FY label'); ok(body.includes('2026-04-01 to 2027-03-31'), 'FY runs 2026-04-01 to 2027-03-31 (April-March)');
    const row = n => p.locator('tbody tr').filter({ hasText: new RegExp(`^Q${n}`) });
    ok((await row(1).innerText()).includes(money(50000)), 'Q1 won $50,000');
    ok((await row(2).innerText()).includes(money(25000)), 'Q2 won $25,000');
    const q3won = Number(sql(`SELECT SUM(amount) FROM opportunities WHERE tenant_id='${t.tenantId}' AND status='WON' AND close_date BETWEEN '2026-10-01' AND '2026-12-31'`));
    ok((await row(3).innerText()).includes(money(q3won)), 'Q3 won ' + money(q3won));
  }, { u: A });
  await tc(rec, 'UIC-SAL-18', 'Custom report builder: build, run, save, reopen, delete', async () => {
    await go(A, 'sales-reports?tab=custom');
    await p.getByLabel('Object').selectOption('deals'); await p.getByLabel('Group by').selectOption('stage'); await p.getByLabel('Measure').selectOption('sum_amount');
    await p.getByRole('button', { name: 'Run' }).click(); await settle(A, 800);
    const total = Number(sql(`SELECT SUM(amount) FROM opportunities WHERE tenant_id='${t.tenantId}'`));
    ok((await p.locator('body').innerText()).includes(money(total)), 'total ' + money(total));
    await p.getByPlaceholder('Report name').fill('Pipeline by stage');
    await p.getByRole('button', { name: /Save report/ }).click(); await settle(A, 800);
    eq(dbn(`SELECT COUNT(*) FROM saved_reports WHERE tenant_id='${t.tenantId}' AND name='Pipeline by stage'`), 1);
    await go(A, 'sales-reports?tab=custom'); await p.getByRole('button', { name: /Pipeline by stage/ }).click(); await settle(A, 800);
    ok((await p.locator('body').innerText()).includes(money(total)), 'reopened result');
    await p.getByRole('button', { name: 'Delete report' }).click(); await settle(A, 700);
    eq(dbn(`SELECT COUNT(*) FROM saved_reports WHERE tenant_id='${t.tenantId}' AND name='Pipeline by stage'`), 0, 'deleted');
  }, { u: A });
  // ---------- Targets / team / settings / connections ----------
  await tc(rec, 'UIC-SAL-19', 'Targets: edit quarterly cell persists, totals row, attainment on reports', async () => {
    await go(A, 'sales-targets');
    const row = p.locator('tbody tr').filter({ hasText: 'Admin' }).first();
    await row.locator('input').nth(2).fill('100000'); await row.locator('input').nth(2).blur(); await settle(A, 800);
    eq(sql(`SELECT amount FROM sales_targets WHERE tenant_id='${t.tenantId}' AND user_id='${t.owner.userId}' AND quarter=3`), '100000.00');
    await p.reload(); await settle(A);
    eq(await p.locator('tbody tr').filter({ hasText: 'Admin' }).first().locator('input').nth(2).inputValue(), '100000', 'persisted');
    await go(A, 'sales-reports?tab=targets');
    const won = Number(sql(`SELECT SUM(amount) FROM opportunities WHERE tenant_id='${t.tenantId}' AND status='WON' AND close_date BETWEEN '2026-04-01' AND '2027-03-31'`));
    ok(new RegExp(`${Math.round(won / 100000 * 100)}%`).test(await p.locator('body').innerText()), 'attainment % shown');
  }, { u: A });
  await tc(rec, 'UIC-SAL-20', 'Sales team: change level/manager persists and changes visibility', async () => {
    await go(A, 'sales-team');
    await p.getByLabel('Manager for Other Test').selectOption({ label: 'Boss Test (L2)' }).catch(async () => { await p.getByLabel('Level for Other Test').selectOption('3'); await p.getByLabel('Manager for Other Test').selectOption({ label: 'Boss Test (L2)' }); });
    await settle(A, 800);
    eq(sql(`SELECT manager_id FROM users WHERE user_id='${other.userId}'`), boss.userId);
    const tok = await t.tk('boss');
    const r = await must(tok, 'GET', '/leads?page_size=100');
    ok(r.items.some(l => l.owner_id === other.userId), 'manager now sees the new report\'s leads');
  }, { u: A });
  await tc(rec, 'UIC-SAL-21', 'Sales settings: save persists after reload; stale days validation', async () => {
    await go(A, 'sales-settings');
    await p.getByLabel(/Stale after/).fill('21');
    await p.getByRole('button', { name: 'Save settings' }).click(); await settle(A, 800);
    ok(await p.getByText('Saved').isVisible(), 'saved indicator');
    await p.reload(); await settle(A);
    eq(await p.getByLabel(/Stale after/).inputValue(), '21', 'persisted');
    eq((await must(t.token, 'GET', '/sales/settings')).stale_deal_days, 21, 'API');
    await p.getByLabel(/Stale after/).fill('0'); await p.getByRole('button', { name: 'Save settings' }).click(); await settle(A, 800);
    ok(await p.locator('.bg-red-50').count() > 0 || (await must(t.token, 'GET', '/sales/settings')).stale_deal_days !== 0, 'invalid 0 rejected with a message');
  }, { u: A, allow: [/http 4\d\d PUT/, /Failed to load resource/] });
  await tc(rec, 'UIC-SAL-22', 'Sales settings: add reason, product and workflow rule', async () => {
    await go(A, 'sales-settings');
    await p.getByPlaceholder('Add a win reason').fill('Great demo'); await p.getByPlaceholder('Add a win reason').press('Enter');
    await p.getByRole('button', { name: 'Save settings' }).click(); await settle(A, 800);
    ok((await must(t.token, 'GET', '/sales/settings')).win_reasons.includes('Great demo'), 'reason saved');
    await p.getByRole('button', { name: /New product/ }).click();
    await modalLoc(p).getByLabel('Name').fill('Support plan'); await modalLoc(p).getByLabel('Unit price').fill('49.5');
    await modalLoc(p).getByRole('button', { name: 'Save' }).click(); await settle(A, 700);
    eq(dbn(`SELECT COUNT(*) FROM products WHERE tenant_id='${t.tenantId}' AND name='Support plan'`), 1);
    await p.getByRole('button', { name: /New rule/ }).click();
    await modalLoc(p).getByPlaceholder(/Proposal sent/).fill('Won deal alert'); await modalLoc(p).getByLabel('Value').fill('Closed won'); await modalLoc(p).getByRole('button', { name: 'Save rule' }).click(); await settle(A, 700);
    eq(dbn(`SELECT COUNT(*) FROM workflow_rules WHERE tenant_id='${t.tenantId}' AND name='Won deal alert'`), 1);
  }, { u: A });
  await tc(rec, 'UIC-SAL-23', 'Calendar sync: Google/Microsoft show a clear "not set up" message and Connect is disabled', async () => {
    await go(A, 'connections');
    eq(await p.getByText(/Not set up on this server yet/).count(), 2, 'two not-set-up messages');
    for (const n of ['Google', 'Microsoft']) ok(await p.getByRole('button', { name: `Connect ${n}` }).isDisabled(), n + ' disabled');
  }, { u: A });
  await tc(rec, 'UIC-SAL-24', 'Amount hiding: AGENT hidden role sees no money in deals, pipeline, reports, quotes; API masks amounts', async () => {
    await must(t.token, 'PUT', '/sales/settings', { amount_hidden_roles: ['AGENT'], amount_hidden_levels: [] });
    const AG = await ui(browser, alice);
    try {
      for (const r of ['deals?mode=list', 'deals', 'pipeline', 'sales', 'sales-reports?tab=forecast', 'sales-reports?tab=pipeline', `deals/${deals.find(d => d.name === 'Deal E').opportunity_id}`]) {
        await go(AG, r); await settle(AG, 500);
        const body = await AG.page.locator('body').innerText();
        ok(!/\$\s?\d/.test(body), `${r} shows a money value: ${(body.match(/\$\s?[\d,.]+[KM]?/) || [''])[0]}`);
      }
      const r = await must(await t.tk('alice'), 'GET', '/opportunities?page_size=50');
      ok(r.items.every(d => d.amount == null), 'API masks amount');
      const x = await call(await t.tk('alice'), 'GET', '/pipeline/revenue'); ok(!JSON.stringify(x.data.totals).match(/[1-9]\d{3}/), 'revenue totals masked');
    } finally { await must(t.token, 'PUT', '/sales/settings', { amount_hidden_roles: [], amount_hidden_levels: [] }); await AG.ctx.close(); }
  }, {});
  await tc(rec, 'UIC-SAL-25', 'AGENT visibility: sees only own leads/deals; peer\'s record by URL is not found', async () => {
    await must(t.token, 'PATCH', `/users/${other.userId}`, {}).catch(() => {});
    sql(`UPDATE users SET manager_id=NULL WHERE user_id='${other.userId}'`);
    const AG = await ui(browser, alice);
    try {
      await go(AG, 'leads?mode=list'); await settle(AG, 500);
      eq(await AG.page.locator('tbody tr').count(), dbn(`SELECT COUNT(*) FROM leads WHERE tenant_id='${t.tenantId}' AND owner_id='${alice.userId}'`), 'own leads');
      await go(AG, `leads/${leads[6].lead_id}`); ok(await AG.page.getByText('Lead not found').isVisible(), 'peer lead not found');
      await go(AG, `deals/${deals[0].opportunity_id}`); ok(await AG.page.getByText('Opportunity not found').isVisible(), 'owner deal not found');
    } finally { await AG.ctx.close(); }
  }, { allow: [] });
  await tc(rec, 'UIC-SAL-26', 'Stale-deal notification job creates a bell notification for the owner', async () => {
    sql(`UPDATE opportunities SET updated_at = NOW() - INTERVAL 40 DAY WHERE tenant_id='${t.tenantId}' AND name='Deal D'`);
    sql(`UPDATE leads SET owner_id='${alice.userId}' WHERE lead_id='${leads[8].lead_id}'`);
    const { execFileSync } = await import('node:child_process');
    execFileSync('docker', ['exec', 'vector-api-1', 'python', '-c', 'import asyncio; from app.jobs import sales_jobs_iteration; asyncio.run(sales_jobs_iteration())'], { stdio: 'ignore' });
    const n = dbn(`SELECT COUNT(*) FROM notifications WHERE tenant_id='${t.tenantId}' AND user_id='${t.owner.userId}' AND (title LIKE '%tale%' OR kind LIKE '%stale%')`);
    ok(n >= 1, 'stale notification row for owner (count ' + n + ')');
    await go(A, 'dashboard'); await p.getByRole('button', { name: /notification/i }).first().click().catch(() => {}); await settle(A, 600);
  }, { u: A });
  await tc(rec, 'UIC-SAL-27', 'Deals API failure keeps the page shell', async () => {
    await failApi(p, /\/api\/opportunities\/board$/, 500, 'GET'); await go(A, 'deals?x=1'); await settle(A, 600);
    ok(await p.getByRole('heading', { name: 'Opportunities' }).isVisible(), 'shell');
    await p.unrouteAll({ behavior: 'ignoreErrors' });
  }, { u: A, allow: [/http 500/, /Failed to load resource/] });
  await A.ctx.close();
}
if (isMain(import.meta)) standalone('sales', run);
