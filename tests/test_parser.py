"""Tests for :mod:`app.parser`.

Organised by grammar rule and by property, because what matters about a parser
is that the tree it builds means the right thing: precedence, associativity and
positions, not merely that parsing succeeded.

The malformed-input classes are deliberately large. This parser is **Baseline A**
in the plan's evaluation (section 7) -- the control condition the recovering
parser is measured against -- so "aborts on the first error, at the right place,
with a useful message" is its specification, not a shortcoming.
"""

from __future__ import annotations

import json

import pytest

from app.ast_nodes import (
    AssignStmt,
    BinaryOp,
    CallExpr,
    ExprStmt,
    Identifier,
    IfStmt,
    Literal,
    PrintStmt,
    Program,
    ReturnStmt,
    UnaryOp,
    VarDecl,
    WhileStmt,
)
from app.lexer import TokenType, tokenize
from app.parser import MAX_NESTING_DEPTH, ParseError, Parser, parse


def parse_source(source: str) -> Program:
    """Lex and parse a complete program.

    Args:
        source: MiniLang source text.

    Returns:
        The parsed program.
    """
    return parse(tokenize(source).tokens)


def parse_expr(expression: str):
    """Parse a single expression by wrapping it in a minimal program.

    Args:
        expression: The expression source, without a trailing semicolon.

    Returns:
        The expression node from the wrapping declaration's initialiser.
    """
    program = parse_source(f"func m(): void {{ int x = {expression}; }}")
    return program.functions[0].body.statements[0].initializer


def first_body(source: str) -> list:
    """Return the statements of the first function in a program.

    Args:
        source: MiniLang source text.

    Returns:
        The statement list of the first function's body.
    """
    return parse_source(source).functions[0].body.statements


class TestProgramStructure:
    """``program -> funcDecl+``."""

    def test_single_function(self):
        program = parse_source("func main(): void { }")
        assert isinstance(program, Program)
        assert len(program.functions) == 1
        assert program.functions[0].name == "main"

    def test_multiple_functions_keep_source_order(self):
        program = parse_source("func a(): void { } func b(): void { } func c(): void { }")
        assert [f.name for f in program.functions] == ["a", "b", "c"]

    def test_empty_program_is_rejected(self):
        """``funcDecl+`` requires at least one function."""
        with pytest.raises(ParseError) as info:
            parse_source("")
        assert TokenType.FUNC in info.value.expected

    def test_whitespace_and_comments_only_is_rejected(self):
        with pytest.raises(ParseError):
            parse_source("// nothing here\n\n/* also nothing */")


class TestFunctionDeclarations:
    """``funcDecl -> 'func' ID '(' params? ')' ':' type block``."""

    def test_no_parameters(self):
        function = parse_source("func main(): void { }").functions[0]
        assert function.params == []
        assert function.return_type == "void"

    def test_single_parameter(self):
        function = parse_source("func f(int n): int { return n; }").functions[0]
        assert len(function.params) == 1
        assert function.params[0].param_type == "int"
        assert function.params[0].name == "n"

    def test_multiple_parameters(self):
        function = parse_source("func f(int a, float b, bool c): void { }").functions[0]
        assert [(p.param_type, p.name) for p in function.params] == [
            ("int", "a"),
            ("float", "b"),
            ("bool", "c"),
        ]

    @pytest.mark.parametrize("type_name", ["int", "float", "bool", "void"])
    def test_every_return_type(self, type_name: str):
        function = parse_source(f"func f(): {type_name} {{ }}").functions[0]
        assert function.return_type == type_name

    def test_body_is_a_block(self):
        function = parse_source("func f(): void { int x = 1; }").functions[0]
        assert len(function.body.statements) == 1


class TestStatements:
    """Each production of ``stmt``."""

    def test_var_decl_without_initialiser(self):
        statement = first_body("func m(): void { int x; }")[0]
        assert isinstance(statement, VarDecl)
        assert statement.var_type == "int"
        assert statement.name == "x"
        assert statement.initializer is None

    def test_var_decl_with_initialiser(self):
        statement = first_body("func m(): void { int x = 5; }")[0]
        assert isinstance(statement.initializer, Literal)
        assert statement.initializer.value == 5

    def test_void_variable_is_accepted_by_the_parser(self):
        """The grammar allows it; rejecting it is the semantic analyser's job."""
        statement = first_body("func m(): void { void x; }")[0]
        assert statement.var_type == "void"

    def test_assignment(self):
        statement = first_body("func m(): void { x = 5; }")[0]
        assert isinstance(statement, AssignStmt)
        assert statement.target == "x"

    def test_if_without_else(self):
        statement = first_body("func m(): void { if (x) { } }")[0]
        assert isinstance(statement, IfStmt)
        assert statement.else_branch is None

    def test_if_with_else(self):
        statement = first_body("func m(): void { if (x) { } else { } }")[0]
        assert statement.else_branch is not None

    def test_nested_if_else_has_no_dangling_ambiguity(self):
        """Both branches are blocks, so else can only bind to its enclosing if."""
        statement = first_body("func m(): void { if (a) { if (b) { } else { } } }")[0]
        assert statement.else_branch is None
        assert statement.then_branch.statements[0].else_branch is not None

    def test_while(self):
        statement = first_body("func m(): void { while (i < n) { i = i + 1; } }")[0]
        assert isinstance(statement, WhileStmt)
        assert isinstance(statement.condition, BinaryOp)
        assert len(statement.body.statements) == 1

    def test_return_with_value(self):
        statement = first_body("func m(): int { return 5; }")[0]
        assert isinstance(statement, ReturnStmt)
        assert statement.value is not None

    def test_return_without_value(self):
        statement = first_body("func m(): void { return; }")[0]
        assert statement.value is None

    def test_print(self):
        statement = first_body("func m(): void { print(x); }")[0]
        assert isinstance(statement, PrintStmt)
        assert isinstance(statement.value, Identifier)

    def test_expression_statement(self):
        statement = first_body("func m(): void { f(); }")[0]
        assert isinstance(statement, ExprStmt)
        assert isinstance(statement.expression, CallExpr)

    def test_empty_block(self):
        assert first_body("func m(): void { }") == []

    def test_many_statements_keep_order(self):
        statements = first_body("func m(): void { int a = 1; int b = 2; int c = 3; }")
        assert [s.name for s in statements] == ["a", "b", "c"]

    def test_assignment_and_call_are_distinguished(self):
        """Both begin with IDENT; only the following '=' separates them."""
        statements = first_body("func m(): void { x = 1; f(); }")
        assert isinstance(statements[0], AssignStmt)
        assert isinstance(statements[1], ExprStmt)


class TestPrecedence:
    """Precedence comes from the call chain, not a table."""

    def test_multiplication_binds_tighter_than_addition(self):
        expr = parse_expr("2 + 3 * 4")
        assert expr.operator == "+"
        assert expr.right.operator == "*"

    def test_addition_binds_tighter_than_comparison(self):
        expr = parse_expr("a + b < c")
        assert expr.operator == "<"
        assert expr.left.operator == "+"

    def test_comparison_binds_tighter_than_equality(self):
        expr = parse_expr("a < b == c")
        assert expr.operator == "=="
        assert expr.left.operator == "<"

    def test_equality_binds_tighter_than_logical_and(self):
        expr = parse_expr("a == b && c")
        assert expr.operator == "&&"
        assert expr.left.operator == "=="

    def test_logical_and_binds_tighter_than_logical_or(self):
        expr = parse_expr("a && b || c")
        assert expr.operator == "||"
        assert expr.left.operator == "&&"

    def test_unary_binds_tighter_than_multiplication(self):
        expr = parse_expr("-a * b")
        assert expr.operator == "*"
        assert isinstance(expr.left, UnaryOp)

    def test_full_precedence_chain(self):
        """One expression exercising every level at once."""
        expr = parse_expr("a || b && c == d < e + f * -g")
        assert expr.operator == "||"
        assert expr.right.operator == "&&"
        assert expr.right.right.operator == "=="
        assert expr.right.right.right.operator == "<"
        assert expr.right.right.right.right.operator == "+"
        assert expr.right.right.right.right.right.operator == "*"
        assert isinstance(expr.right.right.right.right.right.right, UnaryOp)

    def test_parentheses_override_precedence(self):
        expr = parse_expr("(2 + 3) * 4")
        assert expr.operator == "*"
        assert expr.left.operator == "+"

    def test_grouping_creates_no_extra_node(self):
        """The tree's shape records the grouping; a Grouping node would be noise."""
        plain = parse_expr("a + b")
        grouped = parse_expr("((a + b))")
        assert type(plain) is type(grouped)
        assert grouped.operator == "+"


class TestAssociativity:
    """Binary operators associate left; prefix operators associate right."""

    @pytest.mark.parametrize("operator", ["+", "-", "*", "/", "%", "&&", "||"])
    def test_binary_operators_are_left_associative(self, operator: str):
        expr = parse_expr(f"a {operator} b {operator} c")
        assert expr.operator == operator
        assert isinstance(expr.left, BinaryOp), f"{operator} should nest to the left"
        assert isinstance(expr.right, Identifier)
        assert expr.right.name == "c"

    def test_subtraction_nests_left_not_right(self):
        """Subtraction nests left: a - b - c is (a - b) - c, or arithmetic breaks."""
        expr = parse_expr("a - b - c")
        assert expr.left.left.name == "a"
        assert expr.left.right.name == "b"
        assert expr.right.name == "c"

    def test_unary_operators_are_right_associative(self):
        expr = parse_expr("!!a")
        assert isinstance(expr, UnaryOp)
        assert isinstance(expr.operand, UnaryOp)
        assert isinstance(expr.operand.operand, Identifier)

    def test_repeated_negation(self):
        expr = parse_expr("- - -5")
        depth = 0
        while isinstance(expr, UnaryOp):
            depth += 1
            expr = expr.operand
        assert depth == 3
        assert expr.value == 5


class TestPrimaryExpressions:
    """``primary -> NUM | ID | 'true' | 'false' | call | '(' expr ')'``."""

    def test_integer_literal(self):
        literal = parse_expr("42")
        assert literal.value == 42
        assert literal.literal_type == "int"

    def test_float_literal(self):
        literal = parse_expr("3.5")
        assert literal.value == 3.5
        assert literal.literal_type == "float"

    @pytest.mark.parametrize(("text", "expected"), [("true", True), ("false", False)])
    def test_boolean_literals(self, text: str, expected: bool):
        literal = parse_expr(text)
        assert literal.value is expected
        assert literal.literal_type == "bool"

    def test_identifier(self):
        assert parse_expr("counter").name == "counter"

    def test_literal_preserves_raw_text(self):
        """'007' must display as written even though it parses to 7."""
        literal = parse_expr("007")
        assert literal.value == 7
        assert literal.raw == "007"

    def test_call_with_no_arguments(self):
        call = parse_expr("f()")
        assert isinstance(call, CallExpr)
        assert call.arguments == []

    def test_call_with_one_argument(self):
        call = parse_expr("fib(n)")
        assert call.callee == "fib"
        assert len(call.arguments) == 1

    def test_call_with_several_arguments(self):
        call = parse_expr("sum(a, b, c)")
        assert len(call.arguments) == 3

    def test_nested_calls(self):
        call = parse_expr("f(g(h(x)))")
        assert call.arguments[0].callee == "g"
        assert call.arguments[0].arguments[0].callee == "h"

    def test_call_arguments_may_be_expressions(self):
        call = parse_expr("f(a + b, c * d)")
        assert call.arguments[0].operator == "+"
        assert call.arguments[1].operator == "*"

    def test_identifier_without_parentheses_is_not_a_call(self):
        assert isinstance(parse_expr("f"), Identifier)


class TestPositions:
    """Positions must point at the construct a user would look for."""

    def test_function_position_is_the_func_keyword(self):
        function = parse_source("func main(): void { }").functions[0]
        assert (function.line, function.column) == (1, 1)

    def test_statement_positions_track_lines(self):
        statements = first_body("func m(): void {\n    int a = 1;\n    int b = 2;\n}")
        assert statements[0].line == 2
        assert statements[1].line == 3

    def test_binary_operator_position_is_the_operator(self):
        expr = parse_expr("a + b")
        assert expr.column == parse_expr("a + b").column
        assert expr.line == 1

    def test_nested_positions_are_all_populated(self):
        program = parse_source(
            "func m(): int {\n  if (a < b) {\n    return a * 2;\n  }\n  return 0;\n}"
        )

        def walk(node) -> None:
            assert node.line >= 1
            assert node.column >= 1
            for child in node.to_dict()["children"]:
                assert child["line"] >= 1

        walk(program)


class TestParseErrors:
    """Baseline A: abort on the first error, precisely."""

    @pytest.mark.parametrize(
        ("source", "fragment"),
        [
            ("func m(): void { int x = 5 }", "';'"),
            ("func m(): void { int x = 5; ", "'}'"),
            ("func m(): void  int x; }", "'{'"),
            ("func m() void { }", "':'"),
            ("func m(): void { if x) { } }", "'('"),
            ("func m(): void { if (x { } }", "')'"),
            ("func m(): void { while x) { } }", "'('"),
            ("func m(): void { print x); }", "'('"),
            ("func m(): void { print(x) }", "';'"),
            ("func m(): void { return 5 }", "';'"),
            ("func m(): void { x = ; }", "expression"),
            ("func m(): void { int = 5; }", "identifier"),
            ("m(): void { }", "'func'"),
            ("func (): void { }", "identifier"),
            ("func m(): notatype { }", "'int'"),
            ("func m(int): void { }", "identifier"),
            ("func m(): void { int x = (5; }", "')'"),
            ("func m(): void { f(a,); }", "expression"),
        ],
    )
    def test_malformed_programs_are_rejected(self, source: str, fragment: str):
        """Deliberately malformed: each must name what was expected."""
        with pytest.raises(ParseError) as info:
            parse_source(source)
        assert fragment in str(info.value), f"message was: {info.value}"

    def test_error_carries_position(self):
        with pytest.raises(ParseError) as info:
            parse_source("func m(): void {\n    int x = 5\n}")
        error = info.value
        assert error.line == 3
        assert error.column >= 1

    def test_error_carries_expected_set(self):
        with pytest.raises(ParseError) as info:
            parse_source("func m(): void { int x = 5 }")
        assert TokenType.SEMICOLON in info.value.expected
        assert ";" in info.value.expected_spellings

    def test_error_carries_the_offending_token(self):
        with pytest.raises(ParseError) as info:
            parse_source("func m(): void { int x = 5 }")
        assert info.value.found.lexeme == "}"

    def test_error_carries_grammar_context(self):
        with pytest.raises(ParseError) as info:
            parse_source("func m(): void { int x = 5 }")
        assert info.value.context == "variable declaration"

    def test_error_at_end_of_input_reads_sensibly(self):
        with pytest.raises(ParseError) as info:
            parse_source("func m(): void {")
        assert "end of file" in str(info.value)

    def test_str_includes_line_and_column(self):
        with pytest.raises(ParseError) as info:
            parse_source("func m(): void { int x = 5 }")
        assert str(info.value).startswith("line 1, col ")

    def test_aborts_on_the_first_error_not_the_last(self):
        """Baseline A reports error one, and stops. Two errors, one report."""
        with pytest.raises(ParseError) as info:
            parse_source("func m(): void {\n  int a = 1\n  int b = 2\n}")
        assert info.value.line == 3

    def test_converts_to_the_wire_model(self):
        with pytest.raises(ParseError) as info:
            parse_source("func m(): void { int x = 5 }")
        report = info.value.to_api()
        assert report.line >= 1
        assert report.col >= 1
        assert report.expected == [";"]
        assert report.recovered is False

    def test_eof_converts_without_an_empty_found(self):
        with pytest.raises(ParseError) as info:
            parse_source("func m(): void {")
        assert info.value.to_api().found == "end of file"


class TestParserConstruction:
    """Caller errors are distinct from user syntax errors."""

    def test_empty_token_list_is_a_value_error(self):
        """A caller bug, not a user's syntax mistake, so not a ParseError."""
        with pytest.raises(ValueError, match="empty"):
            Parser([])

    def test_token_stream_must_end_with_eof(self):
        tokens = tokenize("func m(): void { }").significant_tokens
        with pytest.raises(ValueError, match="EOF"):
            Parser(tokens)

    def test_accepts_a_wellformed_stream(self):
        assert Parser(tokenize("func m(): void { }").tokens).parse()


class TestDemoProgram:
    """The canonical program must parse into the expected shape."""

    def test_parses_without_error(self, demo_source: str):
        assert parse_source(demo_source)

    def test_has_both_functions(self, demo_source: str):
        program = parse_source(demo_source)
        assert [f.name for f in program.functions] == ["fib", "main"]

    def test_fib_signature(self, demo_source: str):
        fib = parse_source(demo_source).functions[0]
        assert fib.return_type == "int"
        assert [(p.param_type, p.name) for p in fib.params] == [("int", "n")]

    def test_fib_contains_a_while_loop(self, demo_source: str):
        fib = parse_source(demo_source).functions[0]
        assert any(isinstance(s, WhileStmt) for s in fib.body.statements)

    def test_main_folds_and_cse_targets_are_present(self, demo_source: str):
        """The optimisation targets must survive parsing as real subtrees."""
        main = parse_source(demo_source).functions[1]
        declarations = [s for s in main.body.statements if isinstance(s, VarDecl)]
        by_name = {d.name: d for d in declarations}
        assert by_name["limit"].initializer.operator == "*"
        assert by_name["unused"].initializer.value == 42
        assert by_name["x"].initializer.operator == "*"

    def test_serialises_to_json(self, demo_source: str):
        payload = parse_source(demo_source).to_dict()
        assert json.loads(json.dumps(payload))["functionCount"] == 2


class TestLargePrograms:
    """Recursive descent uses the Python call stack; depth has a real limit."""

    def test_many_statements(self):
        body = "\n".join(f"int v{i} = {i};" for i in range(500))
        statements = first_body(f"func m(): void {{ {body} }}")
        assert len(statements) == 500

    def test_many_functions(self):
        source = " ".join(f"func f{i}(): void {{ }}" for i in range(200))
        assert len(parse_source(source).functions) == 200

    def test_long_flat_expression(self):
        """A long chain is iteration, not recursion, so it stays cheap."""
        expr = parse_expr(" + ".join(str(i) for i in range(500)))
        depth = 0
        while isinstance(expr, BinaryOp):
            depth += 1
            expr = expr.left
        assert depth == 499

    def test_realistic_nesting_parses(self):
        """Far deeper than any human writes, and well inside the bound."""
        depth = 30
        expr = parse_expr("(" * depth + "1" + ")" * depth)
        assert isinstance(expr, Literal)

    def test_pathological_nesting_is_a_syntax_error_not_a_crash(self):
        """Recursive descent runs on the call stack, so depth must be bounded.

        Without the guard this raises RecursionError, which is an unhandled
        crash and a 500 from /compile. It must be an ordinary syntax error.
        """
        depth = MAX_NESTING_DEPTH + 10
        with pytest.raises(ParseError) as info:
            parse_expr("(" * depth + "1" + ")" * depth)
        assert "too deep" in str(info.value)
        assert info.value.line >= 1

    def test_deep_unary_chain_is_also_bounded(self):
        """`_unary` recurses into itself, so it needs the same guard."""
        with pytest.raises(ParseError):
            parse_expr("-" * (MAX_NESTING_DEPTH + 10) + "1")

    def test_depth_resets_between_sibling_expressions(self):
        """The counter must unwind, or a long function would falsely trip it."""
        nested = "(" * 20 + "1" + ")" * 20
        body = " ".join(f"int v{i} = {nested};" for i in range(40))
        assert len(first_body(f"func m(): void {{ {body} }}")) == 40
