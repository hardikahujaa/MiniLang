"""MiniLang compiler package.

Each compilation phase lives in its own module so that the FastAPI layer in
:mod:`app.main` stays a thin orchestrator:

``lexer`` -> ``parser`` -> ``semantic`` -> ``tac`` -> ``optimize`` -> ``codegen`` -> ``vm``

Supporting modules (``grammar``, ``table_parser``, ``regex_dfa``, ``errors``)
are independent of that pipeline and are consumed by their own endpoints.
"""

__version__ = "0.1.0"
