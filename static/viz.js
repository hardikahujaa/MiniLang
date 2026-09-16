/* ==========================================================================
   MiniLang IDE -- D3 rendering layer

   All SVG drawing lives here, behind a single global `Viz` namespace, so that
   app.js stays concerned with data flow and this file stays concerned with
   pixels. Each build step adds one renderer:

     Tier A, step 3 -> Viz.renderTree      (AST / parse tree)   <- implemented
     Tier A, step 5 -> Viz.renderCfg       (control-flow graph)
     Tier B, step 8 -> Viz.renderAutomaton (LR(0) item sets, DFA)

   D3 is vendored at static/lib/d3.v7.min.js, so nothing here needs a network.
   ========================================================================== */

"use strict";

const Viz = (() => {
  /** Vertical spacing between sibling nodes, in pixels. */
  const ROW_GAP = 26;

  /** Horizontal spacing between tree depths, in pixels. */
  const DEPTH_GAP = 190;

  /** Node colours, keyed by the `kind` field the backend attaches. */
  const KIND_COLOURS = {
    program: "var(--violet)",
    function: "var(--syn-func)",
    param: "var(--syn-type)",
    block: "var(--fg-faint)",
    declaration: "var(--syn-type)",
    assign: "var(--syn-operator)",
    control: "var(--syn-keyword)",
    io: "var(--syn-keyword)",
    statement: "var(--fg-muted)",
    binary: "var(--syn-operator)",
    unary: "var(--syn-operator)",
    call: "var(--syn-func)",
    literal: "var(--syn-number)",
    identifier: "var(--fg)",
  };

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
    p.className = "viz-placeholder";
    p.textContent = message;
    host.appendChild(p);
  }

  /**
   * Count the nodes and maximum depth of a serialised tree.
   *
   * @param {Object} data - Root node, with optional `children`.
   * @returns {{nodes: number, depth: number}} Totals for the whole subtree.
   */
  function measure(data) {
    let nodes = 0;
    let depth = 0;

    // Iterative, not recursive: a deep tree would otherwise risk the JS stack,
    // and this runs on every render.
    const stack = [{ node: data, level: 1 }];
    while (stack.length > 0) {
      const { node, level } = stack.pop();
      nodes += 1;
      if (level > depth) depth = level;
      const children = node.children || [];
      for (let i = 0; i < children.length; i += 1) {
        stack.push({ node: children[i], level: level + 1 });
      }
    }
    return { nodes: nodes, depth: depth };
  }

  /**
   * Render a serialised AST as an interactive, collapsible tree.
   *
   * The backend's `to_dict()` output already carries `name` and `children`,
   * which are exactly `d3.hierarchy`'s default accessors, so no transformation
   * is needed here.
   *
   * Interaction: click a node to fold or unfold its subtree, scroll to zoom,
   * drag to pan.
   *
   * @param {string} selector - CSS selector of the container element.
   * @param {Object} data - The serialised root node.
   * @param {Object} [options] - Rendering options.
   * @param {boolean} [options.collapsed] - Start with deep nodes folded.
   * @returns {Object|null} A handle exposing `expandAll`, `collapseAll` and
   *   `resetView`, or null if nothing could be drawn.
   */
  function renderTree(selector, data, options) {
    const settings = options || {};
    const host = clear(selector);
    if (!host) return null;

    if (!isAvailable()) {
      placeholder(selector, "D3 failed to load, so the tree cannot be drawn.");
      return null;
    }
    if (!data) {
      placeholder(selector, "No tree to display. Press Build.");
      return null;
    }

    const d3 = window.d3;
    const width = host.clientWidth || 900;
    const height = host.clientHeight || 600;

    const svg = d3
      .select(host)
      .append("svg")
      .attr("class", "tree-svg")
      .attr("width", "100%")
      .attr("height", "100%");

    const viewport = svg.append("g");

    const zoom = d3
      .zoom()
      .scaleExtent([0.15, 2.5])
      .on("zoom", (event) => viewport.attr("transform", event.transform));
    svg.call(zoom);

    const root = d3.hierarchy(data);
    root.x0 = height / 2;
    root.y0 = 0;

    // Fold anything below the second level, so a large program opens readable
    // rather than as an unreadable wall of nodes.
    if (settings.collapsed !== false) {
      root.descendants().forEach((node) => {
        if (node.depth >= 2 && node.children) {
          node._children = node.children;
          node.children = null;
        }
      });
    }

    const layout = d3.tree().nodeSize([ROW_GAP, DEPTH_GAP]);
    const diagonal = d3
      .linkHorizontal()
      .x((node) => node.y)
      .y((node) => node.x);

    /**
     * Lay out and draw the tree from its current expanded/collapsed state.
     * @param {Object} source - Node the transition should originate from.
     * @returns {void}
     */
    function draw(source) {
      layout(root);

      const nodes = root.descendants();
      const links = root.links();

      const transition = svg.transition().duration(200);

      // --- links ---
      const link = viewport
        .selectAll("path.tree-link")
        .data(links, (d) => d.target.id || (d.target.id = nextId()));

      link
        .enter()
        .append("path")
        .attr("class", "tree-link")
        .attr("d", () => {
          const origin = { x: source.x0, y: source.y0 };
          return diagonal({ source: origin, target: origin });
        })
        .merge(link)
        .transition(transition)
        .attr("d", diagonal);

      link
        .exit()
        .transition(transition)
        .remove()
        .attr("d", () => {
          const origin = { x: source.x, y: source.y };
          return diagonal({ source: origin, target: origin });
        });

      // --- nodes ---
      const node = viewport
        .selectAll("g.tree-node")
        .data(nodes, (d) => d.id || (d.id = nextId()));

      const entering = node
        .enter()
        .append("g")
        .attr("class", "tree-node")
        .attr("transform", () => `translate(${source.y0},${source.x0})`)
        .on("click", (event, d) => {
          if (d.children) {
            d._children = d.children;
            d.children = null;
          } else {
            d.children = d._children;
            d._children = null;
          }
          draw(d);
        });

      entering
        .append("circle")
        .attr("r", 4.5)
        .attr("fill", (d) => KIND_COLOURS[d.data.kind] || "var(--fg-muted)");

      entering
        .append("text")
        .attr("dy", "0.32em")
        .attr("x", 9)
        .attr("text-anchor", "start")
        .text((d) => d.data.name);

      entering.append("title").text((d) => {
        const where = `line ${d.data.line}, col ${d.data.column}`;
        return `${d.data.node} - ${where}`;
      });

      const merged = entering.merge(node);
      merged.transition(transition).attr("transform", (d) => `translate(${d.y},${d.x})`);
      merged
        .select("circle")
        .attr("stroke", (d) => (d._children ? "var(--accent)" : "none"))
        .attr("stroke-width", 2.5);

      node
        .exit()
        .transition(transition)
        .attr("transform", () => `translate(${source.y},${source.x})`)
        .remove();

      nodes.forEach((d) => {
        d.x0 = d.x;
        d.y0 = d.y;
      });
    }

    let idCounter = 0;
    /**
     * Return a stable identity for D3's data join.
     * @returns {number} A unique integer.
     */
    function nextId() {
      idCounter += 1;
      return idCounter;
    }

    /**
     * Centre the tree in the viewport at a readable scale.
     * @returns {void}
     */
    function resetView() {
      svg
        .transition()
        .duration(250)
        .call(zoom.transform, d3.zoomIdentity.translate(70, height / 2).scale(0.85));
    }

    draw(root);
    resetView();

    return {
      /**
       * Unfold every node in the tree.
       * @returns {void}
       */
      expandAll() {
        root.descendants().forEach((node) => {
          if (node._children) {
            node.children = node._children;
            node._children = null;
          }
        });
        draw(root);
      },

      /**
       * Fold everything below the root's immediate children.
       * @returns {void}
       */
      collapseAll() {
        root.descendants().forEach((node) => {
          if (node.depth >= 1 && node.children) {
            node._children = node.children;
            node.children = null;
          }
        });
        draw(root);
      },

      resetView: resetView,
    };
  }

  return {
    isAvailable: isAvailable,
    clear: clear,
    placeholder: placeholder,
    measure: measure,
    renderTree: renderTree,
  };
})();

window.Viz = Viz;
