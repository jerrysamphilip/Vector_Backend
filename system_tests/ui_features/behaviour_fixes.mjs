// UIF-BF: UI cases for the behaviour-fix round
//   U21 Unsubscribe / Resubscribe buttons have their own accessible names (not the whole label),
//   "Create deal" from a contact shows the linked contact (read-only), @mention pickers on contact and
//   deal notes only offer people who can see the record, campaign wizard "AI unavailable" notice (D1),
//   Send Now continues in batches with a progress label and one result summary.
// Uses the ui_crm helpers (own reusable tenant "behfix", wiped on every run).  node behaviour_fixes.mjs
import crypto from 'node:crypto';
import { ui, go, settle, tc, ok, eq, must, sql, sq, dbn, standalone, isMain, modalLoc, Tenant } from '../ui_crm/crm_common.mjs';

const NOTICE = 'AI unavailable - starter templates were used, please review';

export async function run(rec, browser) {
  const t = await (await Tenant.get('behfix')).init();
  const aaron = await t.addUser('AGENT', 'Aaron');
  const zed = await t.addUser('AGENT', 'Zed');
  const contact = await must(t.token, 'POST', '/contacts', { email: `una.${t.rid}@bf.example`, first_name: 'Una', last_name: 'Subscribe',
    owner_id: aaron.userId });
  const id = contact.prospect_id;
  const consent = () => sql(`SELECT consent_status FROM prospects WHERE prospect_id='${id}'`);

  const A = await ui(browser, t.owner); const p = A.page;

  await tc(rec, 'UIF-BF-01', 'U21: Unsubscribe / Resubscribe are buttons with their own accessible names; confirm -> DB consent flips', async () => {
    await go(A, `contacts/${id}`);
    const group = p.getByRole('group', { name: 'Email subscription' });
    await group.waitFor();
    const unsub = group.getByRole('button', { name: 'Unsubscribe', exact: true });
    ok(await unsub.isVisible(), 'Unsubscribe button named "Unsubscribe"');
    // the button must not be wrapped in a <label> (its name would become the whole label text)
    eq(await unsub.evaluate(b => !!b.closest('label')), false, 'button not inside a label');
    await unsub.click(); await settle(A, 900);
    ok(/Unsubscribe this contact/.test(A.lastDialog || ''), 'confirm dialog shown');
    eq(consent(), 'UNSUBSCRIBED', 'DB unsubscribed');
    const resub = group.getByRole('button', { name: 'Resubscribe', exact: true });
    await resub.waitFor({ timeout: 8000 });
    await resub.click(); await settle(A, 900);
    eq(consent(), 'OPT_IN', 'DB resubscribed');
  }, { u: A });

  await tc(rec, 'UIF-BF-02', 'Create deal from the contact record shows the linked contact (read-only) and links the deal to it', async () => {
    await go(A, `contacts/${id}`);
    await p.getByRole('button', { name: '+ Create deal' }).click();
    const m = modalLoc(p);
    const field = m.getByLabel('Contact', { exact: true });
    await field.waitFor();
    eq(await field.inputValue(), 'Una Subscribe', 'contact name shown');
    ok(!(await field.isEditable()), 'contact field is read-only');
    await m.getByLabel('Name *').fill(`BF deal ${t.rid}`);
    await m.getByRole('button', { name: 'Create', exact: true }).click(); await settle(A, 900);
    await p.waitForURL(/\/app\/deals\//, { timeout: 8000 });
    eq(sql(`SELECT prospect_id FROM opportunities WHERE name=${sq(`BF deal ${t.rid}`)} AND tenant_id='${t.tenantId}'`), id, 'deal linked to the contact');
  }, { u: A });

  await tc(rec, 'UIF-BF-03', 'Contact note @mention picker asks for prospect_id and offers only people who can see the contact', async () => {
    await go(A, `contacts/${id}`);
    const reqP = p.waitForRequest(r => /mentionable-users/.test(r.url()), { timeout: 8000 });
    const note = p.getByRole('textbox', { name: 'Note' }).first();
    await note.click(); await note.fill('@');
    await note.press('End');
    const r = await reqP;
    ok(new URL(r.url()).searchParams.get('prospect_id') === id, `picker request carries prospect_id: ${r.url()}`);
    const list = p.getByRole('listbox', { name: 'Mention a colleague' });
    await list.waitFor({ timeout: 8000 });
    const names = await list.getByRole('option').allInnerTexts();
    ok(names.some(n => /Aaron/.test(n)), `contact owner offered: ${names}`);
    ok(!names.some(n => /Zed/.test(n)), `agent who cannot see the contact not offered: ${names}`);
  }, { u: A });

  await tc(rec, 'UIF-BF-04', 'Deal note @mention picker asks for opportunity_id', async () => {
    const stage = (await must(t.token, 'GET', '/sales/stages')).find(s => s.status === 'OPEN');
    const deal = await must(t.token, 'POST', '/opportunities', { name: `BF mention ${t.rid}`, owner_id: aaron.userId, stage_id: stage.stage_id });
    await go(A, `deals/${deal.opportunity_id}`);
    const reqP = p.waitForRequest(r => /mentionable-users/.test(r.url()), { timeout: 8000 });
    const note = p.getByRole('textbox', { name: 'Note' }).first();
    await note.click(); await note.fill('@'); await note.press('End');
    const r = await reqP;
    eq(new URL(r.url()).searchParams.get('opportunity_id'), deal.opportunity_id, 'picker request carries opportunity_id');
    const list = p.getByRole('listbox', { name: 'Mention a colleague' });
    await list.waitFor({ timeout: 8000 });
    const names = await list.getByRole('option').allInnerTexts();
    ok(!names.some(n => /Zed/.test(n)), `agent who cannot see the deal not offered: ${names}`);
  }, { u: A });

  await tc(rec, 'UIF-BF-05', 'Send Now: confirm -> batches continue with a cursor until done -> one result summary (Sent/Failed/Skipped)', async () => {
    const camp = await must(t.token, 'POST', '/campaigns', { campaign_name: `BF send now ${t.rid}`, start_date: new Date(Date.now() + 20 * 864e5).toISOString().slice(0, 10) });
    const cid = camp.campaign_id;
    await must(t.token, 'POST', `/campaigns/${cid}/sequences`, { step_number: 1, wait_days: 0 });
    await must(t.token, 'POST', `/campaigns/${cid}/content/stub`);
    const seq = sql(`SELECT sequence_id FROM email_sequences WHERE campaign_id='${cid}' AND step_number=1`);
    const tpl = sql(`SELECT template_id FROM email_templates WHERE campaign_id='${cid}' LIMIT 1`);
    // unsubscribed recipients: every message is stopped by the send-time guard, nothing reaches SES
    const values = [];
    for (let i = 0; i < 30; i++) {
      const c = await must(t.token, 'POST', '/contacts', { email: `sn${i}.${t.rid}@bf.example`, first_name: `Sn${i}`, consent_status: 'UNSUBSCRIBED' });
      values.push(`(${[crypto.randomUUID(), cid, c.prospect_id, seq, tpl, 'OUTBOUND', 'QUEUED', `sn${i}.${t.rid}@bf.example`, 'BF'].map(sq).join(',')}, UTC_TIMESTAMP())`);
    }
    sql(`INSERT INTO email_messages (message_id, campaign_id, prospect_id, sequence_id, template_id, direction, status, to_email, subject, scheduled_at) VALUES ${values.join(',')}`);
    sql(`UPDATE campaigns SET status='ACTIVE' WHERE campaign_id='${cid}'`);

    const calls = [];
    const onReq = r => { if (/\/send-now/.test(r.url())) calls.push(r.url()); };
    p.on('request', onReq);
    await go(A, `campaigns/${cid}`);
    A.lastDialog = null;
    await p.getByRole('button', { name: /Send Now/ }).click();
    const deadline = Date.now() + 60000;
    while (!/Success/.test(A.lastDialog || '') && Date.now() < deadline) await p.waitForTimeout(300);
    p.off('request', onReq);
    ok(/Success/.test(A.lastDialog || ''), `result summary shown: ${A.lastDialog}`);
    ok(/Sent: 0/.test(A.lastDialog) && /Failed: 0/.test(A.lastDialog) && /Skipped: 30/.test(A.lastDialog), `totals over all batches: ${A.lastDialog}`);
    eq(calls.length, 2, `two batch requests (25 + 5): ${calls}`);
    ok(new URL(calls[1]).searchParams.get('cursor') && new URL(calls[1]).searchParams.get('since'), 'second request continues the run');
    eq(dbn(`SELECT COUNT(*) FROM email_messages WHERE campaign_id='${cid}' AND status='CANCELLED'`), 30, 'every message handled once');
  }, { u: A });

  await tc(rec, 'UIF-BF-06', 'Campaign wizard: when the generation response says starter templates were used, the "AI unavailable" notice is shown (and only then)', async () => {
    const dom = `bfw${t.rid}.example`;
    const local = `wiz${crypto.randomBytes(2).toString('hex')}`;
    await must(t.token, 'POST', '/inboxes', { email_address: `${local}@${dom}`, smtp_host: 'smtp.invalid', smtp_username: local,
      smtp_password: `Smtp-${t.rid}`, warmup_enabled: false });
    const pc = await must(t.token, 'POST', '/contacts', { email: `wiz.${t.rid}@bfw.example`, first_name: 'Wiz', company_name: 'Co' });
    const listName = `BF wiz list ${t.rid}`;
    const l = await must(t.token, 'POST', '/lists', { list_name: listName });
    await must(t.token, 'POST', `/lists/${l.list_id}/members`, { prospect_ids: [pc.prospect_id] });

    await go(A, 'campaigns/new?fresh=true', { wait: 1200 });
    await p.getByPlaceholder('e.g. Q4 Enterprise Outreach').fill(`BF wizard ${t.rid}`);
    await p.getByText(`${local}@${dom}`).first().click();
    await p.getByRole('button', { name: /Save & Continue/ }).click();
    await p.getByText(/Sequence Engine/).first().waitFor({ timeout: 15000 });
    await p.getByRole('button', { name: /Add Follow-up Step/ }).click();
    await p.getByRole('button', { name: /Save Sequence/ }).click();
    await p.getByText(listName).first().waitFor({ timeout: 15000 });
    const respP = p.waitForResponse(r => /\/campaigns\/[^/]+\/generations$/.test(new URL(r.url()).pathname) && r.request().method() === 'POST', { timeout: 120000 });
    await p.getByRole('button', { name: /Process & Generate/ }).click();
    const resp = await respP;
    const data = await resp.json();
    await p.getByText(/Persona Blueprints/).first().waitFor({ timeout: 30000 });
    const notice = p.getByTestId('ai-fallback-notice');
    if (data.ai_fallback_used) {
      await notice.waitFor({ timeout: 8000 });
      eq((await notice.innerText()).trim(), NOTICE, 'notice text');
      const cid = data.campaign_id;
      eq(dbn(`SELECT COUNT(*) FROM email_templates WHERE campaign_id='${cid}'`) >= 2, true, 'a template for each of the 2 steps');
    } else {
      eq(await notice.count(), 0, 'no notice when the AI wrote every step');
    }
  }, { u: A, allow: [/campaigns\/lists/] });

  await A.ctx.close();
}
if (isMain(import.meta)) standalone('behaviour_fixes', run);
