// UIC-CD: ContactDetail (3-column record page)
import { Tenant, ui, go, settle, tc, skip, ok, eq, num, must, call, sql, sq, dbn, modalLoc, standalone, isMain, failApi } from './crm_common.mjs';

const newContact = (t, extra = {}) => must(t.token, 'POST', '/contacts', { email: `cd.${Math.random().toString(36).slice(2, 8)}.${t.rid}@cdco.example`, first_name: 'Dana', last_name: 'Detail', designation: 'Buyer', company_name: 'Detail Co', lifecycle_stage: 'LEAD', lead_status: 'NEW', owner_id: t.owner.userId, ...extra });
const composer = p => ({ tab: n => p.getByRole('button', { name: n, exact: true }) });

export async function run(rec, browser) {
  const t = await (await Tenant.get('contactdetail')).init();
  const alice = await t.addUser('AGENT', 'Alice');
  const mia = await t.addUser('MANAGER', 'Mia', { custom_permissions: ['manage_prospects'] });
  const bob = await t.addUser('ADMIN', 'Bob');
  const c = await newContact(t, { tags: ['seed'], phone: '+14155550100' });
  const id = c.prospect_id;
  const A = await ui(browser, t.owner); const p = A.page;
  const row = col => sql(`SELECT ${col} FROM prospects WHERE prospect_id='${id}'`);

  await tc(rec, 'UIC-CD-01', 'Record page renders header, stats, 3 columns of cards', async () => {
    await go(A, `contacts/${id}`);
    await p.getByRole('heading', { name: 'Dana Detail' }).waitFor();
    for (const h of ['About this contact', 'Details', 'Activity', 'Sales', 'Companies', 'Tasks', 'Lists', 'Campaigns']) ok(await p.getByRole('heading', { name: new RegExp(`^${h}`) }).first().isVisible(), `card ${h}`);
    for (const s of ['Emails sent', 'Opened', 'Replies', 'Calls', 'Meetings', 'Open tasks']) ok(await p.getByText(s, { exact: true }).first().isVisible(), `stat ${s}`);
    ok(await p.getByText(c.email).first().isVisible(), 'email');
    ok(await p.getByText('Buyer').first().isVisible() && await p.getByText('Detail Co').first().isVisible(), 'title + company');
  }, { u: A });

  await tc(rec, 'UIC-CD-02', 'Change lifecycle stage inline -> persisted, appears as property change with old/new', async () => {
    await go(A, `contacts/${id}`);
    await p.getByLabel('Lifecycle stage').selectOption('MQL'); await settle(A, 800);
    eq(row('lifecycle_stage'), 'MQL');
    await p.getByRole('button', { name: 'Changes' }).click(); await settle(A, 600);
    const txt = await p.locator('ul').filter({ hasText: 'Lifecycle stage changed' }).first().innerText();
    ok(/Lead/.test(txt) && /Marketing qualified lead/.test(txt), 'history old -> new: ' + txt.slice(0, 200));
    eq(dbn(`SELECT COUNT(*) FROM property_changes WHERE object_id='${id}' AND field='lifecycle_stage'`) >= 1, true, 'history row in DB');
  }, { u: A });

  await tc(rec, 'UIC-CD-03', 'Change lead status inline persisted', async () => {
    await go(A, `contacts/${id}`);
    await p.getByLabel('Lead status').selectOption('CONNECTED'); await settle(A, 700);
    eq(row('lead_status'), 'CONNECTED');
    await p.reload(); await settle(A);
    eq(await p.getByLabel('Lead status').inputValue(), 'CONNECTED', 'after reload');
  }, { u: A });

  await tc(rec, 'UIC-CD-04', 'Edit details: save changes (name, phone, title, company, location) -> DB; Cancel discards', async () => {
    await go(A, `contacts/${id}`);
    await p.getByRole('button', { name: 'Edit' }).first().click();
    await p.getByLabel('First name').fill('Danielle'); await p.getByLabel('Job title').fill('Head of Buying');
    await p.getByLabel('Company', { exact: false }).first().fill('Renamed Detail Co');
    await p.getByLabel('City').fill('Berlin'); await p.getByLabel('Country').fill('Germany');
    await p.getByRole('button', { name: 'Cancel' }).first().click();
    eq(row('first_name'), 'Dana', 'cancel does not save');
    await p.getByRole('button', { name: 'Edit' }).first().click();
    await p.getByLabel('First name').fill('Danielle'); await p.getByLabel('Job title').fill('Head of Buying');
    await p.getByLabel('Company', { exact: false }).first().fill('Renamed Detail Co');
    await p.getByLabel('City').fill('Berlin'); await p.getByLabel('Country').fill('Germany');
    await p.getByRole('button', { name: 'Save', exact: true }).click(); await settle(A, 900);
    eq(sql(`SELECT CONCAT_WS('|',first_name,designation,company_name,poc_city,poc_country) FROM prospects WHERE prospect_id='${id}'`), 'Danielle|Head of Buying|Renamed Detail Co|Berlin|Germany');
    ok(await p.getByRole('heading', { name: 'Danielle Detail' }).isVisible(), 'heading updated');
    ok(await p.getByText('Berlin, Germany').isVisible(), 'location shown');
    ok(sql(`SELECT account_id IS NOT NULL FROM prospects WHERE prospect_id='${id}'`) === '1', 'linked to renamed company account');
  }, { u: A });

  await tc(rec, 'UIC-CD-05', 'Edit details: invalid email shows an error and saves nothing', async () => {
    await go(A, `contacts/${id}`);
    await p.getByRole('button', { name: 'Edit' }).first().click();
    await p.getByLabel('Email', { exact: true }).fill('broken-email');
    await p.getByRole('button', { name: 'Save', exact: true }).click(); await settle(A, 600);
    // native type=email validation may block; either way nothing changes
    eq(row('email'), c.email, 'email unchanged');
    await p.getByRole('button', { name: 'Cancel' }).first().click();
  }, { u: A, allow: [/http 4\d\d PATCH/, /Failed to load resource/] });

  await tc(rec, 'UIC-CD-06', 'Tags: add and remove in header persists', async () => {
    await go(A, `contacts/${id}`);
    const tags = p.getByPlaceholder('Add a tag…').or(p.locator('input[list="tag-suggestions"]')).first();
    await tags.fill('vip-customer'); await tags.press('Enter'); await settle(A, 700);
    ok(row('tags').includes('vip-customer'), 'tag added: ' + row('tags'));
    await p.getByRole('button', { name: 'Remove seed' }).click(); await settle(A, 700);
    ok(!row('tags').includes('"seed"'), 'tag removed: ' + row('tags'));
  }, { u: A });

  await tc(rec, 'UIC-CD-07', 'Owner change inline persists and is recorded in history', async () => {
    await go(A, `contacts/${id}`);
    await p.locator('select').filter({ hasText: 'Unassigned' }).first().selectOption({ label: 'Bob Test' }); await settle(A, 800);
    eq(row('owner_id'), bob.userId);
    await p.getByRole('button', { name: 'Changes' }).click(); await settle(A, 500);
    ok(await p.getByText('Owner changed').first().isVisible(), 'owner change in timeline');
  }, { u: A });

  await tc(rec, 'UIC-CD-08', 'Unsubscribe then resubscribe (with confirm) updates consent + global unsubscribe list', async () => {
    await go(A, `contacts/${id}`);
    await p.locator('button:text-is("Unsubscribe")').click(); await settle(A, 800);
    ok(/Unsubscribe this contact/.test(A.lastDialog || ''), 'confirm text');
    eq(row('consent_status'), 'UNSUBSCRIBED');
    ok(dbn(`SELECT COUNT(*) FROM global_unsubscribes WHERE tenant_id='${t.tenantId}' AND email='${c.email}'`) >= 1, 'on global unsubscribe list');
    ok(await p.getByText('Unsubscribed').first().isVisible(), 'badge');
    await p.locator('button:text-is("Resubscribe")').click(); await settle(A, 800);
    eq(row('consent_status'), 'OPT_IN');
    eq(dbn(`SELECT COUNT(*) FROM global_unsubscribes WHERE tenant_id='${t.tenantId}' AND email='${c.email}'`), 0, 'removed from global list');
  }, { u: A });

  await tc(rec, 'UIC-CD-09', 'Log a note -> timeline entry + DB activity', async () => {
    await go(A, `contacts/${id}`);
    await p.getByPlaceholder('Write a note about this contact…').fill('Spoke about renewal pricing.');
    await p.getByRole('button', { name: 'Save note' }).click(); await settle(A, 800);
    ok(await p.getByText('Spoke about renewal pricing.').first().isVisible(), 'note in timeline');
    eq(dbn(`SELECT COUNT(*) FROM contact_activities WHERE prospect_id='${id}' AND activity_type='NOTE' AND body LIKE '%renewal pricing%'`), 1);
    ok(await p.getByRole('button', { name: 'Save note' }).isDisabled(), 'composer cleared');
  }, { u: A });

  await tc(rec, 'UIC-CD-10', 'Log a call (subject, outcome, duration) -> timeline + Calls stat', async () => {
    await go(A, `contacts/${id}`);
    await composer(p).tab('Log call').click();
    await p.getByPlaceholder('Call subject').fill('Intro call');
    await p.getByLabel('Outcome').selectOption('Left voicemail');
    await p.getByLabel('Duration in minutes').fill('12');
    await p.getByRole('button', { name: 'Log call', exact: true }).last().click(); await settle(A, 900);
    ok(await p.getByText('Intro call').first().isVisible(), 'call in timeline');
    ok(await p.getByText('Left voicemail · 12 min').isVisible(), 'outcome + duration shown');
    eq(sql(`SELECT CONCAT_WS('|',outcome,duration_minutes) FROM contact_activities WHERE prospect_id='${id}' AND activity_type='CALL'`), 'Left voicemail|12');
    const calls = num(await p.locator('p:has-text("Calls") + p').first().innerText());
    eq(calls, 1, 'Calls stat');
  }, { u: A });

  await tc(rec, 'UIC-CD-11', 'Log an email and a meeting', async () => {
    await go(A, `contacts/${id}`);
    await composer(p).tab('Log email').click();
    await p.getByPlaceholder('Email subject').fill('Pricing follow-up');
    await p.getByPlaceholder('What did the email say?').fill('Sent the new price sheet.');
    await p.getByRole('button', { name: 'Log email', exact: true }).last().click(); await settle(A, 800);
    ok(await p.getByText('Pricing follow-up').first().isVisible(), 'email in timeline');
    await composer(p).tab('Log meeting').click();
    await p.getByPlaceholder('Meeting title').fill('Quarterly review');
    await p.getByLabel('Duration in minutes').fill('45');
    await p.getByRole('button', { name: 'Log meeting', exact: true }).last().click(); await settle(A, 800);
    ok(await p.getByText('Quarterly review').first().isVisible(), 'meeting in timeline');
    eq(dbn(`SELECT COUNT(*) FROM contact_activities WHERE prospect_id='${id}' AND activity_type IN ('EMAIL','MEETING')`), 2);
    eq(num(await p.locator('p:has-text("Meetings") + p').first().innerText()), 1, 'Meetings stat');
  }, { u: A });

  await tc(rec, 'UIC-CD-12', 'Edit a logged activity then delete it (confirm)', async () => {
    await must(t.token, 'POST', `/contacts/${id}/activities`, { activity_type: 'NOTE', body: 'Spoke about renewal pricing.' });
    await go(A, `contacts/${id}`);
    const item = p.locator('li').filter({ hasText: 'Spoke about renewal pricing.' }).first();
    await item.locator('button:has-text("Edit")').click();
    await item.locator('textarea').fill('Spoke about renewal pricing and discounts.');
    await p.locator('li').filter({ has: p.locator('textarea') }).locator('button:has-text("Save")').click(); await settle(A, 700);
    ok(await p.getByText('renewal pricing and discounts').first().isVisible(), 'edited text shown');
    ok(dbn(`SELECT COUNT(*) FROM contact_activities WHERE prospect_id='${id}' AND body LIKE '%and discounts%'`) === 1, 'edit persisted');
    await p.locator('li').filter({ hasText: 'and discounts' }).first().locator('button:has-text("Delete")').click(); await settle(A, 700);
    // only the edited note is deleted (UIC-CD-09 may have left its own "renewal pricing" note on this contact)
    eq(dbn(`SELECT COUNT(*) FROM contact_activities WHERE prospect_id='${id}' AND body LIKE '%and discounts%'`), 0, 'deleted');
  }, { u: A });

  await tc(rec, 'UIC-CD-13', 'Create task from record (overdue due date) -> card + Open tasks stat + DB; complete it', async () => {
    await go(A, `contacts/${id}`);
    await composer(p).tab('Task').click();
    await p.getByPlaceholder('What needs doing?').fill('Send contract');
    await p.getByLabel('Due').fill('2026-10-01T09:00');
    await p.getByLabel('Priority').selectOption('HIGH');
    await p.getByRole('button', { name: 'Create task' }).click(); await settle(A, 900);
    eq(sql(`SELECT CONCAT_WS('|',title,priority,status) FROM crm_tasks WHERE prospect_id='${id}'`), 'Send contract|HIGH|OPEN');
    ok(await p.getByText(/Overdue/).first().isVisible(), 'overdue marker on task card');
    eq(num(await p.locator('p:has-text("Open tasks") + p').first().innerText()), 1, 'open tasks stat');
    await p.getByRole('button', { name: 'Complete task' }).click(); await settle(A, 900);
    eq(sql(`SELECT status FROM crm_tasks WHERE prospect_id='${id}'`), 'DONE');
    eq(num(await p.locator('p:has-text("Open tasks") + p').first().innerText()), 0, 'open tasks 0');
    await p.getByRole('button', { name: 'Reopen task' }).click(); await settle(A, 700);
    eq(sql(`SELECT status FROM crm_tasks WHERE prospect_id='${id}'`), 'OPEN', 'reopened');
  }, { u: A });

  await tc(rec, 'UIC-CD-14', 'Timeline filters: Notes / Calls / Changes / All and user filter', async () => {
    await go(A, `contacts/${id}`);
    await p.getByRole('button', { name: 'Calls', exact: true }).click(); await settle(A, 600);
    ok(await p.getByText('Intro call').first().isVisible(), 'call visible under Calls');
    eq(await p.getByText('Pricing follow-up').count(), 0, 'emails hidden under Calls');
    await p.getByRole('button', { name: 'Changes', exact: true }).click(); await settle(A, 600);
    ok(await p.getByText(/changed|Created/).first().isVisible(), 'changes visible');
    await p.getByRole('button', { name: 'All', exact: true }).click(); await settle(A, 600);
    ok(await p.getByText('Intro call').first().isVisible() && await p.getByText('Pricing follow-up').first().isVisible(), 'all visible');
    await p.getByLabel('Filter by user').selectOption({ label: 'Alice Test' }); await settle(A, 700);
    eq(await p.getByText('Intro call').count(), 0, 'filtered to another user shows nothing of ours');
  }, { u: A });

  await tc(rec, 'UIC-CD-15', 'Create lead from the record -> redirected to lead, lead in DB, Sales card lists it', async () => {
    await go(A, `contacts/${id}`);
    await p.getByRole('button', { name: '+ Create lead' }).click();
    await modalLoc(p).getByLabel('Lead source').fill('Referral');
    await modalLoc(p).getByRole('button', { name: 'Create lead' }).click();
    await p.waitForURL(/\/app\/leads\/[0-9a-f-]{36}/, { timeout: 10000 });
    eq(sql(`SELECT CONCAT_WS('|',stage,source,owner_id) FROM leads WHERE prospect_id='${id}'`), `NEW|Referral|${row('owner_id')}`);
    await go(A, `contacts/${id}`);
    ok(await p.getByText(/Lead ·/).first().isVisible(), 'sales card shows the lead');
    eq(await p.getByRole('button', { name: '+ Create lead' }).count(), 0, 'no second create while open lead exists');
  }, { u: A });

  // UIC-CD-16 (was BLOCKED not-implemented): the feature now exists and is covered by UIC-NF-10 in new_features.mjs
  // UIC-CD-17 (was BLOCKED not-implemented): the feature now exists and is covered by UIC-NF-05/06 in new_features.mjs

  await tc(rec, 'UIC-CD-18', 'Associations: company link opens the company page; list + campaign cards link out', async () => {
    const sel = sql(`SELECT UUID()`);
    const camp = sql(`SELECT UUID()`);
    sql(`INSERT INTO campaigns (campaign_id,tenant_id,campaign_name,status,created_by,created_at) VALUES ('${camp}','${t.tenantId}','Assoc Campaign','DRAFT','${t.owner.userId}',NOW())`);
    await must(t.token, 'POST', '/lists', { list_name: 'Assoc list', list_type: 'STATIC' }).then(l => must(t.token, 'POST', `/lists/${l.list_id}/members`, { prospect_ids: [id] }));
    await must(t.token, 'POST', '/contacts/bulk', { prospect_ids: [id], action: 'enroll', campaign_id: camp });
    await go(A, `contacts/${id}`);
    ok(await p.getByRole('link', { name: 'Assoc list' }).isVisible(), 'list link');
    ok(await p.getByRole('link', { name: 'Assoc Campaign' }).first().isVisible(), 'campaign link');
    await p.locator('a[href*="/app/accounts/"]').first().click();
    await p.waitForURL(/\/app\/accounts\//, { timeout: 8000 });
    ok(await p.getByRole('heading', { name: new RegExp('^' + row('company_name')) }).first().isVisible(), 'company page');
  }, { u: A });

  await tc(rec, 'UIC-CD-19', 'Unknown contact id shows "Contact not found" with back link', async () => {
    await go(A, 'contacts/00000000-0000-0000-0000-000000000000');
    ok(await p.getByText('Contact not found').isVisible(), 'not found');
    await p.getByRole('link', { name: /Back to contacts/ }).click();
    await p.waitForURL(/\/app\/contacts$/);
  }, { u: A, allow: [/http 404/, /Failed to load resource/] });

  await tc(rec, 'UIC-CD-20', 'Property save API failure shows the error and keeps the old value', async () => {
    await go(A, `contacts/${id}`);
    await failApi(p, /\/contacts\/[0-9a-f-]{36}$/, 500, 'PATCH');
    const before = row('lead_status');
    await p.getByLabel('Lead status').selectOption('OPEN'); await settle(A, 800);
    eq(row('lead_status'), before, 'DB unchanged');
    ok(await p.locator('.bg-red-50').count() > 0, 'an error message is shown to the user');
    await p.unrouteAll({ behavior: 'ignoreErrors' });
  }, { u: A, allow: [/http 500/, /Failed to load resource/] });

  // ---- other roles on the record ----
  const AG = await ui(browser, alice); const pa = AG.page;
  const own = await must(t.token, 'POST', '/contacts', { email: `alice.own.${t.rid}@cdco.example`, first_name: 'Own', last_name: 'ByAlice', owner_id: alice.userId, lifecycle_stage: 'LEAD' });
  await tc(rec, 'UIC-CD-21', 'AGENT on own contact: can edit/log note, owner shown read-only, no Delete', async () => {
    await go(AG, `contacts/${own.prospect_id}`);
    eq(await pa.getByRole('button', { name: /Delete contact/ }).count(), 0, 'no delete for agent');
    ok(await pa.getByText('Alice Test').first().isVisible(), 'owner shown as text');
    await pa.getByPlaceholder('Write a note about this contact…').fill('Agent note');
    await pa.getByRole('button', { name: 'Save note' }).click(); await settle(AG, 800);
    eq(dbn(`SELECT COUNT(*) FROM contact_activities WHERE prospect_id='${own.prospect_id}' AND body='Agent note'`), 1);
    await pa.getByLabel('Lifecycle stage').selectOption('MQL'); await settle(AG, 700);
    eq(sql(`SELECT lifecycle_stage FROM prospects WHERE prospect_id='${own.prospect_id}'`), 'MQL');
  }, { u: AG });

  await tc(rec, 'UIC-CD-22', 'Non-admin manager cannot move lifecycle stage backwards (forward only message)', async () => {
    const M = await ui(browser, mia);
    const mc = await must(t.token, 'POST', '/contacts', { email: `mia.own.${t.rid}@cdco.example`, first_name: 'Mia', last_name: 'Own', owner_id: mia.userId, lifecycle_stage: 'SQL' });
    await go(M, `contacts/${mc.prospect_id}`);
    ok(await M.page.getByText('Moves forward only').isVisible(), 'hint shown');
    await M.page.getByLabel('Lifecycle stage').selectOption('LEAD'); await settle(M, 800);
    eq(sql(`SELECT lifecycle_stage FROM prospects WHERE prospect_id='${mc.prospect_id}'`), 'SQL', 'unchanged');
    ok(await M.page.locator('.bg-red-50').filter({ hasText: /forward/i }).count() > 0, 'forward-only message shown');
    await M.ctx.close();
  }, { u: AG, allow: [/http 4\d\d PATCH/, /Failed to load resource/] });

  await tc(rec, 'UIC-CD-23', 'Delete contact (confirm) -> back to list; listed in Recently deleted; restore brings it back', async () => {
    const d = await newContact(t, { first_name: 'Del', last_name: 'Me' });
    await go(A, `contacts/${d.prospect_id}`);
    await p.getByRole('button', { name: /Delete contact/ }).click();
    await p.waitForURL(/\/app\/contacts$/, { timeout: 8000 });
    ok(sql(`SELECT deleted_at FROM prospects WHERE prospect_id='${d.prospect_id}'`) !== 'NULL', 'soft deleted');
    await go(A, 'recently-deleted');
    ok(await p.getByText('Del Me').first().isVisible(), 'in Recently deleted');
    await p.locator('tr').filter({ hasText: 'Del Me' }).getByRole('button', { name: 'Restore' }).click(); await settle(A, 800);
    eq(sql(`SELECT deleted_at FROM prospects WHERE prospect_id='${d.prospect_id}'`), 'NULL', 'restored');
  }, { u: A });

  await A.ctx.close(); await AG.ctx.close();
}
if (isMain(import.meta)) standalone('contact_detail', run);
