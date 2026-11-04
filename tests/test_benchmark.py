"""Tests for :mod:`benchmark`.

Every claim here is checked against the actual measured numbers, not against
what the project originally expected to find. The most important of those
corrections is documented in :func:`TestHonestFindings
.test_the_corpus_does_not_show_a_false_positive_gap`: building this
benchmark corpus surfaced two genuine panic-mode recovery bugs (both fixed
in ``app/errors.py``, both pinned in ``tests/test_errors.py``), and fixing
them made *both* Baseline B and the full engine more robust at once --
which means this corpus, as it stands, does not demonstrate a precision
difference between them. The real, measured difference is recall
(suppression trades a little away) and ranked suggestions (only the full
engine has any), and that is what this file actually asserts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.lexer import tokenize
from app.parser import ParseError
from app.parser import parse as baseline_parse
from benchmark import PARSERS, run_benchmark

BUGGY_DIR = Path(__file__).resolve().parent / "buggy"
MANIFEST = json.loads((BUGGY_DIR / "manifest.json").read_text(encoding="utf-8"))


class TestManifestIntegrity:
    def test_thirty_files_forty_two_seeded_errors(self) -> None:
        assert len(MANIFEST) == 30
        assert sum(len(entry["seeded_errors"]) for entry in MANIFEST) == 42

    def test_eighteen_single_and_twelve_double_error_files(self) -> None:
        counts = [len(entry["seeded_errors"]) for entry in MANIFEST]
        assert counts.count(1) == 18
        assert counts.count(2) == 12

    def test_every_manifest_file_exists(self) -> None:
        for entry in MANIFEST:
            assert (BUGGY_DIR / entry["file"]).exists(), entry["file"]

    def test_six_error_categories_represented(self) -> None:
        categories = {err["category"] for entry in MANIFEST for err in entry["seeded_errors"]}
        assert categories == {
            "missing_semicolon",
            "assign_for_eq",
            "unbalanced_bracket",
            "missing_type_keyword",
            "missing_colon_before_return_type",
            "misspelled_keyword",
        }


class TestManifestGroundTruthIsReal:
    @pytest.mark.parametrize("entry", MANIFEST, ids=lambda e: e["file"])
    def test_buggy_file_genuinely_raises_under_baseline_a(self, entry: dict) -> None:
        """Guard against a fixture being hand-edited back into validity.

        That would silently make that row of the benchmark meaningless.
        """
        source = (BUGGY_DIR / entry["file"]).read_text(encoding="utf-8")
        with pytest.raises(ParseError):
            baseline_parse(tokenize(source).tokens)

    def test_source_programs_referenced_are_themselves_clean(self) -> None:
        programs_dir = BUGGY_DIR.parent / "programs"
        for entry in MANIFEST:
            source = (programs_dir / entry["source_program"]).read_text(encoding="utf-8")
            baseline_parse(tokenize(source).tokens)  # must not raise


class TestRunBenchmark:
    def test_produces_one_row_per_file_per_parser(self) -> None:
        rows = run_benchmark(BUGGY_DIR, MANIFEST[:3])
        assert len(rows) == 3 * len(PARSERS)

    def test_every_metric_is_in_bounds(self) -> None:
        rows = run_benchmark(BUGGY_DIR, MANIFEST)
        for row in rows:
            assert 0.0 <= row.precision <= 1.0
            assert 0.0 <= row.recall <= 1.0
            assert 0.0 <= row.top1_accuracy <= 1.0
            assert row.true_positives + row.false_positives == row.reports_emitted
            assert row.true_positives <= row.ground_truth_errors

    def test_baseline_a_never_exceeds_one_report_per_file(self) -> None:
        rows = run_benchmark(BUGGY_DIR, MANIFEST)
        for row in rows:
            if row.parser == "baseline_a":
                assert row.reports_emitted <= 1


class TestAggregateFindings:
    """The honest, measured picture across the whole corpus."""

    @pytest.fixture(scope="class")
    def rows(self):
        return run_benchmark(BUGGY_DIR, MANIFEST)

    def _totals(self, rows, parser_name: str) -> dict:
        group = [r for r in rows if r.parser == parser_name]
        return {
            "truth": sum(r.ground_truth_errors for r in group),
            "emitted": sum(r.reports_emitted for r in group),
            "tp": sum(r.true_positives for r in group),
            "fp": sum(r.false_positives for r in group),
        }

    def test_baseline_a_misses_every_second_error_in_a_two_error_file(self, rows) -> None:
        totals = self._totals(rows, "baseline_a")
        # 18 single-error files contribute 1 each; 12 two-error files
        # contribute only their first -- 18 + 12 = 30 of the 42 seeded errors.
        assert totals["tp"] == 30
        assert totals["truth"] == 42

    def test_naive_resume_finds_every_seeded_error_on_this_corpus(self, rows) -> None:
        """Baseline B's defining advantage over Baseline A: it keeps going."""
        totals = self._totals(rows, "baseline_b")
        assert totals["tp"] == totals["truth"] == 42

    def test_the_corpus_does_not_show_a_false_positive_gap(self, rows) -> None:
        """An honest finding, not the one originally assumed.

        Building this corpus surfaced two genuine panic-mode bugs (checking
        for a synchronizing token only after unconditionally discarding one,
        and not depth-tracking the very first discarded token) -- both now
        fixed in ``app/errors.py``, both pinned directly in
        ``tests/test_errors.py``. Fixing them improved Baseline B and the
        full engine's recovery *at the same time*, since both share the
        same ``_panic_skip_statement``/``_panic_skip_function``. The result:
        on this corpus, neither baseline ever reports something that is not
        a real seeded error.
        """
        for parser_name in ("baseline_a", "baseline_b", "full_engine"):
            totals = self._totals(rows, parser_name)
            assert totals["fp"] == 0, f"{parser_name} produced false positives"

    def test_suppression_s_real_cost_is_recall_not_precision(self, rows) -> None:
        """What cascade suppression actually trades away on this corpus.

        The six "close" two-error files are deliberately within the default
        2-token suppression window -- exactly the plan's own request to
        check that suppression does not also eat a second genuine nearby
        error. It does, here: the full engine reports one of the two,
        trading recall for its protection against a cascade that would
        otherwise look identical -- a real, measured trade-off, not a free
        win.
        """
        full = self._totals(rows, "full_engine")
        naive = self._totals(rows, "baseline_b")
        assert full["tp"] < naive["tp"]
        assert full["tp"] == 36  # 42 seeded, minus the 6 suppressed "close" pairs

    def test_ranked_suggestions_are_the_full_engines_real_advantage(self, rows) -> None:
        """Check the one thing Baseline A and B categorically cannot offer.

        The full engine does, correctly, on every single matched report.
        """
        full = [r for r in rows if r.parser == "full_engine" and r.true_positives > 0]
        assert full
        assert all(r.top1_accuracy == 1.0 for r in full)

        for parser_name in ("baseline_a", "baseline_b"):
            group = [r for r in rows if r.parser == parser_name and r.true_positives > 0]
            assert all(r.top1_accuracy == 0.0 for r in group)
