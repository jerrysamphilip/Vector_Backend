// In-app confirm dialogs for the UI suites (Vector_Frontend src/components/ui/ConfirmDialog.jsx, F-09).
//
// Since frontend b3f05f5 (2026-10-28) the app no longer calls window.confirm / window.alert: confirmations are a
// Radix-based ConfirmDialog (role="dialog", aria-modal, titled by an element whose id starts with "confirm-desc-"),
// with Cancel (data-autofocus) and the confirm button last. Tests written for native dialogs answered them with
// page.on('dialog'); onConfirm() gives them the same contract for the in-app dialog:
//
//   const stop = onConfirm(page, text => { log.push(text); return 'accept'; });   // 'accept' | 'dismiss' | null (leave it open)
//   ...; stop();
//
// The newest registered handler answers. The text is "<title>\n\n<message>", the same text ConfirmDialog passes
// to window.confirm when it has no provider. A MutationObserver in the page reports each new confirm dialog once.

const SCRIPT = `(() => {
  if (window.__uiConfirmObserver) return;
  window.__uiConfirmObserver = true;
  const SEL = '[role="dialog"][aria-labelledby^="confirm-desc-"]';
  const seen = new WeakSet();
  const answer = async d => {
    const title = (d.querySelector('h2') || {}).innerText || '';
    const message = (d.querySelector('p') || {}).innerText || '';
    let mode = null;
    try { mode = await window.__uiConfirm([title, message].filter(Boolean).join('\\n\\n')); } catch (e) { mode = null; }
    if (!mode || !d.isConnected) return;
    const buttons = d.querySelectorAll('button');
    const button = mode === 'dismiss' ? (d.querySelector('button[data-autofocus]') || buttons[0]) : buttons[buttons.length - 1];
    if (button) button.click();
  };
  const scan = () => {
    for (const d of document.querySelectorAll(SEL)) {
      if (seen.has(d)) continue;
      seen.add(d);
      answer(d);
    }
  };
  const start = () => { new MutationObserver(scan).observe(document.documentElement, { childList: true, subtree: true }); scan(); };
  if (document.documentElement) start(); else document.addEventListener('DOMContentLoaded', start);
})();`;

const registry = new WeakMap();

async function install(page) {
  let entry = registry.get(page);
  if (entry) return entry;
  entry = { handlers: [], ready: null };
  registry.set(page, entry);
  entry.ready = (async () => {
    await page.exposeFunction('__uiConfirm', text => {
      const top = entry.handlers[entry.handlers.length - 1];
      return top ? top(text) ?? null : null;
    });
    await page.addInitScript(SCRIPT);
    await page.evaluate(SCRIPT).catch(() => {});   // the document already open (about:blank before the first goto)
  })();
  return entry;
}

/** Answer the app's confirm dialogs with handler(text) -> 'accept' | 'dismiss' | null. Returns stop(). */
export function onConfirm(page, handler) {
  const entryPromise = install(page);
  let stopped = false;
  let entryRef = null;
  const ready = entryPromise.then(entry => { entryRef = entry; if (!stopped) entry.handlers.push(handler); return entry.ready; });
  const stop = () => {
    stopped = true;
    if (entryRef) entryRef.handlers = entryRef.handlers.filter(h => h !== handler);
  };
  stop.ready = ready;
  return stop;
}

/** Locator of the open in-app confirm dialog (for tests that answer it themselves). */
export const confirmDialog = page => page.locator('[role="dialog"][aria-labelledby^="confirm-desc-"]');
