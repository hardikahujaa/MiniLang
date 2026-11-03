"""Shared conversions between MiniLang's literal spellings and Python values.

Three phases need exactly this mapping and must agree on it byte for byte:
:mod:`app.tac` (rendering a ``Literal`` AST node as a TAC place),
:mod:`app.optimize` (folding constants at compile time), and :mod:`app.vm`
(pushing a ``PUSH`` operand's value at run time). Centralising it here is
what keeps those three from quietly drifting apart -- the alternative, three
independent copies of "is this token a number", is exactly how a compiler
ends up folding a constant one way and executing it another.

The spelling convention, fixed once, here
-------------------------------------------
``"true"``/``"false"`` for a boolean; otherwise whatever ``str()`` produces
for the Python ``int`` or ``float`` the token parses as. An integer literal
never contains a decimal point (the lexer never emits one for ``INT_LIT``),
so the presence of ``"."`` is what tells an integer spelling apart from a
float one on the way back in.
"""

from __future__ import annotations


def is_literal(token: str | None) -> bool:
    """Report whether a TAC/assembly operand is already a literal spelling.

    Args:
        token: The raw text of an operand slot, or ``None``.

    Returns:
        True for ``"true"``, ``"false"``, or anything :func:`float` accepts
        -- which a variable or temporary name, always identifier-shaped,
        never is.
    """
    if token is None:
        return False
    if token in ("true", "false"):
        return True
    try:
        float(token)
    except ValueError:
        return False
    return True


def parse_literal(token: str) -> int | float | bool:
    """Parse a literal's spelling back into the Python value it denotes.

    Args:
        token: A literal spelling, as :func:`is_literal` accepts it.

    Returns:
        ``True``/``False`` for a boolean spelling; an ``int`` for a spelling
        with no decimal point; a ``float`` otherwise.
    """
    if token == "true":
        return True
    if token == "false":
        return False
    return float(token) if "." in token else int(token)


def format_literal(value: int | float | bool) -> str:
    """Render a Python value back into MiniLang's literal spelling.

    Args:
        value: The value to render.

    Returns:
        ``"true"``/``"false"`` for a boolean, else the value's ``str()``.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


# ---------------------------------------------------------------------------
# Arithmetic shared between compile-time folding and run-time execution
#
# `/` and `%` are the one place MiniLang's arithmetic is not just Python's:
# dividing two ints truncates toward zero (C semantics), not Python's floor
# division, and `%` follows from the same truncated quotient so its sign
# tracks the dividend rather than the divisor. app.optimize folds a `/`/`%`
# at compile time using exactly this; app.vm falls back to it at run time for
# every `/`/`%` folding left alone -- most of all the ones folding declined
# to touch because the divisor was a literal zero. Both must compute the
# same answer, so both call here rather than each writing the formula once.
# ---------------------------------------------------------------------------


def truncating_divide(left: int | float, right: int | float) -> int | float:
    """Divide, truncating toward zero when both operands are ``int``.

    Args:
        left: The dividend.
        right: The divisor.

    Returns:
        ``int(left / right)`` for two ints, else plain float division.

    Raises:
        ZeroDivisionError: If ``right`` is zero -- propagated rather than
            trapped here, so the caller decides what a division by zero
            means in its context (decline to fold; halt the VM).
    """
    if isinstance(left, int) and isinstance(right, int):
        return int(left / right)
    return left / right


def truncating_modulo(left: int | float, right: int | float) -> int | float:
    """Compute a remainder whose sign follows the dividend, as in C.

    Args:
        left: The dividend.
        right: The divisor.

    Returns:
        ``left - truncating_divide(left, right) * right``.

    Raises:
        ZeroDivisionError: If ``right`` is zero.
    """
    quotient = truncating_divide(left, right)
    return left - quotient * right
