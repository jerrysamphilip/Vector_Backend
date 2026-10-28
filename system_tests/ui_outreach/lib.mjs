// Shared helpers for the ui_outreach suites (extends ../ui/ui_common.mjs without editing it).
import { Workspace, launch, session, watch, Recorder, step, api, sql, loginToken, API, WEB, caseFilter } from '../ui/ui_common.mjs';
import { onConfirm } from '../ui/confirm_dialogs.mjs';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
export { Workspace, launch, session, watch, Recorder, step, api, sql, loginToken, API, WEB };

export const HERE = path.dirname(new URL(import.meta.url).pathname);
export const SHOTS = path.join(HERE, 'shots');
fs.mkdirSync(SHOTS, { recursive: true });
export const q = v => (v === null || v === undefined) ? 'NULL' : typeof v === 'number' ? String(v) : `'${String(v).replace(/'/g, "''")}'`;
export const sha256 = s => crypto.createHash('sha256').update(s).digest('hex');
export const rid = () => crypto.randomBytes(3).toString('hex');
export const sleep = ms => new Promise(r => setTimeout(r, ms));

/** Start a suite: browser + recorder + helper closures. */
export async function startSuite(name) {
  const rec = new Recorder(`UI outreach/admin - ${name}`, path.join(HERE, 'out', name));
  const browser = await launch();
  const ctxs = [];
  const S = { name, rec, browser, ctxs };
  /** Open a logged-in page for ws.users[key]; returns {page, w, ctx}. */
  S.open = async (ws, key = 'owner', opts = {}) => {
    const u = ws.users[key];
    let sess; let lastErr;
    for (let i = 0; i < 4 && !sess; i++) {
      try { sess = await session(browser, { email: u.email, password: ws.password }, opts); }
      catch (e) { lastErr = e; console.log('  (login retry: ' + String(e.message).split('\n')[0] + ')'); await sleep(30000); }
    }
    if (!sess) throw lastErr;
    const { ctx, page } = sess;
    ctxs.push(ctx);
    const w = watch(page);
    page.setDefaultTimeout(12000);
    await page.waitForLoadState('networkidle').catch(() => {});
    w.drain();
    return { ctx, page, w, user: u };
  };
  S.shot = async (page, id) => { try { await page.screenshot({ path: path.join(SHOTS, `${name}-${id}.png`), fullPage: false }); } catch {} };
  /** Run a step; failure takes a screenshot of `getPage()`. */
  // ONLY=<regex>: run just the matching case ids; results are merged into the suite's previous results.json.
  // TEST_CASES (a tier's selection, see ../ui/ui_common.mjs caseFilter) narrows the same way; the Recorder
  // then carries the unselected previous rows over itself, so the ONLY merge below is not used for it.
  const ONLY = process.env.ONLY ? new RegExp(process.env.ONLY) : null;
  S.only = ONLY;
  S.want = id => (!ONLY || ONLY.test(id)) && caseFilter.wants(id);
  S.t = async (id, title, fn, page) => { if (!S.want(id)) return; return step(rec, id, title, fn, page ? (i) => S.shot(typeof page === 'function' ? page() : page, i) : undefined); };
  S.done = async () => {
    for (const c of ctxs) await c.close().catch(() => {}); await browser.close().catch(() => {});
    if (ONLY) {
      let prev = [];
      try { prev = JSON.parse(fs.readFileSync(path.join(rec.outDir, 'results.json'), 'utf8')).cases || []; } catch { /* none */ }
      const fresh = new Map(rec.cases.map(c => [c.id, c]));
      const merged = prev.map(c => fresh.get(c.id) || c);
      for (const c of rec.cases) if (!prev.some(p => p.id === c.id)) merged.push(c);
      rec.cases = merged;
    }
    const c = rec.write(); console.log(`\n[${name}]`, JSON.stringify(c)); return c;
  };
  return S;
}

export const U = p => WEB + p;
export async function go(page, route, { wait = 600 } = {}) {
  for (let i = 0; ; i++) {
    try { await page.goto(WEB + route); break; }
    catch (e) { if (i >= 6 || !/ERR_CONNECTION|ERR_EMPTY|ERR_NETWORK|ERR_ABORTED/.test(e.message)) throw e; await page.waitForTimeout(5000); }
  }
  await page.waitForLoadState('networkidle').catch(() => {});
  if (wait) await page.waitForTimeout(wait);
}
export const body = async page => (await page.locator('body').innerText().catch(() => '')) || '';

/** assert helper: returns undefined (pass) or message (fail) */
export function must(cond, msg) { return cond ? undefined : msg; }
/** Combine problems: returns first failing message or undefined */
export function firstProblem(...msgs) { const m = msgs.flat().filter(Boolean); return m.length ? m.join(' | ') : undefined; }
/** Clean check: no watch problems and no bad body text. */
export async function clean(page, w, allow = []) {
  const probs = w.drain(allow);
  const bp = await w.bodyProblems();
  const all = [...probs, ...bp];
  return all.length ? all.join(' ; ') : undefined;
}

// ── seeding via API ─────────────────────────────────────────
export async function mkInbox(ws, local, domain, kw = {}) {
  const r = await api('POST', '/inboxes', { token: ws.token, body: { email_address: `${local}@${domain}`, smtp_host: 'smtp.invalid',
    smtp_username: local, smtp_password: `Smtp-${ws.rid}`, warmup_enabled: false, delay_between_emails: 1, daily_limit: 500, max_emails_per_day: 500, ...kw } });
  if (r.status !== 201) throw new Error(`inbox ${r.status} ${JSON.stringify(r.data).slice(0, 200)}`);
  return r.data.inbox_id;
}
export async function mkContact(ws, email, kw = {}) {
  const r = await api('POST', '/contacts', { token: ws.token, body: { email, first_name: 'Pat', last_name: 'Test', company_name: 'Acme Corp', ...kw } });
  if (r.status !== 201) throw new Error(`contact ${r.status} ${JSON.stringify(r.data).slice(0, 200)}`);
  return r.data.prospect_id;
}
export async function mkCampaign(ws, name, inboxIds, { waits = [0], token, ...kw } = {}) {
  const d = new Date(Date.now() + 20 * 864e5).toISOString().slice(0, 10);
  const r = await api('POST', '/campaigns', { token: token || ws.token, body: { campaign_name: name, inbox_ids: inboxIds, respect_timezone: false,
    campaign_timezone: 'UTC', min_gap_minutes: 0, send_window_start: '09:00:00', send_window_end: '17:00:00', start_date: d, ...kw } });
  if (r.status !== 201) throw new Error(`campaign ${r.status} ${JSON.stringify(r.data).slice(0, 300)}`);
  const cid = r.data.campaign_id;
  for (let i = 0; i < waits.length; i++) {
    const s = await api('POST', `/campaigns/${cid}/sequences`, { token: token || ws.token, body: { step_number: i + 1, wait_days: waits[i] } });
    if (s.status !== 201) throw new Error(`step ${s.status} ${JSON.stringify(s.data).slice(0, 200)}`);
  }
  return cid;
}
export const stubContent = (ws, cid) => api('POST', `/campaigns/${cid}/content/stub`, { token: ws.token });
export const enrollP = (ws, cid, pids) => api('POST', `/campaigns/${cid}/prospects`, { token: ws.token, body: { prospect_ids: pids } });

// ── TOTP (RFC 6238) ─────────────────────────────────────────
export function base32decode(s) {
  const A = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'; let bits = ''; 
  for (const c of s.replace(/=+$/, '').replace(/\s/g, '').toUpperCase()) bits += A.indexOf(c).toString(2).padStart(5, '0');
  const bytes = []; for (let i = 0; i + 8 <= bits.length; i += 8) bytes.push(parseInt(bits.slice(i, i + 8), 2));
  return Buffer.from(bytes);
}
export function totp(secret, offset = 0) {
  const counter = Math.floor(Date.now() / 1000 / 30) + offset;
  const buf = Buffer.alloc(8); buf.writeBigUInt64BE(BigInt(counter));
  const h = crypto.createHmac('sha1', base32decode(secret)).update(buf).digest();
  const o = h[h.length - 1] & 0xf;
  const code = (h.readUInt32BE(o) & 0x7fffffff) % 1e6;
  return String(code).padStart(6, '0');
}

/** Wait until the page text matches regex; returns text. */
export async function waitText(page, re, timeout = 8000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeout) { const t = await body(page); if (re.test(t)) return t; await page.waitForTimeout(150); }
  throw new Error(`text ${re} not found; page says: ${(await body(page)).replace(/\s+/g, ' ').slice(-300)}`);
}
/** Self sign-up is limited to 10/hour per client IP and shared by every agent; present a distinct
 *  test-client address per workspace (the API trusts the last X-Forwarded-For hop). */
export async function createWs(label) {
  const w = new Workspace();
  w.rid = crypto.randomBytes(3).toString('hex');
  w.email = `owner-${label}-${w.rid}@uitest.example`;
  w.password = `Uitest-${w.rid}-pw!`;
  w.xff = `10.${100 + Math.floor(Math.random() * 100)}.${parseInt(w.rid.slice(0, 2), 16)}.${parseInt(w.rid.slice(2, 4), 16)}`;
  const r = await api('POST', '/api/auth/register', { headers: { 'X-Forwarded-For': w.xff }, body: { first_name: 'Ui', last_name: label, email: w.email,
    password: w.password, tenant_name: `Ui ${label} ${w.rid}` } });
  if (r.status !== 201) throw new Error(`register: ${r.status} ${JSON.stringify(r.data)}`);
  w.token = r.data.access_token;
  w.tenantId = sql(`SELECT tenant_id FROM users WHERE email=${q(w.email)}`);
  w.userId = sql(`SELECT user_id FROM users WHERE email=${q(w.email)}`);
  w.users = { owner: { email: w.email, userId: w.userId, token: w.token } };
  w.addUser = async function (role, first, extra = {}) {
    const email = `${first.toLowerCase()}-${this.rid}@uitest.example`;
    const hash = sql(`SELECT password_hash FROM users WHERE email=${q(this.email)}`);
    const userId = crypto.randomUUID();
    const cols = { user_id: userId, tenant_id: this.tenantId, first_name: first, last_name: 'Test', email, role,
      status: 'ACTIVE', password_hash: hash, auth_provider: 'local', email_verified: 1, ...extra };
    sql(`INSERT INTO users (${Object.keys(cols).join(',')}, created_at) VALUES (${Object.values(cols).map(q).join(',')}, NOW())`);
    const lr = await api('POST', '/api/auth/login', { headers: { 'X-Forwarded-For': this.xff }, body: { email, password: this.password } });
    const u = { email, userId, token: lr.data.access_token, password: this.password };
    this.users[first.toLowerCase()] = u;
    return u;
  };
  return w;
}
/** Build a fresh workspace with owner + admin + manager + agent users and a sender identity. */
export async function fullWorkspace(label, { identity = true, roles = ['admin', 'manager', 'agent'] } = {}) {
  const ws = await createWs(label);
  const R = { admin: 'ADMIN', manager: 'MANAGER', agent: 'AGENT' };
  for (const r of roles) await ws.addUser(R[r], r);
  if (identity) await ws.setSenderIdentity();
  return ws;
}

// TOTP codes are single-use per 30s step (server replay protection), so wait for a step later than the last one used.
const lastStep = new Map();
export async function freshTotp(secret) {
  for (;;) {
    const cur = Math.floor(Date.now() / 30000);
    const last = lastStep.get(secret) ?? 0;
    if (cur > last) { lastStep.set(secret, cur); return totp(secret); }
    await sleep(1000);
  }
}
/** Enable 2FA for a user token through the API; returns {secret, recovery}. */
export async function enableMfa(token) {
  const s = await api('POST', '/api/auth/mfa/setup', { token });
  const e = await api('POST', '/api/auth/mfa/enable', { token, body: { code: await freshTotp(s.data.secret) } });
  if (e.status !== 200) throw new Error('mfa enable ' + e.status + JSON.stringify(e.data));
  return { secret: s.data.secret, recovery: e.data.recovery_codes };
}

// Cached workspaces (sign-ups and logins are rate limited and shared with other suites).
const STATE = path.join(HERE, '.state.json');
const loadState = () => { try { return JSON.parse(fs.readFileSync(STATE, 'utf8')); } catch { return {}; } };
export async function getWs(label, opts = {}) {
  const st = loadState();
  const c = st[label];
  if (c && !opts.fresh && sql(`SELECT COUNT(*) FROM users WHERE email=${q(c.email)}`) === '1') {
    const ws = new Workspace(); Object.assign(ws, c);
    ws.addUser = async function () { throw new Error('addUser on cached ws'); };
    for (const k of Object.keys(ws.users)) {
      const lr = await api('POST', '/api/auth/login', { headers: { 'X-Forwarded-For': ws.xff }, body: { email: ws.users[k].email, password: ws.password } });
      if (lr.status === 200 && lr.data.access_token) ws.users[k].token = lr.data.access_token; else if (lr.status === 200) ws.users[k].token = null;
    }
    ws.token = ws.users.owner.token;
    return ws;
  }
  const ws = await fullWorkspace(label, opts);
  if (!opts.fresh) { const s2 = loadState(); const { token, ...rest } = ws; s2[label] = { ...rest, users: Object.fromEntries(Object.entries(ws.users).map(([k, v]) => [k, { ...v, token: undefined }])) }; fs.writeFileSync(STATE, JSON.stringify(s2, null, 1)); }
  return ws;
}

/** Sidebar group header button (accessible name is the DOM text, CSS uppercases it). */
export const groupBtn = (page, g) => page.getByRole('button', { name: new RegExp('^' + g + '$', 'i') });

/** Auto-handle native alert/confirm dialogs and the app's in-app ConfirmDialog (Radix, since frontend b3f05f5);
 *  returns the log array ("confirm: <title>\n\n<message>" for the in-app dialog). mode: 'accept' | 'dismiss' */
export function dialogs(page, mode = 'accept') {
  const log = [];
  const h = async d => { log.push(`${d.type()}: ${d.message()}`); if (mode === 'accept') await d.accept().catch(() => {}); else await d.dismiss().catch(() => {}); };
  page.on('dialog', h);
  const stopConfirm = onConfirm(page, text => { log.push(`confirm: ${text}`); return mode; });
  log.stop = () => { page.off('dialog', h); stopConfirm(); };
  return log;
}
export const rowOf = (page, text) => page.locator('tbody tr', { hasText: text }).first();
/** Seed: ready (launched) campaign with stub content and enrolled prospects. */
export async function seedActive(ws, name, inboxIds, pids, kw = {}) {
  const cid = await mkCampaign(ws, name, inboxIds, kw);
  let r = await enrollP(ws, cid, pids); if (r.status !== 200) throw new Error('enroll ' + r.status + JSON.stringify(r.data));
  // a rejected contact (e.g. active_elsewhere: already in another active campaign) is a 200 with a rejected_summary
  if (r.data && r.data.enrolled_count !== undefined && r.data.enrolled_count !== pids.length) throw new Error('enroll: ' + JSON.stringify(r.data).slice(0, 300));
  r = await stubContent(ws, cid); if (r.status !== 200) throw new Error('stub ' + r.status);
  r = await api('POST', `/campaigns/${cid}/launch`, { token: ws.token }); if (r.status !== 200) throw new Error('launch ' + r.status + JSON.stringify(r.data));
  return cid;
}
export const campStatus = cid => sql(`SELECT status FROM campaigns WHERE campaign_id=${q(cid)}`);
export async function gotoCampaigns(page, w) { await go(page, '/app/campaigns', { wait: 800 }); }
