import { Workspace, launch, session, watch, sql } from '../ui/ui_common.mjs';
const route = process.argv[2] || '/app/dashboard';
const role = process.argv[3] || 'owner';
const ws = await Workspace.create('probe');
if (role !== 'owner') await ws.addUser(role.toUpperCase(), role);
const u = ws.users[role];
const b = await launch();
const { page } = await session(b, { email: u.email, password: ws.password });
await page.goto('http://localhost:8190/vector' + route); await page.waitForLoadState('networkidle');
await page.waitForTimeout(800);
console.log(page.url());
const info = await page.evaluate(() => {
  const out = [];
  document.querySelectorAll('button,a,input,select,textarea,[role=tab],[role=button]').forEach(e => {
    const r = e.getBoundingClientRect(); if (!r.width) return;
    out.push(`${e.tagName.toLowerCase()}${e.type?'['+e.type+']':''} "${(e.innerText||e.value||'').trim().slice(0,40)}" ph=${e.placeholder||''} aria=${e.getAttribute('aria-label')||''} href=${e.getAttribute('href')||''}`);
  }); return out;
});
console.log(info.join('\n'));
console.log('---BODY---\n' + (await page.locator('body').innerText()).slice(0, 2500));
await b.close();
