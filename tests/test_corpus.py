"""Every file in tests/programs/ is claimed valid -- this proves it.

Not a textbook-sourced test (there is no "section" for "this fixture file is
what it says it is"): a proven invariant specific to this project's own
corpus, which both the step-through parser's cross-validation
(``tests/test_table_parser.py``) and the recovery benchmark
(``tests/test_benchmark.py``) depend on being true.
"""

from __future__ import annotations

import pytest

from app.lexer import tokenize
from app.parser import parse
from app.semantic import analyse
from tests.conftest import PROGRAMS_DIR


@pytest.mark.parametrize("program_path", sorted(PROGRAMS_DIR.glob("*.ml")), ids=lambda p: p.name)
def test_all_programs_compile_clean(program_path) -> None:
    source = program_path.read_text(encoding="utf-8")
    program = parse(tokenize(source).tokens)
    result = analyse(program)
    assert result.ok, f"{program_path.name}: {[d.message for d in result.diagnostics]}"
    assert result.diagnostics == []


def test_corpus_has_at_least_twelve_programs() -> None:
    # The canonical demo program plus the eleven added for the benchmark's
    # seeded-error corpus (plan section 7).
    assert len(list(PROGRAMS_DIR.glob("*.ml"))) >= 12
