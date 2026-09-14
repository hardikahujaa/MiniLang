/* ==========================================================================
   MiniLang Compiler Visualizer -- D3 rendering layer

   All SVG drawing lives here, behind a single global `Viz` namespace, so that
   app.js stays concerned with data flow and this file stays concerned with
   pixels. Each build step adds one renderer:

     Tier A, step 3 -> Viz.renderTree   (parse tree and AST)
     Tier A, step 5 -> Viz.renderCfg    (control-flow graph)
     Tier B, step 8 -> Viz.renderAutomaton (LR(0) item sets, DFA)

   D3 is vendored at static/lib/d3.v7.min.js, so nothing here needs a network.
   ========================================================================== */

"use strict";

const Viz = (() => {
  /**
   * Report whether the vendored D3 bundle loaded successfully.
   *
   * Checked before any render call so that a missing or corrupt vendor file
   * produces a readable message in the panel instead of a console-only
   * `ReferenceError` during a demo.
   *
   * @returns {boolean} True when the global `d3` object is available.
   */
  function isAvailable() {
    return typeof window.d3 !== "undefined";
  }

  /**
   * Remove any previously rendered SVG from a container.
   * @param {string} selector - CSS selector of the container element.
   * @returns {Element|null} The cleared container, or null if not found.
   */
  function clear(selector) {
    const host = document.querySelector(selector);
    if (host) host.innerHTML = "";
    return host;
  }

  /**
   * Draw a short message inside a container, used when there is nothing to
   * visualise or when D3 is unavailable.
   *
   * @param {string} selector - CSS selector of the container element.
   * @param {string} message - Text to display.
   * @returns {void}
   */
  function placeholder(selector, message) {
    const host = clear(selector);
    if (!host) return;
    const p = document.createElement("p");
    p.style.color = "var(--text-faint)";
    p.style.fontSize = "13px";
    p.style.textAlign = "center";
    p.style.padding = "28px 0";
    p.textContent = message;
    host.appendChild(p);
  }

  return { isAvailable, clear, placeholder };
})();

window.Viz = Viz;
