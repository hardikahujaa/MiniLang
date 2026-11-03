/* ==========================================================================
   MiniLang IDE -- D3 rendering layer

   All SVG drawing lives here, behind a single global `Viz` namespace, so that
   app.js stays concerned with data flow and this file stays concerned with
   pixels. Each build step adds one renderer:

     Tier A, step 3 -> Viz.renderTree      (AST / parse tree)   <- implemented
     Tier A, step 5 -> Viz.renderCfg       (control-flow graph) <- implemented
     Tier B, step 8 -> Viz.renderAutomaton (LR(0) item sets, DFA)

   D3 is vendored at static/lib/d3.v7.min.js, so nothing here needs a network.

   How the AST is drawn
   --------------------
   Top-down, root at the top centre, because that is how a syntax tree is drawn
   in every compiler text and because depth then reads as evaluation order: the
   deeper an operator sits, the tighter it binds and the sooner it runs.

   Each node is a box showing its formal AST class name on the first line and
   the datum it holds on the second -- `BinaryOp` over `( * )`, never a
   flattened caption like "func fib(int n): int". Each edge is labelled with
   the role the child fills in its parent (`left`, `right`, `cond`, `then`,
   `else`, `body`, ...), which is what makes the drawing unambiguous: `a - b`
   and `b - a` have identical node sets and differ only by those labels.

   Overlap is prevented structurally rather than by tuning constants. Boxes are
   sized to their text, so a fixed sibling gap would be wrong; instead the tree
   layout runs with `nodeSize([1, ROW_H])` and a separation function returning
   half of each neighbour's width plus a gap. Because dx is 1, that value is
   already in pixels, so adjacent boxes can never be closer than COL_GAP no
   matter how long their labels are.
   ========================================================================== */

"use strict";

const Viz = (() => {
  /** Height of a node box, in pixels. */
  const NODE_H = 38;

  /** Height of a node box that also shows an inferred type, in pixels. */
  const NODE_H_TYPED = 50;

  /** Vertical distance between one tree depth and the next, in pixels. */
  const ROW_H = NODE_H_TYPED + 54;

  /** Minimum clear space between two adjacent boxes, in pixels. */
  const COL_GAP = 24;

  /** Horizontal padding inside a node box, in pixels. */
  const PAD_X = 11;

  /** Width bounds for a node box, in pixels. Long labels ellipsize. */
  const MIN_W = 76;
  const MAX_W = 232;

  /** Fold the tree on first draw once it has more nodes than this. */
  const AUTO_COLLAPSE_ABOVE = 150;

  /** Depth at which an auto-folded tree is cut. */
  const AUTO_COLLAPSE_DEPTH = 4;

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
    type: "var(--syn-type)",
    list: "var(--fg-faint)",
  };

  /** Class names whose nodes denote an expression. */
  const EXPR_NODES = new Set([
    "BinaryOp",
    "UnaryOp",
    "CallExpr",
    "Literal",
    "Identifier",
  ]);

  /** Expression nodes that actually perform a step of computation. */
  const OPERATOR_NODES = new Set(["BinaryOp", "UnaryOp", "CallExpr"]);

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

  /* ---------------------------------------------------------------------
     Text metrics

     Box widths have to be known before the layout runs, so the text cannot
     simply be measured after it is in the DOM. A detached 2-D canvas gives
     exact advance widths for the same font the SVG will use.
     --------------------------------------------------------------------- */

  /** Lazily created measuring context. */
  let metricsCtx = null;

  /** Cache of font + text -> width, so repeated labels cost nothing. */
  const widthCache = new Map();

  /**
   * Resolve the stack behind the `--mono` custom property.
   * @returns {string} A CSS font-family list.
   */
  function monoFamily() {
    const declared = getComputedStyle(document.documentElement)
      .getPropertyValue("--mono")
      .trim();
    return declared || "Consolas, monospace";
  }

  /** Font used for the class-name line. */
  function classFont() {
    return `600 12px ${monoFamily()}`;
  }

  /** Font used for the value line. */
  function valueFont() {
    return `11px ${monoFamily()}`;
  }

  /**
   * Measure a string's rendered width.
   *
   * @param {string} text - The string to measure.
   * @param {string} font - A CSS shorthand font value.
   * @returns {number} Width in pixels.
   */
  function textWidth(text, font) {
    const key = `${font}\u0000${text}`;
    const cached = widthCache.get(key);
    if (cached !== undefined) return cached;

    if (metricsCtx === null) {
      metricsCtx = document.createElement("canvas").getContext("2d");
    }
    let width;
    if (metricsCtx) {
      metricsCtx.font = font;
      width = metricsCtx.measureText(text).width;
    } else {
      // No canvas (very old browser): fall back to a monospace estimate.
      width = text.length * 7;
    }
    widthCache.set(key, width);
    return width;
  }

  /**
   * Shorten a string with an ellipsis until it fits a pixel budget.
   *
   * @param {string} text - The string to fit.
   * @param {string} font - A CSS shorthand font value.
   * @param {number} budget - Available width in pixels.
   * @returns {string} The original string, or a truncated one ending in an ellipsis.
   */
  function fitText(text, font, budget) {
    if (textWidth(text, font) <= budget) return text;
    let low = 0;
    let high = text.length;
    while (low < high) {
      const mid = Math.ceil((low + high) / 2);
      if (textWidth(`${text.slice(0, mid)}…`, font) <= budget) low = mid;
      else high = mid - 1;
    }
    return `${text.slice(0, low)}…`;
  }

  /**
   * The value line drawn under a node's class name.
   * @param {Object} data - A serialised node.
   * @returns {string} The parenthesised datum, or an empty string.
   */
  function valueLine(data) {
    return data.detail === null || data.detail === undefined ? "" : `( ${data.detail} )`;
  }

  /**
   * The type line drawn under a node's value, when the analyser inferred one.
   *
   * Only expressions carry an inferred type, so the line's absence on a
   * statement node is information rather than a gap.
   *
   * @param {Object} data - A serialised node.
   * @returns {string} The type, delimited, or an empty string.
   */
  function typeLine(data) {
    return data.inferredType ? `\u2237 ${data.inferredType} \u2237` : "";
  }

  /** Font used for the inferred-type line. */
  function typeFont() {
    return `600 9.5px ${monoFamily()}`;
  }

  /**
   * Width of a node's box, memoised on the hierarchy node.
   *
   * @param {Object} node - A `d3.hierarchy` node.
   * @returns {number} Box width in pixels.
   */
  function boxWidth(node) {
    if (node.boxW === undefined) {
      const value = valueLine(node.data);
      const type = typeLine(node.data);
      const widest = Math.max(
        textWidth(node.data.node || "", classFont()),
        value ? textWidth(value, valueFont()) : 0,
        type ? textWidth(type, typeFont()) : 0
      );
      node.boxW = Math.max(MIN_W, Math.min(MAX_W, Math.ceil(widest) + PAD_X * 2));
    }
    return node.boxW;
  }

  /**
   * Height of a node's box: taller when it carries a type annotation.
   *
   * Sized per node rather than globally, so a tree with no semantic
   * information is not padded with empty space.
   *
   * @param {Object} node - A `d3.hierarchy` node.
   * @returns {number} Box height in pixels.
   */
  function boxHeight(node) {
    return typeLine(node.data) ? NODE_H_TYPED : NODE_H;
  }

  /* ---------------------------------------------------------------------
     Evaluation order
     --------------------------------------------------------------------- */

  /**
   * Number the operator nodes of every expression in evaluation order.
   *
   * Numbering restarts at 1 for each *maximal* expression subtree -- an
   * expression whose ancestors are all statements -- so each equation reads
   * independently rather than continuing a count from the previous line.
   *
   * The order is post-order: both operands are numbered before the operator
   * that combines them, which is exactly the order a machine evaluates them.
   * That is what makes precedence checkable by eye: in `a + b * c` the `*`
   * sits deeper and therefore gets the lower number.
   *
   * @param {Array<Object>} all - Every node of the hierarchy, collapsed or not.
   * @returns {void}
   */
  function assignEvalOrder(all) {
    all.forEach((node) => {
      node.evalOrder = null;
    });

    all.forEach((node) => {
      if (!EXPR_NODES.has(node.data.node)) return;
      for (let up = node.parent; up; up = up.parent) {
        if (EXPR_NODES.has(up.data.node)) return; // not the top of its expression
      }

      // Post-order over the whole subtree, including anything folded away, so
      // the numbers do not change when the user collapses a branch.
      let step = 0;
      const stack = [{ node: node, expanded: false }];
      while (stack.length > 0) {
        const frame = stack.pop();
        if (frame.expanded) {
          if (OPERATOR_NODES.has(frame.node.data.node)) {
            step += 1;
            frame.node.evalOrder = step;
          }
          continue;
        }
        stack.push({ node: frame.node, expanded: true });
        const kids = frame.node.children || frame.node._children || [];
        for (let i = kids.length - 1; i >= 0; i -= 1) {
          stack.push({ node: kids[i], expanded: false });
        }
      }
    });
  }

  /**
   * Render a serialised AST as an interactive, collapsible, top-down tree.
   *
   * The backend's `to_dict()` output carries `children`, which is
   * `d3.hierarchy`'s default accessor, plus `node`, `detail` and `edge` for
   * the label and the edge role.
   *
   * Interaction: click a node to fold or unfold its subtree, scroll to zoom,
   * drag to pan.
   *
   * @param {string} selector - CSS selector of the container element.
   * @param {Object} data - The serialised root node.
   * @param {Object} [options] - Rendering options.
   * @param {boolean} [options.collapsed] - Force folding on or off. Omitted,
   *   a large tree folds itself and a small one opens fully.
   * @returns {Object|null} A handle exposing `expandAll`, `collapseAll`,
   *   `resetView` and `setEvalOrder`, or null if nothing could be drawn.
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
    const linkLayer = viewport.append("g").attr("class", "tree-links");
    const labelLayer = viewport.append("g").attr("class", "tree-edge-labels");
    const nodeLayer = viewport.append("g").attr("class", "tree-nodes");

    const zoom = d3
      .zoom()
      .scaleExtent([0.05, 2.5])
      .on("zoom", (event) => viewport.attr("transform", event.transform));
    svg.call(zoom);

    const root = d3.hierarchy(data);
    const allNodes = root.descendants();
    assignEvalOrder(allNodes);

    root.x0 = 0;
    root.y0 = 0;

    // A small tree is most useful fully open; a large one is unreadable that
    // way, so it starts folded and the toolbar expands it on demand.
    const shouldFold =
      settings.collapsed === true ||
      (settings.collapsed !== false && allNodes.length > AUTO_COLLAPSE_ABOVE);
    if (shouldFold) {
      allNodes.forEach((node) => {
        if (node.depth >= AUTO_COLLAPSE_DEPTH && node.children) {
          node._children = node.children;
          node.children = null;
        }
      });
    }

    const layout = d3
      .tree()
      .nodeSize([1, ROW_H])
      .separation((a, b) => boxWidth(a) / 2 + boxWidth(b) / 2 + COL_GAP);

    const linkVertical = d3
      .linkVertical()
      .x((point) => point.x)
      .y((point) => point.y);

    /**
     * Path for one edge, drawn box-edge to box-edge rather than centre to
     * centre, so the curve never disappears under a node.
     *
     * @param {Object} source - `{x, y}` of the parent's centre.
     * @param {Object} target - `{x, y}` of the child's centre.
     * @returns {string} An SVG path.
     */
    function edgePath(source, target) {
      return linkVertical({
        source: { x: source.x, y: source.y + (source.h || NODE_H) / 2 },
        target: { x: target.x, y: target.y - (target.h || NODE_H) / 2 },
      });
    }

    /**
     * Describe a laid-out node as an endpoint for {@link edgePath}.
     * @param {Object} node - A laid-out `d3.hierarchy` node.
     * @returns {{x: number, y: number, h: number}} Its centre and box height.
     */
    function endpoint(node) {
      return { x: node.x, y: node.y, h: boxHeight(node) };
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
     * Lay out and draw the tree from its current expanded/collapsed state.
     * @param {Object} source - Node the transition should originate from.
     * @returns {void}
     */
    function draw(source) {
      layout(root);

      const nodes = root.descendants();
      const links = root.links();
      const transition = svg.transition().duration(220);
      const origin = { x: source.x0, y: source.y0 };

      // --- links ---
      const link = linkLayer
        .selectAll("path.tree-link")
        .data(links, (d) => d.target.id || (d.target.id = nextId()));

      link
        .enter()
        .append("path")
        .attr("class", "tree-link")
        .attr("d", () => edgePath(origin, origin))
        .merge(link)
        .transition(transition)
        .attr("d", (d) => edgePath(endpoint(d.source), endpoint(d.target)));

      link
        .exit()
        .transition(transition)
        .attr("d", () => edgePath({ x: source.x, y: source.y }, { x: source.x, y: source.y }))
        .remove();

      // --- edge role labels ---
      const label = labelLayer
        .selectAll("text.tree-edge-label")
        .data(
          links.filter((d) => Boolean(d.target.data.edge)),
          (d) => d.target.id || (d.target.id = nextId())
        );

      label
        .enter()
        .append("text")
        .attr("class", "tree-edge-label")
        .attr("text-anchor", "middle")
        .attr("dy", "0.32em")
        .text((d) => d.target.data.edge)
        .attr("x", origin.x)
        .attr("y", origin.y)
        .merge(label)
        .transition(transition)
        .attr("x", (d) => (d.source.x + d.target.x) / 2)
        .attr("y", (d) => (d.source.y + d.target.y) / 2);

      label.exit().remove();

      // --- nodes ---
      const node = nodeLayer
        .selectAll("g.tree-node")
        .data(nodes, (d) => d.id || (d.id = nextId()));

      const entering = node
        .enter()
        .append("g")
        .attr("class", "tree-node")
        .attr("transform", () => `translate(${origin.x},${origin.y})`)
        .on("click", (event, d) => {
          if (d.children) {
            d._children = d.children;
            d.children = null;
          } else if (d._children) {
            d.children = d._children;
            d._children = null;
          }
          draw(d);
        });

      entering
        .append("rect")
        .attr("rx", 5)
        .attr("ry", 5)
        .attr("width", (d) => boxWidth(d))
        .attr("height", (d) => boxHeight(d))
        .attr("x", (d) => -boxWidth(d) / 2)
        .attr("y", (d) => -boxHeight(d) / 2)
        .attr("stroke", (d) => KIND_COLOURS[d.data.kind] || "var(--fg-muted)");

      // Three layouts: class alone, class over value, or class over value over
      // inferred type. Every node carrying a type also carries a value, so a
      // class-plus-type box cannot arise.
      entering
        .append("text")
        .attr("class", "tree-node-class")
        .attr("text-anchor", "middle")
        .attr("y", (d) => (typeLine(d.data) ? -8 : valueLine(d.data) ? -2 : 4))
        .attr("fill", (d) => KIND_COLOURS[d.data.kind] || "var(--fg-muted)")
        .text((d) => fitText(d.data.node || "", classFont(), boxWidth(d) - PAD_X * 2));

      entering
        .append("text")
        .attr("class", "tree-node-value")
        .attr("text-anchor", "middle")
        .attr("y", (d) => (typeLine(d.data) ? 5 : 12))
        .text((d) => fitText(valueLine(d.data), valueFont(), boxWidth(d) - PAD_X * 2));

      entering
        .append("text")
        .attr("class", "tree-node-type")
        .attr("text-anchor", "middle")
        .attr("y", 18)
        .text((d) => fitText(typeLine(d.data), typeFont(), boxWidth(d) - PAD_X * 2));

      const badge = entering
        .append("g")
        .attr("class", "tree-eval")
        .attr(
          "transform",
          (d) => `translate(${boxWidth(d) / 2 - 3},${-boxHeight(d) / 2 + 3})`
        );
      badge.append("circle").attr("r", 8);
      badge
        .append("text")
        .attr("text-anchor", "middle")
        .attr("dy", "0.34em")
        .text((d) => d.evalOrder || "");

      entering
        .append("text")
        .attr("class", "tree-fold-count")
        .attr("text-anchor", "middle")
        .attr("y", (d) => boxHeight(d) / 2 + 13);

      entering.append("title").text((d) => {
        const where = `line ${d.data.line}, col ${d.data.column}`;
        const signature = d.data.signature ? `\n${d.data.signature}` : "";
        const role = d.data.edge ? `\nrole in parent: ${d.data.edge}` : "";
        const typed = d.data.inferredType ? `\ninferred type: ${d.data.inferredType}` : "";
        return `${d.data.name}\n${where}${role}${typed}${signature}`;
      });

      const merged = entering.merge(node);
      merged.transition(transition).attr("transform", (d) => `translate(${d.x},${d.y})`);
      merged.classed("is-folded", (d) => Boolean(d._children));
      merged.select("g.tree-eval").style("display", (d) => (d.evalOrder ? null : "none"));
      merged
        .select("text.tree-fold-count")
        .text((d) => (d._children ? `+${foldedCount(d)}` : ""));

      node
        .exit()
        .transition(transition)
        .attr("transform", () => `translate(${source.x},${source.y})`)
        .remove();

      nodes.forEach((d) => {
        d.x0 = d.x;
        d.y0 = d.y;
      });
    }

    /**
     * Count the nodes hidden underneath a folded node.
     * @param {Object} node - A folded `d3.hierarchy` node.
     * @returns {number} How many descendants are currently not drawn.
     */
    function foldedCount(node) {
      let total = 0;
      const stack = [...(node._children || [])];
      while (stack.length > 0) {
        const current = stack.pop();
        total += 1;
        const kids = current.children || current._children || [];
        for (let i = 0; i < kids.length; i += 1) stack.push(kids[i]);
      }
      return total;
    }

    /**
     * Frame the whole tree in the viewport.
     *
     * Fits to the real bounding box rather than a fixed scale: a 60-node tree
     * and a 600-node tree both have to land readable, and a hard-coded zoom
     * can only ever suit one of them.
     *
     * @returns {void}
     */
    function resetView() {
      const drawn = root.descendants();
      let minX = Infinity;
      let maxX = -Infinity;
      let minY = Infinity;
      let maxY = -Infinity;

      drawn.forEach((d) => {
        const half = boxWidth(d) / 2;
        if (d.x - half < minX) minX = d.x - half;
        if (d.x + half > maxX) maxX = d.x + half;
        const halfH = boxHeight(d) / 2;
        if (d.y - halfH < minY) minY = d.y - halfH;
        if (d.y + halfH > maxY) maxY = d.y + halfH;
      });

      const margin = 48;
      const spanX = Math.max(1, maxX - minX + margin * 2);
      const spanY = Math.max(1, maxY - minY + margin * 2);
      const scale = Math.max(0.05, Math.min(1.1, Math.min(width / spanX, height / spanY)));

      svg
        .transition()
        .duration(280)
        .call(
          zoom.transform,
          d3.zoomIdentity
            .translate(width / 2 - (scale * (minX + maxX)) / 2, height / 2 - (scale * (minY + maxY)) / 2)
            .scale(scale)
        );
    }

    draw(root);
    resetView();

    return {
      /**
       * Unfold every node in the tree.
       * @returns {void}
       */
      expandAll() {
        allNodes.forEach((node) => {
          if (node._children) {
            node.children = node._children;
            node._children = null;
          }
        });
        draw(root);
        resetView();
      },

      /**
       * Fold everything below the root's immediate children.
       * @returns {void}
       */
      collapseAll() {
        allNodes.forEach((node) => {
          if (node.depth >= 1 && node.children) {
            node._children = node.children;
            node.children = null;
          }
        });
        draw(root);
        resetView();
      },

      /**
       * Show or hide the evaluation-order badges.
       *
       * A class on the SVG rather than a redraw: the numbers are already on
       * every node, so toggling them is a style change, not a layout change.
       *
       * @param {boolean} on - True to show the numbering.
       * @returns {void}
       */
      setEvalOrder(on) {
        svg.classed("show-eval", Boolean(on));
      },

      resetView: resetView,
    };
  }

  /* ---------------------------------------------------------------------
     Control-flow graph (ICG tab)

     Drawn top-down per function, exactly like a textbook CFG figure: each
     basic block is a box holding its own three-address code, laid out by
     BFS distance from the function's entry block so that depth reads as
     "how many branches away from the top". Two or more functions are two or
     more disconnected components (MiniLang has no indirect calls, so `call`
     is deliberately not drawn as an edge -- see app/tac.py); each gets its
     own lane, side by side, labelled by the function it belongs to.

     A forward edge (to a strictly deeper level) is drawn as a straight
     vertical curve, same as the AST tree. A loop's back edge -- target at
     the same level or shallower -- would otherwise retrace a forward edge
     exactly in reverse, so it is bowed out to the side instead, which is
     the conventional way a compiler text draws a loop closing.
     --------------------------------------------------------------------- */

  /** Fixed column width every CFG node reserves, regardless of its own box width. */
  const CFG_CELL_W = 260;

  /** Vertical distance between one level and the next, in pixels. */
  const CFG_ROW_H = 130;

  /** Horizontal gap, in pixels, left between two disconnected components. */
  const CFG_COMPONENT_GAP = 70;

  /** Width bounds for a block box, in pixels. */
  const CFG_MIN_W = 150;
  const CFG_MAX_W = 236;

  /** Height, in pixels, of a block box's header row (its id and function). */
  const CFG_HEAD_H = 22;

  /** Height, in pixels, of one instruction line inside a block box. */
  const CFG_LINE_H = 15;

  /** Instruction lines shown before a block box is truncated with "+N more". */
  const CFG_MAX_LINES = 7;

  /** Resolve a CSS custom property to its computed value. */
  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  /**
   * Classify a CFG edge label into one of four fixed colour roles.
   * @param {string|null} label - The edge's label, e.g. `"true"`.
   * @returns {string} `"ok"`, `"err"`, `"faint"` or `"accent"`.
   */
  function edgeRole(label) {
    if (label === "true") return "ok";
    if (label === "false") return "err";
    if (label === "fall-through") return "faint";
    return "accent";
  }

  /** CSS custom property backing each edge role's colour. */
  const EDGE_ROLE_VAR = { ok: "--ok", err: "--err", faint: "--fg-faint", accent: "--accent" };

  /**
   * Font used inside a CFG block's instruction lines.
   * @returns {string} A CSS shorthand font value.
   */
  function cfgLineFont() {
    return `11px ${monoFamily()}`;
  }

  /**
   * Render a basic block's instruction text, truncated to a line budget.
   * @param {Object} block - A `BasicBlock` from `CompileResponse.blocks`.
   * @returns {string[]} The lines to draw inside the box, in order.
   */
  function cfgBlockLines(block) {
    const all = (block.instructions || []).map((instr) => instr.text);
    if (all.length <= CFG_MAX_LINES) return all;
    const shown = all.slice(0, CFG_MAX_LINES - 1);
    shown.push(`… +${all.length - (CFG_MAX_LINES - 1)} more`);
    return shown;
  }

  /**
   * Render a control-flow graph: one box per basic block, showing its own
   * three-address code, linked by labelled edges.
   *
   * Interaction: scroll to zoom, drag to pan -- same gesture set as the AST
   * tree, so switching between the Syntax and ICG tabs needs no relearning.
   *
   * @param {string} selector - CSS selector of the container element.
   * @param {Object} cfg - A `CfgView`: `{nodes, edges, entry, exits}`.
   * @returns {Object|null} A handle exposing `resetView`, or null if nothing
   *   could be drawn.
   */
  function renderCfg(selector, cfg) {
    const host = clear(selector);
    if (!host) return null;

    if (!isAvailable()) {
      placeholder(selector, "D3 failed to load, so the graph cannot be drawn.");
      return null;
    }
    if (!cfg || !Array.isArray(cfg.nodes) || cfg.nodes.length === 0) {
      placeholder(selector, "No control-flow graph to display. Press Build.");
      return null;
    }

    const d3 = window.d3;
    const width = host.clientWidth || 900;
    const height = host.clientHeight || 600;
    const nodes = cfg.nodes;
    const edges = cfg.edges || [];

    // --- layering: BFS distance from each component's entry block --------
    const byId = new Map(nodes.map((n) => [n.id, n]));
    const indegree = new Map(nodes.map((n) => [n.id, 0]));
    edges.forEach((e) => indegree.set(e.target, (indegree.get(e.target) || 0) + 1));

    const roots = nodes.filter((n) => (indegree.get(n.id) || 0) === 0).map((n) => n.id);
    if (roots.length === 0) roots.push(nodes[0].id);

    const level = new Map();
    const componentOf = new Map();
    roots.forEach((rootId, componentIndex) => {
      if (level.has(rootId)) return;
      level.set(rootId, 0);
      componentOf.set(rootId, componentIndex);
      const queue = [rootId];
      while (queue.length > 0) {
        const id = queue.shift();
        const current = byId.get(id);
        (current.successors || []).forEach((succId) => {
          if (!level.has(succId) && byId.has(succId)) {
            level.set(succId, level.get(id) + 1);
            componentOf.set(succId, componentIndex);
            queue.push(succId);
          }
        });
      }
    });
    // A node no root could reach (should not happen; defensive) gets its own slot.
    let strayComponent = roots.length;
    nodes.forEach((n) => {
      if (!level.has(n.id)) {
        level.set(n.id, 0);
        componentOf.set(n.id, strayComponent);
        strayComponent += 1;
      }
    });

    // --- sizing: each box measures its own header and instruction lines --
    nodes.forEach((n) => {
      const lines = cfgBlockLines(n);
      const widest = Math.max(
        textWidth(`${n.id}  ${n.function || ""}`, classFont()),
        ...lines.map((line) => textWidth(line, cfgLineFont())),
        0
      );
      n.boxW = Math.max(CFG_MIN_W, Math.min(CFG_MAX_W, Math.ceil(widest) + PAD_X * 2));
      n.boxH = CFG_HEAD_H + lines.length * CFG_LINE_H + 10;
      n.lines = lines;
    });

    // --- positions: group by (component, level), spread evenly per row ---
    const componentCount = new Set(componentOf.values()).size;
    const byComponent = Array.from({ length: componentCount }, () => new Map());
    nodes.forEach((n) => {
      const rows = byComponent[componentOf.get(n.id)];
      const lvl = level.get(n.id);
      if (!rows.has(lvl)) rows.set(lvl, []);
      rows.get(lvl).push(n.id);
    });

    let originX = 0;
    byComponent.forEach((rows) => {
      let widestRow = 1;
      rows.forEach((ids) => {
        widestRow = Math.max(widestRow, ids.length);
      });
      const componentW = widestRow * CFG_CELL_W;
      rows.forEach((ids, lvl) => {
        ids.forEach((id, i) => {
          const node = byId.get(id);
          node.x = originX + componentW / 2 + (i - (ids.length - 1) / 2) * CFG_CELL_W;
          node.y = lvl * CFG_ROW_H + 50;
        });
      });
      originX += componentW + CFG_COMPONENT_GAP;
    });

    // --- draw --------------------------------------------------------------
    const svg = d3
      .select(host)
      .append("svg")
      .attr("class", "cfg-svg")
      .attr("width", "100%")
      .attr("height", "100%");

    const defs = svg.append("defs");
    Object.keys(EDGE_ROLE_VAR).forEach((role) => {
      defs
        .append("marker")
        .attr("id", `cfg-arrow-${role}`)
        .attr("viewBox", "0 0 10 10")
        .attr("refX", 8)
        .attr("refY", 5)
        .attr("markerWidth", 7)
        .attr("markerHeight", 7)
        .attr("orient", "auto-start-reverse")
        .append("path")
        .attr("d", "M0,0L10,5L0,10Z")
        .attr("fill", cssVar(EDGE_ROLE_VAR[role]));
    });

    const viewport = svg.append("g");
    const linkLayer = viewport.append("g").attr("class", "cfg-links");
    const labelLayer = viewport.append("g").attr("class", "cfg-edge-labels");
    const nodeLayer = viewport.append("g").attr("class", "cfg-nodes");

    const zoom = d3
      .zoom()
      .scaleExtent([0.1, 2.5])
      .on("zoom", (event) => viewport.attr("transform", event.transform));
    svg.call(zoom);

    /**
     * Path for one edge. Forward edges (to a deeper level) curve straight
     * down, box-edge to box-edge; a back or lateral edge bows out to the
     * side so it cannot retrace a forward edge in reverse.
     *
     * @param {Object} source - The edge's source node, laid out.
     * @param {Object} target - The edge's target node, laid out.
     * @returns {string} An SVG path.
     */
    function edgePath(source, target) {
      const sx = source.x;
      const sy = source.y + source.boxH / 2;
      const tx = target.x;
      const ty = target.y - target.boxH / 2;

      if (target.y > source.y) {
        return d3.linkVertical().x((p) => p.x).y((p) => p.y)({
          source: { x: sx, y: sy },
          target: { x: tx, y: ty },
        });
      }

      const bow = Math.max(source.boxW, target.boxW) / 2 + 46;
      const bx = Math.max(sx, tx) + bow;
      return (
        `M${sx},${source.y} ` +
        `C ${bx},${source.y} ${bx},${target.y} ${tx},${target.y}`
      );
    }

    linkLayer
      .selectAll("path.cfg-link")
      .data(edges)
      .enter()
      .append("path")
      .attr("class", (d) => `cfg-link cfg-link-${edgeRole(d.label)}`)
      .attr("marker-end", (d) => `url(#cfg-arrow-${edgeRole(d.label)})`)
      .attr("d", (d) => edgePath(byId.get(d.source), byId.get(d.target)));

    labelLayer
      .selectAll("text.cfg-edge-label")
      .data(edges.filter((d) => d.label))
      .enter()
      .append("text")
      .attr("class", (d) => `cfg-edge-label cfg-edge-label-${edgeRole(d.label)}`)
      .attr("text-anchor", "middle")
      .attr("dy", "0.32em")
      .attr("x", (d) => (byId.get(d.source).x + byId.get(d.target).x) / 2)
      .attr("y", (d) => (byId.get(d.source).y + byId.get(d.target).y) / 2)
      .text((d) => d.label);

    const node = nodeLayer
      .selectAll("g.cfg-node")
      .data(nodes)
      .enter()
      .append("g")
      .attr("class", "cfg-node")
      .classed("is-exit", (d) => !(d.successors && d.successors.length))
      .attr("transform", (d) => `translate(${d.x},${d.y})`);

    node
      .append("rect")
      .attr("class", "cfg-node-rect")
      .attr("rx", 5)
      .attr("ry", 5)
      .attr("width", (d) => d.boxW)
      .attr("height", (d) => d.boxH)
      .attr("x", (d) => -d.boxW / 2)
      .attr("y", (d) => -d.boxH / 2);

    node
      .append("text")
      .attr("class", "cfg-node-head")
      .attr("text-anchor", "middle")
      .attr("x", 0)
      .attr("y", (d) => -d.boxH / 2 + 14)
      .text((d) => fitText(`${d.id}  ${d.function || ""}`, classFont(), d.boxW - PAD_X * 2));

    node
      .append("line")
      .attr("class", "cfg-node-rule")
      .attr("x1", (d) => -d.boxW / 2 + 6)
      .attr("x2", (d) => d.boxW / 2 - 6)
      .attr("y1", (d) => -d.boxH / 2 + CFG_HEAD_H - 4)
      .attr("y2", (d) => -d.boxH / 2 + CFG_HEAD_H - 4);

    node.each(function drawLines(d) {
      const group = d3.select(this);
      d.lines.forEach((line, i) => {
        group
          .append("text")
          .attr("class", "cfg-node-line")
          .attr("text-anchor", "middle")
          .attr("x", 0)
          .attr("y", -d.boxH / 2 + CFG_HEAD_H + 4 + i * CFG_LINE_H)
          .text(fitText(line, cfgLineFont(), d.boxW - PAD_X * 2));
      });
    });

    node.append("title").text((d) => (d.instructions || []).map((i) => i.text).join("\n"));

    // --- fit the whole graph in view ---------------------------------------
    let minX = Infinity;
    let maxX = -Infinity;
    let minY = Infinity;
    let maxY = -Infinity;
    nodes.forEach((d) => {
      if (d.x - d.boxW / 2 < minX) minX = d.x - d.boxW / 2;
      if (d.x + d.boxW / 2 > maxX) maxX = d.x + d.boxW / 2;
      if (d.y - d.boxH / 2 < minY) minY = d.y - d.boxH / 2;
      if (d.y + d.boxH / 2 > maxY) maxY = d.y + d.boxH / 2;
    });

    /**
     * Frame the whole graph in the viewport, fit to its real bounding box.
     * @returns {void}
     */
    function resetView() {
      const margin = 48;
      const spanX = Math.max(1, maxX - minX + margin * 2);
      const spanY = Math.max(1, maxY - minY + margin * 2);
      const scale = Math.max(0.1, Math.min(1.1, Math.min(width / spanX, height / spanY)));
      svg.call(
        zoom.transform,
        d3.zoomIdentity
          .translate(width / 2 - (scale * (minX + maxX)) / 2, height / 2 - (scale * (minY + maxY)) / 2)
          .scale(scale)
      );
    }

    resetView();
    return { resetView: resetView };
  }

  return {
    isAvailable: isAvailable,
    clear: clear,
    placeholder: placeholder,
    measure: measure,
    renderTree: renderTree,
    renderCfg: renderCfg,
  };
})();

window.Viz = Viz;
