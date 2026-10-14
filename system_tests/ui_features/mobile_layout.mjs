// UIF-MOB: responsive layout of the key screens at phone (390x844) and tablet (768x1024) viewports.
//   Per screen (SCREENS in _ui_extra.mjs: login, home, contacts, contact record, campaigns, deals, pipeline, sales reports,
//   settings, help center, webhooks, contact scoring, sales reports CRM KPIs tab):
//     overflow case  - no horizontal page scroll (documentElement.scrollWidth <= innerWidth + 1) and no console / API errors;
//                      on failure the detail names the outermost offending elements (selector, right edge, text)
//     primary case   - the screen's main action is visible, not covered, clickable (trial click) without scrolling sideways
//   plus: navigation reachable (hamburger / collapsed sidebar opens, or the sidebar link is on screen and works),
//   content area keeps most of the phone width, and the Add contact / New opportunity / Change password modals fit the viewport.
//   Screenshots: system_tests/ui_crm/shots/mobile_layout/<viewport>-<screen>.png
// Case numbering: phone overflow 01-13, phone primary 21-33, tablet overflow 41-53, tablet primary 61-73, extra 81-90.
// Uses the ui_crm helpers (own reusable tenant "mobile", wiped on every run).   node mobile_layout.mjs
import path from 'node:path';
import { ui, go, settle, tc, ok, must, standalone, isMain, Tenant, WEB } from '../ui_crm/crm_common.mjs';
import { PHONE, TABLET, SCREENS, anon, shotsDir, overflowReport, overflowMessage, fitsViewport, modalPanel, overlay, sidebarLink } from './_ui_extra.mjs';

const pad = n => String(n).padStart(2, '0');

async function primaryCheck(page, loc) {
  await page.evaluate(() => window.scrollTo(0, 0));
  await loc.waitFor({ state: 'visible', timeout: 8000 });
  await loc.scrollIntoViewIfNeeded();
  const sx = await page.evaluate(() => window.scrollX);
  const box = await fitsViewport(loc);
  await loc.click({ trial: true, timeout: 5000 });   // actionability: visible, stable, enabled, receives the pointer (not covered)
  ok(sx === 0, `primary action is only reachable by scrolling sideways (scrollX=${sx}, element left=${box.left} right=${box.right}, viewport ${box.vw})`);
  ok(box.left >= 0 && box.right <= box.vw + 1, `primary action outside the viewport (left=${box.left} right=${box.right}, viewport ${box.vw})`);
}

async function closeModal(page) {
  await page.keyboard.press('Escape'); await page.waitForTimeout(400);
  for (const name of ['Close', 'Cancel']) {
    const b = overlay(page).getByRole('button', { name, exact: true });
    if (await b.count()) { await b.first().click().catch(() => {}); await page.waitForTimeout(300); }
  }
  if (await page.locator('div.fixed.inset-0').count()) await page.mouse.click(2, 2).catch(() => {});
}

export async function run(rec, browser) {
  const t = await (await Tenant.get('mobile')).init();
  const contact = await must(t.token, 'POST', '/contacts', { email: `mo.${t.rid}@mob.example`, first_name: 'Mona', last_name: `Mobile${t.rid}`,
    company_name: `Mobile Co ${t.rid}`, designation: 'Head of Operations' });
  const data = { contactId: contact.prospect_id };
  const SHOT = shotsDir('mobile_layout');
  const shot = (page, name) => page.screenshot({ path: path.join(SHOT, `${name}.png`), fullPage: false }).catch(() => {});

  const VIEWPORTS = [{ tag: 'phone', vp: PHONE, base: 0 }, { tag: 'tablet', vp: TABLET, base: 40 }];
  const sessions = {};
  for (const V of VIEWPORTS) {
    const U = await ui(browser, t.owner, { viewport: V.vp });
    const L = await anon(browser, { viewport: V.vp });
    sessions[V.tag] = U;
    for (const [i, s] of SCREENS.entries()) {
      const u = s.route ? U : L;
      const label = `${V.tag} ${V.vp.width}x${V.vp.height}`;
      await tc(rec, `UIF-MOB-${pad(V.base + 1 + i)}`, `${s.name} at ${label}: no horizontal page scroll, no console / API errors`, async () => {
        if (s.route) await go(u, s.route(data), { wait: 1200 });
        else { await u.page.goto(`${WEB}/login`); await settle(u, 800); }
        await shot(u.page, `${V.tag}-${s.key}`);
        const r = await overflowReport(u.page);
        ok(r.sw <= r.vw + 1, overflowMessage(r));
      }, { u });
      await tc(rec, `UIF-MOB-${pad(V.base + 21 + i)}`, `${s.name} at ${label}: primary action visible and clickable without sideways scrolling`, async () => {
        await primaryCheck(u.page, s.primary(u.page));
      }, { u });
    }
    await L.ctx.close();
  }

  for (const [k, V] of VIEWPORTS.entries()) {
    const U = sessions[V.tag]; const p = U.page;
    await tc(rec, `UIF-MOB-${pad(81 + k)}`, `Navigation reachable at ${V.tag} ${V.vp.width}px: menu toggle opens the nav (or the sidebar link is on screen) and navigates`, async () => {
      await go(U, 'dashboard', { wait: 800 });
      // the opener (not the drawer's own "Close navigation menu" button, which sits first in the DOM while the drawer is closed)
      const toggle = p.getByRole('button', { name: /^(open|show|toggle)\b.*(menu|navigation|sidebar)|^menu$/i });
      if (await toggle.count() && await toggle.first().isVisible()) { await toggle.first().click(); await settle(U, 500); }
      const link = await sidebarLink(p, 'Contacts', 'CRM');
      await link.waitFor({ state: 'visible', timeout: 5000 });
      const box = await fitsViewport(link);
      ok(box.left >= 0 && box.right <= box.vw + 1, `Contacts nav link outside the viewport (${box.left}..${box.right})`);
      await link.click();
      await p.waitForURL(/\/app\/contacts/, { timeout: 8000 });
      await shot(p, `${V.tag}-nav`);
    }, { u: U });
  }

  await tc(rec, 'UIF-MOB-83', 'Phone 390px: the page content keeps at least 70% of the width (the sidebar collapses)', async () => {
    const U = sessions.phone; await go(U, 'contacts', { wait: 800 });
    const r = await U.page.evaluate(() => {
      const main = document.querySelector('main'); const aside = document.querySelector('aside');
      return { main: main ? Math.round(main.getBoundingClientRect().width) : 0, aside: aside ? Math.round(aside.getBoundingClientRect().width) : 0,
        asideVisible: aside ? getComputedStyle(aside).display !== 'none' && aside.getBoundingClientRect().right > 0 : false, vw: innerWidth };
    });
    ok(r.main >= r.vw * 0.7, `content area is ${r.main}px of ${r.vw}px; sidebar ${r.asideVisible ? `always shown, ${r.aside}px wide` : 'hidden'} (aside.fixed w-60 + main ml-60 have no responsive variant)`);
  }, { u: sessions.phone });

  await tc(rec, 'UIF-MOB-84', 'Tablet 768px: the page content keeps at least 60% of the width', async () => {
    const U = sessions.tablet; await go(U, 'contacts', { wait: 800 });
    const r = await U.page.evaluate(() => ({ main: Math.round(document.querySelector('main')?.getBoundingClientRect().width || 0), vw: innerWidth }));
    ok(r.main >= r.vw * 0.6, `content area is ${r.main}px of ${r.vw}px`);
  }, { u: sessions.tablet });

  const MODALS = [
    { id: 85, name: 'Add contact', route: 'contacts', open: p => p.getByRole('button', { name: /Add contact/ }).first().click(),
      action: p => overlay(p).getByRole('button', { name: /Add contact/ }) },
    { id: 87, name: 'New opportunity', route: 'deals', open: p => p.getByRole('button', { name: /New opportunity/ }).click(),
      action: p => overlay(p).getByRole('button', { name: 'Create', exact: true }) },
    { id: 89, name: 'Change password', route: 'dashboard', open: async p => { await p.getByRole('button', { name: 'Account' }).click(); await p.getByRole('button', { name: /Change password/ }).click(); },
      action: p => overlay(p).getByRole('button', { name: 'Update Password' }) },
  ];
  for (const M of MODALS) {
    for (const [k, V] of VIEWPORTS.entries()) {
      const U = sessions[V.tag]; const p = U.page;
      await tc(rec, `UIF-MOB-${pad(M.id + k)}`, `${M.name} modal at ${V.tag} ${V.vp.width}x${V.vp.height}: fits the viewport, its main button is on screen`, async () => {
        await go(U, M.route, { wait: 800 });
        await M.open(p); await settle(U, 700);
        const panel = modalPanel(p);
        await panel.waitFor({ state: 'visible', timeout: 8000 });
        await shot(p, `${V.tag}-modal-${M.name.toLowerCase().replace(/\s+/g, '-')}`);
        const box = await fitsViewport(panel);
        try {
          ok(box.fits, `modal panel ${box.left},${box.top} .. ${box.right},${box.bottom} exceeds the ${box.vw}x${box.vh} viewport`);
          const btn = M.action(p);
          await btn.waitFor({ state: 'visible', timeout: 5000 });
          await btn.scrollIntoViewIfNeeded();
          const b = await fitsViewport(btn);
          ok(b.fits, `"${M.name}" main button outside the viewport (${b.left},${b.top} .. ${b.right},${b.bottom})`);
          const sx = await p.evaluate(() => window.scrollX);
          ok(sx === 0, `modal needs sideways page scrolling (scrollX=${sx})`);
        } finally { await closeModal(p); }
      }, { u: U });
    }
  }

  for (const U of Object.values(sessions)) await U.ctx.close();
}
if (isMain(import.meta)) standalone('mobile_layout', run);
