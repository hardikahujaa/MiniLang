"""Panic-mode syntax error recovery, with a ranked repair-suggestion engine.

Tier B's stated novelty feature: *"Most student parsers abort on the first
syntax error or emit a cascade of false ones; mine recovers, suggests the
fix, and reports only genuine errors"* (the plan, section 7). This module
adds that on top of ``app/parser.py`` without changing it at all -- Baseline
A, the control condition the project's benchmark measures this against, has
to keep behaving exactly as it always has for that comparison to mean
anything.

What is textbook, and what is this project's own design
-----------------------------------------------------------
**Panic-mode error recovery** is section 4.1.3 of Aho, Lam, Sethi & Ullman's
*Compilers: Principles, Techniques, and Tools* (2nd edition): on a syntax
error, discard input symbols until a *synchronizing* token is reached, then
resume parsing from there. That part -- :func:`RecoveringParser
._panic_skip_statement` and :func:`RecoveringParser._panic_skip_function` --
is a direct implementation of the book's description: if the cursor is
already sitting on a synchronizing token, nothing is discarded at all;
otherwise at least one genuinely bad token is discarded before the next
check. Either way this provably terminates (it cannot loop forever re-trying
the same failing parse at the same position), checked directly for both
cases in ``tests/test_errors.py``.

**The ranking engine is not from the book.** The book describes panic mode
in general terms and leaves "which synchronizing set, and what to tell the
user" to the implementer. The specific formula here -- Levenshtein distance
plus a small table of common-mistake bonuses plus a line-position
heuristic -- is this project's own engineering layer on top, exactly as the
plan frames it as the project's novelty contribution rather than a textbook
algorithm. It is still held to the same bar as everything else in this
project: every claim about it is a test, not an assertion.

Why ``app/parser.py`` needs no changes at all
------------------------------------------------
Every grammar-rule method in :class:`~app.parser.Parser` calls the next one
through ``self.``, never through the class name or a bare function --
ordinary Python virtual dispatch. :class:`RecoveringParser` overrides only
``_program`` and ``_block``, the two methods with their own ``while`` loop
over a sequence of declarations or statements; every other method is
inherited completely unmodified, and a subclass overriding those two
transparently intercepts every call site inside them. A ``ParseError``
raised while parsing something nested inside an ``if``/``while`` body
propagates up through the ordinary (unmodified) call chain to the *nearest*
enclosing ``_block``'s overridden loop -- "resume at the enclosing statement
boundary" falls out of normal exception propagation, not extra bookkeeping.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from app.ast_nodes import Block, FuncDecl, Program, Stmt
from app.lexer import Token, TokenType
from app.parser import TYPE_TOKENS, ParseError, Parser, describe
from app.schemas import Suggestion, SyntaxErrorReport

#: Tokens that resynchronise a panic-mode skip at the statement level: every
#: statement-starting keyword, plus the block terminator (so a skip that
#: reaches the end of its own block stops there rather than consuming the
#: brace and running into the next statement's territory). ``;`` is checked
#: separately, since it ends the statement the skip is trying to get past.
_STATEMENT_SYNC: Final[frozenset[TokenType]] = TYPE_TOKENS | frozenset(
    {TokenType.IF, TokenType.WHILE, TokenType.RETURN, TokenType.PRINT, TokenType.RBRACE}
)

#: Default number of tokens within which a second error is treated as a
#: cascade of the first one, rather than a genuine second mistake, and
#: suppressed. Taken directly from the plan's own specification (section 7).
_DEFAULT_SUPPRESS_WINDOW: Final[int] = 2


# ---------------------------------------------------------------------------
# Levenshtein distance (Wagner-Fischer dynamic programming)
# ---------------------------------------------------------------------------


def levenshtein(a: str, b: str) -> int:
    """Return the Levenshtein edit distance between two strings.

    The classical Wagner-Fischer dynamic-programming formulation: minimum
    number of single-character insertions, deletions or substitutions to
    turn ``a`` into ``b``. Plain Levenshtein, not Damerau-Levenshtein -- a
    single adjacent transposition (``whiel`` for ``while``) costs 2, not 1,
    which matters for anyone tuning the scoring weights below later.

    Args:
        a: The first string.
        b: The second string.

    Returns:
        The edit distance, 0 if the strings are equal.
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i] + [0] * len(b)
        for j, char_b in enumerate(b, start=1):
            cost = 0 if char_a == char_b else 1
            current[j] = min(
                previous[j] + 1,  # deletion from a
                current[j - 1] + 1,  # insertion into a
                previous[j - 1] + cost,  # substitution
            )
        previous = current
    return previous[-1]


def _similarity(a: str, b: str) -> float:
    """Normalise a Levenshtein distance into a [0, 1] similarity score."""
    longest = max(len(a), len(b), 1)
    return 1.0 - (levenshtein(a, b) / longest)


# ---------------------------------------------------------------------------
# Ranked suggestions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Candidate:
    """One scored repair suggestion before the top-3 are chosen."""

    text: str
    score: float
    rationale: str


def _found_text(found: Token) -> str:
    """Return the text to compare candidate spellings against."""
    return "" if found.type is TokenType.EOF else found.lexeme


def _previous_token(tokens: list[Token], found: Token) -> Token | None:
    """Return the token immediately before ``found`` in ``tokens``, if any."""
    for index, token in enumerate(tokens):
        if token is found:
            return tokens[index - 1] if index > 0 else None
    return None


def _common_mistake_bonus(
    token_type: TokenType, error: ParseError, tokens: list[Token]
) -> tuple[float, str] | None:
    """Check ``token_type`` against the fixed common-mistake table.

    Each entry here is a pattern this project chose to special-case,
    grounded in an actual, verified ``(expected, context, found)``
    combination ``app/parser.py`` produces -- not a guess at what a parser
    error "generally" might mean.

    Args:
        token_type: The candidate token being scored.
        error: The error being ranked.
        tokens: The full token stream, for looking at neighbouring tokens.

    Returns:
        A ``(bonus, rationale)`` pair if a pattern matched, else ``None``.
    """
    found = error.found

    if token_type in (TokenType.SEMICOLON, TokenType.RBRACE):
        previous = _previous_token(tokens, found)
        if previous is not None and found.line > previous.line:
            return 0.5, "likely missing at the end of the previous line"

    if token_type in (TokenType.RPAREN, TokenType.RBRACE) and found.type is TokenType.EOF:
        return 0.6, f"unbalanced '{describe(token_type)}'"

    if token_type in TYPE_TOKENS and found.type is TokenType.IDENT:
        return 0.3, "missing type keyword"

    if (
        token_type is TokenType.COLON
        and error.context == "function return type"
        and found.type is not TokenType.COLON
    ):
        return 0.4, "':' omitted before the return type"

    return None


def _assign_for_eq_candidate(error: ParseError) -> _Candidate | None:
    """Detect ``=`` written where ``==`` was meant inside a condition.

    Not derived from ``error.expected``: when this happens, the parser has
    already finished parsing ``x`` as a complete expression and is expecting
    the condition's closing ``)`` -- ``==`` never appears in ``expected`` at
    all, so this candidate has to be injected rather than scored from the
    expected set the way every other candidate is.

    Args:
        error: The error being ranked.

    Returns:
        A candidate suggesting ``==``, if this exact pattern matched.
    """
    if error.found.type is TokenType.ASSIGN and error.context in (
        "if condition",
        "while condition",
    ):
        return _Candidate(
            text="'==' instead of '='", score=0.95, rationale="'=' used where '==' was meant"
        )
    return None


def rank_suggestions(error: ParseError, tokens: list[Token]) -> list[Suggestion]:
    """Rank candidate repairs for a syntax error (plan section 7, step 2).

    For each token in ``error.expected``, scores it by similarity between
    its spelling and the token actually found (catches typos such as
    ``whiel`` for ``while``), plus a bonus from :func:`_common_mistake_bonus`
    when one of the project's hand-identified patterns matches. A handful of
    patterns that are not about any single expected token -- currently just
    ``=``/``==`` confusion -- are injected as extra candidates. When
    ``error.expected`` is empty (only ``Parser``'s nesting-depth guard
    raises with no expected set), the one sensible suggestion is to simplify
    the expression, since there is no token to propose inserting.

    Args:
        error: The error to rank suggestions for.
        tokens: The full token stream the error occurred in.

    Returns:
        The top 3 candidates, highest confidence first.
    """
    if not error.expected:
        return [
            Suggestion(
                text="simplify this expression",
                confidence=0.3,
                rationale="expression nesting is too deep to continue",
            )
        ]

    found_text = _found_text(error.found)
    candidates: list[_Candidate] = []

    for token_type in error.expected:
        spelling = describe(token_type)
        score = _similarity(spelling, found_text) if found_text else 0.0
        rationale = "similar spelling" if score > 0.0 else "expected here"
        bonus = _common_mistake_bonus(token_type, error, tokens)
        if bonus is not None:
            bonus_score, bonus_rationale = bonus
            score = max(score, bonus_score)
            rationale = bonus_rationale
        candidates.append(
            _Candidate(text=f"insert '{spelling}'", score=min(score, 1.0), rationale=rationale)
        )

    extra = _assign_for_eq_candidate(error)
    if extra is not None:
        candidates.append(extra)

    candidates.sort(key=lambda c: c.score, reverse=True)
    return [
        Suggestion(text=c.text, confidence=round(c.score, 2), rationale=c.rationale)
        for c in candidates[:3]
    ]


# ---------------------------------------------------------------------------
# Panic-mode recovery
# ---------------------------------------------------------------------------


class RecoveringParser(Parser):
    """A :class:`~app.parser.Parser` that recovers from syntax errors.

    Overrides only ``_program`` and ``_block`` -- see this module's
    docstring for why that alone is enough to intercept every statement and
    every top-level declaration without touching anything else in
    :class:`~app.parser.Parser`.
    """

    def __init__(
        self,
        tokens: list[Token],
        *,
        rank: bool = True,
        suppress: bool = True,
        suppress_window: int = _DEFAULT_SUPPRESS_WINDOW,
    ) -> None:
        """Prepare to parse and recover from a token stream.

        Args:
            tokens: Tokens to parse, terminated by an ``EOF`` token.
            rank: Whether to run the suggestion engine. ``False`` produces
                empty ``suggestions`` on every report -- this is Baseline
                B's "naive resume" mode.
            suppress: Whether to suppress an error reported within
                ``suppress_window`` tokens of the previous one. ``False`` is
                also part of Baseline B.
            suppress_window: How many tokens apart two errors must be to
                both be reported.
        """
        super().__init__(tokens)
        self.reports: list[SyntaxErrorReport] = []
        self._rank = rank
        self._suppress = suppress
        self._suppress_window = suppress_window
        self._last_reported_token_index: int | None = None

    def _panic_skip_statement(self) -> None:
        """Discard tokens until a statement boundary (plan section 7, step 3).

        Checks *before* discarding anything, not after: if the cursor
        already sits on a synchronizing token -- a stray ``;`` closing out
        the bad statement, or a token that starts a perfectly good one right
        after it (a type keyword, ``if``, the block's own closing ``}``) --
        there is nothing to discard, and discarding it anyway would eat the
        first token of whatever comes next for no reason. Only genuine
        garbage (a token that is itself neither a legal statement start nor
        ``;``) gets discarded, one token at a time, until a synchronizing
        token or the end of input is reached.

        Brace-depth aware: an error raised while parsing a construct's own
        header -- an ``if``/``while`` condition, most often -- leaves an
        unmatched ``{`` sitting in the remaining input, the one that was
        about to open that construct's body. Treating every ``}`` as a
        synchronizing token regardless of depth would let the discard walk
        straight through that unmatched ``{`` and stop on the *body's own*
        closing ``}`` (or a keyword inside the body that merely looks like a
        fresh statement start), handing control back one nesting level
        shallower than where the error actually happened -- silently
        dropping the rest of that body and corrupting everything after it.
        A local depth counter, incremented on ``{`` and decremented on
        ``}``, restricts the synchronizing-token check to depth 0: a brace
        opened during the discard must be closed again by the discard
        itself before anything is treated as a resumption point.

        Termination: every branch below either returns immediately or
        discards at least one real token before the loop can run again, so
        this cannot loop forever -- checked directly, for both the
        immediate-return case and the discard case, in
        ``tests/test_errors.py``.
        """
        if self._is_at_end():
            return
        if self._check(TokenType.SEMICOLON):
            self._advance()
            return
        if self._check_any(_STATEMENT_SYNC):
            return

        depth = 0
        if self._check(TokenType.LBRACE):
            depth += 1
        elif self._check(TokenType.RBRACE):
            depth -= 1
        self._advance()
        while not self._is_at_end():
            if depth == 0 and (
                self._check(TokenType.SEMICOLON) or self._check_any(_STATEMENT_SYNC)
            ):
                break
            if self._check(TokenType.LBRACE):
                depth += 1
            elif self._check(TokenType.RBRACE):
                depth -= 1
            self._advance()
        if self._check(TokenType.SEMICOLON):
            self._advance()

    def _panic_skip_function(self) -> None:
        """Discard tokens until the next top-level function declaration.

        Same "check before discarding" shape as :meth:`_panic_skip_statement`
        and for the same reason: if the cursor already sits on ``func``,
        there is a perfectly good declaration right there and nothing should
        be thrown away to reach it.
        """
        if self._is_at_end() or self._check(TokenType.FUNC):
            return
        self._advance()
        while not self._is_at_end() and not self._check(TokenType.FUNC):
            self._advance()

    def _record_and_recover(self, exc: ParseError, sync: Callable[[], None]) -> None:
        """Record one error (unless suppressed) and panic-mode skip past it.

        Args:
            exc: The error that was caught.
            sync: Which panic-mode skip to run -- statement- or
                function-level, depending on which loop caught the error.
        """
        error_token_index = self._pos
        suppressed = (
            self._suppress
            and self._last_reported_token_index is not None
            and error_token_index - self._last_reported_token_index <= self._suppress_window
        )

        sync()

        if suppressed:
            return

        suggestions = rank_suggestions(exc, self._tokens) if self._rank else []
        report = exc.to_api().model_copy(
            update={
                "recovered": True,
                "suggestions": suggestions,
                "suggestion": suggestions[0].text if suggestions else None,
                "confidence": suggestions[0].confidence if suggestions else 0.0,
            }
        )
        self.reports.append(report)
        self._last_reported_token_index = error_token_index

    def _program(self) -> Program:
        """Parse ``program -> funcDecl+``, recovering from a bad declaration.

        Identical to :meth:`Parser._program` except that each
        ``self._func_decl()`` call is wrapped to recover rather than abort --
        including the "no functions at all" case, which the base class
        raises before its loop even starts and so needs its own recovery
        rather than a try/except around a loop body. Recovering from it
        still means something useful here: a report is produced, and an
        (empty) :class:`~app.ast_nodes.Program` is returned rather than an
        exception propagating out of ``recover_parse``.
        """
        start = self._peek()
        functions: list[FuncDecl] = []

        if self._is_at_end():
            self._record_and_recover(
                self._error([TokenType.FUNC], "program"), self._panic_skip_function
            )
            return Program(functions=functions, line=start.line, column=start.col)

        while not self._is_at_end():
            try:
                functions.append(self._func_decl())
            except ParseError as exc:
                self._record_and_recover(exc, self._panic_skip_function)

        return Program(functions=functions, line=start.line, column=start.col)

    def _block(self) -> Block:
        """Parse ``block -> '{' stmt* '}'``, recovering from a bad statement.

        Identical to :meth:`Parser._block` except that each
        ``self._statement()`` call is wrapped to recover rather than abort.
        A missing opening or closing brace is not caught here -- there is no
        sensible "next statement" to resume from inside a block that never
        properly started or ended, so that error propagates to whichever
        enclosing loop can resynchronise instead.
        """
        opening = self._expect(TokenType.LBRACE, "block")
        statements: list[Stmt] = []

        while not self._check(TokenType.RBRACE) and not self._is_at_end():
            try:
                statements.append(self._statement())
            except ParseError as exc:
                self._record_and_recover(exc, self._panic_skip_statement)

        self._expect(TokenType.RBRACE, "block")
        return Block(statements=statements, line=opening.line, column=opening.col)


@dataclass(frozen=True)
class RecoveryResult:
    """The result of a recovering parse.

    Attributes:
        program: The parsed program, built from whatever the recovery
            engine could salvage. Never ``None`` -- even a program that is
            entirely malformed parses to an empty :class:`~app.ast_nodes.Program`,
            since ``_program``'s own loop keeps running past every error it
            catches.
        reports: Every syntax error found, each already carrying ranked
            suggestions (or empty ones, in Baseline B mode).
    """

    program: Program
    reports: list[SyntaxErrorReport]


def recover_parse(
    tokens: list[Token],
    *,
    rank: bool = True,
    suppress: bool = True,
) -> RecoveryResult:
    """Parse ``tokens``, recovering from every syntax error found.

    ``recover_parse(tokens, rank=False, suppress=False)`` *is* Baseline B
    from the plan's benchmark (section 7): panic-mode resynchronisation with
    neither ranked suggestions nor cascade suppression. There is
    deliberately no second implementation of that baseline -- it is this
    same engine with two features turned off, which is also what proves
    those two features are genuinely separable improvements rather than
    bundled together by construction.

    Args:
        tokens: Tokens to parse, terminated by an ``EOF`` token.
        rank: Whether to run the suggestion engine.
        suppress: Whether to suppress cascaded errors.

    Returns:
        The recovered program and every error report found.
    """
    parser = RecoveringParser(tokens, rank=rank, suppress=suppress)
    # Parser.parse() -- inherited unchanged -- is just `return self._program()`.
    # Ordinary virtual dispatch means that call already reaches
    # RecoveringParser's own overridden `_program`, so there is nothing to
    # override on `parse()` itself.
    program = parser.parse()
    return RecoveryResult(program=program, reports=parser.reports)
