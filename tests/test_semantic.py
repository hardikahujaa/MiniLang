"""Tests for :mod:`app.semantic`.

Organised by property rather than by method, because what matters about a
semantic analyser is not that it runs but that it enforces the right rules:
that scope is a genuine stack, that shadowing and redeclaration are told apart,
that widening goes one way only, and that one mistake yields one diagnostic
rather than an avalanche of consequences.

The adversarial suite at the bottom is the acceptance test. Each snippet is
written to fool a checker that looks plausible but is wrong somewhere -- a
shared namespace it forgot about, a conversion it allowed in both directions,
a scope it never popped.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.ast_nodes import AssignStmt as Assign
from app.ast_nodes import (
    BinaryOp,
    Block,
    CallExpr,
    Expr,
    ExprStmt,
    IfStmt,
    PrintStmt,
    Program,
    ReturnStmt,
    Stmt,
    UnaryOp,
    VarDecl,
    WhileStmt,
)
from app.lexer import tokenize
from app.parser import parse
from app.semantic import (
    BOOL,
    FLOAT,
    INT,
    UNKNOWN,
    VOID,
    Analyzer,
    SemanticResult,
    SymbolEntry,
    SymbolTable,
    analyse,
    assignable,
    function_type,
    widen,
)


def check(source: str) -> SemanticResult:
    """Lex, parse and analyse a source string.

    Args:
        source: MiniLang source text.

    Returns:
        The analysis result.
    """
    return analyse(parse(tokenize(source).tokens))


def messages(result: SemanticResult) -> list[str]:
    """Return every diagnostic message.

    Args:
        result: An analysis result.

    Returns:
        The messages, in the order they were reported.
    """
    return [d.message for d in result.diagnostics]


def errors(result: SemanticResult) -> list[str]:
    """Return only the hard-error messages.

    Args:
        result: An analysis result.

    Returns:
        The messages whose severity is ``"error"``.
    """
    return [d.message for d in result.diagnostics if d.severity == "error"]


def warnings(result: SemanticResult) -> list[str]:
    """Return only the warning messages.

    Args:
        result: An analysis result.

    Returns:
        The messages whose severity is ``"warning"``.
    """
    return [d.message for d in result.diagnostics if d.severity == "warning"]


def expressions(node: Stmt | Program) -> Iterator[Expr]:
    """Yield every expression in a subtree, including nested ones.

    Args:
        node: A statement, or the whole program.

    Yields:
        Each :class:`~app.ast_nodes.Expr` reachable from ``node``.
    """
    if isinstance(node, Program):
        for func in node.functions:
            yield from expressions(func.body)
    elif isinstance(node, Block):
        for statement in node.statements:
            yield from expressions(statement)
    elif isinstance(node, VarDecl):
        if node.initializer is not None:
            yield from _sub(node.initializer)
    elif isinstance(node, Assign):
        yield from _sub(node.value)
    elif isinstance(node, IfStmt):
        yield from _sub(node.condition)
        yield from expressions(node.then_branch)
        if node.else_branch is not None:
            yield from expressions(node.else_branch)
    elif isinstance(node, WhileStmt):
        yield from _sub(node.condition)
        yield from expressions(node.body)
    elif isinstance(node, ReturnStmt):
        if node.value is not None:
            yield from _sub(node.value)
    elif isinstance(node, PrintStmt):
        yield from _sub(node.value)
    elif isinstance(node, ExprStmt):
        yield from _sub(node.expression)


def _sub(expr: Expr) -> Iterator[Expr]:
    """Yield an expression and every expression beneath it.

    Args:
        expr: The root of an expression subtree.

    Yields:
        ``expr`` first, then its operands.
    """
    yield expr
    if isinstance(expr, BinaryOp):
        yield from _sub(expr.left)
        yield from _sub(expr.right)
    elif isinstance(expr, UnaryOp):
        yield from _sub(expr.operand)
    elif isinstance(expr, CallExpr):
        for argument in expr.arguments:
            yield from _sub(argument)


# ---------------------------------------------------------------------------
# The type lattice
# ---------------------------------------------------------------------------


class TestWidening:
    """Numeric conversion goes one way: int to float, never the reverse."""

    @pytest.mark.parametrize(
        ("left", "right", "expected"),
        [
            (INT, INT, INT),
            (INT, FLOAT, FLOAT),
            (FLOAT, INT, FLOAT),
            (FLOAT, FLOAT, FLOAT),
        ],
    )
    def test_numeric_join(self, left: str, right: str, expected: str):
        assert widen(left, right) == expected

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            (BOOL, INT),
            (INT, BOOL),
            (BOOL, BOOL),
            (BOOL, FLOAT),
            (VOID, INT),
            (INT, VOID),
        ],
    )
    def test_bool_and_void_have_no_numeric_join(self, left: str, right: str):
        """`bool` is not a small integer; that is the whole point of having it."""
        assert widen(left, right) is None

    @pytest.mark.parametrize("other", [INT, FLOAT, BOOL, VOID])
    def test_unknown_absorbs_everything(self, other: str):
        """An already-reported error must not provoke a second one."""
        assert widen(UNKNOWN, other) == UNKNOWN
        assert widen(other, UNKNOWN) == UNKNOWN

    def test_widening_is_commutative(self):
        for left in (INT, FLOAT, BOOL):
            for right in (INT, FLOAT, BOOL):
                assert widen(left, right) == widen(right, left)


class TestAssignability:
    """Storing a value is widening-only: narrowing silently loses information."""

    @pytest.mark.parametrize(
        ("target", "source"),
        [(INT, INT), (FLOAT, FLOAT), (BOOL, BOOL), (FLOAT, INT), (VOID, VOID)],
    )
    def test_accepted(self, target: str, source: str):
        assert assignable(target, source)

    @pytest.mark.parametrize(
        ("target", "source"),
        [(INT, FLOAT), (INT, BOOL), (BOOL, INT), (BOOL, FLOAT), (INT, VOID), (FLOAT, BOOL)],
    )
    def test_rejected(self, target: str, source: str):
        assert not assignable(target, source)

    def test_narrowing_is_never_implicit(self):
        assert assignable(FLOAT, INT)
        assert not assignable(INT, FLOAT)

    @pytest.mark.parametrize("other", [INT, FLOAT, BOOL, VOID])
    def test_unknown_is_compatible_both_ways(self, other: str):
        assert assignable(UNKNOWN, other)
        assert assignable(other, UNKNOWN)


class TestFunctionType:
    """Function types are rendered for the symbol table, not parsed back."""

    def test_rendering(self):
        assert function_type((INT, BOOL), FLOAT) == "(int, bool) -> float"

    def test_nullary(self):
        assert function_type((), VOID) == "() -> void"


# ---------------------------------------------------------------------------
# The symbol table
# ---------------------------------------------------------------------------


def entry(name: str, type_name: str = INT, kind: str = "variable") -> SymbolEntry:
    """Build a minimal symbol entry for table tests.

    Args:
        name: The identifier.
        type_name: Its declared type.
        kind: ``"variable"``, ``"parameter"`` or ``"function"``.

    Returns:
        A populated :class:`~app.semantic.SymbolEntry`.
    """
    return SymbolEntry(name=name, type=type_name, kind=kind, scope="", depth=0, line=1, column=1)


class TestSymbolTableMechanics:
    """A stack of hash tables, with the emphasis on *stack*."""

    def test_starts_with_only_the_global_scope(self):
        table = SymbolTable()
        assert table.depth == 1
        assert table.current.name == "global"
        assert table.current.depth == 0

    def test_push_and_pop_are_balanced(self):
        table = SymbolTable()
        table.push("f")
        table.push("f.block@2")
        assert table.depth == 3
        table.pop()
        table.pop()
        assert table.depth == 1

    def test_pushed_scope_records_its_depth(self):
        table = SymbolTable()
        assert table.push("f").depth == 1
        assert table.push("f.block@2").depth == 2

    def test_popping_the_global_scope_is_a_bug_not_a_no_op(self):
        """Silently allowing it would hide a push/pop imbalance in the walker."""
        with pytest.raises(RuntimeError):
            SymbolTable().pop()

    def test_declaration_succeeds_and_resolves(self):
        table = SymbolTable()
        assert table.declare(entry("x")) is None
        found = table.resolve("x")
        assert found is not None
        assert found.name == "x"

    def test_redeclaration_in_the_same_scope_is_refused(self):
        table = SymbolTable()
        table.declare(entry("x"))
        clash = table.declare(entry("x", FLOAT))
        assert clash is not None
        assert clash.type == INT, "the original binding must survive"

    def test_shadowing_in_an_inner_scope_is_allowed(self):
        table = SymbolTable()
        table.declare(entry("x", INT))
        table.push("inner")
        assert table.declare(entry("x", BOOL)) is None

        inner = table.resolve("x")
        assert inner is not None and inner.type == BOOL

        table.pop()
        outer = table.resolve("x")
        assert outer is not None and outer.type == INT, "the outer binding must reappear"

    def test_a_closed_scope_leaks_nothing(self):
        """The defining property: a name dies with the scope that declared it."""
        table = SymbolTable()
        table.push("inner")
        table.declare(entry("secret"))
        assert table.resolve("secret") is not None
        table.pop()
        assert table.resolve("secret") is None

    def test_resolution_walks_outward(self):
        table = SymbolTable()
        table.declare(entry("g"))
        table.push("a")
        table.push("b")
        assert table.resolve("g") is not None

    def test_declaration_stamps_the_owning_scope(self):
        table = SymbolTable()
        table.push("fib")
        table.declare(entry("n"))
        found = table.resolve("n")
        assert found is not None
        assert found.scope == "fib"
        assert found.depth == 1

    def test_the_record_survives_the_scope(self):
        """The live stack shrinks; the log the Semantic tab reads does not."""
        table = SymbolTable()
        table.push("inner")
        table.declare(entry("gone"))
        table.pop()

        assert table.resolve("gone") is None
        assert [e.name for e in table.recorded()] == ["gone"]

    def test_record_is_in_declaration_order(self):
        table = SymbolTable()
        for name in ("a", "b", "c"):
            table.declare(entry(name))
        assert [e.name for e in table.recorded()] == ["a", "b", "c"]

    def test_record_is_a_copy(self):
        """Callers must not be able to mutate the analyser's log."""
        table = SymbolTable()
        table.declare(entry("a"))
        table.recorded().append(entry("injected"))
        assert [e.name for e in table.recorded()] == ["a"]


# ---------------------------------------------------------------------------
# Scope hygiene through a real walk
# ---------------------------------------------------------------------------


class TestScopeHygiene:
    """After analysis the environment must be exactly as it started."""

    SOURCES = [
        "func m(): void { int x = 1; }",
        "func m(): void { if (true) { int x = 1; } else { int y = 2; } }",
        "func m(): void { while (true) { while (true) { int d = 1; } } }",
        "func m(int a): int { if (a > 0) { if (a > 1) { return 1; } } return 0; }",
        "func a(): void { int x = 1; } func b(): void { int x = 2; }",
    ]

    @pytest.mark.parametrize("source", SOURCES)
    def test_stack_returns_to_the_global_scope(self, source: str):
        analyzer = Analyzer()
        analyzer.analyse(parse(tokenize(source).tokens))
        assert analyzer.table.depth == 1, "a scope was pushed and never popped"
        assert analyzer.table.current.name == "global"

    @pytest.mark.parametrize("source", SOURCES)
    def test_symbols_outlive_their_scopes(self, source: str):
        analyzer = Analyzer()
        result = analyzer.analyse(parse(tokenize(source).tokens))
        assert result.symbols, "the declaration log was emptied with the scopes"

    def test_nesting_depth_is_tracked(self):
        source = (
            "func m(): void {\n"
            "    int d1 = 1;\n"
            "    if (true) {\n"
            "        int d2 = 2;\n"
            "        while (true) {\n"
            "            int d3 = 3;\n"
            "        }\n"
            "    }\n"
            "}"
        )
        by_name = {s.name: s for s in check(source).symbols}
        assert by_name["d1"].depth == 1
        assert by_name["d2"].depth == 2
        assert by_name["d3"].depth == 3

    def test_sibling_blocks_are_the_same_depth_not_nested(self):
        source = "func m(): void {\n    if (true) { int a = 1; }\n    if (true) { int b = 2; }\n}"
        by_name = {s.name: s for s in check(source).symbols}
        assert by_name["a"].depth == by_name["b"].depth == 2
        assert by_name["a"].scope != by_name["b"].scope, "sibling scopes are distinct"

    def test_parameters_and_body_share_one_scope(self):
        """As in C: redeclaring a parameter at the body's top level is an error."""
        result = check("func m(int n): void { int n = 1; }")
        assert any("redeclaration of 'n'" in m for m in errors(result))

    def test_a_nested_block_may_shadow_a_parameter(self):
        result = check("func m(int n): void { if (true) { bool n = true; } }")
        assert errors(result) == []


# ---------------------------------------------------------------------------
# Type annotation
# ---------------------------------------------------------------------------


class TestAnnotation:
    """Types are synthesized attributes, written onto the tree in place."""

    def test_every_expression_is_annotated(self):
        source = (
            "func f(int n): int { return n * 2; }\n"
            "func m(): void {\n"
            "    float r = 1.5;\n"
            "    int i = f(3) + 1;\n"
            "    bool b = !(i > 2) && r < 9.0;\n"
            "    print(i);\n"
            "}"
        )
        program = parse(tokenize(source).tokens)
        analyse(program)

        found = list(expressions(program))
        assert len(found) >= 15, "the walk did not reach the interesting nodes"
        for expr in found:
            assert expr.inferred_type is not None, f"{type(expr).__name__} was left untyped"

    @pytest.mark.parametrize(
        ("expression", "expected"),
        [
            ("1 + 2", INT),
            ("1 + 2.0", FLOAT),
            ("2.0 * 4", FLOAT),
            ("7 % 2", INT),
            ("1 < 2", BOOL),
            ("1.0 >= 2", BOOL),
            ("1 == 2", BOOL),
            ("true == false", BOOL),
            ("true && false", BOOL),
            ("!true", BOOL),
            ("-3", INT),
            ("-3.5", FLOAT),
        ],
    )
    def test_inferred_result_types(self, expression: str, expected: str):
        # Declaring the result at its expected type makes the assertion double:
        # the initialiser must both carry that type and be assignable to it.
        source = f"func m(): void {{ {expected} v = {expression}; }}"
        program = parse(tokenize(source).tokens)
        result = analyse(program)
        assert errors(result) == [], f"{expression} should be {expected}"

        decl = program.functions[0].body.statements[0]
        assert isinstance(decl, VarDecl)
        assert decl.initializer is not None
        assert decl.initializer.inferred_type == expected

    def test_an_errored_expression_is_typed_unknown(self):
        source = "func m(): void { int x = ghost + 1; }"
        program = parse(tokenize(source).tokens)
        analyse(program)
        decl = program.functions[0].body.statements[0]
        assert isinstance(decl, VarDecl)
        assert decl.initializer is not None
        assert decl.initializer.inferred_type == UNKNOWN


# ---------------------------------------------------------------------------
# Individual diagnostics
# ---------------------------------------------------------------------------


class TestDiagnostics:
    """One rule per test, so a failure names the rule that broke."""

    def test_undeclared_identifier(self):
        assert any(
            "undeclared identifier 'ghost'" in m
            for m in errors(check("func m(): void { int x = ghost; }"))
        )

    def test_assignment_to_undeclared_variable(self):
        assert any(
            "undeclared variable 'ghost'" in m
            for m in errors(check("func m(): void { ghost = 1; }"))
        )

    def test_call_to_undeclared_function(self):
        assert any(
            "undeclared function 'ghost'" in m
            for m in errors(check("func m(): void { print(ghost(1)); }"))
        )

    def test_void_variable(self):
        assert any(
            "cannot have type 'void'" in m for m in errors(check("func m(): void { void x; }"))
        )

    def test_void_parameter(self):
        assert any(
            "cannot have type 'void'" in m for m in errors(check("func m(void p): void { }"))
        )

    def test_narrowing_initialiser(self):
        assert any(
            "with a value of type 'float'" in m
            for m in errors(check("func m(): void { int x = 1.5; }"))
        )

    def test_widening_initialiser_is_accepted(self):
        assert errors(check("func m(): void { float x = 1; }")) == []

    def test_narrowing_assignment(self):
        assert any(
            "cannot assign a value of type 'float'" in m
            for m in errors(check("func m(): void { int x = 0; x = 1.5; }"))
        )

    def test_modulo_requires_integers(self):
        assert any(
            "operator '%' requires 'int'" in m
            for m in errors(check("func m(): void { float a = 5.0; float b = a % 2.0; }"))
        )

    def test_logical_operators_require_bool(self):
        assert any(
            "requires 'bool' operands" in m
            for m in errors(check("func m(): void { bool b = 1 && 2; }"))
        )

    def test_logical_operators_produce_bool(self):
        assert errors(check("func m(): void { bool b = true || false; }")) == []

    def test_arithmetic_rejects_bool(self):
        assert any(
            "requires numeric operands" in m
            for m in errors(check("func m(): void { bool b = true; int x = b + 1; }"))
        )

    def test_not_requires_bool(self):
        assert any(
            "operator '!' requires 'bool'" in m
            for m in errors(check("func m(): void { bool b = !1; }"))
        )

    def test_unary_minus_rejects_bool(self):
        assert any(
            "requires a numeric operand" in m
            for m in errors(check("func m(): void { bool b = true; int x = -b; }"))
        )

    def test_equality_across_incompatible_types(self):
        assert any(
            "cannot compare" in m
            for m in errors(check("func m(): void { bool b = true; bool c = b == 1; }"))
        )

    def test_if_condition_must_be_bool(self):
        assert any(
            "if condition must be 'bool'" in m
            for m in errors(check("func m(): void { if (1) { } }"))
        )

    def test_while_condition_must_be_bool(self):
        assert any(
            "while condition must be 'bool'" in m
            for m in errors(check("func m(): void { while (1) { } }"))
        )

    def test_wrong_arity(self):
        assert any(
            "expects 1 argument(s), got 2" in m
            for m in errors(
                check("func f(int a): int { return a; } func m(): void { print(f(1, 2)); }")
            )
        )

    def test_argument_type_mismatch(self):
        assert any(
            "argument 1 of 'f' expects 'int', got 'bool'" in m
            for m in errors(
                check("func f(int a): int { return a; } func m(): void { print(f(true)); }")
            )
        )

    def test_argument_widening_is_accepted(self):
        assert (
            errors(check("func f(float a): float { return a; } func m(): void { print(f(1)); }"))
            == []
        )

    def test_returning_a_value_from_void(self):
        assert any("returning 'void'" in m for m in errors(check("func m(): void { return 1; }")))

    def test_returning_nothing_from_a_typed_function(self):
        assert any(
            "must return a value of type 'int'" in m
            for m in errors(check("func m(): int { return; }"))
        )

    def test_return_type_mismatch(self):
        assert any(
            "cannot return a value of type 'bool'" in m
            for m in errors(check("func m(): int { return true; }"))
        )

    def test_void_call_used_as_a_value(self):
        assert any(
            "with a value of type 'void'" in m
            for m in errors(check("func n(): void { } func m(): void { int x = n(); }"))
        )

    def test_printing_void_is_refused(self):
        assert any(
            "cannot print a value of type 'void'" in m
            for m in errors(check("func n(): void { } func m(): void { print(n()); }"))
        )

    def test_function_used_as_a_value(self):
        assert any(
            "no first-class functions" in m
            for m in errors(check("func f(): int { return 1; } func m(): void { int x = f; }"))
        )

    def test_assigning_to_a_function(self):
        assert any(
            "which is a function" in m
            for m in errors(check("func f(): int { return 1; } func m(): void { f = 1; }"))
        )

    def test_duplicate_function_names(self):
        assert any(
            "redeclaration of 'f'" in m
            for m in errors(check("func f(): void { } func f(): void { }"))
        )

    def test_missing_return_is_a_warning_not_an_error(self):
        result = check("func f(int n): int { if (n > 0) { return 1; } }")
        assert errors(result) == []
        assert any("without returning" in m for m in warnings(result))

    def test_if_else_that_always_returns_is_not_warned(self):
        result = check("func f(int n): int { if (n > 0) { return 1; } else { return 0; } }")
        assert warnings(result) == []

    def test_void_function_needs_no_return(self):
        assert warnings(check("func m(): void { int x = 1; }")) == []

    def test_declaration_cannot_refer_to_itself(self):
        """`int x = x;` must see the outer x, or none -- never the one being made."""
        assert any(
            "undeclared identifier 'x'" in m for m in errors(check("func m(): void { int x = x; }"))
        )


class TestCascadeSuppression:
    """One mistake, one diagnostic. Consequences are not separate errors."""

    def test_one_undeclared_name_in_a_large_expression(self):
        result = check("func m(): void { int a = ghost * 2 + 3 - 4 / 5; }")
        assert len(errors(result)) == 1

    def test_a_bad_operand_does_not_also_fail_the_assignment(self):
        result = check("func m(): void { bool b = true; int x = b + 1; }")
        assert len(errors(result)) == 1

    def test_a_bad_call_does_not_also_fail_its_use(self):
        result = check("func m(): void { int x = ghost(1) + 2; }")
        assert len(errors(result)) == 1

    def test_errors_inside_arguments_are_still_reported(self):
        """Suppression must not swallow independent mistakes."""
        result = check("func m(): void { print(ghost(missing)); }")
        assert len(errors(result)) == 2


class TestForwardReferences:
    """Two passes: signatures first, so declaration order does not matter."""

    def test_calling_a_function_declared_later(self):
        assert (
            errors(
                check("func m(): void { print(later(1)); } func later(int n): int { return n; }")
            )
            == []
        )

    def test_mutual_recursion(self):
        source = (
            "func even(int n): bool {\n"
            "    if (n == 0) { return true; } else { return odd(n - 1); }\n"
            "}\n"
            "func odd(int n): bool {\n"
            "    if (n == 0) { return false; } else { return even(n - 1); }\n"
            "}"
        )
        assert errors(check(source)) == []

    def test_direct_recursion(self):
        assert (
            errors(
                check("func f(int n): int { if (n < 2) { return n; } else { return f(n - 1); } }")
            )
            == []
        )


class TestSymbolOutput:
    """What the Semantic tab receives."""

    def test_kinds_are_classified(self):
        result = check("func f(int p): int { int v = p; return v; }")
        kinds = {s.name: s.kind for s in result.symbols}
        assert kinds == {"f": "function", "p": "parameter", "v": "variable"}

    def test_function_type_is_a_signature(self):
        result = check("func f(int a, bool b): float { return 1.0; }")
        signature = next(s.type for s in result.symbols if s.name == "f")
        assert signature == "(int, bool) -> float"

    def test_symbols_carry_their_own_positions(self):
        result = check("func m(): void {\n    int counter = 0;\n}")
        counter = next(s for s in result.symbols if s.name == "counter")
        assert counter.line == 2
        assert counter.col == 9, "must point at the name, not the type keyword"

    def test_shadowed_names_both_appear(self):
        """The tab shows the environment's history, not just its final state."""
        result = check("func m(): void { int x = 1; if (true) { bool x = true; } }")
        shadowed = [s for s in result.symbols if s.name == "x"]
        assert len(shadowed) == 2
        assert {s.type for s in shadowed} == {INT, BOOL}
        assert {s.depth for s in shadowed} == {1, 2}


# ---------------------------------------------------------------------------
# The adversarial suite
# ---------------------------------------------------------------------------

#: Ten programs written to break a plausible-but-wrong checker. Each is
#: (title, source, expected error fragments, expected warning count).
ADVERSARIAL: list[tuple[str, str, list[str], int]] = [
    (
        "function name shadowed by a local, then called",
        "func fib(int n): int { return n; }\n"
        "func main(): void {\n"
        "    int fib = 3;\n"
        "    print(fib(2));\n"
        "}",
        ["not a function"],
        0,
    ),
    (
        "narrowing buried inside nested arithmetic",
        "func main(): void {\n    float f = 2.5;\n    int x = 1 + (2 * f);\n}",
        ["with a value of type 'float'"],
        0,
    ),
    (
        "modulo on floating-point operands",
        "func main(): void {\n    float a = 5.0;\n    float c = a % 2.0;\n}",
        ["operator '%' requires 'int'"],
        0,
    ),
    (
        "argument mismatch through a nested call, and wrong arity",
        "func takesInt(int n): int { return n; }\n"
        "func takesBool(bool b): bool { return b; }\n"
        "func two(int a, int b): int { return a + b; }\n"
        "func main(): void {\n"
        "    print(takesInt(takesBool(true)));\n"
        "    print(two(1));\n"
        "}",
        ["argument 1 of 'takesInt' expects 'int', got 'bool'", "expects 2 argument(s), got 1"],
        0,
    ),
    (
        "void abused as a variable, a value, and a return",
        "func nothing(): void {\n"
        "    return 1;\n"
        "}\n"
        "func main(): void {\n"
        "    void v;\n"
        "    int x = nothing();\n"
        "}",
        [
            "returning 'void'",
            "variable 'v' cannot have type 'void'",
            "with a value of type 'void'",
        ],
        0,
    ),
    (
        "non-boolean conditions, and a path that never returns",
        "func check(int n): int {\n    if (n) { return 1; }\n    while (n + 1) { }\n}",
        ["if condition must be 'bool'", "while condition must be 'bool'"],
        1,
    ),
    (
        "redeclaration versus legal shadowing, in the same program",
        "func main(): void {\n"
        "    int x = 1;\n"
        "    if (true) {\n"
        "        int x = 2;\n"
        "        int x = 3;\n"
        "    }\n"
        "    int x = 4;\n"
        "}",
        ["redeclaration of 'x' in this scope"],
        0,
    ),
    (
        "forward reference and mutual recursion, which must be clean",
        "func even(int n): bool {\n"
        "    if (n == 0) { return true; } else { return odd(n - 1); }\n"
        "}\n"
        "func odd(int n): bool {\n"
        "    if (n == 0) { return false; } else { return even(n - 1); }\n"
        "}\n"
        "func main(): void { print(even(4)); }",
        [],
        0,
    ),
    (
        "bool smuggled into arithmetic and ordering, plus a bad return",
        "func bad(): int {\n"
        "    return true;\n"
        "}\n"
        "func main(): void {\n"
        "    bool b = true;\n"
        "    int x = b + 1;\n"
        "    bool c = b < 2;\n"
        "}",
        [
            "cannot return a value of type 'bool'",
            "requires numeric operands",
        ],
        0,
    ),
    (
        "one undeclared name threaded through a long expression",
        "func main(): void {\n    int a = ghost * 2 + 3 - 4 / 5;\n}",
        ["undeclared identifier 'ghost'"],
        0,
    ),
]


class TestAdversarialSuite:
    """Ten programs designed to fool a checker that is nearly right."""

    @pytest.mark.parametrize(
        ("title", "source", "fragments", "warning_count"),
        ADVERSARIAL,
        ids=[case[0] for case in ADVERSARIAL],
    )
    def test_expected_diagnostics(
        self, title: str, source: str, fragments: list[str], warning_count: int
    ):
        result = check(source)
        reported = errors(result)

        for fragment in fragments:
            assert any(fragment in message for message in reported), (
                f"{title}: expected a diagnostic containing {fragment!r}, got {reported}"
            )
        if not fragments:
            assert reported == [], f"{title}: expected a clean program, got {reported}"
        assert len(warnings(result)) == warning_count, f"{title}: {warnings(result)}"

    @pytest.mark.parametrize(
        ("title", "source"),
        [(case[0], case[1]) for case in ADVERSARIAL],
        ids=[case[0] for case in ADVERSARIAL],
    )
    def test_scope_stack_is_balanced_even_when_errors_are_found(self, title: str, source: str):
        """Error paths must not skip a pop; that is where leaks hide."""
        analyzer = Analyzer()
        analyzer.analyse(parse(tokenize(source).tokens))
        assert analyzer.table.depth == 1, title

    def test_redeclaration_case_reports_exactly_two(self):
        """Two genuine clashes, and the inner shadowing must not be counted."""
        source = ADVERSARIAL[6][1]
        assert len([m for m in errors(check(source)) if "redeclaration" in m]) == 2

    def test_cascade_case_reports_exactly_one(self):
        source = ADVERSARIAL[9][1]
        assert len(errors(check(source))) == 1


class TestRobustness:
    """Pathological but legal input must not crash or hang."""

    def test_deeply_nested_blocks(self):
        depth = 20
        body = "int x = 1;"
        for _ in range(depth):
            body = f"if (true) {{ {body} }}"
        result = check(f"func m(): void {{ {body} }}")
        assert errors(result) == []
        assert max(s.depth for s in result.symbols) == depth + 1

    def test_many_declarations(self):
        body = "\n".join(f"int v{i} = {i};" for i in range(400))
        result = check(f"func m(): void {{ {body} }}")
        assert errors(result) == []
        assert len(result.symbols) == 401

    def test_repeated_names_in_sibling_scopes_are_all_legal(self):
        blocks = " ".join(f"if (true) {{ int x = {i}; }}" for i in range(30))
        result = check(f"func m(): void {{ {blocks} }}")
        assert errors(result) == []
        assert len([s for s in result.symbols if s.name == "x"]) == 30

    def test_long_call_chain(self):
        source = (
            "func f(int n): int { return n; }\nfunc m(): void { print("
            + "f(" * 20
            + "1"
            + ")" * 20
            + "); }"
        )
        assert errors(check(source)) == []
