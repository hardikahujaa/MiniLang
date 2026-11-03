"""Tests for :mod:`app.vm`.

Organised by what a reader would actually want proven about an interpreter:
arithmetic and comparisons compute the right answer, control flow (branches,
loops, short-circuit evaluation) really does only run what it should,
function calls and recursion build and tear down activation records
correctly, and the three distinct ways a run can end -- halt, trap, exhaust
its step budget -- are told apart correctly. The demo program and a
recursive factorial are the acceptance tests: one straight-line/iterative,
one recursive, so neither style of program is a blind spot.
"""

from __future__ import annotations

from app.codegen import generate as generate_asm
from app.lexer import tokenize
from app.optimize import optimize
from app.parser import parse
from app.samples import DEMO_PROGRAM
from app.schemas import OptimizationToggles
from app.semantic import analyse
from app.tac import generate as generate_tac
from app.vm import VmResult, run


def execute(source: str, max_steps: int = 1_000_000) -> VmResult:
    """Compile a source string the whole way to assembly and run it.

    No optimisation passes are enabled: the VM must execute unoptimised
    assembly correctly on its own merits, independent of whether folding,
    propagation, CSE or DCE ran first.

    Args:
        source: MiniLang source text.
        max_steps: Instruction budget to pass through to the VM.

    Returns:
        The VM's result.
    """
    program = parse(tokenize(source).tokens)
    analyse(program)
    tac_result = generate_tac(program)
    optimized = optimize(
        tac_result.code, tac_result.function_bounds, OptimizationToggles(**_ALL_OFF)
    )
    asm_result = generate_asm(optimized.tac, tac_result.function_params)
    main_params = len(tac_result.function_params.get("main", []))
    return run(asm_result.asm, asm_result.entry, max_steps=max_steps, main_param_count=main_params)


_ALL_OFF = {
    "constant_folding": False,
    "constant_propagation": False,
    "common_subexpression_elimination": False,
    "dead_code_elimination": False,
}


def lines(result: VmResult) -> list[str]:
    """Split a run's captured output into its printed lines.

    Args:
        result: A VM run result.

    Returns:
        One string per ``print``, in order, with no trailing empty entry.
    """
    return result.output.splitlines()


# ---------------------------------------------------------------------------
# Arithmetic and comparisons
# ---------------------------------------------------------------------------


class TestArithmetic:
    def test_addition_subtraction_multiplication(self):
        result = execute("func main(): void { print(2 + 3); print(5 - 2); print(4 * 3); }")
        assert lines(result) == ["5", "3", "12"]

    def test_integer_division_truncates_toward_zero(self):
        result = execute("func main(): void { print(7 / 2); print(-7 / 2); }")
        assert lines(result) == ["3", "-3"]

    def test_float_division_is_not_truncated(self):
        result = execute("func main(): void { print(7.0 / 2); }")
        assert lines(result) == ["3.5"]

    def test_modulo_follows_the_dividend_sign(self):
        result = execute("func main(): void { print(-7 % 2); print(7 % 2); }")
        assert lines(result) == ["-1", "1"]

    def test_unary_minus_and_not(self):
        result = execute("func main(): void { print(-5); print(!false); }")
        assert lines(result) == ["-5", "true"]

    def test_comparisons(self):
        result = execute(
            "func main(): void { print(1 < 2); print(2 < 1); print(1 == 1); print(1 != 1); }"
        )
        assert lines(result) == ["true", "false", "true", "false"]

    def test_division_by_zero_traps_rather_than_crashing(self):
        # halted=True: a trap is a definite stop, not a program still
        # running when asked to stop looking (that is what halted=False
        # means -- see test_an_infinite_loop_exhausts_its_step_budget).
        result = execute("func main(): void { int z = 0; print(1 / z); }")
        assert result.halted is True
        assert result.error is not None
        assert "division by zero" in result.error

    def test_modulo_by_zero_traps(self):
        result = execute("func main(): void { int z = 0; print(1 % z); }")
        assert result.error is not None
        assert "division by zero" in result.error


# ---------------------------------------------------------------------------
# Control flow
# ---------------------------------------------------------------------------


class TestControlFlow:
    def test_if_without_else_runs_the_body_only_when_true(self):
        result = execute("func main(): void { if (true) { print(1); } if (false) { print(2); } }")
        assert lines(result) == ["1"]

    def test_if_else_picks_the_right_branch(self):
        result = execute("func main(): void { if (false) { print(1); } else { print(2); } }")
        assert lines(result) == ["2"]

    def test_while_loop_runs_the_correct_number_of_times(self):
        result = execute("func main(): void { int i = 0; while (i < 3) { print(i); i = i + 1; } }")
        assert lines(result) == ["0", "1", "2"]

    def test_while_loop_body_never_runs_when_condition_is_initially_false(self):
        result = execute("func main(): void { while (false) { print(1); } print(2); }")
        assert lines(result) == ["2"]

    def test_and_short_circuits_without_evaluating_the_right_operand(self):
        """f() has a visible side effect (print); it must not run."""
        result = execute(
            "func f(): bool { print(99); return true; } "
            "func main(): void { bool x = false && f(); print(x); }"
        )
        assert lines(result) == ["false"]

    def test_or_short_circuits_without_evaluating_the_right_operand(self):
        result = execute(
            "func f(): bool { print(99); return false; } "
            "func main(): void { bool x = true || f(); print(x); }"
        )
        assert lines(result) == ["true"]

    def test_and_does_evaluate_the_right_operand_when_necessary(self):
        result = execute(
            "func f(): bool { print(99); return true; } "
            "func main(): void { bool x = true && f(); print(x); }"
        )
        assert lines(result) == ["99", "true"]

    def test_an_infinite_loop_exhausts_its_step_budget_rather_than_hanging(self):
        result = execute("func main(): void { while (true) { } }", max_steps=2000)
        assert result.halted is False
        assert result.error is None
        assert result.steps == 2000


# ---------------------------------------------------------------------------
# Functions and recursion
# ---------------------------------------------------------------------------


class TestFunctionsAndRecursion:
    def test_a_void_function_call_runs_for_its_side_effect(self):
        result = execute("func f(): void { print(1); } func main(): void { f(); }")
        assert lines(result) == ["1"]

    def test_a_value_returning_function_call(self):
        result = execute("func f(): int { return 42; } func main(): void { print(f()); }")
        assert lines(result) == ["42"]

    def test_parameters_are_passed_by_value_in_order(self):
        result = execute(
            "func f(int a, int b): void { print(a); print(b); } " "func main(): void { f(1, 2); }"
        )
        assert lines(result) == ["1", "2"]

    def test_recursion_computes_the_right_answer(self):
        result = execute(
            "func fact(int n): int { if (n <= 1) { return 1; } return n * fact(n - 1); } "
            "func main(): void { print(fact(6)); }"
        )
        assert lines(result) == ["720"]

    def test_mutual_recursion_through_two_functions(self):
        result = execute(
            "func isEven(int n): bool { if (n == 0) { return true; } return isOdd(n - 1); } "
            "func isOdd(int n): bool { if (n == 0) { return false; } return isEven(n - 1); } "
            "func main(): void { print(isEven(10)); print(isOdd(10)); }"
        )
        assert lines(result) == ["true", "false"]

    def test_each_call_gets_its_own_independent_locals(self):
        """A recursive call's locals must not alias the caller's locals."""
        result = execute(
            "func count(int n): void { "
            "int i = 0; "
            "if (n > 0) { count(n - 1); } "
            "print(n); "
            "}"
            "func main(): void { count(3); }"
        )
        assert lines(result) == ["0", "1", "2", "3"]

    def test_a_bare_declaration_that_is_never_written_reads_as_zero(self):
        result = execute("func main(): void { int x; print(x); }")
        assert lines(result) == ["0"]

    def test_running_with_no_main_function_is_a_clean_error_not_a_crash(self):
        result = execute("func f(): void { }")
        assert result.halted is True
        assert result.error == "no 'main' function to run"

    def test_main_with_parameters_is_rejected_rather_than_reading_garbage(self):
        result = execute("func main(int n): void { print(n); }")
        assert result.halted is True
        assert "parameters" in (result.error or "")


# ---------------------------------------------------------------------------
# Shadowing (end-to-end regression for the app.tac scoping fix)
# ---------------------------------------------------------------------------


class TestShadowing:
    def test_an_inner_shadowed_variable_does_not_corrupt_the_outer_one(self):
        result = execute(
            "func main(): void { "
            "int x = 1; "
            "if (true) { int x = 2; print(x); } "
            "print(x); "
            "}"
        )
        assert lines(result) == ["2", "1"]


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


class TestOutputFormatting:
    def test_booleans_print_as_true_and_false(self):
        result = execute("func main(): void { print(true); print(false); }")
        assert lines(result) == ["true", "false"]

    def test_floats_print_with_their_decimal_point(self):
        result = execute("func main(): void { print(3.5); }")
        assert lines(result) == ["3.5"]

    def test_no_output_is_the_empty_string_not_a_trailing_newline(self):
        result = execute("func main(): void { }")
        assert result.output == ""


# ---------------------------------------------------------------------------
# Acceptance: the canonical demo program
# ---------------------------------------------------------------------------


class TestDemoProgram:
    def test_runs_to_completion_with_the_textbook_fibonacci_value(self):
        result = execute(DEMO_PROGRAM)
        assert result.halted is True
        assert result.error is None
        assert lines(result) == ["55", "49"]

    def test_optimised_and_unoptimised_runs_produce_identical_output(self):
        """The end-to-end proof that optimisation preserves behaviour, not just TAC."""
        program = parse(tokenize(DEMO_PROGRAM).tokens)
        analyse(program)
        tac_result = generate_tac(program)

        unoptimized_asm = generate_asm(tac_result.instructions, tac_result.function_params)
        unoptimized = run(unoptimized_asm.asm, unoptimized_asm.entry)

        optimized = optimize(
            tac_result.code,
            tac_result.function_bounds,
            OptimizationToggles(
                constant_folding=True,
                constant_propagation=True,
                common_subexpression_elimination=True,
                dead_code_elimination=True,
            ),
        )
        optimized_asm = generate_asm(optimized.tac, tac_result.function_params)
        optimized_result = run(optimized_asm.asm, optimized_asm.entry)

        assert unoptimized.output == optimized_result.output == "55\n49\n"
