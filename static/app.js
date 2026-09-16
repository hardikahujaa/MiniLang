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

/** Handle returned by Viz.renderTree, exposing expand/collapse/reset. */
let treeHandle = null;

/** The AST most recently returned by /compile, kept for re-rendering. */
let lastAst = null;

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
  // Both of these measure their container, which is zero while hidden.
  if (name === "lexical") renderTokenWindow();
  if (name === "syntax" && lastAst && !treeHandle) {
    treeHandle = window.Viz.renderTree("#parse-tree-wrap", lastAst);
  }
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
  $("#syntax-empty").hidden = hasTree;
  $("#parse-tree-wrap").hidden = !hasTree;
  $("#tree-toolbar").hidden = !hasTree;

  if (!hasTree) {
    treeHandle = null;
    window.Viz.clear("#parse-tree-wrap");
    explainMissingTree();
    return;
  }

  const stats = window.Viz.measure(ast);
  $("#tree-stats").innerHTML =
    `<span><b>${stats.nodes}</b> nodes</span><span>depth <b>${stats.depth}</b></span>` +
    `<span><b>${ast.functionCount || 0}</b> functions</span>`;

  if ($("#panel-syntax").classList.contains("is-active")) {
    treeHandle = window.Viz.renderTree("#parse-tree-wrap", ast);
  }
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
  $("#btn-toggle-output").addEventListener("click", toggleOutput);
  $("#btn-tree-expand").addEventListener("click", () => treeHandle && treeHandle.expandAll());
  $("#btn-tree-collapse").addEventListener("click", () => treeHandle && treeHandle.collapseAll());
  $("#btn-tree-reset").addEventListener("click", () => treeHandle && treeHandle.resetView());
  $("#language-select").addEventListener("change", (e) => setLanguage(e.target.value));
  $("#token-filter").addEventListener("input", () => renderTokens((lastResult || {}).tokens || []));
  $("#token-viewport").addEventListener("scroll", renderTokenWindow, { passive: true });
  window.addEventListener("resize", renderTokenWindow);

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
      if (file.dataset.open === "grammar" || file.dataset.open === "ambiguous") {
        activateTab("theory");
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
