// UIC-NF: UI cases for the features added in the Oct-12 development round
//   multi-company contacts (Companies card + company page chips), @mentions in notes + bell notification,
//   SQL-to-win and forecast-accuracy KPI cards, Create deal from the contact record, AccountDetail deals,
//   Recently deleted "Delete forever" (purge).
import { Tenant, ui, go, settle, tc, ok, eq, must, call, sql, dbn, modalLoc, standalone, isMain } from './crm_common.mjs';

export async function run(rec, browser) {
  const t = await (await Tenant.get('newfeat')).init();
  const bianca = await t.addUser('ADMIN', 'Bianca');
  await t.addUser('MANAGER', 'Bram');
  const aaron = await t.addUser('AGENT', 'Aaron');
  const acct = async name => must(t.token, 'POST', '/accounts', { name: `${name} ${t.rid}`, domain: `${name.toLowerCase()}-${t.rid}.example` });
  const alpha = await acct('Alpha'); const beta = await acct('Beta'); await acct('Gamma');
  const ALPHA = alpha.name, BETA = beta.name;
  const cara = await must(t.token, 'POST', '/contacts', { email: `cara.${t.rid}@nf.example`, first_name: 'Cara', last_name: 'Companies',
    account_id: alpha.account_id, owner_id: t.owner.userId, lifecycle_stage: 'LEAD' });
  const id = cara.prospect_id;
  const links = () => sql(`SELECT GROUP_CONCAT(CONCAT(a.name,'|',IFNULL(cc.label,''),'|',cc.is_primary) ORDER BY a.name SEPARATOR ';') FROM contact_companies cc JOIN accounts a ON a.account_id=cc.account_id WHERE cc.prospect_id='${id}'`);
  const A = await ui(browser, t.owner); const p = A.page;
  const card = () => p.getByTestId('contact-companies');
  const item = name => card().locator('li').filter({ has: p.getByRole('link', { name }) });

  // ---------- (1) Companies card ----------
  await tc(rec, 'UIC-NF-01', 'Companies card: lists the primary company; Add a second company with a label (search + pick) -> card + DB', async () => {
    await go(A, `contacts/${id}`);
    await card().getByRole('heading', { name: /^Companies/ }).waitFor();
    ok(await item(ALPHA).getByText('Primary', { exact: true }).isVisible(), 'Alpha shown as Primary');
    await card().getByRole('button', { name: 'Add', exact: true }).click();
    await card().getByLabel('Search companies').fill('Beta');
    await card().getByRole('button', { name: new RegExp(BETA) }).click();
    await card().getByLabel('Association label').fill('Decision maker');
    await card().getByRole('button', { name: 'Add company' }).click(); await settle(A, 900);
    await card().getByRole('heading', { name: 'Companies (2)' }).waitFor({ timeout: 8000 });
    ok(await item(BETA).getByText('Decision maker', { exact: true }).isVisible(), 'label chip on Beta');
    eq(await item(BETA).getByText('Primary', { exact: true }).count(), 0, 'Beta not primary');
    eq(links(), `${ALPHA}||1;${BETA}|Decision maker|0`, 'contact_companies rows');
  }, { u: A });

  await tc(rec, 'UIC-NF-02', 'Companies card: star makes the second company primary (badge moves, DB is_primary + contact account)', async () => {
    await go(A, `contacts/${id}`);
    await card().getByRole('button', { name: `Make ${BETA} the primary company` }).click(); await settle(A, 900);
    await item(BETA).getByText('Primary', { exact: true }).waitFor({ timeout: 8000 });
    eq(await item(ALPHA).getByText('Primary', { exact: true }).count(), 0, 'Alpha lost the Primary badge');
    ok(await card().getByRole('button', { name: `Make ${ALPHA} the primary company` }).isVisible(), 'star now offered on Alpha');
    eq(links(), `${ALPHA}||0;${BETA}|Decision maker|1`, 'DB primary moved');
    eq(sql(`SELECT account_id FROM prospects WHERE prospect_id='${id}'`), beta.account_id, 'prospects.account_id follows the primary');
  }, { u: A });

  await tc(rec, 'UIC-NF-03', 'Companies card: removing the primary asks for the next primary (pre-selected), Cancel keeps it, Remove -> Alpha primary', async () => {
    await go(A, `contacts/${id}`);
    await card().getByRole('button', { name: `Remove ${BETA}` }).click();
    const succ = card().getByLabel('New primary company');
    await succ.waitFor();
    eq(await succ.inputValue(), alpha.account_id, 'successor pre-selected');
    ok(await card().getByText(`${BETA} is the primary company`).isVisible(), 'explanation shown');
    await card().getByRole('button', { name: 'Cancel', exact: true }).click();
    eq(await succ.count(), 0, 'cancel closes the successor picker');
    eq(links(), `${ALPHA}||0;${BETA}|Decision maker|1`, 'cancel changed nothing');
    await card().getByRole('button', { name: `Remove ${BETA}` }).click();
    await card().getByRole('button', { name: 'Remove', exact: true }).click(); await settle(A, 900);
    await card().getByRole('heading', { name: 'Companies', exact: true }).waitFor({ timeout: 8000 });
    ok(await item(ALPHA).getByText('Primary', { exact: true }).isVisible(), 'Alpha is primary again');
    eq(links(), `${ALPHA}||1`, 'one link left, primary');
    eq(sql(`SELECT account_id FROM prospects WHERE prospect_id='${id}'`), alpha.account_id, 'prospects.account_id = new primary');
    // the only company cannot be removed: message, nothing sent
    await card().getByRole('button', { name: `Remove ${ALPHA}` }).click(); await settle(A, 300);
    ok(await card().getByText(/only company/).isVisible(), 'only-company message');
    eq(links(), `${ALPHA}||1`, 'still linked');
    eq(dbn(`SELECT COUNT(*) FROM property_changes WHERE object_id='${id}' AND field='company_association'`) >= 2, true, 'association changes in history');
  }, { u: A });

  await tc(rec, 'UIC-NF-04', 'Company page shows association chips: label + "Associated" on the non-primary company; card link opens it', async () => {
    await must(t.token, 'POST', `/contacts/${id}/companies`, { account_id: beta.account_id, label: 'Billing' });
    await go(A, `contacts/${id}`);
    await card().getByRole('link', { name: BETA }).click();
    await p.waitForURL(new RegExp(`/app/accounts/${beta.account_id}$`), { timeout: 8000 }); await settle(A, 700);
    const row = p.locator('a, li, div').filter({ hasText: 'Cara Companies' }).filter({ hasText: 'Billing' }).last();
    await row.waitFor({ timeout: 8000 });
    ok(await row.getByText('Billing', { exact: true }).isVisible(), 'label chip');
    const assoc = row.getByText('Associated', { exact: true });
    ok(await assoc.isVisible(), '"Associated" chip on the non-primary company');
    eq(await assoc.getAttribute('title'), `Primary company: ${ALPHA}`, 'chip tooltip names the primary company');
    await go(A, `accounts/${alpha.account_id}`);
    await p.getByText('Cara Companies').first().waitFor();
    eq(await p.getByText('Associated', { exact: true }).count(), 0, 'no "Associated" chip on the primary company page');
  }, { u: A });

  // ---------- (2) @mention ----------
  const note = `Please review the pricing ${t.rid}`;
  await tc(rec, 'UIC-NF-05', '@mention: type "@B", pick Bianca with the keyboard, save -> highlighted in timeline; note_mentions + MENTION notification (not for author)', async () => {
    await go(A, `contacts/${id}`);
    const ta = p.getByRole('textbox', { name: 'Note', exact: true });
    await ta.click();
    await ta.pressSequentially('Hi @B', { delay: 40 });
    const list = p.getByRole('listbox', { name: 'Mention a colleague' });
    await list.waitFor({ timeout: 8000 });
    const names = (await list.getByRole('option').allInnerTexts()).map(s => s.split('\n')[0].trim());
    // only colleagues who can see this contact are offered (mentions.mentionable_users check_owner): Bram is a
    // MANAGER outside the owner's sales hierarchy, so he cannot open the owner's contact and is not listed
    ok(names.includes('Bianca Test') && !names.includes('Bram Test'), 'picker lists matching colleagues who can see the record: ' + names.join(','));
    eq(await ta.getAttribute('aria-expanded'), 'true', 'textarea aria-expanded');
    // keyboard: move down through the list until Bianca is active, then Enter
    for (let i = 0; i < names.length; i++) {
      const active = await list.locator('[role=option][aria-selected=true]').innerText();
      if (active.startsWith('Bianca Test')) break;
      await ta.press('ArrowDown');
    }
    ok((await list.locator('[role=option][aria-selected=true]').innerText()).startsWith('Bianca Test'), 'Bianca highlighted by arrow keys');
    await ta.press('Enter');
    await list.waitFor({ state: 'hidden' });
    eq(await ta.inputValue(), 'Hi @Bianca Test ', 'picked name inserted, caret after it');
    await ta.pressSequentially(note, { delay: 5 });
    await p.getByRole('button', { name: 'Save note' }).click(); await settle(A, 900);
    const chip = p.locator('[data-mention]', { hasText: '@Bianca Test' }).first();
    await chip.waitFor({ timeout: 8000 });
    const bg = await chip.evaluate(e => getComputedStyle(e).backgroundColor);
    ok(bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent', 'mention highlighted (background ' + bg + ')');
    const act = sql(`SELECT activity_id FROM contact_activities WHERE prospect_id='${id}' AND activity_type='NOTE' AND body LIKE '%${t.rid}%'`);
    ok(act, 'note stored');
    eq(sql(`SELECT GROUP_CONCAT(user_id) FROM note_mentions WHERE activity_id='${act}'`), bianca.userId, 'note_mentions row for Bianca only');
    eq(dbn(`SELECT COUNT(*) FROM notifications WHERE user_id='${bianca.userId}' AND kind='MENTION' AND link='/app/contacts/${id}'`), 1, 'MENTION notification for Bianca with record link');
    eq(dbn(`SELECT COUNT(*) FROM notifications WHERE user_id='${t.owner.userId}' AND kind='MENTION'`), 0, 'author not notified');
  }, { u: A });

  const B = await ui(browser, bianca); const pb = B.page;
  await tc(rec, 'UIC-NF-06', "Mentioned colleague's bell shows the MENTION (unread), click opens the contact record and marks it read", async () => {
    await go(B, 'dashboard');
    const bell = pb.getByRole('button', { name: /^Notifications, 1 unread$/ });
    await bell.waitFor({ timeout: 10000 });
    await bell.click();
    const entry = pb.getByRole('button', { name: /Owner Admin mentioned you in a note on Cara Companies/ });
    await entry.waitFor();
    ok((await entry.innerText()).includes(note), 'notification body carries the note text');
    await entry.click();
    await pb.waitForURL(new RegExp(`/app/contacts/${id}$`), { timeout: 8000 });
    await pb.getByRole('heading', { name: 'Cara Companies' }).waitFor();
    await pb.locator('[data-mention]', { hasText: '@Bianca Test' }).first().waitFor({ timeout: 10000 }); // timeline loads after the header
    await settle(B, 600);
    eq(dbn(`SELECT COUNT(*) FROM notifications WHERE user_id='${bianca.userId}' AND kind='MENTION' AND read_at IS NOT NULL`), 1, 'marked read');
    ok(await pb.getByRole('button', { name: /^Notifications$/ }).isVisible(), 'badge cleared');
  }, { u: B });

  await tc(rec, 'UIC-NF-07', 'Mentioning an AGENT who cannot see the contact is refused (400) and creates no notification; the picker does not offer them', async () => {
    // mentions.validate(check_owner): a notification linking to a record the person cannot open would only lead to a 404
    const r = await call(t.token, 'POST', `/contacts/${id}/activities`, { activity_type: 'NOTE', body: `@Aaron Test FYI ${t.rid}`, mentioned_user_ids: [aaron.userId] });
    eq(r.status, 400, 'mention of an agent who cannot see the record');
    ok(/cannot see this record/.test(JSON.stringify(r.data || r.body || '')), 'error explains why: ' + JSON.stringify(r.data || r.body || '').slice(0, 200));
    eq(dbn(`SELECT COUNT(*) FROM notifications WHERE user_id='${aaron.userId}' AND kind='MENTION'`), 0, 'agent not notified');
    const opts = await must(t.token, 'GET', `/contacts/mentionable-users?q=Aaron&prospect_id=${id}`);
    ok(!JSON.stringify(opts).includes(aaron.userId), 'picker does not offer the agent');
  });

  // ---------- (3) KPI cards ----------
  // 3 SQLs in the last 90 days owned by the owner: one won deal ($12,345.50), one lost, one without a deal
  const stages = await must(t.token, 'GET', '/sales/stages');
  const S = n => stages.find(s => s.name === n).stage_id;
  const kpiLeads = [];
  for (let i = 0; i < 3; i++) {
    const c = await must(t.token, 'POST', '/contacts', { email: `kpi${i}.${t.rid}@nf.example`, first_name: `Kpi${i}`, last_name: 'Lead', owner_id: t.owner.userId });
    const l = await must(t.token, 'POST', '/leads', { prospect_id: c.prospect_id, owner_id: t.owner.userId, source: 'nf' });
    sql(`UPDATE leads SET stage='SQL', qualified_at=NOW() - INTERVAL ${10 + i} DAY WHERE lead_id='${l.lead_id}'`);
    kpiLeads.push(l);
  }
  for (const [i, amount, stage] of [[0, 12345.5, 'Closed won'], [1, 4000, 'Closed lost']]) {
    const d = await must(t.token, 'POST', '/opportunities', { name: `KPI deal ${i}`, amount, stage_id: S('Qualification'), owner_id: t.owner.userId, client_type: 'NEW' });
    await must(t.token, 'PATCH', `/opportunities/${d.opportunity_id}`, { stage_id: S(stage), closed_reason: 'Price' });
    sql(`UPDATE opportunities SET lead_id='${kpiLeads[i].lead_id}' WHERE opportunity_id='${d.opportunity_id}'`);
  }
  const tile = (scope, label) => scope.locator('div').filter({ has: p.locator('p', { hasText: new RegExp(`^${label}$`) }) }).last();
  await tc(rec, 'UIC-NF-08', 'Sales reports > Leads & SQLs: "SQL to win" card numbers match GET /sales-reports/sql-to-win (and the seeded data)', async () => {
    const respP = p.waitForResponse(r => /\/sales-reports\/sql-to-win\?/.test(r.url()) && r.request().method() === 'GET', { timeout: 15000 });
    await go(A, 'sales-reports?tab=leads');
    const resp = await respP;
    const qs = new URL(resp.url()).search;
    const k = await must(t.token, 'GET', `/sales-reports/sql-to-win${qs}`);
    eq([k.sqls, k.won, k.lost, k.open, k.no_deal, k.with_deal, k.won_amount], [3, 1, 1, 0, 1, 2, 12345.5], 'API vs seeded data');
    const cardEl = p.getByTestId('sql-to-win');
    await cardEl.getByRole('heading', { name: 'SQL to win' }).waitFor();
    const txt = async label => (await tile(cardEl, label).innerText()).replace(/\s+/g, ' ');
    const pctS = v => (v == null ? '—' : `${v}%`);
    ok((await txt('SQL → won')).includes(pctS(k.sql_to_win_rate)) && (await txt('SQL → won')).includes(`${k.won} of ${k.sqls} SQLs`), 'SQL→won tile: ' + await txt('SQL → won'));
    ok((await txt('SQL → deal')).includes(pctS(k.sql_to_deal_rate)) && (await txt('SQL → deal')).includes(`${k.with_deal} with a deal`), 'SQL→deal tile: ' + await txt('SQL → deal'));
    ok((await txt('Win rate \\(closed\\)')).includes(pctS(k.closed_win_rate)) && (await txt('Win rate \\(closed\\)')).includes(`${k.won} won · ${k.lost} lost · ${k.open} open`), 'win-rate tile');
    ok((await txt('Revenue won')).includes(`${k.no_deal} SQLs without a deal`) && /\$12\.3K/.test(await txt('Revenue won')), 'revenue tile: ' + await txt('Revenue won'));
    const orow = (await cardEl.locator('tbody tr').filter({ hasText: 'Owner Admin' }).innerText()).split('\t').map(s => s.trim());
    const o = k.by_owner.find(x => x.owner_id === t.owner.userId);
    eq(orow.slice(1, 6).map(Number), [o.sqls, o.won, o.lost, o.open, o.no_deal], 'owner row counts');
    eq(orow[6], '$' + Math.round(o.won_amount).toLocaleString('en'), 'owner won amount cell (whole dollars)');
    ok(orow[7] === pctS(o.sql_to_win_rate), 'owner rate ' + orow[7]);
  }, { u: A });

  await tc(rec, 'UIC-NF-09', 'Forecast tab: "Forecast accuracy" card; admin "Take snapshot now" stores a MANUAL snapshot (DB) and the card shows it', async () => {
    eq(dbn(`SELECT COUNT(*) FROM forecast_snapshots WHERE tenant_id='${t.tenantId}'`), 0, 'precondition: no snapshot');
    await go(A, 'sales-reports?tab=forecast');
    const cardEl = p.getByTestId('forecast-accuracy');
    await cardEl.getByRole('heading', { name: 'Forecast accuracy' }).waitFor();
    for (const l of ['Average accuracy', 'Last finished period', 'This period']) ok(await tile(cardEl, l).isVisible(), 'tile ' + l);
    ok((await tile(cardEl, 'This period').innerText()).includes('No forecast stored yet'), 'this period: none stored');
    const fa = await must(t.token, 'GET', '/sales-reports/forecast-accuracy?period_type=QUARTER');
    ok((await tile(cardEl, 'This period').innerText()).includes(fa.current_period.label), 'current period label ' + fa.current_period.label);
    await cardEl.getByRole('button', { name: 'Take snapshot now' }).click(); await settle(A, 1200);
    await cardEl.getByRole('button', { name: 'Retake snapshot' }).waitFor({ timeout: 8000 });
    const snap = sql(`SELECT CONCAT_WS('|',period_type,period_start,source,taken_by) FROM forecast_snapshots WHERE tenant_id='${t.tenantId}' AND owner_id='${t.owner.userId}'`);
    eq(snap, `QUARTER|${fa.current_period.period_start}|MANUAL|${t.owner.userId}`, 'snapshot row for the owner');
    ok((await tile(cardEl, 'This period').innerText()).includes('Forecast stored'), 'tile now says Forecast stored');
    ok(await cardEl.locator('tbody tr').filter({ hasText: fa.current_period.label }).filter({ hasText: 'by hand' }).count() >= 1, 'period row marked "by hand"');
    // Monthly toggle
    await cardEl.getByRole('button', { name: 'Monthly', exact: true }).click(); await settle(A, 700);
    ok(await cardEl.getByRole('button', { name: 'Take snapshot now' }).isVisible(), 'no monthly snapshot yet');
  }, { u: A });

  // ---------- (4) Create deal / AccountDetail deals / purge ----------
  await tc(rec, 'UIC-NF-10', 'Contact record "+ Create deal": form opens with the primary company pre-selected; Create -> deal linked to contact + company', async () => {
    await go(A, `contacts/${id}`);
    await p.getByRole('button', { name: '+ Create deal' }).click();
    const m = modalLoc(p);
    await m.getByText('New opportunity').waitFor();
    const comp = m.getByLabel('Company');
    eq(await comp.inputValue(), alpha.account_id, 'company pre-filled with the primary company');
    eq((await comp.locator('option:checked').innerText()).trim(), ALPHA, 'company name shown');
    ok(await m.getByRole('button', { name: 'Create' }).isDisabled(), 'Create disabled without a name');
    await m.getByLabel('Name *').fill(`Cara expansion ${t.rid}`);
    await m.getByLabel('Amount').fill('25000');
    await m.getByRole('button', { name: 'Create' }).click();
    await p.waitForURL(/\/app\/deals\/[0-9a-f-]{36}$/, { timeout: 10000 });
    const did = p.url().split('/').pop();
    eq(sql(`SELECT CONCAT_WS('|',prospect_id,account_id,ROUND(amount),owner_id) FROM opportunities WHERE opportunity_id='${did}'`), `${id}|${alpha.account_id}|25000|${t.owner.userId}`, 'deal row');
    const contactLink = p.getByRole('link', { name: 'Cara Companies' }).first();
    await contactLink.waitFor({ timeout: 10000 }).catch(() => {});   // the deal page loads after the redirect
    ok(await contactLink.isVisible(), 'deal page links the contact');
  }, { u: A });

  await tc(rec, 'UIC-NF-11', 'AccountDetail shows the Deals section (count, total, deal link); a company without deals shows the empty state', async () => {
    await go(A, `accounts/${alpha.account_id}`);
    const h = p.getByRole('heading', { name: /^Deals/ });
    await h.waitFor();
    eq((await h.innerText()).trim(), 'Deals (1)', 'deal count');
    ok(await p.getByText('$25,000 total').isVisible(), 'total amount');
    await p.getByRole('link', { name: `Cara expansion ${t.rid}` }).click();
    await p.waitForURL(/\/app\/deals\/[0-9a-f-]{36}$/, { timeout: 8000 });
    await go(A, `accounts/${beta.account_id}`);
    await p.getByRole('heading', { name: 'Deals (0)' }).waitFor();
    ok(await p.getByText('No deals with this company yet.').isVisible(), 'empty state');
  }, { u: A });

  await tc(rec, 'UIC-NF-12', 'Recently deleted: select -> "Delete forever" -> confirm modal (Cancel keeps it) -> contact removed from DB + PURGE_CONTACT audit row', async () => {
    const d = await must(t.token, 'POST', '/contacts', { email: `purge.${t.rid}@nf.example`, first_name: 'Pia', last_name: 'Purge', owner_id: t.owner.userId });
    await must(t.token, 'POST', '/contacts', { email: `keep.${t.rid}@nf.example`, first_name: 'Kim', last_name: 'Keep', owner_id: t.owner.userId })
      .then(k => call(t.token, 'DELETE', `/contacts/${k.prospect_id}`));
    eq((await call(t.token, 'DELETE', `/contacts/${d.prospect_id}`)).status < 300, true, 'soft delete');
    await go(A, 'recently-deleted');
    eq(await p.getByRole('button', { name: /Delete forever/ }).count(), 0, 'no purge button without a selection');
    await p.getByLabel('Select Pia Purge').check();
    await p.getByRole('button', { name: /Delete forever/ }).click();
    const m = modalLoc(p);
    await m.getByText('Delete 1 contact forever?').waitFor();
    ok(await m.getByText(/cannot be undone/).isVisible(), 'warning text');
    await m.getByRole('button', { name: 'Cancel' }).click();
    eq(dbn(`SELECT COUNT(*) FROM prospects WHERE prospect_id='${d.prospect_id}'`), 1, 'cancel keeps it');
    await p.getByRole('button', { name: /Delete forever/ }).click();
    await modalLoc(p).getByRole('button', { name: /Delete forever/ }).click(); await settle(A, 1000);
    await p.getByText('Permanently deleted 1 contact.').waitFor({ timeout: 8000 });
    eq(dbn(`SELECT COUNT(*) FROM prospects WHERE prospect_id='${d.prospect_id}'`), 0, 'row gone from prospects');
    eq(dbn(`SELECT COUNT(*) FROM audit_logs WHERE tenant_id='${t.tenantId}' AND action='PURGE_CONTACT' AND entity_id='${d.prospect_id}' AND user_id='${t.owner.userId}'`), 1, 'audit row');
    eq(await p.getByText('Pia Purge').count(), 0, 'gone from the list');
    ok(await p.getByText('Kim Keep').isVisible(), 'other deleted contact untouched');
    eq(dbn(`SELECT COUNT(*) FROM prospects WHERE email='keep.${t.rid}@nf.example' AND deleted_at IS NOT NULL`), 1, 'other contact still soft-deleted');
  }, { u: A });

  await A.ctx.close(); await B.ctx.close();
}
if (isMain(import.meta)) standalone('new_features', run);
