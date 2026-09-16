# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries are grouped by the build step from the implementation plan, so the
history reads as the story of how the compiler was built rather than as a flat
list of commits.

---

## [Unreleased]

### Tier A · Step 2 — Lexer, and an IDE-grade frontend

#### Added

- **`app/lexer.py`** — phase 1 of the compiler. A hand-written, single-pass
  scanner producing tokens with exact 1-based line and column. Chosen over a
  regex-driven scanner because error recovery needs precise positions and
  specific messages ("invalid float literal `3.4.5`: more than one decimal
  point", not "no rule matched at offset 214"). Character classes are explicit
  ASCII range checks rather than `str.isalpha`/`str.isdigit`, which are
  Unicode-aware and would silently accept accented identifiers.
- **Error recovery in the scanner.** Malformed input never raises; it is
  collected and scanning continues. Where intent is unambiguous the scanner also
  repairs — a lone `&` reports the mistake, suggests `&&`, and emits that token
  so later phases still run.
- **A real editor** (`static/editor.js`) — line-number gutter, syntax
  highlighting, current-line band, auto-indent, Tab/Shift+Tab indentation,
  go-to-problem, and preserved native undo. Built from a transparent textarea
  over a highlighted `<pre>` rather than `contenteditable`, so undo, IME and
  accessibility keep working.
- **IDE shell** — menu bar, project tree, per-phase progress list, build log,
  problems panel and status bar with live Ln/Col. Dark theme only.
- **Syntax highlighting for C, C++ and Java** (`static/highlight.js`) in
  addition to MiniLang. Editing and colouring only: the compiler pipeline
  targets MiniLang, and Build is disabled with an explanation for the others.
- **`run.bat`** — one-click launcher. This machine has a Python on `PATH`
  belonging to Inkscape with no pip, and a second `uvicorn` belonging to the
  system Python without the project's dependencies; running the wrong one gave a
  confusing `ModuleNotFoundError`. The launcher always uses the project venv.
- **JavaScript syntax gate** — `node --check` in both the test suite and CI.
  Previously a stray character in `app.js` would break the entire UI with every
  Python test still green.
- **Cross-layer contract tests** — that the virtual table's row height agrees
  between JS and CSS, that the editor's two layers never disagree on a glyph
  metric, and that the editor highlights exactly the keywords the real lexer
  reserves.

#### Changed

- `POST /compile` runs the lexer and returns real tokens; `IMPLEMENTED_PHASES`
  gains `"lexer"` and `meta.timings` gains a per-phase entry.
- `Token` gains an optional `category` field so the token table can colour-code
  rows without duplicating the type-to-category mapping in JavaScript.
- Test count: 109 → 371.

#### Performance

Long programs were treated as a requirement, not an afterthought:

- The token table is **windowed** — only on-screen rows exist in the DOM, so a
  20 000-token program creates about 40 row elements.
- Repainting the editor is debounced, and highlighting disables itself above
  ~180 000 characters, where the cost is parsing generated HTML rather than
  scanning.
- The caret's line is cached rather than recomputed on every scroll event, which
  would otherwise be O(document length) at 60 fps.

#### Fixed

- Commit authorship on the ten scaffold commits, which used an address not
  registered to the GitHub account and so attributed every commit to an unlinked
  stranger. Rewritten before pushing; the base commit was left untouched.
- The `origin` remote and every README URL, which pointed at a different
  repository than the one this project lives in.

---

### Tier A · Step 1 — Project scaffold

The goal of this step was narrow and deliberate: get `uvicorn app.main:app
--reload` serving the eight-tab UI, with `/compile` returning the complete JSON
contract, **before** writing a single line of compiler code. Every later step
fills one slot in a contract that already exists and is already under test.

#### Added

- **FastAPI application** (`app/main.py`) with the three endpoints from the
  plan — `POST /compile`, `POST /run`, `POST /analyze-grammar` — plus
  `GET /api/health` (reports which phases are implemented) and `GET /api/demo`
  (serves the canonical demo program). All three POST routes are stubs that
  validate input and return schema-correct empty payloads.
- **The `/compile` JSON contract** (`app/schemas.py`) as typed Pydantic models
  covering every phase's artefacts: tokens, DFA, parse trace, parse tree, AST,
  symbols, type errors, TAC, basic blocks, CFG, optimisation results, assembly,
  program output and syntax errors. Python is `snake_case`, the wire is
  `camelCase`.
- **Eight-tab frontend** (`static/`) — hand-written HTML, CSS and vanilla JS
  with no npm, no bundler and no build step. Each not-yet-implemented panel
  shows an empty state naming the build step that will fill it.
- **D3 v7.9.0 vendored** to `static/lib/`, on day one rather than day fourteen,
  so the demo never depends on the venue having internet access.
- **Structured JSON logging** (`app/logging_config.py`) with per-request
  timing. Backend code does not call `print()`.
- **Canonical demo program** (`app/samples.py`, `tests/programs/demo.ml`) and
  the two sample grammars for the Parser Theory Lab, kept byte-identical by a
  test.
- **79 tests** covering the HTTP contract, the schema translation layer, the
  logging formatter, and the sample fixtures — including deliberately malformed
  requests for every endpoint.
- **GitHub Actions CI** running ruff, `black --check` and pytest on Python 3.10
  and 3.12, then booting the server and hitting `/api/health`, `/` and
  `/compile` to prove the app really starts.
- **Tooling configuration** (`pyproject.toml`): black at 100 columns; ruff
  enforcing type annotations (`ANN`), docstrings (`D`, Google convention), no
  `print()` (`T20`) and no blind excepts (`BLE`).
- `.env.example` documenting every environment variable. All are optional; the
  project runs fully offline with none of them set.

#### Notes

- Optimisation toggles are `StrictBool` rather than `bool`. Pydantic's default
  lax mode coerces `"yes"` into `True`, which would let a frontend bug silently
  *enable* an optimisation pass instead of failing loudly. A test pins this.
