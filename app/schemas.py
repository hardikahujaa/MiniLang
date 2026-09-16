"""Typed request/response models for the MiniLang HTTP API.

This module is the single source of truth for the wire format described in
section 3 of the implementation plan. The design rule from the plan is that
``POST /compile`` returns the artefacts of *every* phase in one object, so the
frontend is pure rendering with no orchestration and no client-side state
machine.

Python attributes are ``snake_case``; the JSON they serialise to is
``camelCase`` (``parse_trace`` -> ``parseTrace``), which keeps both sides of
the wire idiomatic. ``populate_by_name`` is enabled so backend code can build
models with either spelling.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool
from pydantic.alias_generators import to_camel


class ApiModel(BaseModel):
    """Base model applying the project-wide camelCase wire convention."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


# ---------------------------------------------------------------------------
# Phase 1 -- Lexical analysis
# ---------------------------------------------------------------------------


class Token(ApiModel):
    """A single lexical token produced by :mod:`app.lexer`."""

    type: str = Field(description="Fine-grained token class, e.g. 'FUNC', 'IDENT', 'LPAREN'.")
    lexeme: str = Field(description="The exact source text this token was matched from.")
    line: int = Field(ge=1, description="1-based source line.")
    col: int = Field(ge=1, description="1-based column of the token's first character.")
    category: str | None = Field(
        default=None,
        description=(
            "Broad class the type belongs to: keyword, identifier, literal, operator, "
            "punctuation or special. Supplied by the backend so the token table can "
            "colour-code rows without duplicating the type-to-category mapping in JS."
        ),
    )


class DfaView(ApiModel):
    """A finite automaton rendered for the Lexical tab's animation.

    Populated by the Tier C ``regex -> NFA -> DFA`` module; ``None`` until then.
    """

    states: list[str] = Field(default_factory=list)
    alphabet: list[str] = Field(default_factory=list)
    start: str | None = None
    accepting: list[str] = Field(default_factory=list)
    transitions: list[dict[str, str]] = Field(
        default_factory=list,
        description="Edges as {'from': .., 'symbol': .., 'to': ..}.",
    )


# ---------------------------------------------------------------------------
# Phase 2 -- Syntax analysis
# ---------------------------------------------------------------------------


class ParseStep(ApiModel):
    """One row of the step-through parse trace shown on the Syntax tab."""

    step: int = Field(ge=0, description="0-based index of this step in the trace.")
    stack: list[str] = Field(default_factory=list, description="Parser stack, bottom first.")
    input: list[str] = Field(default_factory=list, description="Remaining input tokens.")
    action: str = Field(default="", description="Human-readable action, e.g. 'shift id'.")


class TreeNode(ApiModel):
    """A node in a D3-renderable tree (parse tree or AST view).

    The shape is deliberately the one D3's hierarchy layout expects: a ``name``
    to draw and a ``children`` list to recurse into.
    """

    name: str = Field(description="Label drawn on the node.")
    kind: str | None = Field(default=None, description="Node category, used for styling.")
    value: str | None = Field(default=None, description="Literal or identifier text, if any.")
    line: int | None = None
    col: int | None = None
    children: list[TreeNode] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Phase 3 -- Semantic analysis
# ---------------------------------------------------------------------------


class Symbol(ApiModel):
    """One entry of the scoped symbol table shown on the Semantic tab."""

    name: str
    type: str = Field(description="Declared type: int, float, bool, void, or a function type.")
    kind: Literal["variable", "parameter", "function"] = "variable"
    scope: str = Field(default="global", description="Owning scope's qualified name.")
    depth: int = Field(default=0, ge=0, description="Nesting depth; 0 is global.")
    line: int | None = None
    col: int | None = None


class Diagnostic(ApiModel):
    """A non-fatal compiler message: a type error, warning, or note."""

    line: int | None = None
    col: int | None = None
    message: str
    severity: Literal["error", "warning", "info"] = "error"
    phase: str = Field(default="semantic", description="Phase that raised this diagnostic.")


# ---------------------------------------------------------------------------
# Phase 4 -- Intermediate code generation
# ---------------------------------------------------------------------------


class TacInstruction(ApiModel):
    """A single three-address-code instruction."""

    index: int = Field(ge=0, description="Position in the instruction list.")
    op: str = Field(description="Operation, e.g. '+', 'goto', 'param', 'label'.")
    arg1: str | None = None
    arg2: str | None = None
    result: str | None = None
    text: str = Field(description="Pretty-printed form, e.g. 't1 = a + b'.")


class BasicBlock(ApiModel):
    """A maximal straight-line run of TAC with one entry and one exit."""

    id: str = Field(description="Block label, e.g. 'B0'.")
    leader: int = Field(ge=0, description="Index of the leader instruction.")
    instructions: list[TacInstruction] = Field(default_factory=list)
    successors: list[str] = Field(default_factory=list, description="IDs of successor blocks.")


class CfgEdge(ApiModel):
    """A directed edge of the control-flow graph."""

    source: str
    target: str
    label: str | None = Field(default=None, description="e.g. 'true', 'false', 'fall-through'.")


class CfgView(ApiModel):
    """The control-flow graph as consumed by the D3 renderer on the ICG tab."""

    nodes: list[BasicBlock] = Field(default_factory=list)
    edges: list[CfgEdge] = Field(default_factory=list)
    entry: str | None = None
    exits: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Phase 5 -- Optimisation
# ---------------------------------------------------------------------------


class OptimizationToggles(ApiModel):
    """Which optimisation passes to run, one checkbox each on the UI.

    The flags are :class:`~pydantic.StrictBool` rather than plain ``bool`` on
    purpose. Pydantic's default lax mode coerces strings such as ``"yes"`` and
    ``"off"`` into booleans, which would let a frontend bug silently *enable* an
    optimisation pass instead of failing loudly. JSON has real booleans; a
    client sending anything else is wrong and should get a 422.
    """

    constant_folding: StrictBool = True
    constant_propagation: StrictBool = True
    dead_code_elimination: StrictBool = True
    common_subexpression_elimination: StrictBool = False


class OptimizationChange(ApiModel):
    """One entry in the optimisation log: what a pass did, and to which line."""

    pass_name: str = Field(description="Pass that made the change, e.g. 'constant-folding'.")
    index: int | None = Field(default=None, description="TAC index affected, if applicable.")
    before: str | None = None
    after: str | None = None
    detail: str = Field(default="", description="Human-readable explanation of the change.")


class OptimizationStats(ApiModel):
    """Instruction-count deltas. Feeds the results table in the report."""

    instructions_before: int = Field(default=0, ge=0)
    instructions_after: int = Field(default=0, ge=0)
    removed: int = Field(default=0, description="Net instructions eliminated.")
    per_pass: dict[str, int] = Field(
        default_factory=dict,
        description="Instructions removed or rewritten, keyed by pass name.",
    )


class OptimizationResult(ApiModel):
    """The optimised program plus an audit trail of how it got that way."""

    tac: list[TacInstruction] = Field(default_factory=list)
    log: list[OptimizationChange] = Field(default_factory=list)
    stats: OptimizationStats = Field(default_factory=OptimizationStats)
    blocks: list[BasicBlock] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Phase 6 -- Target code generation and execution
# ---------------------------------------------------------------------------


class AsmInstruction(ApiModel):
    """One stack-VM assembly instruction."""

    index: int = Field(ge=0)
    op: str = Field(description="Mnemonic, e.g. 'PUSH', 'ADD', 'JMPF', 'CALL'.")
    operand: str | None = None
    text: str = Field(description="Pretty-printed form, e.g. 'PUSH 10'.")
    comment: str | None = None


# ---------------------------------------------------------------------------
# Error recovery (the project's novelty feature, plan section 7)
# ---------------------------------------------------------------------------


class Suggestion(ApiModel):
    """A single ranked candidate fix for a syntax error."""

    text: str = Field(description="The proposed repair, e.g. insert a semicolon.")
    confidence: float = Field(ge=0.0, le=1.0, description="Ranking score in [0, 1].")
    rationale: str = Field(default="", description="Which heuristic produced this candidate.")


class SyntaxErrorReport(ApiModel):
    """A recovered syntax error with ranked repair suggestions.

    Mirrors the ``errors`` entries of the plan's section 3 schema. ``suggestion``
    and ``confidence`` restate the top-ranked candidate so simple consumers can
    read them without walking ``suggestions``.
    """

    line: int = Field(ge=1)
    col: int = Field(ge=1)
    message: str = Field(default="", description="Primary error text.")
    expected: list[str] = Field(default_factory=list, description="Expected-token set.")
    found: str | None = Field(default=None, description="The offending lexeme.")
    suggestion: str | None = Field(default=None, description="Top-ranked repair.")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    suggestions: list[Suggestion] = Field(
        default_factory=list, description="Top-3 ranked candidates."
    )
    explanation: str | None = Field(
        default=None,
        description="Optional one-line natural-language explanation (LLM layer).",
    )
    recovered: bool = Field(
        default=False, description="True if the parser resynchronised and continued."
    )


# ---------------------------------------------------------------------------
# Request / response envelopes
# ---------------------------------------------------------------------------


class CompileRequest(ApiModel):
    """Body of ``POST /compile``."""

    source: str = Field(description="MiniLang source text to compile.")
    optimizations: OptimizationToggles = Field(default_factory=OptimizationToggles)
    explain_errors: StrictBool = Field(
        default=False,
        description="Request LLM explanations for syntax errors. Ignored when no API key is set.",
    )


class PhaseTiming(ApiModel):
    """Wall-clock cost of one compilation phase, in milliseconds."""

    phase: str
    ms: float = Field(ge=0.0)


class CompileMeta(ApiModel):
    """Non-artefact metadata about a compile: timings and source statistics."""

    ok: bool = Field(default=True, description="False if compilation could not reach codegen.")
    reached_phase: str = Field(
        default="stub", description="Furthest phase completed, e.g. 'codegen'."
    )
    source_lines: int = Field(default=0, ge=0)
    source_bytes: int = Field(default=0, ge=0)
    timings: list[PhaseTiming] = Field(default_factory=list)
    version: str = Field(default="0.1.0")


class CompileResponse(ApiModel):
    """Full ``POST /compile`` payload: every phase's artefacts in one object.

    Field names match section 3 of the implementation plan exactly. Every field
    has a safe empty default, so a phase that has not been implemented yet (or
    that was skipped because an earlier phase failed) simply renders as an empty
    tab rather than breaking the frontend.
    """

    # Phase 1
    tokens: list[Token] = Field(default_factory=list)
    dfa: DfaView | None = None
    # Phase 2
    parse_trace: list[ParseStep] = Field(default_factory=list)
    parse_tree: TreeNode | None = None
    ast: dict[str, Any] | None = None
    # Phase 3
    symbols: list[Symbol] = Field(default_factory=list)
    type_errors: list[Diagnostic] = Field(default_factory=list)
    # Phase 4
    tac: list[TacInstruction] = Field(default_factory=list)
    blocks: list[BasicBlock] = Field(default_factory=list)
    cfg: CfgView | None = None
    # Phase 5
    optimized: OptimizationResult = Field(default_factory=OptimizationResult)
    # Phase 6
    asm: list[AsmInstruction] = Field(default_factory=list)
    output: str = Field(default="", description="Captured stdout from the VM run.")
    # Cross-cutting
    errors: list[SyntaxErrorReport] = Field(default_factory=list)
    meta: CompileMeta = Field(default_factory=CompileMeta)


class RunRequest(ApiModel):
    """Body of ``POST /run``: execute previously generated assembly."""

    asm: list[AsmInstruction] = Field(default_factory=list)
    source: str | None = Field(
        default=None,
        description="Compile this source and run it, when 'asm' is not supplied.",
    )
    max_steps: int = Field(
        default=1_000_000, gt=0, description="Instruction budget; guards against infinite loops."
    )


class RunResponse(ApiModel):
    """Result of executing a program on the stack VM."""

    output: str = Field(default="", description="Captured stdout.")
    steps: int = Field(default=0, ge=0, description="Instructions retired.")
    halted: bool = Field(default=True, description="False if the step budget was exhausted.")
    error: str | None = Field(default=None, description="Runtime error, if execution trapped.")


class GrammarRequest(ApiModel):
    """Body of ``POST /analyze-grammar``: a grammar for the Parser Theory Lab."""

    grammar: str = Field(description="Production rules, one per line, e.g. E -> E + T | T.")
    start_symbol: str | None = Field(
        default=None, description="Defaults to the LHS of the first production."
    )
    input_string: str | None = Field(
        default=None, description="Optional token string to trace a parse over."
    )


class GrammarConflict(ApiModel):
    """A shift/reduce or reduce/reduce conflict found while building a table."""

    kind: Literal["shift-reduce", "reduce-reduce"]
    state: int | None = None
    symbol: str | None = None
    detail: str = ""


class GrammarResponse(ApiModel):
    """Everything the Parser Theory Lab tab renders for one grammar."""

    terminals: list[str] = Field(default_factory=list)
    non_terminals: list[str] = Field(default_factory=list)
    start_symbol: str | None = None
    productions: list[str] = Field(default_factory=list)
    first: dict[str, list[str]] = Field(default_factory=dict)
    follow: dict[str, list[str]] = Field(default_factory=dict)
    ll1_table: dict[str, dict[str, list[str]]] = Field(default_factory=dict)
    is_ll1: bool = False
    lr0_items: list[dict[str, Any]] = Field(default_factory=list)
    slr_table: dict[str, dict[str, list[str]]] = Field(default_factory=dict)
    lalr_table: dict[str, dict[str, list[str]]] = Field(default_factory=dict)
    conflicts: list[GrammarConflict] = Field(default_factory=list)
    parse_trace: list[ParseStep] = Field(default_factory=list)
    errors: list[Diagnostic] = Field(default_factory=list)


class HealthResponse(ApiModel):
    """Payload of ``GET /api/health``."""

    status: Literal["ok"] = "ok"
    version: str
    phases_implemented: list[str] = Field(default_factory=list)


# Resolve the self-reference in TreeNode.children.
TreeNode.model_rebuild()
