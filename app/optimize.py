"""Optimisation: fold, propagate, eliminate common subexpressions, and prune dead code.

Phase 5 of the pipeline, and the four toggles on the Optimization tab. Each
pass is the textbook *local* (single-basic-block) version except dead-code
elimination, which is the textbook *global* (whole-CFG) version -- the two
are not symmetric by accident; see "Why DCE is global and the others are
local" below.

The four passes, and the order they run in
--------------------------------------------
**Constant folding** replaces an instruction whose operands are both already
literal constants with the literal result of computing it at compile time
(``t1 = 3 + 4`` becomes ``t1 = 7``). Division and modulo by a literal zero are
left unfolded -- that is a runtime error, not a compile-time constant, and
folding it would silently hide the trap the (eventual) VM is supposed to
raise.

**Constant propagation** tracks, within one basic block, which places are
currently known to hold a literal constant, and substitutes that literal
everywhere the place is read. It resets at every block boundary -- see below
for why.

**Common-subexpression elimination** tracks, within one basic block, which
pure computations have already been performed and are still valid (neither
operand has been reassigned since), and rewrites a repeated computation into
a copy of the place that already holds the answer.

**Dead-code elimination** removes a *pure* instruction (one of the three
kinds above, or a plain copy) whose result is never used on any path forward
from it, decided by a real backward liveness analysis over the whole
control-flow graph (Aho, Lam, Sethi & Ullman, *Compilers: Principles,
Techniques, and Tools*, section 8.5 and chapter 9's dataflow framework, not
the simplified "is it used later in this block" shortcut).

Folding, propagation and CSE are iterated together to a fixpoint before DCE
ever runs, because each pass can expose work for the other two: propagating
a constant into a binary op can make it foldable; folding can make a
duplicate computation's operands match an earlier one for CSE; CSE's copies
are exactly what propagation is for. Running each pass once, in a fixed
order, would miss cascades like ``x = (3 + 4) * (3 + 4)`` -- the repeated
``3 + 4`` is caught by CSE, and only *then* does propagation see two equal
operands to fold.

Why DCE is global and the others are local
-------------------------------------------
Propagating a constant, or reusing an earlier computation, *into* a later
instruction is only safe if nothing could have redefined the place in
between -- and across a branch or a loop, "in between" is exactly the kind of
question that needs real dataflow analysis (reaching definitions) to answer
soundly. Restricting both passes to one basic block sidesteps that question
entirely: within a straight-line block there is only one path, so "the last
assignment in this block" is unambiguously the only possible source. That is
the standard simplification an introductory compiler makes, and it is sound,
not merely convenient.

Dead-code elimination has no such escape: a value assigned in one block is
routinely consumed in a block reached through a branch or a loop. Judging it
"dead" using only the block it was assigned in would delete `a` in
``if (c) { a = 1; } print(a);`` after a hypothetical refactor, which is wrong
-- not an approximation, wrong. There is no local version of DCE that is both
simple and correct, so this module does the one that is actually correct: a
backward liveness fixpoint over the CFG the blocks already have, then a
single backward removal sweep per block (reusing :func:`app.tac.build_cfg`
precisely so this module never re-derives the leader/successor logic that
module already owns).

What is deliberately not done
------------------------------
A constant condition (``ifFalse true goto L``) is left as data -- simplifying
*it* away would mean deleting a branch and rewiring the control-flow graph,
which is a distinct optimisation (unreachable-code elimination from constant
propagation into control flow) from the four asked for here, not a free
side effect of any of them.
"""

from __future__ import annotations

from typing import Final

from app.literals import (
    format_literal,
    is_literal,
    parse_literal,
    truncating_divide,
    truncating_modulo,
)
from app.schemas import (
    BasicBlock,
    OptimizationChange,
    OptimizationResult,
    OptimizationStats,
    OptimizationToggles,
)
from app.tac import Instr, block_boundaries, build_cfg

#: Binary operators that take two numeric (or, for ==/!=, boolean) operands
#: and compute a value with no side effect.
_PURE_BINARY: Final[frozenset[str]] = frozenset(
    {"+", "-", "*", "/", "%", "<", ">", "<=", ">=", "==", "!="}
)

#: Prefix operators that take one operand and compute a value with no side effect.
_PURE_UNARY: Final[frozenset[str]] = frozenset({"uminus", "not"})

#: Every op whose result a *later* pass might legally delete, propagate past,
#: or reuse: plain copies plus every pure operator. Deliberately excludes
#: ``call`` -- a call may have side effects (it can itself ``print``), so it
#: is kept regardless of whether its result is read.
_DEFINING_OPS: Final[frozenset[str]] = _PURE_BINARY | _PURE_UNARY | {"="}

#: For each op, which attribute(s) hold a *place* that is read (as opposed to
#: a label name, a callee name, or an argument count, none of which are
#: variables and must never be treated as one by propagation/CSE/liveness).
_READ_SLOTS: Final[dict[str, tuple[str, ...]]] = {
    **dict.fromkeys(_PURE_BINARY, ("arg1", "arg2")),
    **dict.fromkeys(_PURE_UNARY, ("arg1",)),
    "=": ("arg1",),
    "if": ("arg1",),
    "ifFalse": ("arg1",),
    "return": ("arg1",),
    "print": ("arg1",),
    "param": ("arg1",),
}

#: For each op, the attribute holding the place it defines, if any. ``call``
#: defines ``result`` only when its callee is non-``void`` (checked at the
#: call site: ``result`` is simply ``None`` on a void call's instruction).
_WRITE_SLOT: Final[dict[str, str]] = {
    **dict.fromkeys(_PURE_BINARY, "result"),
    **dict.fromkeys(_PURE_UNARY, "result"),
    "=": "result",
    "call": "result",
}

#: Passes run, in order, to a fixpoint before dead-code elimination.
_MAX_ROUNDS: Final[int] = 8


# ---------------------------------------------------------------------------
# Constants: parsing, formatting, folding
# ---------------------------------------------------------------------------


def _fold_binary(op: str, left: str, right: str) -> str | None:
    """Evaluate a binary operator over two literal operands.

    Integer ``/`` truncates toward zero and integer ``%`` follows the
    dividend's sign (:func:`app.literals.truncating_divide` and
    :func:`~app.literals.truncating_modulo`), matching the C-family
    semantics the rest of the project is written against -- and matching
    :mod:`app.vm`, which falls back to the exact same two functions for
    whatever a ``/``/``%`` this pass declines to fold. Division and modulo
    by a literal zero return ``None`` here: that is a runtime trap, not a
    compile-time value, and folding it would hide the error instead of
    computing it.

    Args:
        op: One of :data:`_PURE_BINARY`.
        left: The left operand's literal spelling.
        right: The right operand's literal spelling.

    Returns:
        The result's literal spelling, or ``None`` if it cannot be folded.
    """
    a = parse_literal(left)
    b = parse_literal(right)

    if op == "+":
        return format_literal(a + b)
    if op == "-":
        return format_literal(a - b)
    if op == "*":
        return format_literal(a * b)
    if op in ("/", "%"):
        try:
            result = truncating_divide(a, b) if op == "/" else truncating_modulo(a, b)
        except ZeroDivisionError:
            return None
        return format_literal(result)
    if op in ("<", ">", "<=", ">=", "==", "!="):
        comparisons: dict[str, bool] = {
            "<": a < b,
            ">": a > b,
            "<=": a <= b,
            ">=": a >= b,
            "==": a == b,
            "!=": a != b,
        }
        return format_literal(comparisons[op])
    return None  # pragma: no cover - every _PURE_BINARY member is handled above


def _fold_unary(op: str, operand: str) -> str | None:
    """Evaluate a prefix operator over one literal operand.

    Args:
        op: One of :data:`_PURE_UNARY`.
        operand: The operand's literal spelling.

    Returns:
        The result's literal spelling.
    """
    value = parse_literal(operand)
    if op == "uminus":
        return format_literal(-value)
    return format_literal(not value)  # op == "not"


# ---------------------------------------------------------------------------
# Constant folding
# ---------------------------------------------------------------------------


def _fold_pass(code: list[Instr], log: list[OptimizationChange]) -> bool:
    """Fold every instruction whose operands are already literal constants.

    Purely local to one instruction -- no block or dataflow reasoning needed,
    since a literal operand is valid everywhere it appears.

    Args:
        code: The instruction listing, mutated in place.
        log: Appended with one entry per instruction folded.

    Returns:
        True if any instruction changed.
    """
    changed = False
    for index, instr in enumerate(code):
        folded: str | None = None
        if instr.op in _PURE_BINARY and is_literal(instr.arg1) and is_literal(instr.arg2):
            folded = _fold_binary(instr.op, instr.arg1, instr.arg2)
        elif instr.op in _PURE_UNARY and is_literal(instr.arg1):
            folded = _fold_unary(instr.op, instr.arg1)

        if folded is not None:
            before = instr.text()
            instr.op, instr.arg1, instr.arg2 = "=", folded, None
            log.append(
                OptimizationChange(
                    pass_name="constant-folding",
                    index=index,
                    before=before,
                    after=instr.text(),
                    detail=f"computed at compile time: {folded}",
                )
            )
            changed = True
    return changed


# ---------------------------------------------------------------------------
# Constant propagation
# ---------------------------------------------------------------------------


def _propagate_pass(code: list[Instr], log: list[OptimizationChange]) -> bool:
    """Substitute known-constant places into their later uses, per block.

    Forward within each block: ``known`` maps a place to the literal it was
    last assigned, and is reset at every block boundary -- see the module
    docstring for why crossing a block would be unsound here.

    Args:
        code: The instruction listing, mutated in place.
        log: Appended with one entry per substitution.

    Returns:
        True if any instruction changed.
    """
    changed = False
    for start, end in block_boundaries(code):
        known: dict[str, str] = {}
        for index in range(start, end):
            instr = code[index]
            before: str | None = None
            for slot in _READ_SLOTS.get(instr.op, ()):
                token = getattr(instr, slot)
                if token is not None and token in known:
                    before = before or instr.text()
                    setattr(instr, slot, known[token])
                    changed = True
            if before is not None:
                log.append(
                    OptimizationChange(
                        pass_name="constant-propagation",
                        index=index,
                        before=before,
                        after=instr.text(),
                        detail="substituted a known compile-time constant",
                    )
                )

            write_slot = _WRITE_SLOT.get(instr.op)
            place = getattr(instr, write_slot) if write_slot else None
            if place is None:
                continue
            if instr.op == "=" and is_literal(instr.arg1):
                known[place] = instr.arg1
            else:
                known.pop(place, None)
    return changed


# ---------------------------------------------------------------------------
# Common-subexpression elimination
# ---------------------------------------------------------------------------


def _cse_pass(code: list[Instr], log: list[OptimizationChange]) -> bool:
    """Reuse an earlier identical computation, per block, via value numbering.

    ``available`` maps ``(op, arg1, arg2)`` to the place already holding that
    value. An instruction that redefines a place used as either key -- an
    operand of a cached computation, or the cached place itself -- must
    invalidate every entry depending on it *before* this instruction's own
    contribution is recorded, so a fresh definition never erases itself.

    Args:
        code: The instruction listing, mutated in place.
        log: Appended with one entry per computation reused.

    Returns:
        True if any instruction changed.
    """
    changed = False
    for start, end in block_boundaries(code):
        available: dict[tuple[str, str | None, str | None], str] = {}
        for index in range(start, end):
            instr = code[index]
            is_pure = instr.op in _PURE_BINARY or instr.op in _PURE_UNARY
            key = (instr.op, instr.arg1, instr.arg2) if is_pure else None
            existing = available.get(key) if key is not None else None

            write_slot = _WRITE_SLOT.get(instr.op)
            place = getattr(instr, write_slot) if write_slot else None
            if place is not None:
                stale = [k for k in available if place in (k[1], k[2]) or available[k] == place]
                for stale_key in stale:
                    del available[stale_key]

            if key is not None and existing is not None and existing != instr.result:
                before = instr.text()
                instr.op, instr.arg1, instr.arg2 = "=", existing, None
                log.append(
                    OptimizationChange(
                        pass_name="common-subexpression-elimination",
                        index=index,
                        before=before,
                        after=instr.text(),
                        detail=f"reused {existing}, already computed earlier in this block",
                    )
                )
                changed = True
            elif key is not None and instr.result is not None:
                available[key] = instr.result
    return changed


# ---------------------------------------------------------------------------
# Dead-code elimination (global liveness)
# ---------------------------------------------------------------------------


def _uses(instr: Instr) -> list[str]:
    """List the places one instruction reads, excluding literal operands.

    Args:
        instr: The instruction to inspect.

    Returns:
        Each read slot's value that is a place rather than a literal.
    """
    uses = []
    for slot in _READ_SLOTS.get(instr.op, ()):
        token = getattr(instr, slot)
        if token is not None and not is_literal(token):
            uses.append(token)
    return uses


def _definition(instr: Instr) -> str | None:
    """Return the place one instruction defines, if any.

    Args:
        instr: The instruction to inspect.

    Returns:
        The defined place, or ``None`` (including for a ``void`` call, whose
        ``result`` slot is itself already ``None``).
    """
    slot = _WRITE_SLOT.get(instr.op)
    return getattr(instr, slot) if slot else None


def _liveness_fixpoint(code: list[Instr], blocks: list[BasicBlock]) -> dict[str, set[str]]:
    """Compute live-in for every block by backward dataflow to a fixpoint.

    ``live-in[B] = uses(B) U (live-out[B] - defs(B))``, computed exactly
    rather than via block-level summary sets -- the backward scan over each
    block's own instructions already determines precisely which of its
    definitions a given live-out variable survives past, which a cruder
    "defs(B)" set membership test cannot distinguish when a place is both
    defined and later redefined within the same block.

    Args:
        code: The instruction listing.
        blocks: This listing's basic blocks, from :func:`app.tac.build_cfg`.

    Returns:
        Each block's live-in set, keyed by block id.
    """
    live_in: dict[str, set[str]] = {block.id: set() for block in blocks}
    bounds = {block.id: (block.leader, block.leader + len(block.instructions)) for block in blocks}
    successors = {block.id: block.successors for block in blocks}

    changed = True
    while changed:
        changed = False
        for block in blocks:
            live_out: set[str] = set()
            for succ_id in successors[block.id]:
                live_out |= live_in[succ_id]

            start, end = bounds[block.id]
            live = set(live_out)
            for index in range(end - 1, start - 1, -1):
                instr = code[index]
                definition = _definition(instr)
                if definition is not None:
                    live.discard(definition)
                live.update(_uses(instr))

            if live != live_in[block.id]:
                live_in[block.id] = live
                changed = True

    return live_in


def _dce_round(
    code: list[Instr], function_bounds: list[tuple[str, int]], log: list[OptimizationChange]
) -> tuple[list[Instr], bool]:
    """Run one backward liveness analysis and one removal sweep.

    Args:
        code: The instruction listing to shrink.
        function_bounds: Passed through to :func:`app.tac.build_cfg`.
        log: Appended with one entry per instruction removed.

    Returns:
        The surviving instructions, and whether anything was removed.
    """
    api = [instr.to_api(index) for index, instr in enumerate(code)]
    cfg = build_cfg(code, api, function_bounds)
    if not cfg.blocks:
        return code, False

    live_in = _liveness_fixpoint(code, cfg.blocks)
    successors = {block.id: block.successors for block in cfg.blocks}
    bounds = {
        block.id: (block.leader, block.leader + len(block.instructions)) for block in cfg.blocks
    }

    keep = [True] * len(code)
    for block in cfg.blocks:
        live: set[str] = set()
        for succ_id in successors[block.id]:
            live |= live_in[succ_id]

        start, end = bounds[block.id]
        for index in range(end - 1, start - 1, -1):
            instr = code[index]
            definition = _definition(instr)
            if instr.op in _DEFINING_OPS and definition is not None and definition not in live:
                keep[index] = False
                log.append(
                    OptimizationChange(
                        pass_name="dead-code-elimination",
                        index=index,
                        before=instr.text(),
                        after=None,
                        detail=f"'{definition}' is never used on any path from here",
                    )
                )
                continue
            if definition is not None:
                live.discard(definition)
            live.update(_uses(instr))

    survivors = [instr for instr, kept in zip(code, keep, strict=True) if kept]
    return survivors, any(not kept for kept in keep)


def _dce_pass(
    code: list[Instr], function_bounds: list[tuple[str, int]], log: list[OptimizationChange]
) -> list[Instr]:
    """Remove dead instructions to a fixpoint.

    One round can only make the *next* round's liveness analysis see fewer
    uses, never more, so iterating until nothing changes is what makes a
    cascade -- a dead definition's only use was itself in an instruction this
    round just removed -- fully cleaned up rather than left for a pass that
    never comes.

    Args:
        code: The instruction listing to shrink.
        function_bounds: Passed through to :func:`app.tac.build_cfg`.
        log: Appended with one entry per instruction removed, across every
            round.

    Returns:
        The surviving instructions.
    """
    current = code
    while True:
        current, removed_any = _dce_round(current, function_bounds, log)
        if not removed_any:
            return current


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def optimize(
    code: list[Instr],
    function_bounds: list[tuple[str, int]],
    toggles: OptimizationToggles,
) -> OptimizationResult:
    """Run the enabled optimisation passes and rebuild the CFG for the result.

    Folding, propagation and CSE iterate together to a fixpoint (each can
    expose work for the others; see the module docstring); dead-code
    elimination runs once, after, since it only removes instructions and so
    cannot create new folding/propagation/CSE opportunities among the ones
    that survive it.

    Args:
        code: The TAC from phase 4, as :class:`app.tac.Instr` -- not yet
            converted to the wire format, so this module can mutate it
            freely before anyone has rendered it.
        function_bounds: ``(name, start_index)`` for every function, from
            :class:`app.tac.TacGenerator`, needed to re-attribute blocks to
            functions after the instruction count changes.
        toggles: Which of the four passes to run.

    Returns:
        The optimised program: its TAC, its basic blocks, a change log, and
        the instruction-count statistics the report's results table reads.
    """
    original_count = len(code)
    current = [Instr(instr.op, instr.arg1, instr.arg2, instr.result) for instr in code]
    log: list[OptimizationChange] = []

    for _round in range(_MAX_ROUNDS):
        changed = False
        if toggles.constant_folding:
            changed = _fold_pass(current, log) or changed
        if toggles.constant_propagation:
            changed = _propagate_pass(current, log) or changed
        if toggles.common_subexpression_elimination:
            changed = _cse_pass(current, log) or changed
        if not changed:
            break

    if toggles.dead_code_elimination:
        current = _dce_pass(current, function_bounds, log)

    final_api = [instr.to_api(index) for index, instr in enumerate(current)]
    final_cfg = build_cfg(current, final_api, function_bounds)

    per_pass: dict[str, int] = {}
    for change in log:
        per_pass[change.pass_name] = per_pass.get(change.pass_name, 0) + 1

    stats = OptimizationStats(
        instructions_before=original_count,
        instructions_after=len(current),
        removed=original_count - len(current),
        per_pass=per_pass,
    )
    return OptimizationResult(tac=final_api, log=log, stats=stats, blocks=final_cfg.blocks)
