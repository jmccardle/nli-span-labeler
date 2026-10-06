// Browser walkthrough of the multi-labeler path (not run by pytest):
// owner logs in -> Admin -> invite link -> a new browser opens the link ->
// registers -> contributor agreement -> guideline -> quiz with feedback ->
// labels items by keyboard -> owner sees the labeler and agreement on Admin.
//
//   tests/e2e/run_onboarding_browser.sh       # sets up a throwaway instance and runs this
//
// Args: BASE OWNER_LOGIN OWNER_PASSWORD SCREENSHOT_DIR
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const [BASE, OWNER, OWNER_PW, SHOTS] = process.argv.slice(2);
const results = [];
function check(name, ok, detail) {
  results.push([name, !!ok]);
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${name}${detail !== undefined ? '  (' + JSON.stringify(detail) + ')' : ''}`);
}
const REASON_KEYS = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '0'];

(async () => {
  const browser = await chromium.launch(process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : {});
  const errors = [];
  const watch = (page, who) => {
    page.on('pageerror', e => errors.push(`${who}: ${e.message}`));
    page.on('console', m => {
      // 4xx answers the UI handles (e.g. 404 "no quiz started" probe) are logged by Chromium as errors; keep them apart
      if (m.type() === 'error' && !/status of 4\d\d/.test(m.text())) errors.push(`${who}: ${m.text()}`);
    });
  };

  // --- owner: login + invite --------------------------------------------------
  const ownerCtx = await browser.newContext({ viewport: { width: 1366, height: 768 } });
  const op = await ownerCtx.newPage(); watch(op, 'owner');
  await op.goto(BASE + '/');
  await op.waitForSelector('#login-form:not(.hidden)');
  await op.fill('#login-name', OWNER);
  await op.fill('#login-password', 'wrong-password');
  await op.click('#login-form button[type=submit], #login-form button');
  await op.waitForTimeout(500);
  check('wrong password shows an error', (await op.textContent('#login-error')).trim().length > 0,
        (await op.textContent('#login-error')).trim());
  await op.fill('#login-password', OWNER_PW);
  await op.click('#login-form button[type=submit], #login-form button');
  await op.waitForSelector('#auth-modal', { state: 'hidden' });
  check('owner logged in (header shows the pseudonym)', /L01/.test(await op.textContent('#username-display')));
  await op.click('text=Admin');
  await op.waitForSelector('#admin-tab:not(.hidden)');
  await op.selectOption('#invite-clearance', 'public');
  await op.click('button:has-text("Invite")');
  await op.waitForFunction(() => /invite=/.test(document.getElementById('invite-result').textContent));
  const inviteText = await op.textContent('#invite-result');
  const link = inviteText.match(/(https?:\/\/\S+|\/\?invite=\S+)/)[0];
  check('invite link shown once', /invite=/.test(link), link.replace(/invite=.*/, 'invite=<token>'));
  await op.screenshot({ path: `${SHOTS}/01_owner_admin_invite.png` });

  // --- new labeler ---------------------------------------------------------------
  const ctx = await browser.newContext({ viewport: { width: 1366, height: 768 } });
  const p = await ctx.newPage(); watch(p, 'labeler');
  await p.goto(link.startsWith('http') ? link : BASE + link);
  await p.waitForSelector('#register-form:not(.hidden)');
  await p.fill('#register-name', 'dana');
  await p.fill('#register-password', 'dana-password-123');
  await p.screenshot({ path: `${SHOTS}/02_register.png` });
  await p.click('#register-form button');
  await p.waitForSelector('#agreement-modal:not(.hidden)');
  check('contributor agreement shown after registering',
        (await p.textContent('#agreement-text')).includes('Contributor agreement'));
  await p.screenshot({ path: `${SHOTS}/03_agreement.png` });
  await p.click('#agreement-accept');
  await p.waitForSelector('#guideline-modal:not(.hidden)');
  const gl = await p.textContent('#guideline-body');
  check('guideline shown with every reason', ['unrelated', 'conflicting_evidence', 'stale_state', 'subjective']
        .every(r => gl.includes(r)));
  await p.screenshot({ path: `${SHOTS}/04_guideline.png` });
  await p.click('#guideline-start');

  // Quiz: answer from the owner's gold list (the test knows it; a labeler wouldn't).
  const gold = await op.evaluate(async () => (await (await fetch('/api/admin/gold')).json()));
  const goldList = gold.gold || gold;
  const goldBy = Object.fromEntries(goldList.map(g => [g.item_id, g]));
  let answered = 0, shotFeedback = false;
  for (let i = 0; i < 20; i++) {
    await p.waitForFunction(() => /Quiz question|Passed|Not passed/.test(document.body.innerText), null, { timeout: 10000 });
    if (/Passed \(|Not passed/.test(await p.innerText('body'))) break;
    const q = await p.evaluate(() => L.quiz && L.quiz.item && L.quiz.item.item_id);
    const g = goldBy[q];
    const reasonSet = await p.evaluate(() => L.item.reason_set);
    // Reasons with hard span rules need spans the keyboard test doesn't draw: answer those as answerable (a miss).
    const hard = g.reasons.some(r => r === 'conflicting_evidence' || r === 'stale_state');
    if (g.answerable || hard) await p.keyboard.press(' ');
    else for (const r of g.reasons) await p.keyboard.press(REASON_KEYS[reasonSet.indexOf(r)]);
    await p.keyboard.press('Enter');
    await p.waitForSelector('#quiz-feedback:not(.hidden)');
    if (!shotFeedback) { await p.screenshot({ path: `${SHOTS}/05_quiz_feedback.png` }); shotFeedback = true; }
    answered++;
    await p.keyboard.press('Enter');
  }
  await p.waitForFunction(() => /Passed \(|Not passed/.test(document.body.innerText), null, { timeout: 10000 });
  const verdict = (await p.innerText('#label-empty-text').catch(() => '')) || (await p.innerText('body'));
  check('quiz finished and passed in the browser', /Passed \(/.test(verdict), `${answered} answers: ${verdict.slice(0, 80)}`);
  await p.screenshot({ path: `${SHOTS}/06_quiz_result.png` });

  // Labelling by keyboard
  await p.waitForSelector('#label-main:not(.hidden)', { timeout: 10000 });
  let saved = 0;
  for (let i = 0; i < 5; i++) {
    const before = await p.evaluate(() => L.item.item_id);
    await p.keyboard.press(i % 2 ? '2' : ' ');   // not_enough_info | answerable
    await p.keyboard.press('Enter');
    try {
      await p.waitForFunction(b => L.item && L.item.item_id !== b, before, { timeout: 5000 });
      saved++;
    } catch (e) {
      // e.g. a required span: answer answerable instead
      await p.keyboard.press('2'); await p.keyboard.press(' '); await p.keyboard.press('Enter');
      await p.waitForTimeout(800);
    }
    if (i === 0) await p.screenshot({ path: `${SHOTS}/07_labelling.png` });
  }
  check('labeler saves items by keyboard', saved >= 4, saved);
  check('labeler has no Admin tab', !(await p.isVisible('text=Admin')));

  // Owner sees the new labeler
  await op.reload();
  await op.click('text=Admin');
  await op.waitForFunction(() => /dana|L0\d/.test(document.getElementById('labelers-table').innerText));
  const lt = await op.innerText('#labelers-table');
  check('owner sees the labeler with status active', /active/.test(lt), lt.split('\n').slice(0, 4).join(' | '));
  await op.waitForTimeout(1500);
  await op.screenshot({ path: `${SHOTS}/08_owner_dashboard.png`, fullPage: true });

  check('no JS errors', errors.length === 0, errors.slice(0, 5));
  await browser.close();
  const failed = results.filter(r => !r[1]).length;
  console.log(`\n${results.length - failed}/${results.length} browser checks passed`);
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
