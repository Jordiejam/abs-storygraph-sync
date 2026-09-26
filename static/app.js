let autoRefresh = true;
let timer;
let lastLogSeq = 0;     // the newest log line fetched
let readOnlyMode = false;
let lastStatus = '';    // the last /api/status body rendered
const MAX_LOG_LINES = 300;

// Each settings toggle row's pick, set by its data-scope/-mode/-auto buttons.
const choices = { scope: null, mode: null, auto: null };

const SCOPE_HEADERS = {
  in_progress: 'Currently Listening',
  in_progress_finished: 'Currently Listening + Finished',
  library: 'Library',
};

const SECRET_FIELDS = {
  ABS_TOKEN: 's-abs-token',
  STORYGRAPH_SESSION: 's-sg-session',
  STORYGRAPH_REMEMBER_TOKEN: 's-sg-remember',
};

function renderChoices() {
  for (const [key, value] of Object.entries(choices)) {
    document.querySelectorAll(`[data-${key}]`).forEach(b => b.classList.toggle('active', b.dataset[key] === value));
  }
  document.getElementById('daily-time-field').style.display = choices.mode === 'daily' ? 'flex' : 'none';
}

document.querySelectorAll('[data-scope], [data-mode], [data-auto]').forEach(btn => {
  btn.onclick = () => {
    Object.assign(choices, btn.dataset);
    renderChoices();
  };
});

// ── Status ────────────────────────────────────────────────────────────────

async function fetchStatus() {
  try {
    const r = await fetch('/api/status');
    const body = await r.text();
    // Nothing changes between polls most of the time; don't rebuild the page.
    if (body === lastStatus) return;
    const d = JSON.parse(body);
    if (d.error) throw new Error(d.error);
    lastStatus = body;

    setDot('d-abs', d.abs_ok);
    setDot('d-sg', d.sg_ok);
    readOnlyMode = Boolean(d.read_only);
    const pollMinutes = Math.round(d.poll_interval / 60);
    document.querySelector('[data-mode="frequent"]').textContent = `Every ${pollMinutes} Minutes`;
    document.getElementById('l-poll').textContent = readOnlyMode
      ? 'Read-only dev'
      : d.sync_mode === 'daily'
        ? `Daily at ${d.daily_sync_time}`
        : `Every ${pollMinutes} min`;
    document.getElementById('sync-btn').disabled = readOnlyMode;
    document.getElementById('sync-label').textContent = readOnlyMode ? 'Read-only mode' : 'Sync Now';
    document.getElementById('books-header').textContent = SCOPE_HEADERS[d.sync_scope] || 'Currently Listening';

    const grid = document.getElementById('books-grid');
    if (!d.books?.length) {
      grid.innerHTML = emptyState('No audiobooks found for the current sync scope.');
      return;
    }
    grid.innerHTML = d.books.map(b => {
      const syncedLabel = b.last_synced_minutes != null
        ? `Last synced at ${b.last_synced_minutes} min (${b.progress_percent}%)`
        : 'Not yet synced this session';
      return `
        <div class="book-card">
          <div class="book-meta">
            <div class="book-title">${esc(b.title)}${b.is_finished ? ' <span class="badge success">Finished</span>' : ''}</div>
            <div class="book-author">${esc(b.author)}</div>
          </div>
          <div class="progress-wrap">
            <div class="progress-bar">
              <div class="progress-fill" style="width:${b.progress_percent}%"></div>
            </div>
            <div class="progress-row">
              <span>${b.progress_percent}%</span>
              <span>${b.current_minutes} / ${b.duration_minutes} min</span>
            </div>
          </div>
          <div class="last-sync">${esc(syncedLabel)}</div>
          <div class="book-actions">
            <button class="btn btn-ghost" onclick="openHistory(${jsArg(b.abs_item_id)})">History</button>
            ${b.edition_state === 'confirmed' ? '' : `<a class="btn btn-ghost" href="/editions?q=${encodeURIComponent(b.title)}">${editionBadge(b.edition_state)} Edition</a>`}
          </div>
        </div>`;
    }).join('');
  } catch (e) {
    console.error('Status refresh failed', e);
  }
}

const CONFIDENCE_INFO = {
  high: { label: 'High', text: 'Sessions line up cleanly and the furthest position moved forward steadily — this looks safe to trust.' },
  review: { label: 'Needs a look', text: 'A few days show rewinds, re-listening, or no forward progress. Worth skimming the flagged rows below before importing anything.' },
  insufficient: { label: 'Insufficient', text: 'Not enough usable session data (or a missing book runtime) to reconstruct daily progress yet.' },
};

const FLAG_INFO = {
  rewind_or_relisten: 'Rewound / re-listened',
  no_new_progress: 'No forward progress',
  gap: 'Long gap before this session',
  large_jump: 'Jumped ahead — check for skipped material',
};

function flagBadges(flags) {
  if (!flags || !flags.length) return '<span class="tag tag-ok">✓ On track</span>';
  return flags.map(f => `<span class="tag tag-warn">${esc(FLAG_INFO[f] || f)}</span>`).join('');
}

function pctText(percent) {
  return percent == null ? '—' : `${percent}%`;
}

function historySummary(s) {
  const ci = CONFIDENCE_INFO[s.confidence] || { label: s.confidence, text: '' };

  const callouts = [];
  if (s.skipped_session_count) {
    callouts.push(`⚠ ${plural(s.skipped_session_count, 'session')} skipped — Audiobookshelf didn't include a usable date or position, so ${s.skipped_session_count === 1 ? 'it isn\'t' : 'they aren\'t'} counted below.`);
  }
  const gap = Math.round((s.total_listening_minutes - (s.latest_position_minutes || 0)) * 10) / 10;
  if (gap > 1) {
    callouts.push(`ℹ You listened to ${s.total_listening_minutes} min in total, but the furthest position only reached ${s.latest_position_minutes} min. The ${gap} min gap is most likely rewinds or re-listening to earlier parts, not lost progress.`);
  }

  return `
    <div class="history-summary">
      <div class="history-stat"><strong>${s.session_count}</strong><span>ABS sessions</span></div>
      <div class="history-stat"><strong>${s.day_count}</strong><span>Listening days</span></div>
      <div class="history-stat"><strong>${s.total_listening_minutes} min</strong><span>Time listened</span></div>
      <div class="history-stat confidence-${esc(s.confidence)}"><strong>${esc(ci.label)}</strong><span>Preview confidence</span></div>
    </div>
    ${ci.text ? `<div class="history-callouts"><p class="history-note">${esc(ci.text)}</p>${callouts.map(c => `<p class="history-note">${esc(c)}</p>`).join('')}</div>` : ''}`;
}

// ── History & Import ─────────────────────────────────────────────────────

let historyView = null;        // { itemId, data } for the currently open history card
let selectedDays = new Set();  // checkpoint keys ticked for import

function closeHistory() {
  document.getElementById('history-card').style.display = 'none';
  historyView = null;
  selectedDays = new Set();
}

// Shown even once confirmed, since a match can land on the wrong work.
function editionOverrideField(itemId, label) {
  return pasteEditionField('postEditionChoice', itemId, 'manual-edition-url', label,
    'Confirms that edition for both Sync and Import History — useful when a title search lands on the wrong StoryGraph work (e.g. a split-up dramatized adaptation).');
}

// Nothing is importable until a person has confirmed the edition.
function importableDays(data) {
  if (data.state !== 'confirmed') return [];
  return (data.days || []).filter(d => d.progress_percent != null && !d.already_imported && !d.already_logged_on_storygraph);
}

async function openHistory(itemId) {
  const card = document.getElementById('history-card');
  const content = document.getElementById('history-content');
  card.style.display = 'block';
  content.innerHTML = emptyState('Loading ABS listening sessions…');
  card.scrollIntoView({ behavior: 'smooth', block: 'start' });
  try {
    const d = await getJSON(`/api/history-import-preview/${encodeURIComponent(itemId)}`);
    historyView = { itemId, data: d };
    // Flagged days start unticked: the write can't be undone.
    selectedDays = new Set(importableDays(d).filter(x => !x.flags?.length).map(x => x.key));
    document.getElementById('history-title').textContent = `History — ${d.book.title}`;
    renderHistory();
  } catch (e) {
    content.innerHTML = emptyState(e.message || 'Could not load history');
  }
}

function editionSection(itemId, data) {
  if (!data.storygraph_ready) {
    return '<div class="history-note">Add a working StoryGraph session in Settings to match an edition and import these days.</div>';
  }
  const edition = data.edition;
  const pick = id => `postEditionChoice(${jsArg(itemId)}, ${jsArg(id)})`;
  const others = otherCandidates(data.candidates, edition);
  const headline = label => `${editionBadge(data.state)} ${label}: <strong>${editionTitleLink(edition, editionFallbackTitle(edition, data.book))}</strong> ${editionDetails(edition, data.book)}`;
  if (data.state === 'auto') {
    return `<div class="history-note">${headline('Edition')}
      <button class="btn btn-primary" style="margin-left:0.5rem" onclick="${pick(edition.storygraph_book_id)}">Keep</button>
      <a class="btn btn-ghost" href="/editions?q=${encodeURIComponent(data.book.title)}">Review</a>
      ${matchReasonText(data.reason)}</div>
      <div class="history-note">Sync confirmed this edition automatically. Keep it before importing, since imported days can't be undone.</div>`;
  }
  if (data.state === 'confirmed') {
    return `<div class="history-note">${headline('Edition')}</div>`;
  }
  const lead = data.state === 'suggested'
    ? `<div class="history-note">${headline('Suggested edition')}
         <button class="btn btn-primary" style="margin-left:0.5rem" onclick="${pick(edition.storygraph_book_id)}">Confirm</button>
         ${matchReasonText(data.reason)}${readEditionNote(data.reason, pick)}</div>
       <div class="history-note">Nothing can be imported until you confirm which StoryGraph edition this is.</div>`
    : `<div class="history-note">Couldn't confidently match a StoryGraph audio edition — nothing can be imported until you pick one.${matchReasonText(data.reason)}${readEditionNote(data.reason, pick)}</div>`;
  return `
    ${lead}
    ${others.length ? `<div class="edition-candidates">${candidateRows(others, data.book, pick)}</div>` : ''}
    ${editionOverrideField(itemId, 'Or paste a StoryGraph book URL or id')}`;
}

function renderHistory() {
  const { itemId, data } = historyView;
  const content = document.getElementById('history-content');
  const canImport = data.state === 'confirmed';
  const importable = importableDays(data);
  const importableKeys = new Set(importable.map(d => d.key));

  const rows = (data.days || []).map(day => {
    let noteBadge;
    if (day.already_imported) noteBadge = '<span class="tag tag-ok">✓ Already imported</span>';
    else if (day.already_logged_on_storygraph) noteBadge = '<span class="tag tag-ok">✓ Already on StoryGraph</span>';
    else noteBadge = flagBadges(day.flags);
    const box = importableKeys.has(day.key)
      ? `<input type="checkbox" aria-label="Import ${esc(day.date)}" ${selectedDays.has(day.key) ? 'checked' : ''} onchange="toggleDay(${jsArg(day.key)}, this.checked)">`
      : '';
    return `
      <tr>
        ${canImport ? `<td>${box}</td>` : ''}
        <td>${esc(day.date)}</td>
        <td>${day.session_count}</td>
        <td>${day.listening_minutes} min</td>
        <td>${day.end_position_minutes} min</td>
        <td>${pctText(day.progress_percent)}</td>
        <td>${noteBadge}</td>
      </tr>`;
  }).join('');
  const columns = canImport ? 7 : 6;

  content.innerHTML = `
    ${historySummary(data.summary)}
    ${editionSection(itemId, data)}
    <div class="history-table-wrap" style="margin-top:0.75rem">
      <table class="history-table">
        <thead><tr>${canImport ? '<th></th>' : ''}<th>Date</th><th>Sessions</th><th>Listened</th><th>End position</th><th>Progress</th><th>Notes</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="${columns}">No usable sessions found.</td></tr>`}</tbody>
      </table>
    </div>
    ${canImport ? importControls(itemId, importable.length) : ''}`;
}

function importControls(itemId, importableCount) {
  return `
    ${importableCount ? `<div class="text-dim" style="margin-top:0.5rem">
      <a href="#" onclick="setAllDays(true); return false">Select all</a> ·
      <a href="#" onclick="setAllDays(false); return false">Select none</a>
      — flagged days start unticked; review them before including one.
    </div>` : ''}
    ${editionOverrideField(itemId, 'Wrong edition? Paste the right StoryGraph book URL or id')}
    <div id="import-review" style="margin-top:1rem"></div>
    <div class="history-note" id="import-note" style="margin-top:1rem">${importNote(selectedDays.size)}</div>
    <div style="margin-top:0.75rem">
      <button class="btn btn-primary" id="import-confirm" ${selectedDays.size && !readOnlyMode ? '' : 'disabled'} onclick="confirmImport()">${confirmLabel(selectedDays.size)}</button>
    </div>`;
}

function confirmLabel(count) {
  return `Confirm & Import ${plural(count, 'entry', 'entries')}`;
}

// Patched in place: a re-render would drop the checkbox just clicked.
function toggleDay(key, on) {
  if (on) selectedDays.add(key); else selectedDays.delete(key);
  const btn = document.getElementById('import-confirm');
  if (btn) {
    btn.disabled = !selectedDays.size || readOnlyMode;
    btn.textContent = confirmLabel(selectedDays.size);
  }
  const note = document.getElementById('import-note');
  if (note) note.textContent = importNote(selectedDays.size);
}

function setAllDays(on) {
  selectedDays = on ? new Set(importableDays(historyView.data).map(d => d.key)) : new Set();
  renderHistory();
}

function importNote(count) {
  if (readOnlyMode) return 'Read-only development mode — importing is disabled.';
  if (!count) return 'Nothing selected — tick the days above to import them.';
  return `Writing ${plural(count, 'new dated entry', 'new dated entries')} can't be undone automatically — you'd need to delete them by hand on StoryGraph afterward.`;
}

function resultRow(status, title, detail = '') {
  return `
    <div class="result-row">
      <span class="badge ${esc(status)}">${esc(status.replace(/_/g, ' '))}</span>
      <span class="result-title">${esc(title)}</span>
      ${detail}
    </div>`;
}

const FINISH_NOTES = {
  marked_read: 'Marked read on StoryGraph, dated from your Audiobookshelf listening.',
  left_currently_reading: 'Left as currently reading on StoryGraph because some days failed. Import again to retry them; the book is marked read once they are all in.',
  failed: 'The days were written, but marking the book read on StoryGraph failed. Import again to retry it.',
};

async function confirmImport(allowReread = false) {
  const { itemId } = historyView;
  const review = document.getElementById('import-review');
  review.innerHTML = emptyState('Writing to StoryGraph…');
  try {
    // Keys only; the server rebuilds each day from Audiobookshelf.
    const result = await sendJSON(`/api/history-import/${encodeURIComponent(itemId)}`, {
      days: [...selectedDays],
      allow_reread: allowReread,
    });
    const rows = (result.results || []).map(res => resultRow(
      res.status,
      res.date,
      res.reason ? `<span class="text-dim">${esc(res.reason.replace(/_/g, ' '))}</span>` : '',
    )).join('');
    const finish = FINISH_NOTES[result.finish] ? ` ${FINISH_NOTES[result.finish]}` : '';
    review.innerHTML = `<div class="history-note">Imported ${result.imported} of ${result.total}.${esc(finish)} Re-open History to see updated status.</div>${rows}`;
  } catch (e) {
    if (e.data && e.data.already_read) {
      askReread(review);
      return;
    }
    review.innerHTML = emptyState(e.message || 'Import failed');
  }
}

function askReread(review) {
  // Entries on a book StoryGraph has as read start a new read.
  review.innerHTML = `
    <div class="history-note">
      This book is already marked read on StoryGraph. Importing these days
      will add them as a <strong>reread</strong>: a second read of the book
      alongside the one already there. To keep a single read instead, remove
      the book from your shelf on StoryGraph and import again.
    </div>
    <div style="margin-top:0.75rem">
      <button class="btn btn-primary" onclick="confirmImport(true)">Import as a reread</button>
      <button class="btn btn-ghost" style="margin-left:0.5rem" onclick="renderHistory()">Cancel</button>
    </div>`;
}

async function postEditionChoice(itemId, storygraphBookId) {
  const content = document.getElementById('history-content');
  content.innerHTML = emptyState('Confirming that edition…');
  try {
    await confirmEdition(itemId, storygraphBookId);
    openHistory(itemId);
    fetchStatus();
  } catch (e) {
    content.innerHTML = emptyState(e.message || 'Could not use that edition');
  }
}

function setDot(dotId, ok) {
  document.getElementById(dotId).className = 'dot ' + (ok ? 'ok' : 'err');
}

// ── Sync ─────────────────────────────────────────────────────────────────

async function triggerSync() {
  const btn = document.getElementById('sync-btn');
  const lbl = document.getElementById('sync-label');
  btn.disabled = true;
  lbl.innerHTML = '<div class="spinner"></div>';
  const resultEl = document.getElementById('sync-result');
  resultEl.style.display = 'none';
  try {
    const d = await sendJSON('/api/sync');
    toast(`✓ ${d.synced}/${d.total} books synced`);
    resultEl.style.display = 'flex';
    resultEl.innerHTML = (d.results || []).map(r => resultRow(
      r.status,
      r.title,
      r.progress_percent != null ? `<span class="result-pct">${r.progress_percent}%</span>` : '',
    )).join('');
    fetchStatus();
    fetchLogs();
  } catch (e) {
    toast(e.message || 'Sync request failed', 'err');
  } finally {
    btn.disabled = readOnlyMode;
    lbl.textContent = readOnlyMode ? 'Read-only mode' : 'Sync Now';
  }
}

// ── Settings ──────────────────────────────────────────────────────────────

async function loadSettings() {
  try {
    const d = await getJSON('/api/settings');
    if (d.ABS_URL) document.getElementById('s-abs-url').value = d.ABS_URL;
    for (const [key, id] of Object.entries(SECRET_FIELDS)) {
      const input = document.getElementById(id);
      const isSet = d[key] === 'set';
      input.value = '';
      input.placeholder = isSet ? '(already set)' : '••••••••';
      input.classList.toggle('is-set', isSet);
    }
    choices.scope = d.SYNC_SCOPE;
    choices.mode = d.SYNC_MODE;
    choices.auto = d.AUTO_CONFIRM_EDITIONS;
    document.getElementById('s-daily-time').value = d.DAILY_SYNC_TIME;
    document.getElementById('timezone-hint').textContent = `Uses ${d.TIMEZONE}.`;
    renderChoices();
  } catch (e) {
    toast('Could not load settings', 'err');
  }
}

async function saveSettings(e) {
  e.preventDefault();
  const payload = {};
  for (const [key, id] of Object.entries({ ABS_URL: 's-abs-url', ...SECRET_FIELDS })) {
    const v = document.getElementById(id).value;
    if (v) payload[key] = v;
  }
  payload.SYNC_SCOPE = choices.scope;
  payload.SYNC_MODE = choices.mode;
  payload.AUTO_CONFIRM_EDITIONS = choices.auto;
  payload.DAILY_SYNC_TIME = document.getElementById('s-daily-time').value || '00:00';
  payload.TIMEZONE = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  try {
    await sendJSON('/api/settings', payload);
  } catch (err) {
    toast(err.message, 'err');
    return;
  }
  const msg = document.getElementById('save-msg');
  msg.classList.add('show');
  setTimeout(() => msg.classList.remove('show'), 4000);
  await loadSettings();
  fetchStatus();
}

function toggle(id, btn) {
  const el = document.getElementById(id);
  el.type = el.type === 'password' ? 'text' : 'password';
  btn.textContent = el.type === 'password' ? '👁' : '🙈';
}

// ── Users ─────────────────────────────────────────────────────────────────

async function loadUsers() {
  try {
    const d = await getJSON('/api/users');
    document.getElementById('users-list').innerHTML = (d.users || []).map(u => `
      <div class="result-row">
        <span class="result-title">${esc(u.display_name || u.username)}</span>
        ${u.is_admin ? '<span class="badge success">admin</span>' : ''}
        ${u.via_oidc ? '<span class="badge not_found">sso</span>' : ''}
        <button class="btn btn-ghost" onclick="deleteUser(${jsArg(u.id)})">Remove</button>
      </div>`).join('') || emptyState('No users yet.');
  } catch (e) {
    toast('Could not load users', 'err');
  }
}

function showAddUser() {
  document.getElementById('add-user-form').style.display = 'block';
}

async function createUser(e) {
  e.preventDefault();
  const username = document.getElementById('nu-username').value;
  const password = document.getElementById('nu-password').value;
  try {
    await sendJSON('/api/users', { username, password });
  } catch (err) {
    toast(err.message || 'Failed to create user', 'err');
    return;
  }
  document.getElementById('add-user-form').style.display = 'none';
  document.getElementById('nu-username').value = '';
  document.getElementById('nu-password').value = '';
  toast('✓ User created');
  loadUsers();
}

async function deleteUser(id) {
  try {
    await sendJSON(`/api/users/${encodeURIComponent(id)}`, undefined, 'DELETE');
    loadUsers();
  } catch (e) {
    toast(e.message || 'Failed to remove user', 'err');
  }
}

// ── Logs ──────────────────────────────────────────────────────────────────

// Each fetch appends only the lines after the newest one shown.
async function fetchLogs() {
  const box = document.getElementById('log-box');
  if (!box) return;  // only rendered for admins
  try {
    let d = await getJSON(`/api/logs?since=${lastLogSeq}`);
    if (d.latest < lastLogSeq) {
      // The server restarted and numbers afresh.
      box.innerHTML = '';
      lastLogSeq = 0;
      d = await getJSON('/api/logs');
    }
    if (!d.logs.length) return;
    const atBottom = box.scrollHeight - box.scrollTop <= box.clientHeight + 40;
    box.insertAdjacentHTML('beforeend', d.logs.map(l => `
      <div class="log-line">
        <span class="log-time">${esc(l.time)}</span>
        <span class="log-lvl ${esc(l.level)}">${esc(l.level.slice(0,4))}</span>
        <span class="log-msg ${l.level==='ERROR'?'err':''}">${esc(l.msg)}</span>
      </div>`).join(''));
    while (box.children.length > MAX_LOG_LINES) box.firstElementChild.remove();
    lastLogSeq = d.latest;
    if (atBottom) box.scrollTop = box.scrollHeight;
  } catch (e) {
    console.error('Log refresh failed', e);
  }
}

function clearLogs() {
  document.getElementById('log-box').innerHTML = '';
}

function toggleAuto(btn) {
  autoRefresh = !autoRefresh;
  btn.classList.toggle('active', autoRefresh);
  btn.textContent = autoRefresh ? 'Auto-refresh' : 'Auto-refresh (off)';
  if (autoRefresh) refresh(); else clearTimeout(timer);
}

// Refresh now, then 5s after each refresh finishes, so a slow one never overlaps the next.
let refreshing = false;
async function refresh() {
  clearTimeout(timer);
  if (refreshing) return;  // the one in flight schedules the next
  refreshing = true;
  try {
    await Promise.all([fetchStatus(), fetchLogs()]);
  } finally {
    refreshing = false;
  }
  if (autoRefresh && !document.hidden) timer = setTimeout(refresh, 5000);
}

// A hidden tab has no one to show a refresh to.
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    clearTimeout(timer);
  } else if (autoRefresh) {
    refresh();
  }
});

// ── Init ──────────────────────────────────────────────────────────────────
loadSettings();
if (document.getElementById('users-list')) loadUsers();
refresh();
