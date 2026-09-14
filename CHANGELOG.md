# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries are grouped by the build step from the implementation plan, so the
history reads as the story of how the compiler was built rather than as a flat
list of commits.

---

## [Unreleased]

### Tier A · Step 1 — Project scaffold

The goal of this step was narrow and deliberate: get `uvicorn app.main:app
--reload` serving the eight-tab UI, with `/compile` returning the complete JSON
contract, **before** writing a single line of compiler code. Every later step
fills one slot in a contract that already exists and is already under test.

#### Added

- **FastAPI application** (`app/main.py`) with the three endpoints from the
  plan — `POST /compile`, `POST /run`, `POST /analyze-grammar` — plus
  `GET /api/health` (reports which phases are implemented) and `GET /api/demo`
  (serves the canonical demo program). All three POST routes are stubs that
  validate input and return schema-correct empty payloads.
- **The `/compile` JSON contract** (`app/schemas.py`) as typed Pydantic models
  covering every phase's artefacts: tokens, DFA, parse trace, parse tree, AST,
  symbols, type errors, TAC, basic blocks, CFG, optimisation results, assembly,
  program output and syntax errors. Python is `snake_case`, the wire is
  `camelCase`.
- **Eight-tab frontend** (`static/`) — hand-written HTML, CSS and vanilla JS
  with no npm, no bundler and no build step. Each not-yet-implemented panel
  shows an empty state naming the build step that will fill it.
- **D3 v7.9.0 vendored** to `static/lib/`, on day one rather than day fourteen,
  so the demo never depends on the venue having internet access.
- **Structured JSON logging** (`app/logging_config.py`) with per-request
  timing. Backend code does not call `print()`.
- **Canonical demo program** (`app/samples.py`, `tests/programs/demo.ml`) and
  the two sample grammars for the Parser Theory Lab, kept byte-identical by a
  test.
- **79 tests** covering the HTTP contract, the schema translation layer, the
  logging formatter, and the sample fixtures — including deliberately malformed
  requests for every endpoint.
- **GitHub Actions CI** running ruff, `black --check` and pytest on Python 3.10
  and 3.12, then booting the server and hitting `/api/health`, `/` and
  `/compile` to prove the app really starts.
- **Tooling configuration** (`pyproject.toml`): black at 100 columns; ruff
  enforcing type annotations (`ANN`), docstrings (`D`, Google convention), no
  `print()` (`T20`) and no blind excepts (`BLE`).
- `.env.example` documenting every environment variable. All are optional; the
  project runs fully offline with none of them set.

#### Notes

- Optimisation toggles are `StrictBool` rather than `bool`. Pydantic's default
  lax mode coerces `"yes"` into `True`, which would let a frontend bug silently
  *enable* an optimisation pass instead of failing loudly. A test pins this.
