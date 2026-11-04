"""Table-driven predictive parsing: MiniLang's own grammar, Algorithm 4.4 style.

Tier B item 9, and the backend for the Syntax tab's step-through parse
animation. Where :mod:`app.grammar` builds tables *generically*, for any
grammar a user pastes, this module runs one specific table -- built from
:data:`app.samples.MINILANG_GRAMMAR` -- over the real token stream of
whatever MiniLang program is being compiled, using the classical
stack-based LL(1) driver (Aho, Lam, Sethi & Ullman, *Compilers: Principles,
Techniques, and Tools*, 2nd edition, Algorithm 4.4 / Figure 4.19, section
4.4.1, "Nonrecursive Predictive Parsing").

Why this exists alongside ``app/parser.py``
---------------------------------------------
``app/parser.py`` is deliberately hand-written recursive descent, not
table-driven, for the reasons given in its own docstring -- and its own
``_statement`` docstring says plainly that distinguishing an assignment from
an expression statement needs two tokens of lookahead. A genuinely
table-driven parser cannot use that shortcut: Algorithm 4.4 consults the
table on exactly one lookahead symbol, full stop. So this module's grammar
is not simply "the plan's grammar" -- it is that grammar with the one
left-recursion-eliminated already (Report 1's own expression grammar) and,
new here, **left factored** (Aho et al., section 4.3.4) wherever two
alternatives shared a leading terminal. :data:`app.samples.MINILANG_GRAMMAR`
carries the full rationale and is independently confirmed conflict-free
LL(1) in ``tests/test_grammar.py``.

The two parsers never call each other and are never compared for anything
but acceptance (``tests/test_table_parser.py`` cross-checks that both accept
exactly the same set of programs) -- this module's own tree and trace are a
second, independent artefact, not a replacement for the AST
``app/parser.py``/``app/ast_nodes.py`` already produce.

The algorithm, briefly
------------------------
A stack starts holding the end marker ``$`` with the start symbol above it.
Repeatedly: if the stack's top is a terminal (or ``$``), it must match the
current input symbol exactly -- match and pop both, or the input is
rejected; if the top is a nonterminal, the table entry for
(top, current input symbol) says which production to apply -- pop the
nonterminal, push its right-hand side in reverse (so the leftmost symbol
ends up on top again). The parse accepts when both stack and input are
simultaneously exhausted to ``$``. Because the table was built from a
grammar already proven to have at most one entry per cell, this process
never has to choose between competing productions -- which is the entire
point of insisting on an LL(1) grammar before writing this driver.

Building the concrete parse tree is the same process read as a derivation:
every production applied becomes one node with one child per right-hand-side
symbol (an explicit epsilon leaf for an empty production, so the tree stays
honest about what was actually derived rather than silently skipping it),
and every match of a terminal fills in that leaf's source position and
lexeme. This is the "concrete parse tree" a compilers course draws by hand
next to a derivation -- a different, larger object than the abstract syntax
tree :mod:`app.ast_nodes` builds, which deliberately collapses grammar
scaffolding (the ``Tail`` nonterminals above are a prime example) that
exists only to keep the grammar LL(1), not because it means anything once
parsing is done.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Final

from app.grammar import END_MARKER, EPSILON, GrammarAnalysis, analyse
from app.lexer import Token, TokenType
from app.samples import MINILANG_GRAMMAR

_START_SYMBOL: Final[str] = "program"

#: Built once at import time. The assertion is a regression guard: if a
#: future edit to :data:`app.samples.MINILANG_GRAMMAR` ever reintroduces an
#: LL(1) conflict, this fails immediately and loudly on startup rather than
#: silently producing a table with missing or arbitrary entries.
_ANALYSIS: Final[GrammarAnalysis] = analyse(MINILANG_GRAMMAR, start_symbol=_START_SYMBOL)
assert _ANALYSIS.ll1.is_ll1, "MINILANG_GRAMMAR is no longer LL(1) -- see its own docstring"

#: Maximum steps before giving up, guarding the demo server against an
#: infinite loop -- which a sound LL(1) driver cannot produce on its own
#: (every iteration strictly shrinks the stack or consumes a token), but a
#: defensive bound costs nothing and matches the discipline already used for
#: :func:`app.grammar.run_lr_parse`.
_MAX_STEPS: Final[int] = 50_000

#: Maximum parser-stack length before a parse is rejected as too deeply
#: nested. Table-driven parsing is iterative (Algorithm 4.4 has no call
#: stack of its own), so unlike ``app.parser``'s recursive descent it has no
#: inherent nesting limit on the parse itself -- a MiniLang program's stack
#: length grows only with genuine nesting, never with its length or
#: statement count (measured: a 200-statement, unnested function peaks at a
#: stack length of 12; ``tests/programs/demo.ml`` peaks at 22).
#:
#: The number here is not chosen for the parse's own sake, though: it is
#: pinned to a *downstream* constraint. ``pydantic-core``'s serializer
#: refuses a self-referential model (:class:`app.schemas.TreeNode`'s
#: ``children: list[TreeNode]``) nested more than roughly 95-98 levels deep
#: with "Circular reference detected (depth exceeded)" -- a hard-coded
#: limit, confirmed by direct measurement to be entirely unaffected by
#: ``sys.setrecursionlimit``, so it cannot be configured away. Measured
#: empirically: a stack length of 64 keeps the resulting tree at a depth
#: safely below that ceiling (confirmed by round-tripping the deepest
#: parse this limit still accepts through
#: :meth:`~app.schemas.TreeNode.model_dump_json` directly) with a
#: comfortable margin, while a stack length of 82 (seven parentheses
#: nested, in practice) already produces a tree past it. This keeps the
#: table-driven path from crashing the HTTP response on input the
#: recursive-descent parser would separately, and for its own reasons,
#: reject anyway via ``app.parser.MAX_NESTING_DEPTH``.
_MAX_STACK_DEPTH: Final[int] = 64


def terminal_for(token: Token) -> str:
    """Map a lexer token onto the grammar terminal it corresponds to.

    Mechanical and total: every :class:`~app.lexer.TokenType` member maps to
    ``member.value.lower()`` (e.g. ``IDENT`` -> ``"ident"``,
    ``LPAREN`` -> ``"lparen"``), which is exactly how every terminal in
    :data:`app.samples.MINILANG_GRAMMAR` is spelled -- except ``EOF``, which
    maps to :data:`app.grammar.END_MARKER` (``"$"``) instead, since the
    grammar never names the end marker directly (section 4.4.2 treats it
    the same way: implicit, not a symbol of the grammar).

    Args:
        token: The token to classify.

    Returns:
        The grammar terminal name.
    """
    if token.type is TokenType.EOF:
        return END_MARKER
    return token.type.value.lower()


@dataclass
class ParseTreeNode:
    """One node of the concrete parse tree, built up during the parse.

    Mutable, unlike :mod:`app.grammar`'s frozen dataclasses: a node for a
    nonterminal is created with empty ``children`` the moment the production
    that introduces it is applied, and filled in afterwards as its own
    children are parsed -- exactly how a derivation grows a parse tree one
    production at a time.

    Attributes:
        symbol: The grammar symbol this node stands for.
        kind: ``"terminal"``, ``"nonterminal"``, or ``"epsilon"`` (a node
            standing in for an empty production's right-hand side).
        step: The trace step at which this node was created.
        value: For a terminal, its actual source lexeme, filled in once
            matched; ``None`` before that and for every nonterminal/epsilon
            node, which has no single lexeme of its own.
        line: Source line, filled in once a terminal is matched.
        col: Source column, filled in once a terminal is matched.
        children: Child nodes, left to right, in production order.
    """

    symbol: str
    kind: str
    step: int
    value: str | None = None
    line: int | None = None
    col: int | None = None
    children: list[ParseTreeNode] = field(default_factory=list)


@dataclass(frozen=True)
class TableParseStep:
    """One step of a table-driven predictive parse, for display or testing.

    Attributes:
        step: 0-based index of this step.
        stack: The parser's stack *before* this step's action, bottom
            first -- grammar symbols only, no states (an LL(1) table has
            none), e.g. ``("$", "block", "rbrace")``.
        input: The input terminals not yet consumed, including the trailing
            end marker.
        action: What this step does: ``"match '<lexeme>'"``,
            ``"apply <production>"``, ``"accept"``, or an ``"error: ..."``
            description of why the parse cannot continue.
    """

    step: int
    stack: tuple[str, ...]
    input: tuple[str, ...]
    action: str


def _step_table_parse(tokens: Sequence[Token], root: ParseTreeNode) -> Iterator[TableParseStep]:
    """Shared driver behind :func:`run_table_parse` and :func:`trace_table_parse`.

    Builds the concrete parse tree into ``root`` as a side effect while
    yielding one :class:`TableParseStep` per action -- the same "one
    generator, two consumers" discipline :mod:`app.grammar` uses for its own
    LR driver, so a boolean "did it parse" answer and the full step trace
    can never silently disagree with each other.

    Args:
        tokens: The token stream to parse, already ``EOF``-terminated (as
            every :func:`app.lexer.tokenize` result is).
        root: A freshly created node for the start symbol; mutated in place
            to become the finished (or partial, on error) parse tree.

    Yields:
        One step per action taken. The last one yielded has
        ``action == "accept"`` on success, or an ``action`` starting with
        ``"error:"`` otherwise.

    Raises:
        ValueError: ``tokens`` is empty or not ``EOF``-terminated.
    """
    terminals = [terminal_for(token) for token in tokens]
    if not terminals or terminals[-1] != END_MARKER:
        raise ValueError("tokens must be a non-empty, EOF-terminated token stream")

    stack: list[str] = [END_MARKER, _START_SYMBOL]
    node_stack: list[ParseTreeNode | None] = [None, root]
    position = 0
    step = 0

    while step < _MAX_STEPS:
        top = stack[-1]
        lookahead = terminals[position]
        remaining = tuple(terminals[position:])

        if top == END_MARKER and lookahead == END_MARKER:
            yield TableParseStep(step, tuple(stack), remaining, "accept")
            return

        if top not in _ANALYSIS.grammar.nonterminals:
            # A terminal (or "$") on top must match the input exactly.
            if top != lookahead:
                yield TableParseStep(
                    step,
                    tuple(stack),
                    remaining,
                    f"error: expected '{top}' but found '{lookahead}'",
                )
                return
            actual = tokens[position]
            node = node_stack[-1]
            assert node is not None  # every real stack symbol carries one
            node.value, node.line, node.col = actual.lexeme, actual.line, actual.col
            yield TableParseStep(step, tuple(stack), remaining, f"match '{actual.lexeme}'")
            stack.pop()
            node_stack.pop()
            position += 1
            step += 1
            continue

        cell = _ANALYSIS.ll1.table.get((top, lookahead))
        if not cell:
            yield TableParseStep(
                step,
                tuple(stack),
                remaining,
                f"error: no rule for '{top}' on lookahead '{lookahead}'",
            )
            return
        production = cell[0]
        yield TableParseStep(step, tuple(stack), remaining, f"apply {production}")

        parent = node_stack[-1]
        assert parent is not None
        stack.pop()
        node_stack.pop()
        if not production.rhs:
            parent.children.append(ParseTreeNode(symbol=EPSILON, kind="epsilon", step=step))
        else:
            new_children = [
                ParseTreeNode(
                    symbol=symbol,
                    kind="nonterminal" if symbol in _ANALYSIS.grammar.nonterminals else "terminal",
                    step=step,
                )
                for symbol in production.rhs
            ]
            parent.children.extend(new_children)
            for symbol, child in zip(reversed(production.rhs), reversed(new_children), strict=True):
                stack.append(symbol)
                node_stack.append(child)
            if len(stack) > _MAX_STACK_DEPTH:
                yield TableParseStep(
                    step + 1,
                    tuple(stack),
                    tuple(terminals[position:]),
                    f"error: expression nesting is too deep (limit {_MAX_STACK_DEPTH})",
                )
                return
        step += 1

    yield TableParseStep(
        step, tuple(stack), tuple(terminals[position:]), "error: exceeded maximum steps"
    )


@dataclass(frozen=True)
class TableParseResult:
    """The full result of a table-driven parse.

    Attributes:
        steps: Every step taken, in order.
        tree: The concrete parse tree built along the way -- complete on
            success, partial (whatever was built before the failing step)
            otherwise.
        accepted: Whether the last step's action was ``"accept"``.
    """

    steps: tuple[TableParseStep, ...]
    tree: ParseTreeNode
    accepted: bool


def trace_table_parse(tokens: Sequence[Token]) -> TableParseResult:
    """Parse ``tokens`` against :data:`MINILANG_GRAMMAR`, keeping every step.

    Args:
        tokens: The token stream to parse, already ``EOF``-terminated.

    Returns:
        The full trace, tree, and acceptance verdict.
    """
    root = ParseTreeNode(symbol=_START_SYMBOL, kind="nonterminal", step=0)
    steps = tuple(_step_table_parse(tokens, root))
    accepted = bool(steps) and steps[-1].action == "accept"
    return TableParseResult(steps=steps, tree=root, accepted=accepted)


def run_table_parse(tokens: Sequence[Token]) -> bool:
    """Parse ``tokens`` against :data:`MINILANG_GRAMMAR`, keeping only the verdict.

    Args:
        tokens: The token stream to parse, already ``EOF``-terminated.

    Returns:
        ``True`` if the input is accepted, ``False`` otherwise.
    """
    root = ParseTreeNode(symbol=_START_SYMBOL, kind="nonterminal", step=0)
    last_action = ""
    for recorded_step in _step_table_parse(tokens, root):
        last_action = recorded_step.action
    return last_action == "accept"
