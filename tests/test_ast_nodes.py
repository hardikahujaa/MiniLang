"""Tests for :mod:`app.ast_nodes`.

The AST is the contract between the parser and every phase after it, and
``to_dict`` is the contract between the backend and the D3 renderer. Both are
pinned here: a node that forgets its position, or emits a shape the tree view
cannot draw, breaks a downstream phase rather than failing locally.
"""

from __future__ import annotations

import json
import re
from dataclasses import FrozenInstanceError

import pytest

from app.ast_nodes import (
    AssignStmt,
    ASTNode,
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

#: Every concrete node, with a factory that builds a minimal valid instance.
NODE_FACTORIES = {
    "Literal": lambda: Literal(value=1, literal_type="int", raw="1", line=1, column=1),
    "Identifier": lambda: Identifier(name="x", line=1, column=1),
    "BinaryOp": lambda: BinaryOp(
        operator="+",
        left=Identifier(name="a", line=1, column=1),
        right=Identifier(name="b", line=1, column=5),
        line=1,
        column=3,
    ),
    "UnaryOp": lambda: UnaryOp(
        operator="-", operand=Identifier(name="a", line=1, column=2), line=1, column=1
    ),
    "CallExpr": lambda: CallExpr(callee="f", arguments=[], line=1, column=1),
    "Block": lambda: Block(statements=[], line=1, column=1),
    "VarDecl": lambda: VarDecl(var_type="int", name="x", initializer=None, line=1, column=1),
    "AssignStmt": lambda: AssignStmt(
        target="x", value=Identifier(name="y", line=1, column=5), line=1, column=1
    ),
    "IfStmt": lambda: IfStmt(
        condition=Identifier(name="c", line=1, column=4),
        then_branch=Block(statements=[], line=1, column=7),
        else_branch=None,
        line=1,
        column=1,
    ),
    "WhileStmt": lambda: WhileStmt(
        condition=Identifier(name="c", line=1, column=7),
        body=Block(statements=[], line=1, column=10),
        line=1,
        column=1,
    ),
    "ReturnStmt": lambda: ReturnStmt(value=None, line=1, column=1),
    "PrintStmt": lambda: PrintStmt(value=Identifier(name="x", line=1, column=7), line=1, column=1),
    "ExprStmt": lambda: ExprStmt(
        expression=Identifier(name="x", line=1, column=1), line=1, column=1
    ),
    "Param": lambda: Param(param_type="int", name="n", line=1, column=1),
    "FuncDecl": lambda: FuncDecl(
        name="f",
        params=[],
        return_type="void",
        body=Block(statements=[], line=1, column=20),
        line=1,
        column=1,
    ),
    "Program": lambda: Program(functions=[], line=1, column=1),
}


def _roles(payload: dict) -> list[str]:
    """Return the branch label of each child, in source order.

    Args:
        payload: A serialised node.

    Returns:
        Every child's ``edge`` -- the role it fills in its parent, which is what
        names the branch in the drawn tree.
    """
    return [child["edge"] for child in payload["children"]]


class TestBaseClasses:
    """The abstract bases must stay abstract."""

    @pytest.mark.parametrize("base", [ASTNode, Expr, Stmt])
    def test_base_classes_cannot_be_instantiated(self, base: type):
        """A base with an unimplemented to_dict must not be constructible."""
        with pytest.raises(TypeError):
            base(line=1, column=1)

    def test_expressions_derive_from_expr(self):
        for name in ("Literal", "Identifier", "BinaryOp", "UnaryOp", "CallExpr"):
            assert isinstance(NODE_FACTORIES[name](), Expr)

    def test_statements_derive_from_stmt(self):
        for name in (
            "Block",
            "VarDecl",
            "AssignStmt",
            "IfStmt",
            "WhileStmt",
            "ReturnStmt",
            "PrintStmt",
            "ExprStmt",
        ):
            assert isinstance(NODE_FACTORIES[name](), Stmt)

    def test_every_node_is_an_astnode(self):
        for factory in NODE_FACTORIES.values():
            assert isinstance(factory(), ASTNode)


class TestPositions:
    """Position is required on every node: diagnostics depend on it."""

    @pytest.mark.parametrize("name", sorted(NODE_FACTORIES))
    def test_every_node_carries_a_position(self, name: str):
        node = NODE_FACTORIES[name]()
        assert node.line >= 1
        assert node.column >= 1

    def test_position_is_keyword_only(self):
        """Keeps constructors readable: BinaryOp('+', l, r, line=.., column=..)."""
        with pytest.raises(TypeError):
            Identifier("x", 1, 1)

    def test_position_is_required(self):
        with pytest.raises(TypeError):
            Identifier(name="x")

    @pytest.mark.parametrize("name", sorted(NODE_FACTORIES))
    def test_position_survives_serialisation(self, name: str):
        payload = NODE_FACTORIES[name]().to_dict()
        assert payload["line"] >= 1
        assert payload["column"] >= 1


class TestSerialisationEnvelope:
    """Every node emits the same envelope, so the renderer needs no special cases."""

    @pytest.mark.parametrize("name", sorted(NODE_FACTORIES))
    def test_envelope_keys_are_present(self, name: str):
        payload = NODE_FACTORIES[name]().to_dict()
        for key in ("node", "name", "kind", "line", "column", "children"):
            assert key in payload, f"{name}.to_dict() is missing '{key}'"

    @pytest.mark.parametrize("name", sorted(NODE_FACTORIES))
    def test_node_field_names_the_class(self, name: str):
        assert NODE_FACTORIES[name]().to_dict()["node"] == name

    @pytest.mark.parametrize("name", sorted(NODE_FACTORIES))
    def test_children_is_always_a_list(self, name: str):
        """D3's hierarchy layout recurses into children unconditionally."""
        assert isinstance(NODE_FACTORIES[name]().to_dict()["children"], list)

    @pytest.mark.parametrize("name", sorted(NODE_FACTORIES))
    def test_name_is_a_non_empty_label(self, name: str):
        label = NODE_FACTORIES[name]().to_dict()["name"]
        assert isinstance(label, str)
        assert label.strip()

    @pytest.mark.parametrize("name", sorted(NODE_FACTORIES))
    def test_serialises_to_json(self, name: str):
        """The payload crosses the wire, so it must be JSON-encodable."""
        json.dumps(NODE_FACTORIES[name]().to_dict())


class TestOptionalChildren:
    """A missing optional child must not leave a null in the children list."""

    def test_vardecl_without_initialiser_still_has_type_and_name(self):
        """`int x;` is three facts, not one: a type, a name, and no value."""
        node = VarDecl(var_type="int", name="x", initializer=None, line=1, column=1)
        assert _roles(node.to_dict()) == ["type", "name"]

    def test_vardecl_with_initialiser_adds_an_init_branch(self):
        node = VarDecl(
            var_type="int",
            name="x",
            initializer=Literal(value=5, literal_type="int", raw="5", line=1, column=9),
            line=1,
            column=1,
        )
        assert _roles(node.to_dict()) == ["type", "name", "init"]

    def test_return_without_value(self):
        payload = ReturnStmt(value=None, line=1, column=1).to_dict()
        assert payload["children"] == []
        assert payload["hasValue"] is False

    def test_if_without_else_has_two_children(self):
        node = NODE_FACTORIES["IfStmt"]()
        payload = node.to_dict()
        assert _roles(payload) == ["cond", "then"]
        assert payload["hasElse"] is False

    def test_if_with_else_has_three_children(self):
        node = IfStmt(
            condition=Identifier(name="c", line=1, column=4),
            then_branch=Block(statements=[], line=1, column=7),
            else_branch=Block(statements=[], line=3, column=7),
            line=1,
            column=1,
        )
        payload = node.to_dict()
        assert _roles(payload) == ["cond", "then", "else"]
        assert payload["hasElse"] is True

    def test_no_child_is_ever_null(self):
        """A null child would crash D3's hierarchy walk."""
        for factory in NODE_FACTORIES.values():
            assert all(child is not None for child in factory().to_dict()["children"])


class TestNodeSpecificFields:
    """Fields each node contributes beyond the shared envelope."""

    def test_literal_keeps_raw_source_text(self):
        """'007' must render as written, not as the integer 7."""
        node = Literal(value=7, literal_type="int", raw="007", line=1, column=1)
        payload = node.to_dict()
        assert payload["name"] == "Literal (007)"
        assert payload["detail"] == "007"
        assert payload["value"] == 7

    @pytest.mark.parametrize(
        ("value", "type_name", "raw"),
        [(42, "int", "42"), (3.5, "float", "3.5"), (True, "bool", "true")],
    )
    def test_literal_types(self, value: object, type_name: str, raw: str):
        node = Literal(value=value, literal_type=type_name, raw=raw, line=1, column=1)
        assert node.to_dict()["literalType"] == type_name

    def test_binary_op_labels_with_its_operator(self):
        payload = NODE_FACTORIES["BinaryOp"]().to_dict()
        assert payload["name"] == "BinaryOp (+)"
        assert payload["detail"] == "+"

    def test_binary_operands_are_labelled_left_and_right(self):
        """`a - b` and `b - a` share a node set and differ only by these roles."""
        assert _roles(NODE_FACTORIES["BinaryOp"]().to_dict()) == ["left", "right"]

    def test_unary_op_is_distinguishable_from_binary(self):
        """'-' as prefix and '-' as subtraction must not look identical."""
        unary = UnaryOp(
            operator="-", operand=Identifier(name="a", line=1, column=2), line=1, column=1
        ).to_dict()
        binary = BinaryOp(
            operator="-",
            left=Identifier(name="a", line=1, column=1),
            right=Identifier(name="b", line=1, column=5),
            line=1,
            column=3,
        ).to_dict()

        # Same operator spelling; the class name is what tells them apart.
        assert unary["detail"] == binary["detail"] == "-"
        assert unary["name"] == "UnaryOp (-)"
        assert binary["name"] == "BinaryOp (-)"
        assert _roles(unary) == ["operand"]

    def test_call_records_arity(self):
        node = CallExpr(
            callee="fib",
            arguments=[Identifier(name="n", line=1, column=5)],
            line=1,
            column=1,
        )
        payload = node.to_dict()
        assert payload["callee"] == "fib"
        assert payload["argumentCount"] == 1
        assert _roles(payload) == ["callee", "args"]

        callee, args = payload["children"]
        assert (callee["node"], callee["detail"]) == ("Identifier", "fib")
        assert args["node"] == "ArgList"
        assert _roles(args) == ["arg 0"]

    def test_funcdecl_expands_into_four_named_branches(self):
        node = FuncDecl(
            name="add",
            params=[
                Param(param_type="int", name="a", line=1, column=10),
                Param(param_type="int", name="b", line=1, column=17),
            ],
            return_type="int",
            body=Block(statements=[], line=1, column=30),
            line=1,
            column=1,
        )
        payload = node.to_dict()
        assert payload["name"] == "FuncDecl (add)"
        assert payload["arity"] == 2
        assert _roles(payload) == ["name", "params", "returns", "body"]

        # The flattened signature survives as tooltip data only -- never as the
        # drawn label, which is the whole point of this shape.
        assert payload["signature"] == "func add(int a, int b): int"

        name, params, returns, body = payload["children"]
        assert (name["node"], name["detail"]) == ("Identifier", "add")
        assert params["node"] == "ParamList"
        assert _roles(params) == ["param 0", "param 1"]
        assert _roles(params["children"][0]) == ["type", "name"]
        assert (returns["node"], returns["detail"]) == ("Type", "int")
        assert body["node"] == "Block"

    def test_program_counts_its_functions(self):
        program = Program(
            functions=[NODE_FACTORIES["FuncDecl"]()],
            line=1,
            column=1,
        )
        assert program.to_dict()["functionCount"] == 1


class TestMutability:
    """Nodes are mutable so the semantic analyser can annotate them in place."""

    def test_inferred_type_starts_unset(self):
        assert Identifier(name="x", line=1, column=1).inferred_type is None

    def test_inferred_type_can_be_assigned(self):
        node = Identifier(name="x", line=1, column=1)
        node.inferred_type = "int"
        assert node.to_dict()["inferredType"] == "int"

    def test_nodes_are_not_frozen(self):
        """Phase 3 annotates the tree in place rather than rebuilding it."""
        node = Identifier(name="x", line=1, column=1)
        try:
            node.name = "y"
        except FrozenInstanceError:  # pragma: no cover - would be a design change
            pytest.fail("AST nodes must stay mutable for semantic annotation")
        assert node.name == "y"


class TestNestedTrees:
    """Serialisation must recurse to arbitrary depth."""

    def test_deeply_nested_expression(self):
        expr: Expr = Literal(value=1, literal_type="int", raw="1", line=1, column=1)
        for _ in range(50):
            expr = UnaryOp(operator="-", operand=expr, line=1, column=1)

        payload = expr.to_dict()
        depth = 0
        while payload["children"]:
            payload = payload["children"][0]
            depth += 1
        assert depth == 50

    def test_full_subtree_round_trips_through_json(self):
        program = Program(
            functions=[
                FuncDecl(
                    name="main",
                    params=[],
                    return_type="void",
                    body=Block(
                        statements=[
                            VarDecl(
                                var_type="int",
                                name="x",
                                initializer=BinaryOp(
                                    operator="*",
                                    left=Literal(
                                        value=2, literal_type="int", raw="2", line=2, column=13
                                    ),
                                    right=Literal(
                                        value=3, literal_type="int", raw="3", line=2, column=17
                                    ),
                                    line=2,
                                    column=15,
                                ),
                                line=2,
                                column=5,
                            )
                        ],
                        line=1,
                        column=20,
                    ),
                    line=1,
                    column=1,
                )
            ],
            line=1,
            column=1,
        )
        restored = json.loads(json.dumps(program.to_dict()))
        func = restored["children"][0]
        body = func["children"][_roles(func).index("body")]
        var_decl = body["children"][0]
        assert var_decl["node"] == "VarDecl"

        init = var_decl["children"][_roles(var_decl).index("init")]
        assert init["name"] == "BinaryOp (*)"


def _walk(payload: dict) -> list[dict]:
    """Collect every node in a serialised tree.

    Args:
        payload: The serialised root.

    Returns:
        Every node in the subtree, root included, in no particular order.
    """
    found: list[dict] = []
    stack = [payload]
    while stack:
        node = stack.pop()
        found.append(node)
        stack.extend(node["children"])
    return found


class TestNoFlattening:
    """Structure must be nodes and edges, never text packed into a label.

    This is the contract the Syntax tab is graded on. A label like
    ``func fib(int n): int`` hides four distinct syntactic children behind one
    caption, which is exactly what an abstract syntax tree is supposed to make
    visible, so it is pinned here rather than left to review.
    """

    #: A legal label: a class name, then at most one parenthesised datum.
    LABEL = re.compile(r"^[A-Za-z]\w*(?: \(.*\))?$")

    @pytest.mark.parametrize("name", sorted(NODE_FACTORIES))
    def test_label_is_a_class_name_plus_at_most_one_datum(self, name: str):
        payload = NODE_FACTORIES[name]().to_dict()
        assert self.LABEL.match(payload["name"]), payload["name"]
        assert payload["name"].split(" (")[0] == payload["node"]

    @pytest.mark.parametrize("name", sorted(NODE_FACTORIES))
    def test_every_child_declares_the_role_it_fills(self, name: str):
        """An unlabelled edge makes the drawing ambiguous."""
        for child in NODE_FACTORIES[name]().to_dict()["children"]:
            assert child["edge"], f"{name} -> {child['node']} has no edge role"

    def test_the_root_fills_no_role(self):
        assert NODE_FACTORIES["Program"]().to_dict()["edge"] is None

    def test_no_label_in_a_real_program_is_flattened(self):
        """The regression this whole shape exists to prevent."""
        from app.lexer import tokenize
        from app.parser import parse

        source = "func fib(int n): int { int a = 1 + n * 2; return fib(a); }"
        labels = [node["name"] for node in _walk(parse(tokenize(source).tokens).to_dict())]

        assert labels, "the walk found nothing to check"
        for label in labels:
            assert self.LABEL.match(label), f"flattened label: {label!r}"
        assert not any(label.startswith("func ") for label in labels)

    def test_names_and_types_become_nodes_of_their_own(self):
        """Identifiers and type annotations are structure, not decoration."""
        from app.lexer import tokenize
        from app.parser import parse

        source = "func m(int n): void { int x = 1; x = n; print(g(x, n)); }"
        classes = {node["node"] for node in _walk(parse(tokenize(source).tokens).to_dict())}

        assert {"Identifier", "Type", "ParamList", "ArgList"} <= classes

    def test_precedence_reads_as_depth(self):
        """`1 + n * 2` must put the tighter-binding operator underneath."""
        from app.lexer import tokenize
        from app.parser import parse

        payload = parse(tokenize("func m(): void { int a = 1 + n * 2; }").tokens).to_dict()
        func = payload["children"][0]
        body = func["children"][_roles(func).index("body")]
        var_decl = body["children"][0]
        init = var_decl["children"][_roles(var_decl).index("init")]

        assert init["name"] == "BinaryOp (+)"
        right = init["children"][_roles(init).index("right")]
        assert right["name"] == "BinaryOp (*)", "multiplication must sit below addition"


class TestPositionIntegrity:
    """Every drawn node must point at the source text it actually stands for.

    A declaration stores its name as a ``str``, so the ``Identifier`` node the
    tree draws has no position of its own -- it has to be recorded by the
    parser. Inheriting the owning statement's position instead looks harmless
    and is not: the semantic analyser reports undeclared variables and type
    errors against these nodes, and the editor jumps to the position they
    carry. Pointing at the start of the statement sends the cursor to the type
    keyword rather than the offending name.
    """

    SOURCE = (
        "func total(int count, bool flag): int {\n"
        "    int sum = count + 1;\n"
        "    bool seen = flag;\n"
        "    sum = sum * 2;\n"
        "    return sum;\n"
        "}"
    )

    def _tree(self) -> dict:
        from app.lexer import tokenize
        from app.parser import parse

        return parse(tokenize(self.SOURCE).tokens).to_dict()

    def test_every_leaf_points_at_its_own_text(self):
        lines = self.SOURCE.splitlines()
        checked = 0
        for node in _walk(self._tree()):
            detail = node.get("detail")
            if node["node"] not in ("Identifier", "Type", "Literal"):
                continue
            if not isinstance(detail, str):
                continue
            checked += 1
            text = lines[node["line"] - 1]
            found = text[node["column"] - 1 : node["column"] - 1 + len(detail)]
            assert found == detail, (
                f"{node['node']} ({detail}) claims "
                f"{node['line']}:{node['column']}, which holds {found!r}"
            )
        assert checked >= 12, "the walk did not reach the interesting nodes"

    def test_declared_name_does_not_inherit_the_statement_position(self):
        """`int sum = ...` -- the name is at column 9, the statement at 5."""
        tree = self._tree()
        decls = [n for n in _walk(tree) if n["node"] == "VarDecl" and n["identifier"] == "sum"]
        assert len(decls) == 1

        decl = decls[0]
        roles = {child["edge"]: child for child in decl["children"]}
        assert (decl["line"], decl["column"]) == (2, 5)
        assert (roles["type"]["line"], roles["type"]["column"]) == (2, 5)
        assert (roles["name"]["line"], roles["name"]["column"]) == (2, 9)

    def test_function_name_and_return_type_carry_their_own_positions(self):
        """FuncDecl sits on `func`; its name and return type do not."""
        func = self._tree()["children"][0]
        roles = {child["edge"]: child for child in func["children"]}
        assert (func["line"], func["column"]) == (1, 1)
        assert (roles["name"]["line"], roles["name"]["column"]) == (1, 6)
        assert roles["returns"]["column"] > func["column"]

    def test_parameter_names_carry_their_own_positions(self):
        func = self._tree()["children"][0]
        params = {c["edge"]: c for c in func["children"]}["params"]
        for param in params["children"]:
            roles = {child["edge"]: child for child in param["children"]}
            assert roles["name"]["column"] > roles["type"]["column"]
