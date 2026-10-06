// Browser check for JSON states (API_CONTRACT rule 7, amended 2026-10-06): the
// state shows as the canonical rendering, and a drag from a key to its value
// makes one span over both, stored as offsets into the rendering.
//   SINGLE_USER=1 server on BASE with one open batch holding a JSON item; then
//   node tests/e2e/json_span_browser.js BASE SCREENSHOT.png
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const [BASE, SHOT] = process.argv.slice(2);
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1366, height: 768 } });
  const errors = [];
  p.on('pageerror', e => errors.push(e.message));
  await p.goto(BASE + '/');
  await p.waitForSelector('#label-main:not(.hidden)');
  const shown = await p.innerText('#state-view');
  const ok = [];
  const check = (name, cond, d) => { ok.push(!!cond); console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}`, d ?? ''); };
  check('JSON state shows as key: value lines', /trace_summary:\n\s+constraint_violations: 1/.test(shown));
  check('keys are styled', (await p.$$('#state-view .tok.json-key')).length > 5);
  // Drag from the key "constraint_violations" to the value "1"
  const key = p.locator('#state-view .tok.w', { hasText: /^constraint_violations$/ });
  const toks = p.locator('#state-view .tok.w');
  const n = await toks.count();
  let valueIdx = -1;
  for (let i = 0; i < n; i++) {
    if ((await toks.nth(i).innerText()) === 'constraint_violations') { valueIdx = i + 1; break; }
  }
  const kb = await key.boundingBox(), vb = await toks.nth(valueIdx).boundingBox();
  await p.mouse.move(kb.x + 2, kb.y + kb.height / 2);
  await p.mouse.down();
  await p.mouse.move(vb.x + vb.width - 1, vb.y + vb.height / 2, { steps: 5 });
  await p.mouse.up();
  const sel = await p.evaluate(() => L.selection && { text: L.selection.text, start: L.selection.start, end: L.selection.end });
  check('drag key -> value selects both', sel && sel.text === 'constraint_violations: 1', sel);
  await p.keyboard.press('s');                       // support
  await p.keyboard.press('t');                       // option "true"
  const spans = await p.evaluate(() => L.spans.map(s => ({ text: s.text, pointer: s.pointer, option: s.option })));
  check('span added on option true', spans.length === 1 && spans[0].option === 'true' && spans[0].pointer === null, spans);
  await p.keyboard.press(' ');                       // answerable
  await p.screenshot({ path: SHOT });
  await p.keyboard.press('Enter');
  await p.waitForTimeout(1200);
  check('saved (no banner error)', !(await p.innerText('#mode-banner').catch(() => '')).trim(),
        await p.innerText('#mode-banner').catch(() => ''));
  check('no JS errors', errors.length === 0, errors);
  await b.close();
  process.exit(ok.every(Boolean) ? 0 : 1);
})().catch(e => { console.error(e); process.exit(2); });
