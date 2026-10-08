// Browser check for annotator mode (docs/e13/ANNOTATOR.md): Dataset tab, the
// numbered workspace, typed and recorded utterances through the (fake) engines
// to a proposal, review, manual versions, notes and navigation.
//   node tests/e2e/annotator_browser.js BASE SHOTS_DIR   (run_annotator_browser.sh sets everything up)
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const [BASE, SHOTS] = process.argv.slice(2);

(async () => {
  const b = await chromium.launch({ args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'] });
  const ctx = await b.newContext({ viewport: { width: 1440, height: 1000 } });
  await ctx.grantPermissions(['microphone'], { origin: BASE });
  const p = await ctx.newPage();
  const errors = [];
  p.on('pageerror', e => errors.push(e.message));
  const ok = [];
  const check = (name, cond, d) => { ok.push(!!cond); console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}`, d === undefined ? '' : JSON.stringify(d)); };
  const st = () => p.evaluate(() => ({
    item: L && L.item.item_id, annot: !!(L && L.annot), clauses: L ? L.clauses.map(c => `${c.stance}:${c.text}`) : [],
    label: L && typeof finalLabel === 'function' && L.clauses ? finalLabel() : null,
    proposal: A.ws && A.ws.proposal ? A.ws.proposal.id : null, utts: A.ws ? A.ws.utterances.length : 0 }));

  await p.goto(BASE + '/');
  await p.waitForSelector('#label-empty:not(.hidden)');   // the queue has nothing: the batch is curated
  check('curated batch is not in the queue', /No items/.test(await p.innerText('#label-empty-text')));

  await p.click('.nav-tab[data-tab="dataset"]');
  await p.waitForSelector('.dataset-row');
  check('dataset lists the batch items', (await p.$$('.dataset-row')).length === 3);
  await p.click('.dataset-row >> nth=0');
  await p.waitForSelector('#annot-panel:not(.hidden)');
  let s = await st();
  check('workspace opened in the label screen', s.annot && s.item === 'pair0#clauses', s);
  const numbered = await p.$$eval('#hypothesis-view .tok[data-n]', ts => ts.map(t => t.dataset.n + ':' + t.textContent));
  check('hypothesis words carry node numbers 12..', numbered[0] === '12:The' && numbered.includes('15:walking'), numbered);
  check('trees list both texts', (await p.$$('.tree-node')).length === 19);

  // Shift-click a tree line selects the word; 2 makes it a contradicted clause
  await p.click('.tree-node:has(.node-n:text-is("15"))', { modifiers: ['Shift'] });
  s = await p.evaluate(() => L.selection && L.selection.text);
  check('tree click selects the word', s === 'walking', s);

  // Typed utterance -> agent (fake) -> proposal
  await p.fill('#annot-text', '13 is supported by 2. 15 contradicts 4. 16 is undetermined, 5 suggests it.');
  await p.focus('#annot-text');
  await p.keyboard.press('Control+Enter');
  await p.waitForFunction(() => A.ws && A.ws.proposal, null, { timeout: 15000 }).catch(() => {});
  s = await st();
  check('typed utterance became a proposal', s.proposal && s.utts === 1, s);
  check('proposal shows clauses, relation, note and question',
        /walking/.test(await p.innerText('#annot-proposal')) && /referent/.test(await p.innerText('#annot-proposal'))
        && /one clause\?/.test(await p.innerText('#annot-proposal')));
  await p.screenshot({ path: `${SHOTS}/annotator_proposal.png` });

  await p.click('#state-view');                              // keys go to the label screen
  await p.keyboard.press('Escape');
  await p.keyboard.press('y');                               // accept
  await p.waitForFunction(() => A.ws && A.ws.annotation, null, { timeout: 5000 }).catch(() => {});
  s = await st();
  check('accepted as version 1', s.label === 'contradiction' && s.clauses.length === 3, s);
  check('note and relation kept', /standing and walking/.test(await p.innerText('#annot-notes')));

  // Manual edit: re-tag the active clause (the first) and save version 2
  await p.keyboard.press('3');
  await p.keyboard.press('Enter');
  await p.waitForFunction(() => A.ws && A.ws.annotation && A.ws.annotation.version === 2, null, { timeout: 5000 }).catch(() => {});
  check('manual edit saved as version 2', await p.evaluate(() => A.ws.annotation.version) === 2);

  // Recording (Chromium's fake microphone) -> transcription (fake) -> new proposal
  await p.keyboard.press('v');
  await p.waitForTimeout(1500);
  await p.keyboard.press('v');
  await p.waitForFunction(() => A.ws && A.ws.utterances.some(u => u.audio && u.text) && A.ws.proposal, null,
                          { timeout: 20000 }).catch(() => {});
  s = await p.evaluate(() => A.ws.utterances.map(u => ({ src: u.source, text: u.text, ms: u.duration_ms, kind: u.kind })));
  check('recording uploaded, transcribed and proposed', s.length === 2 && s[1].src === 'audio' && s[1].text
        && s[1].ms > 1000, s);
  check('audio plays back', await p.$$eval('#annot-utterances audio', a => a.length) === 1);
  await p.click('text=reject');
  await p.waitForFunction(() => A.ws && !A.ws.proposal, null, { timeout: 5000 }).catch(() => {});
  check('proposal rejected', await p.evaluate(() => !A.ws.proposal));

  // Typed note attached to the selected words
  await p.click('.tree-node:has(.node-n:text-is("18"))', { modifiers: ['Shift'] });
  await p.fill('#annot-note-text', 'a room is more specific than a doorway');
  await p.press('#annot-note-text', 'Enter');
  await p.waitForFunction(() => A.ws.notes.length === 2, null, { timeout: 5000 }).catch(() => {});
  check('typed note added on node 18', await p.evaluate(() => A.ws.notes.some(n => n.source === 'typed' && n.nodes.includes(18))));

  // Navigation and the list
  await p.click('#state-view');
  await p.keyboard.press('.');
  await p.waitForFunction(() => L && L.item.item_id === 'pair1#clauses', null, { timeout: 5000 }).catch(() => {});
  check('. opens the next item', (await st()).item === 'pair1#clauses');
  await p.click('.nav-tab[data-tab="dataset"]');
  await p.waitForFunction(() => A.items.length && A.items[0].versions === 2
                          && /v2/.test(document.querySelector('.dataset-row').innerText), null, { timeout: 5000 })
         .catch(() => {});
  const row0 = await p.innerText('.dataset-row >> nth=0');
  check('list shows label, versions and history', /neutral|contradiction/.test(row0) && /v2/.test(row0) && /🗣2/.test(row0), row0);
  await p.screenshot({ path: `${SHOTS}/annotator_dataset.png` });
  await p.click('.nav-tab[data-tab="label"]');
  await p.waitForTimeout(500);
  check('Label tab returns to the queue', !(await st()).annot);
  check('no JS errors', errors.length === 0, errors);
  await b.close();
  process.exit(ok.every(Boolean) ? 0 : 1);
})().catch(e => { console.error(e); process.exit(2); });
