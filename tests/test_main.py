"""Tests for the HTTP surface in :mod:`app.main`.

At scaffold stage the compiler phases do not exist yet, so what is under test
here is the *contract*: that every route exists, that it validates its input,
that it rejects malformed input rather than accepting it silently, and that
``/compile`` returns the exact JSON shape from section 3 of the plan.

These tests are written to survive the phases being implemented: they assert on
the schema and on invariants, not on artefacts being empty.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import __version__

# Every top-level key the plan's section 3 schema requires. Guarding this as a
# set means a future refactor cannot silently drop a field the frontend reads.
EXPECTED_COMPILE_KEYS = {
    "tokens",
    "dfa",
    "parseTrace",
    "parseTree",
    "ast",
    "symbols",
    "typeErrors",
    "tac",
    "blocks",
    "cfg",
    "optimized",
    "asm",
    "output",
    "errors",
    "meta",
}


class TestMetaRoutes:
    """The frontend and metadata endpoints."""

    def test_index_serves_html(self, client: TestClient):
        response = client.get("/")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert "MiniLang" in response.text

    def test_index_contains_all_eight_tabs(self, client: TestClient):
        """The eight-tab UI from plan section 4 must all be present."""
        body = client.get("/").text
        for tab in (
            "source",
            "lexical",
            "theory",
            "syntax",
            "semantic",
            "icg",
            "optimization",
            "target",
        ):
            assert f'data-tab="{tab}"' in body, f"missing tab shell: {tab}"
            assert f'id="panel-{tab}"' in body, f"missing panel: {tab}"

    def test_health(self, client: TestClient):
        response = client.get("/api/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["version"] == __version__
        assert isinstance(body["phasesImplemented"], list)

    def test_demo_returns_canonical_program(self, client: TestClient, demo_source: str):
        response = client.get("/api/demo")
        assert response.status_code == 200
        body = response.json()
        assert body["source"] == demo_source
        assert "grammar" in body
        assert "ambiguousGrammar" in body

    def test_static_assets_are_served(self, client: TestClient):
        """The vendored D3 bundle must be served locally, not from a CDN."""
        for path in ("/static/style.css", "/static/app.js", "/static/viz.js"):
            assert client.get(path).status_code == 200, f"missing asset: {path}"

        d3 = client.get("/static/lib/d3.v7.min.js")
        assert d3.status_code == 200
        assert "d3js.org" in d3.text[:200]

    def test_index_does_not_reference_a_cdn(self, client: TestClient):
        """Offline demo requirement: no external script or style sources."""
        body = client.get("/").text
        for host in ("cdn.jsdelivr.net", "unpkg.com", "cdnjs.cloudflare.com", "//d3js.org"):
            assert host not in body, f"index.html must not load from {host}"


class TestCompileEndpoint:
    """``POST /compile`` -- the pipeline's single entry point."""

    def test_returns_full_schema(self, client: TestClient, demo_source: str):
        response = client.post("/compile", json={"source": demo_source})
        assert response.status_code == 200
        assert set(response.json().keys()) == EXPECTED_COMPILE_KEYS

    def test_reports_source_statistics(self, client: TestClient, demo_source: str):
        """The stub must still echo real measurements, proving the round trip."""
        response = client.post("/compile", json={"source": demo_source})
        meta = response.json()["meta"]
        assert meta["sourceLines"] == len(demo_source.splitlines())
        assert meta["sourceBytes"] == len(demo_source.encode("utf-8"))
        assert meta["ok"] is True
        assert meta["version"] == __version__

    def test_records_timing(self, client: TestClient):
        response = client.post("/compile", json={"source": "func main(): void {}"})
        timings = response.json()["meta"]["timings"]
        assert any(t["phase"] == "total" for t in timings)
        assert all(t["ms"] >= 0 for t in timings)

    def test_empty_source_is_accepted(self, client: TestClient):
        """An empty buffer is a legitimate editor state, not a client error."""
        response = client.post("/compile", json={"source": ""})
        assert response.status_code == 200
        meta = response.json()["meta"]
        assert meta["sourceLines"] == 0
        assert meta["sourceBytes"] == 0

    def test_optimization_toggles_accepted(self, client: TestClient):
        response = client.post(
            "/compile",
            json={
                "source": "func main(): void {}",
                "optimizations": {
                    "constantFolding": False,
                    "constantPropagation": False,
                    "deadCodeElimination": True,
                    "commonSubexpressionElimination": True,
                },
            },
        )
        assert response.status_code == 200

    def test_optimization_toggles_default_when_omitted(self, client: TestClient):
        """Omitting the toggles must not 422; they carry defaults."""
        assert client.post("/compile", json={"source": "x"}).status_code == 200

    @pytest.mark.parametrize(
        ("payload", "reason"),
        [
            ({}, "source is required"),
            ({"source": 123}, "source must be a string"),
            ({"source": None}, "source may not be null"),
            ({"src": "func main(): void {}"}, "misspelled field name"),
            ({"source": "x", "unknownField": True}, "extra fields are forbidden"),
            ({"source": "x", "optimizations": "all"}, "toggles must be an object"),
            ({"source": "x", "optimizations": {"constantFolding": "yes"}}, "toggle must be bool"),
        ],
    )
    def test_malformed_requests_are_rejected(self, client: TestClient, payload: dict, reason: str):
        """Deliberately malformed input must fail validation, not pass through."""
        response = client.post("/compile", json=payload)
        assert response.status_code == 422, f"should have rejected: {reason}"

    def test_invalid_json_body_is_rejected(self, client: TestClient):
        response = client.post(
            "/compile",
            content=b"{not valid json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422

    def test_unicode_source_round_trips(self, client: TestClient):
        """Byte count must be UTF-8 bytes, not character count."""
        source = "// ééé 中文\nfunc main(): void {}"
        response = client.post("/compile", json={"source": source})
        assert response.status_code == 200
        assert response.json()["meta"]["sourceBytes"] == len(source.encode("utf-8"))

    def test_crlf_line_endings_counted_correctly(self, client: TestClient):
        """Windows-authored files must not report doubled line counts."""
        response = client.post("/compile", json={"source": "a\r\nb\r\nc"})
        assert response.json()["meta"]["sourceLines"] == 3

    def test_large_source_is_accepted(self, client: TestClient):
        source = "\n".join(f"int v{i} = {i};" for i in range(5000))
        response = client.post("/compile", json={"source": source})
        assert response.status_code == 200
        assert response.json()["meta"]["sourceLines"] == 5000


class TestRunEndpoint:
    """``POST /run`` -- stack-VM execution."""

    def test_accepts_empty_program(self, client: TestClient):
        response = client.post("/run", json={"asm": []})
        assert response.status_code == 200
        body = response.json()
        assert body["output"] == ""
        assert body["steps"] == 0
        assert body["halted"] is True

    def test_accepts_source_instead_of_asm(self, client: TestClient):
        response = client.post("/run", json={"source": "func main(): void {}"})
        assert response.status_code == 200

    @pytest.mark.parametrize(
        ("payload", "reason"),
        [
            ({"maxSteps": 0}, "step budget must be positive"),
            ({"maxSteps": -5}, "step budget must be positive"),
            ({"asm": [{"op": "PUSH"}]}, "asm entries need index and text"),
            ({"asm": "PUSH 1"}, "asm must be a list"),
        ],
    )
    def test_malformed_run_requests_are_rejected(
        self, client: TestClient, payload: dict, reason: str
    ):
        assert client.post("/run", json=payload).status_code == 422, reason

    def test_wellformed_asm_entry_is_accepted(self, client: TestClient):
        response = client.post(
            "/run",
            json={"asm": [{"index": 0, "op": "PUSH", "operand": "1", "text": "PUSH 1"}]},
        )
        assert response.status_code == 200


class TestGrammarEndpoint:
    """``POST /analyze-grammar`` -- the Parser Theory Lab."""

    def test_accepts_a_grammar(self, client: TestClient):
        response = client.post(
            "/analyze-grammar",
            json={"grammar": "E -> E + T | T\nT -> id", "startSymbol": "E"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["startSymbol"] == "E"
        for key in ("first", "follow", "ll1Table", "slrTable", "lalrTable", "conflicts"):
            assert key in body, f"missing analysis field: {key}"

    def test_start_symbol_is_optional(self, client: TestClient):
        response = client.post("/analyze-grammar", json={"grammar": "S -> a"})
        assert response.status_code == 200
        assert response.json()["startSymbol"] is None

    @pytest.mark.parametrize(
        ("payload", "reason"),
        [
            ({}, "grammar is required"),
            ({"grammar": None}, "grammar may not be null"),
            ({"grammar": ["S -> a"]}, "grammar must be a string"),
            ({"grammar": "S -> a", "bogus": 1}, "extra fields are forbidden"),
        ],
    )
    def test_malformed_grammar_requests_are_rejected(
        self, client: TestClient, payload: dict, reason: str
    ):
        assert client.post("/analyze-grammar", json=payload).status_code == 422, reason


class TestSyntaxAnalysis:
    """Phase 2 as seen through the API."""

    def test_valid_source_returns_an_ast(self, client: TestClient, demo_source: str):
        body = client.post("/compile", json={"source": demo_source}).json()
        assert body["ast"] is not None
        assert body["ast"]["node"] == "Program"
        assert body["ast"]["functionCount"] == 2
        assert body["meta"]["reachedPhase"] == "parser"

    def test_ast_is_shaped_for_the_tree_renderer(self, client: TestClient, demo_source: str):
        """D3's hierarchy layout reads `name` and `children` by default."""
        ast = client.post("/compile", json={"source": demo_source}).json()["ast"]
        assert isinstance(ast["name"], str)
        assert isinstance(ast["children"], list)
        assert ast["children"], "the demo program has functions to draw"

    def test_syntax_error_is_reported_and_ast_is_null(self, client: TestClient):
        body = client.post("/compile", json={"source": "func m(): void { int x = 5 }"}).json()
        assert body["ast"] is None
        assert len(body["errors"]) == 1
        assert "';'" in body["errors"][0]["message"]
        assert body["meta"]["ok"] is False
        assert body["meta"]["reachedPhase"] == "lexer"

    def test_syntax_error_carries_position_and_expected_set(self, client: TestClient):
        source = "func m(): void {\n    int x = 5\n}"
        error = client.post("/compile", json={"source": source}).json()["errors"][0]
        assert error["line"] == 3
        assert error["col"] >= 1
        assert error["expected"] == [";"]
        assert error["recovered"] is False

    def test_tokens_are_still_returned_when_parsing_fails(self, client: TestClient):
        """A failed parse must not blank the Lexical tab."""
        body = client.post("/compile", json={"source": "func m(): void { int x = 5 }"}).json()
        assert len(body["tokens"]) > 5

    def test_parsing_runs_even_after_a_lexical_error(self, client: TestClient):
        """The lexer recovers, so the stream is still complete enough to parse."""
        body = client.post("/compile", json={"source": "func m(): void { if (a & b) { } }"}).json()
        assert len(body["errors"]) >= 1
        assert body["ast"] is not None

    def test_pathological_nesting_is_an_error_not_a_500(self, client: TestClient):
        """Unbounded recursive descent would raise RecursionError and 500 here."""
        depth = 400
        source = f"func m(): void {{ int x = {'(' * depth}1{')' * depth}; }}"
        response = client.post("/compile", json={"source": source})
        assert response.status_code == 200
        assert any("too deep" in e["message"] for e in response.json()["errors"])

    def test_timings_include_the_parser_phase(self, client: TestClient, demo_source: str):
        timings = client.post("/compile", json={"source": demo_source}).json()["meta"]["timings"]
        phases = [t["phase"] for t in timings]
        assert "lexer" in phases
        assert "parser" in phases

    def test_health_reports_both_phases(self, client: TestClient):
        assert client.get("/api/health").json()["phasesImplemented"] == ["lexer", "parser"]


class TestOpenApi:
    """The generated OpenAPI document, which is also the API's documentation."""

    def test_openapi_is_valid_json_and_lists_every_route(self, client: TestClient):
        response = client.get("/openapi.json")
        assert response.status_code == 200
        spec = json.loads(response.text)
        for path in ("/compile", "/run", "/analyze-grammar", "/api/health", "/api/demo"):
            assert path in spec["paths"], f"route missing from OpenAPI: {path}"
