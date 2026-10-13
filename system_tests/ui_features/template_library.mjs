// UIF-TL: Email template library (BRD v2.0 BR-SF-10), screen /app/templates (src/pages/TemplateLibrary.jsx)
//   list + empty state, create with merge-field buttons, validation messages, edit, category filter and search,
//   sample preview, render for a real contact (POST /template-library/{id}/render), usage count + ordering,
//   permission differences (owner/admin vs AGENT; private templates), delete, API failure handling.
// Uses the ui_crm helpers (own reusable tenant "tmpllib"; message_templates rows are wiped at the start of every run).
//   node template_library.mjs
import { sidebarLink } from './_ui_extra.mjs';
import { ui, go, settle, tc, ok, eq, must, call, sql, sq, standalone, isMain, modalLoc, failApi, Tenant } from '../ui_crm/crm_common.mjs';

export async function run(rec, browser) {
  const t = await (await Tenant.get('tmpllib')).init();
  sql(`DELETE FROM message_templates WHERE tenant_id=${sq(t.tenantId)}`);
  const agent = await t.addUser('AGENT', 'Tara');
  const agent2 = await t.addUser('AGENT', 'Bea');
  const contact = await must(t.token, 'POST', '/contacts', { email: `zoe.${t.rid}@tl.example`, first_name: 'Zoe', last_name: `Render${t.rid}`,
    company_name: `Zeta Labs ${t.rid}`, designation: 'CFO' });
  const NAME1 = `Follow up ${t.rid}`;
  const NAME1b = `Follow up v2 ${t.rid}`;
  const tplRow = (name) => sql(`SELECT CONCAT_WS('|', template_id, IFNULL(category,''), shared, owner_id, usage_count) FROM message_templates WHERE tenant_id=${sq(t.tenantId)} AND name=${sq(name)}`);
  const count = () => sql(`SELECT COUNT(*) FROM message_templates WHERE tenant_id=${sq(t.tenantId)}`);
  const rows = (p) => p.locator('tbody tr');
  const row = (p, name) => p.locator('tbody tr').filter({ hasText: name });
  const names = async (p) => (await p.locator('tbody tr td:first-child p:first-child').allInnerTexts()).map(s => s.replace(/\s*private$/, '').trim());

  const A = await ui(browser, t.owner); const p = A.page;

  await tc(rec, 'UIF-TL-01', 'Sidebar Templates opens /app/templates: heading, New template button and the empty state on a new workspace', async () => {
    await go(A, 'dashboard');
    await (await sidebarLink(p, 'Templates', 'Outreach')).click();
    await p.waitForURL(/\/app\/templates$/, { timeout: 8000 }); await settle(A, 600);
    await p.getByRole('heading', { name: 'Email templates', exact: true }).waitFor();
    ok(await p.getByRole('button', { name: /New template/ }).isVisible(), 'New template button');
    await p.getByText('No templates yet').waitFor();
    eq(await rows(p).count(), 0, 'no table rows');
  }, { u: A });

  await tc(rec, 'UIF-TL-02', 'Validation: empty name, then empty subject / body -> server message in the modal; nothing stored', async () => {
    await go(A, 'templates');
    await p.getByRole('button', { name: /New template/ }).click();
    const m = modalLoc(p);
    await m.getByRole('heading', { name: 'New template' }).waitFor();
    await m.getByRole('button', { name: 'Save template' }).click(); await settle(A, 500);
    await m.getByText('The template needs a name').waitFor({ timeout: 8000 });
    await m.getByLabel('Name', { exact: true }).fill(NAME1);
    await m.getByRole('button', { name: 'Save template' }).click(); await settle(A, 500);
    await m.getByText('The template needs a subject').waitFor({ timeout: 8000 });
    await m.getByLabel('Subject', { exact: true }).fill('Hi');
    await m.getByRole('button', { name: 'Save template' }).click(); await settle(A, 500);
    await m.getByText('The template needs a body').waitFor({ timeout: 8000 });
    eq(count(), '0', 'DB: nothing created');
    await m.getByRole('button', { name: 'Cancel', exact: true }).click();
    eq(await p.getByRole('heading', { name: 'New template' }).count(), 0, 'modal closed');
  }, { u: A, allow: [/http 400 POST .*template-library/, /status of 400/] });

  await tc(rec, 'UIF-TL-03', 'Create: merge-field buttons insert at the cursor of subject / body; row shows name, category, owner, Used 0; DB row shared', async () => {
    await go(A, 'templates');
    await p.getByRole('button', { name: /New template/ }).click();
    const m = modalLoc(p);
    await m.getByLabel('Name', { exact: true }).fill(NAME1);
    await m.getByLabel('Category', { exact: true }).fill('Follow-up');
    const subj = m.getByLabel('Subject', { exact: true });
    await subj.fill('Hello ');
    await subj.click(); await subj.press('End');
    await m.getByText('Insert a merge field into the subject').waitFor();
    await m.getByRole('button', { name: 'First name', exact: true }).click();
    eq(await subj.inputValue(), 'Hello {{first_name}}', 'subject token inserted at the cursor');
    const body = m.getByLabel('Body', { exact: true });
    await body.fill('We help ');
    await body.click(); await body.press('End');
    await m.getByRole('button', { name: 'Company', exact: true }).click();
    await body.press('End'); await body.pressSequentially(' grow.');
    eq(await body.inputValue(), 'We help {{company_name}} grow.', 'body token inserted');
    ok(await m.getByLabel(/Share with everyone/).isChecked(), 'shared by default');
    await m.getByRole('button', { name: 'Save template' }).click(); await settle(A, 900);
    eq(await p.getByRole('heading', { name: 'New template' }).count(), 0, 'modal closed after save');
    const r = row(p, NAME1); await r.waitFor({ timeout: 8000 });
    const txt = await r.innerText();
    ok(txt.includes('Follow-up'), `category in row: ${txt}`);
    ok(txt.includes('Hello {{first_name}}'), 'subject under the name');
    ok(/Owner/.test(txt), `owner name in row: ${txt}`);
    eq((await r.locator('td').nth(3).innerText()).trim(), '0', 'Used 0');
    const [, cat, shared, owner, used] = tplRow(NAME1).split('|');
    eq([cat, shared, owner, used], ['Follow-up', '1', t.owner.userId, '0'], 'DB row');
    eq(sql(`SELECT subject FROM message_templates WHERE tenant_id=${sq(t.tenantId)} AND name=${sq(NAME1)}`), 'Hello {{first_name}}', 'DB subject');
  }, { u: A });

  await tc(rec, 'UIF-TL-04', 'Edit: modal opens pre-filled; rename + new subject -> row and DB updated', async () => {
    await go(A, 'templates');
    await row(p, NAME1).getByRole('button', { name: 'Edit', exact: true }).click();
    const m = modalLoc(p);
    await m.getByRole('heading', { name: 'Edit template' }).waitFor();
    eq(await m.getByLabel('Name', { exact: true }).inputValue(), NAME1, 'pre-filled name');
    eq(await m.getByLabel('Category', { exact: true }).inputValue(), 'Follow-up', 'pre-filled category');
    await m.getByLabel('Name', { exact: true }).fill(NAME1b);
    await m.getByLabel('Subject', { exact: true }).fill('Quick follow-up for {{first_name}}');
    await m.getByRole('button', { name: 'Save template' }).click(); await settle(A, 900);
    await row(p, NAME1b).waitFor({ timeout: 8000 });
    ok((await row(p, NAME1b).innerText()).includes('Quick follow-up for {{first_name}}'), 'new subject in row');
    eq(sql(`SELECT COUNT(*) FROM message_templates WHERE tenant_id=${sq(t.tenantId)} AND name=${sq(NAME1)}`), '0', 'old name gone');
    eq(sql(`SELECT subject FROM message_templates WHERE tenant_id=${sq(t.tenantId)} AND name=${sq(NAME1b)}`), 'Quick follow-up for {{first_name}}', 'DB subject');
  }, { u: A });

  // two more templates for filtering (API)
  const intro = await must(t.token, 'POST', '/template-library', { name: `Intro ${t.rid}`, category: 'Intro', subject: 'Meet {{your_name}}', body: 'Hi {{first_name}} from {{company_name}}' });
  await must(t.token, 'POST', '/template-library', { name: `Plain ${t.rid}`, subject: 'Plain subject', body: 'Plain body' });

  await tc(rec, 'UIF-TL-05', 'Category filter lists the categories and narrows the list; search box filters by name / subject', async () => {
    await go(A, 'templates');
    const sel = p.getByRole('combobox', { name: 'Category' });
    const opts = await sel.locator('option').allInnerTexts();
    eq(opts, ['All categories', 'Follow-up', 'Intro'], 'category options');
    eq((await names(p)).length, 3, 'all three listed');
    await sel.selectOption('Follow-up'); await settle(A, 700);
    eq(await names(p), [NAME1b], 'Follow-up only');
    await sel.selectOption('Intro'); await settle(A, 700);
    eq(await names(p), [`Intro ${t.rid}`], 'Intro only');
    await sel.selectOption(''); await settle(A, 700);
    eq((await names(p)).length, 3, 'back to all');
    await p.getByPlaceholder('Search templates').fill('Plain'); await settle(A, 900);
    eq(await names(p), [`Plain ${t.rid}`], 'search by name');
    await p.getByPlaceholder('Search templates').fill('Meet'); await settle(A, 900);
    eq(await names(p), [`Intro ${t.rid}`], 'search by subject');
    await p.getByPlaceholder('Search templates').fill(`nomatch${t.rid}`); await settle(A, 900);
    await p.getByText('No templates yet').waitFor();
  }, { u: A });

  await tc(rec, 'UIF-TL-06', 'Preview: modal with the template name and sample values substituted (no {{tokens}} left)', async () => {
    await go(A, 'templates');
    await row(p, `Intro ${t.rid}`).getByRole('button', { name: 'Preview', exact: true }).click(); await settle(A, 800);
    const m = modalLoc(p);
    await m.getByRole('heading', { name: `Intro ${t.rid}` }).waitFor();
    await m.getByText('Preview with sample contact values').waitFor();
    const txt = await m.innerText();
    ok(txt.includes('Meet Owner Admin'), `subject with sender name: ${txt.slice(0, 200)}`);
    ok(txt.includes('Hi Alex from Acme Corp'), 'body with sample contact values');
    ok(!txt.includes('{{'), 'no unreplaced merge tokens');
    await m.getByRole('button', { name: 'Close' }).click();
  }, { u: A });

  await tc(rec, 'UIF-TL-07', 'Render for a real contact (API): contact values substituted, sample=false; unknown contact -> 404', async () => {
    const r = await must(t.token, 'POST', `/template-library/${intro.template_id}/render`, { prospect_id: contact.prospect_id });
    eq(r.subject, 'Meet Owner Admin', 'subject');
    eq(r.body, `Hi Zoe from Zeta Labs ${t.rid}`, 'body');
    eq(r.sample, false, 'sample flag');
    const bad = await call(t.token, 'POST', `/template-library/${intro.template_id}/render`, { prospect_id: '00000000-0000-0000-0000-000000000000' });
    eq(bad.status, 404, 'unknown contact');
    const none = await call(t.token, 'POST', '/template-library/00000000-0000-0000-0000-000000000000/render', {});
    eq(none.status, 404, 'unknown template');
    eq(tplRow(`Intro ${t.rid}`).split('|')[4], '0', 'render without count_use does not count a use');
  });

  await tc(rec, 'UIF-TL-08', 'Usage count: two renders with count_use -> Used 2 in the list and the template sorts first', async () => {
    for (let i = 0; i < 2; i++) await must(t.token, 'POST', `/template-library/${intro.template_id}/render`, { prospect_id: contact.prospect_id, count_use: true });
    eq(tplRow(`Intro ${t.rid}`).split('|')[4], '2', 'DB usage_count');
    await go(A, 'templates');
    eq((await names(p))[0], `Intro ${t.rid}`, 'most used first');
    eq((await row(p, `Intro ${t.rid}`).locator('td').nth(3).innerText()).trim(), '2', 'Used column');
  }, { u: A });

  const G = await ui(browser, agent); const pg = G.page;
  await tc(rec, 'UIF-TL-09', 'AGENT sees shared templates with Preview only (no Edit / Delete); API edit / delete of an admin template -> 403', async () => {
    await go(G, 'templates');
    const r = row(pg, NAME1b); await r.waitFor({ timeout: 8000 });
    ok(await r.getByRole('button', { name: 'Preview', exact: true }).isVisible(), 'Preview');
    eq(await r.getByRole('button', { name: 'Edit', exact: true }).count(), 0, 'no Edit');
    eq(await r.getByRole('button', { name: 'Delete', exact: true }).count(), 0, 'no Delete');
    const tk = await t.tokenOf(agent);
    const id = tplRow(NAME1b).split('|')[0];
    eq((await call(tk, 'PATCH', `/template-library/${id}`, { name: 'hijack' })).status, 403, 'PATCH 403');
    eq((await call(tk, 'DELETE', `/template-library/${id}`)).status, 403, 'DELETE 403');
    eq(tplRow(NAME1b).split('|')[0], id, 'template unchanged');
  }, { u: G });

  const PRIV = `Private ${t.rid}`;
  await tc(rec, 'UIF-TL-10', 'AGENT creates a private template: "private" marker, own Edit / Delete; another agent cannot see or open it', async () => {
    await go(G, 'templates');
    await pg.getByRole('button', { name: /New template/ }).click();
    const m = modalLoc(pg);
    await m.getByLabel('Name', { exact: true }).fill(PRIV);
    await m.getByLabel('Category', { exact: true }).fill(`Secret-${t.rid}`);
    await m.getByLabel('Subject', { exact: true }).fill('Private subject');
    await m.getByLabel('Body', { exact: true }).fill('Private body');
    await m.getByLabel(/Share with everyone/).uncheck();
    await m.getByRole('button', { name: 'Save template' }).click(); await settle(G, 900);
    const r = row(pg, PRIV); await r.waitFor({ timeout: 8000 });
    ok(await r.getByText('private', { exact: true }).isVisible(), 'private marker');
    ok(await r.getByRole('button', { name: 'Edit', exact: true }).isVisible(), 'own Edit');
    ok(await r.getByRole('button', { name: 'Delete', exact: true }).isVisible(), 'own Delete');
    const [id, , shared, owner] = tplRow(PRIV).split('|');
    eq([shared, owner], ['0', agent.userId], 'DB private, owned by the agent');
    const tk2 = await t.tokenOf(agent2);
    const list2 = await must(tk2, 'GET', '/template-library');
    ok(!list2.items.some(x => x.name === PRIV), 'other agent: not listed');
    eq((await call(tk2, 'POST', `/template-library/${id}/render`, {})).status, 404, 'other agent: render 404');
  }, { u: G });

  await tc(rec, 'UIF-TL-11', 'Category dropdown only offers categories of templates the user can see (no category of another user\'s private template)', async () => {
    const list2 = await must(await t.tokenOf(agent2), 'GET', '/template-library');
    ok(!list2.categories.includes(`Secret-${t.rid}`), `categories leak another user's private template category: ${JSON.stringify(list2.categories)}`);
  });

  const SHARED_AG = `Agent shared ${t.rid}`;
  await tc(rec, 'UIF-TL-12', 'Admin can edit a template an AGENT shared (Edit shown, rename saved)', async () => {
    await must(await t.tokenOf(agent), 'POST', '/template-library', { name: SHARED_AG, subject: 'S', body: 'B', shared: true });
    await go(A, 'templates');
    await row(p, SHARED_AG).getByRole('button', { name: 'Edit', exact: true }).click();
    const m = modalLoc(p);
    await m.getByLabel('Name', { exact: true }).fill(`${SHARED_AG} (edited)`);
    await m.getByRole('button', { name: 'Save template' }).click(); await settle(A, 900);
    await row(p, `${SHARED_AG} (edited)`).waitFor({ timeout: 8000 });
    eq(tplRow(`${SHARED_AG} (edited)`).split('|')[3], agent.userId, 'owner unchanged');
  }, { u: A });

  await tc(rec, 'UIF-TL-13', 'Save fails (API 500): modal stays open with the error, nothing stored', async () => {
    await go(A, 'templates');
    await failApi(p, /\/template-library$/, 500, 'POST');
    try {
      const before = count();
      await p.getByRole('button', { name: /New template/ }).click();
      const m = modalLoc(p);
      await m.getByLabel('Name', { exact: true }).fill(`Fails ${t.rid}`);
      await m.getByLabel('Subject', { exact: true }).fill('s');
      await m.getByLabel('Body', { exact: true }).fill('b');
      await m.getByRole('button', { name: 'Save template' }).click(); await settle(A, 800);
      await m.getByText('injected failure').waitFor({ timeout: 8000 });
      ok(await m.getByRole('heading', { name: 'New template' }).isVisible(), 'modal still open');
      eq(count(), before, 'nothing stored');
      await m.getByRole('button', { name: 'Cancel', exact: true }).click();
    } finally { await p.unrouteAll({ behavior: 'ignoreErrors' }); }
  }, { u: A, allow: [/500/, /Failed to load resource/] });

  await tc(rec, 'UIF-TL-14', 'Delete fails (API 500): the row stays and the user is told the delete failed', async () => {
    await go(A, 'templates');
    await failApi(p, /\/template-library\/[^/]+$/, 500, 'DELETE');
    try {
      await row(p, `Plain ${t.rid}`).getByRole('button', { name: 'Delete', exact: true }).click(); await settle(A, 900);
      ok(await row(p, `Plain ${t.rid}`).isVisible(), 'row still listed');
      const shown = await p.getByText(/injected failure|could not|couldn't|failed|went wrong/i).count();
      ok(shown > 0, 'no error message shown after a failed delete (the mutation has no onError)');
    } finally { await p.unrouteAll({ behavior: 'ignoreErrors' }); }
  }, { u: A, allow: [/500/, /Failed to load resource/] });

  await tc(rec, 'UIF-TL-15', 'List fails (API 500): an error is shown, not the "No templates yet" empty state', async () => {
    await failApi(p, /\/template-library$/, 500, 'GET');
    try {
      await go(A, 'templates', { wait: 3500 });   // react-query retries before giving up
      const empty = await p.getByText('No templates yet').count();
      const err = await p.getByText(/injected failure|could not load|couldn't load|failed to load|went wrong|try again/i).count();
      ok(empty === 0 && err > 0, `on a failed load the page shows ${empty ? 'the empty state "No templates yet"' : 'no error message'}`);
    } finally { await p.unrouteAll({ behavior: 'ignoreErrors' }); }
  }, { u: A, allow: [/500/, /Failed to load resource/] });

  await tc(rec, 'UIF-TL-16', 'Delete (confirm): row gone, DB row removed; Preview / Edit of a deleted template id via API -> 404', async () => {
    await go(A, 'templates');
    const id = tplRow(`Plain ${t.rid}`).split('|')[0];
    await row(p, `Plain ${t.rid}`).getByRole('button', { name: 'Delete', exact: true }).click(); await settle(A, 900);
    ok(/Delete "Plain/.test(A.lastDialog || ''), `confirm dialog: ${A.lastDialog}`);
    await row(p, `Plain ${t.rid}`).waitFor({ state: 'detached', timeout: 8000 });
    eq(sql(`SELECT COUNT(*) FROM message_templates WHERE template_id=${sq(id)}`), '0', 'DB row deleted');
    eq((await call(t.token, 'POST', `/template-library/${id}/render`, {})).status, 404, 'render 404');
    eq((await call(t.token, 'PATCH', `/template-library/${id}`, { name: 'x' })).status, 404, 'patch 404');
  }, { u: A });

  await tc(rec, 'UIF-TL-17', 'Deleting every visible template brings the empty state back', async () => {
    const tk = await t.tokenOf(agent);
    for (const it of (await must(t.token, 'GET', '/template-library')).items) await must(t.token, 'DELETE', `/template-library/${it.template_id}`);
    for (const it of (await must(tk, 'GET', '/template-library')).items) await must(tk, 'DELETE', `/template-library/${it.template_id}`);
    await go(A, 'templates');
    await p.getByText('No templates yet').waitFor({ timeout: 8000 });
    eq(count(), '0', 'DB empty');
  }, { u: A });

  await A.ctx.close(); await G.ctx.close();
}
if (isMain(import.meta)) standalone('template_library', run);
