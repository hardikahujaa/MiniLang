"""Abstract syntax tree node definitions for MiniLang.

The AST is the contract between the parser and every phase after it. The
semantic analyser walks it, the three-address code generator lowers it, and the
frontend draws it -- so the shape chosen here is load-bearing.

Design decisions
----------------
**Every node carries its position.** ``line`` and ``column`` are keyword-only
and required on every node, because a compiler that cannot say *where* a
problem is is not useful. Making them keyword-only keeps child constructors
readable: ``BinaryOp("+", left, right, line=3, column=11)`` rather than
``BinaryOp(3, 11, "+", left, right)``.

**Every node serialises itself.** :meth:`ASTNode.to_dict` is abstract, so a new
node type cannot be added without deciding how it is rendered. The dictionaries
it produces are directly consumable by D3's hierarchy layout -- each carries a
``name`` to draw and a ``children`` list to recurse into -- while also keeping
the node's own fields, so one serialisation serves both the AST view and the
tree view.

**Nodes are mutable.** Not frozen dataclasses: the semantic analyser annotates
expressions with their resolved type in place (see :attr:`Expr.inferred_type`),
which is what lets the Semantic tab show types on the tree.

**Grouping parentheses produce no node.** ``(a + b) * c`` and the precedence of
``*`` over ``+`` are both expressed by the tree's *shape*; a ``Grouping`` node
would be redundant structure that every later phase would have to skip over.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Base classes
# ---------------------------------------------------------------------------


@dataclass
class ASTNode(ABC):
    """Base class for every node in a MiniLang syntax tree.

    Attributes:
        line: 1-based source line where the construct begins.
        column: 1-based column where the construct begins.
    """

    line: int = field(kw_only=True)
    column: int = field(kw_only=True)

    @abstractmethod
    def to_dict(self) -> dict[str, Any]:
        """Serialise this node, and everything under it, to plain data.

        The result is JSON-encodable and directly renderable by D3: it always
        carries ``name`` (the label to draw) and ``children`` (the nodes to
        recurse into), alongside the node's own fields.

        Returns:
            A dictionary describing this subtree.
        """
        raise NotImplementedError

    def _node_dict(
        self,
        name: str,
        kind: str,
        children: list[ASTNode | None] | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        """Build the common part of a node's serialised form.

        Centralised so that every node emits the same envelope and a new node
        type cannot accidentally omit its position or its children key.

        Args:
            name: Label drawn on the node in the tree view.
            kind: Broad category used for styling, e.g. ``"expr"``.
            children: Child nodes, in source order. ``None`` entries are
                dropped, which lets callers pass optional children directly.
            **fields: Extra node-specific fields to include.

        Returns:
            The node's dictionary representation.
        """
        payload: dict[str, Any] = {
            "node": type(self).__name__,
            "name": name,
            "kind": kind,
            "line": self.line,
            "column": self.column,
        }
        payload.update(fields)
        payload["children"] = [child.to_dict() for child in (children or []) if child is not None]
        return payload


@dataclass
class Expr(ASTNode):
    """Base class for expressions -- constructs that produce a value.

    Attributes:
        inferred_type: The type the semantic analyser resolved for this
            expression, or ``None`` before analysis has run. Populated in
            phase 3; present here so type annotations can be attached in place
            rather than held in a side table.
    """

    inferred_type: str | None = field(default=None, kw_only=True)

    @abstractmethod
    def to_dict(self) -> dict[str, Any]:
        """Serialise this expression.

        Returns:
            A dictionary describing this subtree.
        """
        raise NotImplementedError


@dataclass
class Stmt(ASTNode):
    """Base class for statements -- constructs executed for their effect."""

    @abstractmethod
    def to_dict(self) -> dict[str, Any]:
        """Serialise this statement.

        Returns:
            A dictionary describing this subtree.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------


@dataclass
class Literal(Expr):
    """A literal constant: an integer, a float, or a boolean.

    Attributes:
        value: The Python value the literal denotes.
        literal_type: The MiniLang type name -- ``"int"``, ``"float"`` or
            ``"bool"``.
        raw: The exact source text, preserved so the tree view can show what
            was written (``007``) rather than what it parsed to (``7``).
    """

    value: int | float | bool
    literal_type: str
    raw: str

    def to_dict(self) -> dict[str, Any]:
        """Serialise this literal.

        Returns:
            A dictionary describing this node.
        """
        return self._node_dict(
            name=self.raw,
            kind="literal",
            value=self.value,
            literalType=self.literal_type,
            inferredType=self.inferred_type,
        )


@dataclass
class Identifier(Expr):
    """A reference to a variable or parameter by name.

    Attributes:
        name: The identifier's spelling.
    """

    name: str

    def to_dict(self) -> dict[str, Any]:
        """Serialise this identifier reference.

        Returns:
            A dictionary describing this node.
        """
        return self._node_dict(
            name=self.name,
            kind="identifier",
            identifier=self.name,
            inferredType=self.inferred_type,
        )


@dataclass
class BinaryOp(Expr):
    """A binary operation such as ``a + b`` or ``x < y``.

    Precedence and associativity are not stored: they are already expressed by
    where this node sits in the tree, which is what the recursive descent chain
    in :mod:`app.parser` establishes.

    Attributes:
        operator: The operator's source spelling, e.g. ``"+"`` or ``"&&"``.
        left: Left operand.
        right: Right operand.
    """

    operator: str
    left: Expr
    right: Expr

    def to_dict(self) -> dict[str, Any]:
        """Serialise this binary operation and both operands.

        Returns:
            A dictionary describing this subtree.
        """
        return self._node_dict(
            name=self.operator,
            kind="binary",
            children=[self.left, self.right],
            operator=self.operator,
            inferredType=self.inferred_type,
        )


@dataclass
class UnaryOp(Expr):
    """A prefix unary operation: logical ``!`` or arithmetic negation ``-``.

    Attributes:
        operator: The operator's source spelling.
        operand: The expression the operator applies to.
    """

    operator: str
    operand: Expr

    def to_dict(self) -> dict[str, Any]:
        """Serialise this unary operation and its operand.

        Returns:
            A dictionary describing this subtree.
        """
        return self._node_dict(
            name=f"unary {self.operator}",
            kind="unary",
            children=[self.operand],
            operator=self.operator,
            inferredType=self.inferred_type,
        )


@dataclass
class CallExpr(Expr):
    """A function call such as ``fib(n)``.

    Attributes:
        callee: Name of the function being called. MiniLang has no first-class
            functions, so this is a plain name rather than an expression.
        arguments: Argument expressions, in source order.
    """

    callee: str
    arguments: list[Expr]

    def to_dict(self) -> dict[str, Any]:
        """Serialise this call and its arguments.

        Returns:
            A dictionary describing this subtree.
        """
        return self._node_dict(
            name=f"call {self.callee}",
            kind="call",
            children=list(self.arguments),
            callee=self.callee,
            argumentCount=len(self.arguments),
            inferredType=self.inferred_type,
        )


# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------


@dataclass
class Block(Stmt):
    """A brace-delimited sequence of statements, and a new scope.

    Attributes:
        statements: The statements inside the braces, in source order.
    """

    statements: list[Stmt]

    def to_dict(self) -> dict[str, Any]:
        """Serialise this block and every statement in it.

        Returns:
            A dictionary describing this subtree.
        """
        return self._node_dict(
            name="block",
            kind="block",
            children=list(self.statements),
            statementCount=len(self.statements),
        )


@dataclass
class VarDecl(Stmt):
    """A variable declaration, with an optional initialiser.

    Attributes:
        var_type: Declared type name.
        name: The variable's name.
        initializer: The initialising expression, or ``None`` for a bare
            declaration such as ``int x;``.
    """

    var_type: str
    name: str
    initializer: Expr | None

    def to_dict(self) -> dict[str, Any]:
        """Serialise this declaration and its initialiser, if any.

        Returns:
            A dictionary describing this subtree.
        """
        return self._node_dict(
            name=f"{self.var_type} {self.name}",
            kind="declaration",
            children=[self.initializer],
            varType=self.var_type,
            identifier=self.name,
        )


@dataclass
class AssignStmt(Stmt):
    """An assignment to an existing variable.

    Attributes:
        target: Name of the variable being assigned to.
        value: The expression whose value is stored.
    """

    target: str
    value: Expr

    def to_dict(self) -> dict[str, Any]:
        """Serialise this assignment.

        Returns:
            A dictionary describing this subtree.
        """
        return self._node_dict(
            name=f"{self.target} =",
            kind="assign",
            children=[self.value],
            target=self.target,
        )


@dataclass
class IfStmt(Stmt):
    """A conditional, with an optional ``else`` branch.

    Attributes:
        condition: The guard expression.
        then_branch: Block executed when the condition holds.
        else_branch: Block executed otherwise, or ``None``.
    """

    condition: Expr
    then_branch: Block
    else_branch: Block | None

    def to_dict(self) -> dict[str, Any]:
        """Serialise this conditional and its branches.

        Returns:
            A dictionary describing this subtree.
        """
        return self._node_dict(
            name="if",
            kind="control",
            children=[self.condition, self.then_branch, self.else_branch],
            hasElse=self.else_branch is not None,
        )


@dataclass
class WhileStmt(Stmt):
    """A ``while`` loop.

    Attributes:
        condition: The guard expression, evaluated before each iteration.
        body: The loop body.
    """

    condition: Expr
    body: Block

    def to_dict(self) -> dict[str, Any]:
        """Serialise this loop.

        Returns:
            A dictionary describing this subtree.
        """
        return self._node_dict(
            name="while",
            kind="control",
            children=[self.condition, self.body],
        )


@dataclass
class ReturnStmt(Stmt):
    """A ``return``, with or without a value.

    Attributes:
        value: The returned expression, or ``None`` for a bare ``return;``.
    """

    value: Expr | None

    def to_dict(self) -> dict[str, Any]:
        """Serialise this return statement.

        Returns:
            A dictionary describing this subtree.
        """
        return self._node_dict(
            name="return",
            kind="control",
            children=[self.value],
            hasValue=self.value is not None,
        )


@dataclass
class PrintStmt(Stmt):
    """A ``print(expr);`` statement.

    Attributes:
        value: The expression whose value is printed.
    """

    value: Expr

    def to_dict(self) -> dict[str, Any]:
        """Serialise this print statement.

        Returns:
            A dictionary describing this subtree.
        """
        return self._node_dict(name="print", kind="io", children=[self.value])


@dataclass
class ExprStmt(Stmt):
    """An expression evaluated for its effect, such as a bare call.

    Attributes:
        expression: The evaluated expression.
    """

    expression: Expr

    def to_dict(self) -> dict[str, Any]:
        """Serialise this expression statement.

        Returns:
            A dictionary describing this subtree.
        """
        return self._node_dict(name="expr stmt", kind="statement", children=[self.expression])


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------


@dataclass
class Param(ASTNode):
    """One formal parameter of a function.

    Attributes:
        param_type: Declared type name.
        name: The parameter's name.
    """

    param_type: str
    name: str

    def to_dict(self) -> dict[str, Any]:
        """Serialise this parameter.

        Returns:
            A dictionary describing this node.
        """
        return self._node_dict(
            name=f"{self.param_type} {self.name}",
            kind="param",
            paramType=self.param_type,
            identifier=self.name,
        )


@dataclass
class FuncDecl(ASTNode):
    """A function definition.

    Attributes:
        name: The function's name.
        params: Formal parameters, in declaration order.
        return_type: Declared return type name.
        body: The function body.
    """

    name: str
    params: list[Param]
    return_type: str
    body: Block

    def to_dict(self) -> dict[str, Any]:
        """Serialise this function, its parameters and its body.

        Returns:
            A dictionary describing this subtree.
        """
        signature = ", ".join(f"{p.param_type} {p.name}" for p in self.params)
        return self._node_dict(
            name=f"func {self.name}({signature}): {self.return_type}",
            kind="function",
            children=[*self.params, self.body],
            identifier=self.name,
            returnType=self.return_type,
            arity=len(self.params),
        )


@dataclass
class Program(ASTNode):
    """The root of a MiniLang syntax tree: one or more function declarations.

    Attributes:
        functions: The program's functions, in source order.
    """

    functions: list[FuncDecl]

    def to_dict(self) -> dict[str, Any]:
        """Serialise the whole program.

        Returns:
            A dictionary describing the entire tree.
        """
        return self._node_dict(
            name="program",
            kind="program",
            children=list(self.functions),
            functionCount=len(self.functions),
        )
