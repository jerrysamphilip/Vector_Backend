// Shared helpers for the cross-screen UI suites (template_library, platform_admin, mobile_layout, a11y_smoke,
// session_auth). Not a suite itself: the "_" prefix keeps it out of scripts/test.py discovery.
//   - freshLogin(): a real login through the form WITHOUT the cookie cache (for tests that rotate / clear cookies)
//   - anon(): a signed-out browser context with a problem watcher
//   - overflowReport(): horizontal page overflow + the offending elements' selectors
//   - unnamedControls(): interactive elements with no accessible name (Chromium's computed AX tree via CDP)
//   - SCREENS: the key screens with their primary action, shared by mobile_layout and a11y_smoke
import fs from 'node:fs';
import path from 'node:path';
import { WEB, watch, SHOTS } from '../ui_crm/crm_common.mjs';

export const PHONE = { width: 390, height: 844 };
export const TABLET = { width: 768, height: 1024 };
export const DESKTOP = { width: 1440, height: 900 };

export function shotsDir(stem) { const d = path.join(SHOTS, stem); fs.mkdirSync(d, { recursive: true }); return d; }

// Logins through the form are limited to 60 / 5 min / IP (shared with other agents): keep our own count low.
const loginTimes = [];
async function pace() {
  const now = Date.now();
  while (loginTimes.length && now - loginTimes[0] > 300000) loginTimes.shift();
  if (loginTimes.length >= 20) { const wait = 300000 - (now - loginTimes[0]) + 1000; console.log(`  (pacing logins: waiting ${Math.round(wait / 1000)}s)`); await new Promise(r => setTimeout(r, wait)); }
  loginTimes.push(Date.now());
}

function wrap(ctx, page, user = null) {
  page.setDefaultTimeout(15000);
  const u = { ctx, page, w: watch(page), user };
  page.on('dialog', d => { u.lastDialog = d.message(); d.accept().catch(() => {}); });
  return u;
}

/** A signed-out context (no cookies, empty storage). */
export async function anon(browser, { viewport = DESKTOP } = {}) {
  const ctx = await browser.newContext({ viewport });
  return wrap(ctx, await ctx.newPage());
}

/** Fill the login form (page must be on /login). */
export async function fillLogin(page, email, password) {
  await page.locator('input[type=email]').fill(email);
  await page.locator('input[type=password]').fill(password);
  await page.locator('button[type=submit]').click();
}

/** Real login through the form in a NEW context, never cached (cookies of this context may be rotated / cleared by the test). */
export async function freshLogin(browser, user, { viewport = DESKTOP, landing = /\/(app|admin)(\/|$|\?)/ } = {}) {
  await pace();
  const u = await anon(browser, { viewport });
  u.user = user;
  await u.page.goto(`${WEB}/login`);
  await fillLogin(u.page, user.email, user.password);
  await u.page.waitForURL(landing, { timeout: 20000 });
  await u.page.waitForLoadState('networkidle').catch(() => {});
  return u;
}
export async function loginPaced() { await pace(); }

export async function cookieNames(ctx) { return (await ctx.cookies()).map(c => c.name); }
export async function dropCookies(ctx, names) { for (const name of names) await ctx.clearCookies({ name }); }

/**
 * Horizontal overflow of the page. Returns { sw, vw, offenders[] }; offenders are the OUTERMOST elements that stick out
 * past the right edge of the viewport and are not clipped by an overflow container or position:fixed.
 */
export async function overflowReport(page) {
  return page.evaluate(() => {
    const vw = window.innerWidth;
    const sw = document.documentElement.scrollWidth;
    const clipRe = /(auto|hidden|scroll|clip)/;
    const short = el => {
      let s = el.tagName.toLowerCase();
      if (el.id) s += `#${CSS.escape(el.id)}`;
      const cls = (typeof el.className === 'string' ? el.className : '').split(/\s+/).filter(Boolean).slice(0, 3);
      if (cls.length) s += '.' + cls.map(c => CSS.escape(c)).join('.');
      const tid = el.getAttribute && el.getAttribute('data-testid'); if (tid) s += `[data-testid="${tid}"]`;
      return s;
    };
    const selector = el => {
      const parts = [];
      for (let e = el, i = 0; e && e !== document.body && i < 4; e = e.parentElement, i++) parts.unshift(short(e));
      return parts.join(' > ');
    };
    const excluded = el => {
      for (let a = el; a && a !== document.documentElement; a = a.parentElement) {
        const s = getComputedStyle(a);
        if (s.position === 'fixed') return true;
        if (a !== el && clipRe.test(s.overflowX)) return true;
      }
      return false;
    };
    const offenders = [];
    if (sw > vw + 1) {
      for (const el of document.body.querySelectorAll('*')) {
        const r = el.getBoundingClientRect();
        if (!r.width || r.right <= vw + 1) continue;
        const st = getComputedStyle(el);
        if (st.display === 'none' || st.visibility === 'hidden') continue;
        if (excluded(el)) continue;
        const p = el.parentElement;
        if (p && p !== document.body && p.getBoundingClientRect().right > vw + 1 && !clipRe.test(getComputedStyle(p).overflowX)) continue;
        const text = (el.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 40);
        offenders.push(`${selector(el)} (right=${Math.round(r.right)}px, width=${Math.round(r.width)}px${text ? `, "${text}"` : ''})`);
        if (offenders.length >= 5) break;
      }
    }
    return { sw, vw, offenders };
  });
}
export function overflowMessage(r) { return `page scrolls horizontally: scrollWidth ${r.sw} > innerWidth ${r.vw}; offending: ${r.offenders.join(' | ') || '(none found outside clipping containers)'}`; }

/** Element rectangle vs. viewport. */
export async function fitsViewport(locator) {
  return locator.evaluate(el => {
    const r = el.getBoundingClientRect();
    return { left: Math.round(r.left), top: Math.round(r.top), right: Math.round(r.right), bottom: Math.round(r.bottom), vw: innerWidth, vh: innerHeight,
      fits: r.left >= -1 && r.top >= -1 && r.right <= innerWidth + 1 && r.bottom <= innerHeight + 1 };
  });
}

/** The panel of the top-most overlay (shared Modal, change-password modal ...): last div.fixed.inset-0's relative child. */
export function modalPanel(page) { return page.locator('div.fixed.inset-0').last().locator('xpath=./div[contains(@class,"relative")]').first(); }
export function overlay(page) { return page.locator('div.fixed.inset-0').last(); }

const INTERACTIVE_ROLES = new Set(['button', 'link', 'textbox', 'searchbox', 'combobox', 'checkbox', 'radio', 'switch', 'slider',
  'spinbutton', 'listbox', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'tab', 'textField', 'TextField', 'PopUpButton', 'ToggleButton']);

/** Interactive elements without an accessible name, from Chromium's computed accessibility tree. Returns ["role <selector> <html>"...]. */
export async function unnamedControls(page) {
  const cdp = await page.context().newCDPSession(page);
  try {
    await cdp.send('DOM.enable'); await cdp.send('Accessibility.enable');
    const { nodes } = await cdp.send('Accessibility.getFullAXTree');
    const out = [];
    for (const n of nodes) {
      if (n.ignored) continue;
      const role = n.role?.value;
      if (!INTERACTIVE_ROLES.has(role)) continue;
      const name = String(n.name?.value ?? '').trim();
      if (name) continue;
      let desc = `${role}`;
      if (n.backendDOMNodeId) {
        try {
          const { node } = await cdp.send('DOM.describeNode', { backendNodeId: n.backendDOMNodeId });
          const attrs = {}; for (let i = 0; i + 1 < (node.attributes || []).length; i += 2) attrs[node.attributes[i]] = node.attributes[i + 1];
          const cls = (attrs.class || '').split(/\s+/).filter(Boolean).slice(0, 3).join('.');
          desc += ` ${node.localName}${attrs.id ? '#' + attrs.id : ''}${cls ? '.' + cls : ''}${attrs['data-testid'] ? `[data-testid=${attrs['data-testid']}]` : ''}${attrs.type ? `[type=${attrs.type}]` : ''}`;
          const { outerHTML } = await cdp.send('DOM.getOuterHTML', { backendNodeId: n.backendDOMNodeId });
          desc += ` ${outerHTML.replace(/\s+/g, ' ').slice(0, 90)}`;
        } catch { /* node gone */ }
      }
      out.push(desc);
    }
    return out;
  } finally { await cdp.detach().catch(() => {}); }
}

/** Is the focused element inside `locator`'s element? */
export async function focusInside(locator) { return locator.evaluate(el => el.contains(document.activeElement) && document.activeElement !== document.body); }
export async function activeDesc(page) {
  return page.evaluate(() => { const a = document.activeElement; if (!a) return 'none'; return `${a.tagName.toLowerCase()}${a.id ? '#' + a.id : ''} "${(a.getAttribute('aria-label') || a.innerText || a.getAttribute('placeholder') || '').trim().slice(0, 30)}"`; });
}

/**
 * Key screens. route(ctx) gives the /app/ route (null = the login page, signed out); primary(page) the locator of the screen's
 * main action (null when the screen has none). ctx = { contactId }.
 */
export const SCREENS = [
  { key: 'login', name: 'Login', route: null, primary: p => p.getByRole('button', { name: 'Login', exact: true }) },
  { key: 'dashboard', name: 'Home', route: () => 'dashboard', primary: p => p.getByRole('button', { name: 'New', exact: true }) },
  { key: 'contacts', name: 'Contacts list', route: () => 'contacts', primary: p => p.getByRole('button', { name: /Add contact/ }).first() },
  { key: 'contact', name: 'Contact record', route: c => `contacts/${c.contactId}`, primary: p => p.getByRole('button', { name: /Log call/ }) },
  { key: 'campaigns', name: 'Campaigns list', route: () => 'campaigns', primary: p => p.getByRole('button', { name: /Create New Campaign/ }) },
  { key: 'deals', name: 'Deals', route: () => 'deals', primary: p => p.getByRole('button', { name: /New opportunity/ }) },
  { key: 'pipeline', name: 'Pipeline', route: () => 'pipeline', primary: p => p.getByRole('button', { name: 'Proposal pipeline' }) },
  { key: 'sales-reports', name: 'Sales reports', route: () => 'sales-reports', primary: p => p.getByRole('button', { name: 'Forecast', exact: true }) },
  { key: 'settings', name: 'Settings (analytics dashboard)', route: () => 'settings', primary: p => p.getByRole('button', { name: /^weekly$/i }) },
  { key: 'help', name: 'Help center', route: () => 'help', primary: p => p.getByPlaceholder('Search all help…') },
  { key: 'webhooks', name: 'Webhooks', route: () => 'webhooks', primary: p => p.getByTestId('webhook-add') },
  { key: 'contact-scoring', name: 'Contact scoring settings', route: () => 'contact-scoring', primary: p => p.getByTestId('scoring-save') },
];

/** A sidebar nav link; expands its (collapsed) group first. The sidebar keeps only the active group open (Sidebar.jsx). */
export async function sidebarLink(page, name, group) {
  const link = page.locator('aside').getByRole('link', { name, exact: true }).first();
  if (!(await link.count()) && group) {
    const g = page.locator('aside').getByRole('button', { name: group, exact: true });
    if (await g.count()) { await g.first().click(); await page.waitForTimeout(250); }
  }
  return link;
}
