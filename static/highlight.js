/* ==========================================================================
   MiniLang IDE -- syntax highlighting engine

   A small, dependency-free tokenizer used purely for *display*. It is
   deliberately separate from the real compiler lexer in app/lexer.py:

     - app/lexer.py is the compiler. It is exact, reports errors, and its
       output is what the Lexical tab shows.
     - this file is the editor's paint layer. It must never fail, never throw,
       and must stay fast while the user types.

   Conflating the two is a common mistake: it couples editor responsiveness to
   compiler correctness, and makes the editor unusable whenever the program is
   mid-edit and therefore invalid.

   Languages: MiniLang (the project's own), plus C, C++ and Java so the editor
   is useful for reading the kind of code this course deals with. Only MiniLang
   is compiled by the backend; see README for what that means per tab.
   ========================================================================== */

"use strict";

const Highlight = (() => {
  /** Keyword sets per language. Order within a set does not matter. */
  const KEYWORDS = {
    minilang: [
      "func", "int", "float", "bool", "void", "if", "else",
      "while", "return", "print", "true", "false",
    ],
    c: [
      "auto", "break", "case", "char", "const", "continue", "default", "do",
      "double", "else", "enum", "extern", "float", "for", "goto", "if",
      "inline", "int", "long", "register", "restrict", "return", "short",
      "signed", "sizeof", "static", "struct", "switch", "typedef", "union",
      "unsigned", "void", "volatile", "while", "NULL",
    ],
    cpp: [
      "alignas", "alignof", "and", "auto", "bool", "break", "case", "catch",
      "char", "class", "const", "constexpr", "const_cast", "continue",
      "decltype", "default", "delete", "do", "double", "dynamic_cast", "else",
      "enum", "explicit", "export", "extern", "false", "float", "for",
      "friend", "goto", "if", "inline", "int", "long", "mutable", "namespace",
      "new", "noexcept", "nullptr", "operator", "override", "private",
      "protected", "public", "reinterpret_cast", "return", "short", "signed",
      "sizeof", "static", "static_cast", "struct", "switch", "template",
      "this", "throw", "true", "try", "typedef", "typeid", "typename",
      "union", "unsigned", "using", "virtual", "void", "volatile", "while",
    ],
    java: [
      "abstract", "assert", "boolean", "break", "byte", "case", "catch",
      "char", "class", "const", "continue", "default", "do", "double", "else",
      "enum", "extends", "final", "finally", "float", "for", "goto", "if",
      "implements", "import", "instanceof", "int", "interface", "long",
      "native", "new", "null", "package", "private", "protected", "public",
      "record", "return", "sealed", "short", "static", "strictfp", "super",
      "switch", "synchronized", "this", "throw", "throws", "transient",
      "true", "false", "try", "var", "void", "volatile", "while",
    ],
  };

  /** Human-readable names for the language picker. */
  const LANGUAGE_NAMES = {
    minilang: "MiniLang",
    c: "C",
    cpp: "C++",
    java: "Java",
  };

  /**
   * Escape text so it can be injected as HTML.
   *
   * Every highlighted fragment passes through here. Skipping it would let a
   * program containing `<script>` execute inside the editor's paint layer.
   *
   * @param {string} text - Raw source text.
   * @returns {string} The text with HTML metacharacters escaped.
   */
  function escapeHtml(text) {
    return text
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  /**
   * Build the combined scanning regex for a language.
   *
   * One alternation covering every rule, scanned left to right, so the first
   * rule that matches at a position wins. Comments and strings come first so
   * that a keyword inside a comment is not highlighted as code.
   *
   * @param {string} language - Language id, e.g. `"cpp"`.
   * @returns {{pattern: RegExp, classes: string[]}} The regex and the CSS class
   *   belonging to each capture group, in order.
   */
  function buildPattern(language) {
    const keywords = KEYWORDS[language] || KEYWORDS.minilang;
    const keywordAlternation = keywords
      .slice()
      .sort((a, b) => b.length - a.length)
      .join("|");

    const rules = [
      // Block and line comments.
      ["tok-comment", "\\/\\*[\\s\\S]*?(?:\\*\\/|$)|\\/\\/[^\\n]*"],
    ];

    // C-family extras. MiniLang has no strings and no preprocessor.
    if (language !== "minilang") {
      rules.push(["tok-preproc", "^[ \\t]*#[^\\n]*"]);
      rules.push(["tok-string", '"(?:\\\\.|[^"\\\\\\n])*"' + "|'(?:\\\\.|[^'\\\\\\n])*'"]);
      if (language === "java") {
        rules.push(["tok-annotation", "@[A-Za-z_]\\w*"]);
      }
    }

    rules.push(["tok-keyword", "\\b(?:" + keywordAlternation + ")\\b"]);
    // A name immediately followed by '(' reads as a call or definition.
    rules.push(["tok-function", "\\b[A-Za-z_]\\w*(?=\\s*\\()"]);
    rules.push(["tok-number", "\\b\\d+\\.\\d+\\b|\\b\\d+\\b"]);
    rules.push(["tok-ident", "\\b[A-Za-z_]\\w*\\b"]);
    rules.push(["tok-operator", "[+\\-*/%!<>=&|^~?]+"]);
    rules.push(["tok-punct", "[(){}\\[\\];,:.]"]);

    const pattern = new RegExp(rules.map((r) => "(" + r[1] + ")").join("|"), "gm");
    return { pattern: pattern, classes: rules.map((r) => r[0]) };
  }

  /** Compiled patterns are reused across keystrokes rather than rebuilt. */
  const patternCache = {};

  /**
   * Return the cached scanning pattern for a language, building it on demand.
   * @param {string} language - Language id.
   * @returns {{pattern: RegExp, classes: string[]}} The compiled rule set.
   */
  function patternFor(language) {
    if (!patternCache[language]) {
      patternCache[language] = buildPattern(language);
    }
    return patternCache[language];
  }

  /**
   * Convert source text into highlighted HTML.
   *
   * Never throws: on any internal failure it falls back to escaped plain text,
   * because a broken highlighter must not be able to break the editor.
   *
   * @param {string} code - The source text to paint.
   * @param {string} language - Language id, e.g. `"minilang"`.
   * @returns {string} HTML with `<span class="tok-…">` wrappers.
   */
  function toHtml(code, language) {
    try {
      const { pattern, classes } = patternFor(language);
      let html = "";
      let lastIndex = 0;
      let match;

      pattern.lastIndex = 0;
      while ((match = pattern.exec(code)) !== null) {
        // Zero-length matches would spin forever; step past them.
        if (match.index === pattern.lastIndex) {
          pattern.lastIndex += 1;
          continue;
        }
        if (match.index > lastIndex) {
          html += escapeHtml(code.slice(lastIndex, match.index));
        }
        let className = null;
        for (let group = 1; group < match.length; group += 1) {
          if (match[group] !== undefined) {
            className = classes[group - 1];
            break;
          }
        }
        html += className
          ? '<span class="' + className + '">' + escapeHtml(match[0]) + "</span>"
          : escapeHtml(match[0]);
        lastIndex = match.index + match[0].length;
      }

      html += escapeHtml(code.slice(lastIndex));
      return html;
    } catch {
      return escapeHtml(code);
    }
  }

  /**
   * List the languages the editor can paint.
   * @returns {Array<{id: string, name: string}>} Language ids with display names.
   */
  function languages() {
    return Object.keys(LANGUAGE_NAMES).map((id) => ({ id: id, name: LANGUAGE_NAMES[id] }));
  }

  return { toHtml: toHtml, escapeHtml: escapeHtml, languages: languages, KEYWORDS: KEYWORDS };
})();

window.Highlight = Highlight;
