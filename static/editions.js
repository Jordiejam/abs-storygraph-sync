// The Editions page: every ABS book and the StoryGraph edition it belongs to.
// Lookups run one book at a time, only for the books a person ticks.

let books = [];                 // rows from /api/editions
let storygraphReady = false;
let filter = null;
let selected = new Set();       // item ids ticked for a lookup
let busy = new Set();           // item ids with a request in flight
let expanded = new Set();       // item ids with their edition picker open
// Rows touched on this visit stay visible under the current filter even after
// their state moves them out of it, so a finished lookup doesn't vanish.
let sticky = new Set();

const FILTERS = {
  review: { label: 'To review', match: b => b.state === 'suggested' || b.state === 'unmatched' },
  unchecked: { label: 'Not searched', match: b => b.state === 'unchecked' },
  confirmed: { label: 'Confirmed', match: b => b.state === 'confirmed' },
  all: { label: 'All', match: () => true },
};

function listeningRank(b) {
  if (b.is_finished) return 1;
  return b.progress_percent > 0 ? 0 : 2;
}

async function loadEditions() {
  try {
    const d = await getJSON('/api/editions');
    storygraphReady = d.storygraph_ready;
    books = d.books.sort((a, b) => listeningRank(a) - listeningRank(b) || a.title.localeCompare(b.title));
    const query = new URLSearchParams(location.search).get('q');
    if (query) {
      document.getElementById('edition-search').value = query;
      filter = 'all';
    } else if (!filter) {
      filter = books.some(FILTERS.review.match) ? 'review' : 'unchecked';
    }
    renderEditions();
  } catch (e) {
    document.getElementById('editions-list').innerHTML = `<div class="empty-state">${esc(e.message || 'Could not load your library')}</div>`;
  }
}

function setFilter(name) {
  filter = name;
  sticky = new Set();
  renderEditions();
}

function renderFilters() {
  document.getElementById('filter-group').innerHTML = Object.entries(FILTERS).map(([name, f]) => `
    <button type="button" class="btn btn-ghost ${name === filter ? 'active' : ''}" onclick="setFilter(${jsArg(name)})">
      ${esc(f.label)} <span class="count">${books.filter(f.match).length}</span>
    </button>`).join('');
}

function visibleBooks() {
  const text = document.getElementById('edition-search').value.trim().toLowerCase();
  return books.filter(b =>
    (FILTERS[filter].match(b) || sticky.has(b.abs_item_id))
    && (!text || `${b.title} ${b.author}`.toLowerCase().includes(text)));
}

function renderEditions() {
  renderFilters();
  const list = document.getElementById('editions-list');
  const shown = visibleBooks();
  const note = storygraphReady ? '' : '<div class="history-note">Add your StoryGraph session in Settings to search for editions.</div>';
  list.innerHTML = note + (shown.length
    ? shown.map(rowHtml).join('')
    : '<div class="empty-state">Nothing here.</div>');
  updateLookupButton();
}

function listeningTag(b) {
  if (b.is_finished) return '<span class="tag tag-ok">Finished</span>';
  if (b.progress_percent > 0) return `<span class="tag tag-warn">Listening · ${b.progress_percent}%</span>`;
  return '';
}

function editionSummary(b) {
  if (b.state === 'unchecked') {
    return b.storygraph_tag ? '<span class="text-dim">Tagged with a StoryGraph edition in Audiobookshelf — sync ABS tags to confirm it</span>' : '';
  }
  if (b.state === 'unmatched') {
    const n = b.candidates.length;
    return `<span class="text-dim">${n
      ? `No confident match · ${plural(n, 'candidate')} to choose from`
      : 'Nothing found on StoryGraph — try different search words'}</span>${matchReasonText(b.reason)}${readEditionNote(b.reason, pickFor(b))}`;
  }
  const e = b.edition;
  const warning = b.synced_book_id && b.synced_book_id !== e.storygraph_book_id
    ? `<div class="edition-warning">Sync has written progress to <a href="${storygraphUrl(b.synced_book_id)}" target="_blank" rel="noopener">a different edition ↗</a>. That progress stays there; only new progress goes to this one.</div>`
    : '';
  const tagDiffers = b.state === 'confirmed' && b.storygraph_tag && b.storygraph_tag !== e.storygraph_book_id
    ? `<div class="read-note">
        <div>Audiobookshelf has this book tagged with ${editionTitleLink({ storygraph_book_id: b.storygraph_tag }, 'a different edition')}.
          Using it confirms that edition here instead; keeping this one re-tags the book.</div>
        <div class="book-actions">
          <button class="btn btn-ghost" onclick="pickEdition(${jsArg(b.abs_item_id)}, ${jsArg(b.storygraph_tag)})">Use the tagged edition</button>
          <button class="btn btn-ghost" onclick="pickEdition(${jsArg(b.abs_item_id)}, ${jsArg(e.storygraph_book_id)})">Keep this one</button>
        </div>
      </div>`
    : '';
  return `
    <div class="edition-title">${editionTitleLink(e, editionFallbackTitle(e, b))}</div>
    <div class="text-dim">${editionDetails(e, b) || 'No format or runtime on file'}</div>
    ${b.state === 'suggested' ? matchReasonText(b.reason) + readEditionNote(b.reason, pickFor(b)) : ''}
    ${warning}${tagDiffers}`;
}

function pickFor(b) {
  return bookId => `pickEdition(${jsArg(b.abs_item_id)}, ${jsArg(bookId)})`;
}

function rowActions(b) {
  const id = jsArg(b.abs_item_id);
  if (busy.has(b.abs_item_id)) return '<div class="spinner"></div>';
  if (!storygraphReady) return '';
  const toggle = label => `<button class="btn btn-ghost" onclick="togglePicker(${id})">${expanded.has(b.abs_item_id) ? 'Close' : label}</button>`;
  switch (b.state) {
    case 'suggested':
      return `<button class="btn btn-primary" onclick="pickEdition(${id}, ${jsArg(b.edition.storygraph_book_id)})">Confirm</button>${toggle('Other…')}`;
    case 'unmatched':
      return toggle('Choose…');
    case 'confirmed':
      return toggle('Change…');
    default:
      return `<button class="btn btn-ghost" onclick="lookUp(${id})">Search</button>`;
  }
}

function pickerHtml(b) {
  const id = jsArg(b.abs_item_id);
  const others = otherCandidates(b.candidates, b.edition);
  const pick = pickFor(b);
  const inputId = `q-${b.abs_item_id}`;
  return `
    <div class="edition-picker">
      ${others.length
        ? `<div class="edition-candidates">${candidateRows(others, b, pick)}</div>`
        : `<div class="history-note">${b.state === 'unchecked' ? '' : 'No other editions came up in the last search.'}</div>`}
      <div class="field" style="margin-top:1rem">
        <label for="${esc(inputId)}">Search StoryGraph with different words</label>
        <div class="picker-row">
          <input type="text" id="${esc(inputId)}" value="${esc(`${b.title} ${b.author}`.trim())}">
          <button class="btn btn-ghost" onclick="lookUp(${id}, document.getElementById(${jsArg(inputId)}).value)">Search</button>
        </div>
        <span class="hint">Useful when the top search result is the wrong book (a different work or a series box set).</span>
      </div>
      ${pasteEditionField('pickEdition', b.abs_item_id, `u-${b.abs_item_id}`, 'Or paste a StoryGraph book URL or id')}
    </div>`;
}

function rowHtml(b) {
  const itemId = b.abs_item_id;
  const box = storygraphReady && !busy.has(itemId)
    ? `<input type="checkbox" aria-label="Select ${esc(b.title)}" ${selected.has(itemId) ? 'checked' : ''} onchange="toggleSelected(${jsArg(itemId)}, this.checked)">`
    : '';
  return `
    <div class="edition-row-wrap" data-item="${esc(itemId)}">
      <div class="edition-row">
        <div class="edition-check">${box}</div>
        <div class="edition-book">
          <div class="book-title">${esc(b.title)} ${listeningTag(b)}</div>
          <div class="book-author">${esc(b.author)}${b.duration_minutes ? ` · ${durationText(b.duration_minutes)}` : ''}${b.identifiers.length ? ` · ${esc(b.identifiers.join(', '))}` : ''}</div>
          ${b.narrators?.length || b.publisher ? `<div class="text-dim book-extra">${[
            b.narrators?.length ? `Narrated by ${esc(b.narrators.join(', '))}` : '',
            esc(b.publisher || ''),
          ].filter(Boolean).join(' · ')}</div>` : ''}
        </div>
        <div class="edition-status">${editionBadge(b.state)}<div>${editionSummary(b)}</div></div>
        <div class="edition-actions">${rowActions(b)}</div>
      </div>
      ${expanded.has(itemId) ? pickerHtml(b) : ''}
    </div>`;
}

// Re-render just one row, so typing in another row's picker survives.
function updateRow(itemId) {
  const el = document.querySelector(`[data-item="${CSS.escape(itemId)}"]`);
  const b = books.find(x => x.abs_item_id === itemId);
  if (el && b) el.outerHTML = rowHtml(b);
  renderFilters();
  updateLookupButton();
}

function replaceBook(itemId, changes) {
  const i = books.findIndex(x => x.abs_item_id === itemId);
  if (i >= 0) books[i] = { ...books[i], ...changes };
  sticky.add(itemId);
}

function toggleSelected(itemId, on) {
  if (on) selected.add(itemId); else selected.delete(itemId);
  updateLookupButton();
}

function togglePicker(itemId) {
  if (expanded.has(itemId)) expanded.delete(itemId); else expanded.add(itemId);
  updateRow(itemId);
}

function updateLookupButton() {
  const btn = document.getElementById('lookup-btn');
  const running = busy.size > 0;
  btn.disabled = running || !selected.size;
  btn.textContent = running ? 'Searching…' : `Search selected${selected.size ? ` (${selected.size})` : ''}`;
}

// Returns false when there's no point carrying on with other books (the
// StoryGraph session is bad).
async function lookUp(itemId, query = '') {
  busy.add(itemId);
  updateRow(itemId);
  try {
    const row = await sendJSON(`/api/editions/${encodeURIComponent(itemId)}/lookup`, { query });
    replaceBook(itemId, row);
    selected.delete(itemId);
    // A lookup that found nothing confident is only useful with the options open.
    if (row.state === 'unmatched') expanded.add(itemId);
    return true;
  } catch (e) {
    toast(`${books.find(b => b.abs_item_id === itemId)?.title || 'Book'}: ${e.message}`, 'err');
    return !/session/i.test(e.message);
  } finally {
    busy.delete(itemId);
    updateRow(itemId);
  }
}

// One book at a time, in list order, to stay gentle on StoryGraph.
async function lookUpSelected() {
  const queue = books.map(b => b.abs_item_id).filter(id => selected.has(id));
  for (const itemId of queue) {
    if (!await lookUp(itemId)) break;
  }
}

async function pickEdition(itemId, storygraphBookId) {
  busy.add(itemId);
  updateRow(itemId);
  try {
    const d = await confirmEdition(itemId, storygraphBookId);
    replaceBook(itemId, {
      state: d.state,
      edition: d.edition,
      ...(d.tag_error ? {} : { storygraph_tag: d.edition.storygraph_book_id }),
    });
    expanded.delete(itemId);
  } catch (e) {
    toast(e.message || 'Could not use that edition', 'err');
  } finally {
    busy.delete(itemId);
    updateRow(itemId);
  }
}

async function syncTags() {
  const btn = document.getElementById('tag-sync-btn');
  btn.disabled = true;
  btn.textContent = 'Syncing tags…';
  try {
    const d = await sendJSON('/api/editions/sync-tags', {});
    const parts = [
      d.confirmed && `${plural(d.confirmed, 'book')} confirmed from tags`,
      d.tagged && `${plural(d.tagged, 'book')} tagged in Audiobookshelf`,
      d.conflicts.length && `${plural(d.conflicts.length, 'book')} tagged with a different edition — see Confirmed`,
      d.untagged && (d.read_only ? `${d.untagged} not tagged in read-only mode` : d.tag_error),
    ].filter(Boolean);
    toast(parts.length ? parts.join(' · ') : '✓ Tags and confirmed editions already match',
      d.conflicts.length || d.untagged ? 'err' : 'ok');
    await loadEditions();
  } catch (e) {
    toast(e.message || 'Could not sync tags', 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Sync ABS tags';
  }
}

loadEditions();
