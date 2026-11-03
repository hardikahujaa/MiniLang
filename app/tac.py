"""Intermediate code generation: three-address code, basic blocks, and the CFG.

Phase 4 of the pipeline. Lowers the (already type-checked) abstract syntax
tree into three-address code (TAC), partitions that code into basic blocks by
the classical leader algorithm, and links the blocks into a control-flow
graph. This is the textbook presentation throughout (Aho, Lam, Sethi & Ullman,
*Compilers: Principles, Techniques, and Tools*, chapters 6 and 8).

Three-address code
-------------------
Every instruction has at most two operands and one result, each either a
variable name, a compiler-generated temporary (``t1``, ``t2``, ...), or a
constant. Expression translation is the standard **synthesized-place**
scheme: each subexpression is translated bottom-up into code that leaves its
value in a *place*, and the parent combines its children's places.

``&&`` and ``||`` are the one place this module uses **jumping code** instead
of value code, because short-circuit evaluation is a semantic requirement, not
a style choice: ``f() && g()`` must not evaluate ``g()`` when ``f()`` is
false. The jump sequence still deposits a 0/1 result in a temporary, so the
*result* is a value like every other expression -- only the *evaluation* of
the right operand is conditional. This is exactly the "flow of control"
translation of boolean expressions a compilers course covers alongside the
value-based translation of everything else (Aho et al., section 6.6.6); using
jumps everywhere would be unnecessary, and using values everywhere would be
wrong.

Basic blocks and the CFG
-------------------------
**Leaders** (the first instruction of a basic block) are found by the
standard three rules: the first instruction of the program is a leader; the
target of any conditional or unconditional jump is a leader; and the
instruction immediately following a jump or a ``return`` is a leader. Every
``label`` instruction is also treated as a leader unconditionally -- a
superset of the strict rule that only costs an occasional singleton block, and
which is what keeps every function's code in blocks of its own even before
control flow is considered.

Each function's body is followed by a synthetic ``return`` (emitted even when
every path already returns explicitly). Two things fall out of that for free:
a ``void`` function with no explicit ``return`` gets one, and -- the reason it
is done unconditionally rather than only when needed -- every function's last
real block ends in a ``return``, so it can never grow a spurious fall-through
edge into the next function's first block. Without this, two functions placed
back to back in one flat instruction stream would appear connected in the CFG,
which would be wrong.

Multiple functions therefore show up as disconnected components of one
combined graph, exactly as they are logically disconnected (MiniLang has no
indirect calls, so ``call`` is deliberately not an interprocedural CFG edge --
this is the intraprocedural control-flow graph each function would have on
its own, the textbook unit of analysis for the optimizer in phase 5).
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

from app.ast_nodes import (
    AssignStmt,
    BinaryOp,
    Block,
    CallExpr,
    Expr,
    ExprStmt,
    FuncDecl,
    Identifier,
    IfStmt,
    Literal,
    PrintStmt,
    Program,
    ReturnStmt,
    Stmt,
    UnaryOp,
    VarDecl,
    WhileStmt,
)
from app.literals import format_literal
from app.schemas import BasicBlock, CfgEdge, CfgView, TacInstruction

#: The MiniLang spelling of the void type. Kept local rather than imported from
#: :mod:`app.semantic` so this module only depends on the tree it is handed.
_VOID: str = "void"

#: Three-address-code ops that unconditionally transfer control.
_JUMPS: frozenset[str] = frozenset({"goto", "if", "ifFalse"})


# ---------------------------------------------------------------------------
# One instruction
# ---------------------------------------------------------------------------


@dataclass
class Instr:
    """One three-address-code instruction, before it is handed to the API.

    The four fields are deliberately generic -- which operand slot carries
    what depends on :attr:`op` -- because a single shape that fits every kind
    of instruction is what lets the leader/block/edge logic stay op-agnostic.

    Attributes:
        op: The operation: an arithmetic/relational/logical operator such as
            ``"+"`` or ``"=="``; ``"uminus"`` or ``"not"`` for the two prefix
            operators; ``"="`` for a copy; ``"label"``, ``"goto"``, ``"if"``,
            ``"ifFalse"`` for control flow; ``"param"``, ``"call"``,
            ``"return"``, ``"print"`` for the rest.
        arg1: First operand, or the condition for ``if``/``ifFalse``, or the
            callee for ``call``, or the value for ``return``/``print``.
        arg2: Second operand, or the argument count for ``call``.
        result: Destination place, or the label name for ``label``, ``goto``,
            ``if`` and ``ifFalse``.
    """

    op: str
    arg1: str | None = None
    arg2: str | None = None
    result: str | None = None

    def text(self) -> str:
        """Render this instruction the way a textbook TAC listing would.

        Returns:
            A one-line human-readable rendering.
        """
        if self.op == "label":
            return f"{self.result}:"
        if self.op == "goto":
            return f"goto {self.result}"
        if self.op == "if":
            return f"if {self.arg1} goto {self.result}"
        if self.op == "ifFalse":
            return f"ifFalse {self.arg1} goto {self.result}"
        if self.op == "return":
            return f"return {self.arg1}" if self.arg1 is not None else "return"
        if self.op == "print":
            return f"print {self.arg1}"
        if self.op == "param":
            return f"param {self.arg1}"
        if self.op == "call":
            call = f"call {self.arg1}, {self.arg2}"
            return f"{self.result} = {call}" if self.result is not None else call
        if self.op == "=":
            return f"{self.result} = {self.arg1}"
        if self.op == "uminus":
            return f"{self.result} = -{self.arg1}"
        if self.op == "not":
            return f"{self.result} = !{self.arg1}"
        return f"{self.result} = {self.arg1} {self.op} {self.arg2}"

    def to_api(self, index: int) -> TacInstruction:
        """Convert to the wire model the ICG tab renders.

        Args:
            index: This instruction's position in the full listing.

        Returns:
            The corresponding :class:`~app.schemas.TacInstruction`.
        """
        return TacInstruction(
            index=index,
            op=self.op,
            arg1=self.arg1,
            arg2=self.arg2,
            result=self.result,
            text=self.text(),
        )


# ---------------------------------------------------------------------------
# Expression and statement translation
# ---------------------------------------------------------------------------


class TacGenerator:
    """Translates a type-checked AST into one flat list of :class:`Instr`.

    One generator handles one whole program; it is not reusable, because the
    emitted code, the temporary counter and the label counter are instance
    state. Temporaries are numbered across the whole program (``t1``, ``t2``,
    ...) while labels restart per function (``fib_L0``, ``main_L0``, ...) and
    are prefixed with the function's name, which is what keeps two functions'
    internal labels from colliding once their code is concatenated.

    **Shadowing gets a distinct place per declaration.** MiniLang legally
    permits ``int x = 1; if (c) { int x = 2; }`` -- two different variables
    that happen to share a source name, per :mod:`app.semantic`'s scope
    rules. If this generator used ``stmt.name`` as the TAC place directly,
    both would collide onto one name and the inner declaration would
    silently overwrite the outer variable for the rest of the function --
    wrong, not merely imprecise. So it keeps its own scope stack, built for
    exactly one purpose: the first time a source name is declared it keeps
    its bare spelling (``x``); the second time that same function declares
    that same name, in any scope, it gets a fresh place (``x~1``) instead.
    A read resolves the name through the active scope stack, innermost
    first, exactly as :class:`app.semantic.SymbolTable` resolves it -- a
    block pushes a scope on entry and pops it on exit, except a function's
    top-level body, which shares the one scope its parameters were declared
    in, matching ``_check_function``'s ``new_scope=False`` there.

    Attributes:
        code: The instructions emitted so far.
        function_bounds: ``(name, start_index)`` for every function, in
            declaration order -- how the caller learns which function each
            instruction (and so each basic block) belongs to.
        function_params: Every function's parameters' places, in
            declaration order, keyed by function name -- how a later phase
            (:mod:`app.codegen`) learns which places are parameters, since
            by the time it sees this listing a parameter's place looks
            exactly like any other (it is one: see the class docstring).
    """

    def __init__(self) -> None:
        """Start with no code emitted and both counters at zero."""
        self.code: list[Instr] = []
        self.function_bounds: list[tuple[str, int]] = []
        self.function_params: dict[str, list[str]] = {}
        self._temp_count = 0
        self._label_count = 0
        self._function_name = ""
        self._scopes: list[dict[str, str]] = []
        self._declaration_counts: dict[str, int] = {}

    # -- counters -----------------------------------------------------------

    def _new_temp(self) -> str:
        """Allocate a fresh temporary name.

        Returns:
            A name such as ``"t1"``, unique within the whole program.
        """
        self._temp_count += 1
        return f"t{self._temp_count}"

    def _new_label(self) -> str:
        """Allocate a fresh label name, scoped to the current function.

        Returns:
            A name such as ``"fib_L0"``, unique within the whole program.
        """
        label = f"{self._function_name}_L{self._label_count}"
        self._label_count += 1
        return label

    # -- scope ------------------------------------------------------------

    def _push_scope(self) -> None:
        """Open a new innermost scope."""
        self._scopes.append({})

    def _pop_scope(self) -> None:
        """Close the innermost scope, discarding its bindings."""
        self._scopes.pop()

    def _declare(self, source_name: str) -> str:
        """Bind a source name to a fresh place in the innermost scope.

        Args:
            source_name: The variable or parameter's spelling in source.

        Returns:
            The place to use for this declaration: ``source_name`` itself
            the first time this function declares that name, or
            ``f"{source_name}~{n}"`` for the ``n``-th time it shadows a name
            it (or an enclosing scope) already used.
        """
        count = self._declaration_counts.get(source_name, 0)
        place = source_name if count == 0 else f"{source_name}~{count}"
        self._declaration_counts[source_name] = count + 1
        self._scopes[-1][source_name] = place
        return place

    def _resolve(self, source_name: str) -> str:
        """Resolve a source name to the place its nearest declaration bound.

        Args:
            source_name: The variable's spelling in source.

        Returns:
            The bound place, searching outward from the innermost scope.
            Falls back to ``source_name`` unresolved if somehow no
            enclosing scope declared it -- keeps translation total rather
            than raising on a program semantic analysis already rejected.
        """
        for scope in reversed(self._scopes):
            if source_name in scope:
                return scope[source_name]
        return source_name

    def _emit(
        self,
        op: str,
        *,
        arg1: str | None = None,
        arg2: str | None = None,
        result: str | None = None,
    ) -> None:
        """Append one instruction to the code list.

        Args:
            op: The operation; see :class:`Instr`.
            arg1: First operand or equivalent slot.
            arg2: Second operand or equivalent slot.
            result: Destination place or equivalent slot.
        """
        self.code.append(Instr(op, arg1, arg2, result))

    # -- entry point ----------------------------------------------------

    def generate(self, program: Program) -> list[Instr]:
        """Translate every function in the program, in declaration order.

        Args:
            program: The root of the type-checked tree.

        Returns:
            The full flat instruction listing.
        """
        for func in program.functions:
            self._function(func)
        return self.code

    def _function(self, func: FuncDecl) -> None:
        """Translate one function: an entry label, its body, and a hard exit.

        The trailing ``return`` is unconditional -- see the module docstring
        for why that is what keeps functions from bleeding into each other in
        the control-flow graph.

        Args:
            func: The function to translate.
        """
        self._function_name = func.name
        self._label_count = 0
        self._declaration_counts = {}
        self.function_bounds.append((func.name, len(self.code)))
        self._emit("label", result=func.name)

        # One scope for parameters and the top level of the body, as in C
        # and as app.semantic._check_function resolves it -- see the class
        # docstring for why that is what a bare `stmt.name` could not do.
        self._push_scope()
        self.function_params[func.name] = [self._declare(param.name) for param in func.params]
        self._block(func.body, new_scope=False)
        self._pop_scope()

        self._emit("return")

    # -- statements -------------------------------------------------------

    def _block(self, block: Block, *, new_scope: bool = True) -> None:
        """Translate every statement of a block, in order.

        Args:
            block: The block to translate.
            new_scope: Whether this block opens a scope of its own. False
                only for a function body, whose scope is the one already
                holding its parameters.
        """
        if new_scope:
            self._push_scope()
        for statement in block.statements:
            self._statement(statement)
        if new_scope:
            self._pop_scope()

    def _statement(self, stmt: Stmt) -> None:
        """Dispatch one statement to its translator.

        Args:
            stmt: The statement to translate.
        """
        if isinstance(stmt, VarDecl):
            self._var_decl(stmt)
        elif isinstance(stmt, AssignStmt):
            self._assign(stmt)
        elif isinstance(stmt, IfStmt):
            self._if(stmt)
        elif isinstance(stmt, WhileStmt):
            self._while(stmt)
        elif isinstance(stmt, ReturnStmt):
            self._return(stmt)
        elif isinstance(stmt, PrintStmt):
            self._print(stmt)
        elif isinstance(stmt, ExprStmt):
            self._expression(stmt.expression)
        elif isinstance(stmt, Block):
            self._block(stmt)

    def _var_decl(self, stmt: VarDecl) -> None:
        """Translate a declaration: nothing for a bare one, else an assignment.

        A bare ``int x;`` declares storage but computes nothing, so it emits
        no instruction -- the variable's place simply comes into existence.

        The initialiser is translated *before* the name is declared, so
        ``int x = x;`` reads whatever ``x`` resolved to beforehand (an
        enclosing scope's, if any), matching how
        :meth:`app.semantic.Analyzer._var_decl` resolves the same program.

        Args:
            stmt: The declaration to translate.
        """
        if stmt.initializer is not None:
            value_place = self._expression(stmt.initializer)
            self._emit("=", arg1=value_place, result=self._declare(stmt.name))
        else:
            self._declare(stmt.name)

    def _assign(self, stmt: AssignStmt) -> None:
        """Translate an assignment to an existing variable.

        Args:
            stmt: The assignment to translate.
        """
        place = self._expression(stmt.value)
        self._emit("=", arg1=place, result=self._resolve(stmt.target))

    def _if(self, stmt: IfStmt) -> None:
        """Translate a conditional using the textbook if/if-else templates.

        ``if (E) S1`` ::

            <code for E, place p>
            ifFalse p goto Lend
            <code for S1>
            Lend:

        ``if (E) S1 else S2`` ::

            <code for E, place p>
            ifFalse p goto Lelse
            <code for S1>
            goto Lend
            Lelse:
            <code for S2>
            Lend:

        Args:
            stmt: The conditional to translate.
        """
        condition = self._expression(stmt.condition)
        if stmt.else_branch is None:
            end_label = self._new_label()
            self._emit("ifFalse", arg1=condition, result=end_label)
            self._block(stmt.then_branch)
            self._emit("label", result=end_label)
            return

        else_label = self._new_label()
        end_label = self._new_label()
        self._emit("ifFalse", arg1=condition, result=else_label)
        self._block(stmt.then_branch)
        self._emit("goto", result=end_label)
        self._emit("label", result=else_label)
        self._block(stmt.else_branch)
        self._emit("label", result=end_label)

    def _while(self, stmt: WhileStmt) -> None:
        """Translate a loop using the textbook template.

        ``while (E) S`` ::

            Lstart:
            <code for E, place p>
            ifFalse p goto Lend
            <code for S>
            goto Lstart
            Lend:

        Args:
            stmt: The loop to translate.
        """
        start_label = self._new_label()
        end_label = self._new_label()
        self._emit("label", result=start_label)
        condition = self._expression(stmt.condition)
        self._emit("ifFalse", arg1=condition, result=end_label)
        self._block(stmt.body)
        self._emit("goto", result=start_label)
        self._emit("label", result=end_label)

    def _return(self, stmt: ReturnStmt) -> None:
        """Translate a ``return``, with or without a value.

        Args:
            stmt: The return statement to translate.
        """
        if stmt.value is None:
            self._emit("return")
            return
        place = self._expression(stmt.value)
        self._emit("return", arg1=place)

    def _print(self, stmt: PrintStmt) -> None:
        """Translate a ``print(expr);`` statement.

        Args:
            stmt: The print statement to translate.
        """
        place = self._expression(stmt.value)
        self._emit("print", arg1=place)

    # -- expressions ------------------------------------------------------

    def _expression(self, expr: Expr) -> str:
        """Translate an expression, returning the place holding its value.

        This is the synthesized-place scheme: every case computes its
        operands' places first, then emits the one instruction that combines
        them, so operands are always fully evaluated (left to right) before
        the operator that uses them.

        Args:
            expr: The expression to translate.

        Returns:
            The place -- a variable name, a temporary, or a literal's spelling
            -- holding the expression's value once this code has run.
        """
        if isinstance(expr, Literal):
            return self._literal_place(expr)
        if isinstance(expr, Identifier):
            return self._resolve(expr.name)
        if isinstance(expr, UnaryOp):
            return self._unary(expr)
        if isinstance(expr, BinaryOp):
            return self._binary(expr)
        if isinstance(expr, CallExpr):
            return self._call(expr)
        return self._new_temp()  # pragma: no cover - every Expr subclass is handled above

    @staticmethod
    def _literal_place(expr: Literal) -> str:
        """Render a literal as a place, with no instruction needed.

        Args:
            expr: The literal to render.

        Returns:
            The value rendered via :func:`app.literals.format_literal`, the
            single source of truth every phase that reads a TAC literal
            spelling back agrees with.
        """
        return format_literal(expr.value)

    def _unary(self, expr: UnaryOp) -> str:
        """Translate a prefix operation into one instruction.

        Args:
            expr: The operation to translate.

        Returns:
            The temporary holding the result.
        """
        operand = self._expression(expr.operand)
        temp = self._new_temp()
        op = "uminus" if expr.operator == "-" else "not"
        self._emit(op, arg1=operand, result=temp)
        return temp

    def _binary(self, expr: BinaryOp) -> str:
        """Translate a binary operation.

        ``&&`` and ``||`` are routed to jumping code for short-circuit
        evaluation; every other operator becomes one value-producing
        instruction, consistent with the module's two translation schemes.

        Args:
            expr: The operation to translate.

        Returns:
            The place holding the result.
        """
        if expr.operator == "&&":
            return self._short_circuit(expr, stop_when="ifFalse", stop_value="false")
        if expr.operator == "||":
            return self._short_circuit(expr, stop_when="if", stop_value="true")

        left = self._expression(expr.left)
        right = self._expression(expr.right)
        temp = self._new_temp()
        self._emit(expr.operator, arg1=left, arg2=right, result=temp)
        return temp

    def _short_circuit(self, expr: BinaryOp, *, stop_when: str, stop_value: str) -> str:
        """Translate ``&&`` or ``||`` as jumping code over the shared template.

        Both operators share one shape: evaluate the left operand, test it,
        and only evaluate the right operand when the test does not already
        settle the answer. What differs is which test short-circuits
        (``ifFalse`` for ``&&``, ``if`` for ``||``) and which constant the
        short-circuited path produces (``false`` for ``&&``, ``true`` for
        ``||``) -- exactly the two arguments this method takes.

        Args:
            expr: The ``&&`` or ``||`` operation to translate.
            stop_when: ``"ifFalse"`` for ``&&``, ``"if"`` for ``||``.
            stop_value: The constant the short-circuited path assigns.

        Returns:
            The temporary holding the result.
        """
        result = self._new_temp()
        stop_label = self._new_label()
        end_label = self._new_label()

        left = self._expression(expr.left)
        self._emit(stop_when, arg1=left, result=stop_label)
        right = self._expression(expr.right)
        self._emit("=", arg1=right, result=result)
        self._emit("goto", result=end_label)
        self._emit("label", result=stop_label)
        self._emit("=", arg1=stop_value, result=result)
        self._emit("label", result=end_label)
        return result

    def _call(self, expr: CallExpr) -> str:
        """Translate a call using the textbook param/call sequence.

        ``f(a, b)`` ::

            param a
            param b
            t = call f, 2

        A call whose declared return type is ``void`` omits the result slot
        entirely -- MiniLang's type checker only ever lets such a call stand
        alone as a statement, so no place is ever needed for it.

        Args:
            expr: The call to translate.

        Returns:
            The temporary holding the result, or ``""`` for a ``void`` call
            (never read: a ``void`` call can only appear as its own statement).
        """
        places = [self._expression(argument) for argument in expr.arguments]
        for place in places:
            self._emit("param", arg1=place)

        if expr.inferred_type == _VOID:
            self._emit("call", arg1=expr.callee, arg2=str(len(places)))
            return ""

        temp = self._new_temp()
        self._emit("call", arg1=expr.callee, arg2=str(len(places)), result=temp)
        return temp


# ---------------------------------------------------------------------------
# Basic blocks
# ---------------------------------------------------------------------------


def _label_indices(code: list[Instr]) -> dict[str, int]:
    """Map every label name to the index of its ``label`` instruction.

    Args:
        code: The full instruction listing.

    Returns:
        Label name to instruction index.
    """
    return {ins.result: i for i, ins in enumerate(code) if ins.op == "label" and ins.result}


def _leaders(code: list[Instr], labels: dict[str, int]) -> list[int]:
    """Find every leader instruction, by the classical three rules plus one.

    The three textbook rules: the first instruction is a leader; any jump
    target is a leader; any instruction right after a jump or a ``return`` is
    a leader. This adds a fourth: every ``label`` is unconditionally a leader,
    a superset that only ever produces extra (never incorrect) block
    boundaries -- see the module docstring for why that matters here.

    Args:
        code: The full instruction listing.
        labels: Label name to instruction index, from :func:`_label_indices`.

    Returns:
        Leader indices, sorted and de-duplicated.
    """
    if not code:
        return []

    leaders = {0}
    for i, ins in enumerate(code):
        if ins.op == "label":
            leaders.add(i)
        elif ins.op in _JUMPS:
            if ins.result in labels:
                leaders.add(labels[ins.result])
            if i + 1 < len(code):
                leaders.add(i + 1)
        elif ins.op == "return" and i + 1 < len(code):
            leaders.add(i + 1)
    return sorted(leaders)


def block_boundaries(code: list[Instr]) -> list[tuple[int, int]]:
    """Partition a TAC listing into basic blocks, as ``(start, end)`` ranges.

    The public half of the leader algorithm: everything :func:`build_cfg`
    needs to go on and compute successors, and everything a later pass (the
    optimiser's per-block constant propagation and common-subexpression
    elimination, and the liveness analysis behind its dead-code elimination)
    needs to know which instructions share a block -- without either one
    duplicating :func:`_leaders`.

    Args:
        code: The full instruction listing.

    Returns:
        Each block's ``(start, end)`` index range, ``end`` exclusive, in
        program order.
    """
    if not code:
        return []
    leader_positions = _leaders(code, _label_indices(code))
    return [
        (start, leader_positions[i + 1] if i + 1 < len(leader_positions) else len(code))
        for i, start in enumerate(leader_positions)
    ]


class _BlockLookup:
    """Resolves an instruction index to the id of the block containing it."""

    def __init__(self, leader_positions: list[int], block_ids: list[str]) -> None:
        """Store the sorted leader positions and their aligned block ids.

        Args:
            leader_positions: Leader indices, sorted ascending.
            block_ids: Block ids, aligned with ``leader_positions``.
        """
        self._positions = leader_positions
        self._ids = block_ids

    def __call__(self, index: int) -> str:
        """Resolve one instruction index.

        Args:
            index: The instruction index to resolve.

        Returns:
            The id of the block containing that instruction.
        """
        position = bisect.bisect_right(self._positions, index) - 1
        return self._ids[position]


@dataclass
class CfgResult:
    """Basic blocks and control-flow edges, ready for the wire.

    Attributes:
        blocks: Every basic block, in program order.
        edges: Every directed edge between blocks.
        entry: Id of the very first block, or ``None`` for an empty program.
        exits: Ids of every block with no successor (a ``return``'s block).
    """

    blocks: list[BasicBlock] = field(default_factory=list)
    edges: list[CfgEdge] = field(default_factory=list)
    entry: str | None = None
    exits: list[str] = field(default_factory=list)


def _function_namer(function_bounds: list[tuple[str, int]]) -> _BlockLookup:
    """Build a "which function contains instruction *i*" lookup.

    Args:
        function_bounds: ``(name, start_index)`` pairs, in ascending order of
            ``start_index``, as recorded by :class:`TacGenerator`.

    Returns:
        A callable mapping an instruction index to its owning function's name.
    """
    positions = [start for _, start in function_bounds]
    names = [name for name, _ in function_bounds]
    return _BlockLookup(positions, names)


def build_cfg(
    code: list[Instr],
    tac_api: list[TacInstruction],
    function_bounds: list[tuple[str, int]],
) -> CfgResult:
    """Partition a TAC listing into basic blocks and link them into a CFG.

    Args:
        code: The full instruction listing.
        tac_api: The same listing, already converted to
            :class:`~app.schemas.TacInstruction` (so each block can embed its
            slice without re-converting).
        function_bounds: ``(name, start_index)`` for every function, used to
            label each block with the function it belongs to.

    Returns:
        The blocks and edges of the control-flow graph.
    """
    if not code:
        return CfgResult()

    labels = _label_indices(code)
    bounds = block_boundaries(code)
    leader_positions = [start for start, _ in bounds]
    block_ids = [f"B{i}" for i in range(len(bounds))]
    block_of = _BlockLookup(leader_positions, block_ids)
    function_of = _function_namer(function_bounds)

    edges: list[CfgEdge] = []
    api_blocks: list[BasicBlock] = []
    for i, (start, end) in enumerate(bounds):
        block_id = block_ids[i]
        last = code[end - 1]
        successors: list[tuple[str, str | None]] = []

        if last.op == "goto":
            if last.result in labels:
                successors.append((block_of(labels[last.result]), None))
        elif last.op in ("if", "ifFalse"):
            jump_label = "true" if last.op == "if" else "false"
            fall_label = "false" if last.op == "if" else "true"
            if last.result in labels:
                successors.append((block_of(labels[last.result]), jump_label))
            if end < len(code):
                successors.append((block_of(end), fall_label))
        elif last.op != "return" and end < len(code):
            successors.append((block_of(end), "fall-through"))

        for target, label in successors:
            edges.append(CfgEdge(source=block_id, target=target, label=label))

        api_blocks.append(
            BasicBlock(
                id=block_id,
                leader=start,
                instructions=tac_api[start:end],
                successors=[target for target, _ in successors],
                function=function_of(start),
            )
        )

    exits = [block.id for block in api_blocks if not block.successors]
    return CfgResult(blocks=api_blocks, edges=edges, entry=block_ids[0], exits=exits)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


@dataclass
class TacResult:
    """Everything the ICG tab renders for one program, plus what phase 5 needs.

    Attributes:
        instructions: The full three-address-code listing.
        blocks: The basic blocks the listing was partitioned into.
        cfg: The control-flow graph linking those blocks.
        code: The same listing as ``instructions``, but as the lightweight
            internal :class:`Instr` the optimiser mutates rather than the
            wire-format :class:`~app.schemas.TacInstruction` -- unlike
            ``instructions``, this is not part of the HTTP response.
        function_bounds: ``(name, start_index)`` for every function, passed
            through unchanged from :class:`TacGenerator` so the optimiser can
            rebuild the CFG (and re-attribute blocks to functions) after it
            changes the instruction count.
        function_params: Every function's parameters' places, in
            declaration order, keyed by function name -- passed through
            unchanged so :mod:`app.codegen` can tell a parameter's place
            apart from a local's.
    """

    instructions: list[TacInstruction] = field(default_factory=list)
    blocks: list[BasicBlock] = field(default_factory=list)
    cfg: CfgView = field(default_factory=CfgView)
    code: list[Instr] = field(default_factory=list)
    function_bounds: list[tuple[str, int]] = field(default_factory=list)
    function_params: dict[str, list[str]] = field(default_factory=dict)


def generate(program: Program) -> TacResult:
    """Generate three-address code, basic blocks and the CFG for a program.

    The module's entry point, mirroring :func:`app.semantic.analyse`. Expects
    a tree that has already been through semantic analysis -- it reads
    ``inferred_type`` off call expressions to decide whether a call produces a
    value -- but runs correctly even over a program with type errors, since
    every expression leaves the analyser with *some* type recorded.

    Args:
        program: The root of the parsed (and ideally analysed) tree.

    Returns:
        The instructions, blocks and control-flow graph.
    """
    generator = TacGenerator()
    code = generator.generate(program)
    instructions = [instr.to_api(index) for index, instr in enumerate(code)]
    cfg = build_cfg(code, instructions, generator.function_bounds)

    return TacResult(
        instructions=instructions,
        blocks=cfg.blocks,
        cfg=CfgView(nodes=cfg.blocks, edges=cfg.edges, entry=cfg.entry, exits=cfg.exits),
        code=code,
        function_bounds=generator.function_bounds,
        function_params=generator.function_params,
    )
