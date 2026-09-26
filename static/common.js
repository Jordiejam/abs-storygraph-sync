// Helpers shared by every page, including the edition rendering both pages use.

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
  if (!r.ok || d.error) {
    const err = new Error(d.error || `Request failed (HTTP ${r.status})`);
    err.data = d;
    throw err;
  }
  return d;
}

// ── Editions ─────────────────────────────────────────────────────────────

const EDITION_STATES = {
  unchecked: { label: 'Not searched', badge: 'skipped' },
  suggested: { label: 'Suggested', badge: 'not_found' },
  unmatched: { label: 'No match', badge: 'failed' },
  confirmed: { label: 'Confirmed', badge: 'success' },
  auto: { label: 'Auto-confirmed', badge: 'not_found' },
};

function editionBadge(state) {
  const info = EDITION_STATES[state] || EDITION_STATES.unchecked;
  return `<span class="badge ${info.badge}">${esc(info.label)}</span>`;
}

function storygraphUrl(bookId) {
  return `https://app.thestorygraph.com/books/${encodeURIComponent(bookId)}`;
}

function durationText(minutes) {
  if (!minutes) return '';
  const h = Math.floor(minutes / 60);
  const m = Math.round(minutes % 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}

function normaliseId(value) {
  return String(value || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
}

// "Audiobook · 10h 2m (+3 min) · ID match" for an edition, judged against the
// ABS book it might belong to.
function editionDetails(edition, book) {
  const parts = [];
  if (edition.format) parts.push(esc(edition.format));
  if (edition.duration_minutes) {
    let text = durationText(edition.duration_minutes);
    if (book?.duration_minutes) {
      const delta = Math.round(edition.duration_minutes - book.duration_minutes);
      text += delta ? ` (${delta > 0 ? '+' : ''}${delta} min)` : ' (same runtime)';
    }
    parts.push(text);
  }
  const ids = new Set((book?.identifiers || []).map(normaliseId));
  if (edition.identifier) {
    parts.push(ids.has(normaliseId(edition.identifier))
      ? `<span class="tag tag-ok">${esc(edition.identifier)} · ID match</span>`
      : esc(edition.identifier));
  }
  const tags = checkTags(edition);
  return parts.join(' · ') + (tags ? `<div class="check-tags">${tags}</div>` : '');
}

// ✓/✗ chips for edition_checks; unknown shows nothing, a shared-but-different
// narrator list shows amber.
function checkTags(edition) {
  const checks = edition.checks || {};
  const tag = (cls, mark, text, title = '') =>
    `<span class="tag ${cls}"${title ? ` title="${esc(title)}"` : ''}>${mark} ${esc(text)}</span>`;
  const pass = (ok, text, title = '') => tag(ok ? 'tag-ok' : 'tag-bad', ok ? '✓' : '✗', text, title);
  const tags = [];
  if (edition.read_by_you) tags.push('<span class="tag tag-read">📖 You\'ve read this</span>');
  // A long cast is cut short on the chip; hovering shows all of it.
  const allNarrators = (edition.narrators || []).join(', ');
  const shortNarrators = shortNameList(edition.narrators || []);
  const narrators = `Narrator: ${shortNarrators}`;
  const fullList = shortNarrators === allNarrators ? '' : allNarrators;
  if (checks.narrator && !checks.narrator_exact) {
    tags.push(tag('tag-warn', '~', narrators,
      `Shares a narrator with Audiobookshelf, but the lists differ${fullList ? `: ${fullList}` : ''}`));
  } else if (checks.narrator != null) {
    tags.push(pass(checks.narrator, narrators, fullList));
  }
  if (checks.publisher != null) tags.push(pass(checks.publisher, `Publisher: ${edition.publisher}`));
  if (checks.language === false) tags.push(pass(false, edition.language));
  return tags.join('');
}

// Whole names, up to about `maxChars`, then "+N more". Always at least one name.
function shortNameList(names, maxChars = 40) {
  let shown = 0;
  let length = 0;
  while (shown < names.length && (!shown || length + 2 + names[shown].length <= maxChars)) {
    length += (shown ? 2 : 0) + names[shown].length;
    shown++;
  }
  const rest = names.length - shown;
  return names.slice(0, shown).join(', ') + (rest ? ` +${rest} more` : '');
}

const READ_EDITION_FALLBACK = 'The edition you\'ve read';

function editionTitleLink(edition, fallback = 'Untitled edition') {
  return `<a href="${storygraphUrl(edition.storygraph_book_id)}" target="_blank" rel="noopener">${esc(edition.title || fallback)} ↗</a>`;
}

// What to call an edition StoryGraph gave no title for, on this ABS book.
function editionFallbackTitle(edition, book) {
  if (edition.storygraph_book_id === book?.storygraph_tag) return 'The edition tagged in Audiobookshelf';
  return edition.read_by_you ? READ_EDITION_FALLBACK : 'The edition earlier syncs used';
}

// The candidates worth listing beside `edition`: every one but itself.
function otherCandidates(candidates, edition) {
  return (candidates || []).filter(c => c.storygraph_book_id !== edition?.storygraph_book_id);
}

// A labelled text input with a button beside it. `onclickJs(valueJs)` is the
// button's onclick body, given a JS expression for the input's value.
function inputButtonField({ inputId, label, button, onclickJs, value = '', placeholder = '', hint = '', margin = '0.75rem' }) {
  return `
    <div class="field" style="margin-top:${margin}">
      <label for="${esc(inputId)}">${esc(label)}</label>
      <div class="picker-row">
        <input type="text" id="${esc(inputId)}" value="${esc(value)}" placeholder="${esc(placeholder)}">
        <button class="btn btn-ghost" onclick="${onclickJs(`document.getElementById(${jsArg(inputId)}).value.trim()`)}">${esc(button)}</button>
      </div>
      ${hint ? `<span class="hint">${esc(hint)}</span>` : ''}
    </div>`;
}

// A field for pasting any StoryGraph book URL or id. Its button calls the
// global function `pickFn(itemId, pasted)`.
function pasteEditionField(pickFn, itemId, inputId, label, hint = '') {
  return inputButtonField({
    inputId, label, hint, button: 'Use this',
    placeholder: 'https://app.thestorygraph.com/books/...',
    onclickJs: value => `${pickFn}(${jsArg(itemId)}, ${value})`,
  });
}

// `pickJs` is the onclick body for this candidate, given its book id.
function candidateRows(candidates, book, pickJs) {
  return candidates.map(c => `
    <div class="edition-candidate">
      <div>
        <strong>${editionTitleLink(c, READ_EDITION_FALLBACK)}</strong><br>
        <span class="text-dim">${editionDetails(c, book)}</span>
      </div>
      <button class="btn btn-ghost" onclick="${pickJs(c.storygraph_book_id)}">Use this edition</button>
    </div>`).join('');
}

function plural(n, word, pluralWord = `${word}s`) {
  return `${n} ${n === 1 ? word : pluralWord}`;
}

// Why the last lookup did or didn't match; `reason` is matcher.match_audio_edition's.
function matchReasonText(reason) {
  if (!reason) return '';
  const audio = plural(reason.audio_editions, 'audio edition');
  const off = Math.abs(reason.closest_delta_minutes);
  const direction = reason.closest_delta_minutes > 0 ? 'longer' : 'shorter';
  const lines = {
    tagged: reason.tagged_unlisted
      ? 'Audiobookshelf has this book tagged with this StoryGraph edition. It didn\'t come up in the search, so its format and runtime aren\'t shown.'
      : 'Audiobookshelf has this book tagged with this StoryGraph edition, from when an edition was last confirmed.',
    identifier: `Matched on ${reason.identifier}, the same ISBN/ASIN Audiobookshelf has.`,
    runtime: off
      ? `Runtime is ${off} min ${direction} than Audiobookshelf's (up to ${reason.tolerance_minutes} min counts as a match).`
      : 'Runtime matches Audiobookshelf\'s to the minute.',
    only_audio: 'The only audio edition found. Audiobookshelf has no runtime to check it against, so check it before confirming.',
    no_results: `StoryGraph's search for “${reason.query}” found nothing. Try different words below.`,
    language_mismatch: `StoryGraph's ${plural(reason.other_language_editions, 'audio edition')} ${reason.other_language_editions === 1 ? 'is' : 'are'} all in a different language from Audiobookshelf's ${reason.language}.`,
    no_audio: `StoryGraph found ${plural(reason.editions, 'edition')} for “${reason.query}”, but none are audio. The search may have landed on the wrong book.`,
    no_abs_runtime: `Audiobookshelf has no runtime for this book, so there's nothing to tell the ${audio} apart by.`,
    no_edition_runtime: `None of the ${audio} list a runtime on StoryGraph to compare with Audiobookshelf's ${durationText(reason.abs_runtime_minutes)}.`,
    runtime_mismatch: `The closest of the ${audio} is ${off} min ${direction} than Audiobookshelf's ${durationText(reason.abs_runtime_minutes)}; only ${reason.tolerance_minutes} min counts as a match.`,
  };
  let text = lines[reason.code] || '';
  if (reason.decided_by && reason.others_within_tolerance) {
    const others = plural(reason.others_within_tolerance, 'other edition');
    const similar = reason.code === 'identifier' ? 'with the same ISBN/ASIN' : 'with a similar runtime';
    const why = {
      read_before: 'you\'ve already read it on StoryGraph',
      narrator: 'the narrator matches Audiobookshelf\'s',
      narrator_exact: 'it lists exactly the narrators Audiobookshelf has',
      publisher: 'the publisher matches Audiobookshelf\'s',
    };
    // An exact narrator list implies a shared narrator, so say only the stronger.
    const decided = reason.decided_by.includes('narrator_exact')
      ? reason.decided_by.filter(name => name !== 'narrator')
      : reason.decided_by;
    text += decided.length
      ? ` Picked over ${others} ${similar} because ${decided.map(name => why[name]).join(' and ')}.`
      : ` Picked over ${others} ${similar} because ${reason.others_within_tolerance === 1 ? 'it lists' : 'they list'} a different narrator.`;
  } else if (reason.code === 'runtime' && reason.others_within_tolerance) {
    text += ` ${plural(reason.others_within_tolerance, 'other audio edition')} ${reason.others_within_tolerance === 1 ? 'is' : 'are'} also that close (often a different region's release), so check it's the one you use.`;
  }
  if (reason.audio_editions && !['identifier', 'tagged', 'no_results', 'no_audio'].includes(reason.code)) {
    text += reason.abs_has_identifier
      ? ' None of them carry the ISBN/ASIN Audiobookshelf has.'
      : ' Audiobookshelf has no ISBN/ASIN for this book to match on.';
  }
  const read = reason.read_edition;
  if (read?.fallback) {
    text += ' So this suggests the edition you\'ve already read on StoryGraph instead, which keeps your progress on the one entry you have.';
  } else if (read?.chosen && !reason.decided_by?.includes('read_before')) {
    text += ' It\'s also the edition you\'ve already read on StoryGraph.';
  }
  if (reason.other_language_editions && reason.code !== 'language_mismatch') {
    text += ` ${plural(reason.other_language_editions, 'audio edition')} in other languages ${reason.other_language_editions === 1 ? 'was' : 'were'} left out.`;
  }
  return text ? `<div class="match-reason">${esc(text)}</div>` : '';
}

// The edition you've read, when it isn't the suggestion: why not, and a way to
// use it anyway. `pickJs` is the onclick body for confirming a book id.
function readEditionNote(reason, pickJs) {
  const read = reason?.read_edition;
  if (!read || read.chosen || read.fallback) return '';
  const off = Math.abs(read.delta_minutes);
  const why = {
    not_listed: 'It wasn\'t among the editions StoryGraph returned, so it couldn\'t be compared.',
    not_audio: `It's the ${read.format} edition, not an audiobook.`,
    other_language: `It's in ${read.language}.`,
    identifier_elsewhere: 'The suggestion carries the ISBN/ASIN Audiobookshelf has.',
    tagged_elsewhere: 'Audiobookshelf has this book tagged with the suggestion instead.',
    no_runtime: 'StoryGraph doesn\'t list its runtime, so it can\'t be checked against Audiobookshelf\'s.',
    runtime_mismatch: `Its runtime is ${off} min ${read.delta_minutes > 0 ? 'longer' : 'shorter'} than Audiobookshelf's.`,
    outranked: read.narrator_check === false
      ? 'It lists a different narrator, so it\'s probably a different recording.'
      : 'Another edition matches Audiobookshelf\'s details more closely.',
  }[read.problem] || '';
  return `
    <div class="read-note">
      <div>📖 You've already read ${editionTitleLink(read, 'another edition')} on StoryGraph. ${esc(why)}
        Using it anyway keeps everything on the one StoryGraph entry you have.</div>
      <button class="btn btn-ghost" onclick="${pickJs(read.storygraph_book_id)}">Use the edition you've read</button>
    </div>`;
}

// A failed Audiobookshelf tag is only a warning; the confirmation stands.
async function confirmEdition(itemId, storygraphBookId) {
  const d = await sendJSON(`/api/editions/${encodeURIComponent(itemId)}/confirm`, { storygraph_book_id: storygraphBookId });
  toast(d.tag_error ? `✓ Edition confirmed. ${d.tag_error}.` : '✓ Edition confirmed', d.tag_error ? 'err' : 'ok');
  return d;
}

function emptyState(text) {
  return `<div class="empty-state">${esc(text)}</div>`;
}

// ── Toast ────────────────────────────────────────────────────────────────

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
