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
node type cannot be added without deciding how it is rendered.

**Serialisation is a textbook projection, not a mirror of the dataclass.**
This is the important decision in this module. In memory, ``FuncDecl.name`` is
a ``str`` -- that is the right representation for a compiler, and every later
phase wants it that way. But a *drawn* abstract syntax tree must not collapse a
construct into a caption: an examiner looking at ``func fib(int n): int`` in a
single box cannot see that the identifier, the parameter list, the return type
and the body are four distinct syntactic children. So :meth:`to_dict` expands
those strings into real child nodes -- ``Identifier``, ``Type``, ``ParamList``,
``ArgList`` -- that exist only in the serialised form. The parser stays simple,
the semantic analyser stays simple, and the JSON is strictly textbook.

**Every child knows its role.** Children are declared as ``(role, node)`` slots
and each child is stamped with an ``edge`` naming the role it fills in its
parent: ``left``/``right`` on a :class:`BinaryOp`, ``cond``/``then``/``else`` on
an :class:`IfStmt`. Without this an AST drawing is ambiguous -- ``a - b`` and
``b - a`` have identical node sets and differ only by which operand is which.

**Nodes are mutable.** Not frozen dataclasses: the semantic analyser annotates
expressions with their resolved type in place (see :attr:`Expr.inferred_type`),
which is what lets the Semantic tab show types on the tree.

**Grouping parentheses produce no node.** ``(a + b) * c`` and the precedence of
``*`` over ``+`` are both expressed by the tree's *shape*; a ``Grouping`` node
would be redundant structure that every later phase would have to skip over.
Precedence is visible as depth: the more tightly an operator binds, the deeper
it sits, and the deepest subexpression is the one evaluated first.

The serialised envelope
-----------------------
Every node, real or synthetic, is a dictionary with these keys::

    node     the formal AST class name  -- "BinaryOp"     (drawn, line 1)
    detail   the datum the node holds   -- "*" or None    (drawn, line 2)
    name     node and detail together   -- "BinaryOp (*)" (convenience/tooltip)
    kind     broad category for styling -- "binary"
    edge     role filled in the parent  -- "left", or None at the root
    line     1-based source line
    column   1-based source column
    children child nodes, in source order

plus whatever node-specific fields the node adds (``operator``, ``literalType``,
``arity``, ...). ``detail`` is deliberately separate from a node's own ``value``
field: :class:`Literal` keeps ``value`` as the *Python* value for constant
folding, while ``detail`` carries the source spelling that gets drawn.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

#: One child slot: the role the child fills in its parent, and the child. The
#: child may be a live node, an already-serialised synthetic node, or ``None``
#: for an absent optional child such as a missing ``else``.
ChildSlot = tuple[str, "ASTNode | dict[str, Any] | None"]

#: A source position: 1-based line, 1-based column.
Position = tuple[int, int]


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _at(position: Position | None, line: int, column: int) -> Position:
    """Resolve a synthetic child's own position.

    A declaration stores its name as a ``str``, so the ``Identifier`` node the
    tree draws has no position of its own unless the parser recorded one. When
    it did, that is the position to report -- a diagnostic on an undeclared
    variable must point at the variable, not at the start of the statement that
    declares it. When it did not, falling back to the owner keeps every node
    positioned rather than emitting a null.

    Args:
        position: The recorded position, or ``None``.
        line: The owning construct's line, used as the fallback.
        column: The owning construct's column, used as the fallback.

    Returns:
        The position to attach to the synthetic node.
    """
    return position if position is not None else (line, column)


def _label(node: str, detail: Any) -> str:
    """Compose the one-line label for a node.

    Args:
        node: The formal AST class name.
        detail: The datum the node holds, or ``None`` if it holds none.

    Returns:
        ``"Identifier (x)"`` when there is a datum, ``"Block"`` when there is
        not. Never a flattened rendering of the node's children.
    """
    return f"{node} ({detail})" if detail is not None else node


def _resolve(children: Sequence[ChildSlot] | None) -> list[dict[str, Any]]:
    """Serialise child slots, stamping each child with the role it fills.

    Args:
        children: Child slots in source order. Slots whose node is ``None`` are
            dropped, which lets callers pass optional children directly.

    Returns:
        The serialised children, each carrying its ``edge``.
    """
    resolved: list[dict[str, Any]] = []
    for role, child in children or []:
        if child is None:
            continue
        payload = child.to_dict() if isinstance(child, ASTNode) else child
        payload["edge"] = role
        resolved.append(payload)
    return resolved


def _synthetic(
    node: str,
    kind: str,
    *,
    line: int,
    column: int,
    detail: Any = None,
    children: Sequence[ChildSlot] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Build a serialised node that has no dataclass behind it.

    Used for the structural nodes that exist only in the drawn tree --
    ``Identifier`` at a declaration site, ``Type``, ``ParamList``, ``ArgList``.
    They take their position from the construct that owns them, so every node
    in the output still answers "where in the source did this come from?".

    Args:
        node: The formal AST class name to draw.
        kind: Broad category used for styling.
        line: 1-based source line.
        column: 1-based source column.
        detail: The datum this node holds, if any.
        children: Child slots, in source order.
        **fields: Extra node-specific fields to include.

    Returns:
        The node's dictionary representation.
    """
    payload: dict[str, Any] = {
        "node": node,
        "detail": detail,
        "name": _label(node, detail),
        "kind": kind,
        "edge": None,
    }
    # No node-specific field shares a name with an envelope key, so this cannot
    # clobber the label. Position and children are written afterwards so that a
    # stray field can never displace them either.
    payload.update(fields)
    payload["line"] = line
    payload["column"] = column
    payload["children"] = _resolve(children)
    return payload


def _identifier_node(name: str, *, line: int, column: int) -> dict[str, Any]:
    """Build the ``Identifier`` node for a name that is stored as a string.

    A declaration's name, an assignment's target and a call's callee are all
    plain ``str`` on their dataclass. In a drawn tree each is a node.

    Args:
        name: The identifier's spelling.
        line: 1-based source line of the owning construct.
        column: 1-based source column of the owning construct.

    Returns:
        A serialised ``Identifier`` node.
    """
    return _synthetic(
        "Identifier", "identifier", line=line, column=column, detail=name, identifier=name
    )


def _type_node(type_name: str, *, line: int, column: int) -> dict[str, Any]:
    """Build the ``Type`` node for a declared type annotation.

    Args:
        type_name: The type's spelling, e.g. ``"int"``.
        line: 1-based source line of the owning construct.
        column: 1-based source column of the owning construct.

    Returns:
        A serialised ``Type`` node.
    """
    return _synthetic(
        "Type", "type", line=line, column=column, detail=type_name, typeName=type_name
    )


def _indexed(role: str, nodes: Sequence[Any]) -> list[ChildSlot]:
    """Number a homogeneous run of children so each edge is distinct.

    Args:
        role: Singular role name, e.g. ``"stmt"``.
        nodes: The children, in source order.

    Returns:
        Slots labelled ``stmt 0``, ``stmt 1``, ... so that sibling order is
        readable off the drawing rather than inferred from position.
    """
    return [(f"{role} {index}", node) for index, node in enumerate(nodes)]


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

        The result is JSON-encodable and directly renderable by D3.

        Returns:
            A dictionary describing this subtree.
        """
        raise NotImplementedError

    def _node_dict(
        self,
        kind: str,
        *,
        detail: Any = None,
        children: Sequence[ChildSlot] | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        """Build the common part of a node's serialised form.

        Centralised so that every node emits the same envelope and a new node
        type cannot accidentally omit its position, its children key, or the
        role labels on its edges.

        Args:
            kind: Broad category used for styling, e.g. ``"expr"``.
            detail: The datum this node holds, if any.
            children: Child slots, in source order.
            **fields: Extra node-specific fields to include.

        Returns:
            The node's dictionary representation.
        """
        return _synthetic(
            type(self).__name__,
            kind,
            line=self.line,
            column=self.column,
            detail=detail,
            children=children,
            **fields,
        )


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

        ``detail`` shows the source spelling; ``value`` keeps the Python value
        that constant folding will read.

        Returns:
            A dictionary describing this node.
        """
        return self._node_dict(
            "literal",
            detail=self.raw,
            value=self.value,
            raw=self.raw,
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
            "identifier",
            detail=self.name,
            identifier=self.name,
            inferredType=self.inferred_type,
        )


@dataclass
class BinaryOp(Expr):
    """A binary operation such as ``a + b`` or ``x < y``.

    Precedence and associativity are not stored: they are already expressed by
    where this node sits in the tree, which is what the recursive descent chain
    in :mod:`app.parser` establishes. The two operands are serialised under
    explicit ``left`` and ``right`` edges, because an unlabelled drawing of
    ``a - b`` is indistinguishable from one of ``b - a``.

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
            "binary",
            detail=self.operator,
            children=[("left", self.left), ("right", self.right)],
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

        The class name alone distinguishes this from subtraction, so the label
        no longer needs the old ``"unary -"`` prefix.

        Returns:
            A dictionary describing this subtree.
        """
        return self._node_dict(
            "unary",
            detail=self.operator,
            children=[("operand", self.operand)],
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
        """Serialise this call, its callee and its argument list.

        The callee becomes an ``Identifier`` node and the arguments are grouped
        under an ``ArgList``, so a call with no arguments still shows the empty
        list it has rather than looking like a leaf.

        Returns:
            A dictionary describing this subtree.
        """
        arg_list = _synthetic(
            "ArgList",
            "list",
            line=self.line,
            column=self.column,
            children=_indexed("arg", self.arguments),
            argumentCount=len(self.arguments),
        )
        return self._node_dict(
            "call",
            detail=self.callee,
            children=[
                ("callee", _identifier_node(self.callee, line=self.line, column=self.column)),
                ("args", arg_list),
            ],
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
            "block",
            children=_indexed("stmt", self.statements),
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
    name_pos: Position | None = field(default=None, kw_only=True)

    def to_dict(self) -> dict[str, Any]:
        """Serialise this declaration: type, name and initialiser as children.

        The node's own position is the type keyword, so the ``Type`` child
        shares it; the name sits further along the line and carries its own.

        Returns:
            A dictionary describing this subtree.
        """
        name_line, name_column = _at(self.name_pos, self.line, self.column)
        return self._node_dict(
            "declaration",
            children=[
                ("type", _type_node(self.var_type, line=self.line, column=self.column)),
                ("name", _identifier_node(self.name, line=name_line, column=name_column)),
                ("init", self.initializer),
            ],
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
        """Serialise this assignment, with the target as an ``Identifier``.

        Returns:
            A dictionary describing this subtree.
        """
        return self._node_dict(
            "assign",
            children=[
                ("target", _identifier_node(self.target, line=self.line, column=self.column)),
                ("value", self.value),
            ],
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
            "control",
            children=[
                ("cond", self.condition),
                ("then", self.then_branch),
                ("else", self.else_branch),
            ],
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
            "control",
            children=[("cond", self.condition), ("body", self.body)],
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
            "control",
            children=[("value", self.value)],
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
        return self._node_dict("io", children=[("value", self.value)])


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
        return self._node_dict("statement", children=[("expr", self.expression)])


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
    name_pos: Position | None = field(default=None, kw_only=True)

    def to_dict(self) -> dict[str, Any]:
        """Serialise this parameter as a type child and a name child.

        Returns:
            A dictionary describing this subtree.
        """
        name_line, name_column = _at(self.name_pos, self.line, self.column)
        return self._node_dict(
            "param",
            children=[
                ("type", _type_node(self.param_type, line=self.line, column=self.column)),
                ("name", _identifier_node(self.name, line=name_line, column=name_column)),
            ],
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
    name_pos: Position | None = field(default=None, kw_only=True)
    return_type_pos: Position | None = field(default=None, kw_only=True)

    def to_dict(self) -> dict[str, Any]:
        """Serialise this function as four labelled branches.

        ``name``, ``params``, ``returns`` and ``body`` -- never as one caption.
        The old flattened signature survives as a ``signature`` field for the
        tooltip, so the information is not lost, only demoted out of the label.

        Returns:
            A dictionary describing this subtree.
        """
        signature = ", ".join(f"{p.param_type} {p.name}" for p in self.params)
        param_list = _synthetic(
            "ParamList",
            "list",
            line=self.line,
            column=self.column,
            children=_indexed("param", self.params),
            arity=len(self.params),
        )
        name_line, name_column = _at(self.name_pos, self.line, self.column)
        type_line, type_column = _at(self.return_type_pos, self.line, self.column)
        return self._node_dict(
            "function",
            detail=self.name,
            children=[
                ("name", _identifier_node(self.name, line=name_line, column=name_column)),
                ("params", param_list),
                ("returns", _type_node(self.return_type, line=type_line, column=type_column)),
                ("body", self.body),
            ],
            identifier=self.name,
            returnType=self.return_type,
            arity=len(self.params),
            signature=f"func {self.name}({signature}): {self.return_type}",
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
            "program",
            children=_indexed("func", self.functions),
            functionCount=len(self.functions),
        )
