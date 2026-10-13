// UIF-WH: outbound webhooks (BRD v2.0 BR-SF-20 / RP-106b)
//   Workspace > Webhooks page: admin-only nav entry, add endpoint (event checkboxes, secret shown once with copy),
//   validation messages, Send test (result banner + delivery log + payload viewer), edit events, rotate secret,
//   turn off, delete.
// Starts a tiny receiver on this host (0.0.0.0:WEBHOOK_UI_RX_PORT, default 18766) that the containers reach at
// WEBHOOK_RX_HOST (default 172.18.0.1); the stack runs with ENVIRONMENT=development (http:// + private allowed).
// Uses the ui_crm helpers (own reusable tenant "webhooks", webhook rows wiped on every run).  node webhooks.mjs
import http from 'node:http';
import { ui, go, settle, tc, ok, eq, must, call, sql, standalone, isMain, modalLoc, Tenant } from '../ui_crm/crm_common.mjs';
import { sidebarLink } from './_ui_extra.mjs';

const PORT = Number(process.env.WEBHOOK_UI_RX_PORT || 18766);
const HOST = process.env.WEBHOOK_RX_HOST || '172.18.0.1';

export async function run(rec, browser) {
  const received = [];
  const server = http.createServer((req, res) => {
    let body = '';
    req.on('data', c => { body += c; });
    req.on('end', () => { received.push({ path: req.url, headers: req.headers, body }); res.writeHead(200); res.end('ok'); });
  });
  await new Promise(r => server.listen(PORT, '0.0.0.0', r));

  const t = await (await Tenant.get('webhooks')).init();
  sql(`DELETE FROM webhook_deliveries WHERE tenant_id='${t.tenantId}'; DELETE FROM webhook_endpoints WHERE tenant_id='${t.tenantId}'`);
  const agent = await t.addUser('AGENT', 'Wade');
  const URL1 = `http://${HOST}:${PORT}/ui-${t.rid}-${Date.now()}`;
  const row = () => sql(`SELECT CONCAT(active,'|',JSON_LENGTH(event_types)) FROM webhook_endpoints WHERE tenant_id='${t.tenantId}' AND url='${URL1}'`);
  const endpointId = () => sql(`SELECT endpoint_id FROM webhook_endpoints WHERE tenant_id='${t.tenantId}' AND url='${URL1}'`);

  const A = await ui(browser, t.owner); const p = A.page;
  let firstSecret = '';

  await tc(rec, 'UIF-WH-01', 'Admin sees Workspace > Webhooks in the sidebar; empty state on a new workspace', async () => {
    await go(A, 'dashboard');
    await (await sidebarLink(p, 'Webhooks', 'Workspace')).click();   // the sidebar keeps only the active group open
    await p.waitForURL(/\/app\/webhooks$/, { timeout: 8000 }); await settle(A, 600);
    await p.getByRole('heading', { name: 'Webhooks', exact: true }).waitFor();
    await p.getByText('No webhook endpoints yet').waitFor();
  }, { u: A });

  await tc(rec, 'UIF-WH-02', 'Validation: non-http(s) URL is refused with the server message; no events -> "Choose at least one event"; nothing stored', async () => {
    await go(A, 'webhooks');
    await p.getByTestId('webhook-add').click();
    const m = modalLoc(p);
    await m.getByTestId('webhook-url').fill('ftp://files.example.com/in');
    await m.getByTestId('webhook-event-contact.created').check();
    await m.getByTestId('webhook-save').click(); await settle(A, 700);
    await m.getByText(/must start with https:\/\//).waitFor({ timeout: 8000 });
    await m.getByTestId('webhook-url').fill(URL1);
    await m.getByTestId('webhook-event-contact.created').uncheck();
    await m.getByTestId('webhook-save').click(); await settle(A, 300);
    await m.getByText('Choose at least one event').waitFor();
    eq(sql(`SELECT COUNT(*) FROM webhook_endpoints WHERE tenant_id='${t.tenantId}'`), '0', 'nothing created');
    await m.getByRole('button', { name: 'Cancel', exact: true }).click();
  }, { u: A, allow: [/422/] });

  await tc(rec, 'UIF-WH-03', 'Add endpoint: tick events, save -> secret shown once with Copy; list shows URL, Active and the secret hint; DB row', async () => {
    await go(A, 'webhooks');
    await p.getByTestId('webhook-add').click();
    const m = modalLoc(p);
    await m.getByTestId('webhook-url').fill(URL1);
    await m.getByTestId('webhook-description').fill('UI test receiver');
    for (const ev of ['contact.created', 'deal.won', 'email.replied']) await m.getByTestId(`webhook-event-${ev}`).check();
    await m.getByTestId('webhook-save').click(); await settle(A, 900);
    const secret = p.getByTestId('webhook-secret');
    await secret.waitFor({ timeout: 8000 });
    firstSecret = await secret.inputValue();
    ok(firstSecret.startsWith('whsec_') && firstSecret.length >= 40, `secret shown: ${firstSecret.slice(0, 8)}`);
    ok(await p.getByTestId('webhook-copy-secret').isVisible(), 'copy button');
    await p.getByTestId('webhook-secret-done').click(); await settle(A, 400);
    const card = p.getByTestId('webhook-row').filter({ hasText: URL1 });
    await card.waitFor();
    ok(await card.getByTestId('webhook-status').filter({ hasText: 'Active' }).isVisible(), 'Active badge');
    ok(await card.getByText(firstSecret.slice(-4)).isVisible(), 'secret hint (last 4)');
    eq(await p.getByTestId('webhook-secret').count(), 0, 'secret no longer on the page');
    eq(row(), '1|3', 'DB: active, 3 events');
    ok(sql(`SELECT secret FROM webhook_endpoints WHERE url='${URL1}'`).startsWith('enc:v1:'), 'secret encrypted at rest');
  }, { u: A });

  await tc(rec, 'UIF-WH-04', 'Send test: banner "Test event delivered", receiver got a signed webhook.test, delivery log lists it and View shows the payload', async () => {
    await go(A, 'webhooks');
    const card = p.getByTestId('webhook-row').filter({ hasText: URL1 });
    const before = received.length;
    await card.getByTestId('webhook-test').click(); await settle(A, 900);
    await card.getByTestId('webhook-test-result').filter({ hasText: 'Test event delivered' }).waitFor({ timeout: 15000 });
    const got = received.slice(before).filter(r => r.headers['x-outreach-event'] === 'webhook.test');
    ok(got.length === 1, `receiver got ${got.length} test event(s)`);
    ok(/^t=\d+,v1=[0-9a-f]{64}$/.test(got[0].headers['x-outreach-signature'] || ''), 'signature header format');
    await card.getByTestId('webhook-deliveries').click(); await settle(A, 700);
    const logRow = card.getByTestId('webhook-delivery-row').first();
    await logRow.waitFor({ timeout: 8000 });
    ok(await logRow.getByText('webhook.test').isVisible(), 'event in the log');
    ok(await logRow.getByText('SUCCEEDED').isVisible(), 'status in the log');
    await logRow.getByRole('button', { name: 'View' }).click();
    await p.getByTestId('webhook-payload').filter({ hasText: '"type": "webhook.test"' }).waitFor({ timeout: 8000 });
    await modalLoc(p).getByRole('button', { name: 'Close' }).click();
  }, { u: A });

  await tc(rec, 'UIF-WH-05', 'Edit endpoint: add an event -> chips and DB event_types updated', async () => {
    await go(A, 'webhooks');
    const card = p.getByTestId('webhook-row').filter({ hasText: URL1 });
    await card.getByTestId('webhook-edit').click();
    const m = modalLoc(p);
    await m.getByTestId('webhook-event-campaign.paused').check();
    await m.getByTestId('webhook-save').click(); await settle(A, 900);
    await card.getByText('Campaign paused', { exact: true }).waitFor({ timeout: 8000 });
    eq(row(), '1|4', 'DB: 4 events');
  }, { u: A });

  await tc(rec, 'UIF-WH-06', 'Rotate secret (confirm) -> a new secret is shown once and differs from the first', async () => {
    await go(A, 'webhooks');
    const card = p.getByTestId('webhook-row').filter({ hasText: URL1 });
    await card.getByTestId('webhook-rotate').click(); await settle(A, 900);
    const secret = p.getByTestId('webhook-secret');
    await secret.waitFor({ timeout: 8000 });
    const second = await secret.inputValue();
    ok(second.startsWith('whsec_') && second !== firstSecret, 'new secret');
    await p.getByTestId('webhook-secret-done').click();
  }, { u: A });

  await tc(rec, 'UIF-WH-07', 'Turn off -> badge "Off", DB inactive; turn on again -> Active', async () => {
    await go(A, 'webhooks');
    const card = p.getByTestId('webhook-row').filter({ hasText: URL1 });
    await card.getByTestId('webhook-toggle').click(); await settle(A, 800);
    await card.getByTestId('webhook-status').filter({ hasText: 'Off' }).waitFor({ timeout: 8000 });
    eq(row(), '0|4', 'DB inactive');
    await card.getByTestId('webhook-toggle').click(); await settle(A, 800);
    await card.getByTestId('webhook-status').filter({ hasText: 'Active' }).waitFor({ timeout: 8000 });
    eq(row(), '1|4', 'DB active again');
  }, { u: A });

  const W = await ui(browser, agent); const pw = W.page;
  await tc(rec, 'UIF-WH-08', 'Agent: no Webhooks nav entry; /app/webhooks says only admins can manage webhooks; API is 403', async () => {
    await go(W, 'dashboard');
    eq(await (await sidebarLink(pw, 'Webhooks', 'Workspace')).count(), 0, 'no nav entry (Workspace group expanded)');
    await go(W, 'webhooks');
    await pw.getByText('Only admins can manage webhooks.').waitFor();
    const r = await call(await t.tokenOf(agent), 'GET', '/integrations/webhooks');
    eq(r.status, 403, 'API refuses agents');
  }, { u: W });

  await tc(rec, 'UIF-WH-09', 'Delete (confirm) -> card gone, empty state back, DB endpoint and its deliveries removed', async () => {
    const id = endpointId();
    await go(A, 'webhooks');
    const card = p.getByTestId('webhook-row').filter({ hasText: URL1 });
    await card.getByTestId('webhook-delete').click(); await settle(A, 900);
    await p.getByText('No webhook endpoints yet').waitFor({ timeout: 8000 });
    eq(sql(`SELECT COUNT(*) FROM webhook_endpoints WHERE endpoint_id='${id}'`), '0', 'endpoint deleted');
    eq(sql(`SELECT COUNT(*) FROM webhook_deliveries WHERE endpoint_id='${id}'`), '0', 'log deleted');
  }, { u: A });

  // sanity: the API agrees with the UI
  const list = await must(t.token, 'GET', '/integrations/webhooks');
  if (list.items.length) console.warn('webhooks left over:', list.items.length);
  await A.ctx.close(); await W.ctx.close();
  server.close();
}
if (isMain(import.meta)) standalone('webhooks', run);
