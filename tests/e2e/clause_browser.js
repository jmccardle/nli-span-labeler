// Browser check for the clauses task (docs/e13/CLAUSE_TASK.md): clauses on the
// hypothesis by mouse and keyboard, premise evidence, the derived label, the
// neutral completion sentences, undo, save, and edit from history.
//   node tests/e2e/clause_browser.js BASE SHOTS_DIR      (run_clause_browser.sh sets up the server)
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const [BASE, SHOTS] = process.argv.slice(2);

(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1366, height: 860 } });
  const errors = [];
  p.on('pageerror', e => errors.push(e.message));
  const ok = [];
  const check = (name, cond, d) => { ok.push(!!cond); console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}`, d === undefined ? '' : JSON.stringify(d)); };

  async function drag(view, from, to = from) {
    const toks = p.locator(`${view} .tok.w`);
    const texts = await toks.allInnerTexts();
    const i = texts.indexOf(from), j = texts.indexOf(to, i);
    const a = await toks.nth(i).boundingBox(), z = await toks.nth(j).boundingBox();
    await p.mouse.move(a.x + 1, a.y + a.height / 2);
    await p.mouse.down();
    await p.mouse.move(z.x + z.width - 1, z.y + z.height / 2, { steps: 4 });
    await p.mouse.up();
  }
  const state = () => p.evaluate(() => ({
    clauses: L.clauses.map(c => ({ text: c.text, stance: c.stance, omission: c.omission, ev: c.evidence.map(e => e.text) })),
    active: L.activeClause, label: finalLabel(), region: L.region, item: L.item.item_id,
  }));

  await p.goto(BASE + '/');
  await p.waitForSelector('#label-main:not(.hidden)');
  check('clause view shown, reasons hidden',
        await p.isVisible('#hypothesis-block') && await p.isVisible('#clause-pane') && !(await p.isVisible('#reasons-pane'))
        && !(await p.isVisible('#options-view')) && !(await p.isVisible('#span-block')));
  check('starts in the hypothesis region', (await state()).region === 'hypothesis');
  const first = (await state()).item;

  // Mouse: "dog" supported, evidence "dog"
  await drag('#hypothesis-view', 'dog');
  await p.keyboard.press('1');
  await drag('#state-view', 'dog');
  await p.keyboard.press('a');
  let s = await state();
  check('mouse: clause + evidence', s.clauses.length === 1 && s.clauses[0].stance === 'supported'
        && s.clauses[0].ev[0] === 'dog' && s.label === 'entailment', s);
  check('premise evidence painted', (await p.$$('#state-view .tok.ev-active')).length === 1);
  check('hypothesis clause painted', (await p.$$('#hypothesis-view .tok.cl-supported')).length === 1);

  // Keyboard: Tab to the hypothesis, select "looking up" with l / L, tag 1; Tab to the premise, select "looks up", a
  await p.keyboard.press('Tab');
  for (let i = 0; i < 5; i++) await p.keyboard.press('l');   // A brown dog is looking
  await p.keyboard.press('L');                               // + up
  await p.keyboard.press('s');
  await p.keyboard.press('Tab');
  for (let i = 0; i < 3; i++) await p.keyboard.press('l');   // A dog looks
  await p.keyboard.press('L');
  await p.keyboard.press('a');
  s = await state();
  check('keyboard: clause + evidence', s.clauses.length === 2 && s.clauses[1].text === 'looking up'
        && s.clauses[1].ev[0] === 'looks up', s);

  // "brown" undetermined on "dog"; "a man" unaddressed -> neutral
  await drag('#hypothesis-view', 'brown');
  await p.keyboard.press('3');
  await drag('#state-view', 'dog');
  await p.keyboard.press('a');
  await drag('#hypothesis-view', 'a', 'man');
  await p.keyboard.press('4');
  s = await state();
  check('clauses kept in hypothesis order', s.clauses.map(c => c.text).join('|') === 'brown|dog|looking up|a man', s);
  check('derived label neutral', s.label === 'neutral' && (await p.innerText('#clause-derived')) === 'neutral');
  check('completion inputs shown for neutral', await p.isVisible('#completion-block'));

  // Re-tag the active clause with no selection; undo it
  await p.keyboard.press('2');
  check('re-tag active clause -> contradiction', (await state()).label === 'contradiction');
  check('completion hidden when not neutral', !(await p.isVisible('#completion-block')));
  await p.keyboard.press('z');
  check('undo restores', (await state()).clauses[3].stance === 'unaddressed');

  // Overlap is refused
  await drag('#hypothesis-view', 'brown', 'dog');
  await p.keyboard.press('1');
  check('overlap refused', (await state()).clauses.length === 4 && /overlaps/.test(await p.innerText('#mode-banner')));
  await p.keyboard.press('Escape');

  await p.keyboard.press('i');
  await p.keyboard.type('The owner is a man.');
  await p.keyboard.press('Escape');
  await p.screenshot({ path: `${SHOTS}/clauses_neutral.png` });
  await p.keyboard.press('Enter');
  await p.waitForFunction(id => L && L.item.item_id !== id, first, { timeout: 5000 }).catch(() => {});
  s = await state();
  check('saved, next item served', s.item !== first && s.clauses.length === 0, s);

  // Second item: contradicted by omission
  await drag('#hypothesis-view', 'a', 'man');
  await p.keyboard.press('c');
  await drag('#state-view', 'its', 'owner');
  await p.keyboard.press('a');
  await p.keyboard.press('o');
  s = await state();
  check('omission on a contradicted clause', s.clauses[0].omission && s.label === 'contradiction', s);
  await p.keyboard.press('Enter');
  await p.waitForFunction(id => L && L.item.item_id !== id, s.item, { timeout: 5000 }).catch(() => {});

  // Edit the first item from history
  await p.keyboard.press('e');
  await p.waitForSelector('#history-modal:not(.hidden)');
  const rows = await p.locator('#history-list .history-row').allInnerTexts();
  check('history shows labels', rows.some(r => r.includes('neutral')) && rows.some(r => r.includes('contradiction')), rows);
  await p.locator('#history-list .history-row', { hasText: 'neutral' }).click();
  await p.waitForFunction(() => L && L.edit);
  s = await state();
  check('edit restores clauses and completion', s.clauses.length === 4 && s.clauses[1].ev[0] === 'dog'
        && (await p.inputValue('#completion-entail')) === 'The owner is a man.', s);
  await p.selectOption('#clause-override', 'entailment');
  await p.keyboard.press('Enter');
  await p.waitForTimeout(600);
  check('override without a note is refused', /needs a note/.test(await p.innerText('#mode-banner')),
        await p.innerText('#mode-banner'));
  await p.fill('#note', 'testing the override');
  await p.focus('#note');
  await p.keyboard.press('Control+Enter');
  await p.waitForFunction(() => L && !L.edit, null, { timeout: 5000 }).catch(() => {});
  check('edit saved', !(await p.evaluate(() => L && L.edit)));
  await p.screenshot({ path: `${SHOTS}/clauses_after.png` });
  check('no JS errors', errors.length === 0, errors);
  await b.close();
  process.exit(ok.every(Boolean) ? 0 : 1);
})().catch(e => { console.error(e); process.exit(2); });
