// UIC-LST: Lists (static + active); UIC-IMP: ImportContacts wizard
import { Tenant, ui, go, settle, tc, skip, ok, eq, num, must, call, sql, sq, dbn, modalLoc, standalone, isMain, failApi, seedContacts, csvText, writeTmp, xlsxFile, downloadVia, TMP } from './crm_common.mjs';
import fs from 'node:fs';

const cnt = (t, w = '1=1') => dbn(`SELECT COUNT(*) FROM prospects WHERE tenant_id='${t.tenantId}' AND deleted_at IS NULL AND (${w})`);
const listRow = (p, name) => p.locator('tbody tr').filter({ hasText: name });

export async function runLists(rec, browser) {
  const t = await (await Tenant.get('lists')).init();
  const alice = await t.addUser('AGENT', 'Alice');
  const contacts = await seedContacts(t, 40, { owners: [t.owner.userId] });
  const A = await ui(browser, t.owner); const p = A.page;

  await tc(rec, 'UIC-LST-01', 'Lists page empty state, then headings/filters render', async () => {
    await go(A, 'lists');
    await p.getByRole('heading', { name: 'Lists', exact: true }).waitFor();
    ok(await p.getByText('No lists yet').isVisible(), 'empty state');
    ok(await p.getByText('0 lists').isVisible(), 'count 0');
    for (const b of ['All', 'Static', 'Active']) ok(await p.getByRole('button', { name: b, exact: true }).isVisible(), b);
  }, { u: A });

  await tc(rec, 'UIC-LST-02', 'New list: name required; create static list -> DB + opens Contacts filtered by it', async () => {
    await go(A, 'lists');
    await p.getByRole('button', { name: /New list/ }).click();
    const m = modalLoc(p);
    ok(await m.getByRole('button', { name: 'Create list' }).isDisabled(), 'disabled without name');
    await m.getByLabel('Name').fill('Webinar invitees'); await m.getByLabel('Description').fill('From the Oct webinar');
    await m.getByRole('button', { name: 'Create list' }).click();
    await p.waitForURL(/\/app\/contacts\?list_id=/, { timeout: 8000 });
    eq(sql(`SELECT CONCAT_WS('|',list_type,description) FROM prospect_lists WHERE tenant_id='${t.tenantId}' AND list_name='Webinar invitees'`), 'STATIC|From the Oct webinar');
    await p.getByText('List: Webinar invitees').waitFor({ timeout: 8000 }).catch(() => {});
    ok(await p.getByText('List: Webinar invitees').isVisible(), 'chip on contacts page');
    ok(await p.getByText('No contacts match').isVisible(), 'empty list');
  }, { u: A });

  await tc(rec, 'UIC-LST-03', 'New list modal: cancel closes without creating', async () => {
    await go(A, 'lists');
    await p.getByRole('button', { name: /New list/ }).click();
    await modalLoc(p).getByLabel('Name').fill('Never saved');
    await modalLoc(p).getByRole('button', { name: 'Cancel' }).click();
    eq(dbn(`SELECT COUNT(*) FROM prospect_lists WHERE tenant_id='${t.tenantId}' AND list_name='Never saved'`), 0);
  }, { u: A });

  await tc(rec, 'UIC-LST-04', 'Active list: needs a criterion; live preview count equals DB; saved list has the same members', async () => {
    await go(A, 'lists');
    await p.getByRole('button', { name: /New list/ }).click();
    const m = modalLoc(p);
    await m.getByLabel('Name').fill('Hot leads');
    await m.getByRole('button', { name: /Active list/ }).click();
    ok(await m.getByRole('button', { name: 'Create list' }).isDisabled(), 'disabled without criteria');
    await m.getByRole('button', { name: 'Condition', exact: true }).click();
    await m.getByLabel('Property').first().selectOption({ label: 'Lifecycle stage' });
    // The value control is a text <input> until the lifecycle options (GET /contacts/meta) have loaded, then a
    // <select>; the Operator's next sibling could still be the <input> (flaky "Element is not a <select>"). Wait for
    // the condition row's third <select> (Property, Operator, Value) as in UIC-LST-08; selectOption then waits for LEAD.
    const row = m.getByLabel('Property').first().locator('xpath=..');
    const valueSel = row.locator('select').nth(2);
    await valueSel.waitFor({ timeout: 10000 });
    await valueSel.selectOption({ value: 'LEAD' });
    const expected = cnt(t, `lifecycle_stage='LEAD'`);
    await m.getByText(/contacts? match right now/).waitFor();
    eq(Number((await m.getByText(/contacts? match right now/).innerText()).match(/(\d+)\s+contacts?\s+match/)[1]), expected, 'preview count');
    await m.getByRole('button', { name: 'Create list' }).click();
    await p.waitForURL(/\/app\/contacts\?list_id=/);
    await settle(A, 800);
    eq(await p.locator('table tbody tr').count(), expected, 'rows on contacts page for the active list');
    await go(A, 'lists');
    const r = await listRow(p, 'Hot leads').innerText();
    ok(/Active/.test(r) && r.includes(String(expected)), 'lists page shows Active badge + count: ' + r.replace(/\n/g, ' '));
  }, { u: A });

  await tc(rec, 'UIC-LST-05', 'Active list membership follows data: changing a contact moves it in/out automatically', async () => {
    const c = contacts.find(x => x.lifecycle_stage === 'SUBSCRIBER');
    await must(t.token, 'PATCH', `/contacts/${c.prospect_id}`, { lifecycle_stage: 'LEAD' });
    await go(A, 'lists'); await listRow(p, 'Hot leads').waitFor();
    const after = (await listRow(p, 'Hot leads').innerText());
    ok(after.includes(String(cnt(t, `lifecycle_stage='LEAD'`))), 'count includes newly-LEAD contact: ' + after.replace(/\n/g, ' '));
  }, { u: A });

  await tc(rec, 'UIC-LST-06', 'Static list: add members from Contacts bulk bar, remove from this list, counts match DB', async () => {
    await go(A, 'contacts?q=Seed1');
    const sel = contacts.filter(c => /^Seed1\d?$/.test(c.first_name)).slice(0, 5);
    for (const c of sel) await p.getByLabel(`Select ${c.first_name} ${c.last_name}`).check();
    await p.getByRole('button', { name: /Add to list/ }).click();
    await p.locator('select').filter({ hasText: 'New static list' }).last().selectOption({ label: 'Webinar invitees' });
    await p.getByRole('button', { name: 'Add', exact: true }).click(); await settle(A, 900);
    const lid = sql(`SELECT list_id FROM prospect_lists WHERE tenant_id='${t.tenantId}' AND list_name='Webinar invitees'`);
    eq(dbn(`SELECT COUNT(*) FROM prospect_list_members WHERE list_id='${lid}'`), 5, 'members added');
    await go(A, 'lists');
    ok((await listRow(p, 'Webinar invitees').innerText()).includes('5'), 'Lists page count 5');
    await go(A, `contacts?list_id=${lid}`);
    eq(await p.locator('table tbody tr').count(), 5, 'contacts filtered by list');
    await p.getByLabel(`Select ${sel[0].first_name} ${sel[0].last_name}`).check();
    await p.getByLabel(`Select ${sel[1].first_name} ${sel[1].last_name}`).check();
    await p.getByRole('button', { name: /Remove from this list/ }).click(); await settle(A, 900);
    eq(dbn(`SELECT COUNT(*) FROM prospect_list_members WHERE list_id='${lid}'`), 3, 'two removed');
    eq(cnt(t, '1=1'), 40, 'contacts themselves kept');
  }, { u: A });

  await tc(rec, 'UIC-LST-07', 'Add contact modal can add the new contact to a static list', async () => {
    await go(A, 'contacts');
    await p.getByRole('button', { name: /Add contact/ }).first().click();
    const m = modalLoc(p);
    await m.getByLabel('Email *').fill(`listed.${t.rid}@x.example`);
    await m.getByLabel('Add to list').selectOption({ label: 'Webinar invitees' });
    await m.getByRole('button', { name: /Add contact/ }).click();
    await p.waitForURL(/\/app\/contacts\/[0-9a-f-]{36}/);
    eq(dbn(`SELECT COUNT(*) FROM prospect_list_members m JOIN prospect_lists l ON l.list_id=m.list_id JOIN prospects pr ON pr.prospect_id=m.prospect_id WHERE l.tenant_id='${t.tenantId}' AND l.list_name='Webinar invitees' AND pr.email='listed.${t.rid}@x.example'`), 1, 'membership created in DB');
    await p.getByRole('link', { name: 'Webinar invitees' }).waitFor({ timeout: 8000 }).catch(() => {});
    ok(await p.getByRole('link', { name: 'Webinar invitees' }).isVisible(), 'list card on record page');
    eq(dbn(`SELECT COUNT(*) FROM prospect_list_members m JOIN prospect_lists l ON l.list_id=m.list_id WHERE l.tenant_id='${t.tenantId}' AND l.list_name='Webinar invitees'`), 4);
  }, { u: A });

  await tc(rec, 'UIC-LST-08', 'Edit list: rename + description persisted; active list criteria edit changes membership', async () => {
    await go(A, 'lists');
    await p.getByRole('button', { name: 'Edit Webinar invitees' }).click();
    const m = modalLoc(p);
    eq(await m.getByLabel('Name').inputValue(), 'Webinar invitees', 'prefilled');
    await m.getByLabel('Name').fill('Oct webinar'); await m.getByLabel('Description').fill('Renamed');
    await m.getByRole('button', { name: 'Save', exact: true }).click(); await settle(A, 800);
    eq(sql(`SELECT CONCAT_WS('|',list_name,description) FROM prospect_lists WHERE tenant_id='${t.tenantId}' AND list_name='Oct webinar'`), 'Oct webinar|Renamed');
    await p.getByRole('button', { name: 'Edit Hot leads' }).click();
    const m2 = modalLoc(p);
    // FilterBuilder renders the value control as a text <input> until the option metadata (GET /contacts/meta) has
    // loaded, then swaps it for a <select>; the old sibling-of-Operator locator raced that swap. Target the condition
    // row's third <select> (Property, Operator, Value), which auto-waits until the options exist.
    const row = m2.getByLabel('Property').first().locator('xpath=..');
    eq(await row.getByLabel('Property').inputValue(), 'lifecycle_stage', 'saved criterion field');
    const valueSel = row.locator('select').nth(2);
    await valueSel.waitFor({ timeout: 10000 });
    eq(await valueSel.inputValue(), 'LEAD', 'saved criterion value prefilled');
    await valueSel.selectOption({ value: 'MQL' });
    await m2.getByText(/contacts? match right now/).waitFor();
    await m2.getByRole('button', { name: 'Save', exact: true }).click(); await settle(A, 900);
    ok((await listRow(p, 'Hot leads').innerText()).includes(String(cnt(t, `lifecycle_stage='MQL'`))), 'member count follows new criteria');
    eq(sql(`SELECT JSON_UNQUOTE(JSON_EXTRACT(filters,'$.conditions[0].value')) FROM prospect_lists WHERE tenant_id='${t.tenantId}' AND list_name='Hot leads'`), 'MQL', 'criteria persisted');
  }, { u: A });

  await tc(rec, 'UIC-LST-09', 'Type tabs and search filter the lists', async () => {
    await go(A, 'lists');
    await p.getByRole('button', { name: 'Static', exact: true }).click(); await settle(A, 400);
    ok(await listRow(p, 'Oct webinar').isVisible() && await listRow(p, 'Hot leads').count() === 0, 'static only');
    await p.getByRole('button', { name: 'Active', exact: true }).click(); await settle(A, 400);
    ok(await listRow(p, 'Hot leads').isVisible() && await listRow(p, 'Oct webinar').count() === 0, 'active only');
    await p.getByRole('button', { name: 'All', exact: true }).click();
    // server-side search, debounced 300 ms with the previous page kept meanwhile (src/pages/Lists.jsx): wait for the result
    await p.getByPlaceholder('Search lists…').fill('webinar');
    ok(await p.getByText('1 list', { exact: true }).waitFor({ timeout: 8000 }).then(() => true, () => false), 'search count');
    await p.getByPlaceholder('Search lists…').fill('zzz');
    ok(await p.getByText('No lists match').waitFor({ timeout: 8000 }).then(() => true, () => false), 'empty search');
  }, { u: A });

  const AG = await ui(browser, alice);
  await tc(rec, 'UIC-LST-10', 'AGENT: can see lists but cannot create/edit/delete (buttons hidden, API refuses)', async () => {
    await go(AG, 'lists');
    eq(await AG.page.getByRole('button', { name: /New list/ }).count(), 0, 'no New list');
    eq(await AG.page.getByRole('button', { name: /^Edit / }).count(), 0, 'no edit');
    eq(await AG.page.getByRole('button', { name: /^Delete / }).count(), 0, 'no delete');
    const tok = await t.tk('alice');
    eq((await call(tok, 'POST', '/lists', { list_name: 'sneaky', list_type: 'STATIC' })).status, 403, 'API create 403');
  }, { u: AG });

  await tc(rec, 'UIC-LST-11', 'Delete list (confirm) removes it, contacts are kept', async () => {
    const before = cnt(t, '1=1');
    await go(A, 'lists');
    await p.getByRole('button', { name: 'Delete Oct webinar' }).click(); await settle(A, 800);
    ok(/Delete the list "Oct webinar"/.test(A.lastDialog || ''), 'confirm text');
    eq(dbn(`SELECT COUNT(*) FROM prospect_lists WHERE tenant_id='${t.tenantId}' AND list_name='Oct webinar'`), 0, 'deleted');
    eq(cnt(t, '1=1'), before, 'contacts kept');
    eq(await listRow(p, 'Oct webinar').count(), 0, 'gone from table');
  }, { u: A });

  await tc(rec, 'UIC-LST-12', 'Lists API failure keeps page shell', async () => {
    await failApi(p, /\/api\/lists$/, 500, 'GET');
    await go(A, 'lists?x=2'); await settle(A, 600);
    ok(await p.getByRole('heading', { name: 'Lists', exact: true }).isVisible(), 'shell');
    await p.unrouteAll({ behavior: 'ignoreErrors' });
  }, { u: A, allow: [/http 500/, /Failed to load resource/] });
  await A.ctx.close(); await AG.ctx.close();
}

// =============================== IMPORT ===============================
// /app/import is the import history; the wizard is a drawer (role=dialog "Import contacts and companies", Vector_Frontend
// src/components/import/ImportDrawer.jsx since 5451f65): File -> Columns -> Options -> Preview (dry run, nothing written)
// -> Start import (a background job; the drawer polls its progress) -> result counts + error file.
function importWizard(A, p) {
  const drawer = () => p.getByRole('dialog', { name: 'Import contacts and companies' });
  const W = {
    drawer,
    fileInput: () => drawer().locator('input[type=file]'),
    nextBtn: () => drawer().getByRole('button', { name: 'Next', exact: true }),
    async open() {
      await p.getByRole('button', { name: /New import/ }).click();
      await drawer().waitFor();
    },
    async goImport() {
      await go(A, 'import');
      await p.getByRole('heading', { name: /Import contacts and companies/ }).first().waitFor();
      await W.open();
    },
    async upload(file) { await W.fileInput().setInputFiles(file); await drawer().getByText(/rows · /).waitFor({ timeout: 20000 }); },
    /** upload, then Next to the column mapping */
    async toMap(file) { await W.upload(file); await W.nextBtn().click(); await drawer().getByText('Column in file').waitFor(); },
    select: header => drawer().getByLabel(`Import ${header} as`, { exact: true }),
    async toOptions() { await W.nextBtn().click(); await drawer().getByLabel('What to do').waitFor(); },
    async toPreview() { await W.nextBtn().click(); await drawer().getByText(/Nothing has been imported yet|Could not check/).first().waitFor({ timeout: 60000 }); },
    async result() {
      await drawer().getByText(/^Import (finished|stopped|cancelled)$/).waitFor({ timeout: 120000 });
      return drawer().innerText();
    },
    /** from the mapping step: options (optional callback), preview, start, wait for the result */
    async run(atOptions) {
      await W.toOptions();
      if (atOptions) await atOptions();
      await W.toPreview();
      await drawer().getByRole('button', { name: /Start import/ }).click();
      return W.result();
    },
    async stat(label) {
      const el = drawer().locator('div.rounded-xl').filter({ has: p.getByText(label, { exact: true }) }).first();
      return num(await el.locator('p').nth(1).innerText());
    },
    overwrite: () => drawer().getByLabel(/Overwrite values of existing records/),
  };
  return W;
}

export async function runImport(rec, browser) {
  const t = await (await Tenant.get('imports')).init();
  const alice = await t.addUser('AGENT', 'Alice');
  const A = await ui(browser, t.owner); const p = A.page;
  const W = importWizard(A, p);
  const mk = (n, extra = {}) => Array.from({ length: n }, (_, i) => ({ email: `imp${i}.${t.rid}@impco${i % 5}.example`, first: `Imp${i}`, last: `User${i}`, company: `ImpCo ${i % 5}`, title: i % 2 ? 'Manager' : 'Director', country: 'US', ...extra }));
  const header = ['Email', 'First Name', 'Last Name', 'Company', 'Job Title', 'Country'];
  const rowsOf = list => list.map(r => [r.email, r.first, r.last, r.company, r.title, r.country]);

  await tc(rec, 'UIC-IMP-01', 'Import page renders drop zone + options hidden until a file is chosen', async () => {
    await W.goImport();
    ok(await W.drawer().getByText('Drop a file here or click to choose').isVisible(), 'dropzone');
    ok(await W.nextBtn().isDisabled(), 'Next disabled until a file is read');
    eq(await W.drawer().getByLabel('What to do').count(), 0, 'no options yet');
  }, { u: A });

  await tc(rec, 'UIC-IMP-02', 'CSV preview: row/column count, suggested mapping, sample values', async () => {
    const rows = mk(12);
    const f = writeTmp('imp-basic.csv', csvText(header, rowsOf(rows)));
    await W.goImport(); await W.upload(f);
    ok(await W.drawer().getByText('12 rows · 6 columns').isVisible(), 'row/column count');
    ok(await W.nextBtn().isEnabled(), 'Next enabled');
    await W.nextBtn().click();
    eq(await W.select('Email').inputValue(), 'contact.email', 'Email auto-mapped');
    ok(await W.drawer().getByText(rows[0].email, { exact: false }).first().isVisible(), 'sample value visible');
    ok(await W.nextBtn().isEnabled(), 'mapping accepted');
  }, { u: A });

  await tc(rec, 'UIC-IMP-03', 'Run import with defaults: result counts, DB rows, owner = me, company linked', async () => {
    const rows = mk(12);
    await W.goImport(); await W.toMap(writeTmp('imp-basic.csv', csvText(header, rowsOf(rows))));
    await W.run();
    eq(await W.stat('Contacts created'), 12, 'created stat');
    eq(await W.stat('Rows with errors'), 0, 'no errors');
    eq(cnt(t, `email LIKE 'imp%.${t.rid}@%'`), 12, 'DB rows');
    eq(cnt(t, `email LIKE 'imp%.${t.rid}@%' AND owner_id='${t.owner.userId}'`), 12, 'owner');
    eq(cnt(t, `email LIKE 'imp%.${t.rid}@%' AND account_id IS NOT NULL`), 12, 'linked to company accounts');
    eq(dbn(`SELECT COUNT(*) FROM accounts WHERE tenant_id='${t.tenantId}' AND name LIKE 'ImpCo %' AND deleted_at IS NULL`), 5, '5 companies');
    await W.drawer().getByRole('link', { name: 'View contacts' }).click(); await p.waitForURL(/\/app\/contacts/);
  }, { u: A });

  // "Overwrite values of existing records" OFF: existing values stay, only empty fields are filled (blank cells never overwrite)
  await tc(rec, 'UIC-IMP-04', 'Re-import same file: update_existing OFF leaves records untouched; ON updates (no duplicates)', async () => {
    const rows = mk(12).map(r => ({ ...r, title: 'VP Updated' }));
    const f = writeTmp('imp-update.csv', csvText(header, rowsOf(rows)));
    await W.goImport(); await W.toMap(f);
    await W.run(() => W.overwrite().uncheck());
    eq(await W.stat('Contacts created'), 0, 'none created');
    eq(cnt(t, `email LIKE 'imp%.${t.rid}@%' AND designation='VP Updated'`), 0, 'titles untouched when OFF');
    await W.goImport(); await W.toMap(f);
    await W.run();
    eq(await W.stat('Contacts updated'), 12, 'updated 12');
    eq(cnt(t, `email LIKE 'imp%.${t.rid}@%' AND designation='VP Updated'`), 12, 'titles updated');
    eq(cnt(t, `email LIKE 'imp%.${t.rid}@%'`), 12, 'no duplicates');
  }, { u: A });
  await tc(rec, 'UIC-IMP-04B', 'With update_existing OFF the result card must report 0 contacts updated', async () => {
    const rows = mk(12).map(r => ({ ...r, title: 'Should Not Apply' }));
    await W.goImport(); await W.toMap(writeTmp('imp-off.csv', csvText(header, rowsOf(rows))));
    await W.run(() => W.overwrite().uncheck());
    eq(cnt(t, `designation='Should Not Apply'`), 0, 'DB untouched');
    eq(await W.stat('Contacts updated'), 0, 'result card "Contacts updated" (API reports contacts_updated=12 although nothing was written)');
  }, { u: A });

  await tc(rec, 'UIC-IMP-05', 'Blank cells never overwrite existing values', async () => {
    const rows = mk(3).map(r => ({ ...r, title: '', country: '' }));
    await W.goImport(); await W.toMap(writeTmp('imp-blank.csv', csvText(header, rowsOf(rows))));
    await W.run();
    eq(cnt(t, `email LIKE 'imp0.${t.rid}@%' AND designation='VP Updated' AND poc_country='US'`), 1, 'values preserved');
  }, { u: A });

  await tc(rec, 'UIC-IMP-06', 'Mapping: create a new custom property from a column (type Number) -> property + values stored', async () => {
    const f = writeTmp('imp-custom.csv', csvText(['Email', 'First Name', 'Deal Size'], [[`cust1.${t.rid}@cimp.example`, 'Cus', '5000'], [`cust2.${t.rid}@cimp.example`, 'Tom', '7500']]));
    await W.goImport(); await W.toMap(f);
    await W.select('Deal Size').selectOption('__new__');
    await W.drawer().getByLabel('New property label').fill('Deal Size');
    await W.drawer().getByLabel('New property type').selectOption('NUMBER');
    await W.run();
    eq(dbn(`SELECT COUNT(*) FROM contact_field_definitions WHERE tenant_id='${t.tenantId}' AND label='Deal Size' AND field_type='NUMBER'`), 1, 'property created');
    ok(/5000/.test(sql(`SELECT custom_fields FROM prospects WHERE email='cust1.${t.rid}@cimp.example'`)), 'value stored: ' + sql(`SELECT custom_fields FROM prospects WHERE email='cust1.${t.rid}@cimp.example'`));
  }, { u: A });

  await tc(rec, 'UIC-IMP-07', 'Options: add to a new list, set owner to Alice', async () => {
    const rows = mk(4).map(r => ({ ...r, email: r.email.replace('imp', 'opt') }));
    await W.goImport(); await W.toMap(writeTmp('imp-opt.csv', csvText(header, rowsOf(rows))));
    await W.run(async () => {
      await W.drawer().getByLabel('Owner of new contacts').selectOption({ label: 'Alice Test' });
      await W.drawer().getByLabel('Add imported contacts to a list').selectOption('new');
      await W.drawer().getByLabel('New list name').fill('Imported Q4');
    });
    const lid = sql(`SELECT list_id FROM prospect_lists WHERE tenant_id='${t.tenantId}' AND list_name='Imported Q4'`);
    eq(dbn(`SELECT COUNT(*) FROM prospect_list_members WHERE list_id='${lid}'`), 4, 'list members');
    eq(cnt(t, `email LIKE 'opt%.${t.rid}@%' AND owner_id='${alice.userId}'`), 4, 'owner Alice');
    await W.drawer().getByRole('link', { name: 'View contacts' }).click(); await p.waitForURL(/list_id=/);
  }, { u: A });

  await tc(rec, 'UIC-IMP-08', 'Options: existing list requires choosing one; imports into it', async () => {
    const rows = mk(2).map(r => ({ ...r, email: r.email.replace('imp', 'ex') }));
    await W.goImport(); await W.toMap(writeTmp('imp-ex.csv', csvText(header, rowsOf(rows))));
    await W.run(async () => {
      await W.drawer().getByLabel('Add imported contacts to a list').selectOption('existing');
      ok(await W.nextBtn().isDisabled(), 'disabled until list chosen');
      await W.drawer().locator('select').filter({ hasText: 'Choose a list' }).selectOption({ label: 'Imported Q4' });
    });
    const lid = sql(`SELECT list_id FROM prospect_lists WHERE tenant_id='${t.tenantId}' AND list_name='Imported Q4'`);
    eq(dbn(`SELECT COUNT(*) FROM prospect_list_members WHERE list_id='${lid}'`), 6, 'members now 6');
  }, { u: A });

  await tc(rec, 'UIC-IMP-09', 'Bad rows (invalid email, missing email, duplicate in file): counts, reasons list, error file download', async () => {
    const good = `good1.${t.rid}@bad.example`;
    const rows = [[good, 'Good', 'One'], ['not-an-email', 'Bad', 'Email'], ['', 'No', 'Email'], [`dup.${t.rid}@bad.example`, 'Dup', 'A'], [`dup.${t.rid}@bad.example`, 'Dup', 'B'], [`good2.${t.rid}@bad.example`, 'Good', 'Two']];
    await W.goImport(); await W.toMap(writeTmp('imp-bad.csv', csvText(['Email', 'First Name', 'Last Name'], rows)));
    await W.toOptions(); await W.toPreview();
    const sample = W.drawer().getByRole('list', { name: 'Sample errors' });
    ok(await sample.count() > 0 && /Row \d+:/.test(await sample.innerText()), 'error list shown in the dry run');
    await W.drawer().getByRole('button', { name: /Start import/ }).click();
    await W.result();
    const errs = await W.stat('Rows with errors');
    ok(errs >= 2, `expected >=2 error rows, got ${errs}`);
    eq(cnt(t, `email IN ('${good}','good2.${t.rid}@bad.example')`), 2, 'good rows imported');
    eq(cnt(t, `email='not-an-email'`), 0, 'bad row not imported');
    const d = await downloadVia(p, () => W.drawer().getByRole('button', { name: /Download error file/ }).click());
    const txt = d.buf.toString('utf8');
    ok(/not-an-email/.test(txt) && /reason|error/i.test(txt.split('\n')[0]), 'error file has failing row + reason column: ' + txt.slice(0, 160));
    ok(!txt.includes(good), 'error file excludes good rows');
  }, { u: A });

  await tc(rec, 'UIC-IMP-10', 'Past imports table lists runs with error counts and downloadable error file', async () => {
    await go(A, 'import');
    const row = p.locator('table').last().locator('tbody tr').filter({ hasText: 'imp-bad.csv' }).first();
    await row.waitFor({ timeout: 15000 }).catch(() => {});
    ok(await row.isVisible(), 'row for imp-bad.csv');
    const btn = row.getByRole('button', { name: /↓/ });
    const d = await downloadVia(p, () => btn.click());
    ok(d.buf.length > 20, 'error file downloads from history');
  }, { u: A });

  await tc(rec, 'UIC-IMP-11', 'Bad files: .txt / binary / empty / header-only show a clear message and no crash', async () => {
    for (const [name, content] of [['not-a-csv.txt', 'hello this is not a spreadsheet'], ['empty.csv', ''], ['header-only.csv', 'Email,First Name\n']]) {
      await W.goImport();
      await W.fileInput().setInputFiles(writeTmp(name, content)); await settle(A, 1500);
      const body = await W.drawer().innerText();
      const showedErr = await W.drawer().locator('.bg-red-50').count() > 0;
      const preview = /rows ·/.test(body);
      ok(showedErr || (preview && /\b0 rows/.test(body)), `${name}: expected an error or a 0-row preview. error=${showedErr} preview=${preview}`);
    }
  }, { u: A, allow: [/http 4\d\d POST .*\/imports\/uploads/, /Failed to load resource/] });

  await tc(rec, 'UIC-IMP-12', 'One-column CSV (email only) imports', async () => {
    const f = writeTmp('imp-one.csv', csvText(['Email'], [[`one1.${t.rid}@solo.example`], [`one2.${t.rid}@solo.example`], [`one3.${t.rid}@solo.example`]]));
    await W.goImport(); await W.upload(f);
    ok(await W.drawer().getByText('3 rows · 1 columns').isVisible(), 'preview');
    await W.nextBtn().click();
    await W.run();
    eq(await W.stat('Contacts created'), 3);
    eq(cnt(t, `email LIKE 'one_.${t.rid}@solo.example'`), 3);
  }, { u: A });

  await tc(rec, 'UIC-IMP-13', 'File without an Email/Company column: warning shown and Import disabled', async () => {
    await W.goImport(); await W.toMap(writeTmp('imp-noemail.csv', csvText(['Favourite colour', 'Shoe size'], [['blue', '42'], ['red', '38']])));
    ok(await W.drawer().getByText(/Map an Email column or a Company name\/domain column/).isVisible(), 'warning');
    ok(await W.nextBtn().isDisabled(), 'import disabled (cannot leave the mapping step)');
  }, { u: A });

  await tc(rec, 'UIC-IMP-14', 'Two columns mapped to the same property show the "last one wins" warning', async () => {
    await W.goImport(); await W.toMap(writeTmp('imp-dupmap.csv', csvText(['Email', 'Work Email'], [[`dm1.${t.rid}@dm.example`, `dm1b.${t.rid}@dm.example`]])));
    await W.select('Work Email').selectOption('contact.email');
    ok(await W.drawer().getByText(/More than one column maps to the same property/).isVisible(), 'warning');
  }, { u: A });

  await tc(rec, 'UIC-IMP-15', 'Company-only import (no email column) creates companies', async () => {
    const f = writeTmp('imp-companies.csv', csvText(['Company', 'Website', 'Industry'], [['Delta Dynamics', 'https://delta-dyn.example', 'Aero'], ['Epsilon Labs', 'https://epsilon-labs.example', 'Biotech']]));
    await W.goImport(); await W.toMap(f);
    await W.select('Company').selectOption('company.name');
    await W.select('Website').selectOption('company.website');
    await W.select('Industry').selectOption('skip');
    await W.run();
    eq(await W.stat('Companies created'), 2, 'companies created stat');
    eq(dbn(`SELECT COUNT(*) FROM accounts WHERE tenant_id='${t.tenantId}' AND name IN ('Delta Dynamics','Epsilon Labs')`), 2);
  }, { u: A });

  await tc(rec, 'UIC-IMP-16', 'Excel (.xlsx) file imports', async () => {
    const f = xlsxFile('imp-excel.xlsx', header, rowsOf(mk(5).map(r => ({ ...r, email: r.email.replace('imp', 'xl') }))));
    await W.goImport(); await W.upload(f);
    ok(await W.drawer().getByText('5 rows · 6 columns').isVisible(), 'preview');
    await W.nextBtn().click();
    await W.run();
    eq(await W.stat('Contacts created'), 5);
    eq(cnt(t, `email LIKE 'xl%.${t.rid}@%'`), 5);
  }, { u: A });

  // Cancel closes the drawer and drops the stored upload; "New import" starts from the file step again
  await tc(rec, 'UIC-IMP-17', 'Cancel resets the wizard', async () => {
    await W.goImport(); await W.toMap(writeTmp('imp-basic.csv', csvText(header, rowsOf(mk(3)))));
    await W.drawer().getByRole('button', { name: 'Cancel', exact: true }).click();
    eq(await W.drawer().count(), 0, 'drawer closed');
    await W.open();
    ok(await W.drawer().getByText('Drop a file here or click to choose').isVisible(), 'back to drop zone');
    ok(await W.nextBtn().isDisabled(), 'nothing to import');
  }, { u: A });

  await tc(rec, 'UIC-IMP-18', '"Import another file" after a run starts over', async () => {
    await W.goImport(); await W.toMap(writeTmp('imp-again.csv', csvText(['Email'], [[`again.${t.rid}@solo.example`]])));
    await W.run();
    await W.drawer().getByRole('button', { name: 'Close', exact: true }).click();
    await W.open();
    ok(await W.drawer().getByText('Drop a file here or click to choose').isVisible(), 'wizard reset');
  }, { u: A });

  await tc(rec, 'UIC-IMP-19', '1,000-row CSV imports in one run (DB count + time)', async () => {
    const rows = Array.from({ length: 1000 }, (_, i) => [`k${i}.${t.rid}@k1.example`, `K${i}`, `Row${i}`, `KCo ${i % 40}`]);
    await W.goImport(); await W.upload(writeTmp('imp-1k.csv', csvText(['Email', 'First Name', 'Last Name', 'Company'], rows)));
    ok(await W.drawer().getByText('1,000 rows · 4 columns').isVisible(), 'preview 1,000');
    await W.nextBtn().click();
    const t0 = Date.now();
    await W.run();
    const secs = (Date.now() - t0) / 1000;
    eq(await W.stat('Contacts created'), 1000, 'created stat');
    eq(cnt(t, `email LIKE 'k%.${t.rid}@k1.example'`), 1000, 'DB rows');
    ok(secs < 120, `import took ${secs}s`);
  }, { u: A });

  const AG = await ui(browser, alice);
  await tc(rec, 'UIC-IMP-20', 'AGENT without manage_prospects cannot use import (route redirected, API 403)', async () => {
    await go(AG, 'import');
    ok(!/\/app\/import/.test(AG.page.url()), 'redirected away from /import: ' + AG.page.url());
    const tok = await t.tk('alice');
    const r = await call(tok, 'GET', '/imports'); eq(r.status, 403, 'API list imports 403 (import history lists file names/counts for users without manage_prospects)');
  }, { u: AG });

  await tc(rec, 'UIC-IMP-21', 'Preview API failure shows an error message', async () => {
    await W.goImport();
    await failApi(p, /\/api\/imports\/uploads$/, 500);
    await W.fileInput().setInputFiles(writeTmp('imp-x.csv', 'Email\na@b.example\n')); await settle(A, 900);
    ok(await W.drawer().locator('.bg-red-50').count() > 0, 'error note shown');
    await p.unrouteAll({ behavior: 'ignoreErrors' });
  }, { u: A, allow: [/http 500/, /Failed to load resource/] });
  await A.ctx.close(); await AG.ctx.close();
}

export async function run(rec, browser) { await runLists(rec, browser); await runImport(rec, browser); }
if (isMain(import.meta)) standalone('lists_import', run);
