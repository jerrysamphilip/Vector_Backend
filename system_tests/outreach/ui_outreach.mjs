
import { chromium } from 'playwright';
// argv after the script: web, email, password, unsubUrl, (unused), campaignId, campaignName.
// OUT-132: the campaign id is argv.slice(2)[5] (process.argv[7]); reading process.argv[6] picked up the
// empty placeholder, so the campaign-detail capture was skipped and came back empty.
const [web, email, password, unsubUrl, , campaignId, campaignName] = process.argv.slice(2);
const out = {pages: {}, errors: [], unsub: null, campaign: null};
const browser = await chromium.launch({executablePath: process.env.CHROMIUM || undefined});
const page = await browser.newPage();
let current = 'login';
page.on('pageerror', e => out.errors.push(`${current}: ${e.message}`));
try {
  await page.goto(`${web}/login`, {waitUntil: 'networkidle'});
  await page.fill('input[type=email]', email);
  await page.fill('input[type=password]', password);
  await page.click('button[type=submit]');
  await page.waitForURL(u => !u.toString().includes('/login'), {timeout: 20000});
  out.loggedIn = page.url();
  for (const p of ['campaigns', 'inboxes', 'inbox', 'prospects', 'lists', 'domain-health']) {
    current = p;
    await page.goto(`${web}/app/${p}`, {waitUntil: 'networkidle'});
    await page.waitForTimeout(1500);
    const text = await page.locator('body').innerText();
    out.pages[p] = {url: page.url(), chars: text.length, crashed: /something went wrong|unexpected application error/i.test(text),
                    snippet: text.slice(0, 120).replace(/\s+/g, ' ')};
  }
  if (campaignId) {
    current = 'campaign-detail';
    await page.goto(`${web}/app/campaigns/${campaignId}`, {waitUntil: 'networkidle'});
    await page.getByText(campaignName || '@@').first().waitFor({timeout: 15000}).catch(() => {});
    const text = await page.locator('body').innerText();
    out.campaign = {chars: text.length, hasName: text.includes(campaignName || '@@'),
                    crashed: /something went wrong|unexpected application error/i.test(text)};
  }
  current = 'unsubscribe';
  const p2 = await browser.newPage();
  await p2.goto(unsubUrl, {waitUntil: 'load'});
  const before = await p2.locator('body').innerText();
  await p2.click('button[type=submit]');
  await p2.waitForLoadState('load');
  const after = await p2.locator('body').innerText();
  out.unsub = {before: before.slice(0, 80), after: after.slice(0, 80)};
} catch (e) { out.fatal = String(e); }
await browser.close();
console.log(JSON.stringify(out));
