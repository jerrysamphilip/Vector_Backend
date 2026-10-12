// Playwright UI checks for the sales suite (SP-151..SP-158). Called by run_sales_pipeline.py with a
// fixture JSON path; prints one JSON line [{id,title,pass,detail}] as the last stdout line.
// Playwright is resolved from PW_MODULES (a folder whose node_modules has playwright).
import { createRequire } from 'module';
import fs from 'fs';
const require = createRequire((process.env.PW_MODULES || process.cwd()).replace(/\/?$/, '/'));
const { chromium } = require('playwright');

const fx = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
fs.mkdirSync(fx.out, { recursive: true });
const results = [];
const rec = (id, title, pass, detail = '') => results.push({ id, title, pass: !!pass, detail: String(detail).slice(0, 400) });
const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' });

async function session(email) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const p = await ctx.newPage();
  p.errors = [];
  p.on('pageerror', e => p.errors.push(e.message));
  await p.goto(fx.web + '/login');
  await p.fill('input[type=email]', email);
  await p.fill('input[type=password]', fx.password);
  await p.click('button[type=submit]');
  await p.waitForURL(/\/app\//, { timeout: 30000 });
  return p;
}

async function text(p, path, shot, waitFor) {
  await p.goto(fx.web + '/app/' + path);
  await p.waitForLoadState('networkidle').catch(() => {});
  if (waitFor) await p.getByText(waitFor, { exact: false }).first().waitFor({ timeout: 15000 }).catch(() => {});
  await p.waitForTimeout(1500);
  await p.screenshot({ path: `${fx.out}/${shot}.png`, fullPage: true });
  return (await p.locator('body').innerText()).toLowerCase();
}

const has = (t, s) => t.includes(String(s).toLowerCase());
function scoped(id, title, t, want, notWant) {
  const missing = want.filter(s => !has(t, s));
  const leaked = notWant.filter(s => has(t, s));
  rec(id, title, missing.length === 0 && leaked.length === 0, `missing=${JSON.stringify(missing)} leaked=${JSON.stringify(leaked)}`);
}

try {
  const m = await session(fx.mgr1);
  let t = await text(m, 'leads', 'mgr1-leads', fx.team_leads[0]);
  scoped('SP-151', 'UI (BD manager): Leads screen shows own team leads and no other team', t, fx.team_leads, fx.other_leads);
  t = await text(m, 'sql-queue', 'mgr1-sql-queue', fx.team_sql[0]);
  scoped('SP-152', 'UI (BD manager): SQL queue shows own team SQLs only', t, fx.team_sql, fx.other_sql);
  t = await text(m, 'deals', 'mgr1-deals', fx.team_deals[0]);
  scoped('SP-153', 'UI (BD manager): Deals screen shows team deals only', t, fx.team_deals, fx.other_deals);
  t = await text(m, 'pipeline', 'mgr1-pipeline');
  rec('SP-154', 'UI (BD manager): Pipeline screen renders without errors and without other-team deals',
      m.errors.length === 0 && !fx.other_deals.some(s => has(t, s)) && t.length > 200, `errors=${m.errors.slice(0, 3)}`);

  const a = await session(fx.ag1a);
  t = await text(a, 'leads', 'ag1a-leads', fx.own_lead);
  scoped('SP-155', 'UI (exec): Leads screen shows own lead and not a peer\'s', t, [fx.own_lead], [fx.peer_lead, ...fx.other_leads]);
  t = await text(a, 'deals', 'ag1a-deals', fx.own_deal);
  scoped('SP-156', 'UI (exec): Deals screen shows own deal and not a peer\'s', t, [fx.own_deal], [fx.peer_deal, ...fx.other_deals]);
  t = await text(a, 'leads/' + fx.peer_lead_id, 'ag1a-peer-lead-url');
  rec('SP-157', 'UI (exec): opening a peer\'s lead by URL shows no peer data', !has(t, fx.peer_lead), `leaked=${has(t, fx.peer_lead)}`);
  t = await text(a, 'deals/' + fx.peer_deal_id, 'ag1a-peer-deal-url');
  rec('SP-158', 'UI (exec): opening a peer\'s deal by URL shows no peer data', !has(t, fx.peer_deal), `leaked=${has(t, fx.peer_deal)}`);
} catch (e) {
  rec('SP-159', 'UI script step', false, 'STEP FAILED: ' + e.message.split('\n')[0]);
}
await browser.close();
console.log(JSON.stringify(results));
