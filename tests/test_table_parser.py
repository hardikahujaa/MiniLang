"""Tests for :mod:`app.table_parser`.

The grammar itself is pinned separately, against the textbook, in
``tests/test_grammar.py::TestLl1Table::test_minilang_full_grammar_is_ll1``.
What this file checks is that the *driver* built on top of that proven-LL(1)
table behaves correctly: it accepts every program the hand-written
recursive-descent parser accepts (an independent cross-check between two
separately built parsers for the same language), it never raises on bad
input, and the concrete parse tree it builds really is a different artefact
from the abstract syntax tree -- which is the entire point of having both.
"""

from __future__ import annotations

import pytest

from app.lexer import TokenType, tokenize
from app.parser import ParseError
from app.parser import parse as parse_ast
from app.table_parser import (
    _MAX_STACK_DEPTH,
    ParseTreeNode,
    run_table_parse,
    terminal_for,
    trace_table_parse,
)
from tests.conftest import PROGRAMS_DIR


def _tokens_for(source: str) -> list:
    return tokenize(source).tokens


class TestTerminalMapping:
    def test_keyword_and_punctuation_tokens_are_lowercased_type_names(self) -> None:
        tokens = _tokens_for("func m(): void {}")
        mapped = [terminal_for(t) for t in tokens]
        assert mapped[0] == "func"
        assert mapped[1] == "ident"
        assert mapped[2] == "lparen"
        assert mapped[3] == "rparen"
        assert mapped[4] == "colon"
        assert mapped[5] == "void"

    def test_eof_maps_to_the_end_marker(self) -> None:
        tokens = _tokens_for("")
        assert tokens[-1].type is TokenType.EOF
        assert terminal_for(tokens[-1]) == "$"


class TestAcceptsRealPrograms:
    def test_accepts_the_demo_program(self, demo_source: str) -> None:
        result = trace_table_parse(_tokens_for(demo_source))
        assert result.accepted
        assert result.steps[-1].action == "accept"

    @pytest.mark.parametrize(
        "program_path", sorted(PROGRAMS_DIR.glob("*.ml")), ids=lambda p: p.name
    )
    def test_agrees_with_baseline_parser_on_every_valid_program(self, program_path) -> None:
        """Independent cross-validation between two separately built parsers.

        Two separately built parsers for the same language must accept
        exactly the same programs. Parametrized over every file in
        tests/programs/ -- picks up Feature 3's corpus automatically as it
        is added, with no changes needed here.
        """
        source = program_path.read_text(encoding="utf-8")
        tokens = _tokens_for(source)

        baseline_accepts = True
        try:
            parse_ast(tokens)
        except ParseError:
            baseline_accepts = False

        assert run_table_parse(tokens) == baseline_accepts


class TestLeftFactoringCases:
    """Constructs that motivated left-factoring `stmt` in MINILANG_GRAMMAR.

    Assignment and every shape of expression-statement collide on a leading
    `ident`, and must all still parse correctly once factored. These are the
    load-bearing regression tests for that design decision -- if left
    factoring were done wrong, exactly these cases would be the ones to
    break.
    """

    @pytest.mark.parametrize(
        "statement",
        [
            "x = 2;",  # assignment
            "x;",  # bare identifier expression-statement
            "f(1);",  # call expression-statement
            "x + 1;",  # larger expression-statement starting with ident
            "x();",  # call with no arguments
        ],
    )
    def test_ident_leading_statements_all_parse(self, statement: str) -> None:
        source = f"func f(int a): int {{ return a; }} func m(): void {{ int x = 1; {statement} }}"
        result = trace_table_parse(_tokens_for(source))
        assert result.accepted, result.steps[-1].action

    def test_prefix_operators_still_use_the_unrestricted_unary(self) -> None:
        # !x; and -x; must still parse the identifier normally after the
        # first token -- unaryNoId only restricts exprStmt's very first
        # symbol, not anything nested after it.
        source = "func m(): void { bool x = true; !x; }"
        result = trace_table_parse(_tokens_for(source))
        assert result.accepted, result.steps[-1].action


class TestRejectsMalformedPrograms:
    def test_missing_semicolon_ends_in_an_error_step_not_an_exception(self) -> None:
        source = "func m(): void { int x = 1 print(x); }"
        result = trace_table_parse(_tokens_for(source))
        assert not result.accepted
        assert result.steps[-1].action.startswith("error:")

    def test_run_table_parse_agrees_with_trace_table_parse(self) -> None:
        # Both share one internal stepping generator specifically so they
        # cannot disagree -- pinned here the same way app.grammar pins the
        # analogous invariant for its own LR driver.
        bad = _tokens_for("func m(): void { int x = 1 print(x); }")
        good = _tokens_for("func m(): void { print(1); }")
        assert run_table_parse(bad) == trace_table_parse(bad).accepted
        assert run_table_parse(good) == trace_table_parse(good).accepted

    def test_pathologically_nested_expression_is_rejected_cleanly(self) -> None:
        depth = _MAX_STACK_DEPTH  # comfortably past the real threshold
        source = f"func m(): void {{ int x = {'(' * depth}1{')' * depth}; }}"
        result = trace_table_parse(_tokens_for(source))
        assert not result.accepted
        assert "too deep" in result.steps[-1].action


class TestConcreteParseTree:
    def test_root_is_the_start_symbol(self, demo_source: str) -> None:
        result = trace_table_parse(_tokens_for(demo_source))
        assert result.tree.symbol == "program"
        assert result.tree.kind == "nonterminal"

    def test_tree_shape_differs_from_the_ast_envelope(self) -> None:
        """The concrete tree keeps grammar scaffolding the AST collapses.

        An assignment's parse tree must contain the `stmtIdTail` node left
        factoring introduced, which has no counterpart anywhere in
        app.ast_nodes's AssignStmt.
        """
        source = "func m(): void { int x = 1; x = 2; }"
        result = trace_table_parse(_tokens_for(source))

        def find_symbols(node: ParseTreeNode) -> set[str]:
            found = {node.symbol}
            for child in node.children:
                found |= find_symbols(child)
            return found

        symbols = find_symbols(result.tree)
        assert "stmtIdTail" in symbols

        ast = parse_ast(_tokens_for(source)).to_dict()
        assert "stmtIdTail" not in str(ast)

    def test_every_node_is_stamped_with_a_valid_step(self, demo_source: str) -> None:
        result = trace_table_parse(_tokens_for(demo_source))
        last_step = result.steps[-1].step

        def check(node: ParseTreeNode) -> None:
            assert 0 <= node.step <= last_step
            for child in node.children:
                check(child)

        check(result.tree)
        assert result.tree.step == 0

    def test_matched_terminal_carries_its_source_position(self) -> None:
        result = trace_table_parse(_tokens_for("func m(): void {}"))
        func_node = result.tree.children[0].children[0]
        assert func_node.symbol == "func"
        assert func_node.kind == "terminal"
        assert func_node.value == "func"
        assert func_node.line == 1
        assert func_node.col == 1

    def test_epsilon_production_produces_an_explicit_leaf(self) -> None:
        # A program with exactly one function has an empty programTail,
        # which must still show up as a real (epsilon) node, not be
        # silently skipped.
        result = trace_table_parse(_tokens_for("func m(): void {}"))
        program_tail = result.tree.children[1]
        assert program_tail.symbol == "programTail"
        assert len(program_tail.children) == 1
        assert program_tail.children[0].kind == "epsilon"
