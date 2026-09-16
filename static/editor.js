/* ==========================================================================
   MiniLang IDE -- code editor component

   A real editor pane built from three precisely-aligned layers:

     gutter   line numbers, scrolled in lockstep with the code
     paint    a <pre> holding syntax-highlighted, read-only HTML
     input    a transparent <textarea> that owns the caret, selection,
              clipboard, undo stack and IME

   The transparent-textarea technique is used instead of a contenteditable so
   that native editing behaviour -- undo/redo, autocorrect, accessibility,
   mobile keyboards -- keeps working. contenteditable would mean reimplementing
   all of it, badly.

   Alignment is the whole trick: paint and input must share font, size,
   line-height, letter-spacing, padding, tab-size and white-space exactly, or
   the caret drifts from the glyphs. Those properties live in one CSS block
   (`.editor-layer`) for that reason -- do not set them per-layer.

   Performance on long files
   -------------------------
   Repainting is debounced, and above LARGE_FILE_CHARS highlighting switches
   off entirely and the editor renders plain text. Beyond that size the cost is
   not the scan (a few ms) but parsing a megabyte of generated HTML on every
   keystroke. Degrading to plain text keeps typing responsive, which matters
   more than colour.
   ========================================================================== */

"use strict";

const CodeEditor = (() => {
  /** Above this many characters, highlighting is disabled to stay responsive. */
  const LARGE_FILE_CHARS = 180000;

  /** Milliseconds of idle typing before the paint layer is refreshed. */
  const REPAINT_DELAY_MS = 90;

  /** Spaces inserted by the Tab key. Matches the demo program's indentation. */
  const INDENT = "    ";

  /**
   * Create an editor bound to an existing container element.
   *
   * @param {Object} options - Editor options.
   * @param {string} options.mount - CSS selector of the container element.
   * @param {string} [options.language] - Initial language id for highlighting.
   * @param {function(Object):void} [options.onCursor] - Called with
   *   `{line, col, lines, chars, selected}` whenever the caret or text moves.
   * @param {function(string):void} [options.onChange] - Called with the full
   *   text after each edit.
   * @returns {Object} The editor's public API.
   */
  function create(options) {
    const host = document.querySelector(options.mount);
    if (!host) {
      throw new Error("CodeEditor: mount element not found: " + options.mount);
    }

    // All mutable state is declared here, before any function that closes over
    // it, so that no helper can be called into a temporal dead zone.
    let language = options.language || "minilang";
    let repaintTimer = null;
    let highlightingEnabled = true;

    /** Number of gutter lines currently rendered; -1 forces a rebuild. */
    let renderedLineCount = -1;

    /** Cached rendered line height in pixels; 0 means "not yet measured". */
    let measuredHeight = 0;

    /** 1-based line holding the caret. Cached; see positionCurrentLine(). */
    let caretLine = 1;

    host.classList.add("editor-root");
    host.innerHTML =
      '<div class="editor-gutter" aria-hidden="true"></div>' +
      '<div class="editor-body">' +
      '<div class="editor-current-line" aria-hidden="true"></div>' +
      '<pre class="editor-layer editor-paint" aria-hidden="true"><code></code></pre>' +
      '<textarea class="editor-layer editor-input" spellcheck="false" autocomplete="off" ' +
      'autocapitalize="off" autocorrect="off" wrap="off"></textarea>' +
      "</div>";

    const gutter = host.querySelector(".editor-gutter");
    const body = host.querySelector(".editor-body");
    const currentLine = host.querySelector(".editor-current-line");
    const paintCode = host.querySelector(".editor-paint code");
    const input = host.querySelector(".editor-input");

    /* ------------------------------------------------------------------
       Rendering
       ------------------------------------------------------------------ */

    /**
     * Repaint the syntax-highlighted layer from the textarea's current value.
     *
     * The trailing newline matters: a `<pre>` collapses a final newline, which
     * would make the paint layer one line shorter than the textarea and push
     * the two out of vertical alignment at the bottom of the file.
     *
     * @returns {void}
     */
    function repaint() {
      const text = input.value;
      if (!highlightingEnabled) {
        paintCode.textContent = text + "\n";
        return;
      }
      paintCode.innerHTML = window.Highlight.toHtml(text, language) + "\n";
    }

    /**
     * Rebuild the line-number gutter.
     *
     * Rebuilt only when the line count actually changes, since this is the
     * most expensive DOM operation in the editor on a long file.
     *
     * @returns {void}
     */
    function renderGutter() {
      const count = countLines(input.value);
      if (count === renderedLineCount) return;
      renderedLineCount = count;

      const numbers = new Array(count);
      for (let i = 0; i < count; i += 1) {
        numbers[i] = i + 1;
      }
      gutter.textContent = numbers.join("\n") + "\n";
    }

    /**
     * Count lines without allocating an array of every line.
     * @param {string} text - The editor contents.
     * @returns {number} The number of lines, minimum 1.
     */
    function countLines(text) {
      let count = 1;
      for (let i = 0; i < text.length; i += 1) {
        if (text.charCodeAt(i) === 10) count += 1;
      }
      return count;
    }

    /**
     * Move the paint layer and gutter to match the textarea's scroll position.
     *
     * Called on every scroll event, so it does no work beyond two assignments.
     *
     * @returns {void}
     */
    function syncScroll() {
      const top = input.scrollTop;
      const left = input.scrollLeft;
      paintCode.parentElement.scrollTop = top;
      paintCode.parentElement.scrollLeft = left;
      gutter.scrollTop = top;
      positionCurrentLine();
    }

    /**
     * Position the highlight band behind the line containing the caret.
     *
     * Reads `caretLine` rather than recomputing it. This runs on every scroll
     * event, and finding the caret's line is O(document length); recomputing it
     * at 60fps would make scrolling a long file crawl. The cached value is
     * refreshed only by events that can actually move the caret.
     *
     * @returns {void}
     */
    function positionCurrentLine() {
      const lineHeight = measuredLineHeight();
      const offset = (caretLine - 1) * lineHeight - input.scrollTop;
      if (offset < -lineHeight || offset > body.clientHeight) {
        currentLine.style.display = "none";
        return;
      }
      currentLine.style.display = "block";
      currentLine.style.transform = "translateY(" + offset + "px)";
      currentLine.style.height = lineHeight + "px";
    }

    /**
     * Return the rendered line height in pixels, measured once and cached.
     * @returns {number} Line height in CSS pixels.
     */
    function measuredLineHeight() {
      if (!measuredHeight) {
        const computed = window.getComputedStyle(input);
        measuredHeight = parseFloat(computed.lineHeight) || 21;
      }
      return measuredHeight;
    }

    /* ------------------------------------------------------------------
       Caret
       ------------------------------------------------------------------ */


    /**
     * Compute the caret's 1-based line and column, and the selection size.
     *
     * @returns {{line: number, col: number, lines: number, chars: number,
     *   selected: number}} Caret and document metrics.
     */
    function caretMetrics() {
      const pos = input.selectionStart;
      const text = input.value;
      let line = 1;
      let lastBreak = -1;
      for (let i = 0; i < pos; i += 1) {
        if (text.charCodeAt(i) === 10) {
          line += 1;
          lastBreak = i;
        }
      }
      caretLine = line;
      return {
        line: line,
        col: pos - lastBreak,
        lines: countLines(text),
        chars: text.length,
        selected: input.selectionEnd - input.selectionStart,
      };
    }

    /**
     * Notify the host of the current caret position and document size.
     * @returns {void}
     */
    function reportCursor() {
      if (options.onCursor) options.onCursor(caretMetrics());
    }

    /* ------------------------------------------------------------------
       Editing behaviour
       ------------------------------------------------------------------ */

    /**
     * Replace the current selection, preserving the native undo stack.
     *
     * `execCommand("insertText")` is deprecated but remains the only way to
     * write into a textarea without destroying undo history. The fallback
     * keeps the editor working where it is unavailable, at the cost of undo.
     *
     * @param {string} text - Text to insert.
     * @returns {void}
     */
    function insertText(text) {
      input.focus();
      let inserted = false;
      try {
        inserted = document.execCommand("insertText", false, text);
      } catch {
        inserted = false;
      }
      if (!inserted) {
        const start = input.selectionStart;
        const end = input.selectionEnd;
        input.value = input.value.slice(0, start) + text + input.value.slice(end);
        input.selectionStart = input.selectionEnd = start + text.length;
        handleInput();
      }
    }

    /**
     * Return the text of the line containing a given offset.
     * @param {string} text - Full document text.
     * @param {number} pos - Character offset.
     * @returns {string} The line's text up to `pos`.
     */
    function lineStartText(text, pos) {
      const start = text.lastIndexOf("\n", pos - 1) + 1;
      return text.slice(start, pos);
    }

    /**
     * Handle the IDE key bindings the editor owns.
     *
     * Tab must insert indentation rather than move focus, and Enter must keep
     * the current indentation and add a level after an opening brace. Without
     * these two, writing a nested block is unbearable.
     *
     * @param {KeyboardEvent} event - The key event.
     * @returns {void}
     */
    function handleKeydown(event) {
      // Let the application's own shortcuts through.
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") return;

      if (event.key === "Tab") {
        event.preventDefault();
        if (event.shiftKey) {
          outdentSelection();
        } else {
          insertText(INDENT);
        }
        return;
      }

      if (event.key === "Enter") {
        event.preventDefault();
        const before = lineStartText(input.value, input.selectionStart);
        const indentMatch = before.match(/^[ \t]*/);
        let indent = indentMatch ? indentMatch[0] : "";
        const opensBlock = /[{([]\s*$/.test(before);
        if (opensBlock) indent += INDENT;
        insertText("\n" + indent);
        return;
      }

      // Typing a closing brace on a blank line pulls it back one level.
      if (event.key === "}") {
        const before = lineStartText(input.value, input.selectionStart);
        if (/^[ \t]+$/.test(before) && before.endsWith(INDENT)) {
          event.preventDefault();
          const start = input.selectionStart;
          input.setSelectionRange(start - INDENT.length, start);
          insertText("}");
        }
      }
    }

    /**
     * Remove one indent level from the start of every selected line.
     * @returns {void}
     */
    function outdentSelection() {
      const text = input.value;
      const start = text.lastIndexOf("\n", input.selectionStart - 1) + 1;
      const end = input.selectionEnd;
      const block = text.slice(start, end);
      const dedented = block.replace(/^[ \t]{1,4}/gm, "");
      if (dedented === block) return;
      input.setSelectionRange(start, end);
      insertText(dedented);
    }

    /* ------------------------------------------------------------------
       Events
       ------------------------------------------------------------------ */

    /**
     * React to a text change: repaint, update the gutter, notify the host.
     * @returns {void}
     */
    function handleInput() {
      const shouldHighlight = input.value.length <= LARGE_FILE_CHARS;
      if (shouldHighlight !== highlightingEnabled) {
        highlightingEnabled = shouldHighlight;
        host.classList.toggle("is-plain", !highlightingEnabled);
      }

      renderGutter();
      reportCursor();

      if (repaintTimer) clearTimeout(repaintTimer);
      repaintTimer = setTimeout(() => {
        repaint();
        positionCurrentLine();
      }, REPAINT_DELAY_MS);

      if (options.onChange) options.onChange(input.value);
    }

    input.addEventListener("input", handleInput);
    input.addEventListener("keydown", handleKeydown);
    input.addEventListener("scroll", syncScroll, { passive: true });
    input.addEventListener("click", reportCursorAndLine);
    input.addEventListener("keyup", reportCursorAndLine);
    input.addEventListener("select", reportCursorAndLine);
    window.addEventListener("resize", positionCurrentLine);

    /**
     * Update both the status readout and the current-line band.
     * @returns {void}
     */
    function reportCursorAndLine() {
      reportCursor();
      positionCurrentLine();
    }

    /* ------------------------------------------------------------------
       Public API
       ------------------------------------------------------------------ */

    return {
      /**
       * Return the editor's contents.
       * @returns {string} The full text.
       */
      getValue() {
        return input.value;
      },

      /**
       * Replace the editor's contents and reset the caret to the start.
       * @param {string} text - New document text.
       * @returns {void}
       */
      setValue(text) {
        input.value = text;
        renderedLineCount = -1;
        highlightingEnabled = text.length <= LARGE_FILE_CHARS;
        host.classList.toggle("is-plain", !highlightingEnabled);
        renderGutter();
        repaint();
        input.selectionStart = input.selectionEnd = 0;
        input.scrollTop = 0;
        syncScroll();
        reportCursor();
        if (options.onChange) options.onChange(text);
      },

      /**
       * Switch the highlighting language and repaint immediately.
       * @param {string} next - Language id, e.g. `"java"`.
       * @returns {void}
       */
      setLanguage(next) {
        language = next;
        repaint();
      },

      /**
       * Return the current highlighting language.
       * @returns {string} The language id.
       */
      getLanguage() {
        return language;
      },

      /**
       * Move the caret to a 1-based line and column and scroll it into view.
       *
       * Used when the user clicks a diagnostic: jumping to the offending
       * position is the single most useful thing an error list can do.
       *
       * @param {number} line - 1-based target line.
       * @param {number} col - 1-based target column.
       * @returns {void}
       */
      goTo(line, col) {
        const lines = input.value.split("\n");
        const target = Math.min(Math.max(line, 1), lines.length);
        let offset = 0;
        for (let i = 0; i < target - 1; i += 1) {
          offset += lines[i].length + 1;
        }
        offset += Math.min(Math.max(col, 1) - 1, lines[target - 1].length);

        input.focus();
        input.setSelectionRange(offset, offset);
        const lineHeight = measuredLineHeight();
        input.scrollTop = Math.max(0, (target - 3) * lineHeight);
        syncScroll();
        reportCursor();
      },

      /**
       * Give the editor keyboard focus.
       * @returns {void}
       */
      focus() {
        input.focus();
      },

      /**
       * Report whether highlighting is currently active.
       * @returns {boolean} False when the file exceeded the size threshold.
       */
      isHighlighting() {
        return highlightingEnabled;
      },
    };
  }

  return { create: create, LARGE_FILE_CHARS: LARGE_FILE_CHARS };
})();

window.CodeEditor = CodeEditor;
