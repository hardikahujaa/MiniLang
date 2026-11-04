"""FastAPI application: HTTP surface for the MiniLang compiler.

Routes
------
``GET  /``                 Serves the single-page frontend (``static/index.html``).
``GET  /api/health``       Liveness probe plus which phases are wired up.
``GET  /api/demo``         The canonical demo program and sample grammars.
``POST /compile``          Runs the whole pipeline, returns every phase's artefacts.
``POST /run``              Executes stack-VM assembly, returns captured stdout.
``POST /analyze-grammar``  Parser Theory Lab: FIRST/FOLLOW, LL(1), SLR, LALR.

Design rule from the plan (section 3): ``/compile`` returns a single JSON object
containing the artefacts of *every* phase at once, so the frontend is pure
rendering -- no client-side state machine, no orchestration, no bugs.

Build status
------------
``/compile`` runs every phase listed in :data:`IMPLEMENTED_PHASES` and leaves
the rest of the response at its empty default, so an unbuilt phase renders as
an empty tab rather than breaking the page. ``/analyze-grammar`` is fully
wired to :mod:`app.grammar` (Tier B step 8): FIRST/FOLLOW, LL(1), LR(0),
SLR(1) and LALR(1) tables, conflicts, and an LALR parse trace when the
request includes an input string.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.ast_nodes import Program
from app.codegen import generate as generate_asm
from app.grammar import Automaton, GrammarAnalysis, GrammarError, LrTable, trace_lr_parse
from app.grammar import analyse as analyse_grammar
from app.lexer import tokenize
from app.logging_config import configure_logging, get_logger
from app.optimize import optimize
from app.parser import ParseError, parse
from app.samples import AMBIGUOUS_GRAMMAR, DEMO_GRAMMAR, DEMO_PROGRAM
from app.schemas import (
    CompileMeta,
    CompileRequest,
    CompileResponse,
    Diagnostic,
    GrammarConflict,
    GrammarRequest,
    GrammarResponse,
    HealthResponse,
    OptimizationToggles,
    ParseStep,
    PhaseTiming,
    RunRequest,
    RunResponse,
    TreeNode,
)
from app.semantic import analyse
from app.table_parser import ParseTreeNode, trace_table_parse
from app.tac import generate as generate_tac
from app.vm import find_entry
from app.vm import run as run_vm

logger = get_logger(__name__)

#: Repository root, resolved from this file so the server can be started from
#: any working directory.
BASE_DIR: Final[Path] = Path(__file__).resolve().parent.parent
STATIC_DIR: Final[Path] = BASE_DIR / "static"
INDEX_HTML: Final[Path] = STATIC_DIR / "index.html"

#: Compilation phases that are fully implemented. Each step of the build adds
#: one entry here, and ``GET /api/health`` reports it, which gives the frontend
#: (and the marker) an honest picture of what is wired up.
IMPLEMENTED_PHASES: Final[list[str]] = ["lexer", "parser", "semantic", "tac", "optimize", "codegen"]


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Configure logging on startup and log the shutdown.

    Args:
        _app: The FastAPI application being started. Unused, but required by the
            lifespan protocol.

    Yields:
        Control back to the server for the lifetime of the application.
    """
    configure_logging()
    logger.info(
        "minilang starting",
        extra={
            "version": __version__,
            "static_dir": str(STATIC_DIR),
            "implemented_phases": IMPLEMENTED_PHASES,
        },
    )
    yield
    logger.info("minilang shutting down", extra={"version": __version__})


app = FastAPI(
    title="MiniLang Compiler Visualizer",
    version=__version__,
    summary="Interactive end-to-end compiler for a custom typed language.",
    description=(
        "Every compilation phase -- lexical analysis through target code generation -- "
        "exposed as JSON and visualised live in the browser, plus a parser theory lab "
        "and AI-assisted syntax error recovery."
    ),
    lifespan=lifespan,
)


@app.middleware("http")
async def log_requests(request: Request, call_next: Callable) -> JSONResponse:
    """Emit one structured log record per HTTP request, with its duration.

    Args:
        request: The incoming request.
        call_next: The next handler in the middleware chain.

    Returns:
        The downstream response, unmodified.
    """
    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    logger.info(
        "request",
        extra={
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": round(elapsed_ms, 2),
        },
    )
    return response


# ---------------------------------------------------------------------------
# Frontend and metadata
# ---------------------------------------------------------------------------


def asset_version() -> str:
    """Return a fingerprint that changes whenever a frontend asset changes.

    Browsers cache ``app.js`` and friends aggressively, so an edit can appear
    to have no effect until a hard refresh -- a genuinely confusing failure,
    because the served file is correct and only the browser's copy is stale.
    Stamping this onto every asset URL makes the browser fetch the new file the
    moment its bytes change, and keep using its cache when they have not.

    Derived from size and modification time rather than file contents: it runs
    per page load, and hashing a 280 KB D3 bundle on every request would be
    waste for no extra correctness here.

    Returns:
        A short hexadecimal fingerprint of the current asset set.
    """
    digest = hashlib.sha256()
    for path in sorted(STATIC_DIR.rglob("*")):
        if path.is_file() and path.suffix in {".js", ".css"}:
            stat = path.stat()
            digest.update(f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}".encode())
    return digest.hexdigest()[:12]


@app.get("/", include_in_schema=False)
async def index() -> HTMLResponse:
    """Serve the single-page frontend, with cache-busted asset URLs.

    Returns:
        ``static/index.html`` with ``__ASSET_VERSION__`` replaced by the current
        :func:`asset_version` fingerprint.
    """
    html = INDEX_HTML.read_text(encoding="utf-8")
    return HTMLResponse(html.replace("__ASSET_VERSION__", asset_version()))


@app.get("/api/health", response_model=HealthResponse, tags=["meta"])
async def health() -> HealthResponse:
    """Report liveness and which compilation phases are implemented.

    Returns:
        A :class:`~app.schemas.HealthResponse` naming the running version and
        the phases currently wired into ``/compile``.
    """
    return HealthResponse(version=__version__, phases_implemented=list(IMPLEMENTED_PHASES))


@app.get("/api/demo", tags=["meta"])
async def demo() -> dict[str, str]:
    """Return the canonical demo program and the sample grammars.

    The frontend calls this on first load so the editor is never empty and the
    Parser Theory Lab ships preloaded, exactly as the plan specifies.

    Returns:
        A mapping with ``source``, ``grammar`` and ``ambiguousGrammar`` keys.
    """
    return {
        "source": DEMO_PROGRAM,
        "grammar": DEMO_GRAMMAR,
        "ambiguousGrammar": AMBIGUOUS_GRAMMAR,
    }


# ---------------------------------------------------------------------------
# Compilation pipeline
# ---------------------------------------------------------------------------


def _table_tree_to_api(root: ParseTreeNode) -> TreeNode:
    """Adapt a :mod:`app.table_parser` concrete parse tree into the wire shape.

    Converts iteratively, with an explicit stack, rather than by Python call
    recursion. This matters: the table-driven parser itself is Algorithm 4.4's
    iterative stack-based driver, so -- unlike recursive descent -- it has no
    inherent nesting-depth limit, and a pathologically nested but otherwise
    valid expression can produce a tree many hundreds of levels deep. A
    recursive version of this function would defeat that advantage by hitting
    Python's own recursion limit on exactly the input the parser itself
    handled without trouble.

    Args:
        root: The root of a tree from :func:`app.table_parser.trace_table_parse`.

    Returns:
        The equivalent :class:`~app.schemas.TreeNode` tree.
    """
    converted: dict[int, TreeNode] = {}
    work: list[tuple[ParseTreeNode, bool]] = [(root, False)]
    while work:
        node, children_converted = work.pop()
        if not children_converted:
            work.append((node, True))
            for child in reversed(node.children):
                work.append((child, False))
            continue
        converted[id(node)] = TreeNode(
            name=node.symbol,
            kind=node.kind,
            value=node.value,
            line=node.line,
            col=node.col,
            step=node.step,
            children=[converted.pop(id(child)) for child in node.children],
        )
    return converted[id(root)]


@app.post("/compile", response_model=CompileResponse, tags=["pipeline"])
async def compile_source(request: CompileRequest) -> CompileResponse:
    """Compile MiniLang source and return every phase's artefacts.

    Phases run in the order given by the plan and each populates its own slice
    of the response. Implemented so far: lexical analysis and syntax analysis.
    Later steps add the semantic analyser, TAC, optimiser, codegen and VM.

    A phase that fails does not abort the request: its diagnostics are recorded
    and ``meta.reached_phase`` reports how far the pipeline got, so the tabs
    that *do* have data still render.

    Args:
        request: The source text plus per-pass optimisation toggles.

    Returns:
        A :class:`~app.schemas.CompileResponse` whose populated fields
        correspond to the phases listed in :data:`IMPLEMENTED_PHASES`.
    """
    started = time.perf_counter()
    source = request.source

    response = CompileResponse(
        meta=CompileMeta(
            ok=True,
            reached_phase="none",
            source_lines=len(source.splitlines()),
            source_bytes=len(source.encode("utf-8")),
            version=__version__,
        )
    )

    # --- Phase 1: lexical analysis -------------------------------------
    lex_started = time.perf_counter()
    lex_result = tokenize(source)
    lex_ms = (time.perf_counter() - lex_started) * 1000.0

    response.tokens = [token.to_api() for token in lex_result.tokens]
    response.errors.extend(error.to_api() for error in lex_result.errors)
    response.meta.reached_phase = "lexer"
    response.meta.ok = lex_result.ok
    response.meta.timings.append(PhaseTiming(phase="lexer", ms=round(lex_ms, 3)))

    # --- Phase 2: syntax analysis --------------------------------------
    # Parsed even when the lexer reported errors: the scanner recovers and
    # always yields a complete, EOF-terminated stream, so the parse is still
    # meaningful and more useful than refusing to run.
    #
    # Populates `ast`, not `parseTree`. They are different artefacts: this is
    # the abstract tree, while `parseTree` in the plan's section 3 schema is the
    # concrete derivation belonging to the Tier B table-driven parser.
    parse_started = time.perf_counter()
    function_count = 0
    program: Program | None = None
    try:
        program = parse(lex_result.tokens)
    except ParseError as exc:
        response.errors.append(exc.to_api())
        response.meta.ok = False
    else:
        response.meta.reached_phase = "parser"
        function_count = len(program.functions)
    parse_ms = (time.perf_counter() - parse_started) * 1000.0
    response.meta.timings.append(PhaseTiming(phase="parser", ms=round(parse_ms, 3)))

    # --- Phase 2b: table-driven parse trace (Tier B item 9) -------------
    # A second, independent parse of the same token stream, over
    # app.table_parser's own LL(1) table -- never raises, so this runs
    # regardless of whether the recursive-descent parse above succeeded,
    # and has no effect on `meta.ok`/`reached_phase`: it is purely additive,
    # populating the two CompileResponse fields (`parseTrace`, `parseTree`)
    # the schema already reserved for exactly this.
    table_started = time.perf_counter()
    table_result = trace_table_parse(lex_result.tokens)
    table_ms = (time.perf_counter() - table_started) * 1000.0
    response.parse_trace = [
        ParseStep(step=s.step, stack=list(s.stack), input=list(s.input), action=s.action)
        for s in table_result.steps
    ]
    response.parse_tree = _table_tree_to_api(table_result.tree)
    response.meta.timings.append(PhaseTiming(phase="table_parser", ms=round(table_ms, 3)))

    # --- Phase 3: semantic analysis ------------------------------------
    # Only runs when the parser produced a tree: there is nothing to resolve
    # names against otherwise. Type errors do not stop the pipeline -- they are
    # reported alongside the artefacts, so the Semantic tab still shows the
    # symbol table that was built before the first bad expression.
    if program is not None:
        semantic_started = time.perf_counter()
        semantics = analyse(program)
        semantic_ms = (time.perf_counter() - semantic_started) * 1000.0

        response.symbols = semantics.symbols
        response.type_errors = semantics.diagnostics
        response.meta.reached_phase = "semantic"
        if not semantics.ok:
            response.meta.ok = False
        response.meta.timings.append(PhaseTiming(phase="semantic", ms=round(semantic_ms, 3)))

    # --- Phase 4: intermediate code generation --------------------------
    # Runs even when semantic analysis found type errors: every expression
    # still carries a type (UNKNOWN where checking failed), so translation
    # cannot raise on an ill-typed program, and showing the TAC for a program
    # with a type error is still useful.
    if program is not None:
        tac_started = time.perf_counter()
        tac_result = generate_tac(program)
        tac_ms = (time.perf_counter() - tac_started) * 1000.0

        response.tac = tac_result.instructions
        response.blocks = tac_result.blocks
        response.cfg = tac_result.cfg
        response.meta.reached_phase = "tac"
        response.meta.timings.append(PhaseTiming(phase="tac", ms=round(tac_ms, 3)))

        # --- Phase 5: optimisation --------------------------------------
        # Runs on the raw (pre-API) instruction list TacResult kept around
        # for exactly this purpose, so optimise() can mutate freely rather
        # than re-parsing the wire format it would otherwise be handed.
        opt_started = time.perf_counter()
        response.optimized = optimize(
            tac_result.code, tac_result.function_bounds, request.optimizations
        )
        opt_ms = (time.perf_counter() - opt_started) * 1000.0
        response.meta.reached_phase = "optimize"
        response.meta.timings.append(PhaseTiming(phase="optimize", ms=round(opt_ms, 3)))

        # --- Phase 6: target code generation ----------------------------
        # Runs on the optimised TAC, so the Target tab's assembly reflects
        # whichever passes the Optimization tab's toggles actually ran.
        codegen_started = time.perf_counter()
        asm_result = generate_asm(response.optimized.tac, tac_result.function_params)
        codegen_ms = (time.perf_counter() - codegen_started) * 1000.0

        response.asm = asm_result.asm
        response.meta.reached_phase = "codegen"
        response.meta.timings.append(PhaseTiming(phase="codegen", ms=round(codegen_ms, 3)))

    # Serialised only once every phase that annotates the tree has run. The
    # semantic analyser writes `inferred_type` onto the expression nodes in
    # place, so a dictionary taken right after parsing would carry none of the
    # type annotations the Syntax tab draws.
    if program is not None:
        response.ast = program.to_dict()

    response.meta.timings.append(
        PhaseTiming(phase="total", ms=round((time.perf_counter() - started) * 1000.0, 3))
    )

    logger.info(
        "compile",
        extra={
            "source_lines": response.meta.source_lines,
            "source_bytes": response.meta.source_bytes,
            "reached_phase": response.meta.reached_phase,
            "token_count": len(response.tokens),
            "lexical_errors": len(lex_result.errors),
            "functions": function_count,
            "symbols": len(response.symbols),
            "type_errors": len(response.type_errors),
            "tac_instructions": len(response.tac),
            "basic_blocks": len(response.blocks),
            "instructions_after_optimization": response.optimized.stats.instructions_after,
            "instructions_removed": response.optimized.stats.removed,
            "asm_instructions": len(response.asm),
            "total_errors": len(response.errors),
            "optimizations": request.optimizations.model_dump(),
        },
    )
    return response


@app.post("/run", response_model=RunResponse, tags=["pipeline"])
async def run_program(request: RunRequest) -> RunResponse:
    """Execute stack-VM assembly and capture its output.

    Two ways to call this, matching the two fields on
    :class:`~app.schemas.RunRequest`: with ``asm`` (the normal path -- the
    frontend sends back exactly the assembly a prior ``/compile`` returned,
    so Run executes what Build actually produced, not a silent recompile of
    whatever the editor holds now), or with ``source`` (a one-shot
    compile-and-run convenience for API callers who have not called
    ``/compile`` first). ``main``'s entry address is never separate state to
    keep in sync with either: it is recovered from the assembly itself by
    :func:`app.vm.find_entry`.

    Args:
        request: Either pre-generated assembly, or source to compile and run,
            plus an instruction budget that guards against infinite loops.

    Returns:
        A :class:`~app.schemas.RunResponse` with captured stdout and the number
        of instructions retired.
    """
    if request.source is not None:
        lex_result = tokenize(request.source)
        try:
            program = parse(lex_result.tokens)
        except ParseError as exc:
            return RunResponse(output="", steps=0, halted=False, error=exc.to_api().message)

        analyse(program)
        tac_result = generate_tac(program)
        optimized = optimize(tac_result.code, tac_result.function_bounds, OptimizationToggles())
        asm_result = generate_asm(optimized.tac, tac_result.function_params)
        asm = asm_result.asm
        entry = asm_result.entry
        main_param_count = len(tac_result.function_params.get("main", []))
    else:
        asm = request.asm
        entry = find_entry(asm, "main")
        main_param_count = 0

    result = run_vm(asm, entry, max_steps=request.max_steps, main_param_count=main_param_count)

    logger.info(
        "run",
        extra={
            "asm_len": len(asm),
            "has_source": request.source is not None,
            "steps": result.steps,
            "halted": result.halted,
            "error": result.error,
        },
    )
    return RunResponse(
        output=result.output, steps=result.steps, halted=result.halted, error=result.error
    )


# ---------------------------------------------------------------------------
# Parser Theory Lab
# ---------------------------------------------------------------------------
#
# Everything below adapts app.grammar's textbook-pure result types (which
# know nothing about JSON or pydantic, on purpose -- see that module's own
# docstring) into the wire shapes app.schemas.GrammarResponse promises. The
# algorithms themselves are proven correct against the book in
# tests/test_grammar.py; what is adapted here is purely presentation, so the
# tests for this layer check that the JSON shape round-trips correctly, not
# that the numbers are right -- that was already settled.


def _action_cell(actions: tuple[Any, ...]) -> list[str]:
    """Render one ACTION cell's actions as the book's own shorthand.

    Args:
        actions: Every action recorded for one (state, terminal) cell --
            normally one; more than one is a conflict, rendered as every
            action so a conflicted cell visibly shows what collided.

    Returns:
        ``["s5"]`` for a shift to state 5, ``["r2"]`` for a reduce by
        production 2, ``["acc"]`` for accept, or more than one entry for a
        conflict (e.g. ``["s6", "r2"]``).
    """
    rendered: list[str] = []
    for action in actions:
        if action.kind == "shift":
            rendered.append(f"s{action.target}")
        elif action.kind == "accept":
            rendered.append("acc")
        else:
            assert action.production is not None  # a reduce action always carries one
            rendered.append(f"r{action.production.index}")
    return rendered


def _lr_table_to_api(table: LrTable) -> dict[str, dict[str, list[str]]]:
    """Reshape an :class:`~app.grammar.LrTable` into the API's table shape.

    Args:
        table: An SLR(1) or LALR(1) table from :mod:`app.grammar`.

    Returns:
        ``{state: {symbol: [action, ...]}}``, merging ACTION (terminal
        columns, shown as ``s``/``r``/``acc`` shorthand) and GOTO
        (nonterminal columns, shown as the bare target state number) into
        one table per state, exactly as the book draws them side by side.
    """
    rows: dict[str, dict[str, list[str]]] = {}
    for (state, symbol), actions in table.action.items():
        rows.setdefault(str(state), {})[symbol] = _action_cell(actions)
    for (state, symbol), target in table.goto.items():
        rows.setdefault(str(state), {})[symbol] = [str(target)]
    return rows


def _lr0_items_to_api(automaton: Automaton) -> list[dict[str, Any]]:
    """Render an LR automaton's states as the "canonical collection" display.

    Args:
        automaton: An LR(0) or merged-LALR automaton from :mod:`app.grammar`.

    Returns:
        One entry per state: its index, its items rendered as
        ``"A -> α . β"`` strings, and its outgoing transitions keyed by
        symbol (a terminal's transition is a shift, a nonterminal's is a
        goto -- the frontend already knows which is which).
    """
    result: list[dict[str, Any]] = []
    for lr_state in automaton.states:
        transitions = {
            symbol: target
            for (state_index, symbol), target in automaton.transitions.items()
            if state_index == lr_state.index
        }
        result.append(
            {
                "state": lr_state.index,
                "items": sorted(str(item) for item in lr_state.items),
                "transitions": transitions,
            }
        )
    return result


def _ll1_table_to_api(analysis: GrammarAnalysis) -> dict[str, dict[str, list[str]]]:
    """Reshape the LL(1) table into ``{nonterminal: {terminal: [production]}}``.

    A cell with more than one production in its list is a conflict -- the
    table itself carries that information, so the frontend does not need a
    separate lookup into :attr:`GrammarAnalysis.ll1`'s conflict list just to
    know which cells to flag.

    Args:
        analysis: The full grammar analysis.

    Returns:
        The reshaped table.
    """
    rows: dict[str, dict[str, list[str]]] = {}
    for (nonterminal, terminal), productions in analysis.ll1.table.items():
        rows.setdefault(nonterminal, {})[terminal] = [str(p) for p in productions]
    return rows


def _conflicts_to_api(analysis: GrammarAnalysis) -> list[GrammarConflict]:
    """Collect every conflict from all three table constructions.

    Args:
        analysis: The full grammar analysis.

    Returns:
        Every LL(1), SLR(1) and LALR(1) conflict, each tagged with which
        table it came from -- the distinction that matters most here, since
        a grammar can have an SLR conflict an LALR table resolves (and the
        whole reason this project builds both).
    """
    return [
        GrammarConflict(
            table=table_name, kind=c.kind, state=c.state, symbol=c.symbol, detail=c.detail
        )
        for table_name, conflicts in (
            ("ll1", analysis.ll1.conflicts),
            ("slr", analysis.slr.conflicts),
            ("lalr", analysis.lalr.conflicts),
        )
        for c in conflicts
    ]


def _parse_trace_to_api(table: LrTable, input_string: str | None) -> list[ParseStep]:
    """Trace ``input_string`` through ``table``, if one was given.

    Always traced on the LALR(1) table rather than SLR(1): LALR is proven
    (``TestSlrGrammarsAreAlwaysLalr`` in ``tests/test_grammar.py``) to accept
    everything SLR does and sometimes more, so it is the table least likely
    to reject input the grammar's author actually meant to be valid.

    Args:
        table: The LALR(1) table to drive.
        input_string: Whitespace-separated terminals, or ``None``/empty for
            no trace.

    Returns:
        The step-by-step trace, or an empty list if no input was given.
    """
    if not input_string or not input_string.strip():
        return []
    steps = trace_lr_parse(table, input_string.split())
    return [
        ParseStep(step=s.step, stack=list(s.stack), input=list(s.input), action=s.action)
        for s in steps
    ]


def _grammar_analysis_to_response(
    analysis: GrammarAnalysis, input_string: str | None
) -> GrammarResponse:
    """Adapt a full :class:`~app.grammar.GrammarAnalysis` into the API shape.

    Args:
        analysis: The result of :func:`app.grammar.analyse`.
        input_string: The request's optional parse-trace input.

    Returns:
        The populated response.
    """
    grammar = analysis.grammar
    return GrammarResponse(
        terminals=sorted(grammar.terminals),
        non_terminals=sorted(grammar.nonterminals),
        start_symbol=grammar.start_symbol,
        productions=[str(p) for p in grammar.productions],
        first={symbol: sorted(values) for symbol, values in analysis.first.items()},
        follow={symbol: sorted(values) for symbol, values in analysis.follow.items()},
        ll1_table=_ll1_table_to_api(analysis),
        is_ll1=analysis.ll1.is_ll1,
        lr0_items=_lr0_items_to_api(analysis.lr0),
        slr_table=_lr_table_to_api(analysis.slr),
        lalr_table=_lr_table_to_api(analysis.lalr),
        conflicts=_conflicts_to_api(analysis),
        parse_trace=_parse_trace_to_api(analysis.lalr, input_string),
        errors=[],
    )


@app.post("/analyze-grammar", response_model=GrammarResponse, tags=["theory"])
async def analyze_grammar(request: GrammarRequest) -> GrammarResponse:
    """Analyse an arbitrary context-free grammar for the Parser Theory Lab.

    Runs every construction in :mod:`app.grammar` -- FIRST/FOLLOW, the LL(1)
    table, the LR(0) canonical collection, and the SLR(1) and LALR(1) tables
    -- over whatever grammar text was submitted, not just MiniLang's own. A
    grammar that fails to parse (a missing arrow, an unresolvable start
    symbol, and so on) is reported as a single error diagnostic rather than
    a 500, so a mid-typing paste in the Parser Theory tab degrades to an
    empty-but-valid response instead of breaking the page.

    Args:
        request: The grammar text, an optional start symbol, and an optional
            input string to trace a parse over.

    Returns:
        A :class:`~app.schemas.GrammarResponse` holding the computed tables.
    """
    try:
        analysis = analyse_grammar(request.grammar, request.start_symbol)
    except GrammarError as exc:
        logger.info("analyze-grammar", extra={"ok": False, "error": str(exc)})
        return GrammarResponse(
            errors=[Diagnostic(message=str(exc), severity="error", phase="grammar")]
        )

    response = _grammar_analysis_to_response(analysis, request.input_string)
    logger.info(
        "analyze-grammar",
        extra={
            "ok": True,
            "start_symbol": response.start_symbol,
            "is_ll1": response.is_ll1,
            "lr0_states": len(response.lr0_items),
            "conflicts": len(response.conflicts),
            "traced_steps": len(response.parse_trace),
        },
    )
    return response


# Mounted last so the API routes above take precedence over static paths.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
