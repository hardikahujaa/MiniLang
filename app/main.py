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
an empty tab rather than breaking the page. ``/run`` and ``/analyze-grammar``
are still stubs, awaiting ``app/vm.py`` and ``app/grammar.py``.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Final

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.lexer import tokenize
from app.logging_config import configure_logging, get_logger
from app.parser import ParseError, parse
from app.samples import AMBIGUOUS_GRAMMAR, DEMO_GRAMMAR, DEMO_PROGRAM
from app.schemas import (
    CompileMeta,
    CompileRequest,
    CompileResponse,
    GrammarRequest,
    GrammarResponse,
    HealthResponse,
    PhaseTiming,
    RunRequest,
    RunResponse,
)

logger = get_logger(__name__)

#: Repository root, resolved from this file so the server can be started from
#: any working directory.
BASE_DIR: Final[Path] = Path(__file__).resolve().parent.parent
STATIC_DIR: Final[Path] = BASE_DIR / "static"
INDEX_HTML: Final[Path] = STATIC_DIR / "index.html"

#: Compilation phases that are fully implemented. Each step of the build adds
#: one entry here, and ``GET /api/health`` reports it, which gives the frontend
#: (and the marker) an honest picture of what is wired up.
IMPLEMENTED_PHASES: Final[list[str]] = ["lexer", "parser"]


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


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    """Serve the single-page frontend.

    Returns:
        The ``static/index.html`` file, which loads ``app.js`` and ``viz.js``
        and renders all eight tabs.
    """
    return FileResponse(INDEX_HTML)


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
    try:
        program = parse(lex_result.tokens)
    except ParseError as exc:
        response.errors.append(exc.to_api())
        response.meta.ok = False
    else:
        response.ast = program.to_dict()
        response.meta.reached_phase = "parser"
        function_count = len(program.functions)
    parse_ms = (time.perf_counter() - parse_started) * 1000.0
    response.meta.timings.append(PhaseTiming(phase="parser", ms=round(parse_ms, 3)))

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
            "total_errors": len(response.errors),
            "optimizations": request.optimizations.model_dump(),
        },
    )
    return response


@app.post("/run", response_model=RunResponse, tags=["pipeline"])
async def run_program(request: RunRequest) -> RunResponse:
    """Execute stack-VM assembly and capture its output.

    **Scaffold stub.** Returns an empty result until ``app/vm.py`` lands in
    step 7 of Tier A.

    Args:
        request: Either pre-generated assembly, or source to compile and run,
            plus an instruction budget that guards against infinite loops.

    Returns:
        A :class:`~app.schemas.RunResponse` with captured stdout and the number
        of instructions retired.
    """
    logger.info(
        "run",
        extra={"asm_len": len(request.asm), "has_source": request.source is not None},
    )
    return RunResponse(output="", steps=0, halted=True)


# ---------------------------------------------------------------------------
# Parser Theory Lab
# ---------------------------------------------------------------------------


@app.post("/analyze-grammar", response_model=GrammarResponse, tags=["theory"])
async def analyze_grammar(request: GrammarRequest) -> GrammarResponse:
    """Analyse an arbitrary context-free grammar for the Parser Theory Lab.

    **Scaffold stub.** Returns an empty analysis until ``app/grammar.py`` lands
    in Tier B, where it computes FIRST/FOLLOW sets, the LL(1) table, the LR(0)
    canonical collection, SLR and LALR tables, and flags conflicts.

    Args:
        request: The grammar text, an optional start symbol, and an optional
            input string to trace a parse over.

    Returns:
        A :class:`~app.schemas.GrammarResponse` holding the computed tables.
    """
    logger.info(
        "analyze-grammar",
        extra={
            "grammar_lines": len(request.grammar.splitlines()),
            "start_symbol": request.start_symbol,
        },
    )
    return GrammarResponse(start_symbol=request.start_symbol)


# Mounted last so the API routes above take precedence over static paths.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
