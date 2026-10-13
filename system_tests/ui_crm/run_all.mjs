// Runs every UI CRM/Sales area, writes results.json/results.md and SCREEN_COVERAGE.md input
import { launch, Recorder, HERE } from './crm_common.mjs';
import path from 'node:path';
const areas = (process.env.AREAS || 'contacts,contact_detail,accounts,lists_import,tasks_trash,sales,new_features').split(',');
const browser = await launch();
const rec = new Recorder('ui_crm (CRM + Sales functional UI)', HERE);
for (const a of areas) {
  try { const m = await import(`./${a}.mjs`); console.log(`== ${a}`); await m.run(rec, browser); }
  catch (e) { rec.record(`UIC-${a.toUpperCase()}-CRASH`, `area ${a} crashed`, false, String(e.message).slice(0, 300), 'test-issue'); }
}
await browser.close();
console.log(JSON.stringify(rec.write()));
