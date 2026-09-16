# MiniLang Compiler Visualizer

[![CI](https://github.com/hardikahujaa/MiniLang/actions/workflows/ci.yml/badge.svg)](https://github.com/hardikahujaa/MiniLang/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![Linted with ruff](https://img.shields.io/badge/linted%20with-ruff-261230.svg)](https://github.com/astral-sh/ruff)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

An end-to-end compiler for **MiniLang**, a small statically-typed language, with
every compilation phase — lexical analysis through target code generation —
exposed as JSON and visualised live in the browser. It also ships a standalone
**parser theory lab** (FIRST/FOLLOW, LL(1), LR(0), SLR, LALR, conflict
detection) and a **syntax error recovery system** that suggests ranked fixes
instead of collapsing into a cascade of false errors.

The frontend is a real IDE, not a form: syntax-highlighted editor with a line
gutter, project tree, build log, problems list, and a status bar — dark theme,
no npm, no build step.

> **Status:** Tier A step 3 of 7 — lexer and parser are live. Press **Build**
> and tab 2 fills with the token stream, tab 4 with a navigable parse tree.
> Syntax errors appear in the Problems panel; click one to jump to it.
> Remaining phases land one per step; see [Build status](#build-status).

## Quick start

**Windows — double-click [`run.bat`](run.bat).** It creates the virtual
environment if needed, installs dependencies on first run, picks a free port and
opens your browser. Everything below is the manual equivalent.

---

## Problem statement

Two problems, one project.

**1. Compiler internals are opaque to learners.** A compiler is usually
encountered as a black box: source goes in, a binary comes out, and the six
phases in between are studied as diagrams on a whiteboard that never connect to
running code. Existing tools each show one slice — Compiler Explorer shows the
target code, JFLAP shows the automata — but none show a single input travelling
through *every* phase of *one* compiler, where each artefact visibly derives
from the previous one.

**2. Student-grade parsers report misleading errors.** Most hand-written
parsers do one of two things when they hit a syntax error: abort on the first
one, or resume naively and emit a cascade of spurious follow-on errors caused
by the parser's own confusion rather than by the program. A missing semicolon on
line 12 produces nine errors, eight of which are fiction.

MiniLang addresses both: the pipeline is fully instrumented and rendered phase
by phase, and the parser recovers with panic-mode synchronisation plus a ranked
suggestion engine, evaluated against a 30-program benchmark with known ground
truth.

---

## Architecture

```
                          BROWSER (no npm, no build step)
   ┌──────────────────────────────────────────────────────────────────┐
   │  IDE shell   menu bar · project tree · 8 tabs · output pane ·    │
   │              status bar                                          │
   │                                                                  │
   │  index.html    markup only                                       │
   │  editor.js     gutter + paint + transparent textarea             │
   │  highlight.js  colouring for MiniLang, C, C++, Java              │
   │  app.js        one fetch, one render pass per tab; the token      │
   │                table is windowed so long programs stay smooth     │
   │  viz.js        D3 drawing (parse tree, CFG, automata)            │
   │  lib/d3.js     VENDORED — the demo needs no internet             │
   └───────────────────────────────┬──────────────────────────────────┘
                                   │  POST /compile   {source, toggles}
                                   │  POST /run       {asm}
                                   │  POST /analyze-grammar {grammar}
                                   ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │                     app/main.py  ·  FastAPI                      │
   │        thin orchestrator: no compiler logic lives here           │
   └───────────────────────────────┬──────────────────────────────────┘
                                   │
     ┌─────────────────────────────┴──────────────────────────────┐
     │                    COMPILATION PIPELINE                    │
     │                                                            │
     │   source                                                   │
     │     │                                                      │
     │     ▼                                                      │
     │  lexer.py ──────────▶ tokens ──────────────┐               │
     │     │                                      │               │
     │     ▼                                      │               │
     │  parser.py ─────────▶ AST (ast_nodes.py) ──┤               │
     │     │    ▲                                 │               │
     │     │    └── errors.py  (recovery + ranked suggestions)    │
     │     ▼                                      │               │
     │  semantic.py ───────▶ symbol table, types ─┤               │
     │     │                                      │  all artefacts│
     │     ▼                                      │  collected    │
     │  tac.py ────────────▶ TAC, basic blocks, CFG               │
     │     │                                      │  into ONE     │
     │     ▼                                      │  JSON object  │
     │  optimize.py ───────▶ folded / propagated / DCE'd TAC      │
     │     │                                      │               │
     │     ▼                                      │               │
     │  codegen.py ────────▶ stack-VM assembly ───┤               │
     │     │                                      │               │
     │     ▼                                      │               │
     │  vm.py ─────────────▶ program output ──────┘               │
     └────────────────────────────────────────────────────────────┘

     ┌────────────────────────────────────────────────────────────┐
     │        PARSER THEORY LAB  (independent of the pipeline)     │
     │  grammar.py       FIRST/FOLLOW, LL(1), LR(0), SLR, LALR     │
     │  table_parser.py  generic table-driven parser + step trace  │
     │  regex_dfa.py     regex → NFA (Thompson) → DFA → minimised  │
     └────────────────────────────────────────────────────────────┘
```

**The one design rule that keeps this simple:** `POST /compile` returns a single
JSON object containing the artefacts of *every* phase at once. The frontend is
therefore pure rendering — no state machine, no per-phase request sequencing, no
orchestration bugs.

**Why recursive descent for the real parser, and table-driven only in the lab?**
Building SLR/LALR tables for the full language grammar is painful and the
resulting table is too large to display usefully. Splitting them means the
pipeline gets the far better error messages and error recovery that recursive
descent allows (which is the novelty feature), while the theory lab still
demonstrates table construction on any grammar the user pastes in.

---

## The language

```
program    → funcDecl+
funcDecl   → 'func' ID '(' params? ')' ':' type block
params     → type ID (',' type ID)*
type       → 'int' | 'float' | 'bool' | 'void'
block      → '{' stmt* '}'

stmt       → varDecl | assign | ifStmt | whileStmt
           | returnStmt | printStmt | exprStmt
varDecl    → type ID ('=' expr)? ';'
assign     → ID '=' expr ';'
ifStmt     → 'if' '(' expr ')' block ('else' block)?
whileStmt  → 'while' '(' expr ')' block
returnStmt → 'return' expr? ';'
printStmt  → 'print' '(' expr ')' ';'

expr       → logicOr
logicOr    → logicAnd ('||' logicAnd)*
logicAnd   → equality ('&&' equality)*
equality   → compare (('==' | '!=') compare)*
compare    → term (('<' | '>' | '<=' | '>=') term)*
term       → factor (('+' | '-') factor)*
factor     → unary (('*' | '/' | '%') unary)*
unary      → ('!' | '-') unary | primary
primary    → NUM | ID | 'true' | 'false'
           | ID '(' args? ')' | '(' expr ')'
```

The canonical demo program lives at [`tests/programs/demo.ml`](tests/programs/demo.ml)
and is served by `GET /api/demo`. It is chosen so that one input exercises every
phase and every optimisation: a `while` loop (control flow and a CFG back edge),
`5 * 2` (constant folding), an unused declaration (dead code elimination), and a
repeated `(3 + 4)` (common subexpression elimination).

---

## Setup

Requires **Python 3.10+**. There is no npm, no bundler, and no build step.

```bash
git clone https://github.com/hardikahujaa/MiniLang.git
cd MiniLang

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt  # add -r requirements-dev.txt for the tooling
```

Run it:

```bash
uvicorn app.main:app --reload
```

Open **<http://127.0.0.1:8000>**. The editor loads the demo program
automatically; press **Compile** (or `Ctrl`/`Cmd`+`Enter`).

> **Windows note:** if `python` resolves to a bundled interpreter that has no
> pip (Inkscape and some other applications put one on `PATH`), use the launcher
> instead: `py -3.12 -m venv .venv`.

### Configuration

Every environment variable is optional and the project runs fully offline with
none of them set. Copy [`.env.example`](.env.example) to `.env` to change the
log level or format, or to enable the optional LLM explanation layer. No API key
is required; without one, error recovery behaves identically minus the
natural-language explanation strings.

---

## Running the tests

```bash
pytest                                        # the suite (644 tests)
pytest --cov=app --cov-report=term-missing    # with coverage
ruff check .                                  # lint
black --check .                               # formatting
node --check static/app.js                    # JavaScript syntax
```

CI runs all of these on every push, against Python 3.10 and 3.12, and
additionally boots the server and hits `/api/health`, `/`, every static asset
and `/compile` to prove the app actually starts and really produces tokens — a
green unit-test run alone does not.

The suite includes cross-layer contract tests, because nothing else would catch
these: that every DOM id `app.js` queries exists in the markup, that the
optimisation checkbox names match the Pydantic aliases exactly, that the virtual
table's row height in JavaScript matches the stylesheet, that the editor's two
layers never disagree on a glyph metric, and that the editor highlights exactly
the keywords the real lexer reserves.

---

## API

| Method | Route               | Purpose                                                        |
|--------|---------------------|----------------------------------------------------------------|
| `GET`  | `/`                 | The single-page frontend                                        |
| `GET`  | `/api/health`       | Liveness, version, and which phases are implemented             |
| `GET`  | `/api/demo`         | The canonical demo program and sample grammars                  |
| `POST` | `/compile`          | Every phase's artefacts in one JSON object                      |
| `POST` | `/run`              | Execute stack-VM assembly, return captured stdout               |
| `POST` | `/analyze-grammar`  | FIRST/FOLLOW, LL(1), SLR, LALR tables for any grammar           |

Interactive API docs are generated automatically at `/docs`. The full response
schema is defined in [`app/schemas.py`](app/schemas.py).

---

## Build status

Built in the tier order from the implementation plan. Each step is a working,
demoable increment.

### Tier A — the working compiler

| # | Step                                                   | Status |
|---|--------------------------------------------------------|--------|
| 1 | Scaffold: FastAPI, 8-tab UI, stubbed `/compile`, CI     | ✅ done |
| 2 | Lexer + token table + IDE frontend                      | ✅ done |
| 3 | AST + recursive descent parser + D3 parse tree          | ✅ done |
| 4 | Semantic analysis + symbol table                        | ⬜ next |
| 5 | TAC generation + basic blocks + CFG                     | ⬜      |
| 6 | Constant folding, propagation, DCE + toggles            | ⬜      |
| 7 | Codegen + stack VM + Run button                         | ⬜      |

### Tier B — the marks multiplier

| #  | Step                                                   | Status |
|----|--------------------------------------------------------|--------|
| 8  | Parser Theory Lab: FIRST/FOLLOW, LL(1), LR(0), SLR, LALR| ⬜      |
| 9  | Generic table-driven parser with step trace             | ⬜      |
| 10 | Step-through parse animation                            | ⬜      |
| 11 | AI-assisted syntax error recovery                       | ⬜      |
| 12 | 30-program benchmark → `results.csv`                    | ⬜      |
| 13 | CFG visualisation polish                                | ⬜      |

### Tier C — upside

regex → NFA → DFA animation · common subexpression elimination · LLM
explanation layer · deployment · loop-invariant code motion ·
**multi-language lexing** (drive the existing scanner from a per-language
keyword table so the Lexical tab works for C, C++ and Java too).

---

## How precedence works

There is no precedence table in this compiler and no operator-precedence
algorithm. Precedence is expressed *structurally*, by the order in which the
expression rules call one another:

```
expression → logic_or → logic_and → equality
           → comparison → term → factor → unary → primary
```

Each level consumes its own operators and delegates to the next level down for
its operands. Because `term` (`+ -`) calls `factor` (`* / %`) for both sides, a
multiplication always lands in a subtree *below* an addition — so `2 + 3 * 4`
parses as `(+ 2 (* 3 4))` without anything ever comparing two operators:

```
  2 + 3 * 4                  (3 + 4) * (3 + 4)

       +                              *
      ╱ ╲                           ╱   ╲
     2   *                         +     +
        ╱ ╲                       ╱ ╲   ╱ ╲
       3   4                     3   4 3   4
```

Binary levels loop with `while`, which makes them left-associative; `unary`
recurses into itself instead, which makes prefix operators right-associative so
`!!flag` and `- -n` parse. Tab 4 draws exactly this, so the tree is visual proof
the precedence is right.

**Depth is bounded.** Recursive descent runs on the Python call stack, and each
level of nesting costs 15 frames (measured). Unguarded, roughly 55 levels of
nested parentheses would raise `RecursionError` — an unhandled crash and a 500
from `/compile`. `MAX_NESTING_DEPTH` turns that into an ordinary, positioned
syntax error.

**Error handling is Baseline A.** The parser aborts on the first syntax error by
design, not as a limitation: it is the control condition in the recovery
benchmark, and the recovering parser in Tier B is measured against it.

---

## The editor

The Source tab is a genuine editor component, not a `<textarea>` with a border.

| Feature | Notes |
|---|---|
| Syntax highlighting | MiniLang, C, C++, Java |
| Line-number gutter | Scrolls in lockstep with the code |
| Current-line highlight | Follows the caret |
| Auto-indent | Enter keeps indentation and adds a level after `{` |
| Tab / Shift+Tab | Insert and remove one indent level; never moves focus |
| Go to problem | Click any entry in the Problems panel to jump to it |
| Native undo/redo | Preserved — see the note below |
| `Ctrl`+`Enter` | Build |
| `Alt`+`1`…`8` | Jump to a tab |

**How it is built.** Three layers share one container: a gutter, a `<pre>`
holding highlighted HTML, and a transparent `<textarea>` that owns the caret,
selection, clipboard, undo stack and IME. The transparent-textarea approach is
used instead of `contenteditable` precisely so that native editing behaviour
keeps working — `contenteditable` would mean reimplementing undo, accessibility
and mobile keyboards by hand, badly. The catch is that the paint and input
layers must render glyphs at identical positions or the caret drifts, so every
metric that affects glyph placement is declared once, on `.editor-layer`, and
[a test](tests/test_frontend.py) fails if either layer overrides one.

### Behaviour on long programs

Two places would otherwise stall, and both are handled and tested:

| Concern | Approach |
|---|---|
| Repainting while typing | Debounced; above ~180 000 characters highlighting is disabled and the editor falls back to plain text, because the cost is parsing generated HTML, not scanning |
| Rendering the token table | Windowed — only the rows on screen exist in the DOM, so a 20 000-token program creates about 40 row elements, not 20 000 |
| Finding the caret's line | Cached, not recomputed per scroll event, which would otherwise be O(document) at 60 fps |

### Other languages

The editor highlights **C, C++ and Java** in addition to MiniLang, so it is
useful for reading the kind of code this course deals with.

**To be exact about what that does and does not mean:** the compiler pipeline
targets MiniLang only. Tabs 2–8 are driven by `app/`'s own lexer, parser and
code generator, and those implement MiniLang's grammar. Selecting C, C++ or
Java gives you editing and colouring; the **Build** button is disabled and the
status bar says why, rather than failing confusingly.

Writing real front ends for C, C++ and Java is not a scoping question — each is
a multi-year project on its own, and C++ is among the hardest languages in
existence to parse. Shelling out to `gcc` or `javac` instead would produce a
wrapper around someone else's compiler, which is the opposite of what this
project demonstrates. The genuinely feasible extension is **lexing** those
languages with the existing scanner driven by a per-language keyword table —
see [Build status](#build-status).

---

## Screenshots

_To be added as each phase lands._

| Tab | Screenshot |
|---|---|
| 1. Source — editor with inline error suggestions | _pending_ |
| 2. Lexical — token stream | _pending_ |
| 3. Parser Theory — FIRST/FOLLOW and SLR tables | _pending_ |
| 4. Syntax — step-through parse animation | _pending_ |
| 5. Semantic — scoped symbol table | _pending_ |
| 6. ICG — three-address code and CFG | _pending_ |
| 7. Optimization — before/after diff | _pending_ |
| 8. Target — stack-VM assembly and output | _pending_ |

---

## Project layout

```
MiniLang/
├── app/
│   ├── main.py             FastAPI routes; thin orchestrator
│   ├── lexer.py            phase 1: hand-written tokenizer
│   ├── ast_nodes.py        AST node definitions, JSON-serialisable
│   ├── parser.py           phase 2: recursive descent (Baseline A)
│   ├── schemas.py          the /compile JSON contract, as typed models
│   ├── logging_config.py   structured JSON logging
│   ├── samples.py          canonical demo program and grammars
│   └── …                   one module per phase, added per build step
├── static/
│   ├── index.html          IDE shell: menu, sidebar, 8 tabs, output pane
│   ├── app.js              controller: fetch + render, windowed token table
│   ├── editor.js           the code editor component (gutter, layers, keys)
│   ├── highlight.js        syntax colouring for MiniLang, C, C++, Java
│   ├── viz.js              D3 drawing
│   ├── style.css           dark theme
│   └── lib/d3.v7.min.js    vendored, so the demo works offline
├── tests/
│   ├── programs/           valid MiniLang programs
│   └── buggy/              programs with seeded syntax errors (Tier B)
├── run.bat                 one-click launcher (Windows)
├── .github/workflows/ci.yml
├── pyproject.toml          black · ruff · pytest configuration
├── requirements.txt
└── requirements-dev.txt
```

---

## Engineering conventions

- **Type hints everywhere**, enforced by `ruff`'s `ANN` rules.
- **Docstrings on every public module, class and function**, enforced by `D`
  (Google convention).
- **No `print()` in backend code** — enforced by `T20`; everything goes through
  structured JSON logging.
- **No bare excepts** — enforced by `BLE`.
- **Every module in `app/` has a matching test file in `tests/`**, covering edge
  cases and at least one deliberately malformed input per phase.
- **No secrets in the repository.** All configuration is read from environment
  variables with documented defaults, and the project runs with all of them
  unset.

---

## License

MIT — see [LICENSE](LICENSE).
