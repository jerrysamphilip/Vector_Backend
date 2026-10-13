// UIF-A11Y: accessibility smoke, data-driven over the key screens (SCREENS in _ui_extra.mjs + a few more) and the main modals.
//   per screen  01-18  every interactive element (button, link, textbox, combobox, checkbox ...) has an accessible name
//                      (Chromium's computed accessibility tree via CDP; the detail lists role, selector and HTML of the offenders)
//               21-38  exactly one level-1 heading
//               61-78  keyboard: Tab from the top of the page reaches the screen's primary action (screens that have one)
//   per modal   41-55  (3 per modal) role=dialog named by its title + aria-modal; focus moves in and Tab / Shift+Tab stay
//                      inside (focus trap); Escape closes it and focus returns to the element that opened it
// Desktop 1440x900. Uses the ui_crm helpers (own reusable tenant "a11y", wiped on every run).   node a11y_smoke.mjs
import { ui, go, settle, tc, ok, must, standalone, isMain, Tenant, WEB } from '../ui_crm/crm_common.mjs';
import { SCREENS, anon, unnamedControls, overlay, focusInside, activeDesc } from './_ui_extra.mjs';

const pad = n => String(n).padStart(2, '0');

const ROUTES = [
  ...SCREENS,
  { key: 'templates', name: 'Template library', route: () => 'templates', primary: p => p.getByRole('button', { name: /New template/ }) },
  { key: 'tasks', name: 'Tasks', route: () => 'tasks', primary: null },
  { key: 'accounts', name: 'Companies', route: () => 'accounts', primary: null },
  { key: 'leads', name: 'Leads', route: () => 'leads', primary: p => p.getByRole('button', { name: /New lead/ }) },
  { key: 'sales-settings', name: 'Sales settings', route: () => 'sales-settings', primary: p => p.getByRole('button', { name: 'Save settings' }) },
  { key: 'security', name: 'Account security', route: () => 'account/security', primary: null },
];

const MODALS = [
  { name: 'Add contact', route: 'contacts', title: 'Add contact', trigger: p => p.getByRole('button', { name: /Add contact/ }).first() },
  { name: 'New opportunity', route: 'deals', title: 'New opportunity', trigger: p => p.getByRole('button', { name: /New opportunity/ }) },
  { name: 'Change password', route: 'dashboard', title: 'Change Password', pre: p => p.getByRole('button', { name: 'Account' }).click(),
    trigger: p => p.getByRole('button', { name: /Change password/ }), returnTo: p => p.getByRole('button', { name: 'Account' }) },
  { name: 'New template', route: 'templates', title: 'New template', trigger: p => p.getByRole('button', { name: /New template/ }) },
  { name: 'Add webhook endpoint', route: 'webhooks', title: 'Add webhook endpoint', trigger: p => p.getByTestId('webhook-add') },
];

async function isFocused(loc) { return loc.evaluate(el => el === document.activeElement || el.contains(document.activeElement)).catch(() => false); }

async function forceClose(page) {
  if (!(await page.locator('div.fixed.inset-0').count())) return;
  for (const name of ['Close', 'Cancel']) {
    const b = overlay(page).getByRole('button', { name, exact: true });
    if (await b.count()) { await b.first().click().catch(() => {}); await page.waitForTimeout(300); return; }
  }
  await page.mouse.click(3, 3).catch(() => {});
}

export async function run(rec, browser) {
  const t = await (await Tenant.get('a11y')).init();
  const contact = await must(t.token, 'POST', '/contacts', { email: `al.${t.rid}@a11y.example`, first_name: 'Ally', last_name: `Access${t.rid}`, company_name: `A11y Co ${t.rid}` });
  const data = { contactId: contact.prospect_id };
  const A = await ui(browser, t.owner);
  const L = await anon(browser);

  const load = async (s) => {
    const u = s.route ? A : L;
    if (s.route) await go(u, s.route(data), { wait: 1000 });
    else { await u.page.goto(`${WEB}/login`); await settle(u, 600); }
    return u;
  };

  for (const [i, s] of ROUTES.entries()) {
    const u = s.route ? A : L; const p = u.page;
    await tc(rec, `UIF-A11Y-${pad(1 + i)}`, `${s.name}: every button / link / form control has an accessible name`, async () => {
      await load(s);
      const bad = await unnamedControls(p);
      ok(bad.length === 0, `${bad.length} unnamed control(s): ${bad.slice(0, 6).join(' || ')}`);
    }, { u });
    await tc(rec, `UIF-A11Y-${pad(21 + i)}`, `${s.name}: exactly one h1`, async () => {
      await load(s);
      const h1 = p.getByRole('heading', { level: 1 });
      const n = await h1.count();
      ok(n === 1, `${n} level-1 headings${n ? ': ' + (await h1.allInnerTexts()).map(x => JSON.stringify(x.trim().slice(0, 40))).join(', ') : ''}`);
    }, { u });
    if (s.primary) {
      await tc(rec, `UIF-A11Y-${pad(61 + i)}`, `${s.name}: Tab from the top of the page reaches the primary action`, async () => {
        await load(s);
        await p.evaluate(() => { document.activeElement?.blur(); window.scrollTo(0, 0); });
        const target = s.primary(p);
        await target.waitFor({ state: 'visible', timeout: 8000 });
        let reached = 0;
        for (let k = 1; k <= 150 && !reached; k++) { await p.keyboard.press('Tab'); if (await isFocused(target)) reached = k; }
        ok(reached > 0, `primary action not reached in 150 Tab presses (focus ended on ${await activeDesc(p)})`);
      }, { u });
    }
  }

  for (const [k, M] of MODALS.entries()) {
    const base = 41 + 3 * k; const p = A.page;
    const open = async () => {
      await go(A, M.route, { wait: 800 });
      if (M.pre) await M.pre(p);
      const trig = M.trigger(p);
      await trig.focus(); await p.keyboard.press('Enter');   // keyboard user: focus + Enter
      await settle(A, 600);
      await p.getByRole('heading', { name: M.title, exact: true }).waitFor({ timeout: 8000 });
    };
    await tc(rec, `UIF-A11Y-${pad(base)}`, `${M.name} modal: role=dialog named by its title, aria-modal="true"`, async () => {
      await open();
      try {
        const dlg = p.getByRole('dialog', { name: M.title });
        const n = await dlg.count();
        ok(n === 1, `no element with role="dialog" named "${M.title}" (found ${await p.getByRole('dialog').count()} dialog(s) in total)`);
        ok(await dlg.getAttribute('aria-modal') === 'true', 'dialog lacks aria-modal="true"');
      } finally { await forceClose(p); }
    }, { u: A });
    await tc(rec, `UIF-A11Y-${pad(base + 1)}`, `${M.name} modal: focus moves into the dialog and Tab / Shift+Tab stay inside it`, async () => {
      await open();
      try {
        const ov = overlay(p);
        ok(await focusInside(ov), `focus did not move into the dialog (focused: ${await activeDesc(p)})`);
        for (let i = 1; i <= 30; i++) {
          await p.keyboard.press('Tab');
          ok(await focusInside(ov), `focus left the dialog after ${i} Tab press(es): ${await activeDesc(p)}`);
        }
        for (let i = 1; i <= 8; i++) {
          await p.keyboard.press('Shift+Tab');
          ok(await focusInside(ov), `focus left the dialog after ${i} Shift+Tab press(es): ${await activeDesc(p)}`);
        }
      } finally { await forceClose(p); }
    }, { u: A });
    await tc(rec, `UIF-A11Y-${pad(base + 2)}`, `${M.name} modal: Escape closes it and focus returns to the control that opened it`, async () => {
      await open();
      try {
        await p.keyboard.press('Escape'); await settle(A, 500);
        const still = await p.getByRole('heading', { name: M.title, exact: true }).count();
        ok(still === 0, 'Escape does not close the dialog');
        const back = (M.returnTo || M.trigger)(p);
        ok(await isFocused(back), `focus not returned to the opener (focused: ${await activeDesc(p)})`);
      } finally { await forceClose(p); }
    }, { u: A });
  }

  await A.ctx.close(); await L.ctx.close();
}
if (isMain(import.meta)) standalone('a11y_smoke', run);
