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
| A | 3. AST + recursive descent parser + D3 parse tree | next |
| A | 4. Semantic analysis + symbol table | |
| A | 5. TAC + basic blocks + CFG | |
| A | 6. Constant folding, propagation, DCE + toggles | |
| A | 7. Codegen + stack VM + Run | |
| B | 8-13. Parser theory lab, step animation, error recovery, benchmark | |
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
