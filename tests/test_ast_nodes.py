"""Tests for :mod:`app.ast_nodes`.

The AST is the contract between the parser and every phase after it, and
``to_dict`` is the contract between the backend and the D3 renderer. Both are
pinned here: a node that forgets its position, or emits a shape the tree view
cannot draw, breaks a downstream phase rather than failing locally.
"""

from __future__ import annotations

import json
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

    def test_vardecl_without_initialiser_has_no_children(self):
        node = VarDecl(var_type="int", name="x", initializer=None, line=1, column=1)
        assert node.to_dict()["children"] == []

    def test_vardecl_with_initialiser_has_one_child(self):
        node = VarDecl(
            var_type="int",
            name="x",
            initializer=Literal(value=5, literal_type="int", raw="5", line=1, column=9),
            line=1,
            column=1,
        )
        assert len(node.to_dict()["children"]) == 1

    def test_return_without_value(self):
        payload = ReturnStmt(value=None, line=1, column=1).to_dict()
        assert payload["children"] == []
        assert payload["hasValue"] is False

    def test_if_without_else_has_two_children(self):
        node = NODE_FACTORIES["IfStmt"]()
        payload = node.to_dict()
        assert len(payload["children"]) == 2
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
        assert len(payload["children"]) == 3
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
        assert payload["name"] == "007"
        assert payload["value"] == 7

    @pytest.mark.parametrize(
        ("value", "type_name", "raw"),
        [(42, "int", "42"), (3.5, "float", "3.5"), (True, "bool", "true")],
    )
    def test_literal_types(self, value: object, type_name: str, raw: str):
        node = Literal(value=value, literal_type=type_name, raw=raw, line=1, column=1)
        assert node.to_dict()["literalType"] == type_name

    def test_binary_op_labels_with_its_operator(self):
        assert NODE_FACTORIES["BinaryOp"]().to_dict()["name"] == "+"

    def test_unary_op_is_distinguishable_from_binary(self):
        """'-' as prefix and '-' as subtraction must not look identical."""
        assert NODE_FACTORIES["UnaryOp"]().to_dict()["name"] == "unary -"

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
        assert len(payload["children"]) == 1

    def test_funcdecl_label_shows_the_signature(self):
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
        assert payload["name"] == "func add(int a, int b): int"
        assert payload["arity"] == 2
        # Two params plus the body.
        assert len(payload["children"]) == 3

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
        var_decl = restored["children"][0]["children"][0]["children"][0]
        assert var_decl["node"] == "VarDecl"
        assert var_decl["children"][0]["name"] == "*"
