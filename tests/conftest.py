"""Shared pytest fixtures for the MiniLang test suite."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

#: Repository root, resolved from this file so tests run from any directory.
REPO_ROOT = Path(__file__).resolve().parent.parent

#: Directory holding valid MiniLang programs used as fixtures.
PROGRAMS_DIR = REPO_ROOT / "tests" / "programs"

#: Directory holding programs with deliberately seeded syntax errors.
BUGGY_DIR = REPO_ROOT / "tests" / "buggy"


@pytest.fixture(scope="session")
def client() -> TestClient:
    """Return a FastAPI test client bound to the application.

    Session-scoped because the app is stateless; building it once keeps the
    suite fast.

    Returns:
        A :class:`fastapi.testclient.TestClient` for the MiniLang app.
    """
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def demo_source() -> str:
    """Return the canonical demo program's source text.

    Returns:
        The contents of ``tests/programs/demo.ml``.
    """
    return (PROGRAMS_DIR / "demo.ml").read_text(encoding="utf-8")
