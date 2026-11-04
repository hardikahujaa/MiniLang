/* ==========================================================================
   MiniLang IDE -- application controller

   Owns data flow and DOM wiring. Drawing lives in viz.js, syntax colouring in
   highlight.js, and the editor component in editor.js.

   Design rule from the plan (section 3): POST /compile returns every phase's
   artefacts in one object, so this file is pure rendering -- one fetch, then
   one render pass per tab. There is no client-side state machine.

   Long-file strategy
   ------------------
   Two places would otherwise fall over on a large program:

     editor      handled in editor.js (debounced repaint, plain-text fallback)
     token table handled here by renderTokens(), which windows the rows so that
                 only what fits on screen exists in the DOM. A 20k-token
                 program creates roughly 40 row elements, not 20,000.
   ========================================================================== */

"use strict";

/** Height of one token row in pixels. Must match `.vt-row` in style.css. */
const ROW_HEIGHT = 24;

/** Extra rows rendered above and below the viewport to smooth fast scrolling. */
const ROW_OVERSCAN = 8;

/** The most recent successful /compile response. */
let lastResult = null;

/** Tokens currently displayed, after the filter box has been applied. */
let visibleTokens = [];

/** The editor instance, created on DOMContentLoaded. */
let editor = null;

/** Symbol-table rows surviving the current filter. */
let visibleSymbols = [];

/** Handle returned by Viz.renderTree, exposing expand/collapse/reset. */
let treeHandle = null;

/** The AST most recently returned by /compile, kept for re-rendering. */
let lastAst = null;

/** `CompileResponse.parseTree` (the table-driven parser's concrete tree). */
let lastParseTree = null;

/** `CompileResponse.parseTrace`, the step-by-step table-driven parse. */
let lastParseTrace = [];

/** Handle returned by Viz.renderTree for the step-through trace's own tree. */
let traceTreeHandle = null;

/** Which Syntax tab sub-view is showing: `"ast"` or `"trace"`. */
let syntaxView = "ast";

/** 0-based index of the step currently revealed in the trace animation. */
let traceStep = 0;

/** `setInterval` handle while the trace is auto-playing, else null. */
let tracePlayTimer = null;

/** `{tac, blocks, cfg}` from the most recent /compile, kept for re-rendering. */
let lastIcg = null;

/** Handle returned by Viz.renderCfg, exposing resetView. */
let cfgHandle = null;

/** Which ICG sub-view is showing: `"tac"` or `"cfg"`. */
let icgView = "tac";

/** The most recent successful `/analyze-grammar` response. */
let lastTheory = null;

/** Which Parser Theory sub-view is showing. */
let theoryView = "sets";

/** `app.samples.DEMO_GRAMMAR`, fetched once from `/api/demo` and reused by
 *  the "Expression grammar" quick-load button and the sidebar file tree. */
let demoGrammarText = "";

/** `app.samples.AMBIGUOUS_GRAMMAR`, fetched once from `/api/demo`. */
let ambiguousGrammarText = "";

/* -------------------------------------------------------------------------
   DOM helpers
   ------------------------------------------------------------------------- */

/**
 * Shorthand for `document.querySelector`.
 * @param {string} selector - A CSS selector.
 * @returns {Element|null} The first match, or null.
 */
function $(selector) {
  return document.querySelector(selector);
}

/**
 * Shorthand for `document.querySelectorAll`, as a real array.
 * @param {string} selector - A CSS selector.
 * @returns {Element[]} All matches.
 */
function $$(selector) {
  return Array.from(document.querySelectorAll(selector));
}

/**
 * Escape text for safe insertion as HTML.
 * @param {string} text - Untrusted text.
 * @returns {string} Escaped text.
 */
function esc(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

/* -------------------------------------------------------------------------
   Status bar and build log
   ------------------------------------------------------------------------- */

/**
 * Set the status bar's state and message.
 * @param {"ready"|"working"|"ok"|"error"} state - Visual state.
 * @param {string} message - Text shown in the leftmost cell.
 * @returns {void}
 */
function setStatus(state, message) {
  const bar = $(".statusbar");
  bar.classList.toggle("is-error", state === "error");
  bar.classList.toggle("is-working", state === "working");
  $("#status-state").textContent = message;
}

/**
 * Append a timestamped line to the Build log tab.
 * @param {string} line - Text to append.
 * @returns {void}
 */
function log(line) {
  const stamp = new Date().toLocaleTimeString([], { hour12: false });
  const pane = $("#out-build");
  pane.textContent += `[${stamp}] ${line}\n`;
  pane.scrollTop = pane.scrollHeight;
}

/* -------------------------------------------------------------------------
   Tabs and panes
   ------------------------------------------------------------------------- */

/**
 * Activate one phase tab and reveal its panel.
 * @param {string} name - The `data-tab` value.
 * @returns {void}
 */
function activateTab(name) {
  $$(".tab").forEach((tab) => {
    const active = tab.dataset.tab === name;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-selected", String(active));
  });
  $$(".panel").forEach((panel) => {
    panel.classList.toggle("is-active", panel.id === `panel-${name}`);
  });
  // These measure their container, which is zero while hidden.
  if (name === "lexical") renderTokenWindow();
  if (name === "semantic") renderSymbolWindow();
  if (name === "syntax" && syntaxView === "ast" && lastAst && !treeHandle) {
    treeHandle = window.Viz.renderTree("#parse-tree-wrap", lastAst);
    applyEvalOrder();
  }
  if (name === "syntax" && syntaxView === "trace" && lastParseTree && !traceTreeHandle) {
    renderTraceTree();
  }
  if (name === "icg") drawCfgIfVisible();
}

/**
 * Activate one output-pane tab.
 * @param {string} name - The `data-out` value.
 * @returns {void}
 */
function activateOutTab(name) {
  $$(".out-tab").forEach((tab) => tab.classList.toggle("is-active", tab.dataset.out === name));
  $$(".out-view").forEach((view) => view.classList.toggle("is-active", view.id === `out-${name}`));
}

/* -------------------------------------------------------------------------
   Token table (windowed)
   ------------------------------------------------------------------------- */

/**
 * Render the token stream, filtered and windowed.
 *
 * Sets the scroll spacer to the full height of all rows so the scrollbar is
 * honest, then draws only the slice currently on screen.
 *
 * @param {Array<Object>} tokens - Entries from `CompileResponse.tokens`.
 * @returns {void}
 */
function renderTokens(tokens) {
  const query = $("#token-filter").value.trim().toLowerCase();
  visibleTokens = !query
    ? tokens
    : tokens.filter(
        (t) =>
          t.type.toLowerCase().includes(query) ||
          t.lexeme.toLowerCase().includes(query) ||
          (t.category || "").toLowerCase().includes(query)
      );

  const hasTokens = tokens.length > 0;
  $("#lexical-empty").hidden = hasTokens;
  $("#token-table-wrap").hidden = !hasTokens;

  const counts = {};
  tokens.forEach((t) => {
    const key = t.category || "other";
    counts[key] = (counts[key] || 0) + 1;
  });
  const parts = [`<span><b>${tokens.length}</b> tokens</span>`];
  Object.keys(counts)
    .sort()
    .forEach((key) => parts.push(`<span>${esc(key)}: <b>${counts[key]}</b></span>`));
  if (query) parts.push(`<span>showing <b>${visibleTokens.length}</b></span>`);
  $("#token-stats").innerHTML = parts.join("");

  $("#token-spacer").style.height = `${visibleTokens.length * ROW_HEIGHT}px`;
  renderTokenWindow();
}

/**
 * Draw only the token rows currently inside the viewport.
 * @returns {void}
 */
function renderTokenWindow() {
  const viewport = $("#token-viewport");
  const rows = $("#token-rows");
  if (!viewport || !rows || $("#token-table-wrap").hidden) return;

  const height = viewport.clientHeight || 400;
  const first = Math.max(0, Math.floor(viewport.scrollTop / ROW_HEIGHT) - ROW_OVERSCAN);
  const count = Math.ceil(height / ROW_HEIGHT) + ROW_OVERSCAN * 2;
  const slice = visibleTokens.slice(first, first + count);

  rows.style.transform = `translateY(${first * ROW_HEIGHT}px)`;
  rows.innerHTML = slice
    .map((token, offset) => {
      const category = token.category || "special";
      return (
        '<div class="vt-row">' +
        `<span class="vt-c-idx">${first + offset + 1}</span>` +
        `<span class="vt-c-type"><span class="vt-type cat-${esc(category)}">${esc(token.type)}</span></span>` +
        `<span class="vt-cat">${esc(category)}</span>` +
        `<span class="vt-c-lex">${esc(token.lexeme) || "&nbsp;"}</span>` +
        `<span class="vt-c-ln">${token.line}</span>` +
        `<span class="vt-c-col">${token.col}</span>` +
        "</div>"
      );
    })
    .join("");
}

/* -------------------------------------------------------------------------
   Parser Theory Lab (Parser Theory tab)

   A separate fetch from Build's: the grammar being analysed has nothing to
   do with the MiniLang source in the editor, so this is its own request,
   its own empty-state/wrap toggle, and its own stats row -- the same shape
   `build()`/`renderResult()` use for the main pipeline, just independent of
   it.
   ------------------------------------------------------------------------- */

/**
 * Switch which Parser Theory sub-view is visible.
 * @param {string} name - One of "sets", "ll1", "lr0", "slr", "lalr",
 *     "conflicts", "trace".
 * @returns {void}
 */
function activateTheoryView(name) {
  theoryView = name;
  $$('.seg-toggle[aria-label="Parser Theory view"] .seg-btn').forEach((btn) =>
    btn.classList.toggle("is-active", btn.dataset.theoryView === name)
  );
  ["sets", "ll1", "lr0", "slr", "lalr", "conflicts", "trace"].forEach((view) => {
    $(`#theory-view-${view}`).hidden = view !== name;
  });
}

/**
 * Render a `{symbol: [member, ...]}` map (FIRST or FOLLOW) as table rows.
 * @param {Object<string, Array<string>>} sets - The map to render.
 * @param {Array<string>} symbols - Which keys to show, in this order.
 * @returns {string} HTML rows.
 */
function renderSetRows(sets, symbols) {
  return symbols
    .map((symbol) => {
      const members = (sets[symbol] || []).map((m) => `<code>${esc(m)}</code>`).join(" ");
      return (
        "<tr>" +
        `<td class="theory-symbol">${esc(symbol)}</td>` +
        `<td>${members || "&mdash;"}</td>` +
        "</tr>"
      );
    })
    .join("");
}

/** Render the "Sets" view: grammar summary plus FIRST/FOLLOW tables. */
function renderTheorySets(result) {
  const productions = (result.productions || [])
    .map((p, i) => `<li><span class="theory-prod-index">r${i + 1}</span><code>${esc(p)}</code></li>`)
    .join("");

  return (
    '<div class="theory-summary">' +
    `<div><span class="theory-label">Start symbol</span><code>${esc(result.startSymbol || "")}</code></div>` +
    `<div><span class="theory-label">Terminals</span>${(result.terminals || [])
      .map((t) => `<code>${esc(t)}</code>`)
      .join(" ")}</div>` +
    `<div><span class="theory-label">Non-terminals</span>${(result.nonTerminals || [])
      .map((t) => `<code>${esc(t)}</code>`)
      .join(" ")}</div>` +
    "</div>" +
    `<ol class="theory-productions">${productions}</ol>` +
    '<table class="theory-table"><thead><tr><th>Symbol</th><th>FIRST</th></tr></thead>' +
    `<tbody>${renderSetRows(result.first || {}, result.nonTerminals || [])}</tbody></table>` +
    '<table class="theory-table"><thead><tr><th>Symbol</th><th>FOLLOW</th></tr></thead>' +
    `<tbody>${renderSetRows(result.follow || {}, result.nonTerminals || [])}</tbody></table>`
  );
}

/** Render the "LL(1)" view: the parsing table, with conflict cells flagged. */
function renderTheoryLl1(result) {
  const table = result.ll1Table || {};
  const terminals = [...(result.terminals || []), "$"];
  const nonTerminals = result.nonTerminals || [];

  const header = terminals.map((t) => `<th>${esc(t)}</th>`).join("");
  const rows = nonTerminals
    .map((nt) => {
      const cells = terminals
        .map((t) => {
          const entries = (table[nt] || {})[t] || [];
          const isConflict = entries.length > 1;
          const content = entries.map((e) => `<code>${esc(e)}</code>`).join("<br/>");
          return `<td class="${isConflict ? "theory-conflict" : ""}">${content || "&nbsp;"}</td>`;
        })
        .join("");
      return `<tr><td class="theory-symbol">${esc(nt)}</td>${cells}</tr>`;
    })
    .join("");

  const banner = result.isLl1
    ? '<div class="theory-banner theory-banner-ok">This grammar is LL(1).</div>'
    : '<div class="theory-banner theory-banner-conflict">Not LL(1) &mdash; see the highlighted cells below.</div>';

  return (
    banner +
    '<table class="theory-table theory-lr-table"><thead><tr><th></th>' +
    `${header}</tr></thead><tbody>${rows}</tbody></table>`
  );
}

/** Render the "LR(0) Items" view: one card per canonical-collection state. */
function renderTheoryLr0(result) {
  return (result.lr0Items || [])
    .map((state) => {
      const items = (state.items || []).map((item) => `<li><code>${esc(item)}</code></li>`).join("");
      const transitions = Object.entries(state.transitions || {})
        .map(([symbol, target]) => `<span><code>${esc(symbol)}</code> &rarr; state ${target}</span>`)
        .join("");
      return (
        '<div class="theory-state-card">' +
        `<div class="theory-state-head">State ${state.state}</div>` +
        `<ul class="theory-items">${items}</ul>` +
        `<div class="theory-transitions">${transitions || "&mdash;"}</div>` +
        "</div>"
      );
    })
    .join("");
}

/**
 * Render an SLR or LALR ACTION/GOTO table, merged into one grid per state.
 * @param {Object} result - The `/analyze-grammar` response.
 * @param {string} key - `"slrTable"` or `"lalrTable"`.
 * @returns {string} HTML.
 */
function renderTheoryLrTable(result, key) {
  const table = result[key] || {};
  const terminals = [...(result.terminals || []), "$"];
  const nonTerminals = result.nonTerminals || [];
  const states = Object.keys(table)
    .map(Number)
    .sort((a, b) => a - b);

  const header =
    [...terminals, ...nonTerminals].map((s) => `<th>${esc(s)}</th>`).join("");
  const rows = states
    .map((state) => {
      const row = table[String(state)] || {};
      const cells = [...terminals, ...nonTerminals]
        .map((symbol) => {
          const entries = row[symbol] || [];
          const isConflict = entries.length > 1;
          const content = entries.join(" / ");
          return `<td class="${isConflict ? "theory-conflict" : ""}">${content || "&nbsp;"}</td>`;
        })
        .join("");
      return `<tr><td class="theory-symbol">${state}</td>${cells}</tr>`;
    })
    .join("");

  return (
    '<table class="theory-table theory-lr-table"><thead><tr><th>State</th>' +
    `${header}</tr></thead><tbody>${rows}</tbody></table>`
  );
}

/** Render the "Conflicts" view: every LL(1)/SLR/LALR conflict, grouped by table. */
function renderTheoryConflicts(result) {
  const conflicts = result.conflicts || [];
  if (conflicts.length === 0) {
    return '<div class="out-empty">No conflicts in any of the three tables.</div>';
  }
  return conflicts
    .map(
      (c) =>
        '<div class="theory-conflict-row">' +
        `<span class="theory-conflict-table">${esc(c.table)}</span>` +
        `<span class="theory-conflict-kind">${esc(c.kind)}</span>` +
        `<span class="theory-conflict-detail">${esc(c.detail)}</span>` +
        "</div>"
    )
    .join("");
}

/** Render the "Trace" view: the step-by-step LALR parse of the input string. */
function renderTheoryTrace(result) {
  const trace = result.parseTrace || [];
  if (trace.length === 0) {
    return '<div class="out-empty">Enter a trace input above and press Analyze.</div>';
  }
  const rows = trace
    .map(
      (step) =>
        "<tr>" +
        `<td>${step.step}</td>` +
        `<td><code>${esc((step.stack || []).join(" "))}</code></td>` +
        `<td><code>${esc((step.input || []).join(" "))}</code></td>` +
        `<td>${esc(step.action)}</td>` +
        "</tr>"
    )
    .join("");
  return (
    '<table class="theory-table"><thead><tr><th>Step</th><th>Stack</th>' +
    `<th>Input</th><th>Action</th></tr></thead><tbody>${rows}</tbody></table>`
  );
}

/**
 * Render the whole Parser Theory tab from an `/analyze-grammar` response.
 * @param {Object} result - The response body.
 * @returns {void}
 */
function renderTheory(result) {
  lastTheory = result;
  $("#theory-empty").hidden = true;
  $("#theory-wrap").hidden = false;

  const errors = result.errors || [];
  $("#theory-errors").hidden = errors.length === 0;
  $("#theory-errors").innerHTML = errors.map((e) => esc(e.message)).join("<br/>");

  const parts = [
    `<span><b>${(result.nonTerminals || []).length}</b> non-terminals</span>`,
    `<span><b>${(result.terminals || []).length}</b> terminals</span>`,
    `<span><b>${(result.lr0Items || []).length}</b> LR(0) states</span>`,
    `<span>LL(1): <b>${result.isLl1 ? "yes" : "no"}</b></span>`,
    `<span><b>${(result.conflicts || []).length}</b> conflicts</span>`,
  ];
  $("#theory-stats").innerHTML = parts.join("");

  $("#theory-view-sets").innerHTML = renderTheorySets(result);
  $("#theory-view-ll1").innerHTML = renderTheoryLl1(result);
  $("#theory-view-lr0").innerHTML = renderTheoryLr0(result);
  $("#theory-view-slr").innerHTML = renderTheoryLrTable(result, "slrTable");
  $("#theory-view-lalr").innerHTML = renderTheoryLrTable(result, "lalrTable");
  $("#theory-view-conflicts").innerHTML = renderTheoryConflicts(result);
  $("#theory-view-trace").innerHTML = renderTheoryTrace(result);
}

/**
 * Submit the Parser Theory tab's grammar text to `/analyze-grammar`.
 * @returns {Promise<void>} Resolves once the response has been rendered.
 */
async function analyzeGrammar() {
  const grammar = $("#theory-grammar-input").value;
  if (!grammar.trim()) {
    setStatus("error", "Paste a grammar first");
    return;
  }
  const startSymbol = $("#theory-start-symbol").value.trim() || null;
  const inputString = $("#theory-input-string").value.trim() || null;

  setStatus("working", "Analysing grammar…");
  try {
    const response = await fetch("/analyze-grammar", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ grammar, startSymbol, inputString }),
    });
    if (!response.ok) {
      const detail = await response.text();
      setStatus("error", `Analysis failed — HTTP ${response.status}`);
      log(`HTTP ${response.status}: ${detail.slice(0, 400)}`);
      return;
    }
    const result = await response.json();
    renderTheory(result);
    const errorCount = (result.errors || []).length;
    setStatus(errorCount ? "error" : "ready", errorCount ? "Grammar has errors" : "Analysis complete");
    log(
      `analyze-grammar: ${(result.nonTerminals || []).length} non-terminals, ` +
        `${(result.conflicts || []).length} conflict(s)`
    );
  } catch (err) {
    setStatus("error", "Backend unreachable");
    log(`fetch failed: ${err.message}`);
  }
}

/* -------------------------------------------------------------------------
   Symbol table (Semantic tab)
   ------------------------------------------------------------------------- */

/**
 * Render a scope depth the way a textbook labels it.
 *
 * @param {number} depth - Nesting depth; 0 is the global scope.
 * @returns {string} "Global" at depth 0, "Local-1", "Local-2" and so on below.
 */
function depthLabel(depth) {
  return depth === 0 ? "Global" : `Local-${depth}`;
}

/**
 * Render the scoped symbol table, filtered and windowed.
 *
 * Shows every name the program declared, including names whose scope has since
 * closed: the analyser keeps an append-only log precisely so this tab can show
 * the whole environment rather than whatever happened to be open at the end.
 *
 * @param {Array<Object>} symbols - Entries from `CompileResponse.symbols`.
 * @returns {void}
 */
function renderSymbols(symbols) {
  const rows = symbols || [];
  const query = $("#symbol-filter").value.trim().toLowerCase();
  visibleSymbols = !query
    ? rows
    : rows.filter(
        (s) =>
          s.name.toLowerCase().includes(query) ||
          (s.type || "").toLowerCase().includes(query) ||
          (s.kind || "").toLowerCase().includes(query) ||
          (s.scope || "").toLowerCase().includes(query)
      );

  const hasRows = rows.length > 0;
  $("#semantic-empty").hidden = hasRows;
  $("#symbol-table-wrap").hidden = !hasRows;

  const counts = {};
  let deepest = 0;
  rows.forEach((s) => {
    const key = s.kind || "variable";
    counts[key] = (counts[key] || 0) + 1;
    if (s.depth > deepest) deepest = s.depth;
  });

  const parts = [`<span><b>${rows.length}</b> symbols</span>`];
  Object.keys(counts)
    .sort()
    .forEach((key) => parts.push(`<span>${esc(key)}: <b>${counts[key]}</b></span>`));
  parts.push(`<span>max depth <b>${deepest}</b></span>`);
  if (query) parts.push(`<span>showing <b>${visibleSymbols.length}</b></span>`);
  $("#symbol-stats").innerHTML = parts.join("");

  $("#symbol-spacer").style.height = `${visibleSymbols.length * ROW_HEIGHT}px`;
  renderSymbolWindow();
}

/**
 * Draw only the symbol rows currently inside the viewport.
 * @returns {void}
 */
function renderSymbolWindow() {
  const viewport = $("#symbol-viewport");
  const rows = $("#symbol-rows");
  if (!viewport || !rows || $("#symbol-table-wrap").hidden) return;

  const height = viewport.clientHeight || 400;
  const first = Math.max(0, Math.floor(viewport.scrollTop / ROW_HEIGHT) - ROW_OVERSCAN);
  const count = Math.ceil(height / ROW_HEIGHT) + ROW_OVERSCAN * 2;
  const slice = visibleSymbols.slice(first, first + count);

  rows.style.transform = `translateY(${first * ROW_HEIGHT}px)`;
  rows.innerHTML = slice
    .map((symbol, offset) => {
      const kind = symbol.kind || "variable";
      return (
        '<div class="vt-row">' +
        `<span class="vt-c-idx">${first + offset + 1}</span>` +
        `<span class="sym-c-name">${esc(symbol.name)}</span>` +
        `<span class="sym-c-type">${esc(symbol.type)}</span>` +
        `<span class="sym-c-kind"><span class="sym-kind kind-${esc(kind)}">${esc(kind)}</span></span>` +
        `<span class="sym-c-scope">${esc(symbol.scope || "global")}</span>` +
        `<span class="sym-c-depth">${depthLabel(symbol.depth || 0)}</span>` +
        `<span class="vt-c-ln">${symbol.line === null ? "" : symbol.line}</span>` +
        `<span class="vt-c-col">${symbol.col === null ? "" : symbol.col}</span>` +
        "</div>"
      );
    })
    .join("");
}

/* -------------------------------------------------------------------------
   Three-address code and the control-flow graph (ICG tab)
   ------------------------------------------------------------------------- */

/** CSS class naming the colour a TAC row's op is drawn in. */
function tacRowClass(op) {
  if (op === "label") return "is-label";
  if (op === "goto" || op === "if" || op === "ifFalse") return "is-jump";
  if (op === "return") return "is-return";
  if (op === "call" || op === "param") return "is-call";
  if (op === "print") return "is-print";
  return "";
}

/**
 * Switch between the flat TAC listing and the D3 control-flow graph.
 * @param {"tac"|"cfg"} name - The sub-view to show.
 * @returns {void}
 */
function activateIcgView(name) {
  icgView = name;
  $$("#icg-toolbar .seg-btn").forEach((btn) =>
    btn.classList.toggle("is-active", btn.dataset.icgView === name)
  );
  $("#tac-wrap").hidden = name !== "tac" || !lastIcg;
  $("#cfg-wrap").hidden = name !== "cfg" || !lastIcg;
  drawCfgIfVisible();
}

/**
 * Draw the CFG once its container is actually visible.
 *
 * An SVG laid out inside a `display: none` container measures zero, so this
 * is called both when the ICG tab is activated and when the sub-view toggle
 * switches to "cfg" -- whichever happens last is what makes the panel
 * visible, and either one needs to trigger the draw.
 *
 * @returns {void}
 */
function drawCfgIfVisible() {
  if (!lastIcg || icgView !== "cfg" || cfgHandle) return;
  if (!$("#panel-icg").classList.contains("is-active")) return;
  cfgHandle = window.Viz.renderCfg("#cfg-wrap", lastIcg.cfg);
}

/**
 * Render the ICG tab: the flat TAC listing, grouped by basic block, plus the
 * control-flow graph those blocks imply.
 *
 * Grouping is free: each block from `CompileResponse.blocks` already carries
 * the slice of `tac` it owns, so this does no re-slicing of its own.
 *
 * @param {Array<Object>} tac - Entries from `CompileResponse.tac`.
 * @param {Array<Object>} blocks - Entries from `CompileResponse.blocks`.
 * @param {Object|null} cfg - `CompileResponse.cfg`.
 * @returns {void}
 */
function renderIcg(tac, blocks, cfg) {
  const rows = tac || [];
  lastIcg = rows.length > 0 ? { tac: rows, blocks: blocks || [], cfg: cfg || null } : null;
  cfgHandle = null;
  window.Viz.clear("#cfg-wrap");

  const hasTac = lastIcg !== null;
  $("#icg-empty").hidden = hasTac;
  $("#icg-toolbar").hidden = !hasTac;
  $("#tac-wrap").hidden = !hasTac || icgView !== "tac";
  $("#cfg-wrap").hidden = !hasTac || icgView !== "cfg";

  if (!hasTac) {
    $("#tac-wrap").innerHTML = "";
    return;
  }

  const functionCount = new Set((blocks || []).map((b) => b.function).filter(Boolean)).size;
  $("#icg-stats").innerHTML =
    `<span><b>${rows.length}</b> instructions</span>` +
    `<span><b>${(blocks || []).length}</b> basic blocks</span>` +
    `<span><b>${functionCount}</b> function(s)</span>`;

  $("#tac-wrap").innerHTML = (blocks || [])
    .map((block) => {
      const instrRows = (block.instructions || [])
        .map(
          (instr) =>
            '<div class="tac-row">' +
            `<span class="tac-idx">${instr.index}</span>` +
            `<span class="tac-text ${tacRowClass(instr.op)}">${esc(instr.text)}</span>` +
            "</div>"
        )
        .join("");
      const successors = block.successors.length ? block.successors.join(", ") : "exit";
      return (
        '<div class="tac-block">' +
        '<div class="tac-block-head">' +
        `<span class="tac-block-id">${esc(block.id)}</span>` +
        (block.function ? `<span class="tac-block-fn">${esc(block.function)}</span>` : "") +
        `<span class="tac-block-succ">&rarr; ${esc(successors)}</span>` +
        "</div>" +
        `<div class="tac-block-body">${instrRows}</div>` +
        "</div>"
      );
    })
    .join("");

  drawCfgIfVisible();
}

/* -------------------------------------------------------------------------
   Optimization tab
   ------------------------------------------------------------------------- */

/**
 * Render one TAC listing as plain rows, reusing the ICG tab's row colouring.
 * @param {Array<Object>} instructions - Entries from a `TacInstruction` list.
 * @returns {string} The rendered HTML.
 */
function renderTacColumn(instructions) {
  return (instructions || [])
    .map(
      (instr) =>
        '<div class="tac-row">' +
        `<span class="tac-idx">${instr.index}</span>` +
        `<span class="tac-text ${tacRowClass(instr.op)}">${esc(instr.text)}</span>` +
        "</div>"
    )
    .join("");
}

/**
 * Render the optimisation change log: one entry per instruction rewritten or
 * removed, across every pass and every round.
 * @param {Array<Object>} log - Entries from `CompileResponse.optimized.log`.
 * @returns {string} The rendered HTML.
 */
function renderOptLog(log) {
  if (!log || log.length === 0) {
    return '<div class="out-empty">No changes - every enabled pass found nothing to do.</div>';
  }
  return log
    .map((change) => {
      const after = change.after === null || change.after === undefined
        ? '<span class="opt-log-removed">removed</span>'
        : `<code>${esc(change.after)}</code>`;
      return (
        '<div class="opt-log-row">' +
        `<span class="opt-log-pass pass-${esc(change.passName)}">${esc(change.passName)}</span>` +
        `<span class="opt-log-change"><code>${esc(change.before || "")}</code> &rarr; ${after}</span>` +
        `<span class="opt-log-detail">${esc(change.detail || "")}</span>` +
        "</div>"
      );
    })
    .join("");
}

/**
 * Render the Optimization tab: before/after TAC columns plus the change log.
 *
 * Re-rendered after every build, which is also triggered by toggling one of
 * the four pass checkboxes -- see `init()` -- so flipping a checkbox and
 * watching the instruction counter move is immediate, matching the demo
 * script's "toggle each optimization one at a time" beat.
 *
 * @param {Array<Object>} before - `CompileResponse.tac`, pre-optimisation.
 * @param {Object|null} optimized - `CompileResponse.optimized`.
 * @returns {void}
 */
function renderOptimization(before, optimized) {
  const hasData = Boolean(optimized) && (before || []).length > 0;
  $("#opt-empty").hidden = hasData;
  $("#opt-wrap").hidden = !hasData;
  if (!hasData) return;

  const stats = optimized.stats || {};
  const parts = [
    `<span><b>${stats.instructionsBefore || 0}</b> before</span>`,
    `<span><b>${stats.instructionsAfter || 0}</b> after</span>`,
    `<span><b>-${stats.removed || 0}</b> removed</span>`,
  ];
  Object.keys(stats.perPass || {})
    .sort()
    .forEach((pass) => parts.push(`<span>${esc(pass)}: <b>${stats.perPass[pass]}</b></span>`));
  $("#opt-stats").innerHTML = parts.join("");

  $("#opt-before-count").textContent = `${before.length} instructions`;
  $("#opt-after-count").textContent = `${(optimized.tac || []).length} instructions`;
  $("#opt-before-body").innerHTML = renderTacColumn(before);
  $("#opt-after-body").innerHTML = renderTacColumn(optimized.tac);
  $("#opt-log-body").innerHTML = renderOptLog(optimized.log);
}

/* -------------------------------------------------------------------------
   Target tab (stack-VM assembly + Run)
   ------------------------------------------------------------------------- */

/** CSS class naming the colour an assembly row's mnemonic is drawn in. */
function asmRowClass(op) {
  if (op === "LABEL") return "is-label";
  if (op === "JMP" || op === "JMPT" || op === "JMPF") return "is-jump";
  if (op === "CALL" || op === "RET" || op === "RETV") return "is-call";
  if (op === "PRINT") return "is-print";
  return "";
}

/**
 * Render the Target tab: the resolved stack-VM assembly listing.
 * @param {Array<Object>} asm - Entries from `CompileResponse.asm`.
 * @returns {void}
 */
function renderTarget(asm) {
  const rows = asm || [];
  const hasAsm = rows.length > 0;
  $("#asm-empty").hidden = hasAsm;
  $("#asm-wrap").hidden = !hasAsm;
  $("#asm-run-status").textContent = "";

  if (!hasAsm) {
    $("#asm-wrap").innerHTML = "";
    return;
  }

  // A function's own label is bare (e.g. "fib"); every internal label app.tac
  // generates is suffixed "_L<n>" (e.g. "fib_L0"), so counting labels without
  // that suffix counts functions without needing a dedicated API field.
  const functionCount = rows.filter(
    (i) => i.op === "LABEL" && i.operand && !/_L\d+$/.test(i.operand)
  ).length;
  $("#asm-stats").innerHTML =
    `<span><b>${rows.length}</b> instructions</span>` +
    `<span><b>${functionCount}</b> function(s)</span>`;

  $("#asm-wrap").innerHTML = rows
    .map((instr) => {
      const comment = instr.comment
        ? ` <span class="asm-comment">; ${esc(instr.comment)}</span>`
        : "";
      return (
        '<div class="tac-row">' +
        `<span class="tac-idx">${instr.index}</span>` +
        `<span class="tac-text ${asmRowClass(instr.op)}">${esc(instr.text)}</span>${comment}` +
        "</div>"
      );
    })
    .join("");
}

/**
 * Reflect a `/run` result in the Target tab's status chip.
 * @param {Object} result - A parsed `RunResponse`.
 * @returns {void}
 */
function renderRunStatus(result) {
  const chip = $("#asm-run-status");
  if (result.error) {
    chip.className = "asm-run-status is-error";
    chip.textContent = `trapped after ${result.steps} step(s): ${result.error}`;
  } else if (!result.halted) {
    chip.className = "asm-run-status is-warn";
    chip.textContent = `step budget exhausted after ${result.steps} step(s)`;
  } else {
    chip.className = "asm-run-status is-ok";
    chip.textContent = `halted after ${result.steps} step(s)`;
  }
}

/**
 * Send the last build's assembly to `/run` and show the captured output.
 *
 * Deliberately runs what Build actually produced (`lastResult.asm`), not a
 * silent recompile of whatever the editor holds now -- Run means "run what
 * I built", matching the button's own disabled state, which tracks
 * `lastResult.asm` rather than the editor's contents.
 *
 * @returns {Promise<void>} Resolves once the result has been rendered.
 */
async function runProgram() {
  if (!lastResult || !lastResult.asm || lastResult.asm.length === 0) return;

  setStatus("working", "Running…");
  try {
    const response = await fetch("/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ asm: lastResult.asm }),
    });

    if (!response.ok) {
      const detail = await response.text();
      setStatus("error", `Run failed — HTTP ${response.status}`);
      log(`run HTTP ${response.status}: ${detail.slice(0, 400)}`);
      activateOutTab("build");
      return;
    }

    const result = await response.json();
    $("#out-program").textContent = result.output || "";
    activateOutTab("program");
    renderRunStatus(result);

    if (result.error) {
      setStatus("error", `Run trapped: ${result.error}`);
      log(`run trapped after ${result.steps} step(s): ${result.error}`);
    } else if (!result.halted) {
      setStatus("error", `Run did not halt within ${result.steps} step(s)`);
      log(`run exhausted its step budget after ${result.steps} step(s)`);
    } else {
      setStatus("ok", `Run finished — ${result.steps} step(s)`);
      log(`run finished in ${result.steps} step(s)`);
    }
  } catch (err) {
    setStatus("error", "Backend unreachable");
    log(`run fetch failed: ${err.message}`);
  }
}

/* -------------------------------------------------------------------------
   Problems
   ------------------------------------------------------------------------- */

/**
 * Render diagnostics into the Problems tab.
 *
 * Each row jumps the editor to the offending position when clicked, which is
 * the single most useful thing an error list can do.
 *
 * @param {Array<Object>} errors - Entries from `CompileResponse.errors`.
 * @param {Array<Object>} typeErrors - Entries from `CompileResponse.typeErrors`.
 * @returns {void}
 */
function renderProblems(errors, typeErrors) {
  const all = [];
  (errors || []).forEach((e) =>
    all.push({
      severity: "error",
      line: e.line,
      col: e.col,
      message: e.message,
      fix: e.suggestion,
    })
  );
  (typeErrors || []).forEach((d) =>
    all.push({
      severity: d.severity || "error",
      line: d.line || 1,
      col: d.col || 1,
      message: d.message,
      fix: null,
    })
  );

  const badge = $("#problem-count");
  badge.textContent = String(all.length);
  badge.classList.toggle("has-errors", all.length > 0);

  const host = $("#problems-list");
  if (all.length === 0) {
    host.innerHTML = '<div class="out-empty">No problems detected.</div>';
    return;
  }

  host.innerHTML = all
    .map(
      (p, i) =>
        `<div class="problem-item" data-i="${i}" data-line="${p.line}" data-col="${p.col}">` +
        `<span class="problem-sev${p.severity === "warning" ? " is-warn" : ""}">` +
        `${p.severity === "warning" ? "warn" : "error"}</span>` +
        `<span class="problem-pos">${p.line}:${p.col}</span>` +
        `<span class="problem-msg">${esc(p.message)}` +
        (p.fix ? ` <span class="problem-fix">&rarr; ${esc(p.fix)}</span>` : "") +
        "</span></div>"
    )
    .join("");

  host.querySelectorAll(".problem-item").forEach((row) => {
    row.addEventListener("click", () => {
      activateTab("source");
      editor.goTo(Number(row.dataset.line), Number(row.dataset.col));
    });
  });
}

/* -------------------------------------------------------------------------
   Render dispatch
   ------------------------------------------------------------------------- */

/**
 * Say why the Syntax tab has no tree, instead of showing a static placeholder.
 *
 * There are three distinct reasons, and a single "press Build" message is wrong
 * for two of them. The third is the important one: if the pipeline stopped at
 * the lexer with nothing to report, the backend is almost certainly running
 * code from before the parser was wired -- uvicorn reloads static files on
 * every request but not Python modules unless started with --reload. That
 * presents as a silently empty tab, which is very hard to diagnose from the
 * outside, so the UI names it.
 *
 * @returns {void}
 */
function explainMissingTree() {
  const host = $("#syntax-empty");
  const meta = (lastResult && lastResult.meta) || null;
  const errors = (lastResult && lastResult.errors) || [];

  let step = "press Build";
  let message = "The parse tree renders here. Click a node to fold it, scroll to zoom, drag to pan.";

  if (meta && errors.length > 0) {
    step = "parse failed";
    message =
      `The program has ${errors.length} problem(s), so no tree was built. ` +
      "See the Problems panel below - click an entry to jump to it.";
  } else if (meta && meta.reachedPhase === "lexer") {
    step = "stale backend?";
    message =
      "The backend tokenised this program but returned no syntax tree, and reported " +
      "no errors. That usually means the server is running code from before the parser " +
      "was added. Restart it (run.bat, or uvicorn app.main:app --reload).";
  }

  host.dataset.step = step;
  const paragraph = host.querySelector("p");
  if (paragraph) paragraph.textContent = message;
}

/**
 * Push the evaluation-order toggle's state onto the current tree.
 *
 * Every render builds a fresh SVG, so the toggle cannot be assumed to survive
 * one: it has to be re-applied each time a tree is drawn.
 *
 * @returns {void}
 */
function applyEvalOrder() {
  const box = $("#opt-eval-order");
  if (treeHandle && box) treeHandle.setEvalOrder(box.checked);
}

/**
 * Render the abstract syntax tree on the Syntax tab.
 *
 * Drawing is skipped while the panel is hidden: an SVG laid out inside a
 * `display: none` container measures zero and would render collapsed into the
 * corner. `activateTab` calls back here once the panel is visible.
 *
 * @param {Object|null} ast - The serialised root node, or null if parsing failed.
 * @returns {void}
 */
function renderAst(ast) {
  lastAst = ast;

  const hasTree = Boolean(ast);
  $("#parse-tree-wrap").hidden = !hasTree;

  if (!hasTree) {
    treeHandle = null;
    window.Viz.clear("#parse-tree-wrap");
    return;
  }

  const stats = window.Viz.measure(ast);
  $("#tree-stats").innerHTML =
    `<span><b>${stats.nodes}</b> nodes</span><span>depth <b>${stats.depth}</b></span>` +
    `<span><b>${ast.functionCount || 0}</b> functions</span>`;

  if (syntaxView === "ast" && $("#panel-syntax").classList.contains("is-active")) {
    treeHandle = window.Viz.renderTree("#parse-tree-wrap", ast);
    applyEvalOrder();
  }
}

/**
 * Switch which Syntax tab sub-view is visible: the abstract syntax tree, or
 * the table-driven parser's step-through trace.
 *
 * The AST-specific toolbar controls (expand/collapse/reset/eval-order) only
 * make sense for the "ast" sub-view and are hidden for "trace", which has
 * its own Play/Step/Reset controls instead.
 *
 * @param {string} name - "ast" or "trace".
 * @returns {void}
 */
function activateSyntaxView(name) {
  syntaxView = name;
  $$('.seg-toggle[aria-label="Syntax view"] .seg-btn').forEach((btn) =>
    btn.classList.toggle("is-active", btn.dataset.syntaxView === name)
  );
  $("#syntax-view-ast").hidden = name !== "ast";
  $("#syntax-view-trace").hidden = name !== "trace";
  $("#eval-order-toggle").hidden = name !== "ast";
  $("#btn-tree-expand").hidden = name !== "ast";
  $("#btn-tree-collapse").hidden = name !== "ast";
  $("#btn-tree-reset").hidden = name !== "ast";

  const panelActive = $("#panel-syntax").classList.contains("is-active");
  if (panelActive && name === "ast" && lastAst && !treeHandle) {
    treeHandle = window.Viz.renderTree("#parse-tree-wrap", lastAst);
    applyEvalOrder();
  }
  if (panelActive && name === "trace" && lastParseTree && !traceTreeHandle) {
    renderTraceTree();
  }
}

/**
 * Adapt a `TreeNode` (name/kind/value/line/col/step/children) into the
 * shape `Viz.renderTree` expects -- the same node/detail/edge envelope the
 * AST already uses. A pure data mapping, not a new renderer; `step` passes
 * through unchanged, since D3 hierarchies tolerate extra data fields.
 *
 * @param {Object} node - A `CompileResponse.parseTree` node.
 * @returns {Object} The equivalent AST-envelope-shaped node.
 */
function toVizShape(node) {
  return {
    node: node.name,
    detail: node.value || "",
    name: node.name,
    kind: node.kind,
    line: node.line,
    column: node.col,
    step: node.step,
    edge: null,
    children: (node.children || []).map(toVizShape),
  };
}

/**
 * Draw the step-through trace's own concrete parse tree, starting fully
 * expanded (it keeps grammar scaffolding the AST collapses, so folding by
 * node count the way the AST view does would hide exactly the nodes the
 * animation is meant to show).
 * @returns {void}
 */
function renderTraceTree() {
  if (!lastParseTree) return;
  traceTreeHandle = window.Viz.renderTree(
    "#parse-trace-tree-wrap",
    toVizShape(lastParseTree),
    { collapsed: false }
  );
  if (traceTreeHandle) traceTreeHandle.setVisibleStep(traceStep);
}

/**
 * Render the trace table: one row per step, step/stack/input/action --
 * the same shape and `theory-table` styling the Parser Theory tab's own
 * trace view already uses.
 * @returns {void}
 */
function renderTraceTable() {
  $("#trace-table-body").innerHTML = lastParseTrace
    .map(
      (step) =>
        `<tr data-step="${step.step}">` +
        `<td>${step.step}</td>` +
        `<td><code>${esc((step.stack || []).join(" "))}</code></td>` +
        `<td><code>${esc((step.input || []).join(" "))}</code></td>` +
        `<td>${esc(step.action)}</td>` +
        "</tr>"
    )
    .join("");
}

/**
 * Reveal the trace tree and highlight the trace table up to one step.
 * @param {number} index - 0-based step index to reveal.
 * @returns {void}
 */
function setTraceStep(index) {
  const last = Math.max(lastParseTrace.length - 1, 0);
  traceStep = Math.max(0, Math.min(index, last));
  $("#trace-slider").value = String(traceStep);
  if (traceTreeHandle) traceTreeHandle.setVisibleStep(traceStep);

  $$("#trace-table-body tr").forEach((row) => {
    row.classList.toggle("is-current", Number(row.dataset.step) === traceStep);
  });
  const current = $(`#trace-table-body tr[data-step="${traceStep}"]`);
  if (current) current.scrollIntoView({ block: "nearest" });

  const action = (lastParseTrace[traceStep] || {}).action || "";
  $("#trace-stats").innerHTML = `<span>step <b>${traceStep}</b> / ${last}</span><span>${esc(action)}</span>`;
}

/**
 * Advance the trace by one step, stopping playback once the end is reached.
 * @returns {void}
 */
function stepTrace() {
  if (traceStep >= lastParseTrace.length - 1) {
    pauseTrace();
    return;
  }
  setTraceStep(traceStep + 1);
}

/**
 * Stop auto-play, if it is running.
 * @returns {void}
 */
function pauseTrace() {
  if (tracePlayTimer) {
    clearInterval(tracePlayTimer);
    tracePlayTimer = null;
  }
  const button = $("#btn-trace-play");
  if (button) button.textContent = "Play";
}

/**
 * Start or stop auto-play, advancing one step every 400ms.
 * @returns {void}
 */
function toggleTracePlay() {
  if (tracePlayTimer) {
    pauseTrace();
    return;
  }
  if (traceStep >= lastParseTrace.length - 1) setTraceStep(0);
  tracePlayTimer = setInterval(stepTrace, 400);
  $("#btn-trace-play").textContent = "Pause";
}

/**
 * Render the Syntax tab's step-through trace sub-view from a /compile result.
 * @param {Object|null} parseTree - `CompileResponse.parseTree`.
 * @param {Array<Object>} parseTrace - `CompileResponse.parseTrace`.
 * @returns {void}
 */
function renderSyntaxTrace(parseTree, parseTrace) {
  pauseTrace();
  lastParseTree = parseTree || null;
  lastParseTrace = parseTrace || [];
  traceTreeHandle = null;
  window.Viz.clear("#parse-trace-tree-wrap");
  $("#parse-trace-tree-wrap").hidden = !lastParseTree;

  renderTraceTable();
  $("#trace-slider").max = String(Math.max(lastParseTrace.length - 1, 0));
  setTraceStep(0);

  if (syntaxView === "trace" && $("#panel-syntax").classList.contains("is-active")) {
    renderTraceTree();
  }
}

/**
 * Reconcile the Syntax tab's shared empty-state and toolbar once both the
 * AST and the trace have been rendered -- whichever of the two actually
 * exists decides whether anything is shown at all, and which sub-view a
 * fresh Build defaults to.
 * @returns {void}
 */
function reconcileSyntaxTab() {
  const hasAst = Boolean(lastAst);
  const hasTrace = lastParseTrace.length > 0;
  const hasAnything = hasAst || hasTrace;

  $("#syntax-empty").hidden = hasAnything;
  $("#tree-toolbar").hidden = !hasAnything;
  if (!hasAnything) {
    explainMissingTree();
    return;
  }
  activateSyntaxView(hasAst ? "ast" : "trace");
}

/**
 * Dispatch a `/compile` response to every tab's renderer.
 * @param {Object} result - The parsed `CompileResponse`.
 * @returns {void}
 */
function renderResult(result) {
  lastResult = result;

  renderTokens(result.tokens || []);
  renderAst(result.ast || null);
  renderSyntaxTrace(result.parseTree || null, result.parseTrace || []);
  reconcileSyntaxTab();
  renderSymbols(result.symbols || []);
  renderIcg(result.tac || [], result.blocks || [], result.cfg || null);
  renderOptimization(result.tac || [], result.optimized || null);
  renderTarget(result.asm || []);
  renderProblems(result.errors, result.typeErrors);

  $("#out-program").textContent = result.output || "";
  $("#btn-run").disabled = !result.asm || result.asm.length === 0;
  $("#status-tokens").textContent = `${(result.tokens || []).length} tokens`;

  const done = new Set();
  const reached = (result.meta && result.meta.reachedPhase) || "";
  if (reached && reached !== "none") done.add(reached);
  $$("#phase-list li").forEach((li) => li.classList.toggle("is-done", done.has(li.dataset.phase)));
}

/* -------------------------------------------------------------------------
   Backend calls
   ------------------------------------------------------------------------- */

/**
 * Read the optimisation checkboxes into the shape `/compile` expects.
 * @returns {Object<string, boolean>} Toggle state keyed by camelCase pass name.
 */
function readToggles() {
  const toggles = {};
  $$("#opt-toggles input[data-opt]").forEach((input) => {
    toggles[input.dataset.opt] = input.checked;
  });
  return toggles;
}

/**
 * Compile the editor's contents and render the result.
 *
 * Network and HTTP failures are surfaced in the status bar rather than thrown,
 * so a backend restart mid-demo degrades visibly instead of silently.
 *
 * @returns {Promise<void>} Resolves once the response has been rendered.
 */
async function build() {
  const language = editor.getLanguage();
  if (language !== "minilang") {
    setStatus("error", `Cannot build: the compiler targets MiniLang, not ${language.toUpperCase()}`);
    log(`build refused: ${language} is supported for editing and highlighting only`);
    activateOutTab("build");
    return;
  }

  setStatus("working", "Building…");
  treeHandle = null;
  const started = performance.now();

  try {
    const response = await fetch("/compile", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source: editor.getValue(),
        optimizations: readToggles(),
        explainErrors: false,
      }),
    });

    if (!response.ok) {
      const detail = await response.text();
      setStatus("error", `Build failed — HTTP ${response.status}`);
      log(`HTTP ${response.status}: ${detail.slice(0, 400)}`);
      activateOutTab("build");
      return;
    }

    const result = await response.json();
    renderResult(result);

    const elapsed = (performance.now() - started).toFixed(0);
    const meta = result.meta || {};
    const errorCount = (result.errors || []).length;
    const phaseTimes = (meta.timings || [])
      .map((t) => `${t.phase} ${t.ms.toFixed(2)}ms`)
      .join(", ");

    log(
      `build finished in ${elapsed}ms — ${meta.sourceLines || 0} lines, ` +
        `${(result.tokens || []).length} tokens, ${errorCount} problem(s)`
    );
    if (phaseTimes) log(`  phases: ${phaseTimes}`);

    if (errorCount > 0) {
      setStatus("error", `Build finished with ${errorCount} problem(s)`);
      activateOutTab("problems");
    } else {
      setStatus("ok", `Build succeeded — reached ${meta.reachedPhase || "?"} in ${elapsed}ms`);
    }
  } catch (err) {
    setStatus("error", "Backend unreachable");
    log(`fetch failed: ${err.message}`);
    activateOutTab("build");
  }
}

/**
 * Load the canonical demo program into the editor.
 * @returns {Promise<void>} Resolves once the editor has been populated.
 */
async function loadDemo() {
  try {
    const response = await fetch("/api/demo");
    if (!response.ok) {
      setStatus("error", `Could not load demo program (HTTP ${response.status})`);
      return;
    }
    const data = await response.json();
    editor.setValue(data.source);
    setLanguage("minilang");
    demoGrammarText = data.grammar || "";
    ambiguousGrammarText = data.ambiguousGrammar || "";
    if ($("#theory-grammar-input") && !$("#theory-grammar-input").value) {
      $("#theory-grammar-input").value = demoGrammarText;
    }
    log("loaded demo.ml");
    setStatus("ready", "Ready — press Build");
  } catch (err) {
    setStatus("error", `Could not load demo program: ${err.message}`);
  }
}

/**
 * Fetch `/api/health` and reflect it in the status bar and sidebar.
 * @returns {Promise<void>} Resolves once the UI has been updated.
 */
async function loadHealth() {
  try {
    const response = await fetch("/api/health");
    if (!response.ok) return;
    const data = await response.json();
    $("#status-version").textContent = `v${data.version}`;
    const phases = data.phasesImplemented || [];
    $("#status-phases").textContent = phases.length
      ? `phases: ${phases.join(" → ")}`
      : "phases: none";
    $$("#phase-list li").forEach((li) =>
      li.classList.toggle("is-done", phases.includes(li.dataset.phase))
    );
    log(`connected to backend v${data.version}`);
  } catch {
    $("#status-phases").textContent = "backend unreachable";
  }
}

/* -------------------------------------------------------------------------
   Language
   ------------------------------------------------------------------------- */

/**
 * Switch the editor's highlighting language and update dependent chrome.
 *
 * Only MiniLang can be compiled; C, C++ and Java are editing-and-highlighting
 * only. The status bar says so rather than letting Build fail confusingly.
 *
 * @param {string} language - Language id.
 * @returns {void}
 */
function setLanguage(language) {
  editor.setLanguage(language);
  $("#language-select").value = language;

  const names = { minilang: "MiniLang", c: "C", cpp: "C++", java: "Java" };
  const extensions = { minilang: "demo.ml", c: "main.c", cpp: "main.cpp", java: "Main.java" };
  $("#status-lang").textContent = names[language] || language;
  $("#file-tab-name").textContent = extensions[language] || "source";

  const compilable = language === "minilang";
  $("#btn-compile").disabled = !compilable;
  $("#btn-compile").title = compilable
    ? "Compile (Ctrl+Enter)"
    : "The compiler pipeline targets MiniLang only";
}

/* -------------------------------------------------------------------------
   Menus
   ------------------------------------------------------------------------- */

/** Menu definitions: label, optional shortcut, and the action to run. */
const MENUS = {
  file: [
    { label: "Load demo program", key: "", run: loadDemo },
    { label: "New empty file", key: "", run: () => editor.setValue("") },
    { sep: true },
    { label: "Download source…", key: "", run: downloadSource },
  ],
  edit: [
    { label: "Select all", key: "Ctrl+A", run: () => editor.focus() },
    { label: "Find in tokens", key: "", run: () => { activateTab("lexical"); $("#token-filter").focus(); } },
  ],
  build: [
    { label: "Build", key: "Ctrl+Enter", run: build },
    { label: "Clear build log", key: "", run: () => { $("#out-build").textContent = ""; } },
  ],
  view: [
    { label: "Toggle output panel", key: "", run: toggleOutput },
    { sep: true },
    { label: "Source", key: "Alt+1", run: () => activateTab("source") },
    { label: "Lexical", key: "Alt+2", run: () => activateTab("lexical") },
  ],
  help: [
    { label: "API documentation", key: "", run: () => window.open("/docs", "_blank") },
    { label: "Health check", key: "", run: () => window.open("/api/health", "_blank") },
  ],
};

/**
 * Open a menu-bar dropdown beneath its button.
 * @param {string} name - Menu id, e.g. `"file"`.
 * @param {Element} button - The button that was clicked.
 * @returns {void}
 */
function openMenu(name, button) {
  const dropdown = $("#menu-dropdown");
  const items = MENUS[name] || [];
  dropdown.innerHTML = items
    .map((item, i) =>
      item.sep
        ? '<div class="menu-sep"></div>'
        : `<button class="menu-row" data-i="${i}">` +
          `<span>${esc(item.label)}</span><kbd>${esc(item.key || "")}</kbd></button>`
    )
    .join("");

  const rect = button.getBoundingClientRect();
  dropdown.style.left = `${rect.left}px`;
  dropdown.style.top = `${rect.bottom + 2}px`;
  dropdown.hidden = false;

  dropdown.querySelectorAll(".menu-row").forEach((row) => {
    row.addEventListener("click", () => {
      closeMenus();
      const item = items[Number(row.dataset.i)];
      if (item && item.run) item.run();
    });
  });

  $$(".menu-item").forEach((m) => m.classList.toggle("is-open", m.dataset.menu === name));
}

/**
 * Close any open menu-bar dropdown.
 * @returns {void}
 */
function closeMenus() {
  $("#menu-dropdown").hidden = true;
  $$(".menu-item").forEach((m) => m.classList.remove("is-open"));
}

/**
 * Offer the editor's contents as a file download.
 * @returns {void}
 */
function downloadSource() {
  const names = { minilang: "source.ml", c: "main.c", cpp: "main.cpp", java: "Main.java" };
  const blob = new Blob([editor.getValue()], { type: "text/plain" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = names[editor.getLanguage()] || "source.txt";
  anchor.click();
  URL.revokeObjectURL(url);
}

/**
 * Collapse or expand the bottom output pane.
 * @returns {void}
 */
function toggleOutput() {
  const pane = $("#outputpane");
  const collapsed = pane.classList.toggle("is-collapsed");
  $("#btn-toggle-output").innerHTML = collapsed ? "&#9652;" : "&#9662;";
  $("#splitter-output").style.display = collapsed ? "none" : "";
}

/* -------------------------------------------------------------------------
   Splitters
   ------------------------------------------------------------------------- */

/**
 * Make a splitter drag-resize an adjacent pane.
 * @param {string} selector - CSS selector of the splitter element.
 * @param {"x"|"y"} axis - Drag axis.
 * @param {function(number):void} apply - Called with the new size in pixels.
 * @returns {void}
 */
function makeSplitter(selector, axis, apply) {
  const splitter = $(selector);
  if (!splitter) return;

  splitter.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    splitter.classList.add("is-dragging");
    splitter.setPointerCapture(event.pointerId);

    const move = (moveEvent) => apply(axis === "x" ? moveEvent.clientX : moveEvent.clientY);
    const up = () => {
      splitter.classList.remove("is-dragging");
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      renderTokenWindow();
    };

    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  });
}

/* -------------------------------------------------------------------------
   Wiring
   ------------------------------------------------------------------------- */

/**
 * Create the editor, attach every listener, and perform the initial load.
 * @returns {void}
 */
function init() {
  editor = window.CodeEditor.create({
    mount: "#editor",
    language: "minilang",
    onCursor(metrics) {
      $("#status-caret").textContent = `Ln ${metrics.line}, Col ${metrics.col}`;
      $("#status-doc").textContent =
        `${metrics.lines} lines` + (metrics.selected ? `, ${metrics.selected} selected` : "");
    },
  });

  $$(".tab").forEach((tab) =>
    tab.addEventListener("click", () => activateTab(tab.dataset.tab))
  );
  $$(".out-tab").forEach((tab) =>
    tab.addEventListener("click", () => activateOutTab(tab.dataset.out))
  );

  $("#btn-compile").addEventListener("click", build);
  $("#btn-run").addEventListener("click", runProgram);
  $("#btn-toggle-output").addEventListener("click", toggleOutput);
  $("#btn-tree-expand").addEventListener("click", () => treeHandle && treeHandle.expandAll());
  $("#btn-tree-collapse").addEventListener("click", () => treeHandle && treeHandle.collapseAll());
  $("#btn-tree-reset").addEventListener("click", () => treeHandle && treeHandle.resetView());
  $("#opt-eval-order").addEventListener("change", applyEvalOrder);
  $("#language-select").addEventListener("change", (e) => setLanguage(e.target.value));
  $("#token-filter").addEventListener("input", () => renderTokens((lastResult || {}).tokens || []));
  $("#token-viewport").addEventListener("scroll", renderTokenWindow, { passive: true });
  $("#symbol-filter").addEventListener("input", () =>
    renderSymbols((lastResult || {}).symbols || [])
  );
  $("#symbol-viewport").addEventListener("scroll", renderSymbolWindow, { passive: true });
  $$("#icg-toolbar .seg-btn").forEach((btn) =>
    btn.addEventListener("click", () => activateIcgView(btn.dataset.icgView))
  );
  $$('.seg-toggle[aria-label="Parser Theory view"] .seg-btn').forEach((btn) =>
    btn.addEventListener("click", () => activateTheoryView(btn.dataset.theoryView))
  );
  $$('.seg-toggle[aria-label="Syntax view"] .seg-btn').forEach((btn) =>
    btn.addEventListener("click", () => activateSyntaxView(btn.dataset.syntaxView))
  );
  $("#btn-trace-play").addEventListener("click", toggleTracePlay);
  $("#btn-trace-step").addEventListener("click", () => {
    pauseTrace();
    stepTrace();
  });
  $("#btn-trace-reset").addEventListener("click", () => {
    pauseTrace();
    setTraceStep(0);
  });
  $("#trace-slider").addEventListener("input", (e) => {
    pauseTrace();
    setTraceStep(Number(e.target.value));
  });
  $("#btn-theory-analyze").addEventListener("click", analyzeGrammar);
  $("#btn-theory-load-expr").addEventListener("click", () => {
    $("#theory-grammar-input").value = demoGrammarText;
    $("#theory-start-symbol").value = "";
    analyzeGrammar();
  });
  $("#btn-theory-load-ambiguous").addEventListener("click", () => {
    $("#theory-grammar-input").value = ambiguousGrammarText;
    $("#theory-start-symbol").value = "";
    analyzeGrammar();
  });
  $$("#opt-toggles input[data-opt]").forEach((input) =>
    input.addEventListener("change", () => {
      if (lastResult) build();
    })
  );
  window.addEventListener("resize", renderTokenWindow);
  window.addEventListener("resize", renderSymbolWindow);
  window.addEventListener("resize", () => cfgHandle && cfgHandle.resetView());

  $$(".menu-item").forEach((button) =>
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      if (button.classList.contains("is-open")) closeMenus();
      else openMenu(button.dataset.menu, button);
    })
  );
  document.addEventListener("click", closeMenus);

  $$(".tree-file").forEach((file) =>
    file.addEventListener("click", () => {
      $$(".tree-file").forEach((f) => f.classList.remove("is-active"));
      file.classList.add("is-active");
      if (file.dataset.open === "demo") loadDemo();
      if (file.dataset.open === "scratch") editor.setValue("");
      if (file.dataset.open === "grammar") {
        activateTab("theory");
        $("#theory-grammar-input").value = demoGrammarText;
        analyzeGrammar();
      }
      if (file.dataset.open === "ambiguous") {
        activateTab("theory");
        $("#theory-grammar-input").value = ambiguousGrammarText;
        analyzeGrammar();
      }
    })
  );

  makeSplitter("#splitter-sidebar", "x", (x) => {
    const width = Math.min(Math.max(x, 150), 460);
    $("#sidebar").style.width = `${width}px`;
  });
  makeSplitter("#splitter-output", "y", (y) => {
    const height = Math.min(Math.max(window.innerHeight - y, 40), window.innerHeight - 240);
    $("#outputpane").style.height = `${height}px`;
  });

  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      build();
    }
    if (event.altKey && event.key >= "1" && event.key <= "8") {
      const names = [
        "source", "lexical", "theory", "syntax",
        "semantic", "icg", "optimization", "target",
      ];
      event.preventDefault();
      activateTab(names[Number(event.key) - 1]);
    }
    if (event.key === "Escape") closeMenus();
  });

  setLanguage("minilang");
  loadHealth();
  loadDemo();
}

document.addEventListener("DOMContentLoaded", init);
