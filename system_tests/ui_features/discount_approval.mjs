// UIF-DA: quote discount approval (BRD v2.0 BR-SF-17 / RP-103)
//   Sales settings "Discount approval" threshold, the approval box in the quote (Submit / Approve / Reject),
//   the badge on the deal's proposals, the status gate, and Pipeline -> "Discount approvals" (Awaiting my approval).
// Uses the ui_crm helpers (own reusable tenant "discappr", wiped on every run).  node discount_approval.mjs
import { ui, go, settle, tc, ok, eq, must, call, sql, standalone, isMain, modalLoc, Tenant } from '../ui_crm/crm_common.mjs';

export async function run(rec, browser) {
  const t = await (await Tenant.get('discappr')).init();
  const mgr = await t.addUser('MANAGER', 'Mona', { sales_level: 2 });
  const rep = await t.addUser('AGENT', 'Remy', { sales_level: 3, manager_id: mgr.userId });
  const repTok = await t.tokenOf(rep);
  const stage = (await must(repTok, 'GET', '/sales/stages')).find(s => s.status === 'OPEN');
  const quote = async (title, discount) => {
    const opp = await must(repTok, 'POST', '/opportunities', { name: `Deal ${title} ${t.rid}`, stage_id: stage.stage_id });
    const p = await must(repTok, 'POST', '/proposals', { opportunity_id: opp.opportunity_id, title: `${title} ${t.rid}` });
    await must(repTok, 'PUT', `/proposals/${p.proposal_id}/lines`, { lines: [
      { description: 'Seats', quantity: 10, unit_price: 100, discount_pct: 5 },
      { description: 'Onboarding', quantity: 1, unit_price: 1000, discount_pct: discount }] });
    return { ...p, opportunity_id: opp.opportunity_id };
  };
  const approval = id => sql(`SELECT approval_status FROM proposals WHERE proposal_id='${id}'`);

  const A = await ui(browser, t.owner); const pa = A.page;
  await tc(rec, 'UIF-DA-01', 'Sales settings: admin sets the discount approval threshold to 15% (saved in the workspace settings)', async () => {
    await go(A, 'sales-settings');
    const box = pa.getByTestId('discount-approval-settings');
    await box.waitFor();
    await box.getByLabel('Discount approval threshold').fill('15');
    await box.getByRole('button', { name: 'Save', exact: true }).click(); await settle(A, 800);
    await box.getByText('Saved').waitFor({ timeout: 8000 });
    const s = await must(t.token, 'GET', '/quotes/approval-settings');
    eq(s.discount_approval_threshold, 15, 'threshold stored');
  }, { u: A });

  const q1 = await quote('Flow', 25);
  const R = await ui(browser, rep); const pr = R.page;
  await tc(rec, 'UIF-DA-02', 'Quote above the limit: status change to Sent is refused with a message; DB status stays DRAFT', async () => {
    await go(R, `deals/${q1.opportunity_id}`);
    await pr.getByLabel('Proposal status').first().selectOption('SENT'); await settle(R, 800);
    await pr.getByText(/cannot be sent or accepted until a manager approves it/).first().waitFor({ timeout: 8000 });
    eq(sql(`SELECT status FROM proposals WHERE proposal_id='${q1.proposal_id}'`), 'DRAFT', 'status unchanged');
  }, { u: R, allow: [/409/] });

  await tc(rec, 'UIF-DA-03', 'Quote modal shows "Approval needed"; owner submits with a note -> "Awaiting approval", DB PENDING, manager notified', async () => {
    await go(R, `deals/${q1.opportunity_id}`);
    await pr.getByRole('button', { name: /Quote & PDF/ }).first().click();
    const m = modalLoc(pr);
    const panel = m.getByTestId('approval-panel');
    await panel.waitFor({ timeout: 8000 });
    ok(await panel.getByText('Approval needed').isVisible(), 'approval needed badge');
    await panel.getByLabel('Approval note').fill('Strategic logo');
    await panel.getByRole('button', { name: /Submit for approval/ }).click(); await settle(R, 900);
    await panel.getByText('Awaiting approval').waitFor({ timeout: 8000 });
    eq(approval(q1.proposal_id), 'PENDING', 'DB pending');
    ok(Number(sql(`SELECT COUNT(*) FROM notifications WHERE user_id='${mgr.userId}' AND kind='QUOTE_APPROVAL'`)) >= 1, 'manager notified');
  }, { u: R });

  const M = await ui(browser, mgr); const pm = M.page;
  await tc(rec, 'UIF-DA-04', 'Pipeline -> Discount approvals: manager sees the quote under "Awaiting my approval"; Reject is disabled without a reason; Approve -> DB APPROVED', async () => {
    await go(M, 'pipeline?tab=approvals');
    const row = pm.getByTestId('approval-row').filter({ hasText: `Flow ${t.rid}` });
    await row.waitFor({ timeout: 8000 });
    ok(await row.getByRole('button', { name: /Reject/ }).isDisabled(), 'reject needs a reason');
    await row.getByLabel('Approval reason').fill('OK for this logo');
    await row.getByRole('button', { name: /Approve/ }).click(); await settle(M, 900);
    eq(approval(q1.proposal_id), 'APPROVED', 'DB approved');
    eq(sql(`SELECT approval_decided_by FROM proposals WHERE proposal_id='${q1.proposal_id}'`), mgr.userId, 'approver recorded');
    await pm.getByTestId('approvals-empty').waitFor({ timeout: 8000 });
  }, { u: M });

  await tc(rec, 'UIF-DA-05', 'Deal page shows the "Discount approved" badge and the quote can now be sent', async () => {
    await go(R, `deals/${q1.opportunity_id}`);
    await pr.getByTestId('approval-badge').filter({ hasText: 'Discount approved' }).first().waitFor({ timeout: 8000 });
    await pr.getByLabel('Proposal status').first().selectOption('SENT'); await settle(R, 900);
    eq(sql(`SELECT status FROM proposals WHERE proposal_id='${q1.proposal_id}'`), 'SENT', 'sent');
  }, { u: R });

  const q2 = await quote('Reject', 40);
  await must(repTok, 'POST', `/proposals/${q2.proposal_id}/approval/submit`, { note: 'please' });
  await tc(rec, 'UIF-DA-06', 'Manager rejects from the quote modal with a reason; owner sees "Discount rejected" with the reason', async () => {
    await go(M, `deals/${q2.opportunity_id}`);
    await pm.getByRole('button', { name: /Quote & PDF/ }).first().click();
    const panel = modalLoc(pm).getByTestId('approval-panel');
    await panel.waitFor({ timeout: 8000 });
    await panel.getByLabel('Approval reason').fill('Too deep');
    await panel.getByRole('button', { name: /Reject/ }).click(); await settle(M, 900);
    await panel.getByText('Discount rejected').waitFor({ timeout: 8000 });
    eq(approval(q2.proposal_id), 'REJECTED', 'DB rejected');
    eq(sql(`SELECT approval_reason FROM proposals WHERE proposal_id='${q2.proposal_id}'`), 'Too deep', 'reason stored');
    await go(R, `deals/${q2.opportunity_id}`);
    await pr.getByTestId('approval-badge').filter({ hasText: 'Discount rejected' }).first().waitFor({ timeout: 8000 });
  }, { u: M });

  // leave the workspace with approval off for other suites
  await call(t.token, 'PUT', '/quotes/approval-settings', { discount_approval_threshold: null });
  await A.ctx.close(); await R.ctx.close(); await M.ctx.close();
}
if (isMain(import.meta)) standalone('discount_approval', run);
