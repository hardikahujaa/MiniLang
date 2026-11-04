"""Tests for the wire contract in :mod:`app.schemas`.

The frontend reads camelCase keys; the backend writes snake_case attributes.
These tests pin that translation down, because a silent rename on either side
breaks every tab at once and would be invisible until a demo.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas import (
    ApiModel,
    CompileRequest,
    CompileResponse,
    OptimizationToggles,
    Suggestion,
    SyntaxErrorReport,
    Token,
    TreeNode,
)


class TestNamingConvention:
    """snake_case in Python, camelCase on the wire."""

    def test_response_serialises_to_camel_case(self):
        payload = CompileResponse().model_dump(by_alias=True)
        assert "parseTrace" in payload
        assert "typeErrors" in payload
        assert "parseTree" in payload
        assert "parse_trace" not in payload

    def test_backend_can_construct_with_python_names(self):
        """populate_by_name lets backend code use the readable spelling."""
        response = CompileResponse(parse_trace=[], type_errors=[])
        assert response.parse_trace == []
        assert response.type_errors == []

    def test_request_accepts_camel_case_from_the_browser(self):
        request = CompileRequest.model_validate(
            {"source": "x", "explainErrors": True, "optimizations": {"constantFolding": False}}
        )
        assert request.explain_errors is True
        assert request.optimizations.constant_folding is False

    def test_every_api_model_shares_the_convention(self):
        """Guard against a new model forgetting to inherit ApiModel's config."""
        for model in (Token, TreeNode, Suggestion, SyntaxErrorReport, OptimizationToggles):
            assert issubclass(model, ApiModel)
            assert model.model_config["alias_generator"] is not None
            assert model.model_config["populate_by_name"] is True


class TestDefaults:
    """Every artefact field must have a safe empty default."""

    def test_empty_response_is_constructible(self):
        """An unimplemented phase renders as an empty tab, never as a crash."""
        response = CompileResponse()
        assert response.tokens == []
        assert response.parse_tree is None
        assert response.ast is None
        assert response.output == ""
        assert response.optimized.stats.instructions_before == 0

    def test_optimization_defaults_match_the_ui_checkboxes(self):
        toggles = OptimizationToggles()
        assert toggles.constant_folding is True
        assert toggles.constant_propagation is True
        assert toggles.dead_code_elimination is True
        # CSE is Tier C, so it ships off by default.
        assert toggles.common_subexpression_elimination is False


class TestValidation:
    """Malformed artefacts must be rejected at construction, not at render time."""

    def test_token_positions_are_one_based(self):
        with pytest.raises(ValidationError):
            Token(type="IDENT", lexeme="x", line=0, col=1)
        with pytest.raises(ValidationError):
            Token(type="IDENT", lexeme="x", line=1, col=0)

    @pytest.mark.parametrize("confidence", [-0.1, 1.1, 2.0])
    def test_confidence_is_bounded(self, confidence: float):
        with pytest.raises(ValidationError):
            Suggestion(text="insert ;", confidence=confidence)

    @pytest.mark.parametrize("confidence", [0.0, 0.5, 1.0])
    def test_confidence_accepts_the_closed_unit_interval(self, confidence: float):
        assert Suggestion(text="insert ;", confidence=confidence).confidence == confidence

    def test_unknown_fields_are_rejected(self):
        """extra='forbid' turns a frontend typo into a loud 422."""
        with pytest.raises(ValidationError):
            CompileRequest.model_validate({"source": "x", "optimisations": {}})

    @pytest.mark.parametrize("value", ["yes", "no", "on", "off", "true", 1, 0])
    def test_toggles_reject_coercible_non_booleans(self, value: object):
        """StrictBool: a pass must never be switched on by a stringly-typed bug.

        Pydantic's lax mode would happily read "yes" as True. For flags that
        decide whether an optimisation runs, silent coercion turns a frontend
        bug into wrong compiler output rather than an error.
        """
        with pytest.raises(ValidationError):
            OptimizationToggles.model_validate({"constantFolding": value})

    @pytest.mark.parametrize("value", [True, False])
    def test_toggles_accept_real_booleans(self, value: bool):
        toggles = OptimizationToggles.model_validate({"constantFolding": value})
        assert toggles.constant_folding is value

    def test_explain_errors_is_strict_too(self):
        with pytest.raises(ValidationError):
            CompileRequest.model_validate({"source": "x", "explainErrors": "true"})

    def test_symbol_kind_is_constrained(self):
        from app.schemas import Symbol

        with pytest.raises(ValidationError):
            Symbol(name="x", type="int", kind="macro")


class TestTreeNode:
    """The recursive tree shape D3 consumes."""

    def test_nests_several_levels_deep(self):
        leaf = TreeNode(name="id", kind="terminal", value="n")
        tree = TreeNode(name="expr", children=[TreeNode(name="term", children=[leaf])])
        assert tree.children[0].children[0].value == "n"

    def test_serialises_recursively(self):
        tree = TreeNode(name="root", children=[TreeNode(name="child")])
        payload = tree.model_dump(by_alias=True)
        assert payload["children"][0]["name"] == "child"

    def test_round_trips_through_json(self):
        original = TreeNode(name="E", children=[TreeNode(name="T", value="id", line=1, col=1)])
        restored = TreeNode.model_validate_json(original.model_dump_json(by_alias=True))
        assert restored == original

    def test_step_field_round_trips_and_defaults_to_none(self):
        bare = TreeNode(name="E")
        assert bare.step is None
        stamped = TreeNode(name="E", step=7)
        restored = TreeNode.model_validate_json(stamped.model_dump_json(by_alias=True))
        assert restored.step == 7

    def test_is_not_actually_arbitrarily_deep(self):
        """Regression guard for a real pydantic-core limitation.

        Despite ``children: list[TreeNode]`` looking like it should nest to
        any depth, pydantic-core's serializer refuses a self-referential
        model nested much past roughly 95-100 levels with "Circular
        reference detected (depth exceeded)" -- a hard-coded limit,
        confirmed (while building app/table_parser.py) to be entirely
        unaffected by ``sys.setrecursionlimit``. 80 levels is comfortably
        below that ceiling; this is what
        ``app.table_parser._MAX_STACK_DEPTH`` is calibrated against, and
        this test is what would catch it if a future pydantic upgrade moved
        the ceiling down far enough to make that calibration wrong.
        """
        node = TreeNode(name="leaf", kind="terminal")
        for i in range(80):
            node = TreeNode(name=f"n{i}", kind="nonterminal", children=[node])
        serialized = node.model_dump_json()
        assert serialized  # did not raise
