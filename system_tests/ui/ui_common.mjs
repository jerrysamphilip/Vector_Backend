// Shared helpers for the Playwright functional UI suites.
//   import { Workspace, launch, session, Recorder, step } from '../ui/ui_common.mjs';
// - Workspace: isolated tenant created through /api/auth/register; addUser() adds role users (DB insert).
// - session(): logs a user in through the real login form ONCE and caches the cookies on disk (the API
//   rate-limits logins per IP: 60 / 5 min), later calls reuse them.
// - watch(page): collects console errors, page errors and failed API responses so a test can assert none occurred.
// - Recorder: writes results.json / results.md in the same shape as the Python suites.
import { chromium } from 'playwright';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

export const API = process.env.API_URL || 'http://localhost:8191';
export const WEB = process.env.WEB_URL || 'http://localhost:8190/vector';
const AUTH_DIR = path.join(path.dirname(new URL(import.meta.url).pathname), '..', '.auth');
fs.mkdirSync(AUTH_DIR, { recursive: true });

export function sql(q) {
  return execFileSync('docker', ['exec', process.env.MYSQL_CONTAINER || 'vector-mysql-1', 'mysql', '-uvector', '-pvector',
    process.env.MYSQL_DATABASE || 'outreach_ai', '-N', '-e', q], { encoding: 'utf8' }).trim();
}
const q = v => (v === null || v === undefined) ? 'NULL' : typeof v === 'number' ? String(v) : `'${String(v).replace(/'/g, "''")}'`;

export async function api(method, p, { token, body, headers = {} } = {}) {
  const h = { ...headers };
  if (token) h.Authorization = `Bearer ${token}`;
  if (body !== undefined) h['Content-Type'] = 'application/json';
  const r = await fetch(API + p, { method, headers: h, body: body === undefined ? undefined : JSON.stringify(body) });
  let data = null; const text = await r.text();
  try { data = JSON.parse(text); } catch { data = text; }
  return { status: r.status, data };
}

export async function loginToken(email, password) {
  const r = await api('POST', '/api/auth/login', { body: { email, password } });
  if (r.status !== 200 || !r.data.access_token) throw new Error(`login ${email}: ${r.status} ${JSON.stringify(r.data).slice(0, 200)}`);
  return r.data.access_token;
}

export class Workspace {
  /** await Workspace.create('label') */
  static async create(label) {
    const w = new Workspace();
    w.rid = crypto.randomBytes(3).toString('hex');
    w.email = `owner-${label}-${w.rid}@uitest.example`;
    w.password = `Uitest-${w.rid}-pw!`;
    const r = await api('POST', '/api/auth/register', { body: { first_name: 'Ui', last_name: label, email: w.email,
      password: w.password, tenant_name: `Ui ${label} ${w.rid}` } });
    if (r.status !== 201) throw new Error(`register: ${r.status} ${JSON.stringify(r.data)}`);
    w.token = r.data.access_token;
    w.tenantId = sql(`SELECT tenant_id FROM users WHERE email=${q(w.email)}`);
    w.userId = sql(`SELECT user_id FROM users WHERE email=${q(w.email)}`);
    w.users = { owner: { email: w.email, userId: w.userId, token: w.token } };
    return w;
  }
  /** Add a user to this tenant (same password as the owner). extra: users columns e.g. { manager_id, sales_level }. */
  async addUser(role, first, extra = {}) {
    const email = `${first.toLowerCase()}-${this.rid}@uitest.example`;
    const hash = sql(`SELECT password_hash FROM users WHERE email=${q(this.email)}`);
    const userId = crypto.randomUUID();
    const cols = { user_id: userId, tenant_id: this.tenantId, first_name: first, last_name: 'Test', email, role,
      status: 'ACTIVE', password_hash: hash, auth_provider: 'local', email_verified: 1, ...extra };
    sql(`INSERT INTO users (${Object.keys(cols).join(',')}, created_at) VALUES (${Object.values(cols).map(q).join(',')}, NOW())`);
    const u = { email, userId, token: await loginToken(email, this.password), password: this.password };
    this.users[first.toLowerCase()] = u;
    return u;
  }
  async setSenderIdentity() {
    return api('PUT', '/api/company-profiles/sender-identity', { token: this.token,
      body: { company_name: `Uitest Co ${this.rid}`, postal_address: '1 Test Street, Austin, TX 78701, USA' } });
  }
}

export async function launch() {
  return chromium.launch({ executablePath: process.env.CHROMIUM || '/opt/pw-browsers/chromium' });
}

/** A logged-in browser context (real login form once per user; cookies cached in system_tests/.auth). */
export async function session(browser, { email, password }, { viewport = { width: 1440, height: 900 } } = {}) {
  const file = path.join(AUTH_DIR, email.replace(/[^a-z0-9]/gi, '_') + '.json');
  if (fs.existsSync(file)) {
    const ctx = await browser.newContext({ viewport, storageState: file });
    const page = await ctx.newPage();
    await page.goto(WEB + '/app/home');
    await page.waitForLoadState('networkidle').catch(() => {});
    if (!page.url().includes('/login')) return { ctx, page };
    await ctx.close();
  }
  const ctx = await browser.newContext({ viewport });
  const page = await ctx.newPage();
  await page.goto(WEB + '/login');
  await page.fill('input[type=email]', email);
  await page.fill('input[type=password]', password);
  await page.click('button[type=submit]');
  await page.waitForURL(/\/(app|admin)\//, { timeout: 20000 });
  await ctx.storageState({ path: file });
  return { ctx, page };
}

/** Collect problems on a page: console errors, uncaught exceptions, failed app API calls, error-boundary text. */
export function watch(page) {
  const problems = [];
  page.on('pageerror', e => problems.push(`pageerror: ${e.message}`));
  page.on('console', m => { if (m.type() === 'error' && !/favicon|Failed to load resource: net::ERR_/.test(m.text())) problems.push(`console: ${m.text().slice(0, 200)}`); });
  page.on('response', r => {
    const u = r.url();
    if ((u.includes('/api/') || u.startsWith(API)) && r.status() >= 400 && !u.includes('/auth/session') && !u.includes('/auth/refresh'))
      problems.push(`http ${r.status()} ${r.request().method()} ${u.replace(/^https?:\/\/[^/]+/, '')}`);
  });
  return {
    problems,
    /** Return and clear problems; pass allow=[regex] for expected ones (e.g. a deliberate 4xx). */
    drain(allow = []) { const out = problems.splice(0).filter(p => !allow.some(a => a.test(p))); return out; },
    async bodyProblems() {
      const t = (await page.locator('body').innerText().catch(() => '')) || '';
      const bad = [];
      if (/\[object Object\]/.test(t)) bad.push('shows "[object Object]"');
      if (/Something went wrong on this screen|Application Error/.test(t)) bad.push('error boundary visible');
      if (/\bundefined\b|\bNaN\b/.test(t.replace(/Undefined behaviou?r/i, ''))) bad.push('shows undefined/NaN text');
      return bad;
    },
  };
}

export class Recorder {
  constructor(suite, outDir) { this.suite = suite; this.outDir = outDir; this.cases = []; this.started = Date.now(); }
  record(id, title, passed, detail = '', category = '') {
    const status = passed === true ? 'PASS' : passed === null ? 'BLOCKED' : 'FAIL';
    this.cases.push({ id, title, status, category: category || (passed === true ? '' : passed === null ? 'blocked' : 'defect'), detail: String(detail).slice(0, 500) });
    console.log(`${status.padEnd(7)} ${id} ${title}${passed === true ? '' : '  -> ' + String(detail).slice(0, 200)}`);
  }
  check(id, title, cond, detail = '', category = '') { this.record(id, title, !!cond, cond ? '' : detail, category); return !!cond; }
  write() {
    fs.mkdirSync(this.outDir, { recursive: true });
    const counts = { PASS: 0, FAIL: 0, BLOCKED: 0 }; this.cases.forEach(c => counts[c.status]++);
    fs.writeFileSync(path.join(this.outDir, 'results.json'), JSON.stringify({ suite: this.suite, counts, seconds: Math.round((Date.now() - this.started) / 1000), cases: this.cases }, null, 2));
    const lines = [`# ${this.suite} results`, '', `PASS ${counts.PASS} · FAIL ${counts.FAIL} · BLOCKED ${counts.BLOCKED}`, '',
      '| ID | Test case | Status | Category | Detail |', '|---|---|---|---|---|',
      ...this.cases.map(c => `| ${c.id} | ${c.title} | ${c.status} | ${c.category} | ${c.detail.replace(/\|/g, '\\|').replace(/\n/g, ' ')} |`)];
    fs.writeFileSync(path.join(this.outDir, 'results.md'), lines.join('\n') + '\n');
    return counts;
  }
}

/** Run one named step; a thrown error (timeout, missing element...) becomes a FAIL with the message. */
export async function step(rec, id, title, fn, shot) {
  try { const r = await fn(); if (r === undefined || r === true) rec.record(id, title, true); else if (r === null) rec.record(id, title, null, 'blocked'); else rec.record(id, title, false, r); }
  catch (e) { rec.record(id, title, false, String(e.message).split('\n')[0]); if (shot) await shot(id).catch(() => {}); }
}
