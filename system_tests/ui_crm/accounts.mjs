// UIC-ACC: Companies list, AccountDetail
import { Tenant, ui, go, settle, tc, skip, ok, eq, num, must, call, sql, sq, dbn, modalLoc, standalone, isMain, failApi } from './crm_common.mjs';

const acct = (t, tenantId = t.tenantId) => `tenant_id='${tenantId}' AND deleted_at IS NULL`;
async function total(p) { const x = await p.getByText(/^[\d,]+ compan(y|ies)$/).first().innerText({ timeout: 8000 }).catch(() => ''); const m = x.match(/([\d,]+)/); return m ? Number(m[1].replace(/,/g, '')) : -1; }

export async function run(rec, browser) {
  const t = await (await Tenant.get('accounts')).init();
  const alice = await t.addUser('AGENT', 'Alice');
  const bob = await t.addUser('ADMIN', 'Bob');
  const names = ['Acme Corp', 'Globex', 'Initech', 'Umbrella', 'Hooli', 'Stark Industries', 'Wayne Enterprises', 'Wonka Factory'];
  const accounts = [];
  for (let i = 0; i < names.length; i++) accounts.push(await must(t.token, 'POST', '/accounts', { name: names[i], domain: `${names[i].toLowerCase().replace(/[^a-z]/g, '')}.example`, industry: i % 2 ? 'Software' : 'Manufacturing', country: i % 2 ? 'US' : 'Germany', city: 'Town' + i, annual_revenue: String((i + 1) * 500000), owner_id: i < 6 ? t.owner.userId : (i === 6 ? alice.userId : null) }));
  // 3 contacts for Acme, 1 for Globex
  for (let i = 0; i < 3; i++) await must(t.token, 'POST', '/contacts', { email: `p${i}.${t.rid}@acmecorp.example`, first_name: `Acme${i}`, last_name: 'Person', company_name: 'Acme Corp', account_id: accounts[0].account_id, owner_id: t.owner.userId });
  await must(t.token, 'POST', '/contacts', { email: `g0.${t.rid}@globex.example`, first_name: 'Glo', last_name: 'Bex', company_name: 'Globex', account_id: accounts[1].account_id, owner_id: t.owner.userId });
  const A = await ui(browser, t.owner); const p = A.page;

  await tc(rec, 'UIC-ACC-01', 'Companies list renders with count, columns and revenue formatting', async () => {
    await go(A, 'accounts');
    await p.getByRole('heading', { name: /^Companies/ }).first().waitFor();
    eq(await total(p), dbn(`SELECT COUNT(*) FROM accounts WHERE ${acct(t)}`), 'count');
    const row = p.locator('tbody tr').filter({ hasText: 'Acme Corp' });
    const txt = await row.innerText();
    ok(/acmecorp\.example/.test(txt) && /Germany/.test(txt) && /\$500K/.test(txt) && /3/.test(txt), 'row content: ' + txt.replace(/\n/g, ' | '));
  }, { u: A });

  await tc(rec, 'UIC-ACC-02', 'Sort by Company asc/desc and by Contacts', async () => {
    await go(A, 'accounts');
    const first = async () => (await p.locator('tbody tr').first().locator('p').first().innerText()).trim();
    eq(await first(), 'Acme Corp', 'default name asc');
    await p.locator('thead').getByRole('button', { name: /^Company/ }).click(); await settle(A, 600);
    eq(await first(), 'Wonka Factory', 'desc');
    await p.locator('thead').getByRole('button', { name: /^Contacts/ }).click(); await settle(A, 600);   // asc by contacts
    await p.locator('thead').getByRole('button', { name: /^Contacts/ }).click(); await settle(A, 600);   // desc
    eq(await first(), 'Acme Corp', 'most contacts first');
  }, { u: A });

  await tc(rec, 'UIC-ACC-03', 'Search by name / domain / industry; empty state; owner segments', async () => {
    await go(A, 'accounts');
    const box = p.getByPlaceholder(/Search name, domain/);
    await box.fill('globex'); await settle(A, 800);
    eq(await total(p), 1, 'name');
    await box.fill('software'); await settle(A, 800);
    eq(await total(p), dbn(`SELECT COUNT(*) FROM accounts WHERE ${acct(t)} AND industry='Software'`), 'industry');
    await box.fill('umbrella.example'); await settle(A, 800);
    eq(await total(p), 1, 'domain');
    await box.fill('nothing-matches-zzz'); await settle(A, 800);
    ok(await p.getByText('No companies match').isVisible(), 'empty');
    await box.fill(''); await settle(A, 800);
    await p.getByRole('button', { name: 'My companies' }).click(); await settle(A, 700);
    eq(await total(p), dbn(`SELECT COUNT(*) FROM accounts WHERE ${acct(t)} AND owner_id='${t.owner.userId}'`), 'mine');
    await p.getByRole('button', { name: 'Unassigned', exact: true }).click(); await settle(A, 700);
    eq(await total(p), dbn(`SELECT COUNT(*) FROM accounts WHERE ${acct(t)} AND owner_id IS NULL`), 'unassigned');
  }, { u: A });

  await tc(rec, 'UIC-ACC-04', 'New company: name required (button disabled), full create -> detail page + DB', async () => {
    await go(A, 'accounts');
    await p.getByRole('button', { name: /New company/ }).click();
    const m = modalLoc(p);
    ok(await m.getByRole('button', { name: 'Create company' }).isDisabled(), 'disabled with empty name');
    await m.getByLabel('Name *').fill('Soylent Corp');
    await m.getByLabel('Domain').fill('https://www.Soylent.example/');
    await m.getByLabel('Industry').fill('Food'); await m.getByLabel('Employees').fill('51-200');
    await m.getByLabel('Annual revenue (USD)').fill('2500000'); await m.getByLabel('City').fill('Chicago'); await m.getByLabel('Country').fill('US');
    await m.getByLabel('Lifecycle stage').selectOption('CUSTOMER');
    await m.getByRole('button', { name: 'Create company' }).click();
    await p.waitForURL(/\/app\/accounts\/[0-9a-f-]{36}/, { timeout: 10000 });
    const id = p.url().split('/').pop();
    eq(sql(`SELECT CONCAT_WS('|',name,domain,industry,emp_band,annual_revenue,city,country,lifecycle_stage) FROM accounts WHERE account_id='${id}'`), 'Soylent Corp|soylent.example|Food|51-200|2500000.00|Chicago|United States|CUSTOMER', 'DB row (domain normalised; the country picker stores the name, src/lib/countries.js)');
    await p.getByRole('heading', { name: /^Soylent Corp/ }).first().waitFor({ timeout: 8000 });
    ok(await p.getByText('$2.5M revenue').isVisible(), 'revenue shown');
  }, { u: A });

  await tc(rec, 'UIC-ACC-05', 'Domain must be unique per workspace: duplicate shows message, nothing created', async () => {
    await go(A, 'accounts');
    await p.getByRole('button', { name: /New company/ }).click();
    const m = modalLoc(p);
    await m.getByLabel('Name *').fill('Globex Clone'); await m.getByLabel('Domain').fill('globex.example');
    await m.getByRole('button', { name: 'Create company' }).click(); await settle(A, 700);
    ok(await m.getByText(/already has the domain/i).isVisible(), 'duplicate message');
    eq(dbn(`SELECT COUNT(*) FROM accounts WHERE ${acct(t)} AND name='Globex Clone'`), 0);
    await m.getByRole('button', { name: 'Cancel' }).click();
  }, { u: A, allow: [/http 409 POST .*\/accounts/, /Failed to load resource/] });

  await tc(rec, 'UIC-ACC-06', 'Company detail: stats, contacts listing, "Open in Contacts" filter chip', async () => {
    await go(A, `accounts/${accounts[0].account_id}`);
    await p.getByRole('heading', { name: /^Acme Corp/ }).first().waitFor();
    eq(num(await p.locator('p:has-text("Contacts") + p').first().innerText()), 3, 'contacts stat');
    for (let i = 0; i < 3; i++) ok(await p.getByText(`Acme${i} Person`).isVisible(), `contact ${i} listed`);
    await p.getByRole('link', { name: 'Open in Contacts' }).click(); await settle(A, 800);
    ok(await p.getByText('Company: Acme Corp').isVisible(), 'chip');
    eq(await p.locator('table tbody tr').count(), 3, 'filtered rows');
    await p.getByRole('button', { name: 'Clear company filter' }).click(); await settle(A, 700);
    ok(await p.locator('table tbody tr').count() > 3, 'filter cleared');
  }, { u: A });

  await tc(rec, 'UIC-ACC-07', 'Edit company (cancel path then rename + fields) -> DB, contacts get new company name, history row', async () => {
    const a = accounts[2];
    await go(A, `accounts/${a.account_id}`);
    await p.getByRole('button', { name: 'Edit company' }).click();
    await modalLoc(p).getByLabel('Name *').fill('Should Not Save');
    await modalLoc(p).getByRole('button', { name: 'Cancel' }).click();
    eq(sql(`SELECT name FROM accounts WHERE account_id='${a.account_id}'`), 'Initech');
    await must(t.token, 'POST', '/contacts', { email: `ini.${t.rid}@initech.example`, first_name: 'Ini', last_name: 'Tech', company_name: 'Initech', account_id: a.account_id, owner_id: t.owner.userId });
    await go(A, `accounts/${a.account_id}`);
    await p.getByRole('button', { name: 'Edit company' }).click();
    const m = modalLoc(p);
    await m.getByLabel('Name *').fill('Initech Global'); await m.getByLabel('Phone').fill('+15125550123'); await m.getByLabel('About').fill('Makes TPS reports.');
    await m.getByRole('button', { name: 'Save', exact: true }).click(); await settle(A, 900);
    eq(sql(`SELECT CONCAT_WS('|',name,phone,description) FROM accounts WHERE account_id='${a.account_id}'`), 'Initech Global|+15125550123|Makes TPS reports.');
    eq(sql(`SELECT company_name FROM prospects WHERE email='ini.${t.rid}@initech.example'`), 'Initech Global', 'contact company name follows rename');
    ok(await p.getByRole('heading', { name: /^Initech Global/ }).first().isVisible(), 'heading');
    ok(await p.getByText('Property history').isVisible() && await p.getByText('Initech Global').count() > 1, 'history shows the rename');
  }, { u: A });

  await tc(rec, 'UIC-ACC-08', 'Owner change on company detail persists', async () => {
    await go(A, `accounts/${accounts[3].account_id}`);
    await p.locator('select').filter({ hasText: 'Unassigned' }).first().selectOption({ label: 'Bob Test' }); await settle(A, 800);
    eq(sql(`SELECT owner_id FROM accounts WHERE account_id='${accounts[3].account_id}'`), bob.userId);
  }, { u: A });

  await tc(rec, 'UIC-ACC-09', 'Add contact from company page: company prefilled, linked, contact count increments', async () => {
    const a = accounts[4];
    await go(A, `accounts/${a.account_id}`);
    await p.getByRole('button', { name: /^Add contact$/ }).click();
    const m = modalLoc(p);
    eq(await m.getByLabel('Company', { exact: false }).first().inputValue(), 'Hooli', 'prefilled');
    await m.getByLabel('Email *').fill(`new.${t.rid}@hooli.example`); await m.getByLabel('First name').fill('Gavin');
    await m.getByRole('button', { name: /Add contact/ }).click(); await settle(A, 1000);
    eq(sql(`SELECT account_id FROM prospects WHERE email='new.${t.rid}@hooli.example'`), a.account_id, 'linked');
    ok(await p.getByText('Gavin').first().isVisible(), 'listed on company');
    eq(num(await p.locator('p:has-text("Contacts") + p').first().innerText()), 1, 'stat');
  }, { u: A });

  await tc(rec, 'UIC-ACC-10', 'Link contacts by company (backfill) shows a notice and links contacts without a company record', async () => {
    const c = await must(t.token, 'POST', '/contacts', { email: `orphan.${t.rid}@brandnewdomain.example`, first_name: 'Orphan', last_name: 'X' });
    sql(`UPDATE prospects SET account_id=NULL WHERE prospect_id='${c.prospect_id}'`);
    await go(A, 'accounts');
    // BackfillDialog (C-14): a dry-run preview first, then "Link N contacts", then a "Done: ..." notice (undo in Runs)
    await p.getByRole('button', { name: /Link contacts by company/ }).click();
    const dlg = p.getByRole('dialog', { name: 'Link contacts by company' });
    // "Link 0 contacts" (disabled) until the preview has loaded
    const runBtn = dlg.getByRole('button', { name: /^Link [1-9][\d,]* contacts?$/ });
    const found = await runBtn.waitFor({ timeout: 15000 }).then(() => true, () => false);
    ok(found && await runBtn.isEnabled(), 'preview found contacts to link: ' + await dlg.getByRole('button', { name: /^Link \d/ }).innerText().catch(() => '?'));
    await runBtn.click();
    await dlg.getByText(/^Done: \d+ linked/).waitFor({ timeout: 15000 });
    ok(await dlg.getByText(/^Done: \d+ linked/).isVisible(), 'notice');
    ok(sql(`SELECT account_id IS NOT NULL FROM prospects WHERE prospect_id='${c.prospect_id}'`) === '1', 'orphan linked');
  }, { u: A });

  await tc(rec, 'UIC-ACC-11', 'Pagination across 25 per page', async () => {
    for (let i = 0; i < 24; i++) await must(t.token, 'POST', '/accounts', { name: `Bulk Co ${String(i).padStart(2, '0')}`, domain: `bulk${i}-${t.rid}.example` });   // distinct registrable domains (subdomains of one domain are one company, app/services/crm.py)
    await go(A, 'accounts');
    const n = dbn(`SELECT COUNT(*) FROM accounts WHERE ${acct(t)}`);
    eq(await total(p), n, 'total');
    ok(n > 25, 'enough rows ' + n);
    eq(await p.locator('tbody tr').count(), 25, 'page 1 rows');
    ok(await p.getByText(`Page 1 of ${Math.ceil(n / 25)}`).isVisible(), 'page text');
    await p.getByRole('button', { name: 'Next page' }).click(); await settle(A, 600);
    eq(await p.locator('tbody tr').count(), n - 25, 'page 2 rows');
    ok(await p.getByRole('button', { name: 'Next page' }).isDisabled(), 'next disabled');
  }, { u: A });

  // UIC-ACC-12 (was BLOCKED not-implemented): the feature now exists and is covered by UIC-NF-11 in new_features.mjs

  await tc(rec, 'UIC-ACC-13', 'Delete company (confirm): contacts kept, appears in Recently deleted > Companies, restore works', async () => {
    const a = accounts[5];
    await go(A, `accounts/${a.account_id}`);
    await p.getByRole('button', { name: /Delete company/ }).click();
    await p.waitForURL(/\/app\/accounts$/, { timeout: 8000 });
    ok(sql(`SELECT deleted_at FROM accounts WHERE account_id='${a.account_id}'`) !== 'NULL', 'soft deleted');
    await go(A, 'recently-deleted');
    await p.getByRole('button', { name: 'Companies' }).click(); await settle(A, 600);
    ok(await p.getByText('Stark Industries').isVisible(), 'listed');
    await p.locator('li').filter({ hasText: 'Stark Industries' }).getByRole('button', { name: 'Restore' }).click(); await settle(A, 800);
    eq(sql(`SELECT deleted_at FROM accounts WHERE account_id='${a.account_id}'`), 'NULL', 'restored');
    ok(await p.getByText('Company restored.').isVisible(), 'notice');
  }, { u: A });

  const AG = await ui(browser, alice); const pa = AG.page;
  await tc(rec, 'UIC-ACC-14', 'AGENT: sees only own companies; no backfill button, owner read-only, no delete', async () => {
    await go(AG, 'accounts');
    const own = dbn(`SELECT COUNT(*) FROM accounts WHERE ${acct(t)} AND owner_id='${alice.userId}'`);
    eq(await total(pa), own, 'own companies only');
    eq(await pa.getByRole('button', { name: /Link contacts by company/ }).count(), 0, 'no backfill');
    await go(AG, `accounts/${accounts[6].account_id}`);
    await pa.getByRole('heading', { name: /^Wayne Enterprises/ }).first().waitFor();
    eq(await pa.getByRole('button', { name: /Delete company/ }).count(), 0, 'no delete');
    ok(await pa.getByText('Alice Test').first().isVisible(), 'owner read-only text');
  }, { u: AG });

  await tc(rec, 'UIC-ACC-15', 'AGENT cannot open another user\'s company (not found) and API refuses', async () => {
    await go(AG, `accounts/${accounts[0].account_id}`);
    ok(await pa.getByText('Company not found').isVisible(), 'not found');
    const tok = await t.tk('alice');
    eq((await call(tok, 'GET', `/accounts/${accounts[0].account_id}`)).status, 404, 'API 404');
    eq((await call(tok, 'DELETE', `/accounts/${accounts[0].account_id}`)).status >= 400, true, 'API refuses delete');
  }, { u: AG, allow: [/http 4\d\d/, /Failed to load resource/] });

  await tc(rec, 'UIC-ACC-16', 'Companies API failure: page keeps its shell', async () => {
    await failApi(p, /\/api\/accounts$/, 500, 'GET');
    await go(A, 'accounts?x=1'); await settle(A, 700);
    ok(await p.getByRole('heading', { name: /^Companies/ }).first().isVisible(), 'shell');
    await p.unrouteAll({ behavior: 'ignoreErrors' });
  }, { u: A, allow: [/http 500/, /Failed to load resource/] });

  await A.ctx.close(); await AG.ctx.close();
}
if (isMain(import.meta)) standalone('accounts', run);
