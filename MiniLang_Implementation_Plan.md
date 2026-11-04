# MiniLang Compiler Visualizer — Full Implementation Plan

**Project:** Interactive end-to-end compiler for a custom language, with every compilation phase visualized live in the browser, plus AI-assisted syntax error recovery.

**Target:** 28+/30 with minimum wasted effort.

**Effort model:** AI writes the code. Your job is to paste, run, screenshot, and be able to explain it. This plan is optimized so that the parts you must *understand* are small, and the parts that *look* impressive are large.

---

## 1. The three decisions that save the most effort

These are the load-bearing choices. Don't deviate from them.

**1. No npm. No build step. No framework.**
Frontend is one `index.html` + vanilla JS + D3 loaded from CDN. FastAPI serves it as a static file. The entire project runs with one command: `uvicorn app.main:app --reload`. Every hour students lose on this project is lost to node_modules, webpack configs, and CORS. You will lose zero.

**2. The actual compiler uses recursive descent. The LR tables are a separate, generic panel.**
Building SLR/LALR tables for a full language grammar is painful and the resulting table is too big to display anyway. So split it:

- **Pipeline** (lexer → parser → AST → semantic → TAC → optimize → codegen) uses hand-written recursive descent. Robust, easy to debug, and gives you far better error recovery — which is your novelty feature.
- **Parser Theory Lab** is an independent module where the user pastes *any* grammar and gets FIRST/FOLLOW, LL(1) table, LR(0) item sets, SLR/LALR tables, conflict detection, and a step-through parse. Ships preloaded with MiniLang's expression grammar.

This decoupling is the single biggest effort saver in the plan, and it means your project silently contains project ideas #2, #3, #4, #5 and #6 as features.

**3. AI error recovery must work offline.**
The deterministic ranking engine (edit distance + expected-token set + common-mistake table) is the core and needs no network. The LLM layer is a thin optional enhancement whose responses are cached to `cache/llm.json`. On demo day you run fully offline. Nothing depending on WiFi or an API key can embarrass you in front of an examiner.

---

## 2. The language: MiniLang

Small enough to build in a weekend, large enough that nobody calls it a toy.

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

**Canonical demo program** — write this once, use it in every screenshot, in the report, and in the viva. It exercises every phase and every optimization.

```c
func fib(int n): int {
    int a = 0;
    int b = 1;
    int i = 0;
    while (i < n) {
        int t = a + b;
        a = b;
        b = t;
        i = i + 1;
    }
    return a;
}

func main(): void {
    int limit = 5 * 2;        // folds to 10
    int unused = 42;          // eliminated by DCE
    int x = (3 + 4) * (3 + 4); // CSE target
    print(fib(limit));
    print(x);
}
```

---

## 3. File structure

```
minilang/
├── app/
│   ├── main.py            # FastAPI: POST /compile, POST /analyze-grammar,
│   │                      #          POST /run, static mount
│   ├── lexer.py           # tokenizer + token stream trace
│   ├── regex_dfa.py       # regex → NFA (Thompson) → DFA (subset) → minimized
│   ├── grammar.py         # FIRST/FOLLOW, LL(1), LR(0) items, SLR, LALR
│   ├── table_parser.py    # generic table-driven parser w/ step trace
│   ├── parser.py          # recursive descent → AST (+ error recovery hooks)
│   ├── ast_nodes.py       # node dataclasses + JSON serialization
│   ├── semantic.py        # scoped symbol table, type checking
│   ├── tac.py             # three-address code + basic blocks + CFG
│   ├── optimize.py        # folding, propagation, DCE, CSE (each toggleable)
│   ├── codegen.py         # stack-VM assembly emission
│   ├── vm.py              # executes the assembly, captures output
│   └── errors.py          # panic-mode sync + ranked suggestions + LLM layer
├── static/
│   ├── index.html         # all 8 tabs
│   ├── app.js             # fetch /compile, render each tab
│   ├── viz.js             # D3: parse tree, AST, DFA, CFG
│   └── style.css
├── tests/
│   ├── programs/          # 12 valid programs
│   └── buggy/             # 30 programs with seeded syntax errors
├── benchmark.py           # error-recovery evaluation → results.csv
├── requirements.txt
└── README.md
```

**One design rule that keeps this simple:** `POST /compile` returns a single JSON object containing the artifacts of *every* phase at once. The frontend is then pure rendering — no state machine, no orchestration, no bugs.

```json
{
  "tokens": [...], "dfa": {...},
  "parseTrace": [...], "parseTree": {...}, "ast": {...},
  "symbols": [...], "typeErrors": [...],
  "tac": [...], "blocks": [...], "cfg": {...},
  "optimized": {"tac": [...], "log": [...], "stats": {...}},
  "asm": [...], "output": "...",
  "errors": [{"line":.., "col":.., "expected":[..],
              "suggestion":"..", "confidence":0.9, "explanation":".."}]
}
```

---

## 4. The eight tabs (what the examiner actually sees)

| Tab | Shows | Wow factor |
|---|---|---|
| 1. Source | Editor + error underlines with hover fix suggestions | Medium |
| 2. Lexical | Token table (type, lexeme, line, col) + regex→NFA→DFA animation | **High** |
| 3. Parser Theory | Paste any grammar → FIRST/FOLLOW/LL(1)/SLR/LALR tables, conflicts highlighted | **High** |
| 4. Syntax | Step-through parse, animated stack, parse tree growing node by node | **Highest** |
| 5. Semantic | Scoped symbol table, type annotations on AST, inline errors | Medium |
| 6. ICG | Three-address code, basic blocks, D3 control-flow graph | **High** |
| 7. Optimization | Four toggles, side-by-side before/after diff, instruction-count delta | **Highest** |
| 8. Target | Stack-VM assembly + "Run" button showing real program output | **High** |

The two "Highest" tabs are where marks are won. Tab 4 because it makes an abstract concept visible; tab 7 because toggling a checkbox and watching lines vanish from the code is instantly legible to anyone.

---

## 5. Effort tiers — build in this order, stop when out of time

### Tier A — the working submission (~26/30)

Non-negotiable. Everything below depends on it.

1. Lexer + token table
2. Recursive descent parser + AST + D3 parse tree
3. Symbol table + type checking
4. TAC generation + basic blocks
5. Constant folding + dead code elimination
6. Stack-VM codegen + working VM execution
7. Minimal 8-tab UI

If you build only Tier A and it runs cleanly, you have a complete compiler and a solid mark. Everything after this is upside.

### Tier B — the marks multiplier (~29/30)

Highest marks-per-hour in the whole project. Do not skip.

8. **Parser Theory Lab** — FIRST/FOLLOW, LL(1), SLR, LALR, conflict detection
9. **Step-through parse animation** — the single most impressive screen
10. **Optimization toggles + before/after diff + stats**
11. **AI error recovery** — panic-mode sync, ranked suggestions, no cascading errors
12. **Benchmark** — 30 buggy programs, baseline vs. yours, a real results table
13. **CFG visualization** in D3

### Tier C — only if you have spare time

14. regex→NFA→DFA animation (pretty, but tab 3 already covers automata theory)
15. Common subexpression elimination
16. LLM explanation layer over the deterministic recovery
17. Deployment to Render
18. Loop-invariant code motion, strength reduction

**Judgment call:** Tier C item 14 looks great in a screenshot but takes real time. Item 16 is a two-line resume upgrade for one hour of work — do that one first.

---

## 6. Sprint schedule (14 working days, ~2.5 h/day)

| Day | Deliverable | You must be able to explain |
|---|---|---|
| 1 | Repo, FastAPI skeleton, `/compile` returning stub JSON, blank 8-tab UI | Request flow end to end |
| 2 | Lexer + token table rendering | Why a hand-written lexer over regex-only |
| 3 | Recursive descent parser → AST JSON | Precedence climbing in the grammar |
| 4 | D3 parse tree + AST view | How the tree maps to the grammar |
| 5 | Symbol table + scopes + type checker | Scope resolution rules |
| 6 | TAC generation + temporaries | Why three-address form |
| 7 | Basic blocks + CFG in D3 | Leader identification algorithm |
| 8 | Constant folding, propagation, DCE + toggles | What each pass proves |
| 9 | Codegen + VM + Run button | Stack machine semantics |
| 10 | **Parser Theory Lab**: FIRST/FOLLOW + LL(1) | Computing FIRST/FOLLOW by hand |
| 11 | **Parser Theory Lab**: LR(0) items, SLR, LALR, conflicts | Shift-reduce vs reduce-reduce |
| 12 | **Step-through parse animation** | The stack at any given step |
| 13 | **Error recovery + benchmark + results.csv** | Your recovery algorithm, precisely |
| 14 | Report, screenshots, demo rehearsal, README | All of it |

Days 10–13 are the marks days. If something slips, cut Tier C, never these.

---

## 7. The novelty feature, specified precisely

This is what separates your project from the other twenty in the class. Be able to state it in one sentence: *"Most student parsers abort on the first syntax error or emit a cascade of false ones; mine recovers, suggests the fix, and reports only genuine errors."*

**Algorithm:**

1. **Detect.** Recursive descent raises `ParseError` carrying the expected-token set for that position, the actual token, and the rule context.
2. **Rank candidate fixes.** For each token `t` in the expected set, score:
   - Levenshtein distance between `t`'s lexeme and the actual lexeme (typos: `whiel` → `while`)
   - Common-mistake table with fixed weights: missing `;`, `=` used for `==`, unbalanced `)` or `}`, missing type keyword, `:` omitted before return type
   - Positional heuristic: an insertion at end-of-previous-line outranks one mid-expression
   Return the top-3 with confidence scores.
3. **Recover.** Panic-mode: discard tokens until a synchronizing token (`;`, `}`, or a statement-start keyword), then resume at the enclosing statement boundary. Suppress any new error reported within 2 tokens of the last one — this is what kills the cascade.
4. **Explain (optional, Tier C).** Send the snippet + expected set to an LLM for a one-line natural-language explanation. Cache to disk. Never required for the demo to work.

**Benchmark — this produces your results table:**

Take your 12 valid programs, seed each with common errors to produce 30 buggy files with known ground truth (you know exactly how many real errors each contains). Compare three parsers:

- **Baseline A:** abort on first error
- **Baseline B:** naive resume, no suppression
- **Yours:** ranked suggestion + panic-mode sync + cascade suppression

Report: true errors found, false errors emitted, precision, recall, and top-1 suggestion accuracy. Even modest numbers here look like real engineering, because they are.

---

## 8. Report structure (10–12 pages)

1. **Abstract** — one paragraph, mention both the visualizer and the recovery contribution
2. **Introduction & motivation** — compiler internals are opaque to learners; parsers report misleading cascades
3. **Literature/tool survey** — brief: ANTLR, Compiler Explorer, JFLAP, and how yours differs
4. **MiniLang specification** — the grammar from §2
5. **System architecture** — one diagram: browser → FastAPI → phase modules → JSON
6. **Phase-by-phase implementation** — one subsection each, with a screenshot and the demo program's output at that phase. This is the bulk and it writes itself from the tabs.
7. **Parser theory module** — FIRST/FOLLOW and SLR/LALR construction, with a worked example table
8. **Error recovery** — the algorithm from §7
9. **Results** — optimization stats (instructions before/after per pass) + the recovery benchmark table
10. **Conclusion & future work** — register allocation, more target backends, loop optimizations

Two tables carry the whole results section: instruction count per optimization pass, and the recovery precision/recall comparison. Get those right and the report is done.

---

## 9. Demo script (8 minutes, rehearse twice)

1. Show the demo program. "Every phase you're about to see comes from this one input." **(30 s)**
2. Tab 2 — token stream. **(30 s)**
3. Tab 4 — hit Step. Let the parse tree build itself while you narrate the stack. **Slow down here.** **(2 min)**
4. Tab 3 — paste the expression grammar, show FIRST/FOLLOW and the SLR table. Then paste a deliberately ambiguous grammar and show the conflict light up red. **(1.5 min)**
5. Tab 6 — TAC and the CFG. **(1 min)**
6. Tab 7 — toggle each optimization one at a time. Point at the instruction counter dropping. **(1.5 min)**
7. Tab 8 — Run. Real output appears. **(30 s)**
8. Break the program: delete a semicolon, change `==` to `=`, misspell `while`. Show three precise errors with suggested fixes and no cascade. Then show the benchmark table. **(1 min)**

Close on the error recovery, not the visualizer. Endings are what get remembered.

---

## 10. Viva questions to prepare

Expect these. Prepare answers you can say without notes.

- Why recursive descent instead of a table-driven parser? *(Better error recovery and error messages; LL(1) tables can't express your precedence chain as readably. Note that you implemented table-driven parsing too, in the theory lab.)*
- Compute FIRST and FOLLOW for this grammar on the board.
- What's the difference between SLR, CLR and LALR? Which is used by yacc/bison, and why?
- Show a grammar with a shift-reduce conflict and explain the resolution.
- How do you identify basic block leaders?
- Which of your optimizations are local and which need global data flow?
- Why three-address code rather than generating target code directly from the AST?
- How does your symbol table handle nested scopes and shadowing?
- Is your error recovery guaranteed to terminate? *(Yes — panic mode always consumes at least one token.)*
- What breaks if the input program is very large?

---

## 11. Resume framing

**Line to use:**

> Built a full-stack compiler for a custom typed language (Python/FastAPI, D3.js) with interactive visualization of all six compilation phases — lexical analysis through target code generation — including an SLR/LALR parser-table generator and a syntax error recovery system that eliminated cascading false-positive errors across a 30-program benchmark.

**Interview stories it gives you:** grammar design and precedence, the recursive-descent vs. table-driven tradeoff, data-flow-based dead code elimination, and designing an evaluation benchmark with ground truth. That last one is the rarest and most valuable — most student projects have no evaluation methodology at all.

Push it to GitHub with the README, the demo GIF, and `results.csv` visible. A recruiter spends 20 seconds; the GIF does the work.

---

## 12. How to drive the build with AI

Feed these one at a time. Verify each runs before moving on. Never ask for the whole project in one prompt — you'll get code you can't debug.

1. "Build `app/main.py`, `requirements.txt`, and a stub `/compile` returning the JSON schema in §3, serving `static/index.html` with 8 empty tabs."
2. "Write `app/lexer.py` for the MiniLang grammar, returning tokens with type, lexeme, line, col. Include a test on the demo program."
3. "Write `app/ast_nodes.py` and `app/parser.py` — recursive descent per the §2 grammar, emitting JSON-serializable AST nodes."
4. "Write `static/viz.js` — D3 collapsible tree rendering the AST JSON."
5. "Write `app/semantic.py` — scoped symbol table and type checker, returning symbols and typed errors."
6. "Write `app/tac.py` — three-address code from the AST, then split into basic blocks and build a CFG."
7. "Write `app/optimize.py` — constant folding, constant propagation, dead code elimination, CSE. Each independently toggleable, each returning a log of what changed."
8. "Write `app/codegen.py` and `app/vm.py` — stack-VM assembly plus an interpreter capturing stdout."
9. "Write `app/grammar.py` — FIRST/FOLLOW, LL(1) table, LR(0) canonical collection, SLR and LALR tables, with conflict detection."
10. "Write `app/table_parser.py` — generic table-driven parser producing a step-by-step trace (stack, input, action)."
11. "Add the step-through parse animation to the frontend, driven by that trace."
12. "Write `app/errors.py` — the recovery algorithm in §7."
13. "Write `benchmark.py` and generate 30 seeded buggy programs with ground truth; output results.csv."
14. "Write the README with setup, architecture diagram, and screenshots."

---

## 13. Risk register

| Risk | Mitigation |
|---|---|
| D3 tree rendering eats a day | Fall back to a nested `<ul>` tree with CSS. Ugly but functional; upgrade later |
| LALR table construction is fiddly | Ship SLR first. LALR is a bonus, not a dependency |
| Demo machine has no internet | Vendor D3 locally in `static/lib/`. Do this on day 1, not day 14 |
| LLM layer fails during demo | Deterministic recovery is the default path; LLM is display-only |
| You can't explain your own code in the viva | Reserve 20 min after each sprint day to read what was generated. This is the one place effort is mandatory |

That last row is the real risk in an AI-built project. The code will be fine. Budget the time to understand it, because the viva is where 30/30 is won or lost.

---

# Addendum A — amendments made during the build

*Added 2026-09-17. The plan above is unchanged and remains the spec. This
section records decisions taken while building that the original plan did not
cover, so the two do not disagree.*

## A.1 The frontend is an IDE, not a minimal UI

The plan says "minimal 8-tab UI" (Tier A item 7). That was the right call for
effort, but the Source tab is the screen the examiner looks at longest, so the
editor was upgraded to a real component:

* line-number gutter, syntax highlighting, current-line band
* auto-indent, Tab / Shift+Tab indentation, preserved native undo
* click a problem to jump to its line and column
* IDE chrome: menu bar, project tree, build log, problems panel, status bar
* dark theme only

Still no npm, no bundler, no framework, no CDN. The whole thing is three
hand-written scripts plus the vendored D3.

**Implementation note for the viva.** The editor is a transparent `<textarea>`
stacked over a `<pre>` of highlighted HTML, with a gutter beside them. This
keeps native undo/redo, clipboard, IME and accessibility, which
`contenteditable` would force you to reimplement. The cost is that both layers
must render glyphs at identical positions, so every metric affecting glyph
placement is declared once in CSS and a test enforces it.

## A.2 Long programs are a stated requirement

Two hot spots, both handled and tested:

| Concern | Approach |
|---|---|
| Token table with thousands of rows | Windowed rendering — only visible rows are in the DOM |
| Repainting the editor while typing | Debounced; highlighting disables itself above ~180k characters |
| Locating the caret's line | Cached, not recomputed per scroll event (would be O(n) at 60 fps) |

## A.3 Other languages — what is and is not feasible

**Requested:** make the IDE work for Java, C and C++.

**Delivered now:** the editor highlights all four languages (MiniLang, C, C++,
Java). The language picker changes colouring; the file tab and status bar
follow.

**Deliberately not delivered:** compiling them. Being exact about why, because
this is a viva-relevant judgement:

1. **Writing real front ends for C, C++ and Java is not a scoping problem.**
   Each is a multi-year project. C++ is among the hardest languages in existence
   to parse — its grammar is not context-free, and `a * b;` cannot be
   disambiguated without full name resolution.
2. **Shelling out to `gcc` or `javac` would be worse than useless here.** It
   would produce a wrapper around someone else's compiler. Tabs 2–8 would go
   blank, because there would be no token stream, AST, symbol table, TAC or
   assembly *of ours* to show. The entire project premise — one input visibly
   travelling through every phase of one compiler you wrote — would be gone,
   and with it the resume line and the novelty feature.
3. **Build is disabled, not broken.** Selecting C, C++ or Java disables the
   Build button and the status bar explains why, rather than failing with a
   confusing error.

**The feasible extension, added to Tier C.** A lexer is essentially a keyword
table plus character classes, so `app/lexer.py` can be parameterised by
language and the **Lexical tab made to work for C, C++ and Java** — perhaps a
day's work. It is also pedagogically good: "same scanner, four keyword tables"
demonstrates exactly what is and is not language-specific about lexical
analysis. Phases 2–6 stay MiniLang, which is what the course requires anyway.

## A.4 Engineering bars added beyond the plan

Enforced throughout, not retrofitted at the end:

* type hints and Google-style docstrings everywhere, enforced by ruff `ANN`/`D`
* no `print()` in backend code (`T20`), no blind excepts (`BLE`)
* black at 100 columns; one config in `pyproject.toml` shared by CI
* every `app/` module has a matching `tests/` file with edge cases and at least
  one deliberately malformed input per phase
* GitHub Actions CI on Python 3.10 and 3.12, which also boots the server and
  asserts it really produces tokens — a green unit-test run does not prove the
  app starts
* `node --check` on every script, since there is no bundler to catch a syntax
  error before the browser does
* cross-layer contract tests where no type checker spans the boundary
* conventional-commit history, one commit per coherent unit
* no secrets in the repository; every environment variable optional

## A.5 Revised build status

| Tier | Step | Status |
|---|---|---|
| A | 1. Scaffold, 8-tab UI, stubbed `/compile`, CI | done |
| A | 2. Lexer + token table + IDE frontend | done |
| A | 3. AST + recursive descent parser + D3 parse tree | done |
| A | 4. Semantic analysis + symbol table | done |
| A | 5. TAC + basic blocks + CFG | done |
| A | 6. Constant folding, propagation, DCE + toggles | done |
| A | 7. Codegen + stack VM + Run | done |
| B | 8a. Parser theory *backend* (`app/grammar.py`): FIRST/FOLLOW, LL(1), LR(0)/SLR/LALR tables, conflict detection | done |
| B | 8b. Parser Theory Lab UI + `/analyze-grammar` wiring: grammar textarea, FIRST/FOLLOW/LL(1)/LR(0)/SLR/LALR views, conflict list, LALR parse trace | done |
| B | 9. Step-through parse animation (Syntax tab): `app/table_parser.py`, Play/Step/Reset UI | done |
| B | 10. Syntax error recovery: `app/errors.py`, panic-mode + ranked suggestions, `/compile` wiring | done |
| B | 11-13. Benchmark: seeded corpus, `benchmark.py` | done |
| C | Multi-language lexing, regex->NFA->DFA, CSE, LLM layer | |

## A.6 Operational notes

* **Run it with `run.bat`** (double-click). This machine has a Python on `PATH`
  belonging to Inkscape that has no pip, and a second `uvicorn` belonging to the
  system Python without the project's dependencies. Running the wrong one gives
  a confusing `ModuleNotFoundError`, which is what made the app appear broken.
  The launcher always uses the project's own virtual environment.
* **Repository:** `https://github.com/hardikahujaa/MiniLang.git`
* **Line endings** are pinned to LF via `.gitattributes`, because Git for
  Windows defaults to `core.autocrlf=true` and the suite compares a fixture
  byte-for-byte against a Python constant.

## A.7 Step 5 (TAC + basic blocks + CFG): decisions made during the build

`app/tac.py` carries the full rationale in its module docstring; this is the
short version for anyone comparing the plan against what shipped.

* **Two translation schemes, not one.** Every expression except `&&`/`||`
  becomes *value* code (Aho et al.'s synthesized-place scheme: one instruction
  per operator, operands evaluated left to right). `&&` and `||` are the one
  exception, translated as *jumping* code so short-circuit evaluation is
  real — `f() && g()` provably never calls `g()` when `f()` is false, because
  the instruction that would call it sits behind a conditional jump, not
  because the frontend trusts a commented promise.
* **Basic blocks use the classical leader algorithm, plus one deliberate
  extra rule:** every `label` instruction is unconditionally a leader, not
  only the ones a jump actually targets. That extra rule, combined with
  giving every function body a synthetic trailing `return` (even one that
  already returns on every path, and even `void` functions that never wrote
  one), is what guarantees two functions concatenated into one flat
  instruction stream never pick up a spurious fall-through edge into each
  other's basic blocks.
* **The CFG is intraprocedural, by function.** `call` is not an edge in this
  graph — MiniLang has no indirect calls, so each function's control-flow
  graph is independent, and the backend now stamps every `BasicBlock` with
  the `function` field that says which one it belongs to, which is also what
  lets the ICG tab draw each function as its own labelled lane.
* **ICG tab ships two sub-views, not a dump.** A flat instruction table alone
  reads as noise; blocks alone lose the ordering. The tab shows the TAC
  grouped by block (free, since each block already carries its own
  instruction slice from the backend) with a toggle to the D3 control-flow
  graph — same zoom/pan gestures as the Syntax tab's AST, so there is nothing
  new to learn switching between them.

## A.8 Step 6 (optimization passes): decisions made during the build

`app/optimize.py` carries the full rationale in its module docstring; this is
the short version.

* **Three passes are local, one is global, and that asymmetry is load-bearing,
  not an inconsistency.** Constant folding, constant propagation and common
  subexpression elimination all operate within a single basic block, which
  is what makes them sound without a real dataflow analysis -- there is only
  one path through a block, so "the last assignment so far" is unambiguous.
  Dead-code elimination has no such shortcut: a value assigned in one block
  is routinely used in a successor reached through a branch or a loop, so it
  runs a real backward liveness analysis (a fixpoint over the CFG) rather
  than a per-block approximation that would be outright incorrect, not just
  conservative. `tests/test_optimize.py::TestSoundness` exists specifically
  to pin this: a definition live only across a branch, only inside a loop
  body, or only through a loop's back-edge must all survive.
* **Folding, propagation and CSE iterate to a fixpoint before DCE runs
  once.** Each of the three can expose work for the other two -- propagating
  a constant can make an op foldable; folding can make two computations'
  operands match for CSE -- so running each once in a fixed order would miss
  cascades. The demo program's own comments are the regression test:
  `int limit = 5 * 2;` folds and propagates all the way into `param 10`
  with no instruction called `limit` surviving, and `(3 + 4) * (3 + 4)`
  collapses to `49` without CSE ever needing to fire, because each `3 + 4`
  is already foldable alone.
* **A call is never removed, regardless of whether its result is read.** It
  may have side effects (it can itself `print`), so DCE's "is this op
  removable at all" check excludes `call` unconditionally -- unlike a plain
  copy or a pure operator, which is removable exactly when its result is
  dead.
* **Constant-condition branches are left alone on purpose.** Folding
  `ifFalse true goto L` into an unconditional jump -- and then deleting the
  now-unreachable branch -- is a real optimization (unreachable-code
  elimination from constant propagation into control flow), but it is a
  different one from the four asked for here, and doing it would mean
  rewriting the CFG's edges, not just its instructions. Scoped out, not
  missed.
* **The Optimization tab shows before/after columns plus a change log, not a
  line-level diff.** A change's `index` is only meaningful within the round
  that produced it -- the instruction list shrinks every time DCE removes
  something -- so aligning "before line 7" with "after line 7" by position
  would be misleading once anything has been removed. The log's entries are
  self-contained strings (`before`, `after`, `detail`) instead, which is
  what actually answers "what happened and why" regardless of how the
  indices moved around underneath it. Toggling a checkbox re-runs `/compile`
  immediately, so the instruction counter visibly moves without a separate
  Build click -- the plan's demo script assumes exactly that.

## A.9 Step 7 (codegen + stack VM + Run): decisions made during the build

`app/codegen.py` and `app/vm.py` carry the full rationale in their module
docstrings; this is the short version.

* **A real bug surfaced and got fixed first: shadowing.** `app/tac.py`
  (step 5) used a declared variable's bare source name as its TAC place
  directly, which meant `int x = 1; if (c) { int x = 2; }` silently aliased
  the shadowed inner `x` onto the outer one -- the outer variable's value
  was corrupted the moment the inner one was assigned. This was latent and
  untested until codegen needed a *permanent, unique* stack slot per
  variable to even be expressible. `TacGenerator` now keeps its own scope
  stack mirroring `app.semantic.SymbolTable`'s rules exactly, so a
  redeclaration gets a fresh place (`x`, then `x~1`, `x~2`, ...) instead of
  colliding. Regression-tested at the TAC level (`TestShadowing` in
  `tests/test_tac.py`) and again end-to-end through the VM
  (`tests/test_vm.py::TestShadowing`).
* **Frame layout is the textbook activation record, not a simplification of
  it.** Parameters at negative offsets, locals at non-negative ones,
  control link and return address in between, addressed through one
  frame pointer -- Aho, Lam, Sethi & Ullman figure 7.9, not a toy
  approximation. This is what makes real recursion (including mutual
  recursion) work correctly with no special-casing: tested directly with a
  recursive factorial and two mutually-recursive functions, not just the
  demo program's iterative `fib`.
* **Every jump and call target is a resolved integer address by the time the
  VM sees it.** Codegen generates symbolically (label and function names as
  placeholders) and backpatches every target to a concrete instruction
  index in one final pass -- the classical two-pass assembler technique.
  The VM therefore never looks a name up by string; it is purely an index
  machine, which is both simpler to get right and closer to what a real
  bytecode interpreter does.
* **One shared module settles arithmetic and literal formatting for all
  three phases.** `app/literals.py` is the single place `/` truncates
  toward zero and `%` follows the dividend's sign (C semantics, not
  Python's); `app.tac`, `app.optimize` and `app.vm` all call into it rather
  than each carrying its own copy, which is what guarantees a value folded
  at compile time and the same value computed at run time are provably
  identical -- not just expected to match by convention.
* **`halted` means exactly one thing: "did the step budget run out while
  still running?"** Not "did it succeed." A clean return from `main` and a
  runtime trap (division by zero, a stack underflow, a call to an
  unresolved target) are both `halted=True`; `error` is what tells them
  apart. Only a genuine step-budget exhaustion -- the guard against an
  infinite MiniLang loop hanging the server -- is `halted=False`. Getting
  this backwards was an actual bug caught by a pre-existing test
  (`test_accepts_empty_program`) that encoded the field's real contract
  before the VM existed to honour it.
* **The Run button runs what Build produced, not what the editor holds
  now.** The frontend posts `lastResult.asm` back to `/run`, which recovers
  `main`'s entry address by scanning the assembly for its own `LABEL`
  rather than keeping it as separate state that could drift out of sync.
  Editing the source after a build and pressing Run without rebuilding
  therefore runs the last build, matching the button's own disabled state.

## A.10 Tier B step 8a (grammar theory backend): decisions made during the build

`app/grammar.py` carries the full rationale in its own module docstring;
this is the short version.

* **It never calls, and is never called by, the real parser.** `app/parser.py`
  is hand-written recursive descent on purpose (its own docstring explains
  why); this module is the opposite -- nothing but the classical table
  constructions, built mechanically from whatever grammar text a user
  pastes. The two are deliberately independent, exactly as section 1 of the
  plan scoped Tier A and Tier B apart from the start.
* **LALR(1) is built by constructing the full canonical LR(1) collection and
  merging states with the same core, not by the more efficient lookahead-
  propagation algorithm (section 4.7.4).** The grammars this tool analyses
  are small, hand-written course examples, so the canonical collection is
  never large enough for the efficiency difference to matter, and building
  it the simple way keeps every intermediate LR(1) item set available to
  show on the tab later and to check in a test -- a by-product the faster
  algorithm doesn't produce. The book states this merge is always sound and
  explicitly allows it as a valid, if less space-efficient, construction.
* **A real bug surfaced and got fixed before the tests that would have hidden
  it were written: `|` as an alternative separator collided with a grammar
  whose own alphabet includes a token spelled `||`.** Splitting the raw RHS
  text on the character `|` tore `||` in half around an invisible empty
  alternative, silently producing three spurious epsilon productions where
  one was meant. The fix tokenizes on whitespace first and only treats a
  *standalone* `|` token as a separator. `TestGrammarParsing
  ::test_pipe_shaped_terminal_is_not_split_by_the_alternative_separator` in
  `tests/test_grammar.py` pins this, and it is what let a reformulated,
  LL(1)-shaped version of MiniLang's own expression grammar -- which uses
  `||` for its own logical-or -- be checked against this module at all.
* **A production with many alternatives may wrap across lines, as long as
  every continuation line starts with `|`.** This surfaced the same way the
  bug above did: the first version of this module's own test suite tried to
  write exactly this and failed, because a natural way to format a grammar
  by hand is not automatically the way a one-production-per-line parser
  expects it.
* **Every result is checked against the book, not just against itself.**
  `tests/test_grammar.py` pins FIRST, FOLLOW and LL(1) table cells for the
  book's own non-left-recursive expression grammar (section 4.4); the LR(0)
  canonical collection's size (12 states) for `app.samples.DEMO_GRAMMAR`,
  the book's own running SLR example (Figure 4.31); and, as the strongest
  single test in the file, that the book's own canonical "SLR fails, LALR
  succeeds" grammar (`S -> L = R | R`, section 4.7.3) reproduces exactly
  that distinction here -- a real shift-reduce conflict from `build_slr_table`,
  none from `build_lalr_table`, on the same grammar.

## A.11 Tier B step 8b (Parser Theory Lab UI + `/analyze-grammar`): decisions made during the build

* **`app/grammar.py` stays textbook-pure; `app/main.py` owns every bit of
  JSON adaptation.** The same split `app/vm.py`/`app/main.py` already use
  for `/run` (`VmResult` is a plain dataclass, `RunResponse` is assembled in
  the route). A set of private `_*_to_api` functions in `main.py` reshape
  `GrammarAnalysis` into `GrammarResponse` -- FIRST/FOLLOW as sorted lists,
  the LL(1)/SLR/LALR tables as `{state: {symbol: [action, ...]}}` using the
  book's own `s5`/`r2`/`acc` shorthand, LR(0) item sets as one entry per
  state. Nothing about table correctness lives in this layer, so its own
  tests check the JSON shape and round-tripping, not the numbers -- those
  were already pinned in `tests/test_grammar.py`.
* **`GrammarConflict` gained a `table: Literal["ll1","slr","lalr"]` field,
  and `kind` now also accepts `"ll1"`.** Mirrors `app.grammar.Conflict`
  exactly. Without it, a single merged conflicts list could not show the one
  thing this whole feature exists to demonstrate: a grammar whose SLR table
  has a conflict that its LALR table does not.
* **`run_lr_parse` and the new `trace_lr_parse` share one internal stepping
  generator (`_step_lr_parse`), added without changing `run_lr_parse`'s own
  signature or behaviour.** `trace_lr_parse` records every
  `LrParseStep` -- stack, remaining input, action -- in the interleaved
  state/symbol form the book draws by hand (Figure 4.33); `run_lr_parse`
  just reduces the same sequence to whether the last step was `"accept"`.
  Sharing the loop is what guarantees the two can never quietly disagree.
* **The parse trace always runs on the LALR table, never SLR**, because
  `TestSlrGrammarsAreAlwaysLalr` already proves LALR accepts everything SLR
  does and sometimes more -- so it is the table least likely to reject input
  the grammar's own author meant to be valid.
* **A malformed grammar paste is a `Diagnostic` in the response, not an HTTP
  500.** The Parser Theory tab's grammar textarea is meant to be typed into
  live, and a syntactically incomplete grammar mid-keystroke is the normal
  case, not an exceptional one.
* **Two stub-era tests in `TestGrammarEndpoint` were updated, not treated as
  fixed requirements.** One asserted `startSymbol` echoes the request
  (`null` when omitted) -- correct for an empty stub, wrong for a working
  analyser, which should report the symbol it actually resolved and
  analysed against (`"S"`, the first production's left-hand side). Updating
  a test to match newly-correct behaviour is the same judgement call every
  earlier Tier A step's integration made when a phase went from stub to
  real.

## A.12 Tier B step 9 (step-through parse animation): decisions made during the build

* **MiniLang's grammar needed a second textbook transformation before a
  table-driven parser could touch it, beyond the left-recursion elimination
  Report 1 already describes.** `app/parser.py`'s own `_statement` docstring
  states plainly that distinguishing `assign` from `exprStmt` needs two
  tokens of lookahead -- fine for hand-written recursive descent, fatal for
  a genuine single-token LL(1) table. `app/samples.py`'s `MINILANG_GRAMMAR`
  applies **left factoring** (Aho et al., section 4.3.4) to `stmt`, plus a
  second, smaller transformation (`unaryNoId`/`primaryNoId`, cloning two
  grammar levels with the `ident`-leading alternative removed) to stop
  `exprStmt`'s own expression chain from silently reintroducing the
  collision the factoring had just removed. Verified conflict-free by
  actually building the LL(1) table (`tests/test_grammar.py
  ::test_minilang_full_grammar_is_ll1`), not just reasoned about on paper,
  and cross-validated against every program in `tests/programs/` parsing
  identically under both parsers.
* **A real, two-layer bug surfaced building the API adapter, not the
  parser.** Table-driven parsing is iterative (Algorithm 4.4 has no call
  stack), so it has no trouble at all accepting an expression nested
  hundreds of parentheses deep -- the exact input `app/parser.py`
  deliberately rejects via `MAX_NESTING_DEPTH`. The first version of
  `_table_tree_to_api` was written recursively anyway and hit Python's own
  recursion limit converting the resulting tree; fixed to convert
  iteratively, with an explicit stack. That fix then exposed a second,
  unrelated ceiling underneath it: `pydantic-core`'s JSON serializer
  refuses a self-referential model (`TreeNode.children: list[TreeNode]`)
  nested much past roughly 95-100 levels, confirmed by direct measurement
  to be a hard-coded limit entirely unaffected by
  `sys.setrecursionlimit`. `app/table_parser.py` now enforces its own
  `_MAX_STACK_DEPTH` (64, empirically calibrated by round-tripping the
  deepest parse it still accepts through `TreeNode.model_dump_json`
  directly) for exactly this reason -- not because the algorithm needs it,
  but because the response it feeds into does.
* **The concrete parse tree is a genuinely different artefact from the
  AST, not a relabelling of it.** `app.ast_nodes` deliberately collapses
  grammar scaffolding that exists only to keep a grammar parseable --
  exactly the `Tail` nonterminals this grammar is full of. The parse tree
  keeps all of it (including an explicit `ε` leaf for every empty
  production), which is the whole pedagogical point: a reader can see the
  derivation the table actually performed, not a simplified summary of it.
  Pinned directly: `tests/test_table_parser.py
  ::test_tree_shape_differs_from_the_ast_envelope`.
* **The Syntax tab gained an "Abstract syntax tree" / "Step-through parse"
  sub-view toggle** (the same `.seg-toggle` pattern already used by the ICG
  and Parser Theory tabs) rather than replacing the AST view or stacking
  both trees at once. The step-through tree is a second `Viz.renderTree`
  instance over the same adapter pattern used for the Parser Theory tab's
  tables: `toVizShape()` maps `TreeNode` onto the AST envelope shape
  `Viz.renderTree` already draws, and a new `setVisibleStep(stepIndex)`
  method on the tree handle reveals nodes by toggling a CSS class rather
  than re-laying out the tree on every Play/Step tick.

## A.13 Tier B step 10 (syntax error recovery): decisions made during the build

* **`app/parser.py` needed zero changes.** Every grammar-rule method calls
  the next one through `self.`, never the class name directly, so
  `RecoveringParser(Parser)` overriding only `_program` and `_block` --
  the two methods with their own loop over a sequence of declarations or
  statements -- transparently intercepts every call site inside the rest of
  `Parser`, unmodified. A `ParseError` raised parsing something nested
  inside an `if`/`while` body propagates to the *nearest* enclosing
  `_block`'s overridden loop through ordinary exception propagation --
  "resume at the enclosing statement boundary" needs no extra bookkeeping
  at all, and happens correctly at whatever nesting depth the error
  actually occurred at, not only at the top level.
* **A real design flaw surfaced through testing, not inspection: panic-mode
  skip originally discarded the first token unconditionally, before
  checking whether the cursor was already sitting on a synchronizing
  one.** That ate the leading token of whatever well-formed statement
  happened to follow the error whenever the error token itself was already
  a legitimate resync point (a type keyword, `}`, a second `;`) --
  concretely, two independent missing-semicolon mistakes on consecutive
  lines collapsed into a single reported error instead of two, because
  recovering from the first swallowed the `int` that started the second.
  Fixed to check before discarding: if already at `;` or a statement-start
  token, nothing is discarded at all. Termination still holds -- re-argued
  and tested directly, both for "already synchronized" and "discard at
  least one genuine token" -- and the original two-error case now reports
  both, pinned permanently in `tests/test_errors.py
  ::TestRecoveryResumesCorrectly::test_two_independent_missing_semicolons_are_both_reported`.
* **The ranking engine's common-mistake table is built from verified
  `(expected, context, found)` triples read directly out of
  `app/parser.py`, not guessed at.** The `=`/`==` confusion case is the
  clearest example of why this mattered: parsing `if (x = 1)` never
  produces an error with `==` anywhere in its expected set at all --
  `=` is not a valid expression operator in MiniLang, so `x = 1` parses as
  the complete expression `x`, and the *actual* error is "expected `)` in
  if condition, but found `=`". The suggestion has to be injected as an
  extra candidate keyed on `found.type is ASSIGN and context in
  ("if condition", "while condition")`, not scored from `expected` the way
  every other candidate is -- scoring `==` against an expected set that
  never contains it would never find this case at all.
* **A misspelled *keyword* only produces a direct, rankable token mismatch
  when the position genuinely requires that exact keyword and nothing
  else.** `whiel (x) {}` does not: MiniLang's grammar accepts a bare
  identifier as a complete expression statement, so a misspelled `while`
  parses as a call-shaped expression (`whiel(x)`) and the real error
  surfaces several tokens later, at the `{` that follows, with no trace of
  "while" in the picture. `func` typo'd as `fun` is the clean version of
  this case: `_func_decl` consumes nothing before checking for `func`
  specifically, so `fun` produces exactly `expected 'func' ..., found
  'fun'` and ranks correctly. Both behaviours are pinned, not just the
  convenient one.
* **Baseline B (the benchmark's "naive resume" comparison point) is this
  same engine with two constructor flags off** (`rank=False,
  suppress=False`), not a second implementation -- `recover_parse`'s own
  docstring states this is deliberate, since it is also what proves
  ranking and suppression are genuinely separable improvements rather than
  bundled together by construction.
* **An edge case needed its own recovery path: an empty token stream.**
  `Parser._program` raises before its loop even starts when there are no
  functions at all, which a try/except around the loop body cannot catch.
  `RecoveringParser._program` builds that same error with `self._error(...)`
  (which returns rather than raises) and feeds it to the normal
  `_record_and_recover` path directly, so `recover_parse("")` still comes
  back with a `RecoveryResult` -- one report, an empty `Program` -- instead
  of a raw exception escaping the one function whose entire purpose is to
  never let that happen.

## A.14 Tier B steps 11-13 (benchmark: seeded corpus, `benchmark.py`): decisions made during the build

* **Building the corpus found two more real panic-mode bugs that reading
  the code hadn't caught.** The 30-file buggy corpus (18 single-error, 12
  two-error, six categories, source programs `control_flow.ml` /
  `functions_calls.ml` / `nested_blocks.ml`) was adversarial by
  construction -- each fixture was written to probe a specific recovery
  path, not just to look wrong. Two constructions broke
  `_panic_skip_statement` in ways the hand-written `test_errors.py` suite
  from step 10 hadn't exercised: (1) an error inside an `if`/`while`
  condition (e.g. `if (x = 0) { ... }`) left an unmatched `{` in the
  remaining input, and the skip loop treated *every* `}` as a valid sync
  point regardless of nesting, so recovery stopped on the body's own
  closing brace and handed control back one level too shallow; (2) even
  after adding a brace-depth counter, the counter didn't track the
  *first* discarded token's own brace-ness, so a construction where the
  error token itself was `{` still cascaded. Both are fixed in
  `app/errors.py` with a depth counter that starts counting at the first
  discarded token, not after it, and both are pinned directly in
  `tests/test_errors.py` (not just indirectly through the benchmark).
* **The manifest (`tests/buggy/manifest.json`) is generated, not
  hand-counted.** An early hand-typed draft of expected line numbers was
  wrong in several places -- panic-mode resync legitimately shifts which
  line a report lands on. The actual manifest is produced by running
  `recover_parse(tokens, rank=False, suppress=False)` against every buggy
  file and recording its *real* output as ground truth, which is also why
  `TestManifestGroundTruthIsReal` in `tests/test_benchmark.py` re-asserts
  that every fixture still raises under plain `app.parser.parse` --
  a fixture hand-edited back into validity would otherwise make its row
  silently meaningless rather than fail loudly.
* **The plan's original benchmark hypothesis -- that Baseline B would show
  more false positives than the full engine on a cascade -- turned out
  false, and the test suite says so honestly instead of asserting it
  anyway.** Once the two bugs above were fixed, both Baseline B and the
  full engine (which share the same `_panic_skip_statement` /
  `_panic_skip_function`) achieved zero false positives across all 42
  seeded errors. `test_the_corpus_does_not_show_a_false_positive_gap`
  documents this directly rather than hiding it. The benchmark's real,
  measured findings instead are: (1) Baseline A (abort on first error)
  finds only 30/42 -- every second error in a two-error file is invisible
  to it by construction; (2) suppression's actual cost is recall, not a
  precision win -- the full engine finds 36/42 (6 fewer than naive
  Baseline B's 42/42), trading away exactly the six deliberately-close
  two-error pairs it is designed to collapse; (3) ranked suggestions are
  the full engine's one categorical advantage -- 100% top-1 accuracy on
  every matched report, versus 0% for both baselines, which have no
  ranking at all. `results.csv` (generated by `python benchmark.py`,
  left untracked/regenerable rather than committed) reproduces this:
  `baseline_a 42/30 TP, prec 1.000 rec 0.714`; `baseline_b 42/42 TP, prec
  1.000 rec 1.000`; `full_engine 42/36 TP, prec 1.000 rec 0.857, top1
  1.000`.
