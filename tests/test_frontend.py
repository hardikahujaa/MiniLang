"""Static consistency checks between the frontend and the backend contract.

There is no bundler and no type checker standing between ``static/app.js`` and
``app/schemas.py``, so nothing would otherwise catch a renamed DOM id or a
mismatched field name until it failed silently in the browser -- which, for this
project, means during a demo. These tests are the substitute for that missing
build step.

They are deliberately static (regex over the files) rather than browser-driven:
a headless browser would be a heavy dependency for a project whose stated design
rule is "no npm, no build step".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.schemas import OptimizationToggles

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
APP_JS = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
VIZ_JS = (STATIC_DIR / "viz.js").read_text(encoding="utf-8")

#: Every `$("#some-id")` lookup performed by app.js.
JS_ELEMENT_IDS = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"\)', APP_JS))

#: Every `id="..."` present in the markup.
HTML_ELEMENT_IDS = set(re.findall(r'id="([^"]+)"', INDEX_HTML))


class TestDomContract:
    """Every element the controller reaches for must exist in the markup."""

    def test_no_javascript_lookup_is_dangling(self):
        missing = sorted(JS_ELEMENT_IDS - HTML_ELEMENT_IDS)
        assert not missing, f"app.js queries ids that index.html does not define: {missing}"

    def test_controller_actually_queries_something(self):
        """Guard the guard: a broken regex here would make the test vacuous."""
        assert len(JS_ELEMENT_IDS) >= 10

    @pytest.mark.parametrize(
        "element_id",
        [
            "source-editor",
            "btn-compile",
            "btn-run",
            "btn-load-demo",
            "status-pill",
            "footer-status",
            "program-output",
            "error-list",
            "opt-toggles",
        ],
    )
    def test_essential_elements_are_present(self, element_id: str):
        assert element_id in HTML_ELEMENT_IDS

    def test_every_tab_has_a_matching_panel(self):
        tabs = set(re.findall(r'data-tab="([^"]+)"', INDEX_HTML))
        panels = {i[len("panel-") :] for i in HTML_ELEMENT_IDS if i.startswith("panel-")}
        assert tabs == panels, f"tabs and panels disagree: {tabs ^ panels}"

    def test_there_are_exactly_eight_tabs(self):
        """Plan section 4 specifies eight tabs; fewer means one was dropped."""
        assert len(re.findall(r'data-tab="', INDEX_HTML)) == 8


class TestOptimizationToggleContract:
    """The checkbox names are sent verbatim to a model with extra='forbid'."""

    def test_ui_toggle_names_match_the_schema_aliases_exactly(self):
        """A typo in either file would 422 every compile once toggles are used."""
        ui_names = set(re.findall(r'data-opt="([^"]+)"', INDEX_HTML))
        schema_aliases = {field.alias for field in OptimizationToggles.model_fields.values()}
        assert ui_names == schema_aliases, (
            f"UI toggles and schema disagree; only in UI: {ui_names - schema_aliases}, "
            f"only in schema: {schema_aliases - ui_names}"
        )

    def test_default_checked_state_matches_the_schema_defaults(self):
        """An unchecked box whose model default is True would mislead the user."""
        checkbox_pattern = re.compile(r'<input type="checkbox" data-opt="([^"]+)"([^>]*)>')
        checked_in_ui = {
            name: "checked" in attrs for name, attrs in checkbox_pattern.findall(INDEX_HTML)
        }
        assert checked_in_ui, "no optimisation checkboxes found in the markup"

        defaults = OptimizationToggles()
        for field_name, field in OptimizationToggles.model_fields.items():
            assert checked_in_ui[field.alias] is getattr(
                defaults, field_name
            ), f"checkbox '{field.alias}' does not match its schema default"


class TestOfflineRequirement:
    """The demo must work on a machine with no internet access."""

    def test_no_external_resources_are_referenced(self):
        for source, name in ((INDEX_HTML, "index.html"), (APP_JS, "app.js"), (VIZ_JS, "viz.js")):
            for url in re.findall(r'(?:src|href)="(https?://[^"]+)"', source):
                pytest.fail(f"{name} loads an external resource: {url}")

    def test_d3_is_vendored_and_referenced_locally(self):
        assert (STATIC_DIR / "lib" / "d3.v7.min.js").is_file(), "D3 is not vendored"
        assert "/static/lib/d3.v7.min.js" in INDEX_HTML

    def test_backend_calls_are_all_same_origin(self):
        """Every fetch must be a relative path, never an absolute URL."""
        for target in re.findall(r'fetch\(\s*"([^"]+)"', APP_JS):
            assert target.startswith("/"), f"fetch target is not same-origin: {target}"


class TestNoDebuggingLeftovers:
    """The frontend may use console for real errors, but not stray debug logs."""

    def test_no_console_log_calls(self):
        for source, name in ((APP_JS, "app.js"), (VIZ_JS, "viz.js")):
            assert "console.log(" not in source, f"{name} contains a leftover console.log"

    def test_no_debugger_statements(self):
        for source, name in ((APP_JS, "app.js"), (VIZ_JS, "viz.js")):
            assert "debugger" not in source, f"{name} contains a debugger statement"
