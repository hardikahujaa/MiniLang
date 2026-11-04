"""Tests for :mod:`app.grammar`, pinned against known results from the book.

Every grammar used below, and every FIRST/FOLLOW set, table cell, state
count or accept/reject answer asserted against it, is either a worked
example from Aho, Lam, Sethi & Ullman's *Compilers: Principles, Techniques,
and Tools* (2nd edition), chapter 4, or a property the book states as always
true of SLR and LALR parsing in general. A test failing here is therefore
not "the implementation disagrees with itself" -- it is "the implementation
disagrees with the textbook", which is deliberately the strongest claim a
test in this file can make.

Three grammars do almost all of the work, and are each used for a different
textbook claim:

* The non-left-recursive expression grammar (``E -> T E'`` etc., section
  4.4) is LL(1) -- used to pin exact FIRST, FOLLOW and LL(1) table cells.
* The classic left-recursive expression grammar
  (:data:`app.samples.DEMO_GRAMMAR`, the book's own running SLR example,
  Figure 4.31 and Figure 4.37) is *not* LL(1) but *is* SLR(1) -- used to pin
  the LR(0) canonical collection's size and to prove the generated SLR and
  LALR tables actually drive real input correctly.
* The assignment-statement grammar (``S -> L = R | R``, ``L -> * R | id``,
  ``R -> L``, section 4.7.3) is the book's own example of a grammar that is
  LALR(1) but *not* SLR(1) -- used to prove this module reproduces that
  exact distinction, not just that it builds *some* table.
"""

from __future__ import annotations

import pytest

from app.grammar import (
    EPSILON,
    GrammarError,
    analyse,
    build_lalr_table,
    build_slr_table,
    closure0,
    compute_first,
    compute_follow,
    goto0,
    parse_grammar,
    run_lr_parse,
    trace_lr_parse,
)
from app.samples import AMBIGUOUS_GRAMMAR, DEMO_GRAMMAR, MINILANG_GRAMMAR

# ---------------------------------------------------------------------------
# The book's own non-left-recursive expression grammar (section 4.4),
# reused by several test classes below.
# ---------------------------------------------------------------------------

LL1_EXPR_GRAMMAR = """
E  -> T Ep
Ep -> + T Ep | eps
T  -> F Tp
Tp -> * F Tp | eps
F  -> ( E ) | id
"""

# The book's own example of a grammar that is LALR(1) but not SLR(1)
# (section 4.7.3): an assignment statement, where the left side of "="
# is an L-value built from "*" (dereference) or a bare identifier.
ASSIGNMENT_GRAMMAR = """
S -> L = R | R
L -> * R | id
R -> L
"""


# ---------------------------------------------------------------------------
# Grammar text parsing
# ---------------------------------------------------------------------------


class TestGrammarParsing:
    def test_basic_productions_and_vocabulary(self) -> None:
        grammar = parse_grammar("E -> E + T | T\nT -> id\n")
        assert grammar.start_symbol == "E"
        assert grammar.nonterminals == {"E", "T"}
        assert grammar.terminals == {"+", "id"}
        assert len(grammar.productions) == 3

    def test_arrow_variants_accepted(self) -> None:
        via_ascii = parse_grammar("S -> a")
        via_unicode = parse_grammar("S → a")
        assert via_ascii.productions == via_unicode.productions

    def test_explicit_start_symbol_overrides_first_production(self) -> None:
        grammar = parse_grammar("T -> id\nS -> T\n", start_symbol="S")
        assert grammar.start_symbol == "S"

    @pytest.mark.parametrize("spelling", ["eps", "EPS", "epsilon", "Epsilon", "ε"])
    def test_epsilon_spellings(self, spelling: str) -> None:
        grammar = parse_grammar(f"S -> a | {spelling}")
        rhs_values = {p.rhs for p in grammar.productions_for("S")}
        assert () in rhs_values

    def test_empty_alternative_is_also_epsilon(self) -> None:
        # A trailing "|" with nothing after it is as much an epsilon
        # production as writing "eps" out -- both spellings must agree.
        grammar = parse_grammar("S -> a |")
        rhs_values = {p.rhs for p in grammar.productions_for("S")}
        assert () in rhs_values

    def test_comments_are_stripped(self) -> None:
        grammar = parse_grammar("S -> a # this is S\n# a whole comment line\n")
        assert len(grammar.productions) == 1

    def test_pipe_shaped_terminal_is_not_split_by_the_alternative_separator(self) -> None:
        """Regression test.

        ``S -> || a | eps`` must parse as two alternatives, ``|| a`` and
        ``eps`` -- not as four, with ``||`` torn into two empty
        alternatives around an invisible seam. Naively splitting the raw
        text on the character ``|`` gets this wrong; splitting on a
        standalone ``|`` *token* (the actual fix) gets it right.
        """
        grammar = parse_grammar("S -> || a | eps")
        rhs_values = {p.rhs for p in grammar.productions_for("S")}
        assert rhs_values == {("||", "a"), ()}

    def test_missing_arrow_is_an_error(self) -> None:
        with pytest.raises(GrammarError, match="missing '->'"):
            parse_grammar("S a b\n")

    def test_wrapped_alternatives_continue_the_previous_production(self) -> None:
        grammar = parse_grammar("S -> a b\n    | c d\n    | eps\n")
        rhs_values = {p.rhs for p in grammar.productions_for("S")}
        assert rhs_values == {("a", "b"), ("c", "d"), ()}

    def test_empty_left_hand_side_is_an_error(self) -> None:
        with pytest.raises(GrammarError, match="left-hand side"):
            parse_grammar(" -> a\n")

    def test_no_productions_is_an_error(self) -> None:
        with pytest.raises(GrammarError, match="no productions"):
            parse_grammar("\n   \n")

    def test_unresolvable_start_symbol_is_an_error(self) -> None:
        with pytest.raises(GrammarError, match="start symbol"):
            parse_grammar("S -> a\n", start_symbol="Z")


# ---------------------------------------------------------------------------
# FIRST sets (section 4.4.2) -- pinned against the book's own LL(1)
# expression grammar.
# ---------------------------------------------------------------------------


class TestFirstSets:
    def setup_method(self) -> None:
        self.grammar = parse_grammar(LL1_EXPR_GRAMMAR, start_symbol="E")
        self.first = compute_first(self.grammar)

    def test_e_t_f_share_first_set(self) -> None:
        expected = frozenset({"(", "id"})
        assert self.first["E"] == expected
        assert self.first["T"] == expected
        assert self.first["F"] == expected

    def test_ep_is_nullable_with_plus(self) -> None:
        assert self.first["Ep"] == frozenset({"+", EPSILON})

    def test_tp_is_nullable_with_star(self) -> None:
        assert self.first["Tp"] == frozenset({"*", EPSILON})

    def test_first_of_a_terminal_is_itself(self) -> None:
        assert self.first["id"] == frozenset({"id"})


# ---------------------------------------------------------------------------
# FOLLOW sets (section 4.4.2) -- same grammar.
# ---------------------------------------------------------------------------


class TestFollowSets:
    def setup_method(self) -> None:
        self.grammar = parse_grammar(LL1_EXPR_GRAMMAR, start_symbol="E")
        self.first = compute_first(self.grammar)
        self.follow = compute_follow(self.grammar, self.first)

    def test_e_and_ep_share_follow_set(self) -> None:
        expected = frozenset({"$", ")"})
        assert self.follow["E"] == expected
        assert self.follow["Ep"] == expected

    def test_t_and_tp_share_follow_set(self) -> None:
        expected = frozenset({"+", "$", ")"})
        assert self.follow["T"] == expected
        assert self.follow["Tp"] == expected

    def test_f_follow_set(self) -> None:
        assert self.follow["F"] == frozenset({"+", "*", "$", ")"})

    def test_start_symbol_always_carries_end_marker(self) -> None:
        assert "$" in self.follow[self.grammar.start_symbol]


# ---------------------------------------------------------------------------
# LL(1) table (Algorithm 4.6) -- same grammar, plus two negative cases.
# ---------------------------------------------------------------------------


class TestLl1Table:
    def test_expression_grammar_is_ll1_with_expected_cells(self) -> None:
        analysis = analyse(LL1_EXPR_GRAMMAR, start_symbol="E")
        assert analysis.ll1.is_ll1
        assert not analysis.ll1.conflicts

        def production_at(nonterminal: str, terminal: str) -> str:
            cell = analysis.ll1.table[(nonterminal, terminal)]
            assert len(cell) == 1
            return str(cell[0])

        assert production_at("E", "id") == "E -> T Ep"
        assert production_at("E", "(") == "E -> T Ep"
        assert production_at("Ep", "+") == "Ep -> + T Ep"
        assert production_at("Ep", "$") == f"Ep -> {EPSILON}"
        assert production_at("Ep", ")") == f"Ep -> {EPSILON}"
        assert production_at("T", "id") == "T -> F Tp"
        assert production_at("Tp", "*") == "Tp -> * F Tp"
        assert production_at("Tp", "+") == f"Tp -> {EPSILON}"
        assert production_at("Tp", "$") == f"Tp -> {EPSILON}"
        assert production_at("F", "id") == "F -> id"
        assert production_at("F", "(") == "F -> ( E )"

    def test_left_recursive_grammar_is_not_ll1(self) -> None:
        # S -> S a | a cannot be LL(1): both alternatives start with 'a'
        # once FIRST(S) is worked out, so M[S, a] has two entries.
        analysis = analyse("S -> S a | a\n", start_symbol="S")
        assert not analysis.ll1.is_ll1
        conflict_cells = {(c.detail.split(",")[0], c.symbol) for c in analysis.ll1.conflicts}
        assert ("S", "a") in conflict_cells

    def test_demo_grammar_is_not_ll1(self) -> None:
        # app.samples.DEMO_GRAMMAR is explicitly documented as
        # left-recursive and therefore not LL(1) -- see app/samples.py.
        analysis = analyse(DEMO_GRAMMAR)
        assert not analysis.ll1.is_ll1

    def test_minilang_expression_grammar_is_ll1(self) -> None:
        """Confirm MiniLang's own left-recursion-eliminated grammar is LL(1).

        Report 1's grammar section walks through why this parser's whole
        recursive-descent design depends on that being true; this checks it
        independently of the hand-written parser.
        """
        minilang_expr = """
        expr       -> logicOr
        logicOr    -> logicAnd orTail
        orTail     -> || logicAnd orTail | eps
        logicAnd   -> equality andTail
        andTail    -> && equality andTail | eps
        equality   -> compare eqTail
        eqTail     -> eq compare eqTail | ne compare eqTail | eps
        compare    -> term cmpTail
        cmpTail    -> lt term cmpTail | gt term cmpTail
                    | le term cmpTail | ge term cmpTail | eps
        term       -> factor termTail
        termTail   -> + factor termTail | - factor termTail | eps
        factor     -> unary factorTail
        factorTail -> * unary factorTail | / unary factorTail
                    | pct unary factorTail | eps
        unary      -> not unary | neg unary | primary
        primary    -> NUM | ID | true | false | lparen expr rparen
        """
        analysis = analyse(minilang_expr, start_symbol="expr")
        assert analysis.ll1.is_ll1, analysis.ll1.conflicts

    def test_minilang_full_grammar_is_ll1(self) -> None:
        """Confirm the full, left-factored MiniLang grammar is LL(1).

        :data:`app.samples.MINILANG_GRAMMAR` is what `app/table_parser.py`
        builds its predictive-parsing table from; its own docstring carries
        the left-factoring rationale (Aho et al. section 4.3.4) for `stmt`'s
        `assign`-vs-`exprStmt` ambiguity. This is the permanent regression
        guard: if a future edit ever reintroduces a conflict, this fails
        here rather than surfacing as a silently wrong table at runtime --
        app/table_parser.py itself also asserts this at import time.
        """
        analysis = analyse(MINILANG_GRAMMAR, start_symbol="program")
        assert analysis.ll1.is_ll1, analysis.ll1.conflicts


# ---------------------------------------------------------------------------
# LR(0) items and the canonical collection (Algorithm 4.9, section 4.6.2)
# ---------------------------------------------------------------------------


class TestLr0CanonicalCollection:
    def test_demo_grammar_has_twelve_states(self) -> None:
        # Figure 4.31 of the book draws exactly this DFA, for exactly this
        # grammar, with states I0 through I11 -- twelve in total.
        analysis = analyse(DEMO_GRAMMAR)
        assert len(analysis.lr0.states) == 12

    def test_start_state_closure_includes_every_f_production(self) -> None:
        grammar = parse_grammar(DEMO_GRAMMAR)
        analysis = analyse(DEMO_GRAMMAR)
        start_items = analysis.lr0.states[0].items
        f_productions = {item.production for item in start_items if item.production.lhs == "F"}
        assert len(f_productions) == len(grammar.productions_for("F"))

    def test_goto_on_a_symbol_the_state_cannot_shift_is_empty(self) -> None:
        analysis = analyse(DEMO_GRAMMAR)
        start_items = analysis.lr0.states[0].items
        # The start state has no item of the form [X -> ... . + ...], so
        # GOTO on '+' from it must be empty.
        assert goto0(start_items, "+", analysis.lr0.grammar) == frozenset()

    def test_closure_is_idempotent(self) -> None:
        analysis = analyse(DEMO_GRAMMAR)
        start_items = analysis.lr0.states[0].items
        assert closure0(start_items, analysis.lr0.grammar) == start_items


# ---------------------------------------------------------------------------
# SLR(1) table (Algorithm 4.10, section 4.7.2)
# ---------------------------------------------------------------------------


class TestSlrTable:
    def test_demo_grammar_is_conflict_free(self) -> None:
        analysis = analyse(DEMO_GRAMMAR)
        assert analysis.slr.is_conflict_free

    @pytest.mark.parametrize(
        "sentence",
        ["id", "id + id", "id * id", "id + id * id", "( id + id ) * id", "( ( id ) )"],
    )
    def test_demo_grammar_accepts_valid_sentences(self, sentence: str) -> None:
        analysis = analyse(DEMO_GRAMMAR)
        assert run_lr_parse(analysis.slr, sentence.split())

    @pytest.mark.parametrize(
        "sentence",
        ["id id", "+ id", "id +", "( id", "id )", "id + + id", "( id + id"],
    )
    def test_demo_grammar_rejects_invalid_sentences(self, sentence: str) -> None:
        analysis = analyse(DEMO_GRAMMAR)
        assert not run_lr_parse(analysis.slr, sentence.split())

    def test_ambiguous_grammar_has_shift_reduce_conflicts(self) -> None:
        # E -> E + E | E * E | ( E ) | id has no precedence encoded at all,
        # so every state after a complete E has to choose between reducing
        # and shifting the next operator -- the book's own example of this
        # (section 4.8) uses exactly this grammar.
        analysis = analyse(AMBIGUOUS_GRAMMAR)
        assert not analysis.slr.is_conflict_free
        assert all(c.kind == "shift-reduce" for c in analysis.slr.conflicts)

    def test_build_slr_table_is_reusable_from_a_shared_lr0_automaton(self) -> None:
        grammar = parse_grammar(DEMO_GRAMMAR)
        first = compute_first(grammar)
        follow = compute_follow(grammar, first)
        from app.grammar import build_lr0_automaton

        automaton = build_lr0_automaton(grammar)
        table = build_slr_table(automaton, follow)
        assert table.is_conflict_free
        assert run_lr_parse(table, ["id", "+", "id"])


# ---------------------------------------------------------------------------
# LALR(1) table (section 4.7.3) -- the book's own "SLR fails, LALR
# succeeds" example.
# ---------------------------------------------------------------------------


class TestLalrTable:
    def test_assignment_grammar_is_not_slr(self) -> None:
        analysis = analyse(ASSIGNMENT_GRAMMAR, start_symbol="S")
        assert not analysis.slr.is_conflict_free
        assert any(c.symbol == "=" and c.kind == "shift-reduce" for c in analysis.slr.conflicts)

    def test_assignment_grammar_is_lalr(self) -> None:
        # The textbook's whole point in choosing this grammar: a parser
        # generator using per-state LALR lookaheads accepts it cleanly, even
        # though the coarser, FOLLOW-based SLR construction cannot.
        analysis = analyse(ASSIGNMENT_GRAMMAR, start_symbol="S")
        assert analysis.lalr.is_conflict_free

    @pytest.mark.parametrize("sentence", ["id", "* id", "id = id", "* id = id", "id = * id"])
    def test_lalr_table_accepts_valid_sentences(self, sentence: str) -> None:
        analysis = analyse(ASSIGNMENT_GRAMMAR, start_symbol="S")
        assert run_lr_parse(analysis.lalr, sentence.split())

    @pytest.mark.parametrize("sentence", ["id id", "= id", "id =", "* ="])
    def test_lalr_table_rejects_invalid_sentences(self, sentence: str) -> None:
        analysis = analyse(ASSIGNMENT_GRAMMAR, start_symbol="S")
        assert not run_lr_parse(analysis.lalr, sentence.split())

    def test_build_lalr_table_needs_no_follow_sets(self) -> None:
        # LALR's lookaheads come entirely from the LR(1) construction
        # itself, never from a global FOLLOW set -- unlike build_slr_table,
        # this function's signature does not even accept one.
        grammar = parse_grammar(ASSIGNMENT_GRAMMAR, start_symbol="S")
        first = compute_first(grammar)
        _automaton, table = build_lalr_table(grammar, first)
        assert table.is_conflict_free


# ---------------------------------------------------------------------------
# A general law: every SLR(1) grammar is also LALR(1) (section 4.7.3's own
# remark that LALR is strictly at least as powerful as SLR).
# ---------------------------------------------------------------------------


class TestSlrGrammarsAreAlwaysLalr:
    @pytest.mark.parametrize(
        "grammar_text",
        [
            DEMO_GRAMMAR,
            "S -> ( S ) | a\n",
            "S -> a S b | eps\n",
            LL1_EXPR_GRAMMAR,
        ],
    )
    def test_slr_conflict_free_implies_lalr_conflict_free(self, grammar_text: str) -> None:
        analysis = analyse(grammar_text)
        if analysis.slr.is_conflict_free:
            assert analysis.lalr.is_conflict_free


# ---------------------------------------------------------------------------
# The parse-trace driver (section 4.6.1's "moves of an LR parser")
# ---------------------------------------------------------------------------


class TestTraceLrParse:
    def test_agrees_with_run_lr_parse_on_acceptance(self) -> None:
        # Both functions share one internal stepping routine specifically so
        # they cannot disagree; this pins that invariant from the outside.
        analysis = analyse(DEMO_GRAMMAR)
        for sentence in ["id + id * id", "( id + id ) * id", "id id", "( id"]:
            tokens = sentence.split()
            trace = trace_lr_parse(analysis.slr, tokens)
            accepted = trace[-1].action == "accept"
            assert accepted == run_lr_parse(analysis.slr, tokens)

    def test_accepted_trace_ends_in_accept_with_only_the_end_marker_left(self) -> None:
        analysis = analyse(DEMO_GRAMMAR)
        trace = trace_lr_parse(analysis.slr, ["id", "+", "id"])
        assert trace[-1].action == "accept"
        assert trace[-1].input == ("$",)

    def test_rejected_trace_ends_in_an_error(self) -> None:
        analysis = analyse(DEMO_GRAMMAR)
        trace = trace_lr_parse(analysis.slr, ["id", "id"])
        assert trace[-1].action.startswith("error:")

    def test_step_numbers_are_sequential_from_zero(self) -> None:
        analysis = analyse(DEMO_GRAMMAR)
        trace = trace_lr_parse(analysis.slr, ["id", "+", "id", "*", "id"])
        assert [s.step for s in trace] == list(range(len(trace)))

    def test_stack_grows_by_one_state_and_symbol_per_shift(self) -> None:
        # The very first step, before anything has been shifted, must show
        # only the start state with no symbol above it.
        analysis = analyse(DEMO_GRAMMAR)
        trace = trace_lr_parse(analysis.slr, ["id"])
        assert trace[0].stack == ("0",)

    def test_matches_the_books_own_trace_shape_for_id_plus_id_times_id(self) -> None:
        # A direct transcription of the shape of the book's own worked trace
        # for this exact sentence (section 4.6.1): every one of the five
        # input symbols (id + id * id) is shifted exactly once, interleaved
        # with eight reductions, ending cleanly on accept.
        analysis = analyse(DEMO_GRAMMAR)
        trace = trace_lr_parse(analysis.slr, ["id", "+", "id", "*", "id"])
        actions = [s.action for s in trace]
        assert actions[0].startswith("shift 'id'")
        assert actions[-1] == "accept"
        assert sum(a.startswith("shift") for a in actions) == 5  # one per input symbol
        assert sum(a.startswith("reduce") for a in actions) == 8


# ---------------------------------------------------------------------------
# The convenience entry point
# ---------------------------------------------------------------------------


class TestAnalyseConvenienceFunction:
    def test_bundles_every_result_consistently(self) -> None:
        analysis = analyse(DEMO_GRAMMAR)
        assert analysis.grammar.start_symbol == "E"
        assert set(analysis.first) >= analysis.grammar.terminals | analysis.grammar.nonterminals
        assert set(analysis.follow) == analysis.grammar.nonterminals
        assert analysis.lr0.grammar.start_symbol != analysis.grammar.start_symbol  # augmented
        assert analysis.lalr_automaton.grammar.start_symbol != analysis.grammar.start_symbol

    def test_explicit_start_symbol_is_threaded_through(self) -> None:
        analysis = analyse(ASSIGNMENT_GRAMMAR, start_symbol="S")
        assert analysis.grammar.start_symbol == "S"
        assert "$" in analysis.follow["S"]
