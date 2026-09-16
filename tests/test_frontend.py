"""Static consistency checks between the frontend and the backend contract.

There is no bundler and no type checker standing between ``static/*.js`` and
``app/schemas.py``, so nothing would otherwise catch a renamed DOM id, a
mismatched field name, or an outright JavaScript syntax error until it failed
silently in the browser -- which, for this project, means during a demo. These
tests are the substitute for that missing build step.

They are deliberately static (regex over the files, plus ``node --check``)
rather than browser-driven: a headless browser would be a heavy dependency for
a project whose stated design rule is "no npm, no build step".
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.lexer import TokenCategory
from app.schemas import OptimizationToggles

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

#: Every hand-written script. The vendored D3 bundle is excluded: it is
#: third-party and not ours to lint.
SCRIPTS = ["app.js", "editor.js", "highlight.js", "viz.js"]
SCRIPT_SOURCES = {name: (STATIC_DIR / name).read_text(encoding="utf-8") for name in SCRIPTS}

APP_JS = SCRIPT_SOURCES["app.js"]
STYLE_CSS = (STATIC_DIR / "style.css").read_text(encoding="utf-8")

#: Every `$("#some-id")` lookup performed by app.js.
JS_ELEMENT_IDS = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"\)', APP_JS))

#: Every `id="..."` present in the markup.
HTML_ELEMENT_IDS = set(re.findall(r'id="([^"]+)"', INDEX_HTML))

NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="Node.js is not installed")


class TestJavaScriptSyntax:
    """A syntax error in any script breaks the whole UI, silently."""

    @requires_node
    @pytest.mark.parametrize("script", SCRIPTS)
    def test_script_parses(self, script: str):
        """`node --check` parses without executing, so this is safe and fast."""
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [NODE, "--check", str(STATIC_DIR / script)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"{script} has a syntax error:\n{result.stderr}"

    @pytest.mark.parametrize("script", SCRIPTS)
    def test_script_is_strict_mode(self, script: str):
        assert '"use strict"' in SCRIPT_SOURCES[script]


class TestDomContract:
    """Every element the controller reaches for must exist in the markup."""

    def test_no_javascript_lookup_is_dangling(self):
        missing = sorted(JS_ELEMENT_IDS - HTML_ELEMENT_IDS)
        assert not missing, f"app.js queries ids that index.html does not define: {missing}"

    def test_controller_actually_queries_something(self):
        """Guard the guard: a broken regex here would make the test vacuous."""
        assert len(JS_ELEMENT_IDS) >= 15

    @pytest.mark.parametrize(
        "element_id",
        [
            "editor",
            "btn-compile",
            "btn-run",
            "language-select",
            "token-filter",
            "token-table-wrap",
            "token-viewport",
            "token-rows",
            "token-spacer",
            "token-stats",
            "lexical-empty",
            "out-problems",
            "problems-list",
            "out-build",
            "out-program",
            "problem-count",
            "outputpane",
            "sidebar",
            "phase-list",
            "parse-tree-wrap",
            "tree-toolbar",
            "syntax-empty",
            "menu-dropdown",
            "opt-toggles",
            "status-state",
            "status-caret",
            "status-tokens",
            "status-phases",
            "status-version",
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

    def test_editor_component_is_mounted_by_id(self):
        assert 'mount: "#editor"' in APP_JS


class TestEditorLayerAlignment:
    """The transparent-textarea editor only works if its layers align exactly.

    ``.editor-paint`` (highlighted HTML) and ``.editor-input`` (the real
    textarea) are stacked. If any glyph-affecting property differs between
    them, the caret drifts away from the text as the user types -- the classic
    failure of this technique. Those properties are therefore declared once on
    the shared ``.editor-layer`` rule, and these tests keep it that way.
    """

    #: Properties that change where a glyph lands.
    METRIC_PROPERTIES = ["font:", "padding:", "tab-size:", "white-space:", "letter-spacing:"]

    def _rule_body(self, selector: str) -> str:
        """Return the declaration block of a CSS rule.

        Args:
            selector: The exact selector text to find.

        Returns:
            The text between that selector's braces.
        """
        match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", STYLE_CSS)
        assert match, f"CSS rule not found: {selector}"
        return match.group(1)

    @pytest.mark.parametrize("prop", METRIC_PROPERTIES)
    def test_shared_layer_defines_every_metric(self, prop: str):
        assert prop in self._rule_body(".editor-layer")

    @pytest.mark.parametrize("selector", [".editor-paint", ".editor-input"])
    @pytest.mark.parametrize("prop", METRIC_PROPERTIES)
    def test_individual_layers_do_not_override_metrics(self, selector: str, prop: str):
        """Overriding a metric on one layer alone is what breaks alignment."""
        assert prop not in self._rule_body(
            selector
        ), f"{selector} overrides '{prop}' - it must only be set on .editor-layer"

    def test_gutter_shares_the_code_line_height(self):
        """Line numbers must track code lines exactly when scrolling."""
        assert "var(--code-line)" in self._rule_body(".editor-gutter")
        assert "var(--code-line)" in self._rule_body(".editor-layer")


class TestVirtualTableContract:
    """The windowed token table depends on JS and CSS agreeing on row height."""

    def test_row_height_matches_the_stylesheet(self):
        """If these drift, rows overlap or leave gaps while scrolling."""
        js_match = re.search(r"const ROW_HEIGHT = (\d+);", APP_JS)
        assert js_match, "ROW_HEIGHT not found in app.js"

        css_match = re.search(r"\.vt-row\s*\{[^}]*?height:\s*(\d+)px", STYLE_CSS, re.DOTALL)
        assert css_match, ".vt-row height not found in style.css"

        assert js_match.group(1) == css_match.group(1), (
            f"ROW_HEIGHT is {js_match.group(1)}px in app.js "
            f"but .vt-row is {css_match.group(1)}px in style.css"
        )

    def test_header_and_row_share_a_column_template(self):
        """Misaligned columns would make the header meaningless."""
        match = re.search(r"\.vt-header,\s*\.vt-row\s*\{([^}]*)\}", STYLE_CSS)
        assert match, "vt-header and vt-row must share one grid-template rule"
        assert "grid-template-columns" in match.group(1)

    def test_table_is_windowed_not_fully_rendered(self):
        """Rendering every row would stall the UI on a long program."""
        assert "renderTokenWindow" in APP_JS
        assert "ROW_OVERSCAN" in APP_JS


class TestCategoryStyling:
    """Every backend token category needs a colour class, or rows render bare."""

    @pytest.mark.parametrize("category", [c.value for c in TokenCategory])
    def test_category_has_a_css_class(self, category: str):
        assert f".cat-{category}" in STYLE_CSS, f"no .cat-{category} rule in style.css"

    def test_problem_rows_are_styled_under_their_actual_class(self):
        """CSS selectors are exact: `.problem` does not match `class="problem-item"`."""
        assert ".problem-item {" in STYLE_CSS
        assert 'class="problem-item' in APP_JS


class TestLanguageSupport:
    """The language picker and the highlighter must offer the same languages."""

    def test_picker_options_all_have_highlighting_rules(self):
        options = set(re.findall(r'<option value="([^"]+)"', INDEX_HTML))
        highlight_js = SCRIPT_SOURCES["highlight.js"]
        defined = set(re.findall(r"^\s{4}(\w+): \[", highlight_js, re.MULTILINE))
        assert options <= defined, f"no highlighting rules for: {sorted(options - defined)}"

    def test_minilang_keywords_match_the_compiler(self):
        """The editor must not colour a word the real lexer does not reserve."""
        from app.lexer import KEYWORDS as REAL_KEYWORDS

        highlight_js = SCRIPT_SOURCES["highlight.js"]
        block = re.search(r"minilang: \[(.*?)\]", highlight_js, re.DOTALL)
        assert block, "minilang keyword list not found in highlight.js"
        painted = set(re.findall(r'"(\w+)"', block.group(1)))
        assert painted == set(REAL_KEYWORDS), (
            f"editor paints {sorted(painted - set(REAL_KEYWORDS))} which the lexer does not "
            f"reserve; lexer reserves {sorted(set(REAL_KEYWORDS) - painted)} which is not painted"
        )

    def test_non_minilang_builds_are_refused_in_the_ui(self):
        """Only MiniLang has a backend pipeline; Build must say so, not 500."""
        assert 'language !== "minilang"' in APP_JS


class TestTabTooltips:
    """Every tab explains itself on hover, and says something true."""

    def test_every_tab_has_a_tooltip(self):
        buttons = re.findall(r"<button[^>]*?class=\"tab[^>]*?>", INDEX_HTML)
        assert len(buttons) == 8
        for button in buttons:
            assert 'title="' in button, f"tab without a tooltip: {button[:80]}"

    def test_tooltips_are_not_all_identical(self):
        """A uniform tooltip would be wrong for most tabs; each states its own reason."""
        titles = re.findall(r'class="tab[^>]*?title="([^"]+)"', INDEX_HTML)
        assert len(set(titles)) == len(titles), "tab tooltips are duplicated"

    @pytest.mark.parametrize(
        ("tab", "fragment"),
        [
            ("lexical", "run Build"),
            ("syntax", "run Build"),
            ("theory", "Tier B"),
            ("semantic", "step 4"),
            ("icg", "step 5"),
            ("optimization", "step 6"),
            ("target", "step 7"),
        ],
    )
    def test_tooltip_names_the_right_milestone(self, tab: str, fragment: str):
        """A built tab says 'run Build'; an unbuilt one names the step that fills it."""
        match = re.search(rf'data-tab="{tab}" title="([^"]+)"', INDEX_HTML)
        assert match, f"no tooltip for tab {tab}"
        assert fragment in match.group(1)


class TestParseTreeContract:
    """The Syntax tab's renderer and its container must agree."""

    def test_viz_exports_render_tree(self):
        assert "renderTree:" in SCRIPT_SOURCES["viz.js"]

    def test_controller_calls_render_tree(self):
        assert 'Viz.renderTree("#parse-tree-wrap"' in APP_JS

    def test_tree_is_redrawn_when_its_tab_becomes_visible(self):
        """An SVG laid out inside display:none measures zero and renders wrong."""
        assert 'name === "syntax"' in APP_JS

    def test_every_node_kind_the_backend_emits_has_a_colour(self):
        """A kind with no colour renders in the fallback grey, losing meaning."""
        from app.ast_nodes import ASTNode

        viz = SCRIPT_SOURCES["viz.js"]
        coloured = set(re.findall(r"^    (\w+): \"var\(", viz, re.MULTILINE))

        emitted = set()
        for subclass in ASTNode.__subclasses__():
            emitted.update(_kinds_of(subclass))
        assert emitted <= coloured, f"kinds with no colour: {sorted(emitted - coloured)}"

    def test_measure_is_iterative_not_recursive(self):
        """A deep tree would otherwise risk the JS stack on every render."""
        viz = SCRIPT_SOURCES["viz.js"]
        body = viz[viz.index("function measure(") :]
        body = body[: body.index("\n  }")]
        assert "stack" in body
        assert "while" in body


def _kinds_of(cls) -> set[str]:
    """Return the `kind` strings a node class and its subclasses emit.

    Args:
        cls: An :class:`~app.ast_nodes.ASTNode` subclass.

    Returns:
        Every literal passed as ``kind=`` in that class tree's ``to_dict``.
    """
    import inspect

    kinds: set[str] = set()
    for subclass in [cls, *cls.__subclasses__()]:
        try:
            source = inspect.getsource(subclass)
        except OSError:  # pragma: no cover - source always available here
            continue
        kinds.update(re.findall(r'kind="(\w+)"', source))
        if subclass is not cls:
            kinds.update(_kinds_of(subclass))
    return kinds


class TestOptimizationToggleContract:
    """The checkbox names are sent verbatim to a model with extra='forbid'."""

    def test_ui_toggle_names_match_the_schema_aliases_exactly(self):
        ui_names = set(re.findall(r'data-opt="([^"]+)"', INDEX_HTML))
        schema_aliases = {field.alias for field in OptimizationToggles.model_fields.values()}
        assert ui_names == schema_aliases, (
            f"UI toggles and schema disagree; only in UI: {ui_names - schema_aliases}, "
            f"only in schema: {schema_aliases - ui_names}"
        )

    def test_default_checked_state_matches_the_schema_defaults(self):
        """An unchecked box whose model default is True would mislead the user."""
        pattern = re.compile(r'<input type="checkbox" data-opt="([^"]+)"([^>]*)>')
        checked_in_ui = {name: "checked" in attrs for name, attrs in pattern.findall(INDEX_HTML)}
        assert checked_in_ui, "no optimisation checkboxes found in the markup"

        defaults = OptimizationToggles()
        for field_name, field in OptimizationToggles.model_fields.items():
            assert checked_in_ui[field.alias] is getattr(
                defaults, field_name
            ), f"checkbox '{field.alias}' does not match its schema default"


class TestOfflineRequirement:
    """The demo must work on a machine with no internet access."""

    def test_no_external_resources_are_referenced(self):
        sources = {"index.html": INDEX_HTML, **SCRIPT_SOURCES}
        for name, source in sources.items():
            for url in re.findall(r'(?:src|href)="(https?://[^"]+)"', source):
                pytest.fail(f"{name} loads an external resource: {url}")

    def test_d3_is_vendored_and_referenced_locally(self):
        assert (STATIC_DIR / "lib" / "d3.v7.min.js").is_file(), "D3 is not vendored"
        assert "/static/lib/d3.v7.min.js" in INDEX_HTML

    def test_every_script_is_loaded_by_the_page(self):
        for name in SCRIPTS:
            assert f"/static/{name}" in INDEX_HTML, f"{name} is never loaded"

    def test_backend_calls_are_all_same_origin(self):
        """Every fetch must be a relative path, never an absolute URL."""
        for target in re.findall(r'fetch\(\s*"([^"]+)"', APP_JS):
            assert target.startswith("/"), f"fetch target is not same-origin: {target}"


class TestNoDebuggingLeftovers:
    """The frontend may use the console for real errors, but not debug logs."""

    @pytest.mark.parametrize("script", SCRIPTS)
    def test_no_console_log_calls(self, script: str):
        assert "console.log(" not in SCRIPT_SOURCES[script]

    @pytest.mark.parametrize("script", SCRIPTS)
    def test_no_debugger_statements(self, script: str):
        assert "debugger" not in SCRIPT_SOURCES[script]


class TestDarkThemeOnly:
    """The UI was specified as dark-only; a stray light rule would break it."""

    def test_no_light_scheme_media_query(self):
        assert "prefers-color-scheme: light" not in STYLE_CSS

    def test_theme_colours_come_from_tokens(self):
        """Hard-coded colours outside :root make the theme impossible to tune."""
        root = re.search(r":root\s*\{(.*?)\}", STYLE_CSS, re.DOTALL)
        assert root, ":root token block not found"
        assert root.group(1).count("--") > 30
