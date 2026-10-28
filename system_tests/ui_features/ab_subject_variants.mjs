// UIF-AB: A/B testing of email subject lines (BRD v2.0 BR-SF-18 / RP-104)
//   Subsequence tab: subject-variant editor per step (add / edit / delete, compliance error shown);
//   Analytics tab: per-variant stats card with the winner indicator, "Show emails by variant" (variant column)
//   and Promote winner with confirm; Schedule tab: variant badge on each email.
// Uses the ui_crm helpers (own reusable tenant "abtest", wiped on every run).  node ab_subject_variants.mjs
// The campaign starts 20 days out, so nothing is really sent; "sent" / opens are written with SQL in our tenant.
import { ui, go, settle, tc, ok, eq, must, sql, standalone, isMain, Tenant } from '../ui_crm/crm_common.mjs';

const N = 62;          // 31 per variant: 30 marked sent, 1 stays scheduled
const MIN_SENDS = 30;

export async function run(rec, browser) {
  const t = await (await Tenant.get('abtest')).init();
  const tok = t.token;
  await must(tok, 'PUT', '/api/company-profiles/sender-identity',
    { company_name: `AB Co ${t.rid}`, postal_address: '1 Test Street, Austin, TX 78701, USA' });
  const future = new Date(Date.now() + 20 * 86400000).toISOString().slice(0, 10);
  const camp = await must(tok, 'POST', '/campaigns', { campaign_name: `AB UI ${t.rid}`, start_date: future,
    respect_timezone: false, campaign_timezone: 'UTC' });
  const cid = camp.campaign_id;
  const step = await must(tok, 'POST', `/campaigns/${cid}/sequences`, { step_number: 1, wait_days: 0 });
  const seq = step.sequence_id;
  await must(tok, 'POST', '/templates', { campaign_id: cid, sequence_id: seq, subject: 'Template subject', body: 'Hello there' });
  const pids = [];
  for (let i = 0; i < N; i++) {
    const c = await must(tok, 'POST', '/contacts', { email: `ab${i}.${t.rid}@abui.example`, first_name: `Ui${i}` });
    pids.push(c.prospect_id);
  }
  await must(tok, 'POST', `/campaigns/${cid}/prospects`, { prospect_ids: pids });
  const variants = () => sql(`SELECT GROUP_CONCAT(CONCAT(label,'|',subject) ORDER BY label SEPARATOR ';') FROM email_subject_variants WHERE sequence_id='${seq}'`);
  const split = () => sql(`SELECT GROUP_CONCAT(CONCAT(IFNULL(subject_variant,'-'),':',n) ORDER BY subject_variant SEPARATOR ',') FROM (SELECT subject_variant, COUNT(*) n FROM email_messages WHERE sequence_id='${seq}' GROUP BY subject_variant) x`);

  const A = await ui(browser, t.owner); const p = A.page;
  const openTab = async (name) => {
    await go(A, `campaigns/${cid}`);
    await p.getByRole('button', { name, exact: true }).first().click(); await settle(A, 800);
  };
  const editor = () => p.getByTestId('subject-variants-step-1');

  await tc(rec, 'UIF-AB-01', 'Subsequence tab: add variants A and B in the step editor -> rows shown, stored in the DB', async () => {
    await openTab('Subsequence');
    await editor().waitFor({ timeout: 10000 });
    for (const subject of ['Quick question, {{first_name}}', 'An idea for you, {{first_name}}']) {
      await editor().getByLabel('New subject variant').fill(subject);
      await editor().getByRole('button', { name: /Add variant/ }).click(); await settle(A, 700);
    }
    await editor().getByTestId('variant-row-B').waitFor({ timeout: 8000 });
    ok(await editor().getByText('Running', { exact: true }).isVisible(), 'test shown as running');
    eq(variants(), 'A|Quick question, {{first_name}};B|An idea for you, {{first_name}}', 'variants in DB');
  }, { u: A });

  await tc(rec, 'UIF-AB-02', 'Editor: a disallowed phrase is refused with the compliance message; nothing stored', async () => {
    await openTab('Subsequence');
    await editor().getByLabel('New subject variant').fill('Claim your prize now');
    await editor().getByRole('button', { name: /Add variant/ }).click(); await settle(A, 700);
    await editor().getByRole('alert').filter({ hasText: /disallowed/i }).waitFor({ timeout: 8000 });
    eq(sql(`SELECT COUNT(*) FROM email_subject_variants WHERE sequence_id='${seq}'`), '2', 'still two variants');
  }, { u: A, allow: [/400/] });

  await tc(rec, 'UIF-AB-03', 'Editor: edit variant B subject inline, add and delete a variant C (confirm)', async () => {
    await openTab('Subsequence');
    await editor().getByTestId('variant-row-B').getByTitle('Edit variant').click();
    await editor().getByLabel('Subject of variant B').fill('A better idea, {{first_name}}');
    await editor().getByTestId('variant-row-B').getByTitle('Save').click(); await settle(A, 700);
    await editor().getByText('A better idea, {{first_name}}').waitFor({ timeout: 8000 });
    await editor().getByLabel('New subject variant').fill('Third angle');
    await editor().getByRole('button', { name: /Add variant/ }).click(); await settle(A, 700);
    await editor().getByTestId('variant-row-C').waitFor({ timeout: 8000 });
    await editor().getByTestId('variant-row-C').getByTitle('Delete variant').click(); await settle(A, 800);
    eq(await editor().getByTestId('variant-row-C').count(), 0, 'C removed');
    eq(variants(), 'A|Quick question, {{first_name}};B|A better idea, {{first_name}}', 'DB after edit + delete');
  }, { u: A });

  await must(tok, 'POST', `/campaigns/${cid}/launch`);
  const ids = label => sql(`SELECT message_id FROM email_messages WHERE sequence_id='${seq}' AND subject_variant='${label}' ORDER BY prospect_id`).split('\n').filter(Boolean);
  await tc(rec, 'UIF-AB-04', 'Launch splits the step evenly (31 / 31); Schedule tab shows a variant badge per email', async () => {
    eq(split(), 'A:31,B:31', 'even split in DB');
    await openTab('Schedule');
    await p.locator('span[title="Subject variant A"], span[title="Subject variant B"]').first().waitFor({ timeout: 10000 });
  }, { u: A });

  const a = ids('A'), b = ids('B');
  const inList = arr => arr.map(x => `'${x}'`).join(',');
  sql(`UPDATE email_messages SET status='SENT', sent_at=UTC_TIMESTAMP() WHERE message_id IN (${inList([...a.slice(0, MIN_SENDS - 1), ...b.slice(0, MIN_SENDS - 1)])})`);
  sql(`INSERT INTO email_events (event_id, message_id, tenant_id, event_type, event_time) VALUES ${[...a.slice(0, 10), ...b.slice(0, 4)].map(m => `(UUID(), '${m}', (SELECT tenant_id FROM email_messages WHERE message_id='${m}'), 2, UTC_TIMESTAMP())`).join(',')}`);
  const card = () => p.getByTestId('variant-stats-step-1');

  await tc(rec, 'UIF-AB-05', 'Analytics tab: per-variant stats card; below 30 sends the card says no winner yet and offers no Promote', async () => {
    await openTab('Analytics');
    await card().waitFor({ timeout: 10000 });
    ok((await card().getByTestId('variant-stat-A').innerText()).includes('29'), 'A sent 29');
    await card().getByTestId('variant-winner-status').filter({ hasText: /at least 30 sent emails/ }).waitFor({ timeout: 8000 });
    eq(await card().getByRole('button', { name: /Promote winner/ }).count(), 0, 'no promote button');
  }, { u: A });

  sql(`UPDATE email_messages SET status='SENT', sent_at=UTC_TIMESTAMP() WHERE message_id IN (${inList([a[MIN_SENDS - 1], b[MIN_SENDS - 1]])})`);
  await tc(rec, 'UIF-AB-06', 'Analytics: with 30 sends each the winner (A, highest open rate) is marked; "Show emails by variant" lists the variant column', async () => {
    await openTab('Analytics');
    await card().getByTestId('variant-winner-status').filter({ hasText: /Variant A has the highest open rate/ }).waitFor({ timeout: 10000 });
    ok(await card().getByTestId('variant-stat-A').getByRole('button', { name: /Promote winner/ }).isVisible(), 'promote offered on A');
    eq(await card().getByTestId('variant-stat-B').getByRole('button', { name: /Promote winner/ }).count(), 0, 'not on B');
    await card().getByRole('button', { name: /Show emails by variant/ }).click(); await settle(A, 800);
    const hist = card().getByTestId('variant-history');
    await hist.waitFor({ timeout: 8000 });
    ok(await hist.getByRole('cell', { name: 'B', exact: true }).first().isVisible(), 'variant column shows B');
  }, { u: A });

  await tc(rec, 'UIF-AB-07', 'Promote winner (confirm) -> remaining unsent emails use A, card shows promoted, editor locked', async () => {
    await openTab('Analytics');
    await card().getByRole('button', { name: /Promote winner/ }).click(); await settle(A, 1000);
    ok(/Promote variant A/.test(A.lastDialog || ''), 'confirm dialog shown');
    await card().getByTestId('variant-winner-status').filter({ hasText: /was promoted/ }).waitFor({ timeout: 8000 });
    eq(sql(`SELECT COUNT(*) FROM email_messages WHERE sequence_id='${seq}' AND status<>'SENT' AND (subject_variant<>'A' OR subject NOT LIKE 'Quick question,%')`), '0', 'pending emails switched to A');
    eq(sql(`SELECT COUNT(*) FROM email_messages WHERE sequence_id='${seq}' AND status='SENT' AND subject_variant='B' AND subject LIKE 'A better idea,%'`), '30', 'sent B emails unchanged');
    await openTab('Subsequence');
    await editor().getByText('Variant A promoted').waitFor({ timeout: 8000 });
    eq(await editor().getByLabel('New subject variant').count(), 0, 'no add form after promotion');
  }, { u: A });

  await A.ctx.close();
}
if (isMain(import.meta)) standalone('ab_subject_variants', run);
