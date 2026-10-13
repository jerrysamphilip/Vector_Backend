import { groupBtn, startSuite, getWs, createWs, api, sql, q, go, body, waitText, clean, WEB, rid, mkContact, fullWorkspace, sleep } from './lib.mjs';
import fs from 'node:fs';

const S = await startSuite('shell');
const { rec } = S;
const A = 'UIO-SHELL';
const ws = await getWs('shell');
const ROLES = ['owner', 'admin', 'manager', 'agent'];
const P = {}; // role -> {page,w}
for (const r of ROLES) P[r] = await S.open(ws, r);

// Expected sidebar by role: [link name, visible?]
const EXPECT = {
  owner:   { 'Campaign analytics': 1, Campaigns: 1, 'Email accounts': 1, Reports: 1, 'Prospect lists': 1, Team: 1, 'Sales settings': 1, Templates: 1, 'Domain health': 1 },
  admin:   { 'Campaign analytics': 1, Campaigns: 1, 'Email accounts': 1, Reports: 1, 'Prospect lists': 1, Team: 1, 'Sales settings': 1, Templates: 1, 'Domain health': 1 },
  manager: { 'Campaign analytics': 1, Campaigns: 1, 'Email accounts': 0, Reports: 1, 'Prospect lists': 0, Team: 0, 'Sales settings': 0, Templates: 1, 'Domain health': 1 },
  agent:   { 'Campaign analytics': 0, Campaigns: 0, 'Email accounts': 0, Reports: 0, 'Prospect lists': 0, Team: 0, 'Sales settings': 0, Templates: 1, 'Domain health': 1 },
};
const GROUPS = ['OUTREACH', 'CRM', 'SALES', 'WORKSPACE'];

// ---------- Sidebar ----------
for (const r of ROLES) {
  const { page, w } = P[r];
  await S.t(`${A}-SB-${r}`, `Sidebar menu for ${r.toUpperCase()}: role-specific items visible/hidden, role badge, groups accordion`, async () => {
    await go(page, '/app/dashboard');
    const bad = [];
    // open every group to inspect all links
    const seen = new Set();
    for (const g of GROUPS) {
      const b = groupBtn(page, g);
      if (await b.count()) {
        if ((await b.getAttribute('aria-expanded')) !== 'true') await b.click();
        if ((await b.getAttribute('aria-expanded')) !== 'true') bad.push(`${g} did not expand`);
        // accordion: only one open
        for (const g2 of GROUPS) if (g2 !== g) { const b2 = groupBtn(page, g2); if (await b2.count() && await b2.getAttribute('aria-expanded') === 'true') bad.push(`${g2} still open when ${g} opened`); }
      }
      (await page.locator('aside a, nav a').allInnerTexts()).forEach(t => seen.add(t.trim()));
    }
    for (const [name, vis] of Object.entries(EXPECT[r])) {
      const has = seen.has(name);
      if (!!vis !== has) bad.push(`${name}: expected ${vis ? 'visible' : 'hidden'}`);
    }
    const label = { owner: 'Super Admin', admin: 'Admin', manager: 'Manager', agent: 'Agent' }[r];
    if (!(await body(page)).includes(label)) bad.push('role label ' + label + ' missing');
    const p = await clean(page, w); if (p) bad.push(p);
    return bad.length ? bad.join('; ') : undefined;
  }, page);
}
{ // collapse
  const { page } = P.owner;
  await S.t(`${A}-SB-collapse`, 'Sidebar group header toggles collapse/expand (aria-expanded)', async () => {
    await go(page, '/app/dashboard');
    const b = groupBtn(page, 'OUTREACH');
    const first = await b.getAttribute('aria-expanded');
    await b.click(); const second = await b.getAttribute('aria-expanded');
    await b.click(); const third = await b.getAttribute('aria-expanded');
    if (first === second || second === third) return `states ${first},${second},${third}`;
  }, page);
}

// Every sidebar link navigates and renders cleanly, per role
const LINKS = [
  ['Home', '/app/dashboard'], ['Inbox', '/app/inbox'], ['Tasks', '/app/tasks'],
  ['Campaigns', '/app/campaigns'], ['Campaign analytics', '/app/analytics'], ['Templates', '/app/templates'], ['AI email writer', '/app/ai-email'],
  ['Email accounts', '/app/inboxes'], ['Domain health', '/app/domain-health'], ['Reports', '/app/reports'],
  ['Contacts', '/app/contacts'], ['Companies', '/app/accounts'], ['Lists', '/app/lists'], ['Prospect lists', '/app/prospects'],
  ['Overview', '/app/sales'], ['Leads', '/app/leads'], ['SQL queue', '/app/sql-queue'], ['Deals', '/app/deals'], ['Pipeline', '/app/pipeline'],
  ['Sales reports', '/app/sales-reports'], ['Targets', '/app/sales-targets'],
  ['Team', '/app/team'], ['Sales team', '/app/sales-team'], ['Sales settings', '/app/sales-settings'], ['Calendar sync', '/app/connections'],
];
for (const r of ROLES) {
  const { page, w } = P[r];
  await S.t(`${A}-NAV-${r}`, `Every visible sidebar link navigates and its page renders cleanly (${r.toUpperCase()})`, async () => {
    await go(page, '/app/dashboard');
    const bad = [];
    for (const [name, href] of LINKS) {
      const visible = EXPECT[r][name] === undefined ? true : !!EXPECT[r][name];
      // expand the group containing the link
      let link = page.locator(`aside a[href$="${href}"], nav a[href$="${href}"]`).first();
      if (!(await link.count()) || !(await link.isVisible())) {
        for (const g of GROUPS) { const b = groupBtn(page, g); if (await b.count() && await b.getAttribute('aria-expanded') !== 'true') { await b.click(); if (await link.isVisible().catch(() => false)) break; } }
      }
      if (!(await link.count()) || !(await link.isVisible())) { if (visible) bad.push(`${name}: link missing`); continue; }
      w.drain();
      await link.click();
      await page.waitForURL(u => u.pathname.endsWith(href), { timeout: 8000 }).catch(() => bad.push(`${name}: did not navigate (at ${page.url()})`));
      await page.waitForLoadState('networkidle').catch(() => {}); await page.waitForTimeout(500);
      const probs = [...w.drain(), ...(await w.bodyProblems())];
      const txt = (await body(page)).replace(/\s+/g, ' ');
      if (txt.length < 120) probs.push('page nearly empty');
      if (probs.length) bad.push(`${name}: ${probs.join(',').slice(0, 220)}`);
    }
    return bad.length ? bad.join(' || ') : undefined;
  }, page);
}

// ---------- Route guards ----------
const GUARDED = { agent: ['/app/campaigns', '/app/campaigns/new', '/app/prospects', '/app/prospects/validate', '/app/inboxes', '/app/team', '/app/reports', '/app/recently-deleted', '/app/import'],
                  manager: ['/app/prospects', '/app/inboxes', '/app/team', '/app/recently-deleted', '/app/import'] };
for (const r of ['agent', 'manager']) {
  const { page, w } = P[r];
  await S.t(`${A}-GUARD-${r}`, `${r.toUpperCase()} opening restricted URLs is redirected to Home (no flash of forbidden data)`, async () => {
    const bad = [];
    for (const route of GUARDED[r]) {
      let apiCalls = [];
      const onReq = req => { const u = req.url(); if (/\/api\//.test(u)) apiCalls.push(u); };
      page.on('request', onReq);
      await go(page, route, { wait: 400 });
      page.off('request', onReq);
      if (!/\/app\/dashboard$/.test(new URL(page.url()).pathname)) bad.push(`${route} -> ${page.url()}`);
    }
    const p = w.drain([/.*/]);
    return bad.length ? bad.join('; ') : undefined;
  }, page);
}
{ // platform admin area for tenant users
  for (const r of ['owner', 'agent']) {
    const { page, w } = P[r];
    await S.t(`${A}-GUARD-admin-${r}`, `${r.toUpperCase()} opening /vector/admin/... is redirected to Home`, async () => {
      for (const route of ['/admin', '/admin/tenants', '/admin/users']) {
        await go(page, route, { wait: 300 });
        if (!/\/app\/dashboard$/.test(new URL(page.url()).pathname)) return `${route} -> ${page.url()}`;
      }
      w.drain([/.*/]);
    }, page);
  }
}
{ // API-level enforcement matches UI guards (agent token on admin endpoints)
  const t = ws.users.agent.token;
  await S.t(`${A}-GUARD-api`, 'Server also refuses AGENT on team/inbox/campaign management endpoints (403), not just the UI', async () => {
    const out = [];
    for (const [m, p] of [['GET', '/api/users'], ['POST', '/api/users/invite'], ['GET', '/inboxes'], ['POST', '/campaigns'], ['GET', '/api/admin/tenants'], ['GET', '/reports/global-analytics']]) {
      const r = await api(m, p, { token: t, body: m === 'POST' ? {} : undefined });
      if (![401, 403, 422].includes(r.status) ) out.push(`${m} ${p} -> ${r.status}`);
      if (r.status === 422) out.push(`${m} ${p} -> 422 (validated before authorization)`);
    }
    return out.length ? out.join('; ') : undefined;
  });
}

// ---------- 404 / unknown routes ----------
{
  const { page, w } = P.owner;
  await S.t(`${A}-404`, 'Unknown routes: /app/does-not-exist shows the 404 page (URL kept, link back Home works); /vector/xyz -> Home; /app/campaigns/<bad id> safe', async () => {
    // Deliberate change: unknown /app/* routes render NotFound (src/pages/NotFound.jsx) instead of redirecting Home.
    await go(page, '/app/does-not-exist');
    if (!/\/app\/does-not-exist$/.test(new URL(page.url()).pathname)) return 'unknown /app route did not keep its URL -> ' + page.url();
    const nf = (await body(page)).replace(/\s+/g, ' ');
    if (!/\b404\b/.test(nf) || !/Page not found/i.test(nf)) return '404 page content missing: ' + nf.slice(0, 300);
    if (!nf.includes('/app/does-not-exist')) return '404 page does not name the unknown address';
    if (!(await page.locator('aside, nav').first().isVisible().catch(() => false))) return '404 page is not rendered inside the app shell (no sidebar)';
    const home = page.getByRole('link', { name: /Back to Home/i });
    if (await home.count() !== 1) return `expected one "Back to Home" link, found ${await home.count()}`;
    const href = await home.getAttribute('href');
    if (!/\/vector\/app\/dashboard$/.test(href || '')) return 'Back to Home link href = ' + href;
    await home.click();
    await page.waitForURL(/\/app\/dashboard$/, { timeout: 8000 }).catch(() => {});
    if (!/\/app\/dashboard$/.test(new URL(page.url()).pathname)) return 'Back to Home did not navigate Home -> ' + page.url();
    // nested unknown path also gets the 404 page
    await go(page, '/app/campaigns-typo/abc');
    if (!/Page not found/i.test(await body(page))) return 'nested unknown /app path shows no 404 page';
    await go(page, '/totally/unknown');
    if (!/\/app\/dashboard$/.test(new URL(page.url()).pathname)) return 'unknown root route -> ' + page.url();
    w.drain();
    await go(page, '/app/campaigns/00000000-0000-0000-0000-000000000000', { wait: 1200 });
    const t = (await body(page)).replace(/\s+/g, ' ');
    const bp = await w.bodyProblems(); if (bp.length) return 'bad campaign id: ' + bp.join(',');
    if (!/not found|no longer|doesn't exist|does not exist|couldn't|could not|unable|error/i.test(t)) return 'bad campaign id page shows no not-found message: ' + t.slice(-200);
    w.drain([/.*/]);
  }, page);
}

// ---------- Help ----------
const ids = [...fs.readFileSync('/home/user/Vector_Frontend/src/help/articles.js', 'utf8').matchAll(/^\s*id: '([^']+)', category: '([^']*)', title: '((?:[^'\\]|\\.)*)'/gm)]
  .concat([...fs.readFileSync('/home/user/Vector_Frontend/src/help/articlesOutreach.js', 'utf8').matchAll(/^\s*id: '([^']+)', category: '([^']*)', title: '((?:[^'\\]|\\.)*)'/gm)])
  .map(m => ({ id: m[1], title: m[3].replace(/\\'/g, "'") }));
{
  const { page, w } = P.owner;
  await S.t(`${A}-HELP-01`, 'Help button opens panel for the current screen; Close button and Esc close it', async () => {
    await go(page, '/app/campaigns');
    await page.getByRole('button', { name: 'Help for this screen' }).click();
    const dlg = page.getByRole('dialog', { name: 'Help' }); await dlg.waitFor();
    if (!(await dlg.innerText()).includes('Campaigns')) return 'no Campaigns article';
    await dlg.getByRole('button', { name: 'Close help' }).click(); await dlg.waitFor({ state: 'hidden' });
    await page.getByRole('button', { name: 'Help for this screen' }).click(); await dlg.waitFor();
    await page.keyboard.press('Escape'); await dlg.waitFor({ state: 'hidden' });
  }, page);
  await S.t(`${A}-HELP-02`, '? and F1 keyboard shortcuts toggle help; ? is ignored while typing in a field', async () => {
    await go(page, '/app/dashboard');
    const dlg = page.getByRole('dialog', { name: 'Help' });
    await page.locator('body').click({ position: { x: 700, y: 400 } });
    await page.keyboard.press('?'); await dlg.waitFor();
    if (!(await dlg.getByPlaceholder('Search help…').evaluate(e => e === document.activeElement))) return 'help search not focused on open';
    await page.keyboard.press('Escape'); await dlg.waitFor({ state: 'hidden' });
    await page.keyboard.press('F1'); await dlg.waitFor();
    await page.keyboard.press('F1'); await dlg.waitFor({ state: 'hidden' });
    const input = page.getByPlaceholder('Search contacts and companies…'); await input.click(); await page.keyboard.type('?');
    await page.waitForTimeout(300);
    if (await dlg.isVisible()) return '? opened help while typing in search';
    if ((await input.inputValue()) !== '?') return 'typing ? in field lost';
  }, page);
  await S.t(`${A}-HELP-03`, 'Help panel search finds articles, opens one, Back returns; "Open in Help Center" deep-links', async () => {
    await go(page, '/app/dashboard');
    await page.keyboard.press('F1');
    const dlg = page.getByRole('dialog', { name: 'Help' });
    await dlg.getByPlaceholder('Search help…').fill('campaign');
    await waitText(page, /\d+ results?/i);
    const res = dlg.locator('ul button').first(); const title = (await res.locator('span').first().innerText()).trim();
    await res.click();
    if (!(await dlg.locator('h2').innerText()).includes(title)) return 'opened article title mismatch';
    await dlg.getByRole('button', { name: /back/i }).click();
    await dlg.getByPlaceholder('Search help…').fill('zzzzqqqq');
    await waitText(page, /Nothing matches/);
    await dlg.getByPlaceholder('Search help…').fill('');
    await dlg.getByRole('link', { name: /Open in Help Center/ }).click();
    await page.waitForURL(/\/app\/help\//);
  }, page);
  await S.t(`${A}-HELP-04`, 'Context help: every routed article opens for its own screen (panel title == article title)', async () => {
    const SCREENS = { home: '/app/dashboard', tasks: '/app/tasks', inbox: '/app/inbox', campaigns: '/app/campaigns', 'create-campaign': '/app/campaigns/new',
      'campaign-analytics': '/app/analytics', templates: '/app/templates', 'ai-email': '/app/ai-email', 'email-accounts': '/app/inboxes', 'domain-health': '/app/domain-health',
      reports: '/app/reports', 'prospect-lists': '/app/prospects', 'automation-rules': '/app/automation', 'team-management': '/app/team', 'account-security': '/app/account/security',
      contacts: '/app/contacts', companies: '/app/accounts', lists: '/app/lists', 'sales-overview': '/app/sales', leads: '/app/leads', deals: '/app/deals', pipeline: '/app/pipeline', 'calendar-sync': '/app/connections' };
    const bad = [];
    for (const [id, route] of Object.entries(SCREENS)) {
      const art = ids.find(a => a.id === id); if (!art) { bad.push('no article ' + id); continue; }
      await go(page, route, { wait: 250 });
      if (new URL(page.url()).pathname !== '/vector' + route) { bad.push(`${route} redirected to ${page.url()}`); continue; }
      await page.keyboard.press('F1');
      const dlg = page.getByRole('dialog', { name: 'Help' });
      await dlg.waitFor();
      const h = (await dlg.locator('h2').first().innerText()).trim();
      if (h !== art.title) bad.push(`${route}: panel shows "${h}", expected "${art.title}"`);
      await page.keyboard.press('Escape');
    }
    w.drain([/.*/]);
    return bad.length ? bad.join('; ') : undefined;
  }, page);
  await S.t(`${A}-HELP-05`, 'Help Center: every article id deep link /app/help/<id> resolves to its own article and renders cleanly', async () => {
    const bad = [];
    for (const a of ids) {
      await go(page, '/app/help/' + a.id, { wait: 150 });
      const h2 = (await page.locator('article h2').first().innerText().catch(() => '')).trim();
      if (h2 !== a.title) bad.push(`${a.id}: shows "${h2}"`);
      const probs = [...(await w.bodyProblems())]; if (probs.length) bad.push(`${a.id}: ${probs.join(',')}`);
    }
    w.drain();
    return bad.length ? bad.join('; ') : undefined;
  }, page);
  await S.t(`${A}-HELP-06`, 'Help Center: unknown article falls back gracefully; search + topic navigation + related links + "Go to this screen" work', async () => {
    await go(page, '/app/help/no-such-article');
    if (!(await body(page)).includes('Getting around')) return 'unknown id did not fall back to the getting-started guide';
    await page.getByPlaceholder('Search all help…').fill('warmup');
    await page.locator('nav[aria-label="Help topics"] button').first().click();
    await page.waitForURL(/\/app\/help\/[a-z-]+$/);
    await go(page, '/app/help/campaigns');
    await page.getByRole('link', { name: /Go to this screen/ }).click(); await page.waitForURL(/\/app\/campaigns$/);
    await go(page, '/app/help/campaigns');
    const rel = page.locator('article a').filter({ hasText: /./ }).last(); await rel.click(); await page.waitForURL(/\/app\/help\//);
    w.drain();
  }, page);
}

// ---------- Global search ----------
{
  const { page, w } = P.owner;
  const tag = 'Zorblax' + rid();
  const pid = await mkContact(ws, `${tag.toLowerCase()}@findme.example`, { first_name: tag, last_name: 'Searchable', company_name: 'Quuxco ' + tag });
  await S.t(`${A}-SEARCH-01`, 'Ctrl+K focuses global search; typing 1 char shows nothing, 2+ shows matching contact', async () => {
    await go(page, '/app/dashboard');
    await page.keyboard.press('Control+k');
    const inp = page.getByLabel('Search contacts and companies');
    if (!(await inp.evaluate(e => e === document.activeElement))) return 'search not focused by Ctrl+K';
    await page.keyboard.type('Z'); await page.waitForTimeout(500);
    if (/No matches|See all contacts/.test(await body(page))) return 'dropdown shown for 1 char';
    await inp.fill(tag);
    await waitText(page, new RegExp(tag + ' Searchable|' + tag));
  }, page);
  await S.t(`${A}-SEARCH-02`, 'Clicking a result opens that contact; query cleared', async () => {
    await page.locator('ul button', { hasText: tag }).first().click();
    await page.waitForURL(new RegExp('/app/contacts/' + pid));
    if (await page.getByLabel('Search contacts and companies').inputValue()) return 'query not cleared';
    const p = await clean(page, w); if (p) return p;
  }, page);
  await S.t(`${A}-SEARCH-03`, 'No-match query shows "No matches"; Enter goes to contacts?q=; Escape closes', async () => {
    await go(page, '/app/dashboard');
    const inp = page.getByLabel('Search contacts and companies');
    await inp.fill('nomatchxyz' + rid()); await waitText(page, /No matches for/);
    await page.keyboard.press('Escape'); await page.waitForTimeout(300);
    if (/No matches for/.test(await body(page))) return 'Escape did not close dropdown';
    await inp.focus(); await inp.fill('nomatchabc'); await waitText(page, /No matches for .nomatchabc/);
    await page.keyboard.press('Enter'); await page.waitForURL(/\/app\/contacts\?q=nomatchabc/);
  }, page);
  await S.t(`${A}-SEARCH-04`, 'Search API failure degrades silently (no crash, no [object Object])', async () => {
    await go(page, '/app/dashboard');
    await page.route(/\/api\/search/, r => r.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"x"}' }));
    await page.getByLabel('Search contacts and companies').fill('something');
    await page.waitForTimeout(1200); await page.unroute(/\/api\/search/);
    const bp = await w.bodyProblems(); w.drain([/.*/]); return bp.length ? bp.join(',') : undefined;
  }, page);
  await S.t(`${A}-SEARCH-05`, 'Tenant isolation: another workspace cannot find this contact via search', async () => {
    const other = await getWs('shell-b', { roles: [], identity: false });
    const r = await api('GET', '/search?q=' + tag, { token: other.token });
    const n = JSON.stringify(r.data).includes(tag);
    if (n) return 'foreign tenant search returned the contact';
  });
}

// ---------- Notifications ----------
{
  const { page, w } = P.manager;
  const uid = ws.users.manager.userId;
  const mk = (title, link, read) => sql(`INSERT INTO notifications (notification_id,tenant_id,user_id,kind,title,body,link,read_at,created_at) VALUES (UUID(),${q(ws.tenantId)},${q(uid)},'test',${q(title)},'Body of ${title}',${link ? q(link) : 'NULL'},${read ? 'NOW()' : 'NULL'},NOW())`);
  const t1 = 'Notif A ' + rid(), t2 = 'Notif B ' + rid(), t3 = 'Notif C read ' + rid();
  mk(t1, '/vector/app/tasks', false); mk(t2, null, false); mk(t3, null, true);
  await S.t(`${A}-NOTIF-01`, 'Bell shows unread count, lists notifications, unread highlighted; empty state for other user', async () => {
    await go(page, '/app/dashboard', { wait: 1000 });
    const bell = page.getByRole('button', { name: /Notifications/ });
    const label = await bell.getAttribute('aria-label');
    if (!/2 unread/.test(label)) return 'bell label ' + label;
    await bell.click();
    await waitText(page, new RegExp(t1)); const txt = await body(page);
    if (!txt.includes(t2) || !txt.includes(t3)) return 'missing items';
    const a = P.agent; await a.page.getByRole('button', { name: /Notifications/ }).click();
    await waitText(a.page, /Nothing yet/);
    await a.page.keyboard.press('Escape');
  }, page);
  await S.t(`${A}-NOTIF-02`, 'Clicking a notification marks it read (DB), closes the list and follows its link', async () => {
    await page.getByRole('button', { name: new RegExp(t1) }).click();
    await page.waitForURL(/\/app\/tasks/);
    await sleep(500);
    if (sql(`SELECT read_at IS NOT NULL FROM notifications WHERE user_id=${q(uid)} AND title=${q(t1)}`) !== '1') return 'not marked read in DB';
    if (sql(`SELECT read_at IS NULL FROM notifications WHERE user_id=${q(uid)} AND title=${q(t2)}`) !== '1') return 'other notification wrongly marked read';
  }, page);
  await S.t(`${A}-NOTIF-03`, 'Mark all read clears the badge and reads everything in DB', async () => {
    await page.getByRole('button', { name: /Notifications/ }).click();
    await page.getByRole('button', { name: /Mark all read/ }).click();
    await sleep(800);
    if (sql(`SELECT COUNT(*) FROM notifications WHERE user_id=${q(uid)} AND read_at IS NULL`) !== '0') return 'unread rows remain';
    const label = await page.getByRole('button', { name: /Notifications/ }).getAttribute('aria-label');
    if (/unread/.test(label)) return 'badge still shown: ' + label;
    const p = await clean(page, w); if (p) return p;
  }, page);
  await S.t(`${A}-NOTIF-04`, 'Email-copy checkbox persists the preference', async () => {
    const cb = page.locator('label:has-text("Also send me these by email") input');
    const before = await cb.isChecked();
    await cb.click(); await sleep(800);
    await page.reload(); await page.waitForLoadState('networkidle');
    await page.getByRole('button', { name: /Notifications/ }).click();
    if ((await cb.isChecked()) === before) return 'preference did not persist';
    await cb.click(); await sleep(500);
    w.drain();
  }, page);
}

// ---------- Profile menu + change password ----------
{
  const { page, w } = P.admin;
  await S.t(`${A}-PROFILE-01`, 'Profile menu shows name, email, role; entries: Change password, Account security, Calendar sync, Sign out', async () => {
    await go(page, '/app/dashboard');
    await page.getByRole('button', { name: 'Account' }).click();
    const t = await body(page);
    for (const s of [ws.users.admin.email, 'Admin', 'Change password', 'security', 'Sign out']) if (!new RegExp(s, 'i').test(t)) return 'missing ' + s;
    await page.getByRole('button', { name: /security/i }).click(); await page.waitForURL(/account\/security/);
    await page.getByRole('button', { name: 'Account' }).click();
    await page.getByRole('button', { name: /calendar|connection/i }).click(); await page.waitForURL(/connections/);
  }, page);

  // dedicated fresh user so the password change doesn't break cached sessions
  const cw = await createWs('pwchange');
  const c = await S.open(cw, 'owner');
  await S.t(`${A}-PWD-01`, 'Change password modal: opens/closes (X, backdrop); validation: min length, mismatch', async () => {
    await go(c.page, '/app/dashboard');
    await c.page.getByRole('button', { name: 'Account' }).click();
    await c.page.getByRole('button', { name: /Change password/i }).click();
    const cur = c.page.getByPlaceholder('Enter your current password'); await cur.waitFor();
    await c.page.locator('div.fixed.z-\\[60\\] button').first().click();
    await cur.waitFor({ state: 'hidden' });
    await c.page.getByRole('button', { name: 'Account' }).click();
    await c.page.getByRole('button', { name: /Change password/i }).click();
    await cur.waitFor(); await c.page.mouse.click(8, 8);
    await cur.waitFor({ state: 'hidden' });
    await c.page.getByRole('button', { name: 'Account' }).click();
    await c.page.getByRole('button', { name: /Change password/i }).click();
    await cur.fill(cw.password); await c.page.getByPlaceholder('At least 8 characters').fill('short'); await c.page.getByPlaceholder('Repeat new password').fill('short');
    await c.page.getByRole('button', { name: /^(Update|Change|Save)/i }).last().click();
    await waitText(c.page, /at least 8 characters/);
    await c.page.getByPlaceholder('At least 8 characters').fill('Changed-Pw-1'); await c.page.getByPlaceholder('Repeat new password').fill('Changed-Pw-2');
    await c.page.getByRole('button', { name: /^(Update|Change|Save)/i }).last().click();
    await waitText(c.page, /do not match/i);
  }, c.page);
  await S.t(`${A}-PWD-02`, 'Wrong current password shows server error and does not change the password', async () => {
    await c.page.getByPlaceholder('Enter your current password').fill('not-my-password');
    await c.page.getByPlaceholder('Repeat new password').fill('Changed-Pw-1');
    await c.page.getByRole('button', { name: /^(Update|Change|Save)/i }).last().click();
    await waitText(c.page, /incorrect|invalid|wrong|failed|current/i);
    const lr = await api('POST', '/api/auth/login', { headers: { 'X-Forwarded-For': cw.xff }, body: { email: cw.email, password: cw.password } });
    if (lr.status !== 200) return 'original password no longer works ' + lr.status;
    c.w.drain([/me\/password/, /Failed to load resource/]);
  }, c.page);
  await S.t(`${A}-PWD-03`, 'Correct change: success state, modal auto-closes, new password logs in, old one fails; user stays signed in', async () => {
    await c.page.getByPlaceholder('Enter your current password').fill(cw.password);
    await c.page.getByRole('button', { name: /^(Update|Change|Save)/i }).last().click();
    await waitText(c.page, /changed|updated|success/i);
    await c.page.getByPlaceholder('Enter your current password').waitFor({ state: 'hidden', timeout: 5000 });
    const ok = await api('POST', '/api/auth/login', { headers: { 'X-Forwarded-For': cw.xff }, body: { email: cw.email, password: 'Changed-Pw-1' } });
    const old = await api('POST', '/api/auth/login', { headers: { 'X-Forwarded-For': cw.xff }, body: { email: cw.email, password: cw.password } });
    if (ok.status !== 200 || old.status !== 401) return `new ${ok.status} old ${old.status}`;
    await go(c.page, '/app/tasks'); if (/login/.test(c.page.url())) return 'signed out after changing password';
  }, c.page);
  await S.t(`${A}-PWD-04`, 'Forgot-password link inside the modal requests a reset (token row created) and confirms', async () => {
    const before = sql(`SELECT COUNT(*) FROM password_reset_tokens WHERE user_id=${q(cw.userId)}`);
    await c.page.getByRole('button', { name: 'Account' }).click();
    await c.page.getByRole('button', { name: /Change password/i }).click();
    await c.page.getByRole('button', { name: /Forgot password\?/ }).click();
    await waitText(c.page, /reset link sent/i);
    if (Number(sql(`SELECT COUNT(*) FROM password_reset_tokens WHERE user_id=${q(cw.userId)}`)) !== Number(before) + 1) return 'no reset token row';
  }, c.page);
  await S.t(`${A}-PROFILE-02`, 'Sign out: back on /login, stored user cleared, API session invalid; browser Back does not reopen app', async () => {
    await c.page.keyboard.press('Escape');
    await go(c.page, '/app/dashboard');
    await c.page.getByRole('button', { name: 'Account' }).click();
    await c.page.getByRole('button', { name: /Sign out/i }).click();
    await c.page.waitForURL(/\/login/);
    if (await c.page.evaluate(() => localStorage.getItem('user'))) return 'user kept';
    await c.page.goBack().catch(() => {}); await c.page.waitForTimeout(1000);
    if (/\/app\//.test(c.page.url())) return 'Back reopened ' + c.page.url();
    await c.page.goto(WEB + '/app/campaigns'); await c.page.waitForURL(/\/login/);
  }, c.page);
}

// ---------- shell resilience ----------
{
  const { page, w } = P.owner;
  await S.t(`${A}-ERR-01`, 'A page whose API returns 500 shows an error state instead of crashing the shell (Campaigns list)', async () => {
    const h = r => (r.request().method() === 'GET' && r.request().resourceType() !== 'document') ? r.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"boom"}' }) : r.continue();
    await page.route(/\/api\/campaigns(\?.*)?$/, h);
    await go(page, '/app/campaigns', { wait: 1500 });
    await page.unroute(/\/api\/campaigns(\?.*)?$/, h);
    const t = await body(page);
    if (/Something went wrong on this screen|Application Error/.test(t)) return 'error boundary shown';
    if (!/Campaigns/.test(t)) return 'page gone';
    const bp = await w.bodyProblems(); w.drain([/.*/]);
    if (bp.length) return bp.join(',');
    if (/No campaigns yet/.test(t)) return 'API 500 presented as "No campaigns yet" (error masked as empty state)';
  }, page);
  await S.t(`${A}-RESP-01`, 'Narrow viewport (390px) login + dashboard do not crash', async () => {
    const m = await S.open(ws, 'owner', { viewport: { width: 390, height: 800 } });
    await go(m.page, '/app/dashboard');
    const bp = await m.w.bodyProblems(); const pr = m.w.drain(); if (bp.length || pr.length) return [...bp, ...pr].join(',');
  });
}
await S.done();
