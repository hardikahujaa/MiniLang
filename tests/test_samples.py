"""Tests for :mod:`app.samples`.

The canonical demo program appears in three places: served by ``GET /api/demo``,
stored as ``tests/programs/demo.ml``, and reproduced in the report. If those
copies drift, screenshots stop matching the text around them. This module pins
them together.
"""

from __future__ import annotations

from pathlib import Path

from app.samples import AMBIGUOUS_GRAMMAR, DEMO_GRAMMAR, DEMO_PROGRAM

PROGRAMS_DIR = Path(__file__).resolve().parent / "programs"


class TestDemoProgram:
    """The canonical program from section 2 of the plan."""

    def test_matches_the_fixture_file_byte_for_byte(self):
        on_disk = (PROGRAMS_DIR / "demo.ml").read_text(encoding="utf-8")
        assert on_disk == DEMO_PROGRAM

    def test_exercises_every_optimisation_the_plan_names(self):
        """Each pass needs a target in the demo, or the Optimization tab is dull."""
        assert "5 * 2" in DEMO_PROGRAM, "constant folding target missing"
        assert "int unused = 42;" in DEMO_PROGRAM, "dead code elimination target missing"
        assert "(3 + 4) * (3 + 4)" in DEMO_PROGRAM, "CSE target missing"

    def test_exercises_every_language_construct(self):
        """The demo must reach every phase, so it must use every construct."""
        for construct in ("func", "while", "return", "print", "int", "void"):
            assert construct in DEMO_PROGRAM, f"demo program never uses '{construct}'"

    def test_declares_both_functions(self):
        assert "func fib(int n): int" in DEMO_PROGRAM
        assert "func main(): void" in DEMO_PROGRAM

    def test_brackets_are_balanced(self):
        """A cheap sanity check that the fixture has not been truncated."""
        for opening, closing in (("{", "}"), ("(", ")")):
            assert DEMO_PROGRAM.count(opening) == DEMO_PROGRAM.count(
                closing
            ), f"unbalanced {opening}{closing} in the demo program"

    def test_ends_with_a_newline(self):
        assert DEMO_PROGRAM.endswith("\n")


class TestSampleGrammars:
    """Grammars preloaded into the Parser Theory Lab."""

    def test_demo_grammar_is_the_classic_expression_grammar(self):
        assert "E -> E + T" in DEMO_GRAMMAR
        assert "T -> T * F" in DEMO_GRAMMAR
        assert "F -> ( E ) | id" in DEMO_GRAMMAR

    def test_demo_grammar_is_left_recursive(self):
        """Left recursion is the point: it is not LL(1) but it is SLR(1)."""
        for line in DEMO_GRAMMAR.strip().splitlines():
            lhs, _, rhs = line.partition("->")
            if lhs.strip() == "E":
                assert rhs.strip().startswith("E"), "E should be left-recursive"

    def test_ambiguous_grammar_has_duplicate_binary_operators(self):
        """The shift/reduce conflict demoed on day 9 comes from this shape."""
        assert "E -> E + E" in AMBIGUOUS_GRAMMAR
        assert "E * E" in AMBIGUOUS_GRAMMAR

    def test_grammars_are_non_empty_and_newline_terminated(self):
        for grammar in (DEMO_GRAMMAR, AMBIGUOUS_GRAMMAR):
            assert grammar.strip()
            assert grammar.endswith("\n")
