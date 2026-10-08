// The `clauses` task (docs/e13/CLAUSE_TASK.md): split the hypothesis into
// clauses, give each a stance, link the premise words behind it. The sentence
// label is derived from the stances (the server derives it again on save).
// Loaded after label.js; label.js calls in here when L.task === 'clauses'.

const STANCES = ['supported', 'contradicted', 'undetermined', 'unaddressed'];
const STANCE_KEYS = { 1: 'supported', 2: 'contradicted', 3: 'undetermined', 4: 'unaddressed',
                      s: 'supported', c: 'contradicted', d: 'undetermined', u: 'unaddressed' };
const STANCE_HELP = {
    supported: 'premise words settle it true',
    contradicted: 'premise words settle it false',
    undetermined: 'related premise words, but they don\'t settle it',
    unaddressed: 'nothing in the premise bears on it',
};

function isClauseTask() {
    return !!(L && L.task === 'clauses');
}

function deriveLabel(clauses) {
    const st = clauses.map(c => c.stance);
    if (st.includes('contradicted')) return 'contradiction';
    if (st.length && st.every(s => s === 'supported')) return 'entailment';
    return 'neutral';
}

function finalLabel() {
    return L.labelOverride || (L.clauses.length ? deriveLabel(L.clauses) : null);
}

// ============================================================================
// Setup and rendering
// ============================================================================

function setupClauseView(item) {
    const on = item.task_type === 'clauses';
    ['hypothesis-block', 'clause-pane', 'clause-key-hints'].forEach(id =>
        document.getElementById(id).classList.toggle('hidden', !on));
    ['reasons-pane', 'span-block', 'reason-key-hints'].forEach(id =>
        document.getElementById(id).classList.toggle('hidden', on));
    document.getElementById('options-view').classList.toggle('hidden', on);
    document.getElementById('note').placeholder = on
        ? 'Optional; required when you override the derived label'
        : 'Optional; required for ambiguous / underspecified / subjective when the batch says so';
    if (!on) return;
    L.clauses = [];
    L.activeClause = -1;
    L.labelOverride = null;
    const hyp = item.question.hypothesis || '';
    document.getElementById('hypothesis-view').innerHTML =
        tokenize(hyp, container({ side: 'hypothesis', text: hyp }));
    document.getElementById('clause-override').value = '';
    document.getElementById('completion-entail').value = '';
    document.getElementById('completion-contradict').value = '';
    renderClauses();
}

function evidenceChip(e, ci, ei) {
    return `<span class="ev-chip">“${escapeHtml(e.text)}”` +
           `<button class="chip-x" title="Remove this evidence" ` +
           `onclick="event.stopPropagation(); labelAction(() => removeEvidence(${ci}, ${ei}))">×</button></span>`;
}

function renderClauses() {
    if (!isClauseTask()) return;
    const list = document.getElementById('clause-list');
    list.innerHTML = L.clauses.length ? L.clauses.map((c, i) => `
        <div class="clause-row ${i === L.activeClause ? 'active' : ''}" onclick="labelAction(() => { L.activeClause = ${i}; })">
            <span class="stance-chip ${c.stance}" title="${escapeHtml(STANCE_HELP[c.stance])}">${c.stance}${c.omission ? ' (omission)' : ''}</span>
            <span class="clause-text">“${escapeHtml(c.text)}”</span>
            <span class="clause-ev">${c.evidence.length ? '← ' + c.evidence.map((e, j) => evidenceChip(e, i, j)).join(' ')
                : (c.stance === 'unaddressed' ? '' : '<em>no evidence yet: select premise words, press a</em>')}</span>
            <button class="chip-x" title="Remove this clause"
                    onclick="event.stopPropagation(); labelAction(() => removeClause(${i}))">×</button>
        </div>`).join('')
        : '<span class="option-desc">No clauses yet. Select hypothesis words, then 1 supported · 2 contradicted · ' +
          '3 undetermined · 4 unaddressed.</span>';
    const derived = L.clauses.length ? deriveLabel(L.clauses) : '–';
    document.getElementById('clause-derived').textContent = derived;
    document.getElementById('clause-derived').className = `label-${derived}`;
    document.getElementById('clause-override').value = L.labelOverride || '';
    document.getElementById('completion-block').classList.toggle('hidden', finalLabel() !== 'neutral');
    paintTokens();
}

// Called from paintTokens (label.js) after the common classes are cleared
function paintClauses() {
    document.querySelectorAll('.tok').forEach(t => t.classList.remove(
        ...STANCES.map(s => `cl-${s}`), 'cl-active', 'ev-active', 'ev-other'));
    const overlap = (t, s, e) => +t.dataset.s < e && +t.dataset.e > s;
    const hypToks = [...document.querySelectorAll('#hypothesis-view .tok')];
    const stateToks = [...document.querySelectorAll('#state-view .tok')];
    L.clauses.forEach((c, i) => {
        hypToks.forEach(t => {
            if (overlap(t, c.start, c.end)) {
                t.classList.add(`cl-${c.stance}`);
                if (i === L.activeClause) t.classList.add('cl-active');
            }
        });
        c.evidence.forEach(ev => stateToks.forEach(t => {
            if (!overlap(t, ev.start, ev.end) || /^\s+$/.test(t.textContent)) return;
            if (i === L.activeClause) t.classList.add('ev-active', `cl-${c.stance}`);
            else t.classList.add('ev-other');
        }));
    });
}

// ============================================================================
// Actions (each wrapped in labelAction, so z undoes them)
// ============================================================================

function addOrRestance(stance) {
    const sel = L.selection;
    if (sel && sel.side === 'hypothesis') {
        // A selection inside one clause re-tags that clause; otherwise it makes a new one
        const inside = L.clauses.findIndex(c => sel.start >= c.start && sel.end <= c.end);
        if (inside >= 0) {
            L.clauses[inside].stance = stance;
            if (stance !== 'contradicted') L.clauses[inside].omission = false;
            if (stance === 'unaddressed') L.clauses[inside].evidence = [];
            L.activeClause = inside;
        } else if (L.clauses.some(c => sel.start < c.end && sel.end > c.start)) {
            setBanner('That overlaps a clause. Remove it (Del) or select words outside it.');
            return;
        } else {
            L.clauses.push({ start: sel.start, end: sel.end, text: sel.text, stance, omission: false, note: null,
                             evidence: [] });
            L.clauses.sort((a, b) => a.start - b.start);
            L.activeClause = L.clauses.findIndex(c => c.start === sel.start);
        }
        L.selection = null;
        L.anchor = -1;
        setBanner(stance === 'unaddressed' || L.clauses[L.activeClause].evidence.length ? null
            : 'Now select the premise words behind it and press a (Tab moves to the premise).');
        return;
    }
    if (sel && sel.side === 'state') {
        setBanner('Clauses are made on the hypothesis. To link these premise words to the active clause, press a.');
        return;
    }
    const c = L.clauses[L.activeClause];
    if (!c) {
        setBanner('Select hypothesis words first (drag, or Tab to the hypothesis and use ← → with Shift).');
        return;
    }
    c.stance = stance;
    if (stance !== 'contradicted') c.omission = false;
    if (stance === 'unaddressed') c.evidence = [];
    setBanner(null);
}

function attachEvidence() {
    const sel = L.selection;
    const c = L.clauses[L.activeClause];
    if (!sel || sel.side !== 'state') {
        setBanner('Select premise words first, then press a.');
        return;
    }
    if (!c) {
        setBanner('Make a clause on the hypothesis first; evidence attaches to the active clause.');
        return;
    }
    if (c.stance === 'unaddressed') {
        setBanner('An unaddressed clause has no evidence. Re-tag it (3 undetermined) if these words bear on it.');
        return;
    }
    if (c.evidence.some(e => e.start === sel.start && e.end === sel.end)) {
        setBanner('Already linked.');
        return;
    }
    c.evidence.push({ start: sel.start, end: sel.end, text: sel.text });
    c.evidence.sort((a, b) => a.start - b.start);
    L.selection = null;
    L.anchor = -1;
    setBanner(null);
}

function removeClause(i) {
    L.clauses.splice(i, 1);
    L.activeClause = Math.min(i, L.clauses.length - 1);
}

function removeEvidence(ci, ei) {
    L.clauses[ci].evidence.splice(ei, 1);
    L.activeClause = ci;
}

function toggleOmission() {
    const c = L.clauses[L.activeClause];
    if (!c || c.stance !== 'contradicted') {
        setBanner('Omission marks a contradicted clause whose evidence is an exhaustive scope that leaves it out.');
        return;
    }
    c.omission = !c.omission;
}

function cycleClause(delta) {
    if (!L.clauses.length) return;
    L.activeClause = (L.activeClause + delta + L.clauses.length) % L.clauses.length;
}

// ============================================================================
// Keyboard: handled here first in clause mode; anything else falls through to label.js
// ============================================================================

function selectionOrCursor() {
    if (!L.selection) {
        const cur = regionTokens()[L.cursor];
        if (cur) L.selection = selectionFromRange(cur, 0, cur, cur.textContent.length, false);
    }
}

function clauseKeyDown(e) {
    if (e.ctrlKey || e.metaKey || e.altKey) return false;
    const k = e.key;
    if (k in STANCE_KEYS) {
        selectionOrCursor();
        labelAction(() => addOrRestance(STANCE_KEYS[k]));
    } else if (k === 'a') {
        selectionOrCursor();
        labelAction(attachEvidence);
    } else if (k === 'o') {
        labelAction(toggleOmission);
    } else if (k === '[' || k === ']') {
        labelAction(() => cycleClause(k === '[' ? -1 : 1));
    } else if (k === 'Delete' || k === 'Backspace') {
        const c = L.clauses[L.activeClause];
        if (!c) return true;
        if (e.shiftKey && c.evidence.length) labelAction(() => removeEvidence(L.activeClause, c.evidence.length - 1));
        else labelAction(() => removeClause(L.activeClause));
    } else if (k === 'Tab') {
        setRegion(L.region === 'hypothesis' ? 'state' : 'hypothesis');
        L.selection = null;
        paintTokens();
    } else if (k === 'i') {
        if (finalLabel() === 'neutral') {
            const entail = document.getElementById('completion-entail');
            (entail.value ? document.getElementById('completion-contradict') : entail).focus();
        }
    } else if (/^[0-9]$/.test(k) || k === ' ' || k === 'r' || k === 'm') {
        // reasons-task keys: nothing to do here
    } else {
        return false;
    }
    e.preventDefault();
    return true;
}

// ============================================================================
// Submit and edit
// ============================================================================

function clauseBody(override) {
    const completion = {
        entail: document.getElementById('completion-entail').value.trim() || null,
        contradict: document.getElementById('completion-contradict').value.trim() || null,
    };
    return {
        item_id: L.item.item_id,
        clauses: L.clauses.map(({ start, end, text, stance, omission, note, evidence }) =>
            ({ start, end, text, stance, omission, note, evidence: evidence.map(({ start, end, text }) => ({ start, end, text })) })),
        label_override: L.labelOverride,
        completion: finalLabel() === 'neutral' && (completion.entail || completion.contradict) ? completion : null,
        note: document.getElementById('note').value,
        policy_override: override,
        active_ms: Math.round(L.timer.active),
    };
}

function loadClauseEdit(edit) {
    L.clauses = (edit.clauses || []).map(c => ({ ...c, evidence: c.evidence.map(({ start, end, text }) => ({ start, end, text })) }));
    L.activeClause = L.clauses.length ? 0 : -1;
    L.labelOverride = edit.label_override || null;
    document.getElementById('completion-entail').value = (edit.completion || {}).entail || '';
    document.getElementById('completion-contradict').value = (edit.completion || {}).contradict || '';
    renderClauses();
}

document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('hypothesis-view').addEventListener('mouseup', handleMouseUp);
    document.getElementById('clause-override').addEventListener('change', e => {
        labelAction(() => { L.labelOverride = e.target.value || null; });
        if (L.labelOverride) setBanner('Overriding the derived label: say why in the note (n).');
        e.target.blur();
    });
    ['completion-entail', 'completion-contradict'].forEach(id => {
        const input = document.getElementById(id);
        input.addEventListener('keydown', e => {
            if (e.key === 'Escape') {
                input.blur();
            } else if (e.key === 'Enter' && L) {
                e.preventDefault();
                input.blur();
                submitItem(e.shiftKey);
            }
        });
    });
});
