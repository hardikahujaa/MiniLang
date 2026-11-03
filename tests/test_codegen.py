"""Tests for :mod:`app.codegen`.

Organised by instruction shape (one TAC op at a time), then by frame layout
(parameters negative, locals non-negative, shadowed places never colliding),
then by address resolution, then the demo program as the acceptance test.
"""

from __future__ import annotations

from app.codegen import CodegenResult
from app.codegen import generate as generate_asm
from app.lexer import tokenize
from app.optimize import optimize
from app.parser import parse
from app.samples import DEMO_PROGRAM
from app.schemas import AsmInstruction, OptimizationToggles
from app.semantic import analyse
from app.tac import generate as generate_tac


def compile_asm(source: str, **enabled: bool) -> CodegenResult:
    """Lex, parse, analyse, translate, optimise and generate assembly.

    Args:
        source: MiniLang source text.
        **enabled: Optimisation passes to turn on (all default off, same
            convention as ``tests/test_optimize.py``'s helper).

    Returns:
        The generated assembly and ``main``'s entry address.
    """
    program = parse(tokenize(source).tokens)
    analyse(program)
    tac_result = generate_tac(program)
    toggles = OptimizationToggles(
        constant_folding=enabled.get("constant_folding", False),
        constant_propagation=enabled.get("constant_propagation", False),
        common_subexpression_elimination=enabled.get("common_subexpression_elimination", False),
        dead_code_elimination=enabled.get("dead_code_elimination", False),
    )
    optimized = optimize(tac_result.code, tac_result.function_bounds, toggles)
    return generate_asm(optimized.tac, tac_result.function_params)


def texts(result: CodegenResult) -> list[str]:
    """Render every instruction's text, in order.

    Args:
        result: A codegen result.

    Returns:
        One string per instruction.
    """
    return [instr.text for instr in result.asm]


def ops(result: CodegenResult) -> list[str]:
    """Return every instruction's mnemonic, in order.

    Args:
        result: A codegen result.

    Returns:
        One mnemonic per instruction.
    """
    return [instr.op for instr in result.asm]


def by_text_prefix(result: CodegenResult, prefix: str) -> AsmInstruction:
    """Find the one instruction whose text starts with a prefix.

    Args:
        result: A codegen result.
        prefix: The text prefix to search for.

    Returns:
        The matching instruction.
    """
    return next(instr for instr in result.asm if instr.text.startswith(prefix))


# ---------------------------------------------------------------------------
# One instruction at a time
# ---------------------------------------------------------------------------


class TestInstructionShapes:
    """Each TAC op becomes the textbook stack-machine sequence."""

    def test_copy_pushes_then_stores(self):
        result = compile_asm("func m(): void { int x = 5; }")
        assert ops(result) == ["LABEL", "ENTER", "PUSH", "STORE", "RET"]

    def test_binary_op_pushes_both_operands_then_combines_then_stores(self):
        # `int x = 1 + 2` is two TAC steps (compute into a temp, copy the
        # temp into x), so PUSH/PUSH/ADD is immediately followed by a STORE
        # into the temp's own slot, not yet into x.
        result = compile_asm("func m(): void { int x = 1 + 2; }")
        assert ops(result)[:6] == ["LABEL", "ENTER", "PUSH", "PUSH", "ADD", "STORE"]

    def test_every_binary_operator_has_a_mnemonic(self):
        source = (
            "func m(): void { "
            "int a=1+1; int b=1-1; int c=1*1; int d=1/1; int e=1%1; "
            "bool f=1<1; bool g=1>1; bool h=1<=1; bool i=1>=1; "
            "bool j=1==1; bool k=1!=1; }"
        )
        result = compile_asm(source)
        for mnemonic in ("ADD", "SUB", "MUL", "DIV", "MOD", "LT", "GT", "LE", "GE", "EQ", "NE"):
            assert mnemonic in ops(result), mnemonic

    def test_unary_minus_is_neg(self):
        result = compile_asm("func m(): void { int x = -5; }")
        assert "NEG" in ops(result)

    def test_unary_not_is_not_mnemonic(self):
        result = compile_asm("func m(): void { bool x = !true; }")
        assert "NOT" in ops(result)

    def test_goto_becomes_jmp(self):
        result = compile_asm("func m(): void { while (true) { } }")
        assert "JMP" in ops(result)

    def test_if_condition_becomes_jmpt_or_jmpf(self):
        # `if`/`ifFalse` in TAC both arise; while/if both lower to JMPF here
        # since app.tac only ever emits `ifFalse`, never `if`, for control
        # flow (`if` is reserved for the `||` short-circuit template).
        result = compile_asm("func m(): void { if (true) { } }")
        assert "JMPF" in ops(result)

    def test_or_short_circuit_becomes_jmpt(self):
        result = compile_asm("func m(): void { bool x = true || false; }")
        assert "JMPT" in ops(result)

    def test_print_pops_and_prints(self):
        result = compile_asm("func m(): void { print(1); }")
        assert ops(result) == ["LABEL", "ENTER", "PUSH", "PRINT", "RET"]

    def test_param_is_a_plain_push_with_no_opcode_of_its_own(self):
        result = compile_asm("func f(int n): void { } func m(): void { f(5); }")
        main_ops = [i.op for i in result.asm if i.index >= by_text_prefix(result, "m:").index]
        assert "PARAM" not in main_ops
        assert "PUSH" in main_ops
        assert "CALL" in main_ops

    def test_void_return_has_no_push(self):
        # app.tac always appends its own synthetic trailing `return` on top
        # of the explicit one, so a void function ends in two bare RETs.
        result = compile_asm("func m(): void { return; }")
        assert ops(result) == ["LABEL", "ENTER", "RET", "RET"]

    def test_value_return_pushes_then_retv(self):
        result = compile_asm("func m(): int { return 1; }")
        retv_index = ops(result).index("RETV")
        assert ops(result)[retv_index - 1] == "PUSH"

    def test_the_synthetic_trailing_return_is_also_translated(self):
        """Codegen must not choke on the extra synthetic RET app.tac always appends."""
        result = compile_asm("func m(): void { }")
        assert ops(result) == ["LABEL", "ENTER", "RET"]


# ---------------------------------------------------------------------------
# Frame layout
# ---------------------------------------------------------------------------


class TestFrameLayout:
    """Parameters get negative offsets; locals get non-negative ones."""

    def test_a_single_parameter_is_at_offset_minus_3(self):
        result = compile_asm("func m(int n): void { print(n); }")
        load = by_text_prefix(result, "LOAD n")
        assert load.operand == "-3"

    def test_two_parameters_are_at_minus_4_and_minus_3(self):
        result = compile_asm("func m(int a, int b): void { print(a); print(b); }")
        assert by_text_prefix(result, "LOAD a").operand == "-4"
        assert by_text_prefix(result, "LOAD b").operand == "-3"

    def test_locals_start_at_offset_zero(self):
        result = compile_asm("func m(): void { int x = 1; }")
        assert by_text_prefix(result, "STORE x").operand == "0"

    def test_locals_after_parameters_still_start_at_zero(self):
        """A parameter's negative offset must not consume a local slot number."""
        result = compile_asm("func m(int n): void { int x = 1; print(n); print(x); }")
        assert by_text_prefix(result, "LOAD n").operand == "-3"
        assert by_text_prefix(result, "STORE x").operand == "0"

    def test_each_distinct_place_gets_its_own_slot_once(self):
        result = compile_asm("func m(): void { int x = 1; int y = 2; print(x); print(y); }")
        stores = [i for i in result.asm if i.op == "STORE"]
        assert {i.operand for i in stores} == {"0", "1"}

    def test_enter_reserves_exactly_the_locals_used(self):
        result = compile_asm("func m(): void { int x = 1; int y = 2; }")
        enter = by_text_prefix(result, "ENTER")
        assert enter.operand == "2"

    def test_a_bare_declaration_that_is_only_read_still_gets_a_slot(self):
        """A place that is only ever read must still get a slot, allocated on that read."""
        result = compile_asm("func m(): void { int x; print(x); }")
        load = by_text_prefix(result, "LOAD x")
        assert load.operand == "0"
        assert by_text_prefix(result, "ENTER").operand == "1"

    def test_shadowed_places_get_distinct_slots(self):
        result = compile_asm(
            "func m(): void { int x = 1; if (true) { int x = 2; print(x); } print(x); }"
        )
        assert by_text_prefix(result, "STORE x").operand == "0"
        # app.tac names the shadowed declaration "x~1"; codegen must treat
        # it as a wholly different place, not alias it onto slot 0.
        shadow_store = next(i for i in result.asm if i.op == "STORE" and "x~1" in i.text)
        assert shadow_store.operand == "1"

    def test_two_functions_do_not_share_slot_numbering(self):
        """Each function's frame is independent, so slot numbering restarts per function."""
        result = compile_asm("func f(): void { int y = 9; } func m(): void { int x = 1; f(); }")
        f_section = [i for i in result.asm if i.index >= by_text_prefix(result, "f:").index]
        f_store = next(i for i in f_section if i.op == "STORE")
        assert f_store.operand == "0"


# ---------------------------------------------------------------------------
# Address resolution
# ---------------------------------------------------------------------------


class TestAddressResolution:
    """Every jump and call target becomes a concrete instruction index."""

    def test_entry_is_mains_label_index(self):
        result = compile_asm("func main(): void { }")
        assert result.entry == 0
        assert result.asm[0].op == "LABEL"
        assert result.asm[0].operand == "main"

    def test_entry_is_none_without_a_main_function(self):
        result = compile_asm("func f(): void { }")
        assert result.entry is None

    def test_a_call_resolves_to_the_calees_label_index(self):
        result = compile_asm("func f(): void { } func main(): void { f(); }")
        call = by_text_prefix(result, "CALL f")
        f_label = by_text_prefix(result, "f:")
        assert call.operand == str(f_label.index)

    def test_a_backward_jump_resolves_correctly(self):
        """Resolution must not assume every jump target is forward."""
        result = compile_asm("func main(): void { while (true) { } }")
        jmp = next(i for i in result.asm if i.op == "JMP")
        target = result.asm[int(jmp.operand)]
        assert target.op == "LABEL"
        assert target.index < jmp.index

    def test_a_forward_jump_resolves_correctly(self):
        result = compile_asm("func main(): void { if (true) { print(1); } }")
        jmpf = next(i for i in result.asm if i.op == "JMPF")
        target = result.asm[int(jmpf.operand)]
        assert target.op == "LABEL"
        assert target.index > jmpf.index

    def test_call_to_an_undeclared_function_resolves_to_a_sentinel_not_a_crash(self):
        """A program with a semantic error reaching codegen must degrade to a sentinel."""
        result = compile_asm("func main(): void { ghost(); }")
        call = by_text_prefix(result, "CALL ghost")
        assert call.operand == "-1"
        assert "unresolved" in (call.comment or "")


# ---------------------------------------------------------------------------
# Acceptance: the canonical demo program
# ---------------------------------------------------------------------------


class TestDemoProgram:
    """The shipped demo must generate a complete, well-formed listing."""

    def test_generates_without_error(self):
        result = compile_asm(DEMO_PROGRAM)
        assert result.asm
        assert result.entry is not None

    def test_every_control_transfer_resolves_to_a_real_instruction(self):
        result = compile_asm(
            DEMO_PROGRAM,
            constant_folding=True,
            constant_propagation=True,
            dead_code_elimination=True,
        )
        for instr in result.asm:
            if instr.op in ("JMP", "JMPT", "JMPF", "CALL"):
                address = int(instr.operand)
                assert 0 <= address < len(result.asm), instr.text

    def test_fib_is_called_with_its_folded_argument(self):
        result = compile_asm(
            DEMO_PROGRAM,
            constant_folding=True,
            constant_propagation=True,
            dead_code_elimination=True,
        )
        call = by_text_prefix(result, "CALL fib")
        # The PUSH right before CALL is the (folded) argument, 10.
        pre_call = result.asm[call.index - 1]
        assert pre_call.op == "PUSH"
        assert pre_call.operand == "10"
