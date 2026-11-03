"""Target code generation: lower three-address code to stack-VM assembly.

Phase 6 of the pipeline. Takes the (optimised) TAC listing and produces the
flat assembly :mod:`app.vm` executes: one accumulator-free stack machine,
one operand stack doubling as the call stack, activation records laid out
exactly as a textbook describes them (Aho, Lam, Sethi & Ullman, *Compilers:
Principles, Techniques, and Tools*, section 7.2 and figure 7.9).

The instruction set
--------------------
``PUSH v`` / ``LOAD k`` / ``STORE k``
    Push a literal; push ``stack[fp + k]``; pop into ``stack[fp + k]``. ``k``
    is a signed frame-relative offset, resolved at compile time -- the VM
    never looks a name up by string.
``ADD SUB MUL DIV MOD NEG NOT LT GT LE GE EQ NE``
    Pop one or two operands, push the result. ``&&``/``||`` need no opcode
    here: :mod:`app.tac` already lowered them to jumps.
``JMP a`` / ``JMPT a`` / ``JMPF a``
    Unconditional jump; jump if the popped top of stack is truthy / falsy.
``CALL a`` / ``RET n`` / ``RETV n``
    See "Calling convention" below.
``PRINT``
    Pop one value, append its rendering to captured output.
``LABEL name`` / ``ENTER n``
    Pseudo-ops with no effect on the data they carry: ``LABEL`` exists so a
    jump or call target has an address and the listing has a name to show
    for it; ``ENTER n`` reserves ``n`` zero-initialised local slots when a
    function is entered (see "Frame layout").

Every jump and call target is resolved to a concrete instruction index
during code generation (the classical two-pass assembler technique:
generate symbolically with names, then backpatch once every label's address
is known) -- never at run time. ``app.vm`` therefore never performs a name
lookup; this module is where every name in the program is spent.

Frame layout
-------------
Each function's parameters and local variables (its declared variables and
every compiler temporary -- :mod:`app.optimize` and :mod:`app.tac` have
already made every one of them a distinct place, shadowing included) get a
permanent slot for the function's whole invocation, numbered relative to a
frame pointer ``fp``, exactly as a textbook activation record lays out::

    [ param 0 ] [ param 1 ] ... [ param N-1 ]   offsets -2-N .. -3
    [ control link (caller's fp) ]               offset -2
    [ return address ]                           offset -1
    fp ->
    [ local 0 ] [ local 1 ] ...                  offsets 0, 1, ...

A parameter's offset is negative, a local's is not, and both are computed
once, the first time this function's code mentions that place -- no two
distinct places are ever assigned the same offset (no register-allocation
style reuse), which is the simplest allocator that is still always correct.
This is also why a bare ``int x;`` with no initialiser is safe to read: its
slot still exists (``ENTER`` reserved it, zero-initialised) even though no
instruction ever defined it.

Calling convention
--------------------
A call's arguments are pushed by the TAC ``param`` instructions immediately
before it and are **not** moved again -- they already sit exactly where the
negative-offset formula above expects them, so ``CALL`` only has to push the
control link and the return address and jump. Tearing the frame down on
return needs to know how many parameters to discard along with it, which is
why ``RET``/``RETV`` carry the *current* function's parameter count as their
own operand rather than asking the VM to look it up: whoever generates a
function's body already knows its own signature.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from app.literals import is_literal
from app.schemas import AsmInstruction, TacInstruction

#: Binary TAC operators, each with no side effect, mapped to their mnemonic.
_BINARY_OPS: Final[dict[str, str]] = {
    "+": "ADD",
    "-": "SUB",
    "*": "MUL",
    "/": "DIV",
    "%": "MOD",
    "<": "LT",
    ">": "GT",
    "<=": "LE",
    ">=": "GE",
    "==": "EQ",
    "!=": "NE",
}

#: Prefix TAC operators mapped to their mnemonic.
_UNARY_OPS: Final[dict[str, str]] = {"uminus": "NEG", "not": "NOT"}

#: Ops whose operand is a jump/call target name, backpatched to an address
#: once every label in the program has been emitted.
_CONTROL_OPS: Final[frozenset[str]] = frozenset({"JMP", "JMPT", "JMPF", "CALL"})


@dataclass
class _Raw:
    """One assembly instruction before address resolution.

    Attributes:
        op: The mnemonic.
        operand: A resolved value (a literal, an offset, a parameter count)
            if this op needs no resolution; a symbolic label or function
            name, pending :meth:`CodeGenerator._resolve_addresses`, if it
            does.
        text: The human-readable rendering, fixed at emission time -- it
            names the symbol even after ``operand`` is overwritten with its
            resolved address, so the Target tab stays readable.
        comment: An optional annotation, e.g. the resolved address.
    """

    op: str
    operand: str | None = None
    text: str = ""
    comment: str | None = None

    def to_api(self, index: int) -> AsmInstruction:
        """Convert to the wire model the Target tab renders.

        Args:
            index: This instruction's position in the final listing.

        Returns:
            The corresponding :class:`~app.schemas.AsmInstruction`.
        """
        return AsmInstruction(
            index=index, op=self.op, operand=self.operand, text=self.text, comment=self.comment
        )


@dataclass
class CodegenResult:
    """Everything the Target tab and the VM need.

    Attributes:
        asm: The full assembly listing.
        entry: Index of ``main``'s first real instruction, or ``None`` if
            the program declares no function named ``main`` -- there is
            nothing to run, which :mod:`app.vm` reports rather than guesses
            at.
    """

    asm: list[AsmInstruction] = field(default_factory=list)
    entry: int | None = None


class CodeGenerator:
    """Translates one optimised TAC listing into one assembly listing.

    One generator handles one whole program; like :class:`app.tac.TacGenerator`
    it is not reusable, since the emitted assembly and the current
    function's slot table are instance state.
    """

    def __init__(self, function_params: dict[str, list[str]]) -> None:
        """Start with no assembly emitted.

        Args:
            function_params: Every function's parameters' places, in
                declaration order, keyed by function name -- from
                :attr:`app.tac.TacGenerator.function_params`. This is what
                tells a parameter's place apart from a local's, and what
                tells this generator where one function's TAC ends and the
                next begins (every function starts with a ``label``
                instruction whose name is a key of this mapping).
        """
        self.function_params = function_params
        self.asm: list[_Raw] = []
        self._slots: dict[str, int] = {}
        self._next_local = 0
        self._num_params = 0

    # -- entry point ----------------------------------------------------

    def generate(self, tac: list[TacInstruction]) -> CodegenResult:
        """Translate every function in the listing, then resolve addresses.

        Args:
            tac: The full (optimised) three-address-code listing.

        Returns:
            The resolved assembly listing and ``main``'s entry address.
        """
        index = 0
        total = len(tac)
        while index < total:
            if tac[index].op == "label" and tac[index].result in self.function_params:
                index = self._function(tac, index)
            else:  # pragma: no cover - app.tac always opens with a function label
                index += 1

        label_address = self._resolve_addresses()
        return CodegenResult(
            asm=[raw.to_api(i) for i, raw in enumerate(self.asm)],
            entry=label_address.get("main"),
        )

    # -- one function -----------------------------------------------------

    def _function(self, tac: list[TacInstruction], start: int) -> int:
        """Translate one function's TAC, from its label to the next one.

        Buffers the translated body separately from ``self.asm`` so that
        ``ENTER`` -- which must appear right after the function's label, but
        whose operand is the *total* number of local slots -- can be emitted
        once that total is known, without a second translation pass.

        Args:
            tac: The full TAC listing.
            start: Index of this function's ``label`` instruction.

        Returns:
            Index of the next function's ``label`` instruction, or
            ``len(tac)`` if this was the last function.
        """
        name = tac[start].result
        params = self.function_params.get(name, [])
        num_params = len(params)

        self._slots = {place: i - 2 - num_params for i, place in enumerate(params)}
        self._next_local = 0
        self._num_params = num_params

        body: list[_Raw] = []
        index = start + 1
        total = len(tac)
        while index < total and not (
            tac[index].op == "label" and tac[index].result in self.function_params
        ):
            self._translate(tac[index], body)
            index += 1

        self.asm.append(_Raw("LABEL", name, text=f"{name}:"))
        self.asm.append(
            _Raw(
                "ENTER",
                str(self._next_local),
                text=f"ENTER {self._next_local}",
                comment="reserve this function's local slots",
            )
        )
        self.asm.extend(body)
        return index

    # -- slots --------------------------------------------------------------

    def _slot_for(self, place: str) -> int:
        """Resolve a place to its frame offset, allocating one if new.

        The first mention of a place -- a read or a write, whichever comes
        first in program order -- is what allocates its slot. A bare
        ``int x;`` that is only ever read is exactly why a *read* must be
        able to allocate: nothing ever writes ``x``, so its slot would
        otherwise never exist (:class:`CodegenResult`'s ``ENTER`` would
        reserve one too few, and the ``LOAD`` would miss).

        Args:
            place: A TAC place: a parameter, a local variable, or a
                temporary.

        Returns:
            The place's frame-relative offset.
        """
        if place in self._slots:
            return self._slots[place]
        offset = self._next_local
        self._slots[place] = offset
        self._next_local += 1
        return offset

    def _push(self, token: str, buffer: list[_Raw]) -> None:
        """Emit the instruction that pushes one TAC operand's value.

        Args:
            token: A literal spelling, or a place name.
            buffer: The current function's body, appended to.
        """
        if is_literal(token):
            buffer.append(_Raw("PUSH", token, text=f"PUSH {token}"))
            return
        offset = self._slot_for(token)
        buffer.append(_Raw("LOAD", str(offset), text=f"LOAD {token}", comment=f"offset {offset}"))

    def _store(self, place: str, buffer: list[_Raw]) -> None:
        """Emit the instruction that pops into one TAC place's slot.

        Args:
            place: The place being written.
            buffer: The current function's body, appended to.
        """
        offset = self._slot_for(place)
        buffer.append(_Raw("STORE", str(offset), text=f"STORE {place}", comment=f"offset {offset}"))

    # -- one instruction ----------------------------------------------------

    def _translate(self, instr: TacInstruction, buffer: list[_Raw]) -> None:
        """Translate one TAC instruction into zero or more assembly ones.

        Args:
            instr: The instruction to translate.
            buffer: The current function's body, appended to.
        """
        op = instr.op

        if op == "label":
            buffer.append(_Raw("LABEL", instr.result, text=f"{instr.result}:"))
        elif op == "=":
            self._push(instr.arg1, buffer)
            self._store(instr.result, buffer)
        elif op in _BINARY_OPS:
            self._push(instr.arg1, buffer)
            self._push(instr.arg2, buffer)
            mnemonic = _BINARY_OPS[op]
            buffer.append(_Raw(mnemonic, text=mnemonic))
            self._store(instr.result, buffer)
        elif op in _UNARY_OPS:
            self._push(instr.arg1, buffer)
            mnemonic = _UNARY_OPS[op]
            buffer.append(_Raw(mnemonic, text=mnemonic))
            self._store(instr.result, buffer)
        elif op == "goto":
            buffer.append(_Raw("JMP", instr.result, text=f"JMP {instr.result}"))
        elif op == "if":
            self._push(instr.arg1, buffer)
            buffer.append(_Raw("JMPT", instr.result, text=f"JMPT {instr.result}"))
        elif op == "ifFalse":
            self._push(instr.arg1, buffer)
            buffer.append(_Raw("JMPF", instr.result, text=f"JMPF {instr.result}"))
        elif op == "param":
            self._push(instr.arg1, buffer)
        elif op == "call":
            buffer.append(_Raw("CALL", instr.arg1, text=f"CALL {instr.arg1}"))
            if instr.result is not None:
                self._store(instr.result, buffer)
        elif op == "return":
            self._translate_return(instr, buffer)
        elif op == "print":
            self._push(instr.arg1, buffer)
            buffer.append(_Raw("PRINT", text="PRINT"))

    def _translate_return(self, instr: TacInstruction, buffer: list[_Raw]) -> None:
        """Translate a ``return``, carrying the current function's arity.

        Args:
            instr: The ``return`` instruction to translate.
            buffer: The current function's body, appended to.
        """
        if instr.arg1 is None:
            buffer.append(_Raw("RET", str(self._num_params), text=f"RET {self._num_params}"))
            return
        self._push(instr.arg1, buffer)
        buffer.append(_Raw("RETV", str(self._num_params), text=f"RETV {self._num_params}"))

    # -- address resolution ---------------------------------------------

    def _resolve_addresses(self) -> dict[str, int]:
        """Backpatch every jump and call operand to a concrete address.

        The classical two-pass assembler technique: every ``LABEL``
        instruction's own index *is* its address, so the first pass simply
        records them; the second pass rewrites every control-transfer
        operand from the symbolic name it was emitted with to that address,
        preserving the name in ``comment`` for the listing.

        An operand with no matching label (possible only for a program with
        semantic errors already reaching this phase, e.g. a call to an
        undeclared function) resolves to ``-1`` rather than raising -- the
        VM traps on an out-of-range address if this ever actually runs,
        which is the right place for that program to fail, not here.

        Returns:
            Every label's and function's name mapped to its address, so the
            caller can look up ``main``'s entry without a second pass.
        """
        label_address: dict[str, int] = {
            raw.operand: index
            for index, raw in enumerate(self.asm)
            if raw.op == "LABEL" and raw.operand is not None
        }

        for raw in self.asm:
            if raw.op not in _CONTROL_OPS:
                continue
            target = raw.operand
            address = label_address.get(target)
            if address is None:
                raw.operand = "-1"
                raw.comment = f"unresolved target: {target!r}"
            else:
                raw.operand = str(address)
                raw.comment = f"-> {target} @ {address}"

        return label_address


def generate(tac: list[TacInstruction], function_params: dict[str, list[str]]) -> CodegenResult:
    """Generate stack-VM assembly for a program's optimised TAC.

    The module's entry point, mirroring :func:`app.tac.generate` and
    :func:`app.optimize.optimize`.

    Args:
        tac: The optimised three-address-code listing.
        function_params: Every function's parameters' places, in
            declaration order, keyed by function name.

    Returns:
        The resolved assembly listing and ``main``'s entry address.
    """
    return CodeGenerator(function_params).generate(tac)
