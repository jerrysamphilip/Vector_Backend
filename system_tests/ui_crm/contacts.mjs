// UIC-CON: Contacts list, create modal, filters, views, board, bulk actions, export, duplicates, properties; ContactDetail UIC-CD
import { Tenant, ui, go, settle, tc, skip, ok, eq, near, num, seedContacts, must, call, sql, sq, dbn, downloadVia, modalLoc, standalone, isMain, API, dnd, failApi } from './crm_common.mjs';
import { execFileSync } from 'node:child_process';

const dbc = (t, where = '1=1') => dbn(`SELECT COUNT(*) FROM prospects WHERE tenant_id='${t.tenantId}' AND deleted_at IS NULL AND (${where})`);
const rows = page => page.locator('table tbody tr');
const totalText = async page => (await page.getByText(/\d[\d,]* contacts?$/).first().innerText().catch(() => '')).trim();
async function listTotal(page) { const t = await page.getByText(/^[\d,]+ contacts?$/).first().innerText({ timeout: 8000 }).catch(() => ''); const m = t.match(/([\d,]+) contacts?/); return m ? Number(m[1].replace(/,/g, '')) : -1; }
async function openFilters(page) { await page.getByRole('button', { name: /^Filters/ }).click(); }
async function addCond(page, i, prop, op, val) {
  const panel = page; // filter panel lives in the toolbar
  await page.getByRole('button', { name: /^Condition$|Condition/ }).first().click();
  const p = page.getByLabel('Property').nth(i);
  await p.selectOption({ label: prop });
  if (op) await page.getByLabel('Operator').nth(i).selectOption({ label: op });
  if (val !== undefined && val !== null) {
    const v = page.getByLabel('Operator').nth(i).locator('xpath=following-sibling::*[1]');
    const tag = await v.evaluate(e => e.tagName);
    if (tag === 'SELECT') { try { await v.selectOption({ label: val }, { timeout: 2500 }); } catch { await v.selectOption({ value: val }); } } else await v.fill(String(val));
  }
}
async function applyFilters(page) { await page.getByRole('button', { name: 'Apply filters' }).click(); await settle({ page }, 700); }
async function clearFilters(page) { if (!(await page.getByRole('button', { name: 'Clear all' }).isVisible().catch(() => false))) await openFilters(page); await page.getByRole('button', { name: 'Clear all' }).click(); await settle({ page }, 600); }

export async function run(rec, browser) {
  const t = await (await Tenant.get('contacts')).init();
  const alice = await t.addUser('AGENT', 'Alice');
  const bob = await t.addUser('ADMIN', 'Bob');
  const contacts = await seedContacts(t, 60, { owners: [t.owner.userId, alice.userId, bob.userId], companies: ['Acme Corp', 'Globex', 'Initech', 'Umbrella', 'Hooli', 'Stark', 'Wayne', 'Wonka'] });
  // 12 unassigned
  sql(`UPDATE prospects SET owner_id=NULL WHERE tenant_id='${t.tenantId}' AND first_name IN (${contacts.slice(48).map(c => sq(c.first_name)).join(',')})`);
  // campaign (DRAFT) for enrollment
  const campId = sql(`SELECT UUID()`);
  sql(`INSERT INTO campaigns (campaign_id,tenant_id,campaign_name,status,created_by,created_at) VALUES ('${campId}','${t.tenantId}','Q4 Outreach','DRAFT','${t.owner.userId}',NOW())`);
  const A = await ui(browser, t.owner);
  const p = A.page;

  // ---------------- list ----------------
  await tc(rec, 'UIC-CON-01', 'Contacts list renders 25/60 with pager text and header', async () => {
    await go(A, 'contacts');
    await p.getByRole('heading', { name: 'Contacts', exact: true }).waitFor();
    eq(await rows(p).count(), 25, 'rows page 1');
    eq(await listTotal(p), 60, 'total');
    ok(await p.getByText('1–25 of 60').isVisible(), 'pager text');
    ok(await p.getByText('Page 1 of 3').isVisible(), 'page of');
    const first = await rows(p).first().innerText();
    ok(/Person|Seed/.test(first), 'row content ' + first.slice(0, 60));
  }, { u: A });

  await tc(rec, 'UIC-CON-02', 'Pagination next/prev/last page counts and disabled state', async () => {
    await go(A, 'contacts');
    await p.getByRole('button', { name: 'Next page' }).click(); await settle(A);
    ok(await p.getByText('26–50 of 60').isVisible(), 'page 2 text');
    eq(await rows(p).count(), 25);
    await p.getByRole('button', { name: 'Next page' }).click(); await settle(A);
    ok(await p.getByText('51–60 of 60').isVisible(), 'page 3 text');
    eq(await rows(p).count(), 10);
    ok(await p.getByRole('button', { name: 'Next page' }).isDisabled(), 'next disabled on last');
    await p.getByRole('button', { name: 'Previous page' }).click(); await settle(A);
    ok(await p.getByText('Page 2 of 3').isVisible(), 'back to 2');
    ok(/page=2/.test(p.url()) , 'page in URL');
  }, { u: A });

  await tc(rec, 'UIC-CON-03', 'Sort by name asc/desc matches API order', async () => {
    await go(A, 'contacts');
    const nameBtn = p.locator('thead').getByRole('button', { name: /^name/i });
    await nameBtn.click(); await settle(A);   // created_at desc default -> name asc
    const asc = await must(t.token, 'GET', '/contacts?sort_by=name&sort_order=asc&page_size=25');
    const firstName = (await rows(p).first().locator('td').nth(1).locator('p').first().innerText()).trim();
    eq(firstName, asc.items[0].full_name, 'asc first');
    await nameBtn.click(); await settle(A);
    const desc = await must(t.token, 'GET', '/contacts?sort_by=name&sort_order=desc&page_size=25');
    const f2 = (await rows(p).first().locator('td').nth(1).locator('p').first().innerText()).trim();
    eq(f2, desc.items[0].full_name, 'desc first');
    ok(/sort_order=desc/.test(p.url()), 'url sort');
  }, { u: A });

  await tc(rec, 'UIC-CON-04', 'Sort by Email and Company headers', async () => {
    await go(A, 'contacts');
    await p.locator('thead').getByRole('button', { name: /^email/i }).click(); await settle(A);
    const api = await must(t.token, 'GET', '/contacts?sort_by=email&sort_order=asc&page_size=25');
    const txt = await rows(p).first().innerText();
    ok(txt.includes(api.items[0].email), `first row should be ${api.items[0].email}: ${txt.slice(0, 80)}`);
    await p.locator('thead').getByRole('button', { name: /^company/i }).click(); await settle(A);
    const api2 = await must(t.token, 'GET', '/contacts?sort_by=company&sort_order=asc&page_size=25');
    const txt2 = await rows(p).first().innerText();
    ok(txt2.includes(api2.items[0].email), 'company sort first row');
  }, { u: A });

  await tc(rec, 'UIC-CON-05', 'Search box filters by name/email/company; empty-state; clear', async () => {
    await go(A, 'contacts');
    const box = p.getByPlaceholder(/Search name, email/);
    await box.fill('Seed7'); await settle(A, 900);
    const exp = dbc(t, `search_text LIKE '%seed7%' OR first_name LIKE 'Seed7%'`);
    const got = await listTotal(p);
    ok(got > 0 && got === exp, `search Seed7 total ${got} vs db ${exp}`);
    await box.fill('Globex'); await settle(A, 900);
    eq(await listTotal(p), dbc(t, `company_name='Globex'`), 'company search');
    await box.fill('zzz-nomatch-xyz'); await settle(A, 900);
    ok(await p.getByText('No contacts match').isVisible(), 'empty state');
    await p.getByRole('button', { name: 'Clear search' }).click(); await settle(A, 900);
    eq(await listTotal(p), 60, 'cleared');
  }, { u: A });

  await tc(rec, 'UIC-CON-06', 'Column chooser hides/shows columns, persists, Reset restores', async () => {
    await go(A, 'contacts');
    ok(await p.locator('thead').getByText('Tags', { exact: false }).count() > 0, 'tags column initially');
    await p.getByRole('button', { name: 'Columns' }).click();
    await p.getByLabel('Tags', { exact: true }).uncheck();
    await p.getByRole('button', { name: 'Done' }).click();
    eq(await p.locator('thead').getByText(/^tags$/i).count(), 0, 'tags hidden');
    await p.reload(); await settle(A);
    eq(await p.locator('thead').getByText(/^tags$/i).count(), 0, 'persisted after reload');
    await p.getByRole('button', { name: 'Columns' }).click();
    await p.getByLabel('Lead source').check();
    await p.getByRole('button', { name: 'Done' }).click();
    ok(await p.locator('thead').getByText(/lead source/i).count() > 0, 'lead source shown');
    await p.getByRole('button', { name: 'Columns' }).click();
    await p.getByRole('button', { name: 'Reset' }).click();
    await p.getByRole('button', { name: 'Done' }).click();
    ok(await p.locator('thead').getByText(/^tags$/i).count() > 0, 'reset restores tags');
    eq(await p.locator('thead').getByText(/lead source/i).count(), 0, 'reset removes lead source');
  }, { u: A });

  // ---------------- advanced filters ----------------
  await tc(rec, 'UIC-CON-07', 'Filter builder: enum (lifecycle stage is Lead) equals DB count', async () => {
    await go(A, 'contacts'); await openFilters(p);
    await addCond(p, 0, 'Lifecycle stage', 'is', 'Lead');
    await applyFilters(p);
    eq(await listTotal(p), dbc(t, `lifecycle_stage='LEAD'`), 'stage LEAD');
    ok(await p.getByRole('button', { name: /Filters \(1\)/ }).isVisible(), 'badge (1)');
  }, { u: A });

  await tc(rec, 'UIC-CON-08', 'Filter builder: AND of two conditions (stage + country text)', async () => {
    await go(A, 'contacts'); await openFilters(p);
    await addCond(p, 0, 'Lifecycle stage', 'is', 'Lead');
    await addCond(p, 1, 'Country', 'is', 'India');
    await applyFilters(p);
    eq(await listTotal(p), dbc(t, `lifecycle_stage='LEAD' AND poc_country='India'`), 'AND');
  }, { u: A });

  await tc(rec, 'UIC-CON-09', 'Filter builder: toggle AND->OR changes result to union', async () => {
    await go(A, 'contacts'); await openFilters(p);
    await addCond(p, 0, 'Lifecycle stage', 'is', 'Lead');
    await addCond(p, 1, 'Country', 'is', 'India');
    await p.getByRole('button', { name: 'AND', exact: true }).click();
    await applyFilters(p);
    eq(await listTotal(p), dbc(t, `lifecycle_stage='LEAD' OR poc_country='India'`), 'OR');
  }, { u: A });

  await tc(rec, 'UIC-CON-10', 'Filter builder: tag has tag "hot"; "has no tags"', async () => {
    await go(A, 'contacts'); await openFilters(p);
    await addCond(p, 0, 'Tags', 'has tag', 'hot');
    await applyFilters(p);
    eq(await listTotal(p), dbc(t, `JSON_CONTAINS(tags,'"hot"')`), 'tag hot');
    await openFilters(p);
    await p.getByLabel('Operator').nth(0).selectOption({ label: 'has no tags' });
    await applyFilters(p);
    eq(await listTotal(p), dbc(t, `tags IS NULL OR JSON_LENGTH(tags)=0`), 'no tags');
  }, { u: A });

  await tc(rec, 'UIC-CON-11', 'Filter builder: text contains / starts with / is empty', async () => {
    await go(A, 'contacts'); await openFilters(p);
    await addCond(p, 0, 'Job title', 'contains', 'Sales');
    await applyFilters(p);
    eq(await listTotal(p), dbc(t, `designation LIKE '%Sales%'`), 'contains');
    await openFilters(p);
    await p.getByLabel('Operator').nth(0).selectOption({ label: 'starts with' });
    await p.getByLabel('Operator').nth(0).locator('xpath=following-sibling::*[1]').fill('VP');
    await applyFilters(p);
    eq(await listTotal(p), dbc(t, `designation LIKE 'VP%'`), 'starts with');
    await openFilters(p);
    await p.getByLabel('Property').nth(0).selectOption({ label: 'Mobile' });
    await p.getByLabel('Operator').nth(0).selectOption({ label: 'is empty' });
    await applyFilters(p);
    eq(await listTotal(p), dbc(t, `mobile_phone IS NULL OR mobile_phone=''`), 'is empty');
  }, { u: A });

  await tc(rec, 'UIC-CON-12', 'Filter builder: date "in the last N days" and owner "Me"', async () => {
    await go(A, 'contacts'); await openFilters(p);
    await addCond(p, 0, 'Create date', 'in the last (days)', 7);
    await applyFilters(p);
    eq(await listTotal(p), dbc(t, `created_at >= NOW() - INTERVAL 7 DAY`), 'last 7');
    await openFilters(p);
    await p.getByLabel('Property').nth(0).selectOption({ label: 'Owner' });
    await p.getByLabel('Operator').nth(0).locator('xpath=following-sibling::*[1]').selectOption({ label: 'Me' });
    await applyFilters(p);
    eq(await listTotal(p), dbc(t, `owner_id='${t.owner.userId}'`), 'owner me');
  }, { u: A });

  await tc(rec, 'UIC-CON-13', 'Filter builder: nested group  stage=Lead AND (country=US OR tag=hot)', async () => {
    await go(A, 'contacts'); await openFilters(p);
    await addCond(p, 0, 'Lifecycle stage', 'is', 'Lead');
    await p.getByRole('button', { name: 'Group' }).click();
    // group's first condition was auto-created as lifecycle stage; reconfigure it
    await p.getByLabel('Property').nth(1).selectOption({ label: 'Country' });
    await p.getByLabel('Operator').nth(1).selectOption({ label: 'is' });
    await p.getByLabel('Operator').nth(1).locator('xpath=following-sibling::*[1]').fill('US');
    await p.getByRole('button', { name: 'Condition', exact: true }).first().click();   // nested group's button precedes the top-level one in DOM order
    await p.getByLabel('Property').nth(2).selectOption({ label: 'Tags' });
    await p.getByLabel('Operator').nth(2).selectOption({ label: 'has tag' });
    await p.getByLabel('Operator').nth(2).locator('xpath=following-sibling::*[1]').selectOption({ label: 'hot' });
    ok(await p.getByRole('button', { name: 'OR', exact: true }).count() === 1, 'a new group starts with OR between its conditions');
    await applyFilters(p);
    const urlF = decodeURIComponent(p.url().split('filters=')[1] || '');
    eq(await listTotal(p), dbc(t, `lifecycle_stage='LEAD' AND (poc_country='US' OR JSON_CONTAINS(tags,'"hot"'))`), 'nested; filters=' + urlF);
  }, { u: A });

  await tc(rec, 'UIC-CON-14', 'Filter builder: remove condition / Clear all resets to 60', async () => {
    await go(A, 'contacts'); await openFilters(p);
    await addCond(p, 0, 'Lifecycle stage', 'is', 'Lead'); await applyFilters(p);
    ok(await listTotal(p) < 60, 'filtered');
    await clearFilters(p);
    eq(await listTotal(p), 60, 'cleared');
    ok(!/filters=/.test(decodeURIComponent(p.url())), 'filters removed from URL');
  }, { u: A });

  // ---------------- views ----------------
  await tc(rec, 'UIC-CON-15', 'Built-in views from sidebar: My contacts / Unassigned / Recently created', async () => {
    await go(A, 'contacts');
    await p.getByRole('link', { name: 'My contacts' }).click(); await settle(A, 900);
    eq(await listTotal(p), dbc(t, `owner_id='${t.owner.userId}'`), 'mine');
    await p.getByRole('link', { name: 'Unassigned' }).click(); await settle(A, 900);
    eq(await listTotal(p), dbc(t, `owner_id IS NULL`), 'unassigned');
    await p.getByRole('link', { name: 'Recently created' }).click(); await settle(A, 900);
    eq(await listTotal(p), dbc(t, `created_at >= NOW() - INTERVAL 30 DAY`), 'recent');
    await p.getByRole('link', { name: 'All contacts' }).click(); await settle(A, 900);
    eq(await listTotal(p), 60, 'all');
  }, { u: A });

  let viewId = null;
  await tc(rec, 'UIC-CON-16', 'Save filtered view -> appears in sidebar & DB; reopening applies filter', async () => {
    await go(A, 'contacts'); await openFilters(p);
    await addCond(p, 0, 'Lifecycle stage', 'is', 'MQL'); await applyFilters(p);
    await p.getByRole('button', { name: /Save view/ }).click();
    const m = modalLoc(p);
    await m.getByLabel('Name').fill('MQL pipeline');
    await m.getByRole('button', { name: 'Save', exact: true }).click(); await settle(A, 900);
    viewId = sql(`SELECT view_id FROM saved_views WHERE tenant_id='${t.tenantId}' AND name='MQL pipeline'`);
    ok(viewId, 'view row in DB');
    ok(await p.getByRole('link', { name: 'MQL pipeline' }).isVisible(), 'sidebar link');
    await go(A, 'contacts');
    await p.getByRole('link', { name: 'MQL pipeline' }).click(); await settle(A, 900);
    eq(await listTotal(p), dbc(t, `lifecycle_stage='MQL'`), 'view applies filter');
  }, { u: A });

  await tc(rec, 'UIC-CON-17', 'Rename saved view (Update this view) and persisted', async () => {
    await go(A, `contacts?view=${viewId}&apply=1`); await settle(A, 800);
    await openFilters(p);
    await p.getByLabel('Operator').nth(0).locator('xpath=following-sibling::*[1]').selectOption({ label: 'Lead' });
    await applyFilters(p);
    await p.getByRole('button', { name: /Save view/ }).click();
    const m = modalLoc(p);
    ok(await m.getByText('Update this view').isVisible(), 'update radio');
    await m.getByLabel('Name').fill('Lead pipeline');
    await m.getByRole('button', { name: 'Save', exact: true }).click(); await settle(A, 900);
    eq(sql(`SELECT name FROM saved_views WHERE view_id='${viewId}'`), 'Lead pipeline', 'renamed in DB');
    ok(sql(`SELECT filters FROM saved_views WHERE view_id='${viewId}'`).includes('LEAD'), 'filters updated in DB');
    ok(await p.getByRole('link', { name: 'Lead pipeline' }).isVisible(), 'sidebar renamed');
  }, { u: A });

  await tc(rec, 'UIC-CON-18', 'Save-view modal: empty name disables Save; Cancel closes', async () => {
    await go(A, 'contacts');
    await p.getByRole('button', { name: /Save as view/ }).click();
    const m = modalLoc(p);
    ok(await m.getByRole('button', { name: 'Save', exact: true }).isDisabled(), 'save disabled with empty name');
    await m.getByRole('button', { name: 'Cancel' }).click();
    eq(await p.getByText('Filters, columns and sort').count(), 0, 'modal closed');
  }, { u: A });

  await tc(rec, 'UIC-CON-19', 'Delete saved view removes it from sidebar and DB', async () => {
    await go(A, `contacts?view=${viewId}&apply=1`); await settle(A, 800);
    await p.getByRole('button', { name: 'Delete view' }).click(); await settle(A, 900);
    eq(dbn(`SELECT COUNT(*) FROM saved_views WHERE view_id='${viewId}'`), 0, 'deleted in DB');
    eq(await p.getByRole('link', { name: 'Lead pipeline' }).count(), 0, 'gone from sidebar');
  }, { u: A });

  // ---------------- board ----------------
  await tc(rec, 'UIC-CON-20', 'Board view: lane totals equal DB; drag card to another stage persists', async () => {
    await go(A, 'contacts?mode=board'); await settle(A, 800);
    const lanes = p.locator('div.w-64.flex-shrink-0');
    ok(await lanes.count() >= 6, 'lanes rendered ' + await lanes.count());
    const leadLane = lanes.filter({ hasText: /^Lead/i }).first();
    const lt = num(await leadLane.locator('span.font-semibold').first().innerText());
    eq(lt, dbc(t, `lifecycle_stage='LEAD'`), 'lead lane total');
    // drag the first Subscriber card into Lead (forward move)
    const subLane = lanes.filter({ hasText: /^Subscriber/i }).first();
    const card = subLane.locator('div[draggable=true]').first();
    const name = (await card.locator('p').first().innerText()).trim();
    await dnd(p, card, leadLane); await settle(A, 900);
    const stage = sql(`SELECT lifecycle_stage FROM prospects WHERE tenant_id='${t.tenantId}' AND CONCAT(first_name,' ',last_name)=${sq(name)}`);
    eq(stage, 'LEAD', `dragged ${name} stage`);
  }, { u: A });

  await tc(rec, 'UIC-CON-21', 'Board: admin drags a card backwards (allowed for admins, BR-CM-08) and it persists', async () => {
    await go(A, 'contacts?mode=board'); await settle(A, 800);
    const lanes = p.locator('div.w-64.flex-shrink-0');
    const cust = lanes.filter({ hasText: /^Customer/i }).first();
    const card = cust.locator('div[draggable=true]').first();
    const name = (await card.locator('p').first().innerText()).trim();
    await dnd(p, card, lanes.filter({ hasText: /^Subscriber/i }).first()); await settle(A, 900);
    eq(sql(`SELECT lifecycle_stage FROM prospects WHERE tenant_id='${t.tenantId}' AND CONCAT(first_name,' ',last_name)=${sq(name)}`), 'SUBSCRIBER', 'admin backward move');
  }, { u: A });

  await tc(rec, 'UIC-CON-22', 'Board: group by lead status and switch back to table', async () => {
    await go(A, 'contacts?mode=board'); await settle(A, 600);
    await p.locator('select').filter({ hasText: 'Lifecycle stage' }).first().selectOption('lead_status'); await settle(A, 800);
    ok(await p.locator('div.w-64.flex-shrink-0').filter({ hasText: /^New/i }).count() > 0, 'status lanes');
    await p.getByRole('button', { name: 'Table view' }).click(); await settle(A, 800);
    eq(await rows(p).count(), 25, 'back to table');
  }, { u: A });

  // ---------------- create ----------------
  await tc(rec, 'UIC-CON-23', 'Add contact: empty email -> validation message, nothing created', async () => {
    await go(A, 'contacts');
    await p.getByRole('button', { name: /Add contact/ }).first().click();
    const m = modalLoc(p);
    await m.getByLabel('First name').fill('NoEmail');
    await m.getByRole('button', { name: /Add contact/ }).click();
    ok(await m.getByText('Email is required').isVisible(), 'required message');
    eq(dbn(`SELECT COUNT(*) FROM prospects WHERE tenant_id='${t.tenantId}' AND first_name='NoEmail'`), 0);
  }, { u: A });

  await tc(rec, 'UIC-CON-24', 'Add contact: invalid email format -> clear error, nothing created', async () => {
    const m = modalLoc(p);
    await m.getByLabel('Email *').fill('not-an-email');
    await m.getByRole('button', { name: /Add contact/ }).click(); await settle(A, 500);
    const txt = await m.innerText();
    ok(/email/i.test(txt) && /valid|invalid|@/i.test(txt) && !/\[object/.test(txt), 'error text: ' + txt.slice(0, 200));
    eq(dbn(`SELECT COUNT(*) FROM prospects WHERE tenant_id='${t.tenantId}' AND email='not-an-email'`), 0);
  }, { u: A, allow: [/http 4\d\d POST .*\/contacts/, /Failed to load resource/] });

  let newId = null;
  await tc(rec, 'UIC-CON-25', 'Add contact: full form creates contact, company account linked, tags, redirect to detail', async () => {
    const m = modalLoc(p);
    await m.getByLabel('Email *').fill(`brand.new.${t.rid}@newco.example`);
    await m.getByLabel('First name').fill('Brand'); await m.getByLabel('Last name').fill('New');
    await m.getByLabel('Phone', { exact: true }).fill('+14155550199'); await m.getByLabel('Job title').fill('Head of Ops');
    await m.getByLabel('Company', { exact: false }).first().fill('Newco Industries');
    await m.getByLabel('City').fill('Austin'); await m.getByLabel('Country').fill('US');
    await m.getByLabel('Lead source').fill('Trade show');
    const tags = m.getByPlaceholder('Add a tag…'); await tags.fill('vip'); await tags.press('Enter');
    await m.getByRole('button', { name: /Add contact/ }).click();
    await p.waitForURL(/\/app\/contacts\/[0-9a-f-]{36}/, { timeout: 10000 });
    newId = p.url().split('/').pop();
    const row = sql(`SELECT CONCAT_WS('|',first_name,last_name,designation,company_name,poc_city,lead_source,JSON_UNQUOTE(JSON_EXTRACT(tags,'$[0]')),IF(account_id IS NULL,'noacct','acct'),owner_id) FROM prospects WHERE prospect_id='${newId}'`);
    eq(row, `Brand|New|Head of Ops|Newco Industries|Austin|Trade show|vip|acct|${t.owner.userId}`, 'DB row');
    eq(dbn(`SELECT COUNT(*) FROM accounts WHERE tenant_id='${t.tenantId}' AND name='Newco Industries' AND deleted_at IS NULL`), 1, 'account created');
    ok(await p.getByRole('heading', { name: 'Brand New' }).isVisible(), 'detail page heading');
  }, { u: A });

  await tc(rec, 'UIC-CON-26', 'Add contact: duplicate email -> error + "Open the existing contact" link works', async () => {
    await go(A, 'contacts');
    await p.getByRole('button', { name: /Add contact/ }).first().click();
    const m = modalLoc(p);
    await m.getByLabel('Email *').fill(`brand.new.${t.rid}@newco.example`);
    await m.getByRole('button', { name: /Add contact/ }).click(); await settle(A, 500);
    ok(await m.getByText(/already exists/i).isVisible(), 'duplicate message');
    await m.getByRole('button', { name: /Open the existing contact/ }).click();
    await p.waitForURL(new RegExp(newId), { timeout: 8000 });
    eq(dbn(`SELECT COUNT(*) FROM prospects WHERE tenant_id='${t.tenantId}' AND email='brand.new.${t.rid}@newco.example'`), 1, 'still one');
  }, { u: A, allow: [/http 409 POST .*\/contacts/, /Failed to load resource/] });

  await tc(rec, 'UIC-CON-27', 'Add contact: email of a deleted contact -> restore hint (Recently deleted)', async () => {
    const victim = contacts[5];
    await must(t.token, 'DELETE', `/contacts/${victim.prospect_id}`, undefined, [200, 204]);
    await go(A, 'contacts');
    await p.getByRole('button', { name: /Add contact/ }).first().click();
    const m = modalLoc(p);
    await m.getByLabel('Email *').fill(victim.email);
    await m.getByRole('button', { name: /Add contact/ }).click(); await settle(A, 500);
    ok(await m.getByText(/Restore it from Recently deleted/i).isVisible(), 'restore hint: ' + (await m.innerText()).slice(-200));
  }, { u: A, allow: [/http 409 POST .*\/contacts/, /Failed to load resource/] });

  await tc(rec, 'UIC-CON-28', 'Add contact modal: Cancel / X close paths, no row created', async () => {
    await go(A, 'contacts');
    const before = await listTotal(p);
    await p.getByRole('button', { name: /Add contact/ }).first().click();
    let m = modalLoc(p); await m.getByLabel('Email *').fill('cancel.me@x.example');
    await m.getByRole('button', { name: 'Cancel' }).click();
    eq(await p.getByText('Create a single contact without uploading a file').count(), 0, 'closed by cancel');
    await p.getByRole('button', { name: /Add contact/ }).first().click();
    await modalLoc(p).getByRole('button', { name: 'Close' }).click();
    eq(await p.getByText('Create a single contact without uploading a file').count(), 0, 'closed by X');
    eq(dbn(`SELECT COUNT(*) FROM prospects WHERE email='cancel.me@x.example'`), 0);
    eq(await listTotal(p), before);
  }, { u: A });

  // custom properties
  await tc(rec, 'UIC-CON-29', 'Properties modal: create NUMBER and SELECT custom fields; appear in DB and Add form', async () => {
    await go(A, 'contacts');
    await p.getByRole('button', { name: 'Properties' }).click();
    // PropertySettingsModal (bd5f153): "Add property", one input per choice ("Choice N" + "Add choice")
    const m = modalLoc(p);
    await m.getByLabel('Label', { exact: true }).fill('Budget'); await m.getByLabel('Type').selectOption('NUMBER');
    await m.getByRole('button', { name: 'Add property' }).click(); await settle(A, 500);
    await m.getByLabel('Label', { exact: true }).fill('Tier'); await m.getByLabel('Type').selectOption('SELECT');
    for (const [i, c] of ['Gold', 'Silver', 'Bronze'].entries()) {
      if (i) await m.getByRole('button', { name: 'Add choice' }).click();
      await m.getByLabel(`Choice ${i + 1}`, { exact: true }).fill(c);
    }
    await m.getByRole('button', { name: 'Add property' }).click(); await settle(A, 500);
    ok(await m.getByText('Tier').first().isVisible() && await m.getByText('Gold, Silver, Bronze').isVisible(), 'listed');
    eq(dbn(`SELECT COUNT(*) FROM contact_field_definitions WHERE tenant_id='${t.tenantId}' AND label IN ('Budget','Tier')`), 2, 'defs in DB');
    await m.getByRole('button', { name: 'Done' }).click();
  }, { u: A });

  await tc(rec, 'UIC-CON-30', 'Properties modal: blank label disables Add; duplicate label error', async () => {
    await go(A, 'contacts');
    await p.getByRole('button', { name: 'Properties' }).click();
    const m = modalLoc(p);
    ok(await m.getByRole('button', { name: 'Add property' }).isDisabled(), 'disabled blank');
    await m.getByLabel('Label', { exact: true }).fill('Budget'); await m.getByRole('button', { name: 'Add property' }).click(); await settle(A, 500);
    const dup = (await must(t.token, 'GET', '/contacts/fields')).filter(f => f.label === 'Budget');
    for (const f of dup.slice(1)) await call(t.token, 'DELETE', `/contacts/fields/${f.field_id}`);   // clean up so later cases see one Budget field
    ok(dup.length === 1 && await m.locator('.bg-red-50').count() > 0, `duplicate property label should be rejected with a message (found ${dup.length} 'Budget' properties)`);
  }, { u: A, allow: [/http 4\d\d POST .*\/contacts\/fields/, /Failed to load resource/] });

  await tc(rec, 'UIC-CON-31', 'Create contact with custom property values; shown in table column + detail; filterable', async () => {
    await go(A, 'contacts');
    await p.getByRole('button', { name: /Add contact/ }).first().click();
    const m = modalLoc(p);
    await m.getByLabel('Email *').fill(`custom.${t.rid}@cf.example`);
    await m.getByLabel('Budget').fill('125000');
    await m.getByLabel('Tier').selectOption('Gold');
    await m.getByRole('button', { name: /Add contact/ }).click();
    await p.waitForURL(/\/app\/contacts\/[0-9a-f-]{36}/);
    const id = p.url().split('/').pop();
    const cf = sql(`SELECT custom_fields FROM prospects WHERE prospect_id='${id}'`);
    ok(/125000/.test(cf) && /Gold/.test(cf), 'custom_fields in DB: ' + cf);
    ok(await p.getByText('Gold').first().isVisible() && await p.getByText('125,000').first().isVisible(), 'detail shows values');
    // filter in list by custom Tier
    await go(A, 'contacts'); await openFilters(p);
    await addCond(p, 0, 'Tier', 'is', 'Gold'); await applyFilters(p);
    eq(await listTotal(p), dbc(t, `JSON_UNQUOTE(JSON_EXTRACT(custom_fields,'$.tier'))='Gold' OR JSON_SEARCH(custom_fields,'one','Gold') IS NOT NULL`), 'custom filter');
  }, { u: A });

  // "Delete" is a deactivation since C-17 (bd5f153): hidden from forms, values kept, can be reactivated
  await tc(rec, 'UIC-CON-32', 'Properties modal: delete (deactivate) a custom field', async () => {
    await go(A, 'contacts');
    await p.getByRole('button', { name: 'Properties' }).click();
    const m = modalLoc(p);
    A.lastDialog = null;
    await m.getByRole('button', { name: 'Deactivate Tier' }).click(); await settle(A, 800);
    ok(/Deactivate "Tier"/.test(A.lastDialog || ''), 'confirm dialog: ' + A.lastDialog);
    ok(await m.getByRole('button', { name: 'Reactivate Tier' }).isVisible(), 'listed as inactive (Reactivate offered)');
    eq(dbn(`SELECT COUNT(*) FROM contact_field_definitions WHERE tenant_id='${t.tenantId}' AND label='Tier' AND is_active=0`), 1, 'deactivated in DB (definition kept)');
    await m.getByRole('button', { name: 'Done' }).click();
    await p.getByRole('button', { name: /Add contact/ }).first().click();
    eq(await modalLoc(p).getByLabel('Tier').count(), 0, 'hidden from the Add contact form');
  }, { u: A });

  // ---------------- bulk ----------------
  const pick = async (page, names) => { for (const n of names) await page.getByLabel(`Select ${n}`).check(); };
  const bulkBtn = (page, name) => page.getByRole('button', { name: new RegExp(name) });
  await tc(rec, 'UIC-CON-33', 'Bulk: select-all-on-page shows "25 selected", Clear selection works', async () => {
    await go(A, 'contacts?q=Seed');
    await p.getByLabel('Select all on page').check();
    ok(await p.getByText('25 selected').isVisible(), '25 selected');
    await p.getByRole('button', { name: 'Clear selection' }).click();
    eq(await p.getByText(/\d+ selected/).count(), 0, 'cleared');
  }, { u: A });

  await tc(rec, 'UIC-CON-34', 'Bulk: Edit property (lead status) updates DB for selected only', async () => {
    await go(A, 'contacts?q=Seed1');   // Seed1, Seed10..19
    const sel = contacts.filter(c => /^Seed1(\d)?$/.test(c.first_name)).slice(0, 3);
    await pick(p, sel.map(c => c.full_name || `${c.first_name} ${c.last_name}`));
    await bulkBtn(p, 'Edit property').click();
    await p.locator('select').filter({ hasText: 'Lifecycle stage' }).last().selectOption('lead_status');
    await p.locator('select').filter({ hasText: 'Choose' }).last().selectOption('CONNECTED');
    await p.getByRole('button', { name: 'Apply' }).click(); await settle(A, 900);
    for (const c of sel) eq(sql(`SELECT lead_status FROM prospects WHERE prospect_id='${c.prospect_id}'`), 'CONNECTED', c.first_name);
    const others = dbc(t, `lead_status='CONNECTED' AND first_name NOT IN (${sel.map(c => sq(c.first_name)).join(',')})`);
    eq(others, dbc(t, `lead_status='CONNECTED'`) - 3, 'others untouched (control)');
  }, { u: A });

  await tc(rec, 'UIC-CON-35', 'Bulk: Add to new list -> notice and list membership in DB', async () => {
    await go(A, 'contacts?q=Seed2');
    const sel = contacts.filter(c => /^Seed2\d?$/.test(c.first_name)).slice(0, 4);
    await pick(p, sel.map(c => `${c.first_name} ${c.last_name}`));
    await bulkBtn(p, 'Add to list').click();
    await p.getByPlaceholder('List name').fill('VIP list');
    await p.getByRole('button', { name: 'Add', exact: true }).click(); await settle(A, 900);
    ok(await p.getByText(/Added 4 to “VIP list”/).isVisible(), 'notice');
    const lid = sql(`SELECT list_id FROM prospect_lists WHERE tenant_id='${t.tenantId}' AND list_name='VIP list'`);
    eq(dbn(`SELECT COUNT(*) FROM prospect_list_members WHERE list_id='${lid}'`), 4, 'members');
    // add again -> "already there"
    await pick(p, sel.slice(0, 2).map(c => `${c.first_name} ${c.last_name}`));
    await bulkBtn(p, 'Add to list').click();
    await p.locator('select').filter({ hasText: 'New static list' }).last().selectOption({ label: 'VIP list' });
    await p.getByRole('button', { name: 'Add', exact: true }).click(); await settle(A, 900);
    ok(await p.getByText(/2 already there/).isVisible(), 'already there notice');
  }, { u: A });

  await tc(rec, 'UIC-CON-36', 'Bulk: Assign owner persists', async () => {
    await go(A, 'contacts?q=Seed3');
    const sel = contacts.filter(c => /^Seed3\d?$/.test(c.first_name)).slice(0, 3);
    await pick(p, sel.map(c => `${c.first_name} ${c.last_name}`));
    await bulkBtn(p, 'Assign owner').click();
    await p.locator('select').filter({ hasText: 'Unassigned' }).last().selectOption({ label: 'Bob Test' });
    await p.getByRole('button', { name: 'Apply' }).click(); await settle(A, 900);
    for (const c of sel) eq(sql(`SELECT owner_id FROM prospects WHERE prospect_id='${c.prospect_id}'`), bob.userId, c.first_name);
  }, { u: A });

  await tc(rec, 'UIC-CON-37', 'Bulk: Add tag / Remove tag', async () => {
    await go(A, 'contacts?q=Seed4');
    const sel = contacts.filter(c => /^Seed4\d?$/.test(c.first_name)).slice(0, 3);
    await pick(p, sel.map(c => `${c.first_name} ${c.last_name}`));
    await bulkBtn(p, 'Add tag').click();
    await p.getByPlaceholder('Tag name').fill('bulk-tag');
    await p.getByRole('button', { name: 'Add', exact: true }).click(); await settle(A, 900);
    eq(dbc(t, `JSON_CONTAINS(tags,'"bulk-tag"')`), 3, 'tag added');
    await pick(p, sel.slice(0, 1).map(c => `${c.first_name} ${c.last_name}`));
    await bulkBtn(p, 'Remove tag').click();
    await p.getByPlaceholder('Tag name').fill('bulk-tag');
    await p.getByRole('button', { name: 'Remove', exact: true }).click(); await settle(A, 900);
    eq(dbc(t, `JSON_CONTAINS(tags,'"bulk-tag"')`), 2, 'tag removed from one');
  }, { u: A });

  await tc(rec, 'UIC-CON-38', 'Bulk: Enroll in campaign shows enrolled count + rejection reasons (unsubscribed, invalid)', async () => {
    const sel = contacts.filter(c => /^Seed5\d?$/.test(c.first_name)).slice(1, 5); // avoid deleted Seed5 (index5)
    sql(`UPDATE prospects SET consent_status='UNSUBSCRIBED' WHERE prospect_id='${sel[0].prospect_id}'`);
    sql(`UPDATE prospects SET is_valid_email=0 WHERE prospect_id='${sel[1].prospect_id}'`);
    await go(A, 'contacts?q=Seed5');
    await pick(p, sel.map(c => `${c.first_name} ${c.last_name}`));
    await bulkBtn(p, 'Enroll in campaign').click();
    await p.locator('select').filter({ hasText: 'Choose a campaign' }).last().selectOption({ label: 'Q4 Outreach (draft)' });
    await p.getByRole('button', { name: 'Enroll', exact: true }).click(); await settle(A, 1200);
    const dlg = p.getByRole('dialog', { name: 'Enrolled in campaign' });
    await dlg.waitFor();
    const txt = await dlg.innerText();
    ok(/Enrolled\s*2/i.test(txt.replace(/\n/g, ' ')), 'enrolled 2: ' + txt.slice(0, 200));
    ok(/Not enrolled\s*2/i.test(txt.replace(/\n/g, ' ')), 'rejected 2');
    ok(/unsubscribe/i.test(txt) && /invalid|bounced/i.test(txt), 'reasons listed: ' + txt.slice(0, 400));
    eq(dbn(`SELECT COUNT(*) FROM campaign_prospects WHERE campaign_id='${campId}'`), 2, 'DB enrolled');
    await dlg.getByRole('button', { name: /Done/ }).click();
    // enrolling same again -> already enrolled
    await pick(p, sel.slice(2).map(c => `${c.first_name} ${c.last_name}`));
    await bulkBtn(p, 'Enroll in campaign').click();
    await p.locator('select').filter({ hasText: 'Choose a campaign' }).last().selectOption({ label: 'Q4 Outreach (draft)' });
    await p.getByRole('button', { name: 'Enroll', exact: true }).click(); await settle(A, 1200);
    ok(/Already in this campaign/.test(await p.getByRole('dialog').innerText()), 'already enrolled reason');
    await p.getByRole('dialog').getByRole('button', { name: /Done/ }).click();
  }, { u: A });

  await tc(rec, 'UIC-CON-39', 'Bulk: Delete (confirm) soft-deletes and notice; appears in Recently deleted', async () => {
    await go(A, 'contacts?q=Seed1');
    const sel = contacts.filter(c => /^Seed1\d?$/.test(c.first_name)).slice(3, 5);
    await pick(p, sel.map(c => `${c.first_name} ${c.last_name}`));
    await bulkBtn(p, '^Delete').click(); await settle(A, 900);
    ok(await p.getByText(/Deleted 2\./).isVisible(), 'notice');
    for (const c of sel) ok(sql(`SELECT deleted_at FROM prospects WHERE prospect_id='${c.prospect_id}'`) !== 'NULL', 'soft deleted ' + c.first_name);
    ok(/Delete 2 contacts/.test(A.lastDialog || ''), 'confirm dialog text: ' + A.lastDialog);
  }, { u: A });

  // ---------------- export ----------------
  let csvFile = null;
  await tc(rec, 'UIC-CON-40', 'Export CSV downloads file whose rows equal the visible filtered set', async () => {
    await go(A, 'contacts'); await openFilters(p);
    await addCond(p, 0, 'Lifecycle stage', 'is', 'Lead'); await applyFilters(p);
    const n = await listTotal(p);
    await p.getByRole('button', { name: 'Export' }).click();
    const d = await downloadVia(p, () => p.getByRole('button', { name: /This view as CSV/ }).click());
    ok(/\.csv$/i.test(d.name), 'name ' + d.name);
    const lines = d.buf.toString('utf8').replace(/^﻿/, '').trim().split(/\r?\n/);
    eq(lines.length - 1, n, 'csv rows vs UI total');
    ok(/email/i.test(lines[0]), 'header ' + lines[0]);
    ok(lines.slice(1).every(l => /seed|brand|custom/i.test(l)), 'content rows are contacts');
    csvFile = d.file;
  }, { u: A });

  await tc(rec, 'UIC-CON-41', 'Export XLSX downloads a valid workbook with the same row count', async () => {
    await go(A, 'contacts');
    await p.getByRole('button', { name: 'Export' }).click();
    const d = await downloadVia(p, () => p.getByRole('button', { name: /This view as XLSX/ }).click());
    ok(/\.xlsx$/i.test(d.name), 'name ' + d.name);
    ok(d.buf.slice(0, 2).toString() === 'PK', 'zip magic');
    const sheet = execFileSync('unzip', ['-p', d.file, 'xl/worksheets/sheet1.xml'], { encoding: 'utf8', maxBuffer: 1 << 28 });
    const nrows = (sheet.match(/<row /g) || []).length;
    eq(nrows - 1, dbc(t, '1=1'), 'xlsx data rows vs DB');
  }, { u: A });

  // ---------------- merge duplicates ----------------
  await tc(rec, 'UIC-CON-42', 'Duplicates modal: no duplicates state, then merge with per-property choices', async () => {
    // The duplicate finder also flags similar names at one company (C-08), which the numbered seed contacts
    // ("Seed3 Person03" / "Seed43 Person43" at Umbrella) are; mark those pairs "not the same person" first
    await must(t.token, 'POST', '/contacts/duplicates/scan');   // pairs are stored; bring them up to date first
    const pre = await must(t.token, 'GET', '/contacts/duplicates?limit=200');
    for (const g of pre.groups || []) if (g.pair_id) await call(t.token, 'POST', `/contacts/duplicates/${g.pair_id}/dismiss`);
    const left = ((await must(t.token, 'GET', '/contacts/duplicates?limit=200')).groups || []).length;
    eq(left, 0, 'seed pairs dismissed');
    await go(A, 'contacts');
    // MergeDuplicatesModal "Find duplicates" (5451f65) -> "Merge N" opens the "Review merge" preview with per-property radios
    await p.getByRole('button', { name: 'Find duplicates' }).click();
    let m = p.getByRole('dialog', { name: 'Find duplicates' });
    await m.getByText(/No duplicates found|Merge/).first().waitFor();
    const none = await m.getByText('No duplicates found').isVisible().catch(() => false);
    ok(none, 'initially none');
    await m.getByRole('button', { name: 'Done' }).click();
    // two records with same email-ignoring-dots
    const a = await must(t.token, 'POST', '/contacts', { email: `jane.doe.${t.rid}@gmail.com`, first_name: 'Jane', last_name: 'Doe', phone: '+14155550111', designation: 'CEO', company_name: 'DupCo' });
    const bId = sql(`SELECT UUID()`);
    sql(`INSERT INTO prospects (prospect_id,tenant_id,first_name,last_name,email,phone,designation,company_name,lifecycle_stage,created_at,updated_at) VALUES ('${bId}','${t.tenantId}','Jane','Doe',${sq(`janedoe.${t.rid}@gmail.com`)},'+14155550222','CTO','DupCo','LEAD',NOW(),NOW())`);
    // the modal lists stored pairs (C-08: the worker rescans; a row inserted by SQL is found at the next scan)
    await must(t.token, 'POST', '/contacts/duplicates/scan');
    await go(A, 'contacts');
    await p.getByRole('button', { name: 'Find duplicates' }).click();
    m = p.getByRole('dialog', { name: 'Find duplicates' });
    const grp = m.locator('div.border.rounded-2xl').filter({ hasText: `jane.doe.${t.rid}@` }).filter({ hasText: 'Pick the record to keep' });
    await grp.first().waitFor();
    // keep record = the one marked KEEP; choose the OTHER record's phone
    const rowsTxt = await grp.first().locator('div.divide-y > div').allInnerTexts();
    const keepIsA = rowsTxt.find(t2 => /KEEP/.test(t2))?.includes(`jane.doe.${t.rid}@`);
    const wantPhone = keepIsA ? '+14155550222' : '+14155550111';
    await grp.first().getByRole('button', { name: /^Merge 1/ }).click();
    const rv = p.getByRole('dialog', { name: 'Review merge' });
    await rv.getByRole('radio', { name: `Phone: ${wantPhone}` }).check();
    const merged = p.waitForResponse(r => /\/contacts\/merge/.test(r.url()) && r.request().method() === 'POST', { timeout: 15000 });
    await rv.getByRole('button', { name: /^Merge 2 records/ }).click();
    await merged; await settle(A, 500);   // read the DB only after the merge request has committed
    const keptId = a.prospect_id;
    const keeper = sql(`SELECT CONCAT_WS('|',phone,deleted_at IS NULL) FROM prospects WHERE prospect_id IN ('${keptId}','${bId}') AND deleted_at IS NULL`);
    const survivors = dbn(`SELECT COUNT(*) FROM prospects WHERE prospect_id IN ('${keptId}','${bId}') AND deleted_at IS NULL`);
    eq(survivors, 1, 'one survivor');
    ok(keeper.startsWith(wantPhone), `chosen phone ${wantPhone} should be kept: ` + keeper);
  }, { u: A });

  // ---------------- permissions ----------------
  const AG = await ui(browser, alice);
  const pa = AG.page;
  await tc(rec, 'UIC-CON-43', 'AGENT (no manage_prospects): sees only own contacts; no Import/Export/Properties/Duplicates/checkboxes', async () => {
    await go(AG, 'contacts');
    const mine = dbn(`SELECT COUNT(*) FROM prospects WHERE tenant_id='${t.tenantId}' AND deleted_at IS NULL AND owner_id='${alice.userId}'`);
    eq(await listTotal(pa), mine, 'agent total = own');
    for (const n of ['Import', 'Export', 'Properties', 'Duplicates']) eq(await pa.getByRole(n === 'Import' ? 'link' : 'button', { name: n, exact: true }).count(), 0, n + ' hidden');
    eq(await pa.getByLabel('Select all on page').count(), 0, 'no bulk checkbox');
    ok(await pa.getByText('showing contacts you own').isVisible(), 'owner-only subtitle');
  }, { u: AG });

  await tc(rec, 'UIC-CON-44', 'AGENT: direct API refused for bulk/export/merge; own-contact create works without owner field', async () => {
    const tok = await t.tk('alice');
    const mine = (await must(tok, 'GET', '/contacts?page_size=5')).items.map(c => c.prospect_id);
    for (const [m, path, body] of [['POST', '/contacts/bulk', { prospect_ids: mine, action: 'delete' }], ['GET', '/contacts/export?format=csv'], ['POST', '/contacts/merge', { primary_id: mine[0], duplicate_ids: [mine[1]] }]]) {
      const r = await call(tok, m, path, body); eq(r.status, 403, `${m} ${path}`);
    }
    await go(AG, 'contacts');
    await pa.getByRole('button', { name: /Add contact/ }).first().click();
    const mdl = modalLoc(pa);
    eq(await mdl.getByText('Owner', { exact: true }).count(), 0, 'no owner field for agent');
    await mdl.getByLabel('Email *').fill(`agent.created.${t.rid}@x.example`);
    await mdl.getByRole('button', { name: /Add contact/ }).click();
    await pa.waitForURL(/\/app\/contacts\/[0-9a-f-]{36}/);
    eq(sql(`SELECT owner_id FROM prospects WHERE email='agent.created.${t.rid}@x.example'`), alice.userId, 'owner = agent');
  }, { u: AG });

  await tc(rec, 'UIC-CON-45', 'AGENT: cannot open someone else\'s contact by URL ("Contact not found")', async () => {
    const other = contacts.find(c => sql(`SELECT owner_id FROM prospects WHERE prospect_id='${c.prospect_id}'`) === t.owner.userId);
    await go(AG, `contacts/${other.prospect_id}`);
    ok(await pa.getByText('Contact not found').isVisible(), 'not found shown');
  }, { u: AG, allow: [/http 404 GET .*\/contacts\//, /Failed to load resource/] });

  // API failure handling
  await tc(rec, 'UIC-CON-46', 'List API 500 -> page does not crash (graceful state)', async () => {
    await p.route(u => /\/api\/contacts$/.test(u.pathname), r => r.fulfill({ status: 500, body: '{"detail":"boom"}', contentType: 'application/json' }));
    await go(A, 'contacts?q=zzz'); await settle(A, 800);
    const body = await p.locator('body').innerText();
    ok(!/Something went wrong on this screen|Application Error/.test(body), 'error boundary shown');
    ok(await p.getByRole('heading', { name: 'Contacts', exact: true }).isVisible(), 'shell still rendered');
    await p.unrouteAll({ behavior: 'ignoreErrors' });
  }, { u: A, allow: [/http 500/, /console: Failed to load resource/] });

  await A.ctx.close(); await AG.ctx.close();
  return { t, contacts };
}
if (isMain(import.meta)) standalone('contacts', run);
