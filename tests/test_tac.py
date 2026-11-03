"""Tests for :mod:`app.tac`.

Organised by what a reader would check against a textbook: that each
expression and statement form produces exactly the instruction sequence the
Dragon Book would show for it, that leaders are found by the classical three
rules (plus the deliberate fourth, documented in the module), and that the
resulting basic blocks link into a control-flow graph with the right edges
and labels. The demo program is the acceptance test -- the same program the
Semantic tab's tests use, so a regression here shows up next to a regression
there.
"""

from __future__ import annotations

from app.lexer import tokenize
from app.parser import parse
from app.samples import DEMO_PROGRAM
from app.schemas import BasicBlock, TacInstruction
from app.semantic import analyse
from app.tac import TacResult, generate


def compile_tac(source: str) -> TacResult:
    """Lex, parse, analyse and translate a source string to TAC.

    Args:
        source: MiniLang source text.

    Returns:
        The generated three-address code, blocks and CFG.
    """
    program = parse(tokenize(source).tokens)
    analyse(program)
    return generate(program)


def texts(result: TacResult) -> list[str]:
    """Render every instruction's pretty-printed text, in order.

    Args:
        result: A generation result.

    Returns:
        One string per instruction.
    """
    return [instr.text for instr in result.instructions]


def ops(result: TacResult) -> list[str]:
    """Return every instruction's op, in order.

    Args:
        result: A generation result.

    Returns:
        One op string per instruction.
    """
    return [instr.op for instr in result.instructions]


def block_by_id(result: TacResult, block_id: str) -> BasicBlock:
    """Look a basic block up by id.

    Args:
        result: A generation result.
        block_id: The block's id, e.g. ``"B0"``.

    Returns:
        The matching block.
    """
    return next(b for b in result.blocks if b.id == block_id)


def block_texts(block: BasicBlock) -> list[str]:
    """Render a block's instructions as pretty-printed text.

    Args:
        block: A basic block.

    Returns:
        One string per instruction in the block.
    """
    return [instr.text for instr in block.instructions]


# ---------------------------------------------------------------------------
# Expression translation: value code
# ---------------------------------------------------------------------------


class TestValueExpressions:
    """Arithmetic, relational, equality and unary operators."""

    def test_literal_needs_no_temporary(self):
        result = compile_tac("func m(): void { int x = 5; }")
        assert texts(result) == ["m:", "x = 5", "return"]

    def test_float_and_bool_literals_render_their_spelling(self):
        result = compile_tac("func m(): void { float f = 1.5; bool b = true; }")
        assert "f = 1.5" in texts(result)
        assert "b = true" in texts(result)

    def test_binary_arithmetic_is_one_instruction(self):
        result = compile_tac("func m(): void { int x = 1 + 2; }")
        assert "t1 = 1 + 2" in texts(result)
        assert "x = t1" in texts(result)

    def test_operands_are_evaluated_before_the_operator_that_combines_them(self):
        result = compile_tac("func m(): void { int x = (1 + 2) * (3 + 4); }")
        assert texts(result)[:4] == ["m:", "t1 = 1 + 2", "t2 = 3 + 4", "t3 = t1 * t2"]

    def test_relational_and_equality_operators_produce_value_code(self):
        result = compile_tac("func m(): void { bool a = 1 < 2; bool b = 1 == 2; }")
        assert "a = t1" in texts(result)
        assert "t1 = 1 < 2" in texts(result)
        assert "t2 = 1 == 2" in texts(result)

    def test_unary_minus_is_uminus(self):
        result = compile_tac("func m(): void { int x = -5; }")
        assert ops(result)[1] == "uminus"
        assert "t1 = -5" in texts(result)

    def test_unary_not_is_not(self):
        result = compile_tac("func m(): void { bool x = !true; }")
        assert ops(result)[1] == "not"
        assert "t1 = !true" in texts(result)


# ---------------------------------------------------------------------------
# Short-circuit boolean evaluation
# ---------------------------------------------------------------------------


class TestShortCircuit:
    """``&&`` and ``||`` are the one place this module uses jumping code."""

    def test_and_stops_on_a_false_left_operand(self):
        result = compile_tac("func m(): void { bool x = false && true; }")
        assert texts(result) == [
            "m:",
            "ifFalse false goto m_L0",
            "t1 = true",
            "goto m_L1",
            "m_L0:",
            "t1 = false",
            "m_L1:",
            "x = t1",
            "return",
        ]

    def test_or_stops_on_a_true_left_operand(self):
        result = compile_tac("func m(): void { bool x = true || false; }")
        assert texts(result) == [
            "m:",
            "if true goto m_L0",
            "t1 = false",
            "goto m_L1",
            "m_L0:",
            "t1 = true",
            "m_L1:",
            "x = t1",
            "return",
        ]

    def test_right_operand_of_and_is_gated_by_the_left_operands_test(self):
        """The call to g() is translated after the test on f()'s result, never before it."""
        result = compile_tac(
            "func f(): bool { return false; } "
            "func g(): bool { return true; } "
            "func m(): void { bool x = f() && g(); }"
        )
        main_texts = [t for b in result.blocks if b.function == "m" for t in block_texts(b)]
        gate_position = next(i for i, t in enumerate(main_texts) if t.startswith("ifFalse"))
        call_g_position = next(i for i, t in enumerate(main_texts) if "call g" in t)
        assert call_g_position > gate_position


# ---------------------------------------------------------------------------
# Control-flow statement translation
# ---------------------------------------------------------------------------


class TestControlFlowTranslation:
    """if/else and while against the exact textbook templates."""

    def test_if_without_else(self):
        result = compile_tac("func m(): void { if (true) { print(1); } }")
        assert texts(result) == [
            "m:",
            "ifFalse true goto m_L0",
            "print 1",
            "m_L0:",
            "return",
        ]

    def test_if_with_else(self):
        result = compile_tac("func m(): void { if (true) { print(1); } else { print(2); } }")
        assert texts(result) == [
            "m:",
            "ifFalse true goto m_L0",
            "print 1",
            "goto m_L1",
            "m_L0:",
            "print 2",
            "m_L1:",
            "return",
        ]

    def test_while_loop(self):
        result = compile_tac("func m(): void { while (true) { print(1); } }")
        assert texts(result) == [
            "m:",
            "m_L0:",
            "ifFalse true goto m_L1",
            "print 1",
            "goto m_L0",
            "m_L1:",
            "return",
        ]

    def test_labels_restart_per_function(self):
        result = compile_tac("func f(): void { if (true) { } } func g(): void { if (true) { } }")
        assert "f_L0:" in texts(result)
        assert "g_L0:" in texts(result)


# ---------------------------------------------------------------------------
# Shadowing
# ---------------------------------------------------------------------------


class TestShadowing:
    """A name redeclared in a nested scope must not alias the outer one."""

    def test_an_inner_declaration_gets_its_own_place(self):
        result = compile_tac(
            "func m(): void { int x = 1; if (true) { int x = 2; print(x); } print(x); }"
        )
        assert texts(result) == [
            "m:",
            "x = 1",
            "ifFalse true goto m_L0",
            "x~1 = 2",
            "print x~1",
            "m_L0:",
            "print x",
            "return",
        ]

    def test_the_outer_variable_is_unaffected_by_the_inner_assignment(self):
        """The bug this guards against: `print x` after the block reading `2`."""
        result = compile_tac("func m(): void { int x = 1; if (true) { int x = 2; } print(x); }")
        assert texts(result)[-2] == "print x"

    def test_a_parameter_can_be_shadowed_by_a_nested_declaration(self):
        result = compile_tac("func m(int n): void { if (true) { int n = 9; print(n); } print(n); }")
        texts_ = texts(result)
        assert "n~1 = 9" in texts_
        assert "print n~1" in texts_
        assert texts_[-2] == "print n"

    def test_shadowing_in_the_else_branch_does_not_leak_into_the_then_branch(self):
        result = compile_tac(
            "func m(bool c): void { int x = 1; "
            "if (c) { print(x); } else { int x = 2; print(x); } }"
        )
        texts_ = texts(result)
        assert "print x" in texts_
        assert "print x~1" in texts_

    def test_sibling_blocks_each_shadowing_the_same_name_get_distinct_places(self):
        """Two unrelated blocks reusing a name must not collide with each other."""
        result = compile_tac(
            "func m(bool c): void { "
            "if (c) { int x = 1; print(x); } "
            "if (c) { int x = 2; print(x); } }"
        )
        texts_ = texts(result)
        assert "x = 1" in texts_
        assert "x~1 = 2" in texts_

    def test_the_same_name_in_different_functions_does_not_shadow(self):
        """Shadowing is a within-function concept; two functions' `x` are unrelated."""
        result = compile_tac("func f(): void { int x = 1; } func g(): void { int x = 2; }")
        assert "x = 1" in texts(result)
        assert "x = 2" in texts(result)
        assert not any("x~" in t for t in texts(result))

    def test_no_shadowing_leaves_names_untouched(self):
        """The common case must not be cluttered with suffixes it does not need."""
        result = compile_tac(DEMO_PROGRAM)
        assert not any("~" in t for t in texts(result))


# ---------------------------------------------------------------------------
# Functions, calls and void handling
# ---------------------------------------------------------------------------


class TestFunctionsAndCalls:
    """Entry labels, the param/call sequence, and the synthetic exit."""

    def test_each_function_opens_with_a_label_bearing_its_name(self):
        result = compile_tac("func f(): void { } func g(): void { }")
        assert texts(result)[0] == "f:"
        labels = [t for t in texts(result) if t.rstrip(":") in ("f", "g")]
        assert labels == ["f:", "g:"]

    def test_missing_return_gets_a_synthetic_one(self):
        result = compile_tac("func m(): void { }")
        assert texts(result) == ["m:", "return"]

    def test_explicit_return_still_gets_a_trailing_synthetic_one(self):
        """Every function ends in `return`, so blocks never bleed into the next one."""
        result = compile_tac("func m(): int { return 1; }")
        assert texts(result) == ["m:", "return 1", "return"]

    def test_call_emits_param_then_call(self):
        result = compile_tac(
            "func f(int a, int b): int { return a; } func m(): void { int x = f(1, 2); }"
        )
        texts_ = texts(result)
        assert "param 1" in texts_
        assert "param 2" in texts_
        assert any(t.startswith("t") and "= call f, 2" in t for t in texts_)

    def test_void_call_has_no_result_slot(self):
        result = compile_tac("func f(): void { } func m(): void { f(); }")
        assert "call f, 0" in texts(result)
        assert not any("= call f, 0" in t for t in texts(result))


# ---------------------------------------------------------------------------
# Leaders, basic blocks and the CFG
# ---------------------------------------------------------------------------


class TestBasicBlocksAndCfg:
    """The classical leader algorithm and the edges it implies."""

    def test_straight_line_code_is_one_block(self):
        result = compile_tac("func m(): void { int x = 1; int y = 2; }")
        assert len(result.blocks) == 1
        assert result.cfg.entry == "B0"
        assert result.cfg.exits == ["B0"]

    def test_if_else_produces_four_blocks_with_true_false_edges(self):
        result = compile_tac("func m(): void { if (true) { print(1); } else { print(2); } }")
        labels = {(e.source, e.target): e.label for e in result.cfg.edges}
        # B0: ifFalse -> B1 (then, true) / B2 (else, false)
        assert labels[("B0", "B1")] == "true"
        assert labels[("B0", "B2")] == "false"
        # The then-branch (B1) jumps over the else-branch to the join block;
        # the else-branch (B2) has no jump of its own and simply falls into
        # it, since the join label immediately follows.
        assert labels[("B1", "B3")] is None
        assert labels[("B2", "B3")] == "fall-through"

    def test_while_loop_has_a_back_edge(self):
        result = compile_tac("func m(): void { while (true) { print(1); } }")
        labels = {(e.source, e.target): e.label for e in result.cfg.edges}
        # B0: label m: -> falls into B1 (loop header)
        assert labels[("B0", "B1")] == "fall-through"
        # B1: ifFalse -> B2 (body, true) / B3 (exit, false)
        assert labels[("B1", "B2")] == "true"
        assert labels[("B1", "B3")] == "false"
        # B2 (body) loops back to B1 (header) unconditionally.
        assert labels[("B2", "B1")] is None

    def test_no_fallthrough_between_functions(self):
        """A void function with no return must not fall into the next function's block."""
        result = compile_tac("func f(): void { } func g(): void { }")
        f_block = next(b for b in result.blocks if b.function == "f")
        assert f_block.successors == []

    def test_every_block_is_attributed_to_its_function(self):
        result = compile_tac("func f(): void { if (true) { } } func g(): void { while (true) { } }")
        functions = {b.function for b in result.blocks}
        assert functions == {"f", "g"}

    def test_unreachable_block_after_an_always_returning_function(self):
        """The synthetic trailing return is its own unreachable block when every path already returns -- correct, and a free dead-code demonstration."""
        result = compile_tac("func m(): int { return 1; } func n(): void { }")
        m_blocks = [b for b in result.blocks if b.function == "m"]
        assert len(m_blocks) == 2
        assert all(b.successors == [] for b in m_blocks)


# ---------------------------------------------------------------------------
# Acceptance: the canonical demo program
# ---------------------------------------------------------------------------


class TestDemoProgram:
    """The shipped demo must translate to a sensible, well-formed CFG."""

    def test_demo_program_translates_without_error(self):
        result = compile_tac(DEMO_PROGRAM)
        assert all(isinstance(i, TacInstruction) for i in result.instructions)
        assert result.instructions, "the demo program is not empty"

    def test_fib_and_main_are_disconnected_components(self):
        result = compile_tac(DEMO_PROGRAM)
        fib_blocks = {b.id for b in result.blocks if b.function == "fib"}
        main_blocks = {b.id for b in result.blocks if b.function == "main"}
        for edge in result.cfg.edges:
            same_component = (edge.source in fib_blocks and edge.target in fib_blocks) or (
                edge.source in main_blocks and edge.target in main_blocks
            )
            assert same_component, f"edge {edge.source}->{edge.target} crosses functions"

    def test_fib_loop_has_the_textbook_while_shape(self):
        result = compile_tac(DEMO_PROGRAM)
        fib_texts = [t for b in result.blocks if b.function == "fib" for t in block_texts(b)]
        assert "ifFalse t1 goto fib_L1" in fib_texts
        assert any(t == "goto fib_L0" for t in fib_texts)

    def test_every_exit_block_ends_in_return(self):
        result = compile_tac(DEMO_PROGRAM)
        for block in result.blocks:
            if not block.successors:
                assert block.instructions[-1].op == "return"
