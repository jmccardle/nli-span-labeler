// Annotator mode (docs/e13/ANNOTATOR.md): the Dataset tab (browse a curated
// batch), and the item workspace under the clause editor: numbered trees,
// recording or typing what you'd say, the job queue, proposals to review,
// notes and versions. Loaded after label.js and clauses.js; label.js calls in
// here when L.annot is set.

const A = { batch: null, items: [], idx: -1, ws: null, poll: null, rec: null };

function annotActive() {
    return !!(L && L.annot);
}

function enc(itemId) {
    return encodeURIComponent(itemId);
}

// ============================================================================
// Dataset tab
// ============================================================================

async function loadDatasetBatches() {
    const resp = await authenticatedFetch('/api/annotator/batches');
    if (!resp.ok) return;
    const batches = (await resp.json()).batches;
    const sel = document.getElementById('dataset-batch');
    sel.innerHTML = batches.length
        ? batches.map(b => `<option value="${escapeHtml(b.name)}">${escapeHtml(b.name)} (${b.n_labelled}/${b.n_items})</option>`).join('')
        : '<option value="">no curated batches open</option>';
    if (A.batch && batches.some(b => b.name === A.batch)) sel.value = A.batch;
    if (sel.value) await loadDatasetItems();
    else document.getElementById('dataset-items').innerHTML =
        '<p class="option-desc">No curated batch is open. An admin can make one: <code>batch config NAME --mode curated</code>.</p>';
}

async function loadDatasetItems() {
    A.batch = document.getElementById('dataset-batch').value;
    if (!A.batch) return;
    const resp = await authenticatedFetch(`/api/annotator/batches/${encodeURIComponent(A.batch)}/items`);
    if (!resp.ok) return;
    A.items = (await resp.json()).items;
    renderDatasetItems();
}

function itemBadges(it) {
    const b = [];
    if (it.label) b.push(`<span class="label-${it.label}">${it.label}</span>`);
    else if (it.skipped) b.push(`<em>skipped</em>`);
    if (it.versions > 1) b.push(`<span title="versions">v${it.versions}</span>`);
    if (it.label_flips) b.push(`<span class="badge-warn" title="label changed between versions">⇄${it.label_flips}</span>`);
    if (it.utterances) b.push(`<span title="things you said">🗣${it.utterances}</span>`);
    if (it.untranscribed) b.push(`<span class="badge-busy" title="recordings waiting for transcription">…${it.untranscribed}</span>`);
    const busy = (it.jobs.queued || 0) + (it.jobs.running || 0) + (it.jobs.waiting || 0);
    if (busy) b.push(`<span class="badge-busy" title="jobs queued / running / waiting">⚙${busy}</span>`);
    if (it.jobs.failed) b.push(`<span class="badge-warn" title="failed jobs">✗${it.jobs.failed}</span>`);
    if (it.pending_proposal) b.push(`<span class="badge-review" title="a proposal is waiting for your review">review</span>`);
    if (it.notes) b.push(`<span title="notes${it.hedges ? ', hedged: ' + it.hedges : ''}">✎${it.notes}${it.hedges ? '?' : ''}</span>`);
    return b.join(' ');
}

function datasetFilter(it) {
    const f = document.getElementById('dataset-filter').value;
    const busy = (it.jobs.queued || 0) + (it.jobs.running || 0) + (it.jobs.waiting || 0) + (it.untranscribed || 0);
    return !f || (f === 'todo' && !it.label) || (f === 'review' && it.pending_proposal) || (f === 'busy' && busy)
        || (f === 'hard' && (it.label_flips || it.hedges));
}

function renderDatasetItems() {
    const shown = A.items.map((it, i) => ({ it, i })).filter(x => datasetFilter(x.it));
    const done = A.items.filter(it => it.label).length;
    document.getElementById('dataset-summary').textContent =
        `${done}/${A.items.length} labelled · ${A.items.filter(it => it.pending_proposal).length} to review`;
    document.getElementById('dataset-items').innerHTML = shown.length ? `<table class="admin-table dataset-table">
        <tr><th>#</th><th>hypothesis</th><th>premise</th><th>status</th></tr>
        ${shown.map(({ it, i }) => `<tr class="dataset-row" onclick="openItem(${i})">
            <td>${i + 1}</td><td>${escapeHtml(it.hypothesis || '')}</td>
            <td class="option-desc">${escapeHtml(it.premise)}</td><td>${itemBadges(it)}</td></tr>`).join('')}
        </table>` : '<p class="option-desc">Nothing matches this filter.</p>';
}

function openItem(i) {
    A.idx = i;
    switchTab('label', true);
    openWorkspace(A.items[i].item_id);
}

function annotStep(d) {
    if (!A.items.length) return;
    const next = A.idx + d;
    if (next < 0 || next >= A.items.length) return setBanner(d > 0 ? 'That was the last item.' : 'This is the first item.');
    openItem(next);
}

// ============================================================================
// Workspace
// ============================================================================

async function fetchWorkspace(itemId) {
    const resp = await authenticatedFetch(
        `/api/annotator/items/${enc(itemId)}/workspace?batch=${encodeURIComponent(A.batch)}`);
    if (!resp.ok) {
        showMessage((await resp.json()).detail || 'Could not open the item', 'error');
        return null;
    }
    return resp.json();
}

async function openWorkspace(itemId) {
    const ws = await fetchWorkspace(itemId);
    if (!ws) return;
    A.ws = ws;
    A.focus = {};
    renderItem(ws);   // the clause editor (label.js / clauses.js), then setupAnnotatorView
    L.annot = { batch: A.batch, itemId };
    L.relations = ws.annotation ? (ws.annotation.relations || []) : [];
    if (ws.annotation) {
        loadClauseEdit(ws.annotation);
        document.getElementById('note').value = ws.annotation.note || '';
    }
    renderAnnotPanel();
    setBanner(ws.annotation ? `Version ${ws.annotation.version}. Edit and press Enter to save version ${ws.annotation.version + 1}.`
        : 'Say what you see (v records), type it below, or label directly with the keys.');
    schedulePoll();
}

// Called by renderItem (label.js) for every item: the panel shows only for workspace payloads
const ANNOT_HINTS = 'Hypothesis words + <kbd>1</kbd>–<kbd>4</kbd> clause · premise words + <kbd>a</kbd> evidence · ' +
    '<kbd>v</kbd> record · <kbd>y</kbd> accept proposal · <kbd>Enter</kbd> save a version · <kbd>,</kbd> <kbd>.</kbd> prev/next · ' +
    '<kbd>z</kbd> undo · <kbd>Tab</kbd> premise ⇄ hypothesis · <kbd>U</kbd> rest unaddressed · tree click: select + focus ' +
    '(<kbd>Esc</kbd> clears) · <kbd>R</kbd> reference (after your answer)';

function setupAnnotatorView(item) {
    const on = !!item.parse;
    const hints = document.getElementById('clause-key-hints');
    if (!hints.dataset.queue) hints.dataset.queue = hints.innerHTML;
    hints.innerHTML = on ? ANNOT_HINTS : hints.dataset.queue;
    document.getElementById('annot-panel').classList.toggle('hidden', !on);
    document.getElementById('annot-reference').classList.add('hidden');
    document.getElementById('label-main').classList.toggle('show-nodes', on && document.getElementById('annot-show-nodes').checked);
    if (!on) {
        A.ws = null;
        stopPoll();
    }
}

async function refreshPanel() {
    if (!annotActive()) return stopPoll();
    const ws = await fetchWorkspace(L.annot.itemId);
    if (!ws || !annotActive() || ws.item_id !== L.item.item_id) return;
    A.ws = ws;
    renderAnnotPanel(false);
    schedulePoll();
}

function pendingWork(ws) {
    return ws.jobs.some(j => ['queued', 'running', 'waiting'].includes(j.status))
        || ws.utterances.some(u => u.audio && !u.text);
}

function schedulePoll() {
    stopPoll();
    if (A.ws && pendingWork(A.ws)) A.poll = setTimeout(refreshPanel, 3000);
}

function stopPoll() {
    if (A.poll) clearTimeout(A.poll);
    A.poll = null;
}

// ============================================================================
// Reference: the dataset's own answer, after yours (the first view is logged)
// ============================================================================

async function toggleReference() {
    const box = document.getElementById('annot-reference');
    if (!box.classList.contains('hidden')) {
        box.classList.add('hidden');
        document.querySelectorAll('.tok.ref-ev').forEach(t => t.classList.remove('ref-ev'));
        return;
    }
    if (!A.ws.annotation) return setBanner('Save your own annotation first; the reference opens after it.');
    const resp = await authenticatedFetch(
        `/api/annotator/items/${enc(L.annot.itemId)}/reference?batch=${encodeURIComponent(A.batch)}`);
    const data = await resp.json();
    if (!resp.ok) return setBanner(data.detail || 'No reference');
    A.ws.reference_seen_at = data.first_seen_at;
    renderReference(data.reference);
    box.classList.remove('hidden');
    updateRefButton();
}

function renderReference(ref) {
    const g = ref.gold || {};
    const goldText = g.label == null ? '<em>none</em>' : `${escapeHtml(String(g.label))}` +
        (g.nli && g.nli !== g.label ? ` → <span class="label-${g.nli}">${g.nli}</span>` : '') +
        (g.option_is_gold != null ? ` · this option ${g.option_is_gold ? 'is' : 'is not'} the gold answer` : '') +
        (g.dist ? ` · distribution ${escapeHtml(JSON.stringify(g.dist))}` : '');
    const clauses = (ref.clauses || []).map(c => `<div>${c.stance ? chip(c.stance) : ''} “${escapeHtml(c.text)}”
        ${c.stance_hint ? `<span class="option-desc">${escapeHtml(c.stance_hint)}</span>` : ''}
        ${(c.evidence || []).length ? '← ' + c.evidence.map(e => `“${escapeHtml(e.text || '')}”`).join(' ') : ''}
        <span class="node-pos">${escapeHtml(c.origin || '')}</span></div>`).join('');
    const ev = (ref.evidence || []);
    const evText = ev.map(e => `<span class="ref-chip">${escapeHtml(e.side)} “${escapeHtml(e.text || '')}”
        <span class="node-pos">${escapeHtml([e.role, e.set].filter(Boolean).join(' · '))}</span></span>`).join(' ');
    const rel = (ref.relations || []).map(r => `<div>${escapeHtml(JSON.stringify(r))}</div>`).join('');
    const task = ref.task || {};
    document.getElementById('annot-reference').innerHTML = `
        <div class="pane-title"><span>Reference · ${escapeHtml(ref.dataset)} · ${escapeHtml(ref.family)} · ${escapeHtml(ref.source_split || '')}</span>
            <span>first opened ${escapeHtml(A.ws.reference_seen_at || 'now')}; later versions are marked as seen-reference</span></div>
        <div><strong>Gold:</strong> ${goldText}</div>
        ${task.instructions && task.instructions !== 'NLI' ? `<div><strong>Original question:</strong> ${escapeHtml(task.instructions)}</div>` : ''}
        ${clauses ? `<div class="proposal-sub">dataset clauses</div>${clauses}` : ''}
        ${ev.length ? `<div class="proposal-sub">evidence (highlighted in the texts)</div>${evText}` : ''}
        ${rel ? `<div class="proposal-sub">relations</div>${rel}` : ''}
        ${ref.notes ? `<div class="proposal-sub">notes</div><div>${escapeHtml(typeof ref.notes === 'string' ? ref.notes : JSON.stringify(ref.notes))}</div>` : ''}
        ${ref.window ? `<div class="option-desc">The state is a window of the source: ${escapeHtml(JSON.stringify(ref.window))}</div>` : ''}
        ${(ref.blank || []).length ? `<div class="option-desc">The source has no: ${ref.blank.map(escapeHtml).join(', ')}</div>` : ''}`;
    // Highlight the reference evidence on the texts (offsets index the rendering and the hypothesis)
    const views = { premise: '#state-view', hypothesis: '#hypothesis-view' };
    ev.forEach(e => {
        const view = views[e.side];
        if (!view || e.start == null) return;
        document.querySelectorAll(`${view} .tok`).forEach(t => {
            if (+t.dataset.s < e.end && +t.dataset.e > e.start && !/^\s+$/.test(t.textContent)) t.classList.add('ref-ev');
        });
    });
}

function updateRefButton() {
    const btn = document.getElementById('annot-ref-btn');
    btn.classList.toggle('hidden', !(A.ws && A.ws.has_reference));
    btn.disabled = !(A.ws && A.ws.annotation);
    btn.textContent = A.ws && A.ws.reference_seen_at ? 'reference ✓' : 'reference';
}

function renderAnnotPanel(trees = true) {
    const ws = A.ws;
    updateRefButton();
    document.getElementById('annot-where').textContent =
        `${A.idx >= 0 ? `${A.idx + 1}/${A.items.length}` : ''} · ${ws.item_id}`;
    if (trees) {
        renderTrees();
        paintNodeNumbers();
        paintMatches();
    }
    renderUtterances();
    renderProposal();
    renderRelations();
    renderNotes();
    const v = ws.versions;
    document.getElementById('annot-versions').textContent = v.length
        ? `v${v[v.length - 1].version} · ${v.map(x => x.label ? x.label[0].toUpperCase() : '·').join('→')}` : 'not labelled';
}

// Node numbers sit on the words themselves (a CSS ::after from data-n), so selection is untouched
function paintNodeNumbers() {
    const show = document.getElementById('annot-show-nodes').checked;
    document.getElementById('label-main').classList.toggle('show-nodes', show && annotActive());
    if (!A.ws) return;
    const nodes = A.ws.parse.nodes;
    const mark = (view, side) => {
        const byStart = {};
        document.querySelectorAll(`${view} .tok`).forEach(t => { delete t.dataset.n; byStart[t.dataset.s] = t; });
        nodes[side].forEach(n => { const t = byStart[n.start]; if (t) t.dataset.n = n.n; });
    };
    mark('#state-view', 'premise');
    mark('#hypothesis-view', 'hypothesis');
}

function renderTrees() {
    const nodes = A.ws.parse.nodes;
    const tree = side => {
        const kids = {};
        const roots = [];
        nodes[side].forEach(n => (n.head === null ? roots : (kids[n.head] = kids[n.head] || [])).push(n));
        const line = (n, d) => `<div class="tree-node" data-n="${n.n}" style="padding-left:${d * 14}px" onclick="selectNode(${n.n}, event.shiftKey)"
                title="${escapeHtml(n.tag)}"><span class="node-n">${n.n}</span> ${escapeHtml(n.text)}
                <span class="node-pos">${escapeHtml(n.pos)} ${escapeHtml(n.dep)}</span></div>` +
            (kids[n.local] || []).map(k => line(k, d + 1)).join('');
        return roots.map(r => line(r, 0)).join('');
    };
    document.getElementById('annot-trees').innerHTML =
        `<div class="tree-side">premise</div>${tree('premise')}` +
        (nodes.hypothesis.length ? `<div class="tree-side">hypothesis</div>${tree('hypothesis')}` : '');
}

// Click a tree line: select that node's phrase (Shift: the word) in the editor, and focus
// its text on the node: words outside its subtree dim until another node, the same node
// again, or Esc. The focus stays through 1–4 and a, so a sentence can be worked through.
function selectNode(n, wordOnly) {
    const nodes = A.ws.parse.nodes;
    const node = [...nodes.premise, ...nodes.hypothesis].find(x => x.n === n);
    if (!node) return;
    const side = node.side === 'premise' ? 'state' : 'hypothesis';
    const cid = Object.keys(containers).find(c => containers[c].side === side);
    if (!cid) return;
    const prev = A.focus[side];
    if (prev && prev.n === n && !wordOnly && L.selection && L.selection.start === node.phrase[0]
        && L.selection.end === node.phrase[1]) {
        clearFocus(side);       // the same node again: unfocus
        L.selection = null;
        paintTokens();
        return setBanner(null);
    }
    A.focus[side] = { n, start: node.phrase[0], end: node.phrase[1] };
    applyFocus();
    const [start, end] = wordOnly ? [node.start, node.end] : node.phrase;
    const cps = codePoints(cid);
    L.selection = { cid, side, pointer: null, option: undefined, start, end, text: cps.slice(start, end).join('') };
    setRegion(side === 'state' ? 'state' : 'hypothesis');
    paintTokens();
    setBanner(`Selected ${wordOnly ? 'word' : 'phrase'} ${n}: “${L.selection.text}”. ${side === 'hypothesis'
        ? '1–4 makes it a clause.' : 'a links it to the active clause.'} (Esc or the same node again clears the focus.)`);
}

function applyFocus() {
    for (const [side, view] of [['state', '#state-view'], ['hypothesis', '#hypothesis-view']]) {
        const f = A.focus && A.focus[side];
        document.querySelectorAll(`${view} .tok`).forEach(t => t.classList.toggle('dim',
            !!f && !(+t.dataset.s < f.end && +t.dataset.e > f.start)));
    }
    const focused = new Set(Object.values(A.focus || {}).map(f => String(f.n)));
    document.querySelectorAll('.tree-node').forEach(el => el.classList.toggle('focused', focused.has(el.dataset.n)));
}

function clearFocus(side) {
    if (side) delete A.focus[side];
    else A.focus = {};
    applyFocus();
}

// ============================================================================
// Helpers: matching words, filling the rest as unaddressed
// ============================================================================

const CONTENT_POS = new Set(['NOUN', 'PROPN', 'NUM', 'VERB', 'ADJ', 'ADV', 'PRON']);
const EDGE_POS = new Set(['DET', 'AUX', 'ADP', 'CCONJ', 'SCONJ', 'PART', 'PUNCT', 'SYM', 'X', 'SPACE']);

function matchKey(text) {
    let w = text.toLowerCase().replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, '');
    if (w.length > 3 && w.endsWith('s') && !/\d/.test(w)) w = w.slice(0, -1);   // plural: a cheap stem
    return w;
}

// Words of the hypothesis that also occur in the premise (names, dates, numbers, content words)
function paintMatches() {
    document.querySelectorAll('.tok.match').forEach(t => { t.classList.remove('match'); delete t.dataset.m; });
    if (!A.ws || !document.getElementById('annot-show-matches').checked) return;
    const nodes = A.ws.parse.nodes;
    const hypKeys = new Set(nodes.hypothesis.filter(n => CONTENT_POS.has(n.pos) && n.pos !== 'PRON')
                                            .map(n => matchKey(n.text)).filter(k => k.length > 1));
    const premKeys = new Set(nodes.premise.map(n => matchKey(n.text)));
    const mark = (view, list) => {
        const byStart = {};
        document.querySelectorAll(`${view} .tok`).forEach(t => { byStart[t.dataset.s] = t; });
        list.forEach(n => {
            const k = matchKey(n.text);
            const t = byStart[n.start];
            if (t && hypKeys.has(k) && premKeys.has(k)) {
                t.classList.add('match');
                t.dataset.m = k;
            }
        });
    };
    mark('#state-view', nodes.premise);
    mark('#hypothesis-view', nodes.hypothesis);
    const n = document.querySelectorAll('#hypothesis-view .tok.match').length;
    document.getElementById('annot-match-count').textContent = n ? `${n} matched` : '';
}

// U: every stretch of hypothesis words that no clause covers becomes one unaddressed clause
// (trimmed of edge function words; only stretches with a content word)
function fillUnaddressed() {
    const hyp = A.ws.parse.nodes.hypothesis;
    const covered = n => L.clauses.some(c => n.start < c.end && n.end > c.start);
    const runs = [];
    let run = [];
    hyp.forEach(n => {
        if (covered(n)) {
            if (run.length) runs.push(run);
            run = [];
        } else run.push(n);
    });
    if (run.length) runs.push(run);
    const cid = Object.keys(containers).find(c => containers[c].side === 'hypothesis');
    const cps = codePoints(cid);
    let added = 0;
    labelAction(() => {
        runs.forEach(r => {
            while (r.length && EDGE_POS.has(r[0].pos)) r.shift();
            while (r.length && EDGE_POS.has(r[r.length - 1].pos)) r.pop();
            if (!r.some(n => CONTENT_POS.has(n.pos))) return;
            const start = r[0].start, end = r[r.length - 1].end;
            L.clauses.push({ start, end, text: cps.slice(start, end).join(''), stance: 'unaddressed', omission: false,
                             note: null, evidence: [] });
            added++;
        });
        L.clauses.sort((a, b) => a.start - b.start);
    });
    setBanner(added ? `Marked ${added} uncovered stretch${added > 1 ? 'es' : ''} unaddressed (z undoes).`
        : 'Every content word is already in a clause.');
}

// ============================================================================
// Relations of the version being edited (the agent adds them; × removes one)
// ============================================================================

function spanText(s) {
    return `${escapeHtml(s.text || '')} <span class="node-pos">[${(s.nodes || []).join(',')}]</span>`;
}

function renderRelations() {
    const rels = L.relations || [];
    document.getElementById('annot-relations').innerHTML = rels.length ? rels.map((r, i) => `<div class="note-row">
        ${spanText(r.from)} → <strong>${escapeHtml(r.type)}</strong> → ${spanText(r.to)}
        ${r.note ? `<span class="option-desc">${escapeHtml(r.note)}</span>` : ''}
        <button class="chip-x" title="Remove (saved with the next version)" onclick="removeRelation(${i})">×</button></div>`).join('')
        : '<span class="option-desc">No relations yet. Say them: “2 is the referent of 13”.</span>';
}

function removeRelation(i) {
    L.relations.splice(i, 1);
    renderRelations();
    setBanner('Relation removed; Enter saves the version without it.');
}

function selectionNodes() {
    if (!L.selection || !A.ws) return [];
    const side = L.selection.side === 'state' ? 'premise' : (L.selection.side === 'hypothesis' ? 'hypothesis' : null);
    if (!side) return [];
    return A.ws.parse.nodes[side].filter(n => n.start < L.selection.end && n.end > L.selection.start).map(n => n.n);
}

function renderUtterances() {
    const ws = A.ws;
    const jobsFor = uid => ws.jobs.filter(j => j.utterance_id === uid);
    document.getElementById('annot-utterances').innerHTML = ws.utterances.length ? ws.utterances.map(u => {
        const js = jobsFor(u.id).map(j => `<span class="job job-${j.status}" title="${escapeHtml(j.error || '')}">${j.kind} ${j.status}</span>`).join(' ');
        const play = u.audio ? `<audio controls preload="none" src="/api/annotator/utterances/${u.id}/audio"></audio>` : '';
        const text = u.text ? escapeHtml(u.text) : '<em>waiting for transcription</em>';
        return `<div class="utt"><div class="utt-meta">#${u.id} ${u.source}${u.kind !== 'label' ? ' · ' + u.kind : ''}
                ${u.duration_ms ? ' · ' + Math.round(u.duration_ms / 1000) + 's' : ''} ${js}</div>${play}<div>${text}</div></div>`;
    }).join('') : '<span class="option-desc">Nothing said yet.</span>';
    const waiting = ws.jobs.find(j => j.status === 'waiting');
    document.getElementById('annot-queue').textContent = waiting ? `waiting: ${waiting.error || 'engine'}`
        : (pendingWork(ws) ? 'working…' : '');
}

function chip(stance) {
    return `<span class="stance-chip ${stance}">${stance}</span>`;
}

// One line per edit a proposal makes (proposals hold edits, applied in order on accept)
function deltaLines(d) {
    const q = t => `“${escapeHtml(t || '')}”`;
    const ev = es => es.length ? ' ← ' + es.map(e => q(e.text)).join(' ') : '';
    const out = [];
    (d.add_clauses || []).forEach(c => out.push(`<div class="delta add">+ ${chip(c.stance)}${c.omission ? ' (omission)' : ''} ${q(c.text)}${ev(c.evidence)}</div>`));
    (d.change_clauses || []).forEach(c => {
        const bits = [];
        if (c.stance) bits.push(chip(c.stance));
        if (c.omission != null) bits.push(c.omission ? 'omission' : 'no omission');
        if (c.span) bits.push(`span → ${q(c.span.text)}`);
        if (c.add_evidence.length) bits.push(`+ evidence ${c.add_evidence.map(e => q(e.text)).join(' ')}`);
        if (c.remove_evidence.length) bits.push(`− evidence ${c.remove_evidence.map(e => q(e.text)).join(' ')}`);
        out.push(`<div class="delta change">~ ${q(c.target.text)}: ${bits.join(' · ') || 'no change'}</div>`);
    });
    (d.remove_clauses || []).forEach(c => out.push(`<div class="delta remove">− clause ${q(c.text)}</div>`));
    (d.add_relations || []).forEach(r => out.push(`<div class="delta add">+ ${spanText(r.from)} → <strong>${escapeHtml(r.type)}</strong> → ${spanText(r.to)}</div>`));
    (d.remove_relations || []).forEach(r => out.push(`<div class="delta remove">− relation ${q(r.from.text)} → ${escapeHtml(r.type)} → ${q(r.to.text)}</div>`));
    if (d.label_override) out.push(`<div class="delta change">~ label override: ${escapeHtml(d.label_override)}</div>`);
    if (d.completion) out.push(`<div class="delta change">~ completion: ${escapeHtml(JSON.stringify(d.completion))}</div>`);
    return out.join('');
}

function renderProposal() {
    const ps = A.ws.proposals || [];
    const el = document.getElementById('annot-proposal');
    document.getElementById('annot-proposal-state').textContent = ps.length ? `${ps.length} pending` : '';
    if (!ps.length) {
        el.innerHTML = '<span class="option-desc">No proposal waiting. Record or type what you see; each comment becomes a small proposal.</span>';
        return;
    }
    const v = A.ws.pending_view || {};
    const label = v.label ? `<span class="label-${v.label}">${v.label}</span>` : '–';
    const items = ps.map(p => {
        const pl = p.payload;
        const notes = (pl.notes || []).map(n => `<div class="delta add">+ <span class="note-cat">${escapeHtml(n.category)}</span>${n.hedge ? ' ?' : ''} ${escapeHtml(n.text)}</div>`).join('');
        const qs = (pl.questions || []).map(q => `<div class="proposal-q">? ${escapeHtml(q)}</div>`).join('');
        const probs = (p.problems || []).map(q => `<div class="proposal-problem">⚠ ${escapeHtml(q)}</div>`).join('');
        const said = A.ws.utterances.filter(u => p.utterance_ids.includes(u.id)).map(u => escapeHtml(u.text || '')).join(' / ');
        const legacy = !pl.delta && pl.clauses ? '<div class="proposal-problem">⚠ an older full-annotation proposal: accept replaces nothing; use edit first</div>' : '';
        return `<div class="proposal-item"><div class="utt-meta">#${p.id} · “${said.slice(0, 160)}”
                ${p.stale ? ' <span class="badge-warn">older parse</span>' : ''}
                <button class="btn btn-small" onclick="acceptProposal(false, ${p.id})" title="Accept this and every earlier pending one">accept to here</button>
                <button class="btn btn-small" onclick="rejectProposal(${p.id})">reject</button></div>
            ${deltaLines(pl.delta || {}) || '<div class="option-desc">no edits</div>'}${notes}${qs}${probs}${legacy}</div>`;
    }).join('');
    el.innerHTML = `<div class="proposal-label">All pending applied: ${label}${v.label_override ? ' (override)' : ''}</div>
        ${items}
        <div class="proposal-actions">
            <button class="btn btn-success btn-small" onclick="acceptProposal()" title="Accept every pending proposal as the next version (y)">accept all</button>
            <button class="btn btn-small" onclick="editProposal()" title="Load the stacked result into the editor; Enter accepts it as edited">edit first</button>
            <button class="btn btn-small" onclick="document.getElementById('annot-text').focus()">add more</button>
        </div>`;
}

function renderNotes() {
    document.getElementById('annot-notes').innerHTML = A.ws.notes.length ? A.ws.notes.map(n => `<div class="note-row">
        <span class="note-cat">${escapeHtml(n.category)}</span>${n.hedge ? '<span title="hedged">?</span>' : ''}
        ${n.nodes.length ? `<span class="node-pos">[${n.nodes.join(',')}]</span>` : ''} ${escapeHtml(n.text)}
        <button class="chip-x" title="Retract" onclick="retractNote(${n.id})">×</button></div>`).join('')
        : '<span class="option-desc">No notes yet. Select words, then add one below to attach it to them.</span>';
}

// ============================================================================
// Actions
// ============================================================================

async function postJson(url, body) {
    return authenticatedFetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                     body: JSON.stringify(body || {}) });
}

async function afterSave(resp, what) {
    const data = await resp.json();
    if (resp.ok) {
        showMessage(`${what}: version ${data.version}`, 'success');
        await reloadList();
        return openWorkspace(L.annot.itemId);
    }
    const d = data.detail;
    setBanner(d && d.problems ? d.problems.join(' · ') + (d.policy ? '  —  Shift+Enter saves anyway.' : '')
        : (typeof d === 'string' ? d : 'Could not save'));
}

async function reloadList() {
    const resp = await authenticatedFetch(`/api/annotator/batches/${encodeURIComponent(A.batch)}/items`);
    if (resp.ok) A.items = (await resp.json()).items;
}

// label.js submitItem hands the clause body here in annotator mode
async function annotatorSubmit(body, override) {
    body.relations = L.relations || [];   // the version's relations, as shown (× removes one)
    if (L.fromProposal) {
        const resp = await postJson(`/api/annotator/proposals/${L.fromProposal}/accept`,
                                    { edits: body, policy_override: override });
        return afterSave(resp, 'Accepted with edits');
    }
    const resp = await postJson(`/api/annotator/items/${enc(L.annot.itemId)}/annotations?batch=${encodeURIComponent(A.batch)}`, body);
    return afterSave(resp, 'Saved');
}

// Accept every pending proposal up to `id` (default: all), applied in order to the current version
async function acceptProposal(override = false, id = null) {
    const ps = (A.ws && A.ws.proposals) || [];
    if (!ps.length) return;
    const through = id || ps[ps.length - 1].id;
    const resp = await postJson(`/api/annotator/proposals/${through}/accept`, {
        policy_override: override, note: document.getElementById('note').value || null,
        active_ms: Math.round(L.timer.active) });
    return afterSave(resp, 'Accepted');
}

// Load the stacked result (latest version + every pending proposal) into the editor
function editProposal() {
    const ps = (A.ws && A.ws.proposals) || [];
    const v = A.ws && A.ws.pending_view;
    if (!ps.length || !v) return;
    labelAction(() => {
        L.clauses = (v.clauses || []).map(c => ({ start: c.start, end: c.end, text: c.text, stance: c.stance,
            omission: !!c.omission, note: null, evidence: c.evidence.map(({ start, end, text }) => ({ start, end, text })) }));
        L.activeClause = L.clauses.length ? 0 : -1;
        L.labelOverride = v.label_override || null;
    });
    L.relations = (v.relations || []).map(r => ({ ...r }));
    renderRelations();
    L.fromProposal = ps[ps.length - 1].id;
    setBanner(`Editing the ${ps.length} pending proposal${ps.length > 1 ? 's' : ''} together. Enter accepts the result as edited; Esc leaves them pending.`);
}

async function rejectProposal(id = null) {
    const ps = (A.ws && A.ws.proposals) || [];
    const p = id ? ps.find(x => x.id === id) : ps[ps.length - 1];
    if (!p) return;
    const resp = await postJson(`/api/annotator/proposals/${p.id}/reject`);
    if (resp.ok) {
        showMessage('Proposal rejected', 'success');
        L.fromProposal = null;
        refreshPanel();
    }
}

async function sendText() {
    const box = document.getElementById('annot-text');
    const text = box.value.trim();
    if (!text) return;
    const p = A.ws && A.ws.proposal;
    const resp = await postJson(`/api/annotator/items/${enc(L.annot.itemId)}/utterances/text?batch=${encodeURIComponent(A.batch)}`,
                                { text, kind: p ? 'followup' : 'label', proposal_id: p ? p.id : null });
    if (resp.ok) {
        box.value = '';
        showMessage('Queued for the agent', 'success');
        refreshPanel();
    } else {
        showMessage((await resp.json()).detail || 'Could not send', 'error');
    }
}

async function addNote() {
    const input = document.getElementById('annot-note-text');
    const text = input.value.trim();
    if (!text) return;
    const resp = await postJson(`/api/annotator/items/${enc(L.annot.itemId)}/notes?batch=${encodeURIComponent(A.batch)}`, {
        category: document.getElementById('annot-note-cat').value, text, nodes: selectionNodes() });
    if (resp.ok) {
        input.value = '';
        refreshPanel();
    }
}

async function retractNote(id) {
    const resp = await postJson(`/api/annotator/notes/${id}/retract`);
    if (resp.ok) refreshPanel();
}

// ============================================================================
// Recording: the browser only records; transcription happens in the queue
// ============================================================================

async function toggleRecording() {
    if (!annotActive()) return;
    if (A.rec) return A.rec.recorder.stop();
    if (!navigator.mediaDevices || !window.MediaRecorder) {
        return setBanner('This browser can\'t record here (recording needs https or http://localhost).');
    }
    let stream;
    try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
        return setBanner(`Microphone unavailable: ${e.message}`);
    }
    const type = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4']
        .find(t => MediaRecorder.isTypeSupported(t)) || '';
    const recorder = new MediaRecorder(stream, type ? { mimeType: type } : {});
    const chunks = [];
    const itemId = L.annot.itemId, batch = A.batch, proposal = A.ws && A.ws.proposal ? A.ws.proposal.id : null;
    const started = Date.now();
    recorder.ondataavailable = e => { if (e.data.size) chunks.push(e.data); };
    recorder.onstop = async () => {
        stream.getTracks().forEach(t => t.stop());
        clearInterval(A.rec.timer);
        A.rec = null;
        document.getElementById('annot-record').classList.remove('recording');
        document.getElementById('annot-record').textContent = '● record';
        document.getElementById('annot-rec-state').textContent = 'uploading…';
        const blob = new Blob(chunks, { type: recorder.mimeType || type || 'audio/webm' });
        const q = new URLSearchParams({ batch, duration_ms: String(Date.now() - started),
                                        kind: proposal ? 'followup' : 'label' });
        if (proposal) q.set('proposal_id', String(proposal));
        const resp = await authenticatedFetch(`/api/annotator/items/${enc(itemId)}/utterances/audio?${q}`, {
            method: 'POST', headers: { 'Content-Type': blob.type.startsWith('audio/') ? blob.type : 'audio/webm' }, body: blob });
        document.getElementById('annot-rec-state').textContent = resp.ok ? 'saved; queued' : 'upload failed';
        if (resp.ok && annotActive() && L.annot.itemId === itemId) refreshPanel();
        if (resp.ok) reloadList();
    };
    recorder.start();
    A.rec = { recorder, timer: setInterval(() => {
        document.getElementById('annot-rec-state').textContent = `recording ${Math.round((Date.now() - started) / 1000)}s (v stops)`;
    }, 250) };
    document.getElementById('annot-record').classList.add('recording');
    document.getElementById('annot-record').textContent = '■ stop';
}

// ============================================================================
// Keys in annotator mode (before the clause keys)
// ============================================================================

function annotKeyDown(e) {
    if (e.ctrlKey || e.metaKey || e.altKey) return false;
    const k = e.key;
    if (k === 'v') toggleRecording();
    else if (k === ',') annotStep(-1);
    else if (k === '.') annotStep(1);
    else if (k === 'y' && A.ws && (A.ws.proposals || []).length) acceptProposal(false);
    else if (k === 'R') toggleReference();
    else if (k === 'U') fillUnaddressed();
    else if (k === 'x' || k === 'e') setBanner('In the Dataset view, skipping and "edit earlier" are not needed: every item stays open.');
    else if (k === 'Escape' && L.fromProposal && !L.selection) {
        L.fromProposal = null;
        openWorkspace(L.annot.itemId);
    } else if (k === 'Escape' && !L.selection && Object.keys(A.focus || {}).length) {
        clearFocus();
        setBanner(null);
    } else return false;
    e.preventDefault();
    return true;
}

document.addEventListener('DOMContentLoaded', () => {
    const text = document.getElementById('annot-text');
    text.addEventListener('keydown', e => {
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            sendText();
        } else if (e.key === 'Escape') text.blur();
    });
    const note = document.getElementById('annot-note-text');
    note.addEventListener('keydown', e => {
        if (e.key === 'Enter') {
            e.preventDefault();
            addNote();
        } else if (e.key === 'Escape') note.blur();
    });
});
