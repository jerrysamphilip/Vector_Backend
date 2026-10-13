// UIF-AC: admin UI controls round
//   list rename (Lists page, Contacts "List:" chip, Prospect lists), Team: "Resend invite", sales hierarchy
//   ("Hierarchy" modal, invite with Sales level / Reports to, "Team tree" link), Domain health: "Add domain"
//   with the DNS records panel and "Check status".
// Uses the ui_crm helpers (own reusable tenant "admctl", wiped on every run).  node admin_controls.mjs
import { ui, go, settle, tc, ok, eq, must, call, sql, standalone, isMain, modalLoc, Tenant } from '../ui_crm/crm_common.mjs';

export async function run(rec, browser) {
  const t = await (await Tenant.get('admctl')).init();
  const mgr = await t.addUser('MANAGER', 'Marta', { sales_level: 2, manager_id: null });
  const rep = await t.addUser('AGENT', 'Rafael', { sales_level: null, manager_id: null });
  const stamp = `${t.rid}${Date.now().toString(36)}`;

  const A = await ui(browser, t.owner); const pa = A.page;

  // ── Lists ────────────────────────────────────────────────
  const l1 = await must(t.token, 'POST', '/lists', { list_name: `Webinar ${stamp}`, list_type: 'STATIC' });
  await tc(rec, 'UIF-AC-01', 'Lists: the pencil opens "Edit list"; renaming and describing a list is saved', async () => {
    await go(A, 'lists');
    await pa.getByRole('button', { name: `Edit Webinar ${stamp}` }).click();
    const m = modalLoc(pa);
    await m.getByPlaceholder('e.g. Q4 webinar invitees').fill(`Webinar Q4 ${stamp}`);
    await m.getByRole('button', { name: 'Save', exact: true }).click(); await settle(A, 800);
    eq(sql(`SELECT list_name FROM prospect_lists WHERE list_id='${l1.list_id}'`), `Webinar Q4 ${stamp}`, 'renamed in DB');
    await pa.getByText(`Webinar Q4 ${stamp}`).first().waitFor({ timeout: 8000 });
  }, { u: A });

  await tc(rec, 'UIF-AC-02', 'Contacts filtered by a list: "Rename list" on the List chip renames it and updates the chip', async () => {
    await go(A, `contacts?list_id=${l1.list_id}`);
    await pa.getByRole('button', { name: 'Rename list' }).click();
    const m = modalLoc(pa);
    const name = m.getByLabel('List name');
    await name.waitFor();
    await settle(A, 400);
    await name.fill(`Chip ${stamp}`);
    await m.getByLabel('List description').fill('Renamed from the chip');
    await m.getByRole('button', { name: 'Save', exact: true }).click(); await settle(A, 900);
    eq(sql(`SELECT list_name FROM prospect_lists WHERE list_id='${l1.list_id}'`), `Chip ${stamp}`, 'renamed in DB');
    eq(sql(`SELECT description FROM prospect_lists WHERE list_id='${l1.list_id}'`), 'Renamed from the chip', 'description in DB');
    await pa.getByText(`List: Chip ${stamp}`).waitFor({ timeout: 8000 });
  }, { u: A });

  const upId = sql('SELECT UUID()');
  sql(`INSERT INTO prospect_lists (list_id, tenant_id, list_name, source_type, list_type, uploaded_by, uploaded_at) VALUES ('${upId}', '${t.tenantId}', 'upload-${stamp}.csv', 'CSV', 'STATIC', '${t.owner.userId}', NOW())`);
  await tc(rec, 'UIF-AC-03', 'Prospect lists: "Rename" on an uploaded list saves the new name', async () => {
    await go(A, 'prospects');
    const row = pa.locator('tr').filter({ hasText: `upload-${stamp}.csv` });
    await row.hover();
    await row.getByRole('button', { name: `Rename upload-${stamp}.csv` }).click();
    const m = modalLoc(pa);
    await m.getByLabel('List name').fill(`Trade show ${stamp}`);
    await m.getByRole('button', { name: 'Save', exact: true }).click(); await settle(A, 900);
    eq(sql(`SELECT list_name FROM prospect_lists WHERE list_id='${upId}'`), `Trade show ${stamp}`, 'renamed in DB');
  }, { u: A });

  // ── Team: resend invite ──────────────────────────────────
  const invitee = `invitee-${stamp}@uitest.example`;
  await call(t.token, 'POST', '/api/users/invite', { email: invitee, role: 'AGENT' });
  sql(`UPDATE invitations SET created_at = NOW() - INTERVAL 2 MINUTE WHERE email='${invitee}'`);
  await tc(rec, 'UIF-AC-04', 'Team: "Resend invite" on a pending invite issues a new invitation (old one revoked) and shows the result banner', async () => {
    await go(A, 'team');
    const row = pa.locator('tr').filter({ hasText: invitee });
    await row.getByRole('button', { name: /Resend invite/ }).click(); await settle(A, 1200);
    await pa.getByText(new RegExp(`Invitation sent to ${invitee.replace('.', '\\.')}|Account created`)).first().waitFor({ timeout: 10000 });
    eq(sql(`SELECT COUNT(*) FROM invitations WHERE email='${invitee}' AND status='PENDING'`), '1', 'one pending');
    ok(Number(sql(`SELECT COUNT(*) FROM invitations WHERE email='${invitee}' AND status='REVOKED'`)) >= 1, 'old revoked');
    // Straight away again: refused with a message (once a minute)
    await row.getByRole('button', { name: /Resend invite/ }).click(); await settle(A, 900);
    await pa.getByText(/Try again in \d+ seconds/).first().waitFor({ timeout: 8000 });
  }, { u: A, allow: [/429/] });

  // ── Team: sales hierarchy ────────────────────────────────
  await tc(rec, 'UIF-AC-05', 'Team: "Hierarchy" sets Sales level 3 reporting to an L2 manager; Reports to never offers the person themselves', async () => {
    await go(A, 'team');
    await pa.locator('tr').filter({ hasText: rep.email }).getByRole('button', { name: 'Hierarchy' }).click();
    const m = pa.getByRole('dialog', { name: 'Sales hierarchy' });
    await m.getByLabel('Sales level').selectOption('3');
    const options = await m.getByLabel('Reports to').locator('option').allTextContents();
    ok(options.some(o => o.startsWith('Marta')), `manager offered: ${options}`);
    ok(!options.some(o => o.startsWith('Rafael')), 'self not offered');
    await m.getByLabel('Reports to').selectOption({ label: options.find(o => o.startsWith('Marta')) });
    await m.getByRole('button', { name: 'Save', exact: true }).click(); await settle(A, 900);
    eq(sql(`SELECT CONCAT(sales_level, ':', manager_id) FROM users WHERE user_id='${rep.userId}'`), `3:${mgr.userId}`, 'stored');
    await pa.locator('tr').filter({ hasText: rep.email }).getByText(/Reports to Marta/).waitFor({ timeout: 8000 });
  }, { u: A });

  await tc(rec, 'UIF-AC-06', 'Team: a manager cannot be put under someone in their own team (descendants are not offered)', async () => {
    await go(A, 'team');
    await pa.locator('tr').filter({ hasText: mgr.email }).getByRole('button', { name: 'Hierarchy' }).click();
    const m = pa.getByRole('dialog', { name: 'Sales hierarchy' });
    await m.getByLabel('Sales level').selectOption('4');
    const options = await m.getByLabel('Reports to').locator('option').allTextContents();
    ok(!options.some(o => o.startsWith('Rafael')), `own report not offered: ${options}`);
    await m.getByRole('button', { name: 'Cancel' }).click();
  }, { u: A });

  const placed = `placed-${stamp}@uitest.example`;
  await tc(rec, 'UIF-AC-07', 'Team: inviting with Sales level 4 and Reports to stores the hierarchy for the new account', async () => {
    await go(A, 'team');
    await pa.getByRole('button', { name: /Invite Member/ }).click();
    await pa.getByPlaceholder('colleague@company.com').fill(placed);
    await pa.getByLabel('Sales level').selectOption('4');
    const options = await pa.getByLabel('Reports to').locator('option').allTextContents();
    await pa.getByLabel('Reports to').selectOption({ label: options.find(o => o.startsWith('Rafael')) });
    await pa.getByRole('button', { name: /Send Invite/ }).click(); await settle(A, 1500);
    eq(sql(`SELECT CONCAT(sales_level, ':', manager_id) FROM users WHERE email='${placed}'`), `4:${rep.userId}`, 'stored');
  }, { u: A });

  await tc(rec, 'UIF-AC-08', 'Team: "Team tree" opens the read-only hierarchy (Sales team)', async () => {
    await go(A, 'team');
    await pa.getByRole('link', { name: /Team tree/ }).click(); await settle(A, 800);
    ok(pa.url().includes('/app/sales-team'), pa.url());
    await pa.getByText('Marta').first().waitFor({ timeout: 8000 });
  }, { u: A });

  // ── Domain health: add domain ────────────────────────────
  const domain = `ui-${stamp}.example.com`;
  await tc(rec, 'UIF-AC-09', 'Domain health: "Add domain" validates the name, then shows the SPF / DKIM / DMARC records to publish', async () => {
    await go(A, 'domain-health');
    await pa.getByRole('button', { name: /Add domain/ }).first().click();
    const m = pa.getByRole('dialog', { name: 'Add sending domain' });
    await m.getByLabel('Domain name').fill('http://bad');
    await m.getByRole('button', { name: /Add domain/ }).click();
    await m.getByRole('alert').waitFor({ timeout: 5000 });
    await m.getByLabel('Domain name').fill(domain.toUpperCase());
    await m.getByRole('button', { name: /Add domain/ }).click(); await settle(A, 1500);
    const panel = pa.getByTestId('dns-records-panel');
    await panel.waitFor({ timeout: 15000 });
    for (const host of [`mail.${domain}`, `_dmarc.${domain}`]) ok(await panel.getByText(host, { exact: true }).count() >= 1, `record for ${host}`);
    eq(sql(`SELECT COUNT(*) FROM sending_domains WHERE tenant_id='${t.tenantId}' AND domain_name='${domain}'`), '1', 'stored');
  }, { u: A, allow: [/400/] });

  await tc(rec, 'UIF-AC-10', 'DNS panel: "Check status" runs the diagnostics and refreshes SPF / DMARC from UNKNOWN', async () => {
    const panel = pa.getByTestId('dns-records-panel');
    await panel.getByRole('button', { name: /Check status/ }).click();
    await panel.getByRole('button', { name: /Check status/ }).waitFor({ timeout: 60000 });
    await settle(A, 800);
    const spf = await panel.getByTestId('status-spf').textContent();
    ok(!/UNKNOWN/.test(spf), `spf refreshed: ${spf}`);
    ok(sql(`SELECT spf_status FROM sending_domains WHERE tenant_id='${t.tenantId}' AND domain_name='${domain}'`) !== 'UNKNOWN', 'DB refreshed');
    await pa.getByRole('dialog', { name: `DNS records for ${domain}` }).getByRole('button', { name: 'Done' }).click();
  }, { u: A });

  await tc(rec, 'UIF-AC-11', 'Domain card: Details -> "DNS records" reopens the records for that domain', async () => {
    await go(A, 'domain-health');
    // innermost matching box = the domain's own card (the section around all cards matches too)
    const card = pa.locator('div.bg-white.rounded-2xl').filter({ has: pa.getByTitle(domain) }).last();
    await card.getByRole('button', { name: 'Details' }).click();
    await card.getByRole('button', { name: /DNS records/ }).click();
    const dlg = pa.getByRole('dialog', { name: `DNS records for ${domain}` });
    await dlg.getByTestId('dns-records-panel').waitFor({ timeout: 10000 });
    await dlg.getByRole('button', { name: 'Close' }).click();
  }, { u: A });

  await call(t.token, 'DELETE', `/deliverability/domains/${domain}`);
  await A.ctx.close();
}
if (isMain(import.meta)) standalone('admin_controls', run);
