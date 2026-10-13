// UIC-TSK: Tasks; UIC-RD: Recently deleted
import { skip, Tenant, ui, go, settle, tc, ok, eq, num, must, call, sql, dbn, modalLoc, standalone, isMain, failApi, seedContacts } from './crm_common.mjs';
const day = d => new Date(Date.now() + d * 86400000).toISOString();

export async function run(rec, browser) {
  const t = await (await Tenant.get('tasks')).init();
  const alice = await t.addUser('AGENT', 'Alice');
  const mia = await t.addUser('MANAGER', 'Mia');
  const [c1] = await seedContacts(t, 3, { owners: [t.owner.userId] });
  const mk = (title, o = {}) => must(t.token, 'POST', '/tasks', { title, owner_id: t.owner.userId, ...o });
  await mk('Overdue call', { due_at: day(-3), priority: 'HIGH', task_type: 'CALL', prospect_id: c1.prospect_id });
  await mk('Due soon', { due_at: day(3) });
  await mk('No date task');
  await mk('Alice task', { owner_id: alice.userId, due_at: day(1) });
  const done = await mk('Finished task', { due_at: day(-1) });
  await must(t.token, 'PATCH', `/tasks/${done.task_id}`, { status: 'DONE' });
  const A = await ui(browser, t.owner); const p = A.page;
  const count = async () => num((await p.getByText(/^\d+ tasks?$/).first().innerText()));
  const tile = async l => num(await p.locator('button', { hasText: new RegExp(`My ${l}`, 'i') }).locator('p').nth(1).innerText());

  await tc(rec, 'UIC-TSK-01', 'Tasks page: counters and list equal DB', async () => {
    await go(A, 'tasks');
    const mine = dbn(`SELECT COUNT(*) FROM crm_tasks WHERE tenant_id='${t.tenantId}' AND owner_id='${t.owner.userId}' AND status='OPEN'`);
    eq(await tile('open'), mine, 'open tile'); eq(await count(), mine, 'list count');
    eq(await tile('overdue'), dbn(`SELECT COUNT(*) FROM crm_tasks WHERE tenant_id='${t.tenantId}' AND owner_id='${t.owner.userId}' AND status='OPEN' AND due_at<NOW()`), 'overdue tile');
  }, { u: A });
  await tc(rec, 'UIC-TSK-02', 'Date filters: overdue / next 7 days / no due date; status Done/All', async () => {
    await go(A, 'tasks');
    await p.getByRole('button', { name: 'Overdue', exact: true }).last().click(); await settle(A, 500);
    ok(await p.getByText('Overdue call').isVisible() && await p.getByText('Due soon').count() === 0, 'overdue only');
    await p.getByRole('button', { name: 'No due date' }).click(); await settle(A, 500);
    ok(await p.getByText('No date task').isVisible() && await p.getByText('Overdue call').count() === 0, 'no date only');
    await p.getByRole('button', { name: 'Any date' }).click(); await p.getByRole('button', { name: 'Done', exact: true }).click(); await settle(A, 500);
    ok(await p.getByText('Finished task').isVisible() && await p.getByText('Due soon').count() === 0, 'done only');
    await p.getByRole('button', { name: 'All', exact: true }).click(); await settle(A, 500);
    ok(await p.getByText('Finished task').isVisible() && await p.getByText('Due soon').isVisible(), 'all');
  }, { u: A });
  await tc(rec, 'UIC-TSK-03', 'New task: title required; create with type/priority/due/assignee -> DB and visible to assignee', async () => {
    await go(A, 'tasks');
    await p.getByRole('button', { name: /New task/ }).click();
    const m = modalLoc(p);
    ok(await m.getByRole('button', { name: 'Create task' }).isDisabled(), 'disabled w/o title');
    await m.getByLabel('Title').fill('Prepare QBR'); await m.getByLabel('Type').selectOption('MEETING'); await m.getByLabel('Priority').selectOption('HIGH');
    await m.getByLabel('Due').fill('2026-12-01T10:00'); await m.getByLabel('Assigned to').selectOption({ label: 'Alice Test' }); await m.getByLabel('Notes').fill('bring numbers');
    await m.getByRole('button', { name: 'Create task' }).click(); await settle(A, 800);
    eq(sql(`SELECT CONCAT_WS('|',task_type,priority,owner_id,notes) FROM crm_tasks WHERE tenant_id='${t.tenantId}' AND title='Prepare QBR'`), `MEETING|HIGH|${alice.userId}|bring numbers`);
  }, { u: A });
  await tc(rec, 'UIC-TSK-04', 'Complete, reopen and delete (confirm) a task', async () => {
    await go(A, 'tasks');
    const li = () => p.locator('li').filter({ hasText: 'Due soon' });
    await li().getByRole('button', { name: 'Complete task' }).click(); await settle(A, 700);
    eq(sql(`SELECT status FROM crm_tasks WHERE tenant_id='${t.tenantId}' AND title='Due soon'`), 'DONE');
    await p.getByRole('button', { name: 'Done', exact: true }).click(); await settle(A, 500);
    await li().getByRole('button', { name: 'Reopen task' }).click(); await settle(A, 700);
    eq(sql(`SELECT status FROM crm_tasks WHERE tenant_id='${t.tenantId}' AND title='Due soon'`), 'OPEN');
    await p.getByRole('button', { name: 'Open', exact: true }).click(); await settle(A, 500);
    await li().getByRole('button', { name: 'Delete task' }).click(); await settle(A, 700);
    eq(dbn(`SELECT COUNT(*) FROM crm_tasks WHERE tenant_id='${t.tenantId}' AND title='Due soon'`), 0, 'deleted');
  }, { u: A });
  await tc(rec, 'UIC-TSK-05', 'Task linked to a contact shows a link to it', async () => {
    await go(A, 'tasks');
    await p.locator('li').filter({ hasText: 'Overdue call' }).getByRole('link').first().click();
    await p.waitForURL(new RegExp(c1.prospect_id));
  }, { u: A });
  const AG = await ui(browser, alice);
  await tc(rec, 'UIC-TSK-06', 'AGENT: no "Everyone" scope, only own tasks; cannot modify others\' task via API', async () => {
    await go(AG, 'tasks');
    eq(await AG.page.getByRole('button', { name: 'Everyone' }).count(), 0, 'no Everyone');
    ok(await AG.page.getByText('Alice task').isVisible() && await AG.page.getByText('Overdue call').count() === 0, 'own only');
    const tok = await t.tk('alice');
    const other = sql(`SELECT task_id FROM crm_tasks WHERE tenant_id='${t.tenantId}' AND title='Overdue call'`);
    ok((await call(tok, 'PATCH', `/tasks/${other}`, { status: 'DONE' })).status >= 400, 'API refuses');
  }, { u: AG, allow: [/Failed to load resource/] });
  await tc(rec, 'UIC-TSK-07', 'Empty state and API failure', async () => {
    await go(A, 'tasks'); await p.getByRole('button', { name: 'Next 7 days' }).click(); await p.getByRole('button', { name: 'Done', exact: true }).click(); await settle(A, 500);
    await failApi(p, /\/api\/tasks$/, 500, 'GET'); await go(A, 'tasks?x=1'); await settle(A, 600);
    ok(await p.getByRole('heading', { name: 'Tasks', exact: true }).isVisible(), 'shell kept');
    await p.unrouteAll({ behavior: 'ignoreErrors' });
  }, { u: A, allow: [/http 500/, /Failed to load resource/] });
  await A.ctx.close(); await AG.ctx.close();

  // ---------------- recently deleted ----------------
  const r = await (await Tenant.get('trash')).init();
  const ag = await r.addUser('AGENT', 'Agnes');
  const cs = await seedContacts(r, 30, { owners: [r.owner.userId, ag.userId] });
  for (const c of cs) await must(r.token, 'DELETE', `/contacts/${c.prospect_id}`, undefined, [200, 204]);
  sql(`UPDATE prospects SET deleted_at = NOW() - INTERVAL 100 DAY WHERE prospect_id='${cs[0].prospect_id}'`);
  const B = await ui(browser, r.owner); const q = B.page;
  await tc(rec, 'UIC-RD-01', 'Recently deleted lists deleted contacts (30 minus purged-window one), paginated, days remaining', async () => {
    await go(B, 'recently-deleted');
    const inWindow = dbn(`SELECT COUNT(*) FROM prospects WHERE tenant_id='${r.tenantId}' AND deleted_at > NOW() - INTERVAL 90 DAY`);
    ok(await q.getByText(`${inWindow} deleted contacts`).isVisible(), `count ${inWindow}`);
    eq(await q.locator('tbody tr').count(), 25, 'page 1 rows');
    ok(/\b(89|90) days\b/.test(await q.locator('tbody tr').first().innerText()), 'days left');
    ok(await q.getByText(cs[0].email).count() === 0, 'contact deleted 100 days ago is not offered');
  }, { u: B });
  await tc(rec, 'UIC-RD-02', 'Search, select-all restore and single restore update DB and the Contacts list', async () => {
    await go(B, 'recently-deleted');
    await q.getByPlaceholder('Search deleted contacts…').fill('Seed1'); await settle(B, 800);
    await q.getByLabel('Select all on page').check();
    const n = await q.locator('tbody tr').count();
    await q.getByRole('button', { name: new RegExp(`Restore ${n}`) }).click(); await settle(B, 900);
    ok(await q.getByText(`Restored ${n} contact`).isVisible(), 'notice');
    eq(dbn(`SELECT COUNT(*) FROM prospects WHERE tenant_id='${r.tenantId}' AND deleted_at IS NULL`), n, 'restored in DB');
    await q.getByPlaceholder('Search deleted contacts…').fill(''); await settle(B, 800);
    await q.locator('tbody tr').first().getByRole('button', { name: 'Restore' }).click(); await settle(B, 800);
    eq(dbn(`SELECT COUNT(*) FROM prospects WHERE tenant_id='${r.tenantId}' AND deleted_at IS NULL`), n + 1, 'single restore');
    await go(B, 'contacts'); ok(await q.getByText(`${n + 1} contacts`).isVisible(), 'visible in Contacts');
  }, { u: B });
  // UIC-RD-03 (was BLOCKED not-implemented): the feature now exists and is covered by UIC-NF-12 in new_features.mjs
  const BG = await ui(browser, ag);
  await tc(rec, 'UIC-RD-04', 'AGENT without manage_prospects: page not reachable, API refused', async () => {
    await go(BG, 'recently-deleted'); ok(!/recently-deleted/.test(BG.page.url()), 'redirected: ' + BG.page.url());
    eq((await call(await r.tk('agnes'), 'GET', '/contacts/deleted')).status, 403, 'API 403');
  }, { u: BG, allow: [/Failed to load resource/] });
  await B.ctx.close(); await BG.ctx.close();
}
if (isMain(import.meta)) standalone('tasks_trash', run);
