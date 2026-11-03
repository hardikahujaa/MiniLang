"""Tests for :mod:`app.optimize`.

Organised by the four passes, then by how they interact, then by the demo
program as the acceptance test. The interaction and soundness tests are the
ones that matter most: a block-local approximation of dead-code elimination
would pass every single-pass test here and still be wrong, so
``TestSoundness`` exists specifically to catch that class of bug -- a
definition used only across a branch or a loop back-edge must survive, and a
pure instruction with a used result must never be removed regardless of how
many other passes ran first.
"""

from __future__ import annotations

from app.lexer import tokenize
from app.optimize import optimize
from app.parser import parse
from app.samples import DEMO_PROGRAM
from app.schemas import OptimizationResult, OptimizationToggles
from app.semantic import analyse
from app.tac import generate


def run_optimize(source: str, **enabled: bool) -> OptimizationResult:
    """Lex, parse, analyse, translate and optimise a source string.

    Every pass defaults to *off*, deliberately the opposite of
    :class:`OptimizationToggles`'s own pydantic defaults (which mirror the
    UI's default checkbox state: folding/propagation/DCE on, CSE off). A
    unit test names exactly the pass or passes it means to isolate; letting
    the other three default to "on" would silently run them too.

    Args:
        source: MiniLang source text.
        **enabled: Which passes to turn on, e.g. ``constant_folding=True``.

    Returns:
        The optimisation result.
    """
    program = parse(tokenize(source).tokens)
    analyse(program)
    tac_result = generate(program)
    toggles = OptimizationToggles(
        constant_folding=enabled.get("constant_folding", False),
        constant_propagation=enabled.get("constant_propagation", False),
        common_subexpression_elimination=enabled.get("common_subexpression_elimination", False),
        dead_code_elimination=enabled.get("dead_code_elimination", False),
    )
    return optimize(tac_result.code, tac_result.function_bounds, toggles)


def texts(result: OptimizationResult) -> list[str]:
    """Render every surviving instruction's text, in order.

    Args:
        result: An optimisation result.

    Returns:
        One string per instruction.
    """
    return [instr.text for instr in result.tac]


# ---------------------------------------------------------------------------
# Constant folding
# ---------------------------------------------------------------------------


class TestConstantFolding:
    """Evaluating an instruction whose operands are already literal."""

    def test_folds_arithmetic(self):
        result = run_optimize("func m(): void { int x = 3 + 4; }", constant_folding=True)
        assert "t1 = 7" in texts(result)

    def test_folds_relational_and_equality_to_a_boolean_spelling(self):
        result = run_optimize(
            "func m(): void { bool a = 1 < 2; bool b = 1 == 1; }", constant_folding=True
        )
        assert "t1 = true" in texts(result)
        assert "t2 = true" in texts(result)

    def test_folds_unary_minus_and_not(self):
        result = run_optimize(
            "func m(): void { int x = -5; bool y = !true; }", constant_folding=True
        )
        assert "t1 = -5" in texts(result)
        assert "t2 = false" in texts(result)

    def test_integer_division_truncates_toward_zero(self):
        result = run_optimize("func m(): void { int x = 7 / 2; }", constant_folding=True)
        assert "t1 = 3" in texts(result)

    def test_mixed_float_division_is_not_truncated(self):
        result = run_optimize("func m(): void { float x = 7.0 / 2; }", constant_folding=True)
        assert "t1 = 3.5" in texts(result)

    def test_modulo_follows_the_dividend_sign(self):
        # -7 is itself a fold (uminus 7); folding that into the % needs the
        # propagation pass too, exactly as constant folding alone would in a
        # real pipeline -- this is a two-instruction chain, not one op.
        result = run_optimize(
            "func m(): void { int x = -7 % 2; }",
            constant_folding=True,
            constant_propagation=True,
        )
        # -7 truncates to -3 * 2 = -6; -7 - (-6) = -1.
        assert "t2 = -1" in texts(result)

    def test_division_by_a_literal_zero_is_left_unfolded(self):
        """Folding it would hide a runtime trap as a compile-time value."""
        result = run_optimize("func m(): void { int x = 1 / 0; }", constant_folding=True)
        assert "t1 = 1 / 0" in texts(result)

    def test_modulo_by_a_literal_zero_is_left_unfolded(self):
        result = run_optimize("func m(): void { int x = 1 % 0; }", constant_folding=True)
        assert "t1 = 1 % 0" in texts(result)

    def test_disabled_toggle_leaves_the_instruction_untouched(self):
        result = run_optimize("func m(): void { int x = 3 + 4; }")
        assert "t1 = 3 + 4" in texts(result)

    def test_logged_with_the_right_pass_name(self):
        result = run_optimize("func m(): void { int x = 3 + 4; }", constant_folding=True)
        assert any(c.pass_name == "constant-folding" for c in result.log)


# ---------------------------------------------------------------------------
# Constant propagation
# ---------------------------------------------------------------------------


class TestConstantPropagation:
    """Substituting a place's known-constant value into its later uses."""

    def test_propagates_into_a_later_binary_operand(self):
        result = run_optimize(
            "func m(): void { int a = 5; int b = a + 1; }", constant_propagation=True
        )
        assert "t1 = 5 + 1" in texts(result)

    def test_propagates_into_a_print(self):
        result = run_optimize("func m(): void { int a = 5; print(a); }", constant_propagation=True)
        assert "print 5" in texts(result)

    def test_propagates_into_a_call_argument(self):
        result = run_optimize(
            "func f(int n): void { } func m(): void { int a = 5; f(a); }",
            constant_propagation=True,
        )
        assert "param 5" in texts(result)

    def test_does_not_propagate_past_a_redefinition(self):
        result = run_optimize(
            "func m(): void { int a = 5; a = 6; print(a); }", constant_propagation=True
        )
        assert "print 6" in texts(result)

    def test_does_not_propagate_a_non_constant_value(self):
        result = run_optimize(
            "func m(int n): void { int a = n; print(a); }", constant_propagation=True
        )
        assert "print a" in texts(result)

    def test_resets_at_a_block_boundary(self):
        """Propagating across a branch without reaching-definitions analysis would be unsound."""
        result = run_optimize(
            "func m(bool c): void { int a = 5; if (c) { a = 1; } print(a); }",
            constant_propagation=True,
        )
        assert "print a" in texts(result)


# ---------------------------------------------------------------------------
# Common-subexpression elimination
# ---------------------------------------------------------------------------


class TestCommonSubexpressionElimination:
    """Reusing an earlier identical pure computation within one block."""

    def test_reuses_an_identical_computation(self):
        result = run_optimize(
            "func m(int a, int b): void { int y = a + b; int z = a + b; print(z); }",
            common_subexpression_elimination=True,
        )
        assert "t2 = t1" in texts(result)

    def test_does_not_reuse_after_an_operand_is_redefined(self):
        result = run_optimize(
            "func m(int a, int b): void { int y = a + b; a = 9; int z = a + b; print(z); }",
            common_subexpression_elimination=True,
        )
        assert "t2 = a + b" in texts(result)

    def test_does_not_reuse_across_a_block_boundary(self):
        result = run_optimize(
            "func m(bool c, int a, int b): void { "
            "int y = a + b; if (c) { int z = a + b; print(z); } }",
            common_subexpression_elimination=True,
        )
        assert "t2 = a + b" in texts(result)

    def test_disabled_toggle_leaves_the_duplicate_computed_twice(self):
        result = run_optimize(
            "func m(int a, int b): void { int y = a + b; int z = a + b; print(z); }"
        )
        assert "t2 = a + b" in texts(result)


# ---------------------------------------------------------------------------
# Dead-code elimination
# ---------------------------------------------------------------------------


class TestDeadCodeElimination:
    """Backward liveness over the whole CFG, not a per-block shortcut."""

    def test_removes_an_unused_assignment(self):
        result = run_optimize(
            "func m(): void { int unused = 42; print(1); }", dead_code_elimination=True
        )
        assert not any("unused" in t for t in texts(result))

    def test_never_removes_a_call_even_when_its_result_is_unused(self):
        """A call may have side effects; deleting it would be unsound."""
        result = run_optimize(
            "func f(): int { print(99); return 1; } func m(): void { int x = f(); }",
            dead_code_elimination=True,
        )
        assert any("call f" in t for t in texts(result))

    def test_never_removes_print_param_return_or_a_label(self):
        result = run_optimize("func m(): void { print(1); }", dead_code_elimination=True)
        assert texts(result) == ["m:", "print 1", "return"]

    def test_cascades_within_one_backward_sweep(self):
        """`b` only existed to compute `a`; once `a` is dead, so is `b`."""
        result = run_optimize(
            "func m(int n): void { int a = n + 1; int b = a + 1; print(n); }",
            dead_code_elimination=True,
        )
        assert texts(result) == ["m:", "print n", "return"]

    def test_disabled_toggle_leaves_dead_code_in_place(self):
        result = run_optimize("func m(): void { int unused = 42; print(1); }")
        assert any("unused" in t for t in texts(result))


class TestSoundness:
    """Correctness invariants a local approximation would violate."""

    def test_a_definition_used_only_in_a_successor_block_survives(self):
        """The canonical case a block-local DCE gets wrong."""
        result = run_optimize(
            "func m(bool c): void { int a = 1; if (c) { } print(a); }",
            dead_code_elimination=True,
        )
        assert "a = 1" in texts(result)

    def test_a_definition_used_only_inside_a_loop_body_survives(self):
        result = run_optimize(
            "func m(): void { int i = 0; while (i < 3) { print(i); i = i + 1; } }",
            dead_code_elimination=True,
        )
        assert any(t == "i = 0" for t in texts(result))

    def test_a_value_live_only_through_a_loops_back_edge_survives(self):
        """`t` feeds `b` on the next iteration, reached only via the back edge."""
        result = run_optimize(
            "func fib(int n): int { "
            "int a = 0; int b = 1; int i = 0; "
            "while (i < n) { int t = a + b; a = b; b = t; i = i + 1; } "
            "return a; }",
            dead_code_elimination=True,
        )
        assert any(t == "t = t2" for t in texts(result))
        assert any(t == "b = t" for t in texts(result))


# ---------------------------------------------------------------------------
# Pass interaction
# ---------------------------------------------------------------------------


class TestPassInteraction:
    """Folding, propagation and CSE feed each other; DCE cleans up after."""

    def test_propagation_enables_folding(self):
        result = run_optimize(
            "func m(): void { int a = 3; int b = 4; int c = a + b; print(c); }",
            constant_folding=True,
            constant_propagation=True,
        )
        assert "print 7" in texts(result)

    def test_double_fold_cascade_matches_the_demo_programs_comment(self):
        """`(3 + 4) * (3 + 4)` folds to 49 without needing CSE -- each term folds alone."""
        result = run_optimize(
            "func m(): void { int x = (3 + 4) * (3 + 4); print(x); }",
            constant_folding=True,
            constant_propagation=True,
        )
        assert "print 49" in texts(result)

    def test_full_pipeline_collapses_a_fully_constant_computation_to_one_print(self):
        result = run_optimize(
            "func m(): void { int x = (3 + 4) * (3 + 4); print(x); }",
            constant_folding=True,
            constant_propagation=True,
            dead_code_elimination=True,
        )
        assert texts(result) == ["m:", "print 49", "return"]

    def test_cse_then_dce_removes_the_original_now_unused_copy(self):
        result = run_optimize(
            "func m(int a, int b): void { int y = a + b; int z = a + b; print(z); }",
            common_subexpression_elimination=True,
            dead_code_elimination=True,
        )
        assert not any(t.startswith("y") for t in texts(result))


# ---------------------------------------------------------------------------
# Statistics and the audit log
# ---------------------------------------------------------------------------


class TestStatsAndLog:
    """What the Optimization tab's counters and change log read."""

    def test_instruction_counts_reflect_what_was_removed(self):
        result = run_optimize(
            "func m(): void { int unused = 42; print(1); }", dead_code_elimination=True
        )
        assert result.stats.instructions_before == 4
        assert result.stats.instructions_after == 3
        assert result.stats.removed == 1

    def test_per_pass_counts_are_keyed_by_pass_name(self):
        result = run_optimize(
            "func m(): void { int unused = 42; print(1); }", dead_code_elimination=True
        )
        assert result.stats.per_pass == {"dead-code-elimination": 1}

    def test_no_toggles_enabled_leaves_the_program_bit_for_bit_unchanged(self):
        result = run_optimize("func m(): void { int unused = 42; print(1); }")
        assert result.stats.removed == 0
        assert result.log == []

    def test_blocks_are_rebuilt_for_the_optimised_program(self):
        result = run_optimize(
            "func m(bool c): void { if (c) { print(1); } else { print(2); } }",
            constant_folding=True,
        )
        assert len(result.blocks) == 4


# ---------------------------------------------------------------------------
# Acceptance: the canonical demo program
# ---------------------------------------------------------------------------


class TestDemoProgram:
    """Every inline comment in the demo program names an expected effect."""

    def test_default_toggles_match_the_programs_own_comments(self):
        result = run_optimize(
            DEMO_PROGRAM,
            constant_folding=True,
            constant_propagation=True,
            dead_code_elimination=True,
        )
        rendered = texts(result)

        # "folds to 10" and "CSE target" (49 is reachable by folding alone).
        assert "param 10" in rendered
        assert "print 49" in rendered
        # "eliminated by DCE"
        assert not any("unused" in t for t in rendered)
        # fib's loop survives untouched -- nothing in it is foldable,
        # propagable within a block, or dead.
        assert "ifFalse t1 goto fib_L1" in rendered
        assert "goto fib_L0" in rendered

    def test_removes_exactly_the_instructions_the_comments_predict(self):
        result = run_optimize(
            DEMO_PROGRAM,
            constant_folding=True,
            constant_propagation=True,
            dead_code_elimination=True,
        )
        # unused, limit, t4, x, t7, t5, t6: seven instructions whose only
        # purpose was a computation fully subsumed by propagation+folding.
        assert result.stats.removed == 7
        assert result.stats.instructions_after == result.stats.instructions_before - 7

    def test_output_is_unaffected_by_optimisation(self):
        """With every pass enabled, both prints still resolve to a value."""
        result = run_optimize(
            DEMO_PROGRAM,
            constant_folding=True,
            constant_propagation=True,
            common_subexpression_elimination=True,
            dead_code_elimination=True,
        )
        rendered = texts(result)
        assert "print 49" in rendered
        assert any(t.startswith("print t") for t in rendered)  # fib(10)'s result
