"""Tests for :mod:`app.errors`.

Organised by what each piece claims, per the module's own docstring split:
panic-mode recovery is the textbook algorithm (Aho et al. section 4.1.3), so
its tests are about provable properties (termination, "a clean program is
unaffected", "recovery resumes at the right point") rather than opinions
about output; the ranking engine is this project's own design, so its tests
pin concrete, hand-verified cases against the real grammar in
``app/parser.py`` rather than abstract claims about what "should" rank well.
"""

from __future__ import annotations

import pytest

from app.errors import RecoveringParser, levenshtein, recover_parse
from app.lexer import TokenType, tokenize
from app.parser import parse as baseline_parse


def _tokens(source: str) -> list:
    return tokenize(source).tokens


class TestLevenshtein:
    def test_identical_strings(self) -> None:
        assert levenshtein("while", "while") == 0

    def test_empty_strings(self) -> None:
        assert levenshtein("", "") == 0
        assert levenshtein("a", "") == 1
        assert levenshtein("", "abc") == 3

    def test_single_substitution(self) -> None:
        assert levenshtein("cat", "bat") == 1

    def test_single_transposition_costs_two(self) -> None:
        """Plain Levenshtein, not Damerau-Levenshtein.

        An adjacent transposition is a deletion plus an insertion, not one
        discounted "swap" operation -- documented explicitly in the
        module's own docstring so this is never "fixed" into
        Damerau-Levenshtein by accident later.
        """
        assert levenshtein("while", "whiel") == 2

    def test_classic_example(self) -> None:
        # A standard worked example for sanity-checking the DP table itself.
        assert levenshtein("kitten", "sitting") == 3


class TestPanicModeTermination:
    """The textbook's own termination argument, checked directly.

    Rather than only inferred from end-to-end behaviour not hanging.
    """

    def test_already_at_semicolon_consumes_exactly_it(self) -> None:
        parser = RecoveringParser(_tokens("func m(): void { ; }"))
        # Position the cursor at the stray ';' directly, as if a statement
        # had just failed to start there.
        while parser._peek().type is not TokenType.SEMICOLON:  # noqa: SLF001
            parser._advance()  # noqa: SLF001
        before = parser._pos  # noqa: SLF001
        parser._panic_skip_statement()  # noqa: SLF001
        assert parser._pos == before + 1  # noqa: SLF001

    def test_already_at_a_statement_start_consumes_nothing(self) -> None:
        """Regression test for the exact bug this design went through.

        Discarding the token that starts a perfectly good next statement,
        just because it also happens to be a synchronizing token.
        """
        parser = RecoveringParser(_tokens("func m(): void { int x = 1; }"))
        while parser._peek().type is not TokenType.INT:  # noqa: SLF001
            parser._advance()  # noqa: SLF001
        before = parser._pos  # noqa: SLF001
        parser._panic_skip_statement()  # noqa: SLF001
        assert parser._pos == before  # noqa: SLF001

    def test_already_at_end_of_input_consumes_nothing(self) -> None:
        parser = RecoveringParser(_tokens(""))
        assert parser._is_at_end()  # noqa: SLF001
        parser._panic_skip_statement()  # noqa: SLF001
        assert parser._is_at_end()  # noqa: SLF001

    def test_genuine_garbage_is_discarded_until_a_sync_token(self) -> None:
        parser = RecoveringParser(_tokens("func m(): void { ) ) ) int x = 1; }"))
        # func, m, (, ), :, void, { -- lands on the first stray ')'.
        for _ in range(7):
            parser._advance()  # noqa: SLF001
        assert parser._peek().type is TokenType.RPAREN  # noqa: SLF001
        parser._panic_skip_statement()  # noqa: SLF001
        assert parser._peek().type is TokenType.INT  # noqa: SLF001

    def test_recovery_terminates_on_a_pathological_stray_semicolon(self) -> None:
        # ';' appearing exactly where a statement should start -- the
        # original motivating case for checking before discarding.
        source = "func m(): void { ; int x = 1; }"
        result = recover_parse(_tokens(source))
        assert len(result.reports) == 1
        assert len(result.program.functions[0].body.statements) == 1

    def test_function_level_recovery_also_terminates(self) -> None:
        source = "int not a function at all func m(): void {}"
        result = recover_parse(_tokens(source))
        assert len(result.program.functions) == 1
        assert result.program.functions[0].name == "m"


class TestCleanProgramUnaffected:
    def test_zero_reports_on_the_demo_program(self, demo_source: str) -> None:
        result = recover_parse(_tokens(demo_source))
        assert result.reports == []

    def test_ast_is_identical_to_the_baseline_parser(self, demo_source: str) -> None:
        baseline = baseline_parse(_tokens(demo_source))
        recovered = recover_parse(_tokens(demo_source))
        assert recovered.program.to_dict() == baseline.to_dict()


class TestRecoveryResumesCorrectly:
    def test_two_independent_missing_semicolons_are_both_reported(self) -> None:
        """Regression test for the overreach bug.

        Discarding the sync token unconditionally would consume the `int`
        that starts the second, otherwise well-formed statement, silently
        folding it into the first statement's recovery and losing the
        second error entirely.
        """
        source = "func m(): void {\n    int x = 1\n    int y = 2\n}"
        result = recover_parse(_tokens(source))
        assert len(result.reports) == 2
        assert all("';'" in r.message for r in result.reports)

    def test_statements_after_a_recovered_error_still_parse(self) -> None:
        source = "func m(): void { int x = 1 int y = 2; print(y); }"
        result = recover_parse(_tokens(source))
        body = result.program.functions[0].body.statements
        # The second declaration and the print call both survive recovery.
        assert len(body) == 2

    def test_functions_after_a_recovered_declaration_still_parse(self) -> None:
        source = "func bad(: void {} func good(): void { print(1); }"
        result = recover_parse(_tokens(source))
        names = [f.name for f in result.program.functions]
        assert "good" in names


class TestCascadeSuppression:
    def test_errors_within_the_window_are_suppressed(self) -> None:
        # Two stray ')' two tokens apart -- within the default window of 2.
        source = "func m(): void { ) x ) }"
        result = recover_parse(_tokens(source), suppress=True)
        assert len(result.reports) == 1

    def test_errors_outside_the_window_are_both_reported(self) -> None:
        source = "func m(): void {\n    int x = 1\n    int y = 2\n}"
        result = recover_parse(_tokens(source), suppress=True)
        assert len(result.reports) == 2

    def test_suppress_false_reports_everything(self) -> None:
        source = "func m(): void { ) x ) }"
        suppressed = recover_parse(_tokens(source), suppress=True)
        unsuppressed = recover_parse(_tokens(source), suppress=False)
        assert len(unsuppressed.reports) >= len(suppressed.reports)


class TestBaselineBNaiveMode:
    def test_no_suggestions_and_no_confidence(self) -> None:
        source = "func m(): void {\n    int x = 1\n    int y = 2\n}"
        result = recover_parse(_tokens(source), rank=False, suppress=False)
        assert result.reports
        for report in result.reports:
            assert report.suggestions == []
            assert report.suggestion is None
            assert report.confidence == 0.0

    def test_still_recovers_and_still_resyncs(self) -> None:
        # Ranking is off, but panic-mode sync -- the textbook part -- still
        # runs; Baseline B is "no suggestions, no suppression", not "no
        # recovery at all". The malformed declaration itself is never added
        # to the tree (its parse never completed), but the well-formed
        # print statement that follows recovery still is.
        source = "func m(): void { int x = 1 print(x); }"
        result = recover_parse(_tokens(source), rank=False, suppress=False)
        assert len(result.reports) == 1
        statements = result.program.functions[0].body.statements
        assert len(statements) == 1
        assert statements[0].__class__.__name__ == "PrintStmt"


class TestSuggestionRanking:
    def test_misspelled_func_keyword_ranks_the_correct_fix_first(self) -> None:
        result = recover_parse(_tokens("fun m(): void {}"))
        assert result.reports[0].suggestion == "insert 'func'"

    def test_missing_semicolon_ranks_correctly(self) -> None:
        source = "func m(): void {\n    int x = 1\n    print(x);\n}"
        result = recover_parse(_tokens(source))
        assert result.reports[0].suggestion == "insert ';'"
        assert result.reports[0].confidence > 0.0

    def test_assign_for_eq_in_condition_ranks_correctly(self) -> None:
        result = recover_parse(_tokens("func m(): void { if (1 = 1) { print(1); } }"))
        assert result.reports[0].suggestion == "'==' instead of '='"

    def test_missing_colon_before_return_type_ranks_correctly(self) -> None:
        result = recover_parse(_tokens("func m() void { return 1; }"))
        assert result.reports[0].suggestion == "insert ':'"

    def test_at_most_three_suggestions(self) -> None:
        result = recover_parse(_tokens("func m() void { return 1; }"))
        assert len(result.reports[0].suggestions) <= 3

    def test_suggestions_are_sorted_by_confidence_descending(self) -> None:
        result = recover_parse(_tokens("fun m(): void {}"))
        confidences = [s.confidence for s in result.reports[0].suggestions]
        assert confidences == sorted(confidences, reverse=True)

    def test_empty_expected_set_still_produces_a_sensible_suggestion(self) -> None:
        # The only case with an empty expected set: Parser's own nesting
        # depth guard.
        depth = 40
        source = f"func m(): void {{ int x = {'(' * depth}1{')' * depth}; }}"
        result = recover_parse(_tokens(source))
        assert result.reports
        assert result.reports[0].suggestions[0].text == "simplify this expression"


@pytest.mark.parametrize(
    "source",
    [
        "func m(): void { int x = 1 print(x); }",
        "func m(): void {\n    int x = 1\n    int y = 2\n}",
        "fun m(): void {}",
        "func m(): void { if (1 = 1) { print(1); } }",
        "func m() void { return 1; }",
        "func m(): void { ; int x = 1; }",
        "",
        "func m(): void { ) ) ) int x = 1; }",
    ],
)
class TestNeverRaises:
    def test_recover_parse_never_raises(self, source: str) -> None:
        # Every case above would raise ParseError under app.parser.parse;
        # recover_parse must turn all of them into a RecoveryResult instead.
        result = recover_parse(_tokens(source))
        assert result.program is not None
