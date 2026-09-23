let autoRefresh = true;
let timer;
let shownLogs = 0;
let selectedScope = 'in_progress';
let selectedMode = 'frequent';
let readOnlyMode = false;

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

function renderScope() {
  document.querySelectorAll('.scope-btn').forEach(b => b.classList.toggle('active', b.dataset.scope === selectedScope));
}

function renderSyncMode() {
  document.querySelectorAll('.mode-btn').forEach(b => b.classList.toggle('active', b.dataset.mode === selectedMode));
  document.getElementById('daily-time-field').style.display = selectedMode === 'daily' ? 'flex' : 'none';
}

document.querySelectorAll('.scope-btn').forEach(btn => {
  btn.onclick = () => {
    selectedScope = btn.dataset.scope;
    renderScope();
  };
});

document.querySelectorAll('.mode-btn').forEach(btn => {
  btn.onclick = () => {
    selectedMode = btn.dataset.mode;
    renderSyncMode();
  };
});

// ── Requests ─────────────────────────────────────────────────────────────

async function getJSON(url) {
  const d = await (await fetch(url)).json();
  if (d.error) throw new Error(d.error);
  return d;
}

async function sendJSON(url, body, method = 'POST') {
  const r = await fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok || d.error) throw new Error(d.error || `Request failed (HTTP ${r.status})`);
  return d;
}

// ── Status ────────────────────────────────────────────────────────────────

async function fetchStatus() {
  try {
    const d = await getJSON('/api/status');

    setDot('d-abs', 'l-abs', d.abs_ok, 'ABS');
    setDot('d-sg',  'l-sg',  d.sg_ok,  'StoryGraph');
    readOnlyMode = Boolean(d.read_only);
    const pollMinutes = Math.round(d.poll_interval / 60);
    document.querySelector('.mode-btn[data-mode="frequent"]').textContent = `Every ${pollMinutes} Minutes`;
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
      grid.innerHTML = '<div class="empty-state">No audiobooks found for the current sync scope.</div>';
      return;
    }
    grid.innerHTML = d.books.map(b => {
      const synced = d.last_synced[b.state_key];
      const syncedLabel = synced != null
        ? `Last synced at ${synced} min (${b.progress_percent}%)`
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
            <button class="btn btn-ghost" onclick="previewHistory(${jsArg(b.abs_item_id)})">Preview history</button>
            <button class="btn btn-ghost" onclick="openImport(${jsArg(b.abs_item_id)})">Import History</button>
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

function showCard(cardId, contentId, loadingText) {
  const card = document.getElementById(cardId);
  const content = document.getElementById(contentId);
  card.style.display = 'block';
  content.innerHTML = `<div class="empty-state">${esc(loadingText)}</div>`;
  card.scrollIntoView({ behavior: 'smooth', block: 'start' });
  return content;
}

async function previewHistory(itemId) {
  const content = showCard('history-card', 'history-content', 'Loading ABS listening sessions…');
  try {
    const d = await getJSON(`/api/history-preview/${encodeURIComponent(itemId)}`);
    document.getElementById('history-title').textContent = `History Preview — ${d.book.title}`;
    const s = d.summary;
    const rows = (d.days || []).map(day => `
      <tr>
        <td>${esc(day.date)}</td>
        <td>${day.session_count}</td>
        <td>${day.listening_minutes} min</td>
        <td>${day.end_position_minutes} min</td>
        <td>${pctText(day.progress_percent)}</td>
        <td>${flagBadges(day.flags)}</td>
      </tr>`).join('');

    const ci = CONFIDENCE_INFO[s.confidence] || { label: s.confidence, text: '' };

    const callouts = [];
    if (s.skipped_session_count) {
      callouts.push(`⚠ ${s.skipped_session_count} session${s.skipped_session_count === 1 ? '' : 's'} skipped — Audiobookshelf didn't include a usable date or position, so ${s.skipped_session_count === 1 ? 'it isn\'t' : 'they aren\'t'} counted above.`);
    }
    const gap = Math.round((s.total_listening_minutes - (s.latest_position_minutes || 0)) * 10) / 10;
    if (gap > 1) {
      callouts.push(`ℹ You listened to ${s.total_listening_minutes} min in total, but the furthest position only reached ${s.latest_position_minutes} min. The ${gap} min gap is most likely rewinds or re-listening to earlier parts, not lost progress.`);
    }

    content.innerHTML = `
      <div class="history-summary">
        <div class="history-stat"><strong>${s.session_count}</strong><span>ABS sessions</span></div>
        <div class="history-stat"><strong>${s.day_count}</strong><span>Listening days</span></div>
        <div class="history-stat"><strong>${s.total_listening_minutes} min</strong><span>Time listened</span></div>
        <div class="history-stat confidence-${esc(s.confidence)}"><strong>${esc(ci.label)}</strong><span>Preview confidence</span></div>
      </div>
      ${ci.text ? `<div class="history-callouts"><p class="history-note">${esc(ci.text)}</p>${callouts.map(c => `<p class="history-note">${esc(c)}</p>`).join('')}</div>` : ''}
      <div class="history-table-wrap">
        <table class="history-table">
          <thead><tr><th>Date</th><th>Sessions</th><th>Listened</th><th>End position</th><th>Progress</th><th>Notes</th></tr></thead>
          <tbody>${rows || '<tr><td colspan="6">No usable sessions found.</td></tr>'}</tbody>
        </table>
      </div>
      <div class="history-note">Read-only preview. Nothing on this screen is sent to StoryGraph.</div>`;
  } catch (e) {
    content.innerHTML = `<div class="empty-state">${esc(e.message || 'Could not load history')}</div>`;
  }
}

function closeHistory() {
  document.getElementById('history-card').style.display = 'none';
}

// ── History Import ───────────────────────────────────────────────────────

let importPreview = null;      // { itemId, data } for the currently open import card
let selectedDays = new Set();  // checkpoint keys ticked for import

function closeImport() {
  document.getElementById('import-card').style.display = 'none';
  importPreview = null;
  selectedDays = new Set();
}

// Shown even once an edition is matched: an auto-match can land on the
// wrong StoryGraph work, so the override must stay reachable.
function editionOverrideField(itemId, label) {
  return `
    <div class="field" style="margin-top:1rem">
      <label>${label}</label>
      <div class="input-row">
        <input type="text" id="manual-edition-url" placeholder="https://app.thestorygraph.com/books/...">
        <button class="btn btn-ghost" onclick="submitManualEdition(${jsArg(itemId)})">Use this</button>
      </div>
      <span class="hint">Pins this book to that edition for both Sync and Import History — useful when a title search lands on the wrong StoryGraph work (e.g. a split-up dramatized adaptation).</span>
    </div>`;
}

function importableDays(data) {
  return (data.days || []).filter(d => d.progress_percent != null && !d.already_imported && !d.already_logged_on_storygraph);
}

async function openImport(itemId) {
  const content = showCard('import-card', 'import-content', 'Loading ABS listening sessions and matching a StoryGraph edition…');
  try {
    const d = await getJSON(`/api/history-import-preview/${encodeURIComponent(itemId)}`);
    importPreview = { itemId, data: d };
    // Flagged days start unticked, so a rewind or suspicious jump has to be
    // opted into rather than waved through on an irreversible write.
    selectedDays = new Set(importableDays(d).filter(x => !x.flags?.length).map(x => x.key));
    document.getElementById('import-title').textContent = `Import History — ${d.book.title}`;
    renderImport();
  } catch (e) {
    content.innerHTML = `<div class="empty-state">${esc(e.message || 'Could not load import preview')}</div>`;
  }
}

function renderImport() {
  const { itemId, data } = importPreview;
  const content = document.getElementById('import-content');

  if (!data.matched_edition) {
    const candidateRows = (data.candidates || []).map(c => `
      <div class="edition-candidate">
        <div>
          <strong>${esc(c.title)}</strong><br>
          <span class="text-dim">${esc(c.format || '')}${c.duration_minutes ? ` · ${c.duration_minutes} min` : ''}${c.identifier ? ` · ${esc(c.identifier)}` : ''}</span>
        </div>
        <button class="btn btn-ghost" onclick="postEditionChoice(${jsArg(itemId)}, ${jsArg(c.storygraph_book_id)})">Use this edition</button>
      </div>`).join('');

    content.innerHTML = `
      <div class="history-note">Couldn't confidently match a StoryGraph audio edition for this book — nothing can be imported until one is selected. StoryGraph sometimes splits dramatized adaptations into separate "1 of 2" / "2 of 2" listings, which this can't guess between safely.</div>
      ${candidateRows ? `<div class="edition-candidates">${candidateRows}</div>` : '<div class="history-note" style="margin-top:0.75rem">No audio editions turned up in the search either.</div>'}
      ${editionOverrideField(itemId, 'Or paste a StoryGraph book URL or id')}`;
    return;
  }

  const days = data.days || [];
  const importable = importableDays(data);

  const importableKeys = new Set(importable.map(d => d.key));
  const rows = days.map(day => {
    let noteBadge;
    if (day.already_imported) noteBadge = '<span class="tag tag-ok">✓ Already imported</span>';
    else if (day.already_logged_on_storygraph) noteBadge = '<span class="tag tag-ok">✓ Already on StoryGraph</span>';
    else noteBadge = flagBadges(day.flags);
    const box = importableKeys.has(day.key)
      ? `<input type="checkbox" aria-label="Import ${esc(day.date)}" ${selectedDays.has(day.key) ? 'checked' : ''} onchange="toggleDay(${jsArg(day.key)}, this.checked)">`
      : '';
    return `
      <tr>
        <td>${box}</td>
        <td>${esc(day.date)}</td>
        <td>${day.end_position_minutes} min</td>
        <td>${pctText(day.progress_percent)}</td>
        <td>${noteBadge}</td>
      </tr>`;
  }).join('');

  const edition = data.matched_edition;
  content.innerHTML = `
    <div class="history-note">Matched edition: <strong>${esc(edition.title || '(untitled)')}</strong>${edition.format ? ` · ${esc(edition.format)}` : ''}${edition.duration_minutes ? ` · ${edition.duration_minutes} min` : ''} — <a href="https://app.thestorygraph.com/books/${encodeURIComponent(edition.storygraph_book_id)}" target="_blank" rel="noopener">verify on StoryGraph ↗</a></div>
    <div class="history-table-wrap" style="margin-top:0.75rem">
      <table class="history-table">
        <thead><tr><th></th><th>Date</th><th>Position</th><th>Progress</th><th>Status</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="5">No usable sessions found.</td></tr>'}</tbody>
      </table>
    </div>
    ${importable.length ? `<div class="text-dim" style="margin-top:0.5rem">
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
  return `Confirm & Import ${count} ${count === 1 ? 'entry' : 'entries'}`;
}

// Patch the button and note in place: a full re-render would drop the
// checkbox the user just clicked.
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
  selectedDays = on ? new Set(importableDays(importPreview.data).map(d => d.key)) : new Set();
  renderImport();
}

function importNote(count) {
  if (readOnlyMode) return 'Read-only development mode — importing is disabled.';
  if (!count) return 'Nothing selected — tick the days above to import them.';
  return `Writing ${count} new dated entr${count === 1 ? 'y' : 'ies'} can't be undone automatically — you'd need to delete them by hand on StoryGraph afterward.`;
}

function resultRow(status, title, detail = '') {
  return `
    <div class="result-row">
      <span class="badge ${esc(status)}">${esc(status.replace(/_/g, ' '))}</span>
      <span class="result-title">${esc(title)}</span>
      ${detail}
    </div>`;
}

async function confirmImport() {
  const { itemId } = importPreview;
  const review = document.getElementById('import-review');
  review.innerHTML = '<div class="empty-state">Writing to StoryGraph…</div>';
  try {
    // Keys only — the server rebuilds each checkpoint's date and percentage
    // from Audiobookshelf before writing anything.
    const result = await sendJSON(`/api/history-import/${encodeURIComponent(itemId)}`, { days: [...selectedDays] });
    const rows = (result.results || []).map(res => resultRow(
      res.status,
      res.date,
      res.reason ? `<span class="text-dim">${esc(res.reason.replace(/_/g, ' '))}</span>` : '',
    )).join('');
    review.innerHTML = `<div class="history-note">Imported ${result.imported} of ${result.total}. Re-open Import History to see updated status.</div>${rows}`;
  } catch (e) {
    review.innerHTML = `<div class="empty-state">${esc(e.message || 'Import failed')}</div>`;
  }
}

async function submitManualEdition(itemId) {
  const input = document.getElementById('manual-edition-url');
  await postEditionChoice(itemId, input.value.trim());
}

async function postEditionChoice(itemId, storygraphBookId) {
  const content = document.getElementById('import-content');
  content.innerHTML = '<div class="empty-state">Checking that edition…</div>';
  try {
    await sendJSON(`/api/history-import-edition/${encodeURIComponent(itemId)}`, { storygraph_book_id: storygraphBookId });
    openImport(itemId);
  } catch (e) {
    content.innerHTML = `<div class="empty-state">${esc(e.message || 'Could not use that edition')}</div>`;
  }
}

function setDot(dotId, lblId, ok, label) {
  document.getElementById(dotId).className = 'dot ' + (ok ? 'ok' : 'err');
  document.getElementById(lblId).textContent = label;
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
    fetchLogs(true);
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
    selectedScope = d.SYNC_SCOPE || 'in_progress';
    selectedMode = d.SYNC_MODE || 'frequent';
    document.getElementById('s-daily-time').value = d.DAILY_SYNC_TIME || '00:00';
    document.getElementById('timezone-hint').textContent = `Uses ${d.TIMEZONE || 'UTC'}.`;
    renderScope();
    renderSyncMode();
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
  payload.SYNC_SCOPE = selectedScope;
  payload.SYNC_MODE = selectedMode;
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
      </div>`).join('') || '<div class="empty-state">No users yet.</div>';
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

async function fetchLogs(force = false) {
  const box = document.getElementById('log-box');
  if (!box) return;  // only rendered for admins
  try {
    const d = await getJSON('/api/logs');
    if (d.logs.length === shownLogs && !force) return;
    const atBottom = box.scrollHeight - box.scrollTop <= box.clientHeight + 40;
    box.innerHTML = d.logs.map(l => `
      <div class="log-line">
        <span class="log-time">${esc(l.time)}</span>
        <span class="log-lvl ${esc(l.level)}">${esc(l.level.slice(0,4))}</span>
        <span class="log-msg ${l.level==='ERROR'?'err':''}">${esc(l.msg)}</span>
      </div>`).join('');
    shownLogs = d.logs.length;
    if (atBottom) box.scrollTop = box.scrollHeight;
  } catch (e) {
    console.error('Log refresh failed', e);
  }
}

function clearLogs() {
  document.getElementById('log-box').innerHTML = '';
  shownLogs = 0;
}

function toggleAuto(btn) {
  autoRefresh = !autoRefresh;
  btn.classList.toggle('active', autoRefresh);
  btn.textContent = autoRefresh ? 'Auto-refresh' : 'Auto-refresh (off)';
  if (autoRefresh) startTimer(); else clearInterval(timer);
}

function startTimer() {
  clearInterval(timer);
  timer = setInterval(() => { fetchStatus(); fetchLogs(); }, 5000);
}

// ── Toast ─────────────────────────────────────────────────────────────────

let toastTimer;
function toast(msg, type = 'ok') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = `show ${type}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.className = '', 3500);
}

function esc(s) {
  return String(s ?? '')
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;')
    .replace(/'/g,'&#39;');
}

// A value as a JS literal inside an HTML attribute, e.g. onclick="f(${jsArg(id)})".
function jsArg(value) {
  return esc(JSON.stringify(value));
}

// ── Init ──────────────────────────────────────────────────────────────────
fetchStatus();
fetchLogs();
loadSettings();
if (document.getElementById('users-list')) loadUsers();
startTimer();
