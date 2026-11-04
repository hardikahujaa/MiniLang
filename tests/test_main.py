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
import os
import re

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
    """``POST /analyze-grammar`` -- the Parser Theory Lab.

    These are deliberately thin: every number the Parser Theory Lab produces
    is already pinned against the textbook in ``tests/test_grammar.py``, so
    what matters here is only that this HTTP layer carries those numbers
    through correctly -- the right JSON shape, the right status code for a
    bad grammar, and the one genuinely new piece of logic this layer adds
    (picking LALR over SLR for the parse trace, merging three tables' worth
    of conflicts into one list tagged by table).
    """

    def test_accepts_a_grammar(self, client: TestClient):
        response = client.post(
            "/analyze-grammar",
            json={"grammar": "E -> E + T | T\nT -> id", "startSymbol": "E"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["startSymbol"] == "E"
        assert body["terminals"] == ["+", "id"]
        assert body["nonTerminals"] == ["E", "T"]
        assert body["productions"] == ["E -> E + T", "E -> T", "T -> id"]
        for key in ("first", "follow", "ll1Table", "slrTable", "lalrTable", "conflicts"):
            assert key in body, f"missing analysis field: {key}"

    def test_start_symbol_defaults_to_first_productions_left_hand_side(self, client: TestClient):
        # Not an echo of the request: when none is given, the response
        # reports the start symbol app.grammar actually resolved and
        # analysed against, which is what the tab needs to display.
        response = client.post("/analyze-grammar", json={"grammar": "S -> a"})
        assert response.status_code == 200
        assert response.json()["startSymbol"] == "S"

    def test_demo_grammar_is_slr_and_lalr_conflict_free(self, client: TestClient):
        from app.samples import DEMO_GRAMMAR

        body = client.post("/analyze-grammar", json={"grammar": DEMO_GRAMMAR}).json()
        assert body["isLl1"] is False  # left-recursive, so it does have LL(1) conflicts
        tables_with_conflicts = {c["table"] for c in body["conflicts"]}
        assert tables_with_conflicts == {"ll1"}  # SLR and LALR are both clean
        assert len(body["lr0Items"]) == 12  # Aho et al. Figure 4.31

    def test_ambiguous_grammar_reports_shift_reduce_conflicts(self, client: TestClient):
        from app.samples import AMBIGUOUS_GRAMMAR

        body = client.post("/analyze-grammar", json={"grammar": AMBIGUOUS_GRAMMAR}).json()
        lr_conflicts = [c for c in body["conflicts"] if c["table"] in ("slr", "lalr")]
        assert lr_conflicts
        assert all(c["kind"] == "shift-reduce" for c in lr_conflicts)

    def test_assignment_grammar_conflicts_only_under_slr_not_lalr(self, client: TestClient):
        # The book's own canonical example of a grammar that is LALR(1) but
        # not SLR(1) (section 4.7.3) -- the whole reason this project builds
        # both tables rather than just one.
        grammar = "S -> L = R | R\nL -> * R | id\nR -> L\n"
        body = client.post("/analyze-grammar", json={"grammar": grammar, "startSymbol": "S"}).json()
        tables_with_conflicts = {c["table"] for c in body["conflicts"]}
        assert "slr" in tables_with_conflicts
        assert "lalr" not in tables_with_conflicts

    def test_input_string_produces_a_parse_trace(self, client: TestClient):
        from app.samples import DEMO_GRAMMAR

        body = client.post(
            "/analyze-grammar",
            json={"grammar": DEMO_GRAMMAR, "inputString": "id + id"},
        ).json()
        trace = body["parseTrace"]
        assert trace, "expected a non-empty parse trace"
        assert trace[0]["step"] == 0
        assert trace[-1]["action"] == "accept"

    def test_no_input_string_means_no_trace(self, client: TestClient):
        from app.samples import DEMO_GRAMMAR

        body = client.post("/analyze-grammar", json={"grammar": DEMO_GRAMMAR}).json()
        assert body["parseTrace"] == []

    def test_unparsable_grammar_is_a_diagnostic_not_a_500(self, client: TestClient):
        response = client.post("/analyze-grammar", json={"grammar": "not a grammar at all"})
        assert response.status_code == 200
        body = response.json()
        assert body["errors"]
        assert body["errors"][0]["severity"] == "error"
        assert body["errors"][0]["phase"] == "grammar"
        # Nothing downstream was computed for ungradable input.
        assert body["productions"] == []
        assert body["conflicts"] == []

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
        assert body["meta"]["reachedPhase"] == "codegen"

    def test_ast_is_shaped_for_the_tree_renderer(self, client: TestClient, demo_source: str):
        """D3's hierarchy layout reads `children`; the box reads `node` and `detail`."""
        ast = client.post("/compile", json={"source": demo_source}).json()["ast"]
        assert isinstance(ast["name"], str)
        assert isinstance(ast["children"], list)
        assert ast["children"], "the demo program has functions to draw"
        for key in ("node", "detail", "kind", "edge", "line", "column"):
            assert key in ast, f"the renderer needs '{key}'"

    def test_ast_over_the_wire_is_not_flattened(self, client: TestClient, demo_source: str):
        """The whole point of the Syntax tab: structure as nodes, not captions."""
        ast = client.post("/compile", json={"source": demo_source}).json()["ast"]

        nodes = []
        stack = [ast]
        while stack:
            node = stack.pop()
            nodes.append(node)
            stack.extend(node["children"])

        classes = {node["node"] for node in nodes}
        assert {"Identifier", "Type", "ParamList", "ArgList"} <= classes

        # A signature belongs in the tooltip payload, never in a drawn label.
        assert not any(node["name"].startswith("func ") for node in nodes)
        assert any(node.get("signature", "").startswith("func ") for node in nodes)

        # Every child names the role it fills, so the drawing is unambiguous.
        for node in nodes:
            for child in node["children"]:
                assert child["edge"], f"{node['node']} -> {child['node']} has no role"

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

    def test_a_single_syntax_error_keeps_the_plain_baseline_a_report(self, client: TestClient):
        """One real mistake must not trigger the recovery-engine report shape."""
        body = client.post("/compile", json={"source": "func m(): void { int x = 5 }"}).json()
        assert len(body["errors"]) == 1
        assert body["errors"][0]["recovered"] is False

    def test_multiple_syntax_errors_all_come_back_ranked(self, client: TestClient):
        """More than one genuine mistake surfaces all of them, pre-ranked."""
        source = "func m(): void {\n    int x = 1\n    int y = 2\n}"
        body = client.post("/compile", json={"source": source}).json()
        assert len(body["errors"]) == 2
        for error in body["errors"]:
            assert error["recovered"] is True
            assert error["suggestion"] == "insert ';'"
            assert error["suggestions"]

    def test_timings_include_the_parser_phase(self, client: TestClient, demo_source: str):
        timings = client.post("/compile", json={"source": demo_source}).json()["meta"]["timings"]
        phases = [t["phase"] for t in timings]
        assert "lexer" in phases
        assert "parser" in phases

    def test_health_reports_every_wired_phase(self, client: TestClient):
        assert client.get("/api/health").json()["phasesImplemented"] == [
            "lexer",
            "parser",
            "semantic",
            "tac",
            "optimize",
            "codegen",
        ]


class TestAssetFingerprinting:
    """Cache-busting for the frontend assets.

    A stale browser cache presents as "my edit did nothing", which is unusually
    hard to diagnose: the served file is correct and only the browser's copy is
    old, so every check short of a hard refresh looks fine.
    """

    def test_index_has_no_unreplaced_placeholder(self, client: TestClient):
        assert "__ASSET_VERSION__" not in client.get("/").text

    def test_every_local_asset_is_fingerprinted(self, client: TestClient):
        body = client.get("/").text
        for asset in ("style.css", "app.js", "editor.js", "highlight.js", "viz.js"):
            assert re.search(
                rf"/static/{re.escape(asset)}\?v=[0-9a-f]+", body
            ), f"{asset} is not cache-busted"

    def test_vendored_d3_is_fingerprinted_too(self, client: TestClient):
        assert re.search(r"/static/lib/d3\.v7\.min\.js\?v=[0-9a-f]+", client.get("/").text)

    def test_fingerprint_is_stable_while_files_are_unchanged(self, client: TestClient):
        """Otherwise every page load would refetch every asset."""
        first = re.findall(r"app\.js\?v=([0-9a-f]+)", client.get("/").text)[0]
        second = re.findall(r"app\.js\?v=([0-9a-f]+)", client.get("/").text)[0]
        assert first == second

    def test_fingerprint_changes_when_an_asset_changes(self):
        """The whole point: an edit must invalidate the browser's copy."""
        from app import main

        before = main.asset_version()
        target = main.STATIC_DIR / "app.js"
        original = target.stat()
        os.utime(target, ns=(original.st_atime_ns, original.st_mtime_ns + 1_000_000_000))
        try:
            assert main.asset_version() != before
        finally:
            os.utime(target, ns=(original.st_atime_ns, original.st_mtime_ns))
        assert main.asset_version() == before

    def test_fingerprinted_asset_is_actually_served(self, client: TestClient):
        """The query string must not stop StaticFiles from finding the file."""
        assert client.get("/static/app.js?v=deadbeef").status_code == 200

    def test_index_is_served_as_html(self, client: TestClient):
        response = client.get("/")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")


class TestOpenApi:
    """The generated OpenAPI document, which is also the API's documentation."""

    def test_openapi_is_valid_json_and_lists_every_route(self, client: TestClient):
        response = client.get("/openapi.json")
        assert response.status_code == 200
        spec = json.loads(response.text)
        for path in ("/compile", "/run", "/analyze-grammar", "/api/health", "/api/demo"):
            assert path in spec["paths"], f"route missing from OpenAPI: {path}"


class TestSemanticAnalysis:
    """Phase 3 as seen through the API."""

    def test_demo_program_is_semantically_clean(self, client: TestClient, demo_source: str):
        """The shipped demo must never open with errors on the Problems panel."""
        body = client.post("/compile", json={"source": demo_source}).json()
        assert body["typeErrors"] == []
        assert body["meta"]["ok"] is True
        assert body["meta"]["reachedPhase"] == "codegen"

    def test_symbol_table_is_returned(self, client: TestClient, demo_source: str):
        body = client.post("/compile", json={"source": demo_source}).json()
        symbols = body["symbols"]
        assert symbols, "the demo declares functions and variables"

        by_name = {s["name"]: s for s in symbols}
        assert by_name["fib"]["kind"] == "function"
        assert by_name["fib"]["type"] == "(int) -> int"
        assert by_name["fib"]["depth"] == 0
        assert by_name["n"]["kind"] == "parameter"
        assert by_name["t"]["depth"] == 2, "declared inside the while body"

    def test_semantic_timing_is_recorded(self, client: TestClient, demo_source: str):
        body = client.post("/compile", json={"source": demo_source}).json()
        phases = [t["phase"] for t in body["meta"]["timings"]]
        assert phases == [
            "lexer",
            "parser",
            "table_parser",
            "semantic",
            "tac",
            "optimize",
            "codegen",
            "total",
        ]

    def test_type_errors_are_reported_with_positions(self, client: TestClient):
        body = client.post("/compile", json={"source": "func m(): void { int x = 1.5; }"}).json()
        assert len(body["typeErrors"]) == 1

        diagnostic = body["typeErrors"][0]
        assert diagnostic["severity"] == "error"
        assert diagnostic["phase"] == "semantic"
        assert diagnostic["line"] == 1
        assert diagnostic["col"] >= 1
        assert body["meta"]["ok"] is False

    def test_a_type_error_still_returns_the_tree_and_the_symbols(self, client: TestClient):
        """A wrong type is not a parse failure; the artefacts must survive it."""
        body = client.post("/compile", json={"source": "func m(): void { int x = 1.5; }"}).json()
        assert body["ast"] is not None
        assert body["symbols"]

    def test_ast_carries_the_inferred_types(self, client: TestClient):
        """The tree is serialised after annotation, not before."""
        source = "func m(): void { float f = 1; bool b = f < 2.0; }"
        ast = client.post("/compile", json={"source": source}).json()["ast"]

        typed = {}
        stack = [ast]
        while stack:
            node = stack.pop()
            if node.get("inferredType"):
                typed[node["name"]] = node["inferredType"]
            stack.extend(node["children"])

        assert typed, "no expression was annotated"
        assert typed["BinaryOp (<)"] == "bool"
        assert typed["Identifier (f)"] == "float"

    def test_a_syntax_error_skips_phase_three_without_crashing(self, client: TestClient):
        """`program` is unbound on the parse-error path; the guard must hold."""
        body = client.post("/compile", json={"source": "func m(): void { int x = 5 }"}).json()
        assert body["symbols"] == []
        assert body["typeErrors"] == []
        assert body["meta"]["reachedPhase"] == "lexer"

    def test_warnings_do_not_make_a_program_fail(self, client: TestClient):
        source = "func f(int n): int { if (n > 0) { return 1; } }"
        body = client.post("/compile", json={"source": source}).json()
        assert [d["severity"] for d in body["typeErrors"]] == ["warning"]
        assert body["meta"]["ok"] is True
