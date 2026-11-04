"""Benchmark: Baseline A vs. Baseline B vs. the full recovery engine.

Tier B item 11, and the project's own evaluation methodology (the plan,
section 7) -- nothing in this file is a textbook algorithm; the whole point
is *measuring* what panic-mode recovery plus this project's ranking and
suppression layer (``app/errors.py``) actually buys over not having them,
against a corpus with known ground truth (``tests/buggy/manifest.json``).

The three parsers compared, exactly as the plan names them:

* **Baseline A** -- ``app.parser.parse``: abort on the first syntax error.
* **Baseline B** -- ``app.errors.recover_parse(..., rank=False,
  suppress=False)``: panic-mode resynchronisation with neither ranked
  suggestions nor cascade suppression. The same engine as "yours", with two
  features turned off, not a second implementation -- see
  :mod:`app.errors`'s own docstring for why that is deliberate.
* **Yours** (``full_engine`` below) -- ``app.errors.recover_parse(...)``
  with both features on.

Matching a parser's reports against ground truth: a report matches a seeded
error if its line is within one of the seeded line (panic-mode resync
commonly shifts the reported line by one, since recovery resumes at the
*next* token, which may already be on a different line) and it has not
already been claimed by an earlier, better-matching report -- greedy,
nearest-line, one-to-one.

Run as a script: ``python benchmark.py`` writes ``results.csv`` and prints
an aggregate summary. The logic is factored into :func:`run_benchmark` so it
can be tested without shelling out -- see ``tests/test_benchmark.py``.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.errors import recover_parse
from app.lexer import Token, tokenize
from app.parser import ParseError
from app.parser import parse as baseline_parse
from app.schemas import SyntaxErrorReport

#: How many lines a reported error may be from the seeded one and still
#: count as a match for it.
_LINE_TOLERANCE: Final[int] = 1

#: Per category, the exact `.suggestion` text the full engine's own ranking
#: formula (``app.errors.rank_suggestions``) produces for a correctly
#: ranked top candidate -- read directly off that formula, not guessed.
#: `missing_type_keyword` has four acceptable answers because which of
#: int/float/bool/void ranks first depends on the found identifier's own
#: spelling, which differs per fixture.
_EXPECTED_TOP_SUGGESTION: Final[dict[str, frozenset[str]]] = {
    "missing_semicolon": frozenset({"insert ';'"}),
    "assign_for_eq": frozenset({"'==' instead of '='"}),
    "unbalanced_bracket": frozenset({"insert ')'", "insert '}'"}),
    "missing_type_keyword": frozenset(
        {"insert 'int'", "insert 'float'", "insert 'bool'", "insert 'void'"}
    ),
    "missing_colon_before_return_type": frozenset({"insert ':'"}),
    "misspelled_keyword": frozenset({"insert 'func'"}),
}


def _baseline_a_reports(tokens: list[Token]) -> list[SyntaxErrorReport]:
    """Run Baseline A: abort on the first syntax error."""
    try:
        baseline_parse(tokens)
        return []
    except ParseError as exc:
        return [exc.to_api()]


def _baseline_b_reports(tokens: list[Token]) -> list[SyntaxErrorReport]:
    """Run Baseline B: panic-mode recovery, no ranking, no suppression."""
    return recover_parse(tokens, rank=False, suppress=False).reports


def _full_engine_reports(tokens: list[Token]) -> list[SyntaxErrorReport]:
    """Run the full engine: panic-mode recovery, ranked, suppressed."""
    return recover_parse(tokens, rank=True, suppress=True).reports


#: Every parser under comparison, in the order results.csv reports them.
PARSERS: Final[dict[str, Callable[[list[Token]], list[SyntaxErrorReport]]]] = {
    "baseline_a": _baseline_a_reports,
    "baseline_b": _baseline_b_reports,
    "full_engine": _full_engine_reports,
}


@dataclass(frozen=True)
class ResultRow:
    """One row of the benchmark: one parser's performance on one file.

    Attributes:
        file: The buggy file's name.
        parser: Which of :data:`PARSERS` produced this row.
        ground_truth_errors: How many errors were actually seeded.
        reports_emitted: How many errors this parser reported in total.
        true_positives: Reports that matched a seeded error.
        false_positives: Reports that matched nothing.
        precision: ``true_positives / reports_emitted`` (0 if none emitted).
        recall: ``true_positives / ground_truth_errors``.
        top1_accuracy: Among matched reports, the fraction whose top
            suggestion is the category-correct fix (1.0 if there were none
            to check, since there is nothing wrong to count against it).
    """

    file: str
    parser: str
    ground_truth_errors: int
    reports_emitted: int
    true_positives: int
    false_positives: int
    precision: float
    recall: float
    top1_accuracy: float


def _match(
    seeded_errors: list[dict], reports: list[SyntaxErrorReport]
) -> list[tuple[dict, SyntaxErrorReport | None]]:
    """Greedily match each seeded error to its nearest unclaimed report.

    Args:
        seeded_errors: The manifest's ground truth for one file.
        reports: One parser's reports for that file.

    Returns:
        One ``(seeded_error, matching_report_or_None)`` pair per seeded
        error, in manifest order.
    """
    unclaimed = list(reports)
    matches: list[tuple[dict, SyntaxErrorReport | None]] = []
    for seeded in seeded_errors:
        best: SyntaxErrorReport | None = None
        best_distance: int | None = None
        for report in unclaimed:
            distance = abs(report.line - seeded["line"])
            if distance <= _LINE_TOLERANCE and (best_distance is None or distance < best_distance):
                best, best_distance = report, distance
        if best is not None:
            unclaimed.remove(best)
        matches.append((seeded, best))
    return matches


def _is_top1_correct(seeded: dict, report: SyntaxErrorReport) -> bool:
    """Whether a matched report's top suggestion is the category-correct fix."""
    expected = _EXPECTED_TOP_SUGGESTION.get(seeded["category"])
    return expected is not None and report.suggestion in expected


def _score(
    file_name: str, parser_name: str, seeded_errors: list[dict], reports: list[SyntaxErrorReport]
) -> ResultRow:
    """Score one parser's reports for one file against its ground truth."""
    matches = _match(seeded_errors, reports)
    matched_reports = {id(report) for _, report in matches if report is not None}
    true_positives = len(matched_reports)
    false_positives = len(reports) - true_positives

    correct_top1 = sum(
        1 for seeded, report in matches if report is not None and _is_top1_correct(seeded, report)
    )
    top1_accuracy = correct_top1 / true_positives if true_positives else 1.0

    reports_emitted = len(reports)
    ground_truth_errors = len(seeded_errors)
    return ResultRow(
        file=file_name,
        parser=parser_name,
        ground_truth_errors=ground_truth_errors,
        reports_emitted=reports_emitted,
        true_positives=true_positives,
        false_positives=false_positives,
        precision=(true_positives / reports_emitted) if reports_emitted else 0.0,
        recall=(true_positives / ground_truth_errors) if ground_truth_errors else 0.0,
        top1_accuracy=top1_accuracy,
    )


def run_benchmark(buggy_dir: Path, manifest: list[dict]) -> list[ResultRow]:
    """Run every parser against every file in the manifest.

    Args:
        buggy_dir: Directory the manifest's file names are relative to.
        manifest: The parsed contents of ``manifest.json``.

    Returns:
        One :class:`ResultRow` per ``(file, parser)`` combination.
    """
    rows: list[ResultRow] = []
    for entry in manifest:
        source = (buggy_dir / entry["file"]).read_text(encoding="utf-8")
        for parser_name, run in PARSERS.items():
            tokens = tokenize(source).tokens
            reports = run(tokens)
            rows.append(_score(entry["file"], parser_name, entry["seeded_errors"], reports))
    return rows


def _write_csv(rows: list[ResultRow], path: Path) -> None:
    """Write one row per `(file, parser)` to a CSV file."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "file",
                "parser",
                "ground_truth_errors",
                "reports_emitted",
                "true_positives",
                "false_positives",
                "precision",
                "recall",
                "top1_accuracy",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.file,
                    row.parser,
                    row.ground_truth_errors,
                    row.reports_emitted,
                    row.true_positives,
                    row.false_positives,
                    f"{row.precision:.3f}",
                    f"{row.recall:.3f}",
                    f"{row.top1_accuracy:.3f}",
                ]
            )


def _print_summary(rows: list[ResultRow]) -> None:
    """Print one aggregate row per parser across the whole corpus."""
    by_parser: dict[str, list[ResultRow]] = {}
    for row in rows:
        by_parser.setdefault(row.parser, []).append(row)

    print(
        f"{'parser':<12} {'truth':>6} {'emitted':>8} {'TP':>5} {'FP':>5} {'prec':>6} {'rec':>6} {'top1':>6}"
    )
    for parser_name in PARSERS:
        group = by_parser.get(parser_name, [])
        truth = sum(r.ground_truth_errors for r in group)
        emitted = sum(r.reports_emitted for r in group)
        tp = sum(r.true_positives for r in group)
        fp = sum(r.false_positives for r in group)
        precision = tp / emitted if emitted else 0.0
        recall = tp / truth if truth else 0.0
        top1_values = [r.top1_accuracy for r in group if r.true_positives > 0]
        top1 = sum(top1_values) / len(top1_values) if top1_values else 1.0
        print(
            f"{parser_name:<12} {truth:>6} {emitted:>8} {tp:>5} {fp:>5} "
            f"{precision:>6.3f} {recall:>6.3f} {top1:>6.3f}"
        )


def main() -> None:
    """Run the benchmark over ``tests/buggy/`` and write ``results.csv``."""
    repo_root = Path(__file__).resolve().parent
    buggy_dir = repo_root / "tests" / "buggy"
    manifest = json.loads((buggy_dir / "manifest.json").read_text(encoding="utf-8"))

    rows = run_benchmark(buggy_dir, manifest)
    _write_csv(rows, repo_root / "results.csv")
    _print_summary(rows)


if __name__ == "__main__":
    main()
