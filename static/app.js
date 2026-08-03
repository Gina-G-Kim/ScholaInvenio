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
  config: { max_results: 1000, page_batch: 1000 },
  // Every provider is always queried (see providers.py's DEFAULT_PROVIDERS);
  // this is a post-search, client-side show/hide instead of a pre-search
  // opt-in. A source in here is hidden; a paper is hidden only once *all*
  // of its sources are (a paper both OpenAlex and DBLP found stays visible
  // if only DBLP is hidden).
  hiddenSources: new Set(),
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
    throw new Error(msg);
  }
  return payload;
}

function busy(on, label) {
  $("#busy").classList.toggle("hidden", !on);
  if (label) $("#busyText").textContent = label;
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
        max_results: parseInt($("#maxResults").value, 10) || undefined,
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
    render();
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
/* Load more (fetches in additional batches beyond the ceiling)        */
/* ------------------------------------------------------------------ */
async function fetchMore(number) {
  busy(true, "Fetching more…");
  try {
    const out = await api(`/api/session/${state.sessionId}/round/${number}/more`, { method: "POST" });
    const idx = state.rounds.findIndex((r) => r.number === number);
    if (idx >= 0) state.rounds[idx] = out.round;
    // rounds derived from this one were deleted by the server.
    if (out.dropped_rounds && out.dropped_rounds.length) {
      state.rounds = state.rounds.filter((r) => !out.dropped_rounds.includes(r.number));
    }
    render();
    const dropped = out.dropped_rounds && out.dropped_rounds.length
      ? ` Later rounds (${out.dropped_rounds.join(", ")}) were removed since their basis changed.` : "";
    banner(`+${out.added} results — ${out.round.count} total, saved to round_${String(number).padStart(2,"0")}.xml.${dropped}`,
           "ok", 12000);
  } catch (err) {
    banner(err.message);
  } finally {
    busy(false);
  }
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
    render();
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
    render();
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

function render() {
  const wrap = $("#columns");
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

  // scroll to the right when a new round is added (spec 7)
  requestAnimationFrame(() => { wrap.scrollLeft = wrap.scrollWidth; });
  renderDetail(currentPaper());
}

function visiblePapers(papers) {
  if (!state.hiddenSources.size) return papers;
  // hidden only once *every* source that found this paper is hidden
  return papers.filter((p) => !(p.sources || []).every((s) => state.hiddenSources.has(s)));
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

  // when the result count exceeds the ceiling, a bubble confirms fetching more
  if (round.has_more) {
    const known = round.available || round.count;
    const bubble = el("div", "morebubble");
    const text = el("div", "mb-text");
    text.innerHTML = `This search has <b>${known.toLocaleString()}+ results</b>. ` +
                     `Only <b>${round.count.toLocaleString()}</b> have been fetched so far.<br>` +
                     `Fetch ${(state.config.page_batch || 1000).toLocaleString()} more?`;
    bubble.append(text);
    const acts = el("div", "mb-actions");
    const yes = el("button", "btn primary sm", "Load more");
    yes.addEventListener("click", () => fetchMore(round.number));
    const no = el("button", "btn ghost sm", "Dismiss");
    no.addEventListener("click", () => bubble.remove());
    acts.append(yes, no);
    bubble.append(acts);
    list.append(bubble);
  }

  if (!shown.length) {
    list.append(el("div", "item-empty",
      round.papers.length ? "Every result is hidden by the source filters above." : "No results."));
  }
  const cmp = (SORTS[state.sorts[round.number] || "relevance"] || {}).cmp;
  const ordered = cmp ? [...shown].sort(cmp) : shown;
  for (const paper of ordered) {
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
    list.append(item);
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

async function loadSession(sessionId) {
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
    render();
  } catch (err) {
    banner(err.message);
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

// the ceiling is decided server-side; hardcoding it on the frontend would let the two drift apart.
api("/api/config").then((cfg) => {
  state.config = cfg;
  const box = $("#maxResults");
  box.max = cfg.max_results;
  box.value = cfg.max_results;      // default to the ceiling
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
  loadSession(initialSession);
} else {
  render();
}
