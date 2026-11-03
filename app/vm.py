"""The stack-VM interpreter: executes :mod:`app.codegen`'s assembly.

Phase 6's second half, and the Target tab's Run button. One Python list is
both the operand stack and the call stack -- the same role a real stack
machine's single stack plays -- addressed through two registers: ``pc``, the
index of the instruction about to execute, and ``fp``, the base of the
current function's activation record (see ``app.codegen``'s module
docstring for the exact frame layout this VM reads).

Every opcode's semantics mirrors a decision already made and justified
elsewhere, so this module does not re-argue them -- it is purely the
machine that carries them out:

* ``/`` and ``%`` use :func:`app.literals.truncating_divide` and
  :func:`~app.literals.truncating_modulo`, the same two functions
  :mod:`app.optimize` folds a constant ``/``/``%`` with. A division this VM
  ever performs is, by construction, one folding declined to -- usually
  because the divisor was a literal zero, in which case this raises the
  same ``ZeroDivisionError`` folding used to tell "cannot fold" from "folded
  to 0", except here it is not declined, it is the runtime trap MiniLang
  programs are documented to produce for that case.
* A value is printed via :func:`app.literals.format_literal`, the same
  rendering :mod:`app.tac` uses for a literal's TAC spelling -- what the
  Target tab's "Run" output shows for ``true`` is textually what the Syntax
  tab's AST would show for it too.

Calling convention
--------------------
``CALL`` pushes the caller's ``fp`` (the control link) and the return
address (the instruction after the ``CALL``), then jumps -- the arguments
themselves were already pushed by preceding ``PUSH``/``LOAD`` instructions
and are never moved again, exactly as ``app.codegen`` lays them out.
``RET``/``RETV`` read the control link and return address back out of the
frame *before* deleting it (``RETV`` additionally sets the return value
aside first, to replay onto the stack once the frame beneath it is gone),
then jump to the saved return address with the saved ``fp`` restored. This
same sequence is what ends the program: the return address pushed for
``main`` as though some caller had called it is a reserved program-counter
value no real instruction ever occupies (``len(asm)``), so ``main``'s own
``RET``/``RETV`` halts the machine with no special case in the interpreter
loop for "this was the last frame" -- it is just an ordinary return whose
caller happens to not exist.

Halting
--------
``halted`` answers one question only -- "did the step budget run out while
this was still going?" -- not "did it succeed". Three distinct outcomes
follow from that: the program returns from ``main`` (``halted=True``, no
``error``); it traps -- division by zero, a stack underflow from malformed
assembly, a jump to an address no label resolved to (``halted=True`` *with*
``error`` set, since a trap is a definite stop, not a program still
running when asked to stop looking); or it runs out of its step budget
while genuinely still executing (``halted=False``, no ``error`` -- this is
what catches a MiniLang program that really does loop forever, so the demo
server never hangs on one).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.literals import format_literal, parse_literal, truncating_divide, truncating_modulo
from app.schemas import AsmInstruction

#: Binary opcodes that pop two operands and push one result.
_BINARY: Final[frozenset[str]] = frozenset(
    {"ADD", "SUB", "MUL", "DIV", "MOD", "LT", "GT", "LE", "GE", "EQ", "NE"}
)


def _apply_binary(op: str, a: int | float | bool, b: int | float | bool) -> int | float | bool:
    """Apply one binary opcode to its two already-popped operands.

    Args:
        op: One of :data:`_BINARY`.
        a: The left operand (pushed, and so popped, first).
        b: The right operand.

    Returns:
        The result to push.

    Raises:
        ZeroDivisionError: For ``DIV``/``MOD`` with ``b == 0``.
    """
    if op == "ADD":
        return a + b
    if op == "SUB":
        return a - b
    if op == "MUL":
        return a * b
    if op == "DIV":
        return truncating_divide(a, b)
    if op == "MOD":
        return truncating_modulo(a, b)
    if op == "LT":
        return a < b
    if op == "GT":
        return a > b
    if op == "LE":
        return a <= b
    if op == "GE":
        return a >= b
    if op == "EQ":
        return a == b
    return a != b  # op == "NE"


@dataclass
class VmResult:
    """What one run of the VM produced.

    Attributes:
        output: Captured stdout: every ``PRINT``'s value, one per line.
        steps: Instructions retired.
        halted: False only if the step budget ran out while the program was
            still genuinely executing. True both for a clean return from
            ``main`` and for a trap -- ``error`` is what tells those two
            apart, not this field.
        error: The trap's message, or ``None`` for a clean halt or an
            exhausted budget.
    """

    output: str = ""
    steps: int = 0
    halted: bool = True
    error: str | None = None


def run(
    asm: list[AsmInstruction],
    entry: int | None,
    max_steps: int = 1_000_000,
    main_param_count: int = 0,
) -> VmResult:
    """Execute a program's assembly from ``main``'s entry address.

    Args:
        asm: The full resolved assembly listing.
        entry: Index of ``main``'s first instruction, from
            :class:`app.codegen.CodegenResult`, or ``None`` if the program
            declared no ``main``.
        max_steps: Instruction budget. Reached by a genuinely infinite
            MiniLang loop (or a program simply too large for a demo), this
            halts the interpreter rather than the server.
        main_param_count: How many parameters ``main`` declares. Nothing
            ever supplies them -- there is no caller -- so a nonzero count
            is rejected up front rather than left to read whatever garbage
            a negative frame offset happens to wrap around to on ``main``'s
            bootstrap frame, which has no params pushed for it to find.

    Returns:
        The captured output and how the run ended.
    """
    if entry is None:
        return VmResult(output="", steps=0, halted=True, error="no 'main' function to run")
    if main_param_count:
        return VmResult(
            output="", steps=0, halted=True, error="'main' must take no parameters to run"
        )

    halt_pc = len(asm)
    stack: list[int | float | bool] = [None, halt_pc]
    fp = 2
    pc = entry
    output: list[str] = []
    error: str | None = None
    steps = 0
    # True for every outcome except running out of the step budget below --
    # that is the one meaning `halted=False` carries (see VmResult's and
    # RunResponse's docstrings): a trap is still a definite stop, not a
    # program that is still running when asked to stop looking.
    halted = True

    while steps < max_steps:
        if pc == halt_pc:
            break
        if pc < 0 or pc >= len(asm):
            error = f"invalid program counter {pc}"
            break

        instr = asm[pc]
        op = instr.op
        next_pc = pc + 1

        try:
            if op == "PUSH":
                stack.append(parse_literal(instr.operand))
            elif op == "LOAD":
                stack.append(stack[fp + int(instr.operand)])
            elif op == "STORE":
                stack[fp + int(instr.operand)] = stack.pop()
            elif op in _BINARY:
                right = stack.pop()
                left = stack.pop()
                stack.append(_apply_binary(op, left, right))
            elif op == "NEG":
                stack.append(-stack.pop())
            elif op == "NOT":
                stack.append(not stack.pop())
            elif op == "JMP":
                next_pc = int(instr.operand)
            elif op == "JMPT":
                if stack.pop():
                    next_pc = int(instr.operand)
            elif op == "JMPF":
                if not stack.pop():
                    next_pc = int(instr.operand)
            elif op == "CALL":
                target = int(instr.operand)
                if target < 0 or target >= len(asm):
                    error = f"call to an unresolved address at instruction {pc} ({instr.text})"
                    break
                stack.append(fp)
                stack.append(pc + 1)
                fp = len(stack)
                next_pc = target
            elif op == "RET":
                num_params = int(instr.operand)
                saved_fp = stack[fp - 2]
                return_address = stack[fp - 1]
                del stack[fp - 2 - num_params :]
                fp = saved_fp
                next_pc = return_address
            elif op == "RETV":
                num_params = int(instr.operand)
                value = stack.pop()
                saved_fp = stack[fp - 2]
                return_address = stack[fp - 1]
                del stack[fp - 2 - num_params :]
                stack.append(value)
                fp = saved_fp
                next_pc = return_address
            elif op == "PRINT":
                output.append(format_literal(stack.pop()))
            elif op == "ENTER":
                stack.extend([0] * int(instr.operand))
            # "LABEL" is a pure no-op: falls through to pc = next_pc below.
        except ZeroDivisionError:
            error = f"division by zero at instruction {pc} ({instr.text})"
            break
        except IndexError:
            error = f"stack underflow at instruction {pc} ({instr.text})"
            break

        pc = next_pc
        steps += 1
    else:
        # The loop condition (steps < max_steps) went false on its own --
        # no break ran, so nothing halted the program; it simply ran out of
        # budget while still going, e.g. a genuine infinite MiniLang loop.
        halted = False

    newline = "\n" if output else ""
    return VmResult(output="\n".join(output) + newline, steps=steps, halted=halted, error=error)


def find_entry(asm: list[AsmInstruction], name: str = "main") -> int | None:
    """Find a named function's entry address directly from its assembly.

    Used by ``POST /run`` when it is handed assembly rather than source to
    compile: the entry address is not separate metadata that could drift
    out of sync with it, it is simply wherever that function's ``LABEL``
    pseudo-op already sits.

    Args:
        asm: A resolved assembly listing.
        name: The function to find. Defaults to ``"main"``.

    Returns:
        The address of ``name``'s first instruction, or ``None`` if no such
        label exists.
    """
    for index, instr in enumerate(asm):
        if instr.op == "LABEL" and instr.operand == name:
            return index
    return None
