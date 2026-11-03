"""Tests for :mod:`app.literals`.

This module is the one place three different phases (TAC generation,
optimisation, and the VM) must agree on what a literal's spelling means, so
its own correctness is tested directly rather than only indirectly through
whichever phase happens to exercise it.
"""

from __future__ import annotations

import pytest

from app.literals import (
    format_literal,
    is_literal,
    parse_literal,
    truncating_divide,
    truncating_modulo,
)


class TestIsLiteral:
    def test_recognises_integers(self):
        assert is_literal("5")
        assert is_literal("-5")

    def test_recognises_floats(self):
        assert is_literal("3.5")
        assert is_literal("-3.5")

    def test_recognises_booleans(self):
        assert is_literal("true")
        assert is_literal("false")

    def test_rejects_a_name(self):
        assert not is_literal("x")
        assert not is_literal("t1")

    def test_rejects_none(self):
        assert not is_literal(None)


class TestParseLiteral:
    def test_parses_an_integer_with_no_decimal_point(self):
        value = parse_literal("5")
        assert value == 5
        assert isinstance(value, int)

    def test_parses_a_float_with_a_decimal_point(self):
        value = parse_literal("3.5")
        assert value == 3.5
        assert isinstance(value, float)

    def test_parses_booleans(self):
        assert parse_literal("true") is True
        assert parse_literal("false") is False

    def test_parses_negative_numbers(self):
        assert parse_literal("-5") == -5
        assert parse_literal("-3.5") == -3.5


class TestFormatLiteral:
    def test_formats_booleans_as_minilang_spellings(self):
        assert format_literal(True) == "true"
        assert format_literal(False) == "false"

    def test_formats_numbers_via_str(self):
        assert format_literal(5) == "5"
        assert format_literal(3.5) == "3.5"

    def test_round_trips_through_parse_and_format(self):
        for spelling in ("5", "-5", "3.5", "-3.5", "true", "false"):
            assert format_literal(parse_literal(spelling)) == spelling


class TestTruncatingDivide:
    def test_truncates_toward_zero_for_two_positive_ints(self):
        assert truncating_divide(7, 2) == 3

    def test_truncates_toward_zero_for_a_negative_dividend(self):
        """Python's own `//` floors, which would wrongly give -4 here."""
        assert truncating_divide(-7, 2) == -3

    def test_truncates_toward_zero_for_a_negative_divisor(self):
        assert truncating_divide(7, -2) == -3

    def test_float_division_is_not_truncated(self):
        assert truncating_divide(7.0, 2) == 3.5

    def test_raises_on_division_by_zero(self):
        with pytest.raises(ZeroDivisionError):
            truncating_divide(1, 0)


class TestTruncatingModulo:
    def test_follows_the_dividend_sign(self):
        """-7 truncates to -3 * 2 = -6; -7 - (-6) = -1."""
        assert truncating_modulo(-7, 2) == -1

    def test_matches_c_for_two_positive_operands(self):
        assert truncating_modulo(7, 2) == 1

    def test_raises_on_modulo_by_zero(self):
        with pytest.raises(ZeroDivisionError):
            truncating_modulo(1, 0)
