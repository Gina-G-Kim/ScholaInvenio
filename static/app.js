"use strict";

/* ------------------------------------------------------------------ */
/* State                                                               */
/* ------------------------------------------------------------------ */
const state = {
  sessionId: null,
  rounds: [],                 // round array from the server (includes papers)
  selected: null,             // { round, paperId }
  builderTarget: "main",      // "main" | "refine"
  refineDraft: { query: "", scope: "title_abstract" },
  sorts: {},                  // round number -> sort key
  config: { max_results: 1000, initial_batch: 50, page_batch: 50 },
  // Every provider is always queried (see providers.py's DEFAULT_PROVIDERS);
  // this is a post-search, client-side show/hide instead of a pre-search
  // opt-in. A source in here is hidden; a paper is hidden only once *all*
  // of its sources are (a paper both OpenAlex and DBLP found stays visible
  // if only DBLP is hidden).
  hiddenSources: new Set(),
  loadingMore: new Set(),     // round numbers currently mid-background-fetch
  fetchPace: {},              // round number -> { batch, endedAt, tookMs } -- see fetchMore's batch sizing
};

const PROVIDER_LABELS = { openalex: "OpenAlex", gscholar: "Google Scholar", dblp: "DBLP" };
const PROVIDER_SHORT = { openalex: "OA", gscholar: "GS", dblp: "DBLP" };

// sort by date and citation count
const SORTS = {
  relevance: { label: "Relevance (fetch order)", cmp: null },
  year_desc: { label: "Year ↓ newest", cmp: (a, b) => (b.year ?? -1) - (a.year ?? -1) },
  year_asc:  { label: "Year ↑ oldest", cmp: (a, b) => (a.year ?? 1e9) - (b.year ?? 1e9) },
  cited_desc:{ label: "Citations ↓ most", cmp: (a, b) => (b.cited_by ?? -1) - (a.cited_by ?? -1) },
  cited_asc: { label: "Citations ↑ fewest", cmp: (a, b) => (a.cited_by ?? 1e9) - (b.cited_by ?? 1e9) },
};

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};

/* ------------------------------------------------------------------ */
/* Shared helpers                                                      */
/* ------------------------------------------------------------------ */
async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await res.text();
  let payload = null;
  try { payload = text ? JSON.parse(text) : null; } catch { payload = null; }
  if (!res.ok) {
    const msg = (payload && (payload.detail || payload.message)) || text || `HTTP ${res.status}`;
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }
  return payload;
}

/* The gauge tracks real elapsed time instead of looping on its own --
   there's no true percentage for an in-flight fetch, but a width that
   actually grows with how long it's been waiting (plus the elapsed seconds
   in the label) reads as real status, not decoration unrelated to what's
   actually happening. Google Scholar's own per-result delay (see
   gscholar.py) is what usually makes this run long, so that's called out
   once it's been a while, rather than leaving it looking stuck. */
let busyTimer = null;
let busyStart = 0;
let busyLabel = "";

function busy(on, label) {
  const box = $("#busy");
  box.classList.toggle("hidden", !on);
  clearInterval(busyTimer);
  busyTimer = null;
  if (on) {
    busyStart = Date.now();
    busyLabel = label || "Working…";
    updateBusyStatus();
    busyTimer = setInterval(updateBusyStatus, 1000);
  } else {
    const fill = box.querySelector(".gauge-fill");
    if (fill) fill.style.width = "0%";
  }
}

function updateBusyStatus() {
  const elapsed = Math.max(0, Math.round((Date.now() - busyStart) / 1000));
  const fill = document.querySelector("#busy .gauge-fill");
  if (fill) {
    // Asymptotic, not linear -- there's no real total to divide by, so this
    // keeps growing but ever more slowly, reading as "still working" rather
    // than implying a fixed ETA it then blows past.
    fill.style.width = `${Math.min(92, 100 * (1 - Math.exp(-elapsed / 8)))}%`;
  }
  let text = busyLabel;
  if (elapsed >= 1) text += ` (${elapsed}s)`;
  if (elapsed >= 15) text += " — Google Scholar can take the longest of the three, hang tight.";
  $("#busyText").textContent = text;
}

let bannerTimer = null;
function banner(message, kind = "error", holdMs = 9000) {
  const node = $("#banner");
  node.textContent = message;
  node.classList.toggle("ok", kind === "ok");
  node.classList.remove("hidden");
  clearTimeout(bannerTimer);
  bannerTimer = setTimeout(() => node.classList.add("hidden"), holdMs);
}

function hideBanner() { $("#banner").classList.add("hidden"); }

/* ------------------------------------------------------------------ */
/* Keyword builder (spec 1.1, 2, 2.1)                                  */
/* ------------------------------------------------------------------ */
const BUILDER_KEYS = [
  "all_words", "exact_phrase", "or_terms", "exclude", "wildcard",
  "author", "intitle", "source", "year_from", "year_to",
];

function builderSpec() {
  const spec = {};
  for (const key of BUILDER_KEYS) {
    const input = $(`#builder [data-k="${key}"]`);
    const raw = input ? input.value.trim() : "";
    if (key === "year_from" || key === "year_to") {
      spec[key] = raw === "" ? null : parseInt(raw, 10);
      if (Number.isNaN(spec[key])) spec[key] = null;
    } else {
      spec[key] = raw;
    }
  }
  return spec;
}

let previewTimer = null;
let lastBuilt = { query: "", year_from: null, year_to: null };

function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(async () => {
    try {
      // syntax assembly happens entirely on the server (query.py) so the rules never fork.
      const out = await api("/api/build-query", {
        method: "POST",
        body: JSON.stringify(builderSpec()),
      });
      lastBuilt = out;
      $("#queryPreview").textContent = out.display || "—";
    } catch (err) {
      $("#queryPreview").textContent = `Error: ${err.message}`;
    }
  }, 220);
}

function openBuilder(target) {
  state.builderTarget = target;
  $("#builderTarget").textContent = target === "refine" ? "Refine (within the collected list)" : "Round 1";
  $("#builder").classList.remove("hidden");
  schedulePreview();
}

function closeBuilder() { $("#builder").classList.add("hidden"); }

function applyBuilder() {
  const query = lastBuilt.query || "";
  if (state.builderTarget === "refine") {
    state.refineDraft.query = query;
    const input = $("#refineQuery");
    if (input) input.value = query;
    const from = $("#refineFrom"), to = $("#refineTo");
    if (from) from.value = lastBuilt.year_from ?? "";
    if (to) to.value = lastBuilt.year_to ?? "";
  } else {
    $("#mainQuery").value = query;
    $("#yearFromMain") && ($("#yearFromMain").value = lastBuilt.year_from ?? "");
    state.mainYearFrom = lastBuilt.year_from ?? null;
    state.mainYearTo = lastBuilt.year_to ?? null;
  }
  closeBuilder();
}

function resetBuilder() {
  for (const key of BUILDER_KEYS) {
    const input = $(`#builder [data-k="${key}"]`);
    if (input) input.value = "";
  }
  schedulePreview();
}

/* ------------------------------------------------------------------ */
/* Round 1 search                                                      */
/* ------------------------------------------------------------------ */
async function runSearch() {
  const query = $("#mainQuery").value.trim();
  if (!query) { banner("Enter a search query."); return; }

  hideBanner();
  busy(true, "Fetching from every source…");
  try {
    const out = await api("/api/search", {
      method: "POST",
      body: JSON.stringify({
        query,
        year_from: state.mainYearFrom ?? null,
        year_to: state.mainYearTo ?? null,
        // no `max_results` -- the server's own INITIAL_BATCH keeps the first
        // response quick; scrolling near the bottom of the list fetches
        // further batches on its own (see maybeLoadMore below).
        fetch_abstracts: $("#fetchAbstracts").checked,
        // no `providers` field -- the server always queries every registered
        // provider (see providers.py's DEFAULT_PROVIDERS); which ones show up
        // afterward is a display choice, not a fetch-time one (see the
        // source-tag toggles rendered per round).
      }),
    });
    state.sessionId = out.session_id;
    state.rounds = [out.round];
    state.selected = null;
    state.sorts = {};
    state.refineDraft = { query: "", scope: "title_abstract" };
    history.replaceState(null, "", `?session=${encodeURIComponent(out.session_id)}`);
    render(true);
    const note = out.round.notes ? ` ${out.round.notes}` : "";
    banner(`Round 1 complete — ${out.round.count} results, saved to round_01.xml.${note}`, "ok", 14000);
  } catch (err) {
    banner(err.message);
  } finally {
    busy(false);
  }
}

/* clicking the logo/name returns to a blank session (home) */
function goHome() {
  state.sessionId = null;
  state.rounds = [];
  state.selected = null;
  state.sorts = {};
  state.refineDraft = { query: "", scope: "title_abstract" };
  state.mainYearFrom = null;
  state.mainYearTo = null;
  $("#mainQuery").value = "";
  closeBuilder();
  resetBuilder();
  hideBanner();
  $("#sessions").classList.add("hidden");
  history.replaceState(null, "", location.pathname);
  render();
}

/* ------------------------------------------------------------------ */
/* Load more -- fetched automatically as the list is scrolled, in the  */
/* background, rather than a manual "load more" button (spec 7 update) */
/* ------------------------------------------------------------------ */
// Scaled to the list's own visible height rather than a fixed pixel count,
// so the prefetch starts a consistent "about one screen's worth" early
// regardless of row height or window size -- fetching only starts once
// you're within one clientHeight of the bottom, well before you can
// actually get there, so on ordinary scroll speeds the batch has already
// landed by the time you arrive and there's nothing to wait for.
const PREFETCH_SCREENS = 1;

function distanceFromBottom(listEl) {
  return listEl.scrollHeight - listEl.scrollTop - listEl.clientHeight;
}

function pastPrefetchThreshold(listEl) {
  return distanceFromBottom(listEl) < listEl.clientHeight * PREFETCH_SCREENS;
}

// Not "close" -- physically can't scroll any further. Separate from the
// prefetch trigger above on purpose: prefetching should stay invisible
// under normal scrolling, and only surface the loading gauge for the
// (fast-scroll) case where you actually outrun it and hit the true edge
// while it's still in flight.
function isAtBottom(listEl) {
  return distanceFromBottom(listEl) <= 2;
}

/* The loading gauge is only ever meant to be looked at while you're at the
   bottom checking on it -- if you scroll back up mid-fetch to read
   something, it retracts out of view (still loading in the background,
   nothing pinned in your way) and slides back in if you return to the
   bottom before it finishes. It stays in the DOM the whole time; only a
   class toggles, so this never fights with patchColumnAppend/removeLoadingRow. */
function setLoadingRowVisible(listEl, visible) {
  listEl.querySelector(".scroll-loading")?.classList.toggle("visible", visible);
}

function removeLoadingRow(listEl) {
  const row = listEl.querySelector(".scroll-loading");
  if (!row) return;
  row.classList.remove("visible");
  row._removeTimer = setTimeout(() => row.remove(), 260);   // matches the CSS slide-down transition
}

/* Get-or-create: reuses an existing row instead of appending a second one.
   Without this, a fetch that chains straight into another (checkAutoLoadMore
   firing right after a successful patch, the same tick removeLoadingRow's
   own removal is still deferred in) appended a brand new row while the
   previous one's slide-down animation hadn't actually removed it from the
   DOM yet -- two rows, one list, confirmed live by scrolling up and down
   repeatedly during a long background fetch. Cancels any pending removal
   from a prior call so that timer can't yank the row out from under this
   fetch once it eventually fires. */
function ensureLoadingRow(listEl) {
  let row = listEl.querySelector(".scroll-loading");
  if (row) {
    clearTimeout(row._removeTimer);
  } else {
    row = buildLoadingRow();
  }
  listEl.append(row);   // (re-)pin it at the very end, past any newly patched items
  return row;
}

/* A round whose results don't fill the visible list at all has nothing to
   scroll -- 'scroll' never fires, so maybeLoadMore would never get a
   chance to run (confirmed: a query only OpenAlex answered, with DBLP and
   Scholar both down, left a 5-result list far shorter than the column,
   permanently stuck). This drives the same fetch without waiting for a
   scroll event whenever that's the situation. Stops on its own once either
   the list actually overflows or a fetch adds nothing new. */
function checkAutoLoadMore(round, listEl) {
  if (!round.has_more || state.loadingMore.has(round.number)) return;
  if (listEl.scrollHeight > listEl.clientHeight + 2) return;   // already scrollable -- let the user drive it
  fetchMore(round.number, { silent: true });
}

/* Appends only the newly-fetched papers to an already-rendered list,
   in place, instead of the usual full render() teardown-and-rebuild.
   This matters specifically because the fetch is triggered *while the
   user is still scrolling* -- a real (trackpad/wheel) scroll gesture is
   still in flight at that moment, and replacing the very element the
   gesture is scrolling out from under it (render() wipes and rebuilds
   #columns from scratch) drops the rest of that gesture, which is
   exactly what made this feel broken. */
function patchColumnAppend(round, previousRound, listEl) {
  const alreadyShown = new Set(previousRound.papers.map((p) => p.id));
  for (const paper of visiblePapers(round.papers)) {
    if (!alreadyShown.has(paper.id)) listEl.append(buildItem(round, paper));
  }
  const countEl = listEl.closest(".col")?.querySelector(".col-count");
  if (countEl) {
    const shownCount = visiblePapers(round.papers).length;
    countEl.textContent = shownCount === round.count ? `${round.count}` : `${shownCount} of ${round.count}`;
  }
}

// If the previous fetch for this round is still within its own duration
// (i.e. the next batch was already needed before the last one even had time
// to be requested again), scrolling is outrunning the default batch size --
// the next call asks for more up front so a long fast scroll settles into
// fewer, bigger requests instead of a rapid chain of small ones. A calmer
// gap resets back to the configured default, since ordinary browsing should
// stay on small, quick calls rather than always over-fetching.
const MAX_BATCH_MULTIPLIER = 8;

function nextBatchSize(number) {
  const pace = state.fetchPace[number];
  const base = state.config.page_batch;
  if (!pace || performance.now() - pace.endedAt >= pace.tookMs) return base;
  return Math.min(pace.batch * 2, base * MAX_BATCH_MULTIPLIER);
}

async function fetchMore(number, { silent = false } = {}) {
  if (state.loadingMore.has(number)) return;   // already fetching this round
  state.loadingMore.add(number);
  if (!silent) busy(true, "Fetching more…");

  // In-place patching only works for the default (fetch/relevance) order --
  // any other sort needs the whole list re-ordered, not just appended to.
  const patchable = (state.sorts[number] || "relevance") === "relevance";
  let listEl = patchable ? document.querySelector(`.col-list[data-round="${number}"]`) : null;
  if (listEl) {
    const row = ensureLoadingRow(listEl);
    // Only surface it if you're already at the true bottom waiting -- see
    // pastPrefetchThreshold/isAtBottom above for why this normally starts
    // well before that and finishes invisibly.
    if (isAtBottom(listEl)) row.classList.add("visible");
  } else {
    render();
  }

  const batch = nextBatchSize(number);
  const startedAt = performance.now();
  try {
    const out = await api(`/api/session/${state.sessionId}/round/${number}/more`, {
      method: "POST",
      body: JSON.stringify({ batch }),
    });
    state.fetchPace[number] = { batch, endedAt: performance.now(), tookMs: performance.now() - startedAt };
    const idx = state.rounds.findIndex((r) => r.number === number);
    const previousRound = idx >= 0 ? state.rounds[idx] : null;
    if (idx >= 0) state.rounds[idx] = out.round;
    // rounds derived from this one were deleted by the server.
    const dropped = out.dropped_rounds && out.dropped_rounds.length;
    if (dropped) {
      state.rounds = state.rounds.filter((r) => !out.dropped_rounds.includes(r.number));
    }
    state.loadingMore.delete(number);

    listEl = patchable && !dropped ? document.querySelector(`.col-list[data-round="${number}"]`) : null;
    if (listEl && previousRound) {
      removeLoadingRow(listEl);
      patchColumnAppend(out.round, previousRound, listEl);
      if (out.added > 0) checkAutoLoadMore(out.round, listEl);
    } else {
      render();
    }

    if (!silent) {
      const droppedMsg = dropped
        ? ` Later rounds (${out.dropped_rounds.join(", ")}) were removed since their basis changed.` : "";
      banner(`+${out.added} results — ${out.round.count} total, saved to round_${String(number).padStart(2,"0")}.xml.${droppedMsg}`,
             "ok", 12000);
    }
  } catch (err) {
    state.loadingMore.delete(number);
    const stillThere = patchable ? document.querySelector(`.col-list[data-round="${number}"]`) : null;
    if (stillThere) {
      removeLoadingRow(stillThere);
    } else {
      render();
    }
    // A background fetch failing quietly is better than interrupting
    // scrolling -- has_more is untouched, so the next scroll near the
    // bottom just tries again.
    if (!silent) banner(err.message);
  } finally {
    if (!silent) busy(false);
  }
}

/* Called on scroll; fetches the next batch once the list is scrolled
   near its bottom, if this round has more and isn't already fetching. */
function maybeLoadMore(round) {
  if (!round.has_more || state.loadingMore.has(round.number)) return;
  fetchMore(round.number, { silent: true });
}

/* ------------------------------------------------------------------ */
/* Round n search (spec 5, 6)                                          */
/* ------------------------------------------------------------------ */
async function runRefine() {
  const input = $("#refineQuery");
  const query = input ? input.value.trim() : "";
  const from = $("#refineFrom") ? $("#refineFrom").value.trim() : "";
  const to = $("#refineTo") ? $("#refineTo").value.trim() : "";

  if (!query && !from && !to) {
    banner("Enter a keyword or a date range to refine by.");
    return;
  }

  busy(true, "Filtering the collected list…");
  try {
    const out = await api(`/api/session/${state.sessionId}/refine`, {
      method: "POST",
      body: JSON.stringify({
        query,
        scope: state.refineDraft.scope,
        year_from: from === "" ? null : parseInt(from, 10),
        year_to: to === "" ? null : parseInt(to, 10),
      }),
    });
    state.rounds.push(out.round);
    state.refineDraft = { query: "", scope: state.refineDraft.scope };
    render(true);
    const file = `round_${String(out.round.number).padStart(2, "0")}.xml`;
    banner(`Round ${out.round.number} complete — ${out.round.count} results, saved to ${file}.`, "ok");
  } catch (err) {
    banner(err.message);
  } finally {
    busy(false);
  }
}

/* ------------------------------------------------------------------ */
/* Rollback (spec 7.3, 7.3.1)                                          */
/* ------------------------------------------------------------------ */
async function rollback(number) {
  const trailing = state.rounds.filter((r) => r.number >= number).map((r) => `round ${r.number}`);
  const label = trailing.join(", ");
  const question = number === 1
    ? `Deleting round 1 removes every round in this session (${label}) along with its XML files. Continue?`
    : `This deletes the XML for ${label} and rolls back to round ${number - 1}. Continue?`;
  if (!confirm(question)) return;

  busy(true, "Deleting XML…");
  try {
    const out = await api(`/api/session/${state.sessionId}/round/${number}`, { method: "DELETE" });
    if (out.session) {
      state.rounds = out.session.rounds;
    } else {
      state.sessionId = null;
      state.rounds = [];
    }
    state.selected = null;
    render(true);
    banner(`Deleted: ${out.removed.join(", ") || "nothing"}`, "ok", 6000);
  } catch (err) {
    banner(err.message);
  } finally {
    busy(false);
  }
}

/* ------------------------------------------------------------------ */
/* Rendering                                                           */
/* ------------------------------------------------------------------ */
function scopeLabel(scope) {
  if (scope === "title") return "title only";
  if (scope === "title_abstract") return "title + abstract";
  return "whole record";
}

function periodLabel(round) {
  if (round.year_from == null && round.year_to == null) return "";
  return `${round.year_from ?? ""}~${round.year_to ?? ""}`;
}

function render(scrollToEnd = false) {
  const wrap = $("#columns");

  // A background (scroll-triggered) fetch rebuilds the same round's column
  // in place -- without this, the list would jump back to the top on every
  // append, which defeats the point of loading more as the user scrolls.
  const scrollPositions = new Map();
  wrap.querySelectorAll(".col-list[data-round]").forEach((list) => {
    scrollPositions.set(list.dataset.round, list.scrollTop);
  });

  wrap.innerHTML = "";

  if (!state.rounds.length) {
    const empty = el("div", "empty-state");
    empty.append(
      el("h2", null, "Start with a search"),
      el("p", null, "Round 1 results are saved as XML, and round-by-round lists pile up here from the left."),
    );
    wrap.append(empty);
    renderDetail(null);
    return;
  }

  state.rounds.forEach((round, index) => {
    wrap.append(buildColumn(round, index === state.rounds.length - 1));
  });

  wrap.querySelectorAll(".col-list[data-round]").forEach((list) => {
    const prev = scrollPositions.get(list.dataset.round);
    if (prev) list.scrollTop = prev;
  });

  // scroll to the right only when a genuinely new round was just added (spec 7) --
  // not on every background append, which would yank a mid-scroll user sideways.
  if (scrollToEnd) {
    requestAnimationFrame(() => { wrap.scrollLeft = wrap.scrollWidth; });
  }
  renderDetail(currentPaper());

  // A round short enough to not overflow its own column has nothing to
  // scroll, so the listener above would never get a chance to fire --
  // drive the same fetch directly instead of leaving it stuck.
  state.rounds.forEach((round) => {
    const list = wrap.querySelector(`.col-list[data-round="${round.number}"]`);
    if (list) checkAutoLoadMore(round, list);
  });
}

function visiblePapers(papers) {
  if (!state.hiddenSources.size) return papers;
  // hidden only once *every* source that found this paper is hidden
  return papers.filter((p) => !(p.sources || []).every((s) => state.hiddenSources.has(s)));
}

function buildItem(round, paper) {
  const item = el("div", "item");
  if (paper.sources && paper.sources.length) {
    const srcs = el("span", "srcs");
    paper.sources.forEach((name) => {
      srcs.append(el("span", `srcbadge ${name}`, PROVIDER_SHORT[name] || name));
    });
    item.append(srcs);
  }
  if (paper.year) item.append(el("span", "yr", `${paper.year}`));
  item.append(document.createTextNode(paper.title));
  if (state.selected && state.selected.round === round.number && state.selected.paperId === paper.id) {
    item.classList.add("active");
  }
  item.addEventListener("click", () => {
    state.selected = { round: round.number, paperId: paper.id };
    document.querySelectorAll(".item.active").forEach((n) => n.classList.remove("active"));
    item.classList.add("active");
    renderDetail(paper);
  });
  return item;
}

function buildLoadingRow() {
  const loadingRow = el("div", "scroll-loading");
  const gauge = el("div", "gauge");
  gauge.append(el("div", "gauge-fill"));
  loadingRow.append(gauge);
  return loadingRow;
}

function buildColumn(round, isLast) {
  const col = el("div", "col");
  const shown = visiblePapers(round.papers);

  /* --- header: round / count / search keyword (7.2) / x (7.3.1) --- */
  const head = el("div", "col-head");
  const row1 = el("div", "row1");
  row1.append(el("span", "col-badge", `Round ${round.number}`));
  row1.append(el("span", "col-count",
    shown.length === round.count ? `${round.count}` : `${shown.length} of ${round.count}`));
  head.append(row1);

  if (round.label) head.append(el("div", "col-label", round.label));
  const q = el("div", "col-q", round.query || "(no keywords — date range only)");
  head.append(q);

  const bits = [scopeLabel(round.scope)];
  const period = periodLabel(round);
  if (period) bits.push(`range ${period}`);
  bits.push(round.source_round === 0 ? "fetched" : `filtered from round ${round.source_round}`);
  head.append(el("div", "col-meta", bits.join(" · ")));

  if (round.providers && round.providers.length) {
    const provs = el("div", "col-prov");
    provs.append(el("span", "muted", "Sources (click to show/hide): "));
    round.providers.forEach((name) => {
      const tag = el("button", `srcbadge toggle ${name}`, PROVIDER_SHORT[name] || name);
      tag.title = `${PROVIDER_LABELS[name] || name} — click to ${state.hiddenSources.has(name) ? "show" : "hide"}`;
      tag.classList.toggle("off", state.hiddenSources.has(name));
      tag.addEventListener("click", () => {
        if (state.hiddenSources.has(name)) state.hiddenSources.delete(name);
        else state.hiddenSources.add(name);
        render();
      });
      provs.append(tag);
    });
    head.append(provs);
  }

  // sort by date and citation count
  const sortRow = el("div", "col-sort");
  sortRow.append(el("span", "muted", "Sort"));
  const sortSel = el("select");
  for (const [key, def] of Object.entries(SORTS)) {
    const opt = el("option", null, def.label);
    opt.value = key;
    if ((state.sorts[round.number] || "relevance") === key) opt.selected = true;
    sortSel.append(opt);
  }
  sortSel.addEventListener("change", () => {
    state.sorts[round.number] = sortSel.value;
    render();
  });
  sortRow.append(sortSel);
  head.append(sortRow);

  const close = el("button", "col-x", "×");
  close.title = `Roll back round ${round.number} (deletes its XML)`;
  close.addEventListener("click", () => rollback(round.number));
  head.append(close);
  col.append(head);

  /* --- list: titles only (spec 4) --- */
  const list = el("div", "col-list");
  list.dataset.round = String(round.number);

  // Beyond the first batch, more is fetched automatically well before this
  // list is scrolled to its actual bottom (see pastPrefetchThreshold), so it
  // normally finishes before you get there. The loading gauge only shows up
  // if you outscroll it and hit the true bottom (isAtBottom) while it's
  // still in flight -- fetchMore's own trigger order matters here (start the
  // fetch first) so a fetch that begins right at the true edge shows the
  // gauge immediately instead of on the next scroll tick.
  list.addEventListener("scroll", () => {
    if (pastPrefetchThreshold(list)) maybeLoadMore(round);
    setLoadingRowVisible(list, isAtBottom(list));
  });

  if (!shown.length) {
    list.append(el("div", "item-empty",
      round.papers.length ? "Every result is hidden by the source filters above." : "No results."));
  }
  const cmp = (SORTS[state.sorts[round.number] || "relevance"] || {}).cmp;
  const ordered = cmp ? [...shown].sort(cmp) : shown;
  for (const paper of ordered) {
    list.append(buildItem(round, paper));
  }
  // Shown only while a background batch is actually in flight -- nothing
  // sits at the bottom of the list otherwise, so scrolling near it and then
  // stopping (no more to fetch, or already caught up) leaves just the list.
  if (state.loadingMore.has(round.number)) {
    const row = buildLoadingRow();
    if (isAtBottom(list)) row.classList.add("visible");
    list.append(row);
  }
  col.append(list);

  /* --- footer: next-round search, only on the last round --- */
  const foot = el("div", "col-foot");
  const dl = el("a", "dl", "⤓ Download XML");
  dl.href = `/api/session/${state.sessionId}/round/${round.number}/xml`;

  if (isLast) {
    foot.append(el("span", "lbl", `Round ${round.number + 1} — searches within this list only`));

    const row = el("div", "refine-row");
    const input = el("input");
    input.type = "text";
    input.id = "refineQuery";
    input.placeholder = 'Keyword syntax works ("..", OR, -, *, author:)';
    input.value = state.refineDraft.query;
    input.addEventListener("input", () => { state.refineDraft.query = input.value; });
    input.addEventListener("keydown", (ev) => { if (ev.key === "Enter") runRefine(); });

    const gear = el("button", "btn ghost sm", "⚙");
    gear.title = "Keyword builder";
    gear.addEventListener("click", () => openBuilder("refine"));
    row.append(input, gear);
    foot.append(row);

    /* spec 5.2 — scope selector as checkboxes */
    const scopes = el("div", "scopes");
    const mk = (value, text) => {
      const label = el("label");
      const box = el("input");
      box.type = "checkbox";
      box.checked = state.refineDraft.scope === value;
      box.addEventListener("change", () => {
        state.refineDraft.scope = value;  // the two options are mutually exclusive
        scopes.querySelectorAll("input").forEach((other) => {
          other.checked = other === box;
        });
      });
      label.append(box, document.createTextNode(text));
      return label;
    };
    scopes.append(mk("title", "Title only"), mk("title_abstract", "Title + abstract only"));
    foot.append(scopes);

    const period = el("div", "refine-row");
    const from = el("input"); from.type = "text"; from.id = "refineFrom"; from.placeholder = "From year";
    const to = el("input"); to.type = "text"; to.id = "refineTo"; to.placeholder = "To year";
    period.append(from, to);
    foot.append(period);

    const actions = el("div", "foot-actions");
    const go = el("button", "btn primary sm", `Search round ${round.number + 1}`);
    go.addEventListener("click", runRefine);
    actions.append(go, dl);
    foot.append(actions);
  } else {
    const actions = el("div", "foot-actions");
    actions.append(dl);
    foot.append(actions);
  }
  col.append(foot);

  return col;
}

function currentPaper() {
  if (!state.selected) return null;
  const round = state.rounds.find((r) => r.number === state.selected.round);
  if (!round) return null;
  return round.papers.find((p) => p.id === state.selected.paperId) || null;
}

/* spec 4.1 — a side panel shows the full record, not a popup */
function renderDetail(paper) {
  const pane = $("#detail");
  pane.innerHTML = "";

  if (!paper) {
    const empty = el("div", "detail-empty");
    empty.innerHTML = "<p>Click a paper's title<br />to see its full record here.</p>";
    pane.append(empty);
    return;
  }

  if (paper.sources && paper.sources.length) {
    const srcs = el("div", "srcs");
    paper.sources.forEach((name) => {
      srcs.append(el("span", `srcbadge ${name}`, PROVIDER_LABELS[name] || name));
    });
    pane.append(srcs);
  }
  pane.append(el("h2", null, paper.title));

  const field = (key, build) => {
    const box = el("div", "dfield");
    box.append(el("div", "k", key));
    const value = el("div", "v");
    build(value);
    box.append(value);
    pane.append(box);
    return box;
  };

  field("Authors", (v) => {
    if (!paper.authors.length) { v.append(el("span", "muted", "Unknown")); return; }
    v.className = "v authors";
    paper.authors.forEach((name) => v.append(el("span", null, name)));
  });

  field("Publisher", (v) => { v.textContent = paper.publisher || "Unknown"; });
  field("Venue", (v) => { v.textContent = paper.venue || "Unknown"; });
  field("Year", (v) => { v.textContent = paper.year ?? "Unknown"; });

  if (paper.doi) {
    field("DOI", (v) => {
      const a = el("a", null, paper.doi);
      a.href = `https://doi.org/${paper.doi}`;
      a.target = "_blank"; a.rel = "noopener noreferrer";
      v.append(a);
    });
  }

  const abs = field("Abstract", (v) => {
    v.className = "v abs";
    v.textContent = paper.abstract || "Not collected";
  });
  if (paper.abstract_source) {
    const ABS_LABEL = { api: "from API", fulltext: "extracted from source", snippet: "Scholar snippet" };
    const tag = el("span", "tag", ABS_LABEL[paper.abstract_source] || paper.abstract_source);
    abs.querySelector(".k").append(tag);
  }

  if (paper.cited_by != null) {
    field("Citations", (v) => { v.textContent = `${paper.cited_by}`; });
  }

  field("Links", (v) => {
    v.className = "v linklist";
    const names = { primary: "Source", pdf: "PDF", citations: "Cited by", versions: "All versions", related: "Related" };
    const entries = Object.entries(paper.links || {});
    if (!entries.length) { v.append(el("span", "muted", "None")); return; }
    for (const [rel, href] of entries) {
      const a = el("a", null, `${names[rel] || rel} — ${href}`);
      a.href = href;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      v.append(a);
    }
  });

  field("Record ID", (v) => { v.textContent = paper.id; v.className = "v muted"; });
}

/* ------------------------------------------------------------------ */
/* Saved sessions                                                      */
/* ------------------------------------------------------------------ */
async function showSessions() {
  const panel = $("#sessions");
  panel.classList.remove("hidden");
  const list = $("#sessionsList");
  list.innerHTML = "<div class='srow muted'>Loading…</div>";
  try {
    const out = await api("/api/sessions");
    list.innerHTML = "";
    if (!out.sessions.length) {
      list.append(el("div", "srow muted", "No saved sessions."));
      return;
    }
    for (const item of out.sessions) {
      const row = el("div", "srow");
      row.append(el("div", "slabel", item.label || item.query || item.id));
      if (item.query && item.query !== item.label) {
        row.append(el("div", "sq", item.query));
      }
      row.append(el("div", "sm2", `${item.rounds} round(s) · ${item.total} results · ${item.created_at.slice(0, 19)}`));
      row.addEventListener("click", () => loadSession(item.id));

      // clicking x deletes the whole session directory immediately, no confirmation
      const x = el("button", "srow-x", "×");
      x.title = "Delete this session (immediately, no confirmation)";
      x.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        row.remove();
        try {
          await api(`/api/session/${item.id}`, { method: "DELETE" });
          if (state.sessionId === item.id) goHome();
        } catch (err) {
          banner(err.message);
        }
      });
      row.append(x);
      list.append(row);
    }
  } catch (err) {
    list.innerHTML = "";
    list.append(el("div", "srow muted", err.message));
  }
}

async function loadSession(sessionId, { isInitialLoad = false } = {}) {
  busy(true, "Loading session…");
  try {
    const out = await api(`/api/session/${sessionId}`);
    state.sessionId = out.id;
    state.rounds = out.rounds;
    state.selected = null;
    state.sorts = {};
    $("#mainQuery").value = out.rounds.length ? out.rounds[0].query : "";
    state.mainYearFrom = out.rounds.length ? out.rounds[0].year_from : null;
    state.mainYearTo = out.rounds.length ? out.rounds[0].year_to : null;
    $("#sessions").classList.add("hidden");
    // keeping the session in the URL lets a refresh or bookmark come back to it.
    history.replaceState(null, "", `?session=${encodeURIComponent(out.id)}`);
    render(true);
  } catch (err) {
    if (isInitialLoad && err.status === 404) {
      // The URL still names a session from an earlier visit (bookmark, a
      // stale tab left open) that's since been deleted -- greeting a user
      // who hasn't done anything yet with a red error is worse than just
      // quietly landing on the normal empty state.
      history.replaceState(null, "", location.pathname);
      render();
    } else {
      banner(err.message);
    }
  } finally {
    busy(false);
  }
}

/* ------------------------------------------------------------------ */
/* Event wiring                                                        */
/* ------------------------------------------------------------------ */
$("#builderToggle").addEventListener("click", () => {
  const hidden = $("#builder").classList.contains("hidden");
  if (hidden) openBuilder("main"); else closeBuilder();
});
$("#builderClose").addEventListener("click", closeBuilder);
$("#builderApply").addEventListener("click", applyBuilder);
$("#builderReset").addEventListener("click", resetBuilder);
$("#builder").addEventListener("input", schedulePreview);

$("#runSearch").addEventListener("click", runSearch);
$("#mainQuery").addEventListener("keydown", (ev) => { if (ev.key === "Enter") runSearch(); });

$("#sessionsBtn").addEventListener("click", showSessions);
$("#sessionsClose").addEventListener("click", () => $("#sessions").classList.add("hidden"));

state.mainYearFrom = null;
state.mainYearTo = null;

$("#homeBtn").addEventListener("click", goHome);

// the batch sizes are decided server-side; hardcoding them on the frontend would let the two drift apart.
api("/api/config").then((cfg) => {
  state.config = cfg;
  // without a key, OpenAlex allows only 100 requests/day. That fails quietly, so warn up front.
  if (!cfg.openalex_has_key) {
    const hint = $("#keyhint");
    hint.innerHTML =
      "No OpenAlex API key set, so requests are capped at <b>100/day</b> (10,000 with one). " +
      '<a href="https://openalex.org/settings/api" target="_blank" rel="noopener">Get a free key</a> ' +
      "and set it as the <code>OPENALEX_API_KEY</code> environment variable.";
    hint.classList.remove("hidden");
  }
}).catch(() => {});

const initialSession = new URLSearchParams(location.search).get("session");
if (initialSession) {
  loadSession(initialSession, { isInitialLoad: true });
} else {
  render();
}
