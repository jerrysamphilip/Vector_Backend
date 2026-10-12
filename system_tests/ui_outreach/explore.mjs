import { startSuite, getWs, go, body, api, mkInbox, mkContact, sql, q } from './lib.mjs';
const route = process.argv[2] || '/app/campaigns/new?fresh=true';
const role = process.argv[3] || 'owner';
const S = await startSuite('explore');
const ws = await getWs('explore', { roles: ['agent'] });
if (!ws.seeded) {
  const has = sql(`SELECT COUNT(*) FROM sending_inboxes WHERE tenant_id=${q(ws.tenantId)}`);
  if (has === '0') {
    await mkInbox(ws, 'sales', `ex${ws.rid}.example`);
    const pids = []; for (let i = 0; i < 4; i++) pids.push(await mkContact(ws, `p${i}@acme${ws.rid}.example`, { first_name: 'P' + i, job_title: ['CEO', 'VP Sales', 'Engineer', 'Founder'][i] }));
    const l = await api('POST', '/lists', { token: ws.token, body: { list_name: 'Explore list' } });
    await api('POST', `/lists/${l.data.list_id}/members`, { token: ws.token, body: { prospect_ids: pids } });
  }
}
const { page, w } = await S.open(ws, role);
page.on('dialog', d => { console.log('DIALOG', d.type(), d.message()); d.dismiss(); });
await go(page, route, { wait: 1500 });
const dump = async () => {
  console.log('URL', page.url());
  console.log((await page.evaluate(() => {
    const out = [];
    document.querySelectorAll('main button,main a,main input,main select,main textarea,main [role=tab],main [role=switch],main [contenteditable=true]').forEach(e => {
      const r = e.getBoundingClientRect(); if (!r.width) return;
      out.push(`${e.tagName.toLowerCase()}${e.type?'['+e.type+']':''} "${(e.innerText||e.value||'').trim().replace(/\s+/g,' ').slice(0,50)}" ph=${e.placeholder||''} aria=${e.getAttribute('aria-label')||''} name=${e.name||''} id=${e.id||''} ${e.disabled?'DISABLED':''}`);
    }); return out;
  })).join('\n'));
  console.log('---BODY---\n' + (await body(page)).replace(/\n+/g, '\n').slice(0, 3500));
};
await dump();
if (process.env.STEPS) { for (const st of process.env.STEPS.split('||')) { console.log('\n>>>', st); const [kind, ...r] = st.split('::'); const arg = r.join('::'); if (kind === 'click') await page.getByRole('button', { name: new RegExp(arg, 'i') }).first().click(); else if (kind === 'clicktext') await page.getByText(new RegExp(arg, 'i')).first().click(); else if (kind === 'fill') { const [ph, v] = arg.split('=>'); await page.getByPlaceholder(new RegExp(ph, 'i')).first().fill(v); } else if (kind === 'goto') await go(page, arg); await page.waitForTimeout(1500); await dump(); } }
console.log('PROBLEMS', w.drain());
await S.done();
