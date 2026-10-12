// UI system cases for Contact Management (BRD §5.2). Run by run_contacts.py:
//   PW_BASE=<dir with node_modules/playwright> WEB_URL=... UI_EMAIL=... UI_PASSWORD=... UI_CONTACT_ID=... node ui_contacts.mjs
// Prints one JSON line per case: {"id","title","pass","detail"}.
import { createRequire } from 'module';
import fs from 'fs';
const require = createRequire((process.env.PW_BASE || process.cwd()) + '/');
const { chromium } = require('playwright');

const WEB = (process.env.WEB_URL || 'http://localhost:8190/vector').replace(/\/$/, '');
const shots = process.env.UI_SHOTS;
if (shots) fs.mkdirSync(shots, { recursive: true });
const out = (id, title, pass, detail, category) => console.log(JSON.stringify({ id, title, pass, detail, category }));

const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
let errs = [];
page.on('pageerror', e => errs.push('pageerror: ' + e.message));
page.on('console', m => { if (m.type() === 'error') errs.push('console: ' + m.text().slice(0, 200)); });
page.on('response', r => { if (r.status() >= 500) errs.push(r.status() + ' ' + r.url()); });
const take = () => { const e = errs.filter(x => !/favicon|ResizeObserver|401/.test(x)); errs = []; return e; };
const shot = async n => { if (shots) await page.screenshot({ path: `${shots}/${n}.png`, fullPage: false }).catch(() => {}); };

// CM-UI-01 login + Contacts list
try {
  await page.goto(`${WEB}/login`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('input[type=email]', { timeout: 20000 });
  await page.fill('input[type=email]', process.env.UI_EMAIL);
  await page.fill('input[type=password]', process.env.UI_PASSWORD);
  await page.click('button[type=submit]');
  await page.waitForURL(u => !u.pathname.endsWith('/login'), { timeout: 20000 });
  take();
  await page.goto(`${WEB}/app/contacts`, { waitUntil: 'networkidle', timeout: 30000 });
  await page.waitForTimeout(1500);
  const body = await page.innerText('body');
  await shot('contacts-list');
  const e = take();
  // newest contacts first; any row with an email address of this workspace proves the table rendered
  const hasRow = /@[a-z0-9-]+\.(com|io|net|ai|org)/i.test(body) && /contacts/i.test(body);
  out('CM-UI-01', 'UI: log in and open the Contacts list (BR-CM-17)', hasRow && e.length === 0,
      `contact visible=${hasRow} errors=${JSON.stringify(e).slice(0, 300)}`);
} catch (ex) {
  await shot('contacts-list-fail');
  out('CM-UI-01', 'UI: log in and open the Contacts list (BR-CM-17)', false, String(ex).slice(0, 300));
}

// CM-UI-02 contact record page
try {
  await page.goto(`${WEB}/app/contacts/${process.env.UI_CONTACT_ID}`, { waitUntil: 'networkidle', timeout: 30000 });
  await page.waitForTimeout(1500);
  const body = await page.innerText('body');
  await shot('contact-record');
  const e = take();
  const name = body.includes('Jane') && body.includes('Doe');
  const timeline = /timeline|activity|activities/i.test(body);
  const stage = /lifecycle/i.test(body);
  out('CM-UI-02', 'UI: contact record page shows properties, lifecycle and timeline (BR-CM-12)',
      name && timeline && stage && e.length === 0,
      `name=${name} timeline=${timeline} lifecycle=${stage} errors=${JSON.stringify(e).slice(0, 300)}`);
} catch (ex) {
  out('CM-UI-02', 'UI: contact record page shows properties, lifecycle and timeline (BR-CM-12)', false, String(ex).slice(0, 300));
}

// CM-UI-03 import screen
try {
  await page.goto(`${WEB}/app/import`, { waitUntil: 'networkidle', timeout: 30000 });
  await page.waitForTimeout(1200);
  const body = await page.innerText('body');
  await shot('import');
  const e = take();
  const fileInput = await page.$('input[type=file]');
  const ok = /import/i.test(body) && !!fileInput;
  out('CM-UI-03', 'UI: Import screen renders with a file picker (BR-CM-25)', ok && e.length === 0,
      `import text=${/import/i.test(body)} file_input=${!!fileInput} errors=${JSON.stringify(e).slice(0, 300)}`);
} catch (ex) {
  out('CM-UI-03', 'UI: Import screen renders with a file picker (BR-CM-25)', false, String(ex).slice(0, 300));
}

// CM-UI-04 lists, tasks, companies pages
try {
  const res = {};
  for (const p of ['lists', 'tasks', 'accounts']) {
    await page.goto(`${WEB}/app/${p}`, { waitUntil: 'networkidle', timeout: 30000 });
    await page.waitForTimeout(800);
    await shot(p);
    res[p] = take();
  }
  const bad = Object.entries(res).filter(([, v]) => v.length);
  out('CM-UI-04', 'UI: Lists, Tasks and Companies pages render without errors (BR-CM-15/17/22)', bad.length === 0,
      `errors=${JSON.stringify(bad).slice(0, 300)}`);
} catch (ex) {
  out('CM-UI-04', 'UI: Lists, Tasks and Companies pages render without errors (BR-CM-15/17/22)', false, String(ex).slice(0, 300));
}
await browser.close();
