"""Syntax analysis: a token stream to an abstract syntax tree.

Phase 2 of the pipeline. A hand-written recursive descent parser implementing
the MiniLang grammar from section 2 of the implementation plan.

How precedence works
--------------------
There is no precedence table and no operator-precedence algorithm here.
Precedence is expressed *structurally*, by the order in which the expression
rules call one another::

    expression -> logic_or -> logic_and -> equality
               -> comparison -> term -> factor -> unary -> primary

Each level consumes its own operators and then delegates to the next level
down for its operands. Because ``term`` (``+ -``) calls ``factor`` (``* / %``)
for both sides, a multiplication is always parsed into a subtree *below* an
addition -- so ``2 + 3 * 4`` produces ``(+ 2 (* 3 4))`` without anything ever
comparing two operators. Every binary level loops with ``while``, which makes
these operators left-associative; ``unary`` recurses into itself instead, which
makes prefix operators right-associative and lets ``!!x`` and ``--x`` parse.

Error handling: Baseline A
--------------------------
This parser aborts on the first syntax error by raising :class:`ParseError`.
That is deliberate, not a limitation to be fixed later: the plan's evaluation
(section 7) compares three parsers, and this is **Baseline A**, the control
condition that the recovering parser in ``app/errors.py`` is measured against.
Panic-mode synchronisation and ranked suggestions arrive in Tier B, as a
separate strategy layered over this one -- so this file must keep behaving
exactly this way for the benchmark to mean anything.
"""

from __future__ import annotations

from collections.abc import Callable
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
from app.lexer import TOKEN_SPELLING, Token, TokenType
from app.schemas import SyntaxErrorReport

#: Token types that may begin a type annotation: ``type -> int|float|bool|void``.
TYPE_TOKENS: Final[frozenset[TokenType]] = frozenset(
    {TokenType.INT, TokenType.FLOAT, TokenType.BOOL, TokenType.VOID}
)

#: Operators handled at each binary precedence level, loosest binding first.
#: Kept as data so the level methods stay one line of intent each.
_EQUALITY_OPS: Final[frozenset[TokenType]] = frozenset({TokenType.EQ, TokenType.NE})
_COMPARISON_OPS: Final[frozenset[TokenType]] = frozenset(
    {TokenType.LT, TokenType.GT, TokenType.LE, TokenType.GE}
)
_TERM_OPS: Final[frozenset[TokenType]] = frozenset({TokenType.PLUS, TokenType.MINUS})
_FACTOR_OPS: Final[frozenset[TokenType]] = frozenset(
    {TokenType.STAR, TokenType.SLASH, TokenType.PERCENT}
)
_UNARY_OPS: Final[frozenset[TokenType]] = frozenset({TokenType.BANG, TokenType.MINUS})

#: Maximum nesting depth for expressions.
#:
#: Recursive descent runs on the Python call stack. Each level of nesting costs
#: **15 stack frames** (measured, not estimated) as control walks the precedence
#: chain: eight rule methods, six ``_left_associative`` frames, and ``_primary``.
#: Against Python's default limit of 1000 that means an unguarded parser dies of
#: ``RecursionError`` at roughly 55 levels -- an unhandled crash, and a 500 from
#: ``/compile`` rather than a diagnostic.
#:
#: Bounding the depth explicitly turns that into an ordinary, positioned syntax
#: error. 32 costs about 480 frames, which leaves room for the deeper base stack
#: under uvicorn, and is still an order of magnitude past anything a human
#: writes -- the demo program peaks at 3.
MAX_NESTING_DEPTH: Final[int] = 32


def describe(token_type: TokenType) -> str:
    """Return a user-facing name for a token type.

    Error messages must talk about ``';'``, not ``SEMICOLON``. The spelling
    table lives in :mod:`app.lexer`, which is where it is authoritative.

    Args:
        token_type: The type to describe.

    Returns:
        The token's source spelling, or a readable description for classes
        such as identifiers that have no fixed spelling.
    """
    return TOKEN_SPELLING.get(token_type, token_type.value)


class ParseError(Exception):
    """A syntax error that aborts parsing.

    Carries everything needed to point at the problem precisely, and to rank
    candidate repairs later: the set of tokens that would have been legal, the
    token actually found, its position, and the grammar rule being parsed when
    the mismatch occurred.

    Attributes:
        message: Human-readable description of the problem.
        expected: Token types that would have been accepted here.
        found: The offending token.
        line: 1-based line of the offending token.
        column: 1-based column of the offending token.
        context: The grammar rule being parsed, e.g. ``"while statement"``.
    """

    def __init__(
        self,
        message: str,
        *,
        expected: list[TokenType],
        found: Token,
        context: str = "",
    ) -> None:
        """Initialise the error.

        Args:
            message: Human-readable description of the problem.
            expected: Token types that would have been accepted here.
            found: The token actually encountered.
            context: The grammar rule being parsed when this was raised.
        """
        super().__init__(message)
        self.message = message
        self.expected = expected
        self.found = found
        self.line = found.line
        self.column = found.col
        self.context = context

    @property
    def expected_spellings(self) -> list[str]:
        """Return the expected token set as user-facing spellings.

        Returns:
            One readable spelling per expected token type.
        """
        return [describe(token_type) for token_type in self.expected]

    def to_api(self) -> SyntaxErrorReport:
        """Convert to the wire model rendered in the Problems panel.

        Returns:
            A :class:`~app.schemas.SyntaxErrorReport` for this error.
        """
        found_text = (
            describe(TokenType.EOF) if self.found.type is TokenType.EOF else self.found.lexeme
        )
        return SyntaxErrorReport(
            line=self.line,
            col=self.column,
            message=self.message,
            expected=self.expected_spellings,
            found=found_text,
            recovered=False,
        )

    def __str__(self) -> str:
        """Return the message with its source position.

        Returns:
            A string of the form ``"line 3, col 11: <message>"``.
        """
        return f"line {self.line}, col {self.column}: {self.message}"


class Parser:
    """A recursive descent parser for MiniLang.

    One instance parses one token stream. The token list is expected to come
    from :func:`app.lexer.tokenize` and to end with an ``EOF`` token.

    Example:
        >>> from app.lexer import tokenize
        >>> program = Parser(tokenize("func main(): void {}").tokens).parse()
        >>> program.functions[0].name
        'main'
    """

    def __init__(self, tokens: list[Token]) -> None:
        """Prepare to parse a token stream.

        Args:
            tokens: Tokens to parse, terminated by an ``EOF`` token.

        Raises:
            ValueError: If the token stream is empty or is not ``EOF``-terminated.
                Both indicate a caller bug rather than a user syntax error, so
                they are not :class:`ParseError`.
        """
        if not tokens:
            raise ValueError("token stream is empty; expected at least an EOF token")
        if tokens[-1].type is not TokenType.EOF:
            raise ValueError("token stream must be terminated by an EOF token")
        self._tokens = tokens
        self._pos = 0
        self._depth = 0

    # -- Public API ------------------------------------------------------

    def parse(self) -> Program:
        """Parse the token stream into a syntax tree.

        Returns:
            The :class:`~app.ast_nodes.Program` at the root of the tree.

        Raises:
            ParseError: On the first syntax error encountered. This parser is
                Baseline A and does not recover.
        """
        return self._program()

    # -- Cursor ----------------------------------------------------------

    def _peek(self) -> Token:
        """Return the current token without consuming it."""
        return self._tokens[self._pos]

    def _peek_next(self) -> Token:
        """Return the token after the current one, or ``EOF`` at the end."""
        if self._pos + 1 >= len(self._tokens):
            return self._tokens[-1]
        return self._tokens[self._pos + 1]

    def _previous(self) -> Token:
        """Return the most recently consumed token."""
        return self._tokens[self._pos - 1]

    def _is_at_end(self) -> bool:
        """Return whether the cursor has reached the ``EOF`` token."""
        return self._peek().type is TokenType.EOF

    def _advance(self) -> Token:
        """Consume and return the current token.

        The ``EOF`` token is never consumed, so a rule that keeps advancing on
        malformed input cannot run off the end of the list.

        Returns:
            The consumed token.
        """
        if not self._is_at_end():
            self._pos += 1
        return self._previous()

    def _check(self, token_type: TokenType) -> bool:
        """Return whether the current token has the given type.

        Args:
            token_type: The type to test for.

        Returns:
            True if the current token matches.
        """
        return self._peek().type is token_type

    def _check_any(self, token_types: frozenset[TokenType]) -> bool:
        """Return whether the current token is one of several types.

        Args:
            token_types: The types to test for.

        Returns:
            True if the current token matches any of them.
        """
        return self._peek().type in token_types

    def _match(self, token_types: frozenset[TokenType]) -> bool:
        """Consume the current token if it is one of the given types.

        Args:
            token_types: Acceptable types.

        Returns:
            True if a token was consumed.
        """
        if self._check_any(token_types):
            self._advance()
            return True
        return False

    def _expect(self, token_type: TokenType, context: str) -> Token:
        """Consume a token of the required type, or fail.

        Args:
            token_type: The type that must appear here.
            context: The grammar rule being parsed, used in the message.

        Returns:
            The consumed token.

        Raises:
            ParseError: If the current token is of a different type.
        """
        if self._check(token_type):
            return self._advance()
        raise self._error([token_type], context)

    def _enter(self) -> None:
        """Record entry into a nested expression, enforcing the depth bound.

        Raises:
            ParseError: If :data:`MAX_NESTING_DEPTH` would be exceeded. Reported
                as a normal syntax error so that pathological input produces a
                diagnostic rather than a ``RecursionError``.
        """
        self._depth += 1
        if self._depth > MAX_NESTING_DEPTH:
            found = self._peek()
            raise ParseError(
                f"expression nesting is too deep (limit {MAX_NESTING_DEPTH})",
                expected=[],
                found=found,
                context="expression",
            )

    def _leave(self) -> None:
        """Record leaving a nested expression."""
        self._depth -= 1

    def _error(self, expected: list[TokenType], context: str) -> ParseError:
        """Build a :class:`ParseError` describing a mismatch at the cursor.

        Returns the exception rather than raising it, so that every call site
        reads ``raise self._error(...)``. That keeps the control flow explicit
        at the point of failure -- a helper that raised would make its callers
        look as though they fall off the end without returning.

        Args:
            expected: Token types that would have been legal here.
            context: The grammar rule being parsed.

        Returns:
            The error to raise.
        """
        found = self._peek()
        found_text = describe(TokenType.EOF) if found.type is TokenType.EOF else repr(found.lexeme)
        wanted = " or ".join(repr(describe(t)) for t in expected)
        where = f" in {context}" if context else ""
        return ParseError(
            f"expected {wanted}{where}, but found {found_text}",
            expected=expected,
            found=found,
            context=context,
        )

    # -- Grammar: program and declarations -------------------------------

    def _program(self) -> Program:
        """Parse ``program -> funcDecl+``.

        Returns:
            The root program node.

        Raises:
            ParseError: If the program contains no functions, or a declaration
                is malformed.
        """
        start = self._peek()
        functions: list[FuncDecl] = []

        if self._is_at_end():
            raise self._error([TokenType.FUNC], "program")

        while not self._is_at_end():
            functions.append(self._func_decl())

        return Program(functions=functions, line=start.line, column=start.col)

    def _func_decl(self) -> FuncDecl:
        """Parse ``funcDecl -> 'func' ID '(' params? ')' ':' type block``.

        Returns:
            The parsed function declaration.

        Raises:
            ParseError: If the declaration is malformed.
        """
        keyword = self._expect(TokenType.FUNC, "function declaration")
        name = self._expect(TokenType.IDENT, "function name")
        self._expect(TokenType.LPAREN, "function parameter list")

        params = [] if self._check(TokenType.RPAREN) else self._params()

        self._expect(TokenType.RPAREN, "function parameter list")
        self._expect(TokenType.COLON, "function return type")
        return_type = self._type("function return type")
        body = self._block()

        return FuncDecl(
            name=name.lexeme,
            params=params,
            return_type=return_type,
            body=body,
            line=keyword.line,
            column=keyword.col,
        )

    def _params(self) -> list[Param]:
        """Parse ``params -> type ID (',' type ID)*``.

        Returns:
            The declared parameters, in order.

        Raises:
            ParseError: If a parameter is malformed.
        """
        params = [self._param()]
        while self._check(TokenType.COMMA):
            self._advance()
            params.append(self._param())
        return params

    def _param(self) -> Param:
        """Parse one ``type ID`` parameter.

        Returns:
            The parsed parameter.

        Raises:
            ParseError: If the type or name is missing.
        """
        start = self._peek()
        param_type = self._type("parameter")
        name = self._expect(TokenType.IDENT, "parameter name")
        return Param(
            param_type=param_type,
            name=name.lexeme,
            line=start.line,
            column=start.col,
        )

    def _type(self, context: str) -> str:
        """Parse ``type -> 'int' | 'float' | 'bool' | 'void'``.

        ``void`` is accepted anywhere a type is legal, including variable
        declarations. That follows the grammar exactly; rejecting ``void x;``
        is the semantic analyser's job, not the parser's.

        Args:
            context: The grammar rule being parsed, used in the message.

        Returns:
            The type's source spelling.

        Raises:
            ParseError: If the current token is not a type keyword.
        """
        if not self._check_any(TYPE_TOKENS):
            raise self._error(sorted(TYPE_TOKENS, key=lambda t: t.value), context)
        return self._advance().lexeme

    # -- Grammar: statements ---------------------------------------------

    def _block(self) -> Block:
        """Parse ``block -> '{' stmt* '}'``.

        Returns:
            The parsed block.

        Raises:
            ParseError: If a brace is missing or a statement is malformed.
        """
        opening = self._expect(TokenType.LBRACE, "block")
        statements: list[Stmt] = []

        while not self._check(TokenType.RBRACE) and not self._is_at_end():
            statements.append(self._statement())

        self._expect(TokenType.RBRACE, "block")
        return Block(statements=statements, line=opening.line, column=opening.col)

    def _statement(self) -> Stmt:
        """Parse one statement, dispatching on the current token.

        Distinguishing an assignment from an expression statement is the only
        place this grammar needs two tokens of look-ahead: both may begin with
        an identifier, and only the following ``=`` tells them apart.

        Returns:
            The parsed statement.

        Raises:
            ParseError: If the statement is malformed.
        """
        if self._check_any(TYPE_TOKENS):
            return self._var_decl()
        if self._check(TokenType.IF):
            return self._if_stmt()
        if self._check(TokenType.WHILE):
            return self._while_stmt()
        if self._check(TokenType.RETURN):
            return self._return_stmt()
        if self._check(TokenType.PRINT):
            return self._print_stmt()
        if self._check(TokenType.IDENT) and self._peek_next().type is TokenType.ASSIGN:
            return self._assign_stmt()
        return self._expr_stmt()

    def _var_decl(self) -> VarDecl:
        """Parse ``varDecl -> type ID ('=' expr)? ';'``.

        Returns:
            The parsed declaration.

        Raises:
            ParseError: If the declaration is malformed.
        """
        start = self._peek()
        var_type = self._type("variable declaration")
        name = self._expect(TokenType.IDENT, "variable name")

        initializer: Expr | None = None
        if self._check(TokenType.ASSIGN):
            self._advance()
            initializer = self._expression()

        self._expect(TokenType.SEMICOLON, "variable declaration")
        return VarDecl(
            var_type=var_type,
            name=name.lexeme,
            initializer=initializer,
            line=start.line,
            column=start.col,
        )

    def _assign_stmt(self) -> AssignStmt:
        """Parse ``assign -> ID '=' expr ';'``.

        Returns:
            The parsed assignment.

        Raises:
            ParseError: If the assignment is malformed.
        """
        target = self._expect(TokenType.IDENT, "assignment")
        self._expect(TokenType.ASSIGN, "assignment")
        value = self._expression()
        self._expect(TokenType.SEMICOLON, "assignment")
        return AssignStmt(
            target=target.lexeme,
            value=value,
            line=target.line,
            column=target.col,
        )

    def _if_stmt(self) -> IfStmt:
        """Parse ``ifStmt -> 'if' '(' expr ')' block ('else' block)?``.

        Both branches are blocks, so this grammar has no dangling-else
        ambiguity: an ``else`` can only ever attach to the ``if`` whose braces
        enclose it.

        Returns:
            The parsed conditional.

        Raises:
            ParseError: If the statement is malformed.
        """
        keyword = self._expect(TokenType.IF, "if statement")
        self._expect(TokenType.LPAREN, "if condition")
        condition = self._expression()
        self._expect(TokenType.RPAREN, "if condition")
        then_branch = self._block()

        else_branch: Block | None = None
        if self._check(TokenType.ELSE):
            self._advance()
            else_branch = self._block()

        return IfStmt(
            condition=condition,
            then_branch=then_branch,
            else_branch=else_branch,
            line=keyword.line,
            column=keyword.col,
        )

    def _while_stmt(self) -> WhileStmt:
        """Parse ``whileStmt -> 'while' '(' expr ')' block``.

        Returns:
            The parsed loop.

        Raises:
            ParseError: If the statement is malformed.
        """
        keyword = self._expect(TokenType.WHILE, "while statement")
        self._expect(TokenType.LPAREN, "while condition")
        condition = self._expression()
        self._expect(TokenType.RPAREN, "while condition")
        body = self._block()
        return WhileStmt(
            condition=condition,
            body=body,
            line=keyword.line,
            column=keyword.col,
        )

    def _return_stmt(self) -> ReturnStmt:
        """Parse ``returnStmt -> 'return' expr? ';'``.

        Returns:
            The parsed return statement.

        Raises:
            ParseError: If the statement is malformed.
        """
        keyword = self._expect(TokenType.RETURN, "return statement")
        value: Expr | None = None
        if not self._check(TokenType.SEMICOLON):
            value = self._expression()
        self._expect(TokenType.SEMICOLON, "return statement")
        return ReturnStmt(value=value, line=keyword.line, column=keyword.col)

    def _print_stmt(self) -> PrintStmt:
        """Parse ``printStmt -> 'print' '(' expr ')' ';'``.

        Returns:
            The parsed print statement.

        Raises:
            ParseError: If the statement is malformed.
        """
        keyword = self._expect(TokenType.PRINT, "print statement")
        self._expect(TokenType.LPAREN, "print statement")
        value = self._expression()
        self._expect(TokenType.RPAREN, "print statement")
        self._expect(TokenType.SEMICOLON, "print statement")
        return PrintStmt(value=value, line=keyword.line, column=keyword.col)

    def _expr_stmt(self) -> ExprStmt:
        """Parse ``exprStmt -> expr ';'``.

        Returns:
            The parsed expression statement.

        Raises:
            ParseError: If the expression or the semicolon is malformed.
        """
        start = self._peek()
        expression = self._expression()
        self._expect(TokenType.SEMICOLON, "expression statement")
        return ExprStmt(expression=expression, line=start.line, column=start.col)

    # -- Grammar: expressions --------------------------------------------
    #
    # One method per precedence level, loosest binding first. Each consumes its
    # own operators and delegates to the next level down for its operands; the
    # resulting call chain *is* the precedence table.

    def _expression(self) -> Expr:
        """Parse ``expr -> logicOr``.

        Returns:
            The parsed expression.

        Raises:
            ParseError: If the expression is malformed, or nests too deeply.
        """
        self._enter()
        try:
            return self._logic_or()
        finally:
            self._leave()

    def _logic_or(self) -> Expr:
        """Parse ``logicOr -> logicAnd ('||' logicAnd)*``.

        Returns:
            The parsed expression, left-associative.

        Raises:
            ParseError: If an operand is malformed.
        """
        return self._left_associative(frozenset({TokenType.OR}), self._logic_and)

    def _logic_and(self) -> Expr:
        """Parse ``logicAnd -> equality ('&&' equality)*``.

        Returns:
            The parsed expression, left-associative.

        Raises:
            ParseError: If an operand is malformed.
        """
        return self._left_associative(frozenset({TokenType.AND}), self._equality)

    def _equality(self) -> Expr:
        """Parse ``equality -> compare (('==' | '!=') compare)*``.

        Returns:
            The parsed expression, left-associative.

        Raises:
            ParseError: If an operand is malformed.
        """
        return self._left_associative(_EQUALITY_OPS, self._comparison)

    def _comparison(self) -> Expr:
        """Parse ``compare -> term (('<' | '>' | '<=' | '>=') term)*``.

        Returns:
            The parsed expression, left-associative.

        Raises:
            ParseError: If an operand is malformed.
        """
        return self._left_associative(_COMPARISON_OPS, self._term)

    def _term(self) -> Expr:
        """Parse ``term -> factor (('+' | '-') factor)*``.

        Returns:
            The parsed expression, left-associative.

        Raises:
            ParseError: If an operand is malformed.
        """
        return self._left_associative(_TERM_OPS, self._factor)

    def _factor(self) -> Expr:
        """Parse ``factor -> unary (('*' | '/' | '%') unary)*``.

        Returns:
            The parsed expression, left-associative.

        Raises:
            ParseError: If an operand is malformed.
        """
        return self._left_associative(_FACTOR_OPS, self._unary)

    def _left_associative(
        self,
        operators: frozenset[TokenType],
        operand: Callable[[], Expr],
    ) -> Expr:
        """Parse one left-associative binary precedence level.

        Every binary level in this grammar has the identical shape
        ``operand (OP operand)*``; factoring it out keeps the six level methods
        above to a single line of intent each, and guarantees they cannot drift
        apart in associativity.

        Args:
            operators: Operators handled at this level.
            operand: Parser for the next-tighter level.

        Returns:
            The parsed expression, nested left-to-right.

        Raises:
            ParseError: If an operand is malformed.
        """
        left = operand()
        while self._check_any(operators):
            operator = self._advance()
            right = operand()
            left = BinaryOp(
                operator=operator.lexeme,
                left=left,
                right=right,
                line=operator.line,
                column=operator.col,
            )
        return left

    def _unary(self) -> Expr:
        """Parse ``unary -> ('!' | '-') unary | primary``.

        Recurses into itself rather than looping, which makes prefix operators
        right-associative and lets ``!!flag`` and ``--n`` parse.

        Returns:
            The parsed expression.

        Raises:
            ParseError: If the operand is malformed.
        """
        if self._check_any(_UNARY_OPS):
            operator = self._advance()
            self._enter()
            try:
                operand = self._unary()
            finally:
                self._leave()
            return UnaryOp(
                operator=operator.lexeme,
                operand=operand,
                line=operator.line,
                column=operator.col,
            )
        return self._primary()

    def _primary(self) -> Expr:
        """Parse ``primary -> NUM | ID | 'true' | 'false' | call | '(' expr ')'``.

        Returns:
            The parsed expression.

        Raises:
            ParseError: If no primary expression begins here.
        """
        token = self._peek()

        if self._check(TokenType.INT_LIT):
            self._advance()
            return Literal(
                value=int(token.lexeme),
                literal_type="int",
                raw=token.lexeme,
                line=token.line,
                column=token.col,
            )

        if self._check(TokenType.FLOAT_LIT):
            self._advance()
            return Literal(
                value=float(token.lexeme),
                literal_type="float",
                raw=token.lexeme,
                line=token.line,
                column=token.col,
            )

        if self._check_any(frozenset({TokenType.TRUE, TokenType.FALSE})):
            self._advance()
            return Literal(
                value=token.type is TokenType.TRUE,
                literal_type="bool",
                raw=token.lexeme,
                line=token.line,
                column=token.col,
            )

        if self._check(TokenType.IDENT):
            self._advance()
            if self._check(TokenType.LPAREN):
                return self._finish_call(token)
            return Identifier(name=token.lexeme, line=token.line, column=token.col)

        if self._check(TokenType.LPAREN):
            self._advance()
            grouped = self._expression()
            self._expect(TokenType.RPAREN, "parenthesised expression")
            # No Grouping node: the tree's shape already records the grouping.
            return grouped

        raise self._error(
            [
                TokenType.INT_LIT,
                TokenType.FLOAT_LIT,
                TokenType.IDENT,
                TokenType.TRUE,
                TokenType.FALSE,
                TokenType.LPAREN,
            ],
            "expression",
        )

    def _finish_call(self, callee: Token) -> CallExpr:
        """Parse the argument list of a call, after its name was consumed.

        Args:
            callee: The identifier token naming the function.

        Returns:
            The parsed call expression.

        Raises:
            ParseError: If the argument list is malformed.
        """
        self._expect(TokenType.LPAREN, "call arguments")
        arguments: list[Expr] = []

        if not self._check(TokenType.RPAREN):
            arguments.append(self._expression())
            while self._check(TokenType.COMMA):
                self._advance()
                arguments.append(self._expression())

        self._expect(TokenType.RPAREN, "call arguments")
        return CallExpr(
            callee=callee.lexeme,
            arguments=arguments,
            line=callee.line,
            column=callee.col,
        )


def parse(tokens: list[Token]) -> Program:
    """Parse a token stream into a syntax tree.

    The module's entry point; prefer this over constructing a :class:`Parser`
    directly.

    Args:
        tokens: Tokens from :func:`app.lexer.tokenize`, ``EOF``-terminated.

    Returns:
        The root :class:`~app.ast_nodes.Program` node.

    Raises:
        ParseError: On the first syntax error. This parser does not recover.
        ValueError: If the token stream is empty or not ``EOF``-terminated.
    """
    return Parser(tokens).parse()
