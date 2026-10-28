// Runs every UI CRM/Sales area, writes results.json/results.md and SCREEN_COVERAGE.md input
import { launch, Recorder, HERE, caseFilter } from './crm_common.mjs';
import path from 'node:path';
// Case-id prefixes per area: with a tier filter (TEST_CASES) an area none of whose ids can match is skipped.
const PREFIXES = { contacts: ['UIC-CON-'], contact_detail: ['UIC-CD-'], accounts: ['UIC-ACC-'], lists_import: ['UIC-LST-', 'UIC-IMP-'],
  tasks_trash: ['UIC-TSK-', 'UIC-RD-'], sales: ['UIC-SAL-'], new_features: ['UIC-NF-'] };
const areas = (process.env.AREAS || 'contacts,contact_detail,accounts,lists_import,tasks_trash,sales,new_features').split(',')
  .filter(a => !PREFIXES[a] || PREFIXES[a].some(p => caseFilter.wantsPrefix(p)));
if (caseFilter.active) console.log('areas selected by TEST_CASES:', areas.join(','));
const browser = await launch();
const rec = new Recorder('ui_crm (CRM + Sales functional UI)', HERE);
for (const a of areas) {
  try { const m = await import(`./${a}.mjs`); console.log(`== ${a}`); await m.run(rec, browser); }
  catch (e) { rec.record(`UIC-${a.toUpperCase()}-CRASH`, `area ${a} crashed`, false, String(e.message).slice(0, 300), 'test-issue'); }
}
await browser.close();
console.log(JSON.stringify(rec.write()));
