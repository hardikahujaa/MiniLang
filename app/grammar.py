"""Grammar theory: FIRST/FOLLOW, LL(1), LR(0)/SLR(1), and LALR(1) tables.

Phase 2 (Tier B) of the project, and the backend for the Parser Theory Lab
tab, which lets a user paste *any* context-free grammar -- not just
MiniLang's own -- and see the classical table-construction algorithms run
over it. Every algorithm here is the one Aho, Lam, Sethi & Ullman present in
*Compilers: Principles, Techniques, and Tools* (2nd edition), chapter 4, and
each function's docstring names the specific section and algorithm number it
implements, so a claim here can be checked against the book directly rather
than taken on faith.

Why this is a separate module from the real parser
----------------------------------------------------
``app/parser.py`` is a hand-written recursive-descent parser -- deliberately
*not* table-driven, for the reasons given in its own docstring. This module
is the opposite: it is nothing but tables, built mechanically from whatever
grammar text it is handed. The two never call each other. This module exists
to teach and demonstrate the theory a textbook chapter covers, not to parse
MiniLang itself.

What is implemented, and where in the book it comes from
------------------------------------------------------------
* :func:`compute_first` and :func:`first_of_sequence` -- FIRST sets for a
  single symbol and for a string of symbols (section 4.4.2).
* :func:`compute_follow` -- FOLLOW sets (section 4.4.2).
* :func:`build_ll1_table` -- the LL(1) parsing table (Algorithm 4.6, section
  4.4.3), with every cell that more than one production claims recorded as a
  conflict rather than silently overwritten.
* :func:`build_lr0_automaton` -- the canonical collection of LR(0) item sets,
  via :func:`closure0` and :func:`goto0` (Algorithm 4.9, section 4.6.2).
* :func:`build_slr_table` -- the SLR(1) ACTION/GOTO table (Algorithm 4.10,
  section 4.7.2), which decides a reduce action's valid lookaheads from each
  production's *global* FOLLOW set.
* :func:`build_lalr_table` -- the LALR(1) ACTION/GOTO table (section 4.7.3),
  built from each production's *per-state* lookahead set instead of FOLLOW --
  see "How LALR is built" below for the specific construction chosen.
* :func:`run_lr_parse` -- the table-driven shift-reduce recognizer the book
  calls "a model of an LR parser" (Algorithm 4.7, section 4.6.1), used here
  purely as a test oracle: it proves a constructed table actually accepts and
  rejects strings the way the grammar says it should, not just that the
  table-building code ran without raising.

How LALR is built
------------------
The book describes two ways to get an LALR(1) table. One computes each
state's lookaheads directly, by propagating them through the LR(0) automaton
without ever building full LR(1) item sets (section 4.7.4, "Efficient
Construction of LALR Parsing Tables") -- this is what a production compiler
compiler does, because canonical LR(1) collections can be enormous. The
other, simpler one (acknowledged in section 4.7.3 as correct, just not
space-efficient) builds the full canonical collection of LR(1) item sets
first, then merges every pair of states whose *cores* -- their items with the
lookaheads stripped off -- are identical, unioning the lookaheads that
survive the merge. This module takes the second route on purpose: the
grammars this tool is built to analyse are the small, hand-written ones a
course uses for worked examples, so the canonical collection is never large
enough for the efficiency difference to matter, and building it the simple
way means every intermediate state (the full LR(1) item sets, not just the
final merged table) is available to show on the tab and to check in a test,
which the propagation-based algorithm does not produce as a by-product.

A note the book itself makes, repeated here because it is easy to forget:
merging states can occasionally introduce a reduce-reduce conflict that was
not present in the canonical LR(1) collection (never a shift-reduce one --
merging cannot create those). That is not a bug in this module; it is a
genuine property of LALR parsing, and :func:`build_lalr_table` reports it the
same way it reports any other conflict.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

#: The empty-string symbol. Also accepted, case-insensitively, as the sole
#: symbol of a production's right-hand side: ``eps`` or ``epsilon``.
EPSILON: str = "ε"

#: The input end-marker, written ``$`` throughout the book. It is seeded into
#: FOLLOW(start symbol) and used as the lookahead of the augmented grammar's
#: initial LR(1) item, exactly as section 4.4.2 and section 4.7.1 do.
END_MARKER: str = "$"

_EPSILON_SPELLINGS: frozenset[str] = frozenset({"ε", "eps", "epsilon"})
_ARROW_PATTERN = re.compile(r"->|→")


class GrammarError(Exception):
    """Raised when grammar text cannot be parsed, or names no productions."""


# ---------------------------------------------------------------------------
# Grammar representation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Production:
    """One production, ``lhs -> rhs``.

    Attributes:
        lhs: The nonterminal on the left-hand side.
        rhs: The right-hand side, as a tuple of symbols. An empty tuple is
            an epsilon production, ``lhs -> ε``.
        index: 1-based position of this production in the grammar it came
            from, used only for display (``"production 3"``, ``"r3"``).
    """

    lhs: str
    rhs: tuple[str, ...]
    index: int

    def __str__(self) -> str:
        """Render as ``lhs -> s1 s2 ...``, or ``lhs -> ε`` when rhs is empty."""
        body = " ".join(self.rhs) if self.rhs else EPSILON
        return f"{self.lhs} -> {body}"


@dataclass(frozen=True)
class Grammar:
    """A context-free grammar: its productions, vocabulary, and start symbol.

    Attributes:
        productions: Every production, in the order it was written.
        terminals: Every symbol that is never a production's left-hand side.
        nonterminals: Every symbol that is some production's left-hand side.
        start_symbol: The nonterminal every derivation begins from.
    """

    productions: tuple[Production, ...]
    terminals: frozenset[str]
    nonterminals: frozenset[str]
    start_symbol: str

    def productions_for(self, nonterminal: str) -> tuple[Production, ...]:
        """Return every production whose left-hand side is ``nonterminal``.

        Args:
            nonterminal: The left-hand side to filter on.

        Returns:
            The matching productions, in grammar order.
        """
        return tuple(p for p in self.productions if p.lhs == nonterminal)


def _split_alternatives(rhs_text: str) -> list[list[str]]:
    """Split one production's right-hand side text into its alternatives.

    Tokenizes on whitespace first, then starts a new alternative at every
    token that is *exactly* ``|`` -- never at a ``|`` that is merely part of
    a longer token, such as a grammar whose alphabet includes ``||`` or
    ``|=``. Splitting the raw string on the character ``|`` instead would
    cut such a token in half around an invisible empty alternative, which is
    the whole reason this is not simply ``rhs_text.split("|")``.

    Args:
        rhs_text: Everything after the arrow on one production line, still
            possibly holding several ``|``-separated alternatives.

    Returns:
        One list of symbols per alternative, in order. An alternative with
        no tokens (two ``|`` in a row, or none at all between the arrow and
        the first ``|``) comes back as an empty list.
    """
    alternatives: list[list[str]] = [[]]
    for token in rhs_text.split():
        if token == "|":
            alternatives.append([])
        else:
            alternatives[-1].append(token)
    return alternatives


def parse_grammar(text: str, start_symbol: str | None = None) -> Grammar:
    """Parse grammar text into a :class:`Grammar`.

    Accepts the format used throughout this project (and by
    :data:`app.samples.DEMO_GRAMMAR`): one or more lines of
    ``lhs -> rhs1 | rhs2 | ...``, symbols separated by whitespace, ``->`` or
    ``→`` as the arrow, ``#`` starting a trailing comment, and an alternative
    that is empty, or spelled ``ε``/``eps``/``epsilon`` (case-insensitive),
    taken as an epsilon production. A nonterminal is exactly a symbol that
    appears as some production's left-hand side; every other symbol
    mentioned is a terminal -- there is no separate terminal declaration.

    Alternatives are split on ``|`` only where it stands alone as its own
    whitespace-separated token, not wherever the character occurs -- so a
    grammar whose own alphabet includes a token spelled ``||`` (MiniLang's
    logical-or operator, say) is read as that one symbol, never silently
    cut into two empty alternatives around an invisible seam.

    A production with many alternatives may be wrapped across several
    lines, as long as every continuation line starts with ``|`` -- that
    leading pipe is what marks it as "more alternatives for the production
    above", rather than a new left-hand side missing its arrow.

    Args:
        text: The grammar source text.
        start_symbol: The start symbol, if not the left-hand side of the
            first production.

    Returns:
        The parsed grammar.

    Raises:
        GrammarError: The text has no productions, a line has no arrow and
            is not a ``|``-led continuation of the previous one, a line's
            left-hand side is empty, or the resolved start symbol is not
            the left-hand side of any production.
    """
    logical_lines: list[str] = []
    current: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("|") and current:
            current.append(line)
            continue
        if current:
            logical_lines.append(" ".join(current))
        current = [line]
    if current:
        logical_lines.append(" ".join(current))

    raw: list[tuple[str, tuple[str, ...]]] = []
    for line in logical_lines:
        parts = _ARROW_PATTERN.split(line, maxsplit=1)
        if len(parts) != 2:
            raise GrammarError(f"missing '->' in production line: {line!r}")
        lhs = parts[0].strip()
        if not lhs:
            raise GrammarError(f"empty left-hand side in line: {line!r}")
        for symbols in _split_alternatives(parts[1]):
            if not symbols or (len(symbols) == 1 and symbols[0].lower() in _EPSILON_SPELLINGS):
                raw.append((lhs, ()))
            else:
                raw.append((lhs, tuple(symbols)))

    if not raw:
        raise GrammarError("grammar text contains no productions")

    nonterminals = frozenset(lhs for lhs, _ in raw)
    terminals = frozenset(sym for _, rhs in raw for sym in rhs if sym not in nonterminals)
    resolved_start = start_symbol or raw[0][0]
    if resolved_start not in nonterminals:
        raise GrammarError(
            f"start symbol {resolved_start!r} is not the left-hand side of any production"
        )

    productions = tuple(
        Production(lhs=lhs, rhs=rhs, index=i) for i, (lhs, rhs) in enumerate(raw, start=1)
    )
    return Grammar(
        productions=productions,
        terminals=terminals,
        nonterminals=nonterminals,
        start_symbol=resolved_start,
    )


def _augment(grammar: Grammar) -> tuple[Grammar, Production]:
    """Return ``grammar`` with a fresh start production ``S' -> S`` prepended.

    Every LR construction needs this: the accepting condition is "the item
    [S' -> S ., $] is reachable", and S' must not be any symbol already in
    the grammar, so the augmented start symbol is ``grammar.start_symbol``
    with trailing primes appended until it is unique (section 4.6.2).

    Args:
        grammar: The grammar to augment.

    Returns:
        A tuple of the augmented grammar and its new start production.
    """
    new_start = grammar.start_symbol + "'"
    while new_start in grammar.nonterminals or new_start in grammar.terminals:
        new_start += "'"
    augmented_production = Production(lhs=new_start, rhs=(grammar.start_symbol,), index=0)
    augmented_grammar = Grammar(
        productions=(augmented_production, *grammar.productions),
        terminals=grammar.terminals,
        nonterminals=grammar.nonterminals | {new_start},
        start_symbol=new_start,
    )
    return augmented_grammar, augmented_production


# ---------------------------------------------------------------------------
# FIRST and FOLLOW (section 4.4.2)
# ---------------------------------------------------------------------------


def compute_first(grammar: Grammar) -> dict[str, frozenset[str]]:
    """Compute FIRST(X) for every grammar symbol X.

    The three rules, applied to a fixpoint exactly as the book states them:
    FIRST of a terminal is itself; if ``A -> ε`` then ``ε`` is in FIRST(A);
    and for ``A -> Y1 Y2 ... Yk``, FIRST(A) gains FIRST(Yi) - {ε} for the
    first ``i`` whose predecessors Y1..Yi-1 can all derive ε, with ``ε``
    itself added to FIRST(A) only if *every* Yi can derive ε.

    Args:
        grammar: The grammar to compute FIRST sets for.

    Returns:
        A mapping from every terminal and nonterminal to its FIRST set.
    """
    first: dict[str, set[str]] = {terminal: {terminal} for terminal in grammar.terminals}
    for nonterminal in grammar.nonterminals:
        first.setdefault(nonterminal, set())

    changed = True
    while changed:
        changed = False
        for production in grammar.productions:
            target = first[production.lhs]
            if not production.rhs:
                if EPSILON not in target:
                    target.add(EPSILON)
                    changed = True
                continue
            nullable_prefix = True
            for symbol in production.rhs:
                symbol_first = first[symbol]
                added = symbol_first - {EPSILON} - target
                if added:
                    target |= added
                    changed = True
                if EPSILON not in symbol_first:
                    nullable_prefix = False
                    break
            if nullable_prefix and EPSILON not in target:
                target.add(EPSILON)
                changed = True

    return {symbol: frozenset(members) for symbol, members in first.items()}


def first_of_sequence(
    symbols: Sequence[str], first_sets: Mapping[str, frozenset[str]]
) -> frozenset[str]:
    """Compute FIRST of a string of grammar symbols.

    Used for FOLLOW and for LR(1) lookahead sets, both of which need FIRST
    of more than one symbol. A symbol absent from ``first_sets`` -- in practice, only the end-marker
    ``$`` turning up as an LR(1) lookahead -- is treated as its own FIRST
    set, ``{symbol}``, which is the standard way the book treats ``$`` as if
    it were a terminal for this purpose (section 4.4.2's remark, used again
    in section 4.7.1).

    Args:
        symbols: The string of symbols, read left to right. An empty
            sequence's FIRST is ``{ε}``, since the empty string trivially
            derives itself.
        first_sets: FIRST sets for individual symbols, from
            :func:`compute_first`.

    Returns:
        FIRST of the whole string.
    """
    result: set[str] = set()
    for symbol in symbols:
        symbol_first = first_sets.get(symbol, frozenset({symbol}))
        result |= symbol_first - {EPSILON}
        if EPSILON not in symbol_first:
            return frozenset(result)
    result.add(EPSILON)
    return frozenset(result)


def compute_follow(
    grammar: Grammar, first_sets: Mapping[str, frozenset[str]]
) -> dict[str, frozenset[str]]:
    """Compute FOLLOW(A) for every nonterminal A.

    Seeded with ``$`` in FOLLOW(start symbol); then, for every production
    ``A -> αBβ``, FOLLOW(B) gains FIRST(β) - {ε}, and if β is empty or
    nullable, FOLLOW(B) also gains everything in FOLLOW(A) -- both rules
    applied to a fixpoint, exactly as the book states them.

    Args:
        grammar: The grammar to compute FOLLOW sets for.
        first_sets: FIRST sets for every symbol, from :func:`compute_first`.

    Returns:
        A mapping from every nonterminal to its FOLLOW set.
    """
    follow: dict[str, set[str]] = {nonterminal: set() for nonterminal in grammar.nonterminals}
    follow[grammar.start_symbol].add(END_MARKER)

    changed = True
    while changed:
        changed = False
        for production in grammar.productions:
            rhs = production.rhs
            for position, symbol in enumerate(rhs):
                if symbol not in grammar.nonterminals:
                    continue
                beta = rhs[position + 1 :]
                beta_first = first_of_sequence(beta, first_sets)
                before = len(follow[symbol])
                follow[symbol] |= beta_first - {EPSILON}
                if EPSILON in beta_first:
                    follow[symbol] |= follow[production.lhs]
                if len(follow[symbol]) != before:
                    changed = True

    return {symbol: frozenset(members) for symbol, members in follow.items()}


# ---------------------------------------------------------------------------
# A table-construction conflict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Conflict:
    """More than one action was assigned to a single table cell.

    Attributes:
        kind: ``"ll1"`` for two productions claiming the same LL(1) cell,
            ``"shift-reduce"`` or ``"reduce-reduce"`` for an LR table cell.
        state: The LR automaton state the conflict occurred in, or ``None``
            for an LL(1) conflict (which has no notion of state).
        symbol: The terminal the conflicting cell is keyed on.
        detail: A human-readable description of what collided.
    """

    kind: Literal["ll1", "shift-reduce", "reduce-reduce"]
    state: int | None
    symbol: str
    detail: str


# ---------------------------------------------------------------------------
# LL(1) table (Algorithm 4.6, section 4.4.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ll1Table:
    """The LL(1) parsing table, M[nonterminal, terminal].

    Attributes:
        table: Every non-empty cell, keyed by ``(nonterminal, terminal)``.
            A cell's value is the productions assigned to it -- normally
            exactly one; more than one means that cell is a conflict.
        conflicts: Every cell with more than one production.
    """

    table: dict[tuple[str, str], tuple[Production, ...]]
    conflicts: tuple[Conflict, ...]

    @property
    def is_ll1(self) -> bool:
        """Whether the grammar is LL(1): every cell has at most one entry."""
        return not self.conflicts


def build_ll1_table(
    grammar: Grammar,
    first_sets: Mapping[str, frozenset[str]],
    follow_sets: Mapping[str, frozenset[str]],
) -> Ll1Table:
    """Build the LL(1) parsing table (Algorithm 4.6).

    For each production ``A -> α``: add it to M[A, a] for every terminal a
    in FIRST(α); and if ε is in FIRST(α), add it to M[A, b] for every
    terminal b in FOLLOW(A) -- which, because FOLLOW(start symbol) always
    contains ``$``, naturally covers the "use ``$``, too, if ``$`` is in
    FOLLOW(A)" clause without a separate case.

    Args:
        grammar: The grammar to build a table for.
        first_sets: FIRST sets from :func:`compute_first`.
        follow_sets: FOLLOW sets from :func:`compute_follow`.

    Returns:
        The table, with every conflicting cell recorded.
    """
    cells: dict[tuple[str, str], list[Production]] = {}

    def add(nonterminal: str, terminal: str, production: Production) -> None:
        cells.setdefault((nonterminal, terminal), []).append(production)

    for production in grammar.productions:
        alpha_first = first_of_sequence(production.rhs, first_sets)
        for terminal in alpha_first - {EPSILON}:
            add(production.lhs, terminal, production)
        if EPSILON in alpha_first:
            for terminal in follow_sets[production.lhs]:
                add(production.lhs, terminal, production)

    table: dict[tuple[str, str], tuple[Production, ...]] = {}
    conflicts: list[Conflict] = []
    for (nonterminal, terminal), productions in cells.items():
        table[(nonterminal, terminal)] = tuple(productions)
        if len(productions) > 1:
            detail = f"{nonterminal}, {terminal}: " + "; ".join(str(p) for p in productions)
            conflicts.append(Conflict(kind="ll1", state=None, symbol=terminal, detail=detail))

    return Ll1Table(table=table, conflicts=tuple(conflicts))


# ---------------------------------------------------------------------------
# LR(0) items and the canonical collection (section 4.6.2, Algorithm 4.9)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    """An LR(0) item: a production with a dot somewhere in its right side.

    Attributes:
        production: The underlying production.
        dot: Index of the dot within ``production.rhs``, from ``0`` (nothing
            read yet) to ``len(production.rhs)`` (the item is complete).
    """

    production: Production
    dot: int

    @property
    def is_complete(self) -> bool:
        """Whether the dot has reached the end of the right-hand side."""
        return self.dot >= len(self.production.rhs)

    @property
    def symbol_after_dot(self) -> str | None:
        """The symbol immediately after the dot, or ``None`` if complete."""
        rhs = self.production.rhs
        return rhs[self.dot] if self.dot < len(rhs) else None

    def advanced(self) -> Item:
        """Return this item with the dot moved one symbol to the right."""
        return Item(self.production, self.dot + 1)

    def __str__(self) -> str:
        """Render as ``lhs -> before . after``."""
        rhs = self.production.rhs
        if not rhs:
            return f"{self.production.lhs} -> ."
        before = " ".join(rhs[: self.dot])
        after = " ".join(rhs[self.dot :])
        return f"{self.production.lhs} -> {before} . {after}".replace("  ", " ").rstrip()


def closure0(items: frozenset[Item], grammar: Grammar) -> frozenset[Item]:
    """CLOSURE of a set of LR(0) items (Algorithm 4.9's closure step).

    For every item ``A -> α.Bβ`` in the set with B a nonterminal, add
    ``B -> .γ`` for every production ``B -> γ``, repeated to a fixpoint.

    Args:
        items: The items to close.
        grammar: The (augmented) grammar the items' productions belong to.

    Returns:
        The closed item set.
    """
    result = set(items)
    changed = True
    while changed:
        changed = False
        for item in list(result):
            symbol = item.symbol_after_dot
            if symbol is not None and symbol in grammar.nonterminals:
                for production in grammar.productions_for(symbol):
                    new_item = Item(production, 0)
                    if new_item not in result:
                        result.add(new_item)
                        changed = True
    return frozenset(result)


def goto0(items: frozenset[Item], symbol: str, grammar: Grammar) -> frozenset[Item]:
    """GOTO of an item set on a grammar symbol (Algorithm 4.9's goto step).

    Args:
        items: The source item set.
        symbol: The symbol to transition on.
        grammar: The (augmented) grammar the items' productions belong to.

    Returns:
        The closure of every item advanced past ``symbol``, or the empty
        set if no item in ``items`` has ``symbol`` after its dot.
    """
    moved = {item.advanced() for item in items if item.symbol_after_dot == symbol}
    if not moved:
        return frozenset()
    return closure0(frozenset(moved), grammar)


@dataclass(frozen=True)
class LrState:
    """One state of an LR automaton: a number and the item set it holds.

    Attributes:
        index: The state's number (0 is always the start state).
        items: The (closed) item set this state holds. For an LR(0)/SLR
            automaton these are :class:`Item`; for a merged LALR automaton
            these are also :class:`Item` -- the *cores* left after LR(1)
            states sharing a core are merged (see :func:`build_lalr_table`).
    """

    index: int
    items: frozenset[Item]


@dataclass(frozen=True)
class Automaton:
    """A canonical collection of LR item sets, plus its transition table.

    Attributes:
        states: Every state, indexed by :attr:`LrState.index`.
        transitions: ``(state index, symbol) -> target state index``, for
            both terminals (shifts) and nonterminals (gotos) -- which one a
            given entry is depends only on whether ``symbol`` is a terminal
            or a nonterminal in :attr:`grammar`.
        grammar: The *augmented* grammar (with ``S' -> S`` prepended) this
            automaton was built over.
        augmented_production: The ``S' -> S`` production, so the accepting
            item ``[S' -> S., $]`` can be recognised without a string
            comparison.
    """

    states: tuple[LrState, ...]
    transitions: dict[tuple[int, str], int]
    grammar: Grammar
    augmented_production: Production


def _canonical_collection(
    start_items: frozenset[Item],
    grammar: Grammar,
    goto: Callable[[frozenset[Item], str, Grammar], frozenset[Item]],
) -> tuple[tuple[frozenset[Item], ...], dict[tuple[int, str], int]]:
    """Breadth-first construction shared by the LR(0) and LR(1) collections.

    Symbols are tried in a fixed, sorted order at every state, so that
    running this twice on the same grammar always numbers the states
    identically -- state numbers are otherwise an implementation detail of
    discovery order, not something the book's algorithm itself specifies.

    Args:
        start_items: The closed item set for state 0.
        grammar: The augmented grammar.
        goto: Either :func:`goto0` or an LR(1)-item-set-shaped equivalent.

    Returns:
        The states in discovery order, and the transition table between
        them.
    """
    states: list[frozenset[Item]] = [start_items]
    index_of: dict[frozenset[Item], int] = {start_items: 0}
    transitions: dict[tuple[int, str], int] = {}
    symbols = sorted(grammar.terminals | grammar.nonterminals)

    worklist = [0]
    while worklist:
        state_index = worklist.pop(0)
        current = states[state_index]
        for symbol in symbols:
            target = goto(current, symbol, grammar)
            if not target:
                continue
            if target not in index_of:
                index_of[target] = len(states)
                states.append(target)
                worklist.append(index_of[target])
            transitions[(state_index, symbol)] = index_of[target]

    return tuple(states), transitions


def build_lr0_automaton(grammar: Grammar) -> Automaton:
    """Build the canonical collection of LR(0) item sets (Algorithm 4.9).

    Args:
        grammar: The grammar to build the collection for (not yet
            augmented -- this function augments it).

    Returns:
        The LR(0) automaton.
    """
    augmented_grammar, augmented_production = _augment(grammar)
    start_items = closure0(frozenset({Item(augmented_production, 0)}), augmented_grammar)
    states, transitions = _canonical_collection(start_items, augmented_grammar, goto0)
    lr_states = tuple(LrState(index=i, items=items) for i, items in enumerate(states))
    return Automaton(
        states=lr_states,
        transitions=transitions,
        grammar=augmented_grammar,
        augmented_production=augmented_production,
    )


# ---------------------------------------------------------------------------
# ACTION/GOTO table construction shared by SLR and LALR (section 4.7)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Action:
    """One ACTION table cell: shift, reduce, or accept.

    Attributes:
        kind: Which of the three this is.
        target: The state to shift to (``kind == "shift"`` only).
        production: The production to reduce by (``kind == "reduce"`` only).
    """

    kind: Literal["shift", "reduce", "accept"]
    target: int | None = None
    production: Production | None = None


@dataclass(frozen=True)
class LrTable:
    """An SLR(1) or LALR(1) ACTION/GOTO table.

    Attributes:
        automaton: The LR automaton this table was built over.
        action: ``(state, terminal) -> actions``. Normally one action; more
            than one means that cell is a conflict.
        goto: ``(state, nonterminal) -> target state``.
        conflicts: Every ACTION cell with more than one action.
    """

    automaton: Automaton
    action: dict[tuple[int, str], tuple[Action, ...]]
    goto: dict[tuple[int, str], int]
    conflicts: tuple[Conflict, ...]

    @property
    def is_conflict_free(self) -> bool:
        """Whether every ACTION cell has at most one action."""
        return not self.conflicts


def _conflict_kind(first: Action, second: Action) -> Literal["shift-reduce", "reduce-reduce"]:
    """Classify a clash between two already-distinct actions in one cell."""
    kinds = {first.kind, second.kind}
    return "shift-reduce" if kinds == {"shift", "reduce"} else "reduce-reduce"


def _build_table(
    automaton: Automaton, reduce_lookaheads: Callable[[int, Item], frozenset[str]]
) -> LrTable:
    """Build an ACTION/GOTO table (Algorithm 4.10 for SLR, 4.11 for LALR).

    The two algorithms differ only in how a reduce item's valid lookaheads
    are decided -- SLR uses the production's global FOLLOW set; LALR uses
    the lookaheads attached to that specific state (see
    :func:`build_slr_table` and :func:`build_lalr_table`). Everything else
    -- when to shift, when to accept, how GOTO is filled in, how a clash is
    recorded as a conflict -- is identical, so it lives here once.

    Args:
        automaton: The LR automaton (LR(0)-based for SLR, merged-LR(1)-based
            for LALR) to build the table over.
        reduce_lookaheads: Given a state index and a complete item in that
            state, the terminals a reduce by that item's production is valid
            under.

    Returns:
        The constructed table.
    """
    action: dict[tuple[int, str], list[Action]] = {}
    goto: dict[tuple[int, str], int] = {}
    conflicts: list[Conflict] = []

    def add_action(state: int, symbol: str, new_action: Action, detail: str) -> None:
        bucket = action.setdefault((state, symbol), [])
        if bucket and bucket[0] != new_action:
            conflicts.append(
                Conflict(
                    kind=_conflict_kind(bucket[0], new_action),
                    state=state,
                    symbol=symbol,
                    detail=detail,
                )
            )
        if new_action not in bucket:
            bucket.append(new_action)

    for lr_state in automaton.states:
        for item in lr_state.items:
            symbol = item.symbol_after_dot
            if symbol is not None and symbol in automaton.grammar.terminals:
                target = automaton.transitions.get((lr_state.index, symbol))
                if target is not None:
                    add_action(
                        lr_state.index,
                        symbol,
                        Action(kind="shift", target=target),
                        f"shift to state {target} on '{symbol}' (item {item})",
                    )
            elif item.is_complete:
                if item.production == automaton.augmented_production:
                    add_action(
                        lr_state.index,
                        END_MARKER,
                        Action(kind="accept"),
                        "accept: the augmented start production is complete",
                    )
                else:
                    for lookahead in reduce_lookaheads(lr_state.index, item):
                        add_action(
                            lr_state.index,
                            lookahead,
                            Action(kind="reduce", production=item.production),
                            f"reduce by {item.production} (item {item})",
                        )
        for symbol in automaton.grammar.nonterminals:
            target = automaton.transitions.get((lr_state.index, symbol))
            if target is not None:
                goto[(lr_state.index, symbol)] = target

    return LrTable(
        automaton=automaton,
        action={key: tuple(value) for key, value in action.items()},
        goto=goto,
        conflicts=tuple(conflicts),
    )


def build_slr_table(automaton: Automaton, follow_sets: Mapping[str, frozenset[str]]) -> LrTable:
    """Build the SLR(1) table (Algorithm 4.10, section 4.7.2).

    A complete item ``[A -> α.]`` becomes a reduce action for every terminal
    in FOLLOW(A) -- the "simple" LR table, named for using the already-global
    FOLLOW sets rather than a lookahead computed per state.

    Args:
        automaton: An LR(0) automaton from :func:`build_lr0_automaton`.
        follow_sets: FOLLOW sets from :func:`compute_follow`.

    Returns:
        The SLR(1) table.
    """

    def lookaheads(_state: int, item: Item) -> frozenset[str]:
        return follow_sets[item.production.lhs]

    return _build_table(automaton, lookaheads)


# ---------------------------------------------------------------------------
# Canonical LR(1) items and LALR(1) (section 4.7.1, 4.7.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Lr1Item:
    """An LR(1) item: an LR(0) item plus one lookahead terminal.

    Attributes:
        production: The underlying production.
        dot: Index of the dot, as in :class:`Item`.
        lookahead: The single terminal (or ``$``) this item is valid under.
    """

    production: Production
    dot: int
    lookahead: str

    @property
    def core(self) -> Item:
        """The LR(0) item underneath, with the lookahead stripped off."""
        return Item(self.production, self.dot)


def closure1(
    items: frozenset[Lr1Item], grammar: Grammar, first_sets: Mapping[str, frozenset[str]]
) -> frozenset[Lr1Item]:
    """CLOSURE of a set of LR(1) items (section 4.7.1's closure step).

    For every item ``[A -> α.Bβ, a]`` with B a nonterminal, add
    ``[B -> .γ, b]`` for every production ``B -> γ`` and every terminal b in
    FIRST(βa) -- the lookahead propagation that is the entire difference
    from the LR(0) closure, and what gives LALR its sharper lookaheads.

    Args:
        items: The LR(1) items to close.
        grammar: The augmented grammar.
        first_sets: FIRST sets for the grammar's own symbols (not the
            augmented start symbol, which never appears on a right-hand
            side and so is never looked up here).

    Returns:
        The closed LR(1) item set.
    """
    result = set(items)
    changed = True
    while changed:
        changed = False
        for item in list(result):
            symbol = item.core.symbol_after_dot
            if symbol is not None and symbol in grammar.nonterminals:
                beta = item.production.rhs[item.dot + 1 :]
                lookaheads = first_of_sequence((*beta, item.lookahead), first_sets)
                for production in grammar.productions_for(symbol):
                    for lookahead in lookaheads:
                        new_item = Lr1Item(production, 0, lookahead)
                        if new_item not in result:
                            result.add(new_item)
                            changed = True
    return frozenset(result)


def goto1(
    items: frozenset[Lr1Item],
    symbol: str,
    grammar: Grammar,
    first_sets: Mapping[str, frozenset[str]],
) -> frozenset[Lr1Item]:
    """GOTO of an LR(1) item set on a grammar symbol.

    Args:
        items: The source LR(1) item set.
        symbol: The symbol to transition on.
        grammar: The augmented grammar.
        first_sets: FIRST sets, passed through to :func:`closure1`.

    Returns:
        The closure of every item advanced past ``symbol``, lookaheads
        carried over unchanged, or the empty set if none applies.
    """
    moved = {
        Lr1Item(item.production, item.dot + 1, item.lookahead)
        for item in items
        if item.core.symbol_after_dot == symbol
    }
    if not moved:
        return frozenset()
    return closure1(frozenset(moved), grammar, first_sets)


def _lr1_collection(
    grammar: Grammar, first_sets: Mapping[str, frozenset[str]]
) -> tuple[tuple[frozenset[Lr1Item], ...], dict[tuple[int, str], int], Grammar, Production]:
    """Canonical collection of LR(1) item sets.

    The LR(1) analogue of :func:`build_lr0_automaton`, kept separate because
    an :class:`Lr1Item` is not an :class:`Item`.

    Args:
        grammar: The grammar to build the collection for (not yet
            augmented).
        first_sets: FIRST sets from :func:`compute_first`.

    Returns:
        The states in discovery order, the transition table between them,
        the augmented grammar, and its augmented start production.
    """
    augmented_grammar, augmented_production = _augment(grammar)
    start_items = closure1(
        frozenset({Lr1Item(augmented_production, 0, END_MARKER)}), augmented_grammar, first_sets
    )

    states: list[frozenset[Lr1Item]] = [start_items]
    index_of: dict[frozenset[Lr1Item], int] = {start_items: 0}
    transitions: dict[tuple[int, str], int] = {}
    symbols = sorted(augmented_grammar.terminals | augmented_grammar.nonterminals)

    worklist = [0]
    while worklist:
        state_index = worklist.pop(0)
        current = states[state_index]
        for symbol in symbols:
            target = goto1(current, symbol, augmented_grammar, first_sets)
            if not target:
                continue
            if target not in index_of:
                index_of[target] = len(states)
                states.append(target)
                worklist.append(index_of[target])
            transitions[(state_index, symbol)] = index_of[target]

    return tuple(states), transitions, augmented_grammar, augmented_production


def _merge_to_lalr(
    lr1_states: Sequence[frozenset[Lr1Item]], lr1_transitions: Mapping[tuple[int, str], int]
) -> tuple[tuple[LrState, ...], dict[tuple[int, str], int], dict[tuple[int, Item], frozenset[str]]]:
    """Merge LR(1) states sharing a core into LALR states (section 4.7.3).

    Two LR(1) states have the same *core* when stripping the lookahead off
    every item yields the same set of LR(0) items. The book proves this
    merge is always safe -- states with the same core always have identical
    GOTO structure -- which is what guarantees every old transition maps
    onto exactly one new one below, never a contradiction.

    Args:
        lr1_states: The canonical LR(1) collection, in discovery order.
        lr1_transitions: The LR(1) collection's transition table.

    Returns:
        The merged states, their transition table, and a lookahead map from
        ``(merged state index, item core)`` to the union of lookaheads every
        LR(1) item that collapsed onto it carried.
    """
    core_index: dict[frozenset[Item], int] = {}
    cores: list[frozenset[Item]] = []
    old_to_new: dict[int, int] = {}

    for old_index, state_items in enumerate(lr1_states):
        core = frozenset(item.core for item in state_items)
        if core not in core_index:
            core_index[core] = len(cores)
            cores.append(core)
        old_to_new[old_index] = core_index[core]

    lookaheads: dict[tuple[int, Item], set[str]] = {}
    for old_index, state_items in enumerate(lr1_states):
        new_index = old_to_new[old_index]
        for item in state_items:
            lookaheads.setdefault((new_index, item.core), set()).add(item.lookahead)

    merged_states = tuple(LrState(index=i, items=core) for i, core in enumerate(cores))
    merged_transitions = {
        (old_to_new[old_index], symbol): old_to_new[target]
        for (old_index, symbol), target in lr1_transitions.items()
    }
    frozen_lookaheads = {key: frozenset(value) for key, value in lookaheads.items()}
    return merged_states, merged_transitions, frozen_lookaheads


def build_lalr_table(
    grammar: Grammar, first_sets: Mapping[str, frozenset[str]]
) -> tuple[Automaton, LrTable]:
    """Build the LALR(1) automaton and table (section 4.7.3).

    Builds the canonical LR(1) collection, merges states sharing a core, and
    builds the table from each merged state's own lookaheads -- sharper than
    SLR's global FOLLOW sets, which is precisely what lets LALR accept some
    grammars SLR rejects (see the module docstring's worked example).

    Args:
        grammar: The grammar to build a table for (not yet augmented).
        first_sets: FIRST sets from :func:`compute_first`. LALR needs no
            FOLLOW sets at all -- every lookahead it uses comes from the
            LR(1) construction itself.

    Returns:
        A tuple of the merged LALR automaton (useful on its own for
        displaying the item sets) and the ACTION/GOTO table built from it.
    """
    lr1_states, lr1_transitions, augmented_grammar, augmented_production = _lr1_collection(
        grammar, first_sets
    )
    merged_states, merged_transitions, lookaheads = _merge_to_lalr(lr1_states, lr1_transitions)
    automaton = Automaton(
        states=merged_states,
        transitions=merged_transitions,
        grammar=augmented_grammar,
        augmented_production=augmented_production,
    )

    def reduce_lookaheads(state: int, item: Item) -> frozenset[str]:
        return lookaheads.get((state, item), frozenset())

    table = _build_table(automaton, reduce_lookaheads)
    return automaton, table


# ---------------------------------------------------------------------------
# Driving a table over real input (Algorithm 4.7, section 4.6.1) -- used as
# a test oracle, not by any other part of the application.
# ---------------------------------------------------------------------------


def run_lr_parse(table: LrTable, tokens: Sequence[str]) -> bool:
    """Drive an SLR(1)/LALR(1) ACTION/GOTO table over a token string.

    This is "a model of an LR parser" (Algorithm 4.7): a state stack
    starting at state 0, repeatedly shifting the next input symbol and a new
    state, or reducing by popping as many states as the chosen production's
    right-hand side is long and pushing the GOTO of the state beneath that
    on the production's left-hand side, until either an accept action is
    reached (success) or no action applies (failure).

    Args:
        table: The table to drive, from :func:`build_slr_table` or
            :func:`build_lalr_table`.
        tokens: The input string's terminals, without a trailing ``$`` --
            one is appended here.

    Returns:
        ``True`` if the input is accepted, ``False`` on any error. A cell
        with a recorded conflict uses its first action, since this driver
        exists to check unambiguous tables, not to resolve conflicts.
    """
    input_symbols = [*tokens, END_MARKER]
    state_stack: list[int] = [0]
    position = 0
    steps = 0
    max_steps = 10_000

    while steps < max_steps:
        steps += 1
        state = state_stack[-1]
        lookahead = input_symbols[position]
        actions = table.action.get((state, lookahead))
        if not actions:
            return False
        chosen = actions[0]

        if chosen.kind == "accept":
            return True
        if chosen.kind == "shift":
            state_stack.append(chosen.target)  # type: ignore[arg-type]
            position += 1
            continue

        production = chosen.production
        assert production is not None  # a "reduce" action always carries one
        for _ in range(len(production.rhs)):
            state_stack.pop()
        goto_state = table.goto.get((state_stack[-1], production.lhs))
        if goto_state is None:
            return False
        state_stack.append(goto_state)

    return False


# ---------------------------------------------------------------------------
# One call, every result -- the convenience entry point
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GrammarAnalysis:
    """Everything the Parser Theory Lab needs for one grammar, bundled.

    Attributes:
        grammar: The parsed grammar.
        first: FIRST sets.
        follow: FOLLOW sets.
        ll1: The LL(1) table.
        lr0: The LR(0) automaton (its item sets are what a "canonical
            collection of LR(0) items" display would draw).
        slr: The SLR(1) table.
        lalr_automaton: The merged LALR automaton.
        lalr: The LALR(1) table.
    """

    grammar: Grammar
    first: dict[str, frozenset[str]]
    follow: dict[str, frozenset[str]]
    ll1: Ll1Table
    lr0: Automaton
    slr: LrTable
    lalr_automaton: Automaton
    lalr: LrTable


def analyse(text: str, start_symbol: str | None = None) -> GrammarAnalysis:
    """Run every table construction in this module over one grammar.

    Args:
        text: Grammar source text, in the format :func:`parse_grammar`
            accepts.
        start_symbol: The start symbol, if not the left-hand side of the
            first production.

    Returns:
        Every intermediate and final result: the parsed grammar, FIRST and
        FOLLOW sets, and the LL(1), SLR(1) and LALR(1) tables with their
        conflicts.
    """
    grammar = parse_grammar(text, start_symbol)
    first = compute_first(grammar)
    follow = compute_follow(grammar, first)
    ll1 = build_ll1_table(grammar, first, follow)
    lr0 = build_lr0_automaton(grammar)
    slr = build_slr_table(lr0, follow)
    lalr_automaton, lalr = build_lalr_table(grammar, first)
    return GrammarAnalysis(
        grammar=grammar,
        first=first,
        follow=follow,
        ll1=ll1,
        lr0=lr0,
        slr=slr,
        lalr_automaton=lalr_automaton,
        lalr=lalr,
    )
