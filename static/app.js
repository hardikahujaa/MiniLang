/* ==========================================================================
   MiniLang Compiler Visualizer -- frontend controller

   Responsibilities:
     - tab switching
     - editor state and the "load demo" action
     - POST /compile, and dispatching the response to each tab's renderer

   Design rule from the plan (section 3): /compile returns every phase's
   artefacts in one object, so this file is pure rendering. There is no state
   machine here, no per-phase request sequencing, and no orchestration --
   exactly one fetch, then one render pass per tab.

   Scaffold status: the render functions for phases that do not exist yet are
   registered but inert. Each build step replaces one of them.
   ========================================================================== */

"use strict";

/** Shape of the last successful /compile response, kept for re-renders. */
let lastCompileResult = null;

/* -------------------------------------------------------------------------
   DOM helpers
   ------------------------------------------------------------------------- */

/**
 * Shorthand for `document.querySelector`.
 * @param {string} selector - A CSS selector.
 * @returns {Element|null} The first matching element, or null.
 */
function $(selector) {
  return document.querySelector(selector);
}

/**
 * Shorthand for `document.querySelectorAll`, returned as a real array.
 * @param {string} selector - A CSS selector.
 * @returns {Element[]} All matching elements.
 */
function $$(selector) {
  return Array.from(document.querySelectorAll(selector));
}

/* -------------------------------------------------------------------------
   Status reporting
   ------------------------------------------------------------------------- */

/**
 * Update the header pill and the footer status line together.
 *
 * Keeping both in one function means the two indicators can never disagree,
 * which matters during a live demo where the footer is the thing on screen.
 *
 * @param {"idle"|"working"|"ok"|"error"} state - Visual state for the pill.
 * @param {string} label - Short text shown inside the pill.
 * @param {string} [detail] - Longer text for the footer; defaults to `label`.
 * @returns {void}
 */
function setStatus(state, label, detail) {
  const pill = $("#status-pill");
  pill.className = `pill pill-${state}`;
  pill.textContent = label;
  $("#footer-status").textContent = detail || label;
}

/* -------------------------------------------------------------------------
   Tabs
   ------------------------------------------------------------------------- */

/**
 * Activate one tab and reveal its panel, hiding the others.
 * @param {string} name - The `data-tab` value, e.g. `"lexical"`.
 * @returns {void}
 */
function activateTab(name) {
  $$(".tab").forEach((tab) => {
    const isActive = tab.dataset.tab === name;
    tab.classList.toggle("is-active", isActive);
    tab.setAttribute("aria-selected", String(isActive));
  });
  $$(".panel").forEach((panel) => {
    panel.classList.toggle("is-active", panel.id === `panel-${name}`);
  });
}

/* -------------------------------------------------------------------------
   Editor
   ------------------------------------------------------------------------- */

/**
 * Refresh the line/character counter shown above the editor.
 * @returns {void}
 */
function updateEditorMeta() {
  const source = $("#source-editor").value;
  const lines = source.length === 0 ? 0 : source.split("\n").length;
  $("#editor-meta").textContent = `${lines} lines · ${source.length} chars`;
}

/**
 * Read the optimisation checkboxes into the shape `/compile` expects.
 * @returns {Object<string, boolean>} Toggle state keyed by camelCase pass name.
 */
function readOptimizationToggles() {
  const toggles = {};
  $$("#opt-toggles input[data-opt]").forEach((input) => {
    toggles[input.dataset.opt] = input.checked;
  });
  return toggles;
}

/* -------------------------------------------------------------------------
   Per-phase renderers
   ------------------------------------------------------------------------- */

/**
 * Render the syntax-error list on the Source tab.
 *
 * Implemented now (rather than deferred with the other phases) because the
 * scaffold must show *something* when the backend reports a problem, and the
 * error list is what the error-recovery feature ultimately drives.
 *
 * @param {Array<Object>} errors - Entries from `CompileResponse.errors`.
 * @returns {void}
 */
function renderErrors(errors) {
  const wrap = $("#error-list");
  const body = $("#error-list-body");
  body.innerHTML = "";

  if (!errors || errors.length === 0) {
    wrap.hidden = true;
    return;
  }

  errors.forEach((err) => {
    const row = document.createElement("div");
    row.style.fontFamily = "var(--mono)";
    row.style.fontSize = "12px";
    row.style.marginBottom = "6px";
    const where = `line ${err.line}, col ${err.col}`;
    const fix = err.suggestion ? ` — suggested fix: ${err.suggestion}` : "";
    row.textContent = `${where}: ${err.message}${fix}`;
    body.appendChild(row);
  });

  wrap.hidden = false;
}

/**
 * Dispatch a `/compile` response to every tab's renderer.
 *
 * Renderers for unimplemented phases are intentionally absent; their panels
 * keep showing the empty state that names the build step which will fill them.
 *
 * @param {Object} result - The parsed `CompileResponse`.
 * @returns {void}
 */
function renderCompileResult(result) {
  lastCompileResult = result;

  renderErrors(result.errors);

  // Target tab: program output, once the VM exists (Tier A, step 7).
  $("#program-output").textContent = result.output && result.output.length > 0
    ? result.output
    : "—";

  // Enable Run only once codegen has actually produced assembly.
  $("#btn-run").disabled = !result.asm || result.asm.length === 0;
}

/* -------------------------------------------------------------------------
   Backend calls
   ------------------------------------------------------------------------- */

/**
 * POST the editor contents to `/compile` and render the result.
 *
 * Network and HTTP failures are surfaced in the status pill rather than thrown,
 * so a backend restart mid-demo degrades visibly instead of silently.
 *
 * @returns {Promise<void>} Resolves once the response has been rendered.
 */
async function compile() {
  const source = $("#source-editor").value;
  setStatus("working", "compiling…", "Compiling…");

  try {
    const response = await fetch("/compile", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source: source,
        optimizations: readOptimizationToggles(),
        explainErrors: false,
      }),
    });

    if (!response.ok) {
      const detail = await response.text();
      setStatus("error", `HTTP ${response.status}`, `Compile failed: ${detail.slice(0, 200)}`);
      return;
    }

    const result = await response.json();
    renderCompileResult(result);

    const meta = result.meta || {};
    const total = (meta.timings || []).find((t) => t.phase === "total");
    const timing = total ? ` in ${total.ms.toFixed(1)} ms` : "";
    setStatus(
      "ok",
      "compiled",
      `Compiled ${meta.sourceLines || 0} lines (${meta.sourceBytes || 0} bytes)${timing} · ` +
        `reached phase: ${meta.reachedPhase || "unknown"}`
    );
  } catch (err) {
    setStatus("error", "failed", `Could not reach the backend: ${err.message}`);
  }
}

/**
 * Fetch the canonical demo program and load it into the editor.
 * @returns {Promise<void>} Resolves once the editor has been populated.
 */
async function loadDemo() {
  try {
    const response = await fetch("/api/demo");
    if (!response.ok) {
      setStatus("error", "failed", `Could not load demo program (HTTP ${response.status}).`);
      return;
    }
    const data = await response.json();
    $("#source-editor").value = data.source;
    updateEditorMeta();
    setStatus("idle", "idle", "Demo program loaded. Press Compile.");
  } catch (err) {
    setStatus("error", "failed", `Could not load demo program: ${err.message}`);
  }
}

/**
 * Fetch `/api/health` and reflect version and implemented phases in the footer.
 * @returns {Promise<void>} Resolves once the footer has been updated.
 */
async function loadHealth() {
  try {
    const response = await fetch("/api/health");
    if (!response.ok) return;
    const data = await response.json();
    $("#footer-version").textContent = `v${data.version}`;
    const phases = data.phasesImplemented || [];
    $("#footer-phases").textContent =
      phases.length > 0 ? `phases: ${phases.join(" → ")}` : "phases: none (scaffold)";
  } catch {
    $("#footer-phases").textContent = "phases: backend unreachable";
  }
}

/* -------------------------------------------------------------------------
   Wiring
   ------------------------------------------------------------------------- */

/**
 * Attach all event listeners and perform the initial load.
 * @returns {void}
 */
function init() {
  $$(".tab").forEach((tab) => {
    tab.addEventListener("click", () => activateTab(tab.dataset.tab));
  });

  $("#btn-compile").addEventListener("click", compile);
  $("#btn-load-demo").addEventListener("click", loadDemo);
  $("#source-editor").addEventListener("input", updateEditorMeta);

  // Ctrl/Cmd+Enter compiles from anywhere, including inside the editor.
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      compile();
    }
  });

  loadHealth();
  loadDemo();
}

document.addEventListener("DOMContentLoaded", init);
