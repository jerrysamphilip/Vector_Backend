// Playwright UI checks for the reports/platform suite (RP-150..RP-155).
// Usage: node ui_check.mjs <config.json>   (run from a directory where node_modules/playwright resolves)
// Prints one JSON line {cases:[{id,title,pass,detail}]} as the last stdout line.
import { chromium } from 'playwright';
import fs from 'fs';

const cfg = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const exp = cfg.expected;
const cases = [];
const rec = (id, title, pass, detail = '') => cases.push({ id, title, pass: !!pass, detail: String(detail).slice(0, 400) });
const cur = new Intl.NumberFormat('en', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });
const compact = new Intl.NumberFormat('en', { style: 'currency', currency: 'USD', notation: 'compact', maximumFractionDigits: 1 });
const money = v => (v == null ? '—' : cur.format(v));
// ICU builds differ on trailing '.0' in compact notation ("$45.0K" vs "$45K"); the app drops it
const short = v => (v == null ? '—' : compact.format(v).replace(/\.0(?=[KMBT])/, ''));
fs.mkdirSync(cfg.shots, { recursive: true });

const browser = await chromium.launch({
    executablePath: process.env.PW_CHROMIUM || '/opt/pw-browsers/chromium', args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const pageErrors = [];
page.on('pageerror', e => pageErrors.push(`${page.url()}: ${e.message}`));
page.on('response', r => { if (r.status() >= 500) pageErrors.push(`HTTP ${r.status()} ${r.url()}`); });

async function text() { return (await page.locator('body').innerText()).replace(/ /g, ' '); }
async function settle(probe) {
    try { await page.waitForFunction(p => document.body.innerText.includes(p), probe, { timeout: 20000 }); } catch { /* checked below */ }
    await page.waitForTimeout(800);
}

try {
    await page.goto(`${cfg.web}/login`, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await page.fill('input[type="email"]', cfg.email);
    await page.fill('input[type="password"]', cfg.password);
    await page.click('button[type="submit"]');
    await page.waitForURL(/\/app\//, { timeout: 30000 });
    rec('RP-150', 'UI: owner signs in through the login form and lands in the app', true, page.url());
} catch (e) {
    rec('RP-150', 'UI: owner signs in through the login form and lands in the app', false, e.message);
}

// Sales dashboard
try {
    const k = exp.dash.kpis;
    await page.goto(`${cfg.web}/app/sales`, { waitUntil: 'domcontentloaded' });
    await settle('Open pipeline');
    const t = await text();
    await page.screenshot({ path: `${cfg.shots}/sales_dashboard.png`, fullPage: true });
    const want = [short(k.open_pipeline), short(k.won_amount), `${k.won_count} deals`, `${k.open_opportunities} deals`,
        `${k.sqls} became SQL`];
    const missing = want.filter(w => !t.includes(w));
    rec('RP-151', 'UI: sales dashboard tiles show the same KPIs as the API (pipeline, won, counts)', !missing.length,
        missing.length ? `missing ${JSON.stringify(missing)}` : want.join(' | '));
} catch (e) { rec('RP-151', 'UI: sales dashboard tiles match API', false, e.message); }

// Forecast tab
try {
    const f = exp.forecast;
    await page.goto(`${cfg.web}/app/sales-reports?tab=forecast`, { waitUntil: 'domcontentloaded' });
    await settle('Quarter-wise');
    const t = await text();
    await page.screenshot({ path: `${cfg.shots}/forecast.png`, fullPage: true });
    const want = [f.label, ...f.quarters.map(q => q.label), ...f.quarters.map(q => money(q.forecast)), money(f.total.forecast),
        money(f.total.won), money(f.total.weighted), `${f.open_without_close_date.count} deals not in the forecast`];
    const missing = want.filter(w => !t.includes(w));
    rec('RP-152', 'UI: forecast tab shows quarter and FY figures equal to the API', !missing.length,
        missing.length ? `missing ${JSON.stringify(missing)}` : `${want.length} values matched`);
} catch (e) { rec('RP-152', 'UI: forecast tab matches API', false, e.message); }

// Team performance tab
try {
    await page.goto(`${cfg.web}/app/sales-reports?tab=team`, { waitUntil: 'domcontentloaded' });
    const reps = exp.team.reps;
    await settle(reps[reps.length - 1].name);
    const t = await text();
    await page.screenshot({ path: `${cfg.shots}/team.png`, fullPage: true });
    const want = [...reps.map(r => r.name), ...reps.filter(r => r.won_amount).map(r => short(r.won_amount)), short(exp.team.totals.won_amount),
        `${exp.team.totals.leads}`];
    const missing = want.filter(w => !t.includes(w));
    rec('RP-153', 'UI: team performance lists every rep with revenue won as the API', !missing.length,
        missing.length ? `missing ${JSON.stringify(missing)}` : `${want.length} values matched`);
} catch (e) { rec('RP-153', 'UI: team tab matches API', false, e.message); }

// Targets & leaderboard tab
try {
    await page.goto(`${cfg.web}/app/sales-reports?tab=targets`, { waitUntil: 'domcontentloaded' });
    const rows = exp.targets.rows;
    await settle(rows[0].name);
    const t = await text();
    await page.screenshot({ path: `${cfg.shots}/targets.png`, fullPage: true });
    const want = [...rows.filter(r => r.target).map(r => money(r.target)), rows[0].name];
    const missing = want.filter(w => !t.includes(w));
    rec('RP-154', 'UI: targets & leaderboard tab shows rep targets as the API', !missing.length,
        missing.length ? `missing ${JSON.stringify(missing)}` : `${want.length} values matched`);
} catch (e) { rec('RP-154', 'UI: targets tab matches API', false, e.message); }

// Remaining report tabs render without errors
for (const tab of ['leads', 'pipeline', 'roi', 'custom']) {
    try {
        await page.goto(`${cfg.web}/app/sales-reports?tab=${tab}`, { waitUntil: 'domcontentloaded' });
        await page.waitForTimeout(2500);
    } catch (e) { pageErrors.push(`${tab}: ${e.message}`); }
}
rec('RP-155', 'UI: dashboard and every report tab render without page errors or 5xx responses', !pageErrors.length,
    pageErrors.join(' | '));
await browser.close();
console.log(JSON.stringify({ cases }));
