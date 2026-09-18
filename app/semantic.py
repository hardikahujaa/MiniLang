"""Semantic analysis: scope resolution and type checking.

Phase 3 of the pipeline. Takes the abstract syntax tree the parser built,
resolves every name against a scoped symbol table, and computes a type for
every expression -- annotating the tree in place so that later phases, and the
Semantic tab, can read the result off the nodes themselves.

The symbol table
----------------
A **stack of hash tables**, which is the classical presentation: entering a
block pushes a table, leaving it pops one, and a name resolves by walking the
stack outward from the innermost scope. Declaring a name is a lookup in the
*innermost table only* -- that is what makes shadowing legal while
redeclaration in the same scope is not.

Two collections are kept, deliberately:

* ``_stack`` is the live environment. It must shrink on exit; if it did not,
  a name declared inside an ``if`` would stay visible after it, which is the
  classic scope leak.
* ``_recorded`` is an append-only log of everything ever declared. The live
  stack is empty by the time the walk finishes, but the Semantic tab has to
  show the whole program's environment, so the log outlives the scopes.

**One namespace.** Functions and variables share the environment, each entry
carrying a ``kind``, as in the Dragon Book's single-environment presentation.
So ``int fib = 1;`` really does shadow a function named ``fib``, and a call to
``fib`` afterwards is reported as calling something that is not a function.
That is the correct behaviour, not an accident of the design.

**Parameters and the body share one scope.** A function pushes exactly one
scope holding both its parameters and the top level of its body, as in C, so
``func f(int n) { int n = 1; }`` is a redeclaration rather than shadowing.
Nested blocks -- an ``if`` body, a ``while`` body -- push their own.

Types
-----
Types are **synthesized attributes**: a node's type is computed from its
children's types, bottom-up, in a post-order walk. Numeric conversion follows
the usual widening rule -- ``int`` converts to ``float`` implicitly, never the
reverse -- so ``1 + 2.0`` is a ``float`` and ``int x = 1.5;`` is an error.

``%`` is integer-only, as in C: a modulo on floating-point operands is a type
error rather than a silent success.

Cascading errors
----------------
An expression whose type could not be determined gets the internal type
:data:`UNKNOWN`, which is compatible with everything and is never itself
reported. Without it a single undeclared ``x`` would produce one diagnostic per
use, burying the real mistake under noise it caused.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

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
    Param,
    PrintStmt,
    Program,
    ReturnStmt,
    Stmt,
    UnaryOp,
    VarDecl,
    WhileStmt,
)
from app.schemas import Diagnostic, Symbol

# ---------------------------------------------------------------------------
# The type lattice
# ---------------------------------------------------------------------------

#: The MiniLang type names, as written in source.
INT: Final[str] = "int"
FLOAT: Final[str] = "float"
BOOL: Final[str] = "bool"
VOID: Final[str] = "void"

#: Internal bottom type: "an error was already reported for this expression".
#: Compatible with everything, never reported, never written by the parser.
UNKNOWN: Final[str] = "unknown"

#: Types that participate in arithmetic and ordering.
NUMERIC: Final[frozenset[str]] = frozenset({INT, FLOAT})

#: Operators whose operands must be numeric and whose result is their join.
_ARITHMETIC: Final[frozenset[str]] = frozenset({"+", "-", "*", "/"})

#: Operators producing a boolean from two numeric operands.
_ORDERING: Final[frozenset[str]] = frozenset({"<", ">", "<=", ">="})

#: Operators producing a boolean from two operands of compatible type.
_EQUALITY: Final[frozenset[str]] = frozenset({"==", "!="})

#: Short-circuit operators: boolean in, boolean out.
_LOGICAL: Final[frozenset[str]] = frozenset({"&&", "||"})


def widen(left: str, right: str) -> str | None:
    """Return the common numeric type of two operand types.

    Implements the usual widening rule: ``int`` converts to ``float``
    implicitly, so the join of a mixed pair is ``float``. ``bool`` is not
    numeric and never participates.

    Args:
        left: The left operand's type.
        right: The right operand's type.

    Returns:
        The joined type, :data:`UNKNOWN` if either side is already in error, or
        ``None`` when the two types have no numeric join.
    """
    if left == UNKNOWN or right == UNKNOWN:
        return UNKNOWN
    if left in NUMERIC and right in NUMERIC:
        return FLOAT if FLOAT in (left, right) else INT
    return None


def assignable(target: str, source: str) -> bool:
    """Report whether a ``source`` value may be stored in a ``target`` slot.

    Widening is permitted (``float x = 1;``); narrowing never is
    (``int x = 1.5;``), because it silently loses information.

    Args:
        target: The declared type of the destination.
        source: The type of the value being stored.

    Returns:
        True when the assignment is well-typed.
    """
    if UNKNOWN in (target, source):
        return True
    if target == source:
        return True
    return target == FLOAT and source == INT


def function_type(params: tuple[str, ...], return_type: str) -> str:
    """Render a function's type for display in the symbol table.

    Args:
        params: The parameter types, in order.
        return_type: The declared return type.

    Returns:
        A signature such as ``"(int, int) -> int"``.
    """
    return f"({', '.join(params)}) -> {return_type}"


# ---------------------------------------------------------------------------
# The symbol table
# ---------------------------------------------------------------------------


@dataclass
class SymbolEntry:
    """One name binding.

    Attributes:
        name: The identifier's spelling.
        type: Declared type, or a rendered signature for a function.
        kind: ``"variable"``, ``"parameter"`` or ``"function"``.
        scope: Qualified name of the owning scope.
        depth: Nesting depth; 0 is global.
        line: 1-based line where the name was declared.
        column: 1-based column where the name was declared.
        params: Parameter types, for functions only.
        return_type: Declared return type, for functions only.
    """

    name: str
    type: str
    kind: str
    scope: str
    depth: int
    line: int
    column: int
    params: tuple[str, ...] | None = None
    return_type: str | None = None

    def to_api(self) -> Symbol:
        """Convert to the wire model the Semantic tab renders.

        Returns:
            The corresponding :class:`~app.schemas.Symbol`.
        """
        return Symbol(
            name=self.name,
            type=self.type,
            kind=self.kind,
            scope=self.scope,
            depth=self.depth,
            line=self.line,
            col=self.column,
        )


@dataclass
class Scope:
    """One hash table in the environment stack.

    Attributes:
        name: Qualified name, e.g. ``"global"``, ``"fib"``, ``"fib.block@5"``.
        depth: Nesting depth; 0 is global.
        entries: Names bound directly in this scope.
    """

    name: str
    depth: int
    entries: dict[str, SymbolEntry] = field(default_factory=dict)


class SymbolTable:
    """A stack of hash tables implementing lexical scope.

    The stack starts holding the global scope and must end holding only the
    global scope: every ``push`` is paired with a ``pop``. Tests assert that
    directly, because an unbalanced stack is a scope leak that would silently
    let inner declarations escape.
    """

    def __init__(self) -> None:
        """Start with the global scope open and nothing recorded."""
        self._stack: list[Scope] = [Scope(name="global", depth=0)]
        self._recorded: list[SymbolEntry] = []

    @property
    def depth(self) -> int:
        """Return how many scopes are currently open.

        Returns:
            The live stack height; 1 when only the global scope is open.
        """
        return len(self._stack)

    @property
    def current(self) -> Scope:
        """Return the innermost open scope.

        Returns:
            The scope at the top of the stack.
        """
        return self._stack[-1]

    def push(self, name: str) -> Scope:
        """Open a new innermost scope.

        Args:
            name: Qualified name for the new scope.

        Returns:
            The scope that was opened.
        """
        scope = Scope(name=name, depth=len(self._stack))
        self._stack.append(scope)
        return scope

    def pop(self) -> Scope:
        """Close the innermost scope.

        Returns:
            The scope that was closed.

        Raises:
            RuntimeError: If called with only the global scope open, which
                would mean a push/pop imbalance in the analyser.
        """
        if len(self._stack) == 1:
            raise RuntimeError("cannot pop the global scope")
        return self._stack.pop()

    def declare(self, entry: SymbolEntry) -> SymbolEntry | None:
        """Bind a name in the innermost scope.

        The clash check looks at the innermost scope *only*. A name bound in an
        enclosing scope is shadowed, which is legal.

        Args:
            entry: The binding to add. Its ``scope`` and ``depth`` are filled
                in from the innermost scope.

        Returns:
            ``None`` on success, or the existing entry when the name is already
            bound in this same scope.
        """
        scope = self.current
        existing = scope.entries.get(entry.name)
        if existing is not None:
            return existing

        entry.scope = scope.name
        entry.depth = scope.depth
        scope.entries[entry.name] = entry
        self._recorded.append(entry)
        return None

    def resolve(self, name: str) -> SymbolEntry | None:
        """Look a name up, innermost scope first.

        Args:
            name: The identifier to resolve.

        Returns:
            The binding that the name refers to, or ``None`` if undeclared.
        """
        for scope in reversed(self._stack):
            found = scope.entries.get(name)
            if found is not None:
                return found
        return None

    def recorded(self) -> list[SymbolEntry]:
        """Return every binding ever declared, in declaration order.

        Survives the scopes themselves, so the Semantic tab can show the whole
        program's environment after the walk has finished.

        Returns:
            The append-only declaration log.
        """
        return list(self._recorded)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class SemanticResult:
    """What one semantic analysis produced.

    Attributes:
        symbols: Every binding declared, in declaration order.
        diagnostics: Type errors and warnings, in source order of discovery.
    """

    symbols: list[Symbol]
    diagnostics: list[Diagnostic]

    @property
    def ok(self) -> bool:
        """Report whether analysis found no hard errors.

        Warnings do not make a program ill-typed.

        Returns:
            True when no diagnostic has severity ``"error"``.
        """
        return not any(d.severity == "error" for d in self.diagnostics)


# ---------------------------------------------------------------------------
# Analyser
# ---------------------------------------------------------------------------


def _always_returns(stmt: Stmt) -> bool:
    """Report whether a statement guarantees a ``return`` on every path.

    Deliberately conservative and purely syntactic. A ``while`` body may run
    zero times, so a loop never counts as returning even when its body always
    does -- which is exactly why a negative answer is reported as a warning
    rather than an error until the control-flow graph exists in phase 4.

    Args:
        stmt: The statement to inspect.

    Returns:
        True when control cannot leave this statement without returning.
    """
    if isinstance(stmt, ReturnStmt):
        return True
    if isinstance(stmt, Block):
        return any(_always_returns(inner) for inner in stmt.statements)
    if isinstance(stmt, IfStmt):
        return (
            stmt.else_branch is not None
            and _always_returns(stmt.then_branch)
            and _always_returns(stmt.else_branch)
        )
    return False


class Analyzer:
    """Walks an AST, resolving names and computing types.

    One analyser handles one program; it is not reusable, because the symbol
    table and the diagnostic list are instance state.
    """

    def __init__(self) -> None:
        """Prepare an empty environment and diagnostic list."""
        self.table = SymbolTable()
        self.diagnostics: list[Diagnostic] = []
        self._function: SymbolEntry | None = None
        self._function_name: str = "global"

    # -- diagnostics ------------------------------------------------------

    def _report(self, node: Expr | Stmt | FuncDecl | Param, message: str) -> None:
        """Record a hard error positioned on a node.

        Args:
            node: The offending node, whose position is used.
            message: The message shown in the Problems panel.
        """
        self.diagnostics.append(
            Diagnostic(
                line=node.line,
                col=node.column,
                message=message,
                severity="error",
                phase="semantic",
            )
        )

    def _warn(self, node: Expr | Stmt | FuncDecl | Param, message: str) -> None:
        """Record a warning positioned on a node.

        Args:
            node: The node the warning concerns.
            message: The message shown in the Problems panel.
        """
        self.diagnostics.append(
            Diagnostic(
                line=node.line,
                col=node.column,
                message=message,
                severity="warning",
                phase="semantic",
            )
        )

    # -- entry point ------------------------------------------------------

    def analyse(self, program: Program) -> SemanticResult:
        """Resolve and type-check a whole program.

        Runs in two passes. The first records every function signature in the
        global scope, which is what allows a function to call one declared
        below it, and mutual recursion with it. The second checks the bodies.

        Args:
            program: The root of the parsed tree.

        Returns:
            The symbols declared and the diagnostics found.
        """
        self._collect_functions(program)
        for func in program.functions:
            self._check_function(func)
        return SemanticResult(
            symbols=[entry.to_api() for entry in self.table.recorded()],
            diagnostics=self.diagnostics,
        )

    def _collect_functions(self, program: Program) -> None:
        """Bind every function name in the global scope.

        Args:
            program: The root of the parsed tree.
        """
        for func in program.functions:
            params = tuple(param.param_type for param in func.params)
            line, column = func.name_pos or (func.line, func.column)
            clash = self.table.declare(
                SymbolEntry(
                    name=func.name,
                    type=function_type(params, func.return_type),
                    kind="function",
                    scope="global",
                    depth=0,
                    line=line,
                    column=column,
                    params=params,
                    return_type=func.return_type,
                )
            )
            if clash is not None:
                self._report(func, f"redeclaration of '{func.name}' in this scope")

    def _check_function(self, func: FuncDecl) -> None:
        """Check one function's parameters and body.

        Args:
            func: The function to check.
        """
        entry = self.table.resolve(func.name)
        self._function = entry
        self._function_name = func.name

        # One scope for parameters and the top level of the body, as in C.
        self.table.push(func.name)
        for param in func.params:
            self._declare_param(param)
        self._block(func.body, new_scope=False)
        self.table.pop()

        if func.return_type != VOID and not _always_returns(func.body):
            self._warn(
                func,
                f"control may reach the end of '{func.name}' "
                f"without returning a value of type '{func.return_type}'",
            )

        self._function = None
        self._function_name = "global"

    def _declare_param(self, param: Param) -> None:
        """Bind one formal parameter in the function's scope.

        Args:
            param: The parameter to bind.
        """
        line, column = param.name_pos or (param.line, param.column)
        if param.param_type == VOID:
            self._report(param, f"parameter '{param.name}' cannot have type 'void'")

        clash = self.table.declare(
            SymbolEntry(
                name=param.name,
                type=param.param_type,
                kind="parameter",
                scope=self._function_name,
                depth=1,
                line=line,
                column=column,
            )
        )
        if clash is not None:
            self._report(param, f"redeclaration of '{param.name}' in this scope")

    # -- statements -------------------------------------------------------

    def _block(self, block: Block, *, new_scope: bool = True) -> None:
        """Check every statement in a block.

        Args:
            block: The block to check.
            new_scope: Whether the block opens a scope of its own. False for a
                function body, whose scope is the one holding its parameters.
        """
        if new_scope:
            self.table.push(f"{self._function_name}.block@{block.line}")
        for statement in block.statements:
            self._statement(statement)
        if new_scope:
            self.table.pop()

    def _statement(self, stmt: Stmt) -> None:
        """Dispatch one statement to its checker.

        Args:
            stmt: The statement to check.
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
        """Check a variable declaration and bind its name.

        The initialiser is checked *before* the name is bound, so that
        ``int x = x;`` reports an undeclared ``x`` rather than resolving to the
        variable being declared.

        Args:
            stmt: The declaration to check.
        """
        if stmt.initializer is not None:
            value_type = self._expression(stmt.initializer)
            if stmt.var_type != VOID and not assignable(stmt.var_type, value_type):
                self._report(
                    stmt.initializer,
                    f"cannot initialise '{stmt.name}' of type '{stmt.var_type}' "
                    f"with a value of type '{value_type}'",
                )

        if stmt.var_type == VOID:
            self._report(stmt, f"variable '{stmt.name}' cannot have type 'void'")

        line, column = stmt.name_pos or (stmt.line, stmt.column)
        clash = self.table.declare(
            SymbolEntry(
                name=stmt.name,
                type=stmt.var_type,
                kind="variable",
                scope=self.table.current.name,
                depth=self.table.current.depth,
                line=line,
                column=column,
            )
        )
        if clash is not None:
            self._report(stmt, f"redeclaration of '{stmt.name}' in this scope")

    def _assign(self, stmt: AssignStmt) -> None:
        """Check an assignment to an existing variable.

        Args:
            stmt: The assignment to check.
        """
        value_type = self._expression(stmt.value)
        entry = self.table.resolve(stmt.target)

        if entry is None:
            self._report(stmt, f"assignment to undeclared variable '{stmt.target}'")
            return
        if entry.kind == "function":
            self._report(stmt, f"cannot assign to '{stmt.target}', which is a function")
            return
        if not assignable(entry.type, value_type):
            self._report(
                stmt.value,
                f"cannot assign a value of type '{value_type}' "
                f"to '{stmt.target}' of type '{entry.type}'",
            )

    def _condition(self, expr: Expr, construct: str) -> None:
        """Check that a guard expression is boolean.

        Args:
            expr: The condition expression.
            construct: The statement name, used in the message.
        """
        condition_type = self._expression(expr)
        if condition_type not in (BOOL, UNKNOWN):
            self._report(
                expr,
                f"{construct} condition must be 'bool', not '{condition_type}'",
            )

    def _if(self, stmt: IfStmt) -> None:
        """Check a conditional and both its branches.

        Args:
            stmt: The conditional to check.
        """
        self._condition(stmt.condition, "if")
        self._block(stmt.then_branch)
        if stmt.else_branch is not None:
            self._block(stmt.else_branch)

    def _while(self, stmt: WhileStmt) -> None:
        """Check a loop guard and body.

        Args:
            stmt: The loop to check.
        """
        self._condition(stmt.condition, "while")
        self._block(stmt.body)

    def _return(self, stmt: ReturnStmt) -> None:
        """Check a return against the enclosing function's declared type.

        Args:
            stmt: The return statement to check.
        """
        expected = self._function.return_type if self._function else None

        if stmt.value is None:
            if expected not in (None, VOID):
                self._report(stmt, f"function must return a value of type '{expected}'")
            return

        value_type = self._expression(stmt.value)
        if expected == VOID:
            self._report(stmt.value, "cannot return a value from a function returning 'void'")
        elif expected is not None and not assignable(expected, value_type):
            self._report(
                stmt.value,
                f"cannot return a value of type '{value_type}' "
                f"from a function returning '{expected}'",
            )

    def _print(self, stmt: PrintStmt) -> None:
        """Check that a printed expression has a printable type.

        Args:
            stmt: The print statement to check.
        """
        value_type = self._expression(stmt.value)
        if value_type == VOID:
            self._report(stmt.value, "cannot print a value of type 'void'")

    # -- expressions ------------------------------------------------------

    def _expression(self, expr: Expr) -> str:
        """Compute an expression's type and annotate it in place.

        This is the post-order walk: operands are typed first, then the
        operator combines their types into the result. The computed type is
        written to ``expr.inferred_type`` before returning, so the tree carries
        its own annotations for later phases and for the Syntax tab.

        Args:
            expr: The expression to type.

        Returns:
            The expression's type, or :data:`UNKNOWN` when an error was
            reported for it.
        """
        if isinstance(expr, Literal):
            result = expr.literal_type
        elif isinstance(expr, Identifier):
            result = self._identifier(expr)
        elif isinstance(expr, UnaryOp):
            result = self._unary(expr)
        elif isinstance(expr, BinaryOp):
            result = self._binary(expr)
        elif isinstance(expr, CallExpr):
            result = self._call(expr)
        else:  # pragma: no cover - every Expr subclass is handled above
            result = UNKNOWN

        expr.inferred_type = result
        return result

    def _identifier(self, expr: Identifier) -> str:
        """Type a variable reference.

        Args:
            expr: The identifier to resolve.

        Returns:
            The referent's declared type, or :data:`UNKNOWN`.
        """
        entry = self.table.resolve(expr.name)
        if entry is None:
            self._report(expr, f"undeclared identifier '{expr.name}'")
            return UNKNOWN
        if entry.kind == "function":
            self._report(
                expr,
                f"'{expr.name}' is a function; MiniLang has no first-class functions",
            )
            return UNKNOWN
        return entry.type

    def _unary(self, expr: UnaryOp) -> str:
        """Type a prefix operation.

        Args:
            expr: The operation to type.

        Returns:
            The result type, or :data:`UNKNOWN`.
        """
        operand = self._expression(expr.operand)
        if operand == UNKNOWN:
            return UNKNOWN

        if expr.operator == "!":
            if operand != BOOL:
                self._report(expr, f"operator '!' requires 'bool', not '{operand}'")
                return UNKNOWN
            return BOOL

        if operand not in NUMERIC:
            self._report(
                expr,
                f"unary '{expr.operator}' requires a numeric operand, not '{operand}'",
            )
            return UNKNOWN
        return operand

    def _binary(self, expr: BinaryOp) -> str:
        """Type a binary operation.

        Args:
            expr: The operation to type.

        Returns:
            The result type, or :data:`UNKNOWN`.
        """
        left = self._expression(expr.left)
        right = self._expression(expr.right)
        operator = expr.operator

        if UNKNOWN in (left, right):
            return UNKNOWN

        if operator in _LOGICAL:
            if left != BOOL or right != BOOL:
                self._report(
                    expr,
                    f"operator '{operator}' requires 'bool' operands, not '{left}' and '{right}'",
                )
                return UNKNOWN
            return BOOL

        if operator == "%":
            if left != INT or right != INT:
                self._report(
                    expr,
                    f"operator '%' requires 'int' operands, not '{left}' and '{right}'",
                )
                return UNKNOWN
            return INT

        if operator in _ARITHMETIC:
            joined = widen(left, right)
            if joined is None:
                self._report(
                    expr,
                    f"operator '{operator}' requires numeric operands, not '{left}' and '{right}'",
                )
                return UNKNOWN
            return joined

        if operator in _ORDERING:
            if widen(left, right) is None:
                self._report(
                    expr,
                    f"operator '{operator}' requires numeric operands, not '{left}' and '{right}'",
                )
                return UNKNOWN
            return BOOL

        if operator in _EQUALITY:
            if left == BOOL and right == BOOL:
                return BOOL
            if widen(left, right) is None:
                self._report(
                    expr,
                    f"cannot compare '{left}' with '{right}' using '{operator}'",
                )
                return UNKNOWN
            return BOOL

        return UNKNOWN  # pragma: no cover - the parser emits no other operators

    def _call(self, expr: CallExpr) -> str:
        """Type a function call and check its arguments.

        Arguments are typed even when the callee is unknown, so that errors
        inside them are still reported rather than swallowed with the call.

        Args:
            expr: The call to type.

        Returns:
            The callee's return type, or :data:`UNKNOWN`.
        """
        argument_types = [self._expression(argument) for argument in expr.arguments]
        entry = self.table.resolve(expr.callee)

        if entry is None:
            self._report(expr, f"call to undeclared function '{expr.callee}'")
            return UNKNOWN
        if entry.kind != "function":
            self._report(
                expr,
                f"'{expr.callee}' is a {entry.kind} of type '{entry.type}', not a function",
            )
            return UNKNOWN

        expected = entry.params or ()
        if len(argument_types) != len(expected):
            self._report(
                expr,
                f"'{expr.callee}' expects {len(expected)} argument(s), got {len(argument_types)}",
            )
            return entry.return_type or UNKNOWN

        for index, (declared, actual) in enumerate(zip(expected, argument_types, strict=True)):
            if not assignable(declared, actual):
                self._report(
                    expr.arguments[index],
                    f"argument {index + 1} of '{expr.callee}' expects '{declared}', got '{actual}'",
                )

        return entry.return_type or UNKNOWN


def analyse(program: Program) -> SemanticResult:
    """Resolve names and check types for a whole program.

    The module's entry point, mirroring :func:`app.parser.parse`.

    Args:
        program: The root of the parsed tree.

    Returns:
        The symbols declared and the diagnostics found.
    """
    return Analyzer().analyse(program)
