// Shared helpers for the CRM/Sales functional UI suites (extends ../ui/ui_common.mjs without editing it).
import { Workspace, launch, session, watch, Recorder, api as rawApi, loginToken, API, WEB } from '../ui/ui_common.mjs';
import crypto from 'node:crypto';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
export { Workspace, launch, watch, Recorder, API, WEB };
/** Quiet MySQL helper (no password warning on stderr). */
export function sql(qy) {
  return execFileSync('docker', ['exec', process.env.MYSQL_CONTAINER || 'vector-mysql-1', 'mysql', '-uvector', '-pvector', process.env.MYSQL_DATABASE || 'outreach_ai', '-N', '-e', qy],
    { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim();
}

export const HERE = path.dirname(new URL(import.meta.url).pathname);
export const SHOTS = path.join(HERE, 'shots');
export const TMP = path.join(HERE, 'tmp');
fs.mkdirSync(SHOTS, { recursive: true }); fs.mkdirSync(TMP, { recursive: true });
const q = v => (v === null || v === undefined) ? 'NULL' : typeof v === 'number' ? String(v) : `'${String(v).replace(/'/g, "''")}'`;
export const sq = q;

// ---------- login pacing (the API allows 60 logins / 5 min / IP, shared with other agents) ----------
const loginTimes = [];
async function paceLogin() {
  const now = Date.now();
  while (loginTimes.length && now - loginTimes[0] > 300000) loginTimes.shift();
  if (loginTimes.length >= 25) { const wait = 300000 - (now - loginTimes[0]) + 1000; console.log(`  (pacing logins: waiting ${Math.round(wait / 1000)}s)`); await new Promise(r => setTimeout(r, wait)); }
  loginTimes.push(Date.now());
}

/** API call against the root-mounted CRM routes (/contacts, /leads ...). Returns {status, data}. */
export async function call(token, method, p, body) { return rawApi(method, p, { token, body }); }
export async function must(token, method, p, body, expect = [200, 201]) {
  const r = await call(token, method, p, body);
  if (!expect.includes(r.status)) throw new Error(`${method} ${p} -> ${r.status} ${JSON.stringify(r.data).slice(0, 300)}`);
  return r.data;
}

export const PASSWORD = 'Uitest-crm-pw!';
const STATE = path.join(HERE, 'state'); fs.mkdirSync(STATE, { recursive: true });
const WIPE_TABLES = ['accounts', 'campaigns', 'contact_activities', 'contact_field_definitions', 'crm_tasks', 'global_unsubscribes', 'import_jobs', 'leads',
  'notifications', 'opportunities', 'products', 'property_changes', 'proposals', 'prospect_lists', 'prospects', 'sales_settings', 'sales_stages', 'sales_targets',
  'saved_reports', 'saved_views', 'user_connections', 'workflow_rules', 'conversations', 'message_templates',
  'contact_companies', 'note_mentions', 'forecast_snapshots'];
const readJson = (f, d = null) => { try { return JSON.parse(fs.readFileSync(f, 'utf8')); } catch { return d; } };
function jwtExp(tok) { try { return JSON.parse(Buffer.from(tok.split('.')[1], 'base64url').toString()).exp * 1000; } catch { return 0; } }

/**
 * A reusable tenant (created by SQL: /auth/register is limited to 10 per hour per IP and shared with other agents).
 * Tenant.get(label) re-uses the tenant of an earlier run, wipes its CRM data and role users' hierarchy, so every run starts clean
 * while real-login sessions (cookies) and API tokens stay cached.
 */
export class Tenant {
  static async get(label, { keepData = false } = {}) {
    const f = path.join(STATE, `${label}.json`);
    let st = readJson(f);
    const t = new Tenant(); t.label = label; t.file = f;
    let hash = readJson(path.join(STATE, 'pwhash.json'))?.hash;
    if (!hash) { hash = execFileSync('docker', ['exec', 'vector-api-1', 'python', '-c', `from app.core.security import hash_password; print(hash_password('${PASSWORD}'))`], { encoding: 'utf8' }).trim().split('\n').pop(); fs.writeFileSync(path.join(STATE, 'pwhash.json'), JSON.stringify({ hash })); }
    t.hash = hash;
    if (st && sql(`SELECT COUNT(*) FROM tenants WHERE tenant_id=${q(st.tenantId)}`) === '1') {
      t.st = st; t.tenantId = st.tenantId;
      if (!keepData) t.wipe();
    } else {
      const rid = crypto.randomBytes(3).toString('hex');
      const tenantId = crypto.randomUUID();
      sql(`INSERT INTO tenants (tenant_id, tenant_name, status, created_at, updated_at, require_mfa) VALUES (${q(tenantId)}, ${q(`Ui crm ${label} ${rid}`)}, 'ACTIVE', NOW(), NOW(), 0)`);
      t.st = { tenantId, rid, label, users: {} };
      fs.writeFileSync(f, JSON.stringify(t.st, null, 1));
    }
    t.tenantId = t.st.tenantId; t.rid = t.st.rid; t.users = {};
    await t.addUser('SUPER_ADMIN', 'Owner', { sales_level: null, manager_id: null, custom_permissions: null }, 'owner');
    return t;
  }
  wipe() {
    sql(`SET FOREIGN_KEY_CHECKS=0; ${WIPE_TABLES.map(tb => `DELETE FROM ${tb} WHERE tenant_id=${q(this.tenantId)};`).join(' ')} SET FOREIGN_KEY_CHECKS=1;`);
    sql(`UPDATE users SET manager_id=NULL, sales_level=NULL, custom_permissions=NULL, status='ACTIVE' WHERE tenant_id=${q(this.tenantId)}`);
  }
  get token() { return this.users.owner.token; }
  get owner() { return this.users.owner; }
  save() { fs.writeFileSync(this.file, JSON.stringify(this.st, null, 1)); }
  /** Idempotent: creates (once) or resets the user, then applies extra columns (role, manager_id, sales_level, custom_permissions). */
  async addUser(role, first, extra = {}, key = null) {
    const k = key || first.toLowerCase();
    let rec = this.st.users[k];
    const { custom_permissions, ...rest } = extra;
    if (!rec) {
      const userId = crypto.randomUUID();
      const email = `${k}-${this.rid}@uitest.example`;
      sql(`INSERT INTO users (user_id, tenant_id, first_name, last_name, email, role, status, password_hash, auth_provider, email_verified, created_at) VALUES (${[userId, this.tenantId, first, k === 'owner' ? 'Admin' : 'Test', email, role, 'ACTIVE', this.hash, 'local', 1].map(q).join(',')}, NOW())`);
      rec = this.st.users[k] = { userId, email, first };
      this.save();
    }
    const sets = { role, ...rest }; if (custom_permissions !== undefined) sets.custom_permissions = custom_permissions === null ? null : JSON.stringify(custom_permissions);
    sql(`UPDATE users SET ${Object.entries(sets).map(([c, v]) => `${c}=${q(v)}`).join(',')} WHERE user_id=${q(rec.userId)}`);
    const u = { email: rec.email, userId: rec.userId, password: PASSWORD, role, name: `${rec.first} ${k === 'owner' ? 'Admin' : 'Test'}`, token: null, key: k, tenant: this };
    this.users[k] = u;
    u.token = this.cachedToken(u);
    return u;
  }
  cachedToken(u) { const c = readJson(path.join(STATE, `tok-${u.email}.json`)); return c && c.exp - 120000 > Date.now() ? c.token : null; }
  async tokenOf(u) {
    if (typeof u === 'string') u = this.users[u];
    if (!u.token) { await paceLogin(); u.token = await loginToken(u.email, u.password); fs.writeFileSync(path.join(STATE, `tok-${u.email}.json`), JSON.stringify({ token: u.token, exp: jwtExp(u.token) })); }
    return u.token;
  }
  async tk(name) { return this.tokenOf(this.users[name]); }
  async init() { await this.tokenOf(this.users.owner); return this; }
}

/** Real-login browser session for a user (cached cookies), with a problem watcher. */
export async function ui(browser, user, opts = {}) {
  const file = path.join(HERE, '..', '.auth', user.email.replace(/[^a-z0-9]/gi, '_') + '.json');
  if (!fs.existsSync(file)) await paceLogin();
  const { ctx, page } = await session(browser, { email: user.email, password: user.password }, opts);
  page.setDefaultTimeout(15000);
  const w = watch(page);
  const u = { ctx, page, w, user };
  page.on('dialog', d => { u.lastDialog = d.message(); d.accept().catch(() => {}); });
  return u;
}
export async function go(u, route, { wait = 600 } = {}) {
  await u.page.goto(`${WEB}/app/${route.replace(/^\//, '')}`);
  await u.page.waitForLoadState('networkidle').catch(() => {});
  await u.page.waitForTimeout(wait);
}
export async function settle(u, ms = 500) { await u.page.waitForLoadState('networkidle').catch(() => {}); await u.page.waitForTimeout(ms); }

// ---------- assertions ----------
export function ok(cond, msg) { if (!cond) throw new Error(msg || 'assertion failed'); return true; }
export function eq(a, b, msg) { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(`${msg || 'not equal'}: got ${JSON.stringify(a)} expected ${JSON.stringify(b)}`); }
export function near(a, b, msg, eps = 0.5) { if (Math.abs(a - b) > eps) throw new Error(`${msg || 'not near'}: got ${a} expected ${b}`); }
export const num = s => Number(String(s).replace(/[^0-9.\-]/g, ''));

// ---------- classification of failures (edited after triage; never changes pass/fail) ----------
let CLASS = {};
try { CLASS = JSON.parse(fs.readFileSync(path.join(HERE, 'classify.json'), 'utf8')); } catch { /* none yet */ }

/** Run one named UI test case. fn(ctx) may throw or return a string (failure). u = ui() object whose watcher is checked. */
const ONLY = process.env.ONLY ? new RegExp(process.env.ONLY) : null;
export async function tc(rec, id, title, fn, { u, allow = [], noBody = false } = {}) {
  if (ONLY && !ONLY.test(id)) return;
  try {
    if (u) { u.w.drain(); }
    const r = await fn();
    if (typeof r === 'string') throw new Error(r);
    if (u) {
      await u.page.waitForTimeout(150);
      const probs = u.w.drain(allow);
      if (probs.length) throw new Error(`watch: ${probs.slice(0, 3).join(' | ')}`);
      if (!noBody) { const bp = await u.w.bodyProblems(); if (bp.length) throw new Error(`body: ${bp.join(', ')}`); }
    }
    rec.record(id, title, true);
  } catch (e) {
    const c = CLASS[id];
    rec.record(id, title, false, (c ? `[${c.note || ''}] ` : '') + String(e.message).split('\n')[0], c ? c.category : 'unclassified');
    if (u) { await u.page.screenshot({ path: path.join(SHOTS, `${id}.png`), fullPage: false }).catch(() => {}); u.w.drain(); }
  }
}
export function skip(rec, id, title, why, category = 'not-implemented') { rec.record(id, title, null, why, category); }

// ---------- seed helpers ----------
export const STAGES = ['SUBSCRIBER', 'LEAD', 'MQL', 'SQL', 'OPPORTUNITY', 'CUSTOMER'];
export const STATUSES = ['NEW', 'OPEN', 'IN_PROGRESS', 'ATTEMPTED_TO_CONTACT', 'CONNECTED'];
export const COUNTRIES = ['US', 'UK', 'India', 'Germany', 'Canada'];

/** Create n contacts via API; returns [{prospect_id,email,first_name,...}] with deterministic attributes. */
export async function seedContacts(t, n = 60, { owners = [], prefix = 'Seed', companies = [] } = {}) {
  const out = [];
  for (let i = 0; i < n; i++) {
    const body = { email: `${prefix.toLowerCase()}${i}.${t.rid}@acme-${i % 8}.example`, first_name: `${prefix}${i}`, last_name: `Person${String(i).padStart(2, '0')}`,
      designation: i % 3 === 0 ? 'CTO' : i % 3 === 1 ? 'VP Sales' : 'Engineer', company_name: companies[i % Math.max(1, companies.length)] || `Company ${i % 8} ${t.rid}`,
      lifecycle_stage: STAGES[i % STAGES.length], lead_status: STATUSES[i % STATUSES.length], poc_country: COUNTRIES[i % COUNTRIES.length],
      tags: i % 4 === 0 ? ['hot', 'q4'] : i % 4 === 1 ? ['warm'] : [], phone: `+1415555${String(1000 + i)}` };
    if (owners.length) body.owner_id = owners[i % owners.length];
    const c = await must(t.token, 'POST', '/contacts', body);
    out.push({ ...body, ...c });
  }
  return out;
}
export function csvText(header, rows) {
  const esc = v => { const s = String(v ?? ''); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
  return [header, ...rows].map(r => r.map(esc).join(',')).join('\n') + '\n';
}
export function writeTmp(name, content) { const f = path.join(TMP, name); fs.writeFileSync(f, content); return f; }
export async function downloadVia(page, clickFn, name = 'dl') {
  const [dl] = await Promise.all([page.waitForEvent('download', { timeout: 20000 }), clickFn()]);
  const f = path.join(TMP, `${Date.now()}-${dl.suggestedFilename()}`);
  await dl.saveAs(f);
  return { file: f, name: dl.suggestedFilename(), buf: fs.readFileSync(f) };
}
export const dbn = (qy) => Number(sql(qy).split('\n').pop());

/** Run an area file standalone: node area.mjs */
export async function standalone(area, runFn) {
  const browser = await launch();
  const rec = new Recorder(`ui_crm ${area}`, path.join(HERE, 'out', area));
  try { await runFn(rec, browser); } catch (e) { rec.record(`UIC-${area.toUpperCase()}-CRASH`, 'area crashed', false, e.stack?.split('\n').slice(0, 3).join(' '), 'test-issue'); console.error(e); }
  await browser.close();
  const c = rec.write(); console.log(JSON.stringify(c));
}
export const isMain = (meta) => meta.url === `file://${process.argv[1]}`;
export const modalLoc = (page) => page.locator('div.fixed.inset-0').last();

/** HTML5 drag and drop by dispatching the drag events with one shared DataTransfer (works when source/target are not both in the viewport). */
export async function dnd(page, src, dst) {
  const dt = await page.evaluateHandle(() => new DataTransfer());
  await src.scrollIntoViewIfNeeded().catch(() => {});
  await src.dispatchEvent('dragstart', { dataTransfer: dt });
  await dst.dispatchEvent('dragenter', { dataTransfer: dt });
  await dst.dispatchEvent('dragover', { dataTransfer: dt });
  await dst.dispatchEvent('drop', { dataTransfer: dt });
  await src.dispatchEvent('dragend', { dataTransfer: dt });
}
/** Make every API call whose path matches `re` fail with status (default 500) on this page. */
export async function failApi(page, re, status = 500, method = null) {
  await page.route(u => u.pathname.includes('/api/') && re.test(u.pathname), r => (!method || r.request().method() === method)
    ? r.fulfill({ status, body: JSON.stringify({ detail: 'injected failure' }), contentType: 'application/json' }) : r.continue());
}

/** Write an .xlsx file (via python openpyxl) and return its path. */
export function xlsxFile(name, header, rows) {
  const f = path.join(TMP, name);
  const py = 'import sys,json,openpyxl\nd=json.load(sys.stdin)\nwb=openpyxl.Workbook();ws=wb.active\nws.append(d["header"])\n[ws.append(r) for r in d["rows"]]\nwb.save(d["file"])';
  execFileSync('python3', ['-I', '-c', py], { input: JSON.stringify({ header, rows, file: f }) });
  return f;
}
