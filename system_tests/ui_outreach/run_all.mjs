// Runs every UI outreach/admin suite and merges results into results.json / results.md.
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { caseFilter } from '../ui/ui_common.mjs';
const HERE = path.dirname(new URL(import.meta.url).pathname);
const SUITES = ['auth', 'shell', 'security', 'campaigns_list', 'wizard', 'details', 'library', 'prospects', 'mailboxes', 'inboxdomain', 'admin'];
// Case-id prefixes per sub-suite: with a tier filter (TEST_CASES) a sub-suite none of whose ids can match is skipped.
const PREFIXES = { auth: ['UIO-AUTH-'], shell: ['UIO-SHELL-'], security: ['UIO-SEC-'], campaigns_list: ['UIO-CAMP-'], wizard: ['UIO-WIZ-'],
  details: ['UIO-DET-'], library: ['UIO-TPL-', 'UIO-AUTO-', 'UIO-AIGEN-'], prospects: ['UIO-PROS-'], mailboxes: ['UIO-MBX-'],
  inboxdomain: ['UIO-INBOX-', 'UIO-DOM-'], admin: ['UIO-TEAM-', 'UIO-REP-'] };
const only = process.argv.slice(2);
const classify = fs.existsSync(path.join(HERE, 'classify.json')) ? JSON.parse(fs.readFileSync(path.join(HERE, 'classify.json'), 'utf8')) : {};
const cases = [];
const ran = new Set();
for (const s of SUITES) {
  if (only.length && !only.includes(s)) continue;
  if (!PREFIXES[s].some(p => caseFilter.wantsPrefix(p))) { console.log(`\n=== ${s} (skipped: no case selected by TEST_CASES) ===`); continue; }
  console.log(`\n=== ${s} ===`);
  ran.add(s);
  spawnSync('node', [path.join(HERE, s + '.mjs')], { stdio: ['ignore', 'inherit', 'ignore'], cwd: HERE });
}
for (const s of SUITES) {
  const f = path.join(HERE, 'out', s, 'results.json'); if (!fs.existsSync(f)) continue;
  for (const c of JSON.parse(fs.readFileSync(f, 'utf8')).cases) {
    const k = classify[c.id];
    const carried = caseFilter.active && !ran.has(s) ? { carried_over: true } : {};
    cases.push({ ...c, ...carried, suite: s, category: c.status === 'PASS' ? '' : (k?.category || 'unclassified'), note: k?.note || '' });
  }
}
const counts = { PASS: 0, FAIL: 0, BLOCKED: 0 }; cases.filter(c => !c.carried_over).forEach(c => counts[c.status]++);
const byCat = {}; cases.filter(c => c.status !== 'PASS').forEach(c => byCat[c.category] = (byCat[c.category] || 0) + 1);
fs.writeFileSync(path.join(HERE, 'results.json'), JSON.stringify({ suite: 'UI outreach/admin', counts, failuresByCategory: byCat, cases }, null, 2));
const md = [`# UI outreach/admin functional results`, '', `PASS ${counts.PASS} | FAIL ${counts.FAIL} | BLOCKED ${counts.BLOCKED}`, '',
  `Failures by category: ${JSON.stringify(byCat)}`, '', '| ID | Test case | Status | Category | Detail |', '|---|---|---|---|---|',
  ...cases.map(c => `| ${c.id} | ${c.title.replace(/\|/g, '/')} | ${c.status} | ${c.category} | ${(c.note || c.detail).replace(/\|/g, '/').replace(/\n/g, ' ').slice(0, 220)} |`)];
fs.writeFileSync(path.join(HERE, 'results.md'), md.join('\n') + '\n');
console.log('\nTOTAL', JSON.stringify(counts), JSON.stringify(byCat));
