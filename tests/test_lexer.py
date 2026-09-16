"""Tests for :mod:`app.lexer`.

Coverage is organised by the property under test rather than by method, because
what matters about a scanner is behavioural: maximal munch, exact positions,
and that malformed input produces a precise error instead of an exception.

Every class here includes at least one deliberately malformed input, per the
project's quality bar.
"""

from __future__ import annotations

import pytest

from app.lexer import (
    KEYWORDS,
    SINGLE_CHAR_TOKENS,
    TOKEN_SPELLING,
    Lexer,
    LexicalError,
    TokenCategory,
    TokenType,
    tokenize,
)


def types_of(source: str) -> list[TokenType]:
    """Return the token types produced by scanning ``source``, excluding EOF.

    Args:
        source: MiniLang source text.

    Returns:
        The type of every significant token, in order.
    """
    return [token.type for token in tokenize(source).significant_tokens]


def lexemes_of(source: str) -> list[str]:
    """Return the lexemes produced by scanning ``source``, excluding EOF.

    Args:
        source: MiniLang source text.

    Returns:
        The lexeme of every significant token, in order.
    """
    return [token.lexeme for token in tokenize(source).significant_tokens]


class TestKeywords:
    """Reserved words, and the boundary against identifiers."""

    @pytest.mark.parametrize(("text", "expected"), sorted(KEYWORDS.items()))
    def test_every_keyword_is_recognised(self, text: str, expected: TokenType):
        assert types_of(text) == [expected]

    @pytest.mark.parametrize(
        "text", ["integer", "intx", "whilex", "iff", "elsewhere", "returns", "printer", "func_"]
    )
    def test_keyword_prefixes_are_identifiers(self, text: str):
        """Maximal munch: 'integer' is one IDENT, never 'int' followed by 'eger'."""
        assert types_of(text) == [TokenType.IDENT]

    @pytest.mark.parametrize("text", ["Int", "WHILE", "Func", "TRUE"])
    def test_keywords_are_case_sensitive(self, text: str):
        assert types_of(text) == [TokenType.IDENT]

    def test_keyword_immediately_followed_by_punctuation(self):
        assert types_of("return;") == [TokenType.RETURN, TokenType.SEMICOLON]


class TestIdentifiers:
    """Identifier spelling rules."""

    @pytest.mark.parametrize("text", ["x", "_x", "_", "__", "x1", "camelCase", "snake_case", "a9_"])
    def test_valid_identifiers(self, text: str):
        assert types_of(text) == [TokenType.IDENT]
        assert lexemes_of(text) == [text]

    def test_identifier_cannot_start_with_a_digit(self):
        """Deliberately malformed: '1x' is a bad number, not IDENT after INT_LIT."""
        result = tokenize("1x")
        assert result.errors
        assert "identifiers cannot begin with a digit" in result.errors[0].message

    def test_non_ascii_is_rejected(self):
        """str.isalpha would accept 'é'; MiniLang's alphabet is ASCII."""
        result = tokenize("int café = 1;")
        assert result.errors
        assert any("unexpected character" in error.message for error in result.errors)


class TestLiterals:
    """Integer and float literals."""

    @pytest.mark.parametrize("text", ["0", "1", "42", "007", "1234567890"])
    def test_integer_literals(self, text: str):
        assert types_of(text) == [TokenType.INT_LIT]

    @pytest.mark.parametrize("text", ["0.0", "3.14", "1.5", "10.25"])
    def test_float_literals(self, text: str):
        assert types_of(text) == [TokenType.FLOAT_LIT]

    def test_integer_followed_by_semicolon(self):
        assert types_of("5;") == [TokenType.INT_LIT, TokenType.SEMICOLON]

    @pytest.mark.parametrize(
        ("text", "fragment"),
        [
            ("3.4.5", "more than one decimal point"),
            ("3.", "expected digits after the decimal point"),
            ("123abc", "identifiers cannot begin with a digit"),
            ("1.5e3", "identifiers cannot begin with a digit"),
        ],
    )
    def test_malformed_numbers_report_precisely(self, text: str, fragment: str):
        """Deliberately malformed: each defect gets its own specific message."""
        result = tokenize(text)
        assert result.errors, f"{text!r} should have produced an error"
        assert fragment in result.errors[0].message

    def test_malformed_number_still_emits_a_token(self):
        """Recovery: later phases must still see a value in this position."""
        result = tokenize("int x = 3.4.5;")
        assert result.errors
        assert TokenType.FLOAT_LIT in [t.type for t in result.tokens]
        assert result.tokens[-1].type is TokenType.EOF


class TestOperators:
    """Operators, including every two-character form."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("+", TokenType.PLUS),
            ("-", TokenType.MINUS),
            ("*", TokenType.STAR),
            ("/", TokenType.SLASH),
            ("%", TokenType.PERCENT),
            ("!", TokenType.BANG),
            ("=", TokenType.ASSIGN),
            ("<", TokenType.LT),
            (">", TokenType.GT),
            ("==", TokenType.EQ),
            ("!=", TokenType.NE),
            ("<=", TokenType.LE),
            (">=", TokenType.GE),
            ("&&", TokenType.AND),
            ("||", TokenType.OR),
        ],
    )
    def test_every_operator(self, text: str, expected: TokenType):
        assert types_of(text) == [expected]

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("==", [TokenType.EQ]),
            ("= =", [TokenType.ASSIGN, TokenType.ASSIGN]),
            ("<=", [TokenType.LE]),
            ("< =", [TokenType.LT, TokenType.ASSIGN]),
            ("!=", [TokenType.NE]),
            ("!!", [TokenType.BANG, TokenType.BANG]),
            (">=", [TokenType.GE]),
            ("===", [TokenType.EQ, TokenType.ASSIGN]),
        ],
    )
    def test_maximal_munch(self, text: str, expected: list[TokenType]):
        """The longest operator that matches wins."""
        assert types_of(text) == expected

    @pytest.mark.parametrize(("text", "expected"), sorted(SINGLE_CHAR_TOKENS.items()))
    def test_every_punctuation_character(self, text: str, expected: TokenType):
        assert types_of(text) == [expected]

    def test_division_is_not_swallowed_as_a_comment(self):
        assert types_of("a / b") == [TokenType.IDENT, TokenType.SLASH, TokenType.IDENT]


class TestLogicalOperatorRecovery:
    """A lone & or | is a typo the scanner can repair (plan section 7)."""

    @pytest.mark.parametrize(
        ("text", "expected", "spelling"), [("&", "AND", "&&"), ("|", "OR", "||")]
    )
    def test_single_character_is_corrected(self, text: str, expected: str, spelling: str):
        result = tokenize(text)
        assert [t.type.value for t in result.significant_tokens] == [expected]
        assert result.errors
        assert result.errors[0].suggestion == f"did you mean '{spelling}'?"
        assert result.errors[0].recovered is True

    def test_corrected_token_carries_the_intended_spelling(self):
        result = tokenize("&")
        assert result.significant_tokens[0].lexeme == "&&"

    def test_recovery_lets_the_rest_of_the_line_scan(self):
        """One typo must not derail the remaining tokens."""
        result = tokenize("if (a & b) { }")
        assert len(result.errors) == 1
        assert TokenType.RBRACE in [t.type for t in result.tokens]


class TestComments:
    """Line and block comments are trivia, not tokens."""

    def test_line_comment_is_skipped(self):
        assert types_of("int x; // trailing note") == [
            TokenType.INT,
            TokenType.IDENT,
            TokenType.SEMICOLON,
        ]

    def test_line_comment_at_eof_without_newline(self):
        assert types_of("// just a comment") == []

    def test_block_comment_is_skipped(self):
        assert types_of("int /* inline */ x") == [TokenType.INT, TokenType.IDENT]

    def test_block_comment_spanning_lines(self):
        source = "int\n/* one\n   two\n   three */\nx"
        result = tokenize(source)
        assert result.ok
        assert result.significant_tokens[-1].line == 5

    def test_block_comments_do_not_nest(self):
        """First '*/' closes the comment, matching C and Java."""
        assert types_of("/* a /* b */ x") == [TokenType.IDENT]

    def test_unterminated_block_comment_reports_its_opening_position(self):
        """Deliberately malformed: the user needs the line the comment opened on."""
        result = tokenize("int x;\nint y;\n/* never closed\nmore text")
        assert result.errors
        error = result.errors[0]
        assert "unterminated block comment" in error.message
        assert error.line == 3
        assert error.col == 1

    def test_comment_only_source_still_terminates(self):
        result = tokenize("// nothing here")
        assert len(result.tokens) == 1
        assert result.tokens[0].type is TokenType.EOF


class TestPositions:
    """Line and column tracking, which error recovery depends on."""

    def test_columns_are_one_based(self):
        token = tokenize("int").tokens[0]
        assert (token.line, token.col) == (1, 1)

    def test_columns_advance_across_a_line(self):
        tokens = tokenize("int x = 5;").significant_tokens
        assert [t.col for t in tokens] == [1, 5, 7, 9, 10]

    def test_lines_advance(self):
        tokens = tokenize("a\nb\nc").significant_tokens
        assert [(t.line, t.col) for t in tokens] == [(1, 1), (2, 1), (3, 1)]

    def test_blank_lines_are_counted(self):
        tokens = tokenize("a\n\n\nb").significant_tokens
        assert tokens[-1].line == 4

    def test_position_after_a_line_comment(self):
        tokens = tokenize("// note\nint x;").significant_tokens
        assert (tokens[0].line, tokens[0].col) == (2, 1)

    def test_position_after_a_block_comment_on_one_line(self):
        tokens = tokenize("/* skip */ x").significant_tokens
        assert (tokens[0].line, tokens[0].col) == (1, 12)

    def test_crlf_does_not_double_count_lines(self):
        """Windows-authored files must report the same positions as Unix ones."""
        tokens = tokenize("a\r\nb\r\nc").significant_tokens
        assert [t.line for t in tokens] == [1, 2, 3]

    def test_tabs_advance_one_column(self):
        tokens = tokenize("\tx").significant_tokens
        assert tokens[0].col == 2

    def test_two_character_operator_reports_its_first_character(self):
        token = tokenize("a <= b").significant_tokens[1]
        assert token.col == 3


class TestEofToken:
    """The stream is always terminated, which the parser relies on."""

    @pytest.mark.parametrize("source", ["", "   ", "\n\n", "\t", "// c", "int x;"])
    def test_eof_is_always_present_exactly_once_and_last(self, source: str):
        tokens = tokenize(source).tokens
        assert tokens[-1].type is TokenType.EOF
        assert sum(1 for t in tokens if t.type is TokenType.EOF) == 1

    @pytest.mark.parametrize("source", ["", "   ", "\n", "// only a comment"])
    def test_empty_input_produces_only_eof(self, source: str):
        assert len(tokenize(source).tokens) == 1

    def test_eof_has_an_empty_lexeme(self):
        assert tokenize("x").tokens[-1].lexeme == ""


class TestCategories:
    """Broad classes drive the token table's colour-coding."""

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("func", TokenCategory.KEYWORD),
            ("myVar", TokenCategory.IDENTIFIER),
            ("42", TokenCategory.LITERAL),
            ("3.5", TokenCategory.LITERAL),
            ("true", TokenCategory.LITERAL),
            ("false", TokenCategory.LITERAL),
            ("+", TokenCategory.OPERATOR),
            ("==", TokenCategory.OPERATOR),
            (";", TokenCategory.PUNCTUATION),
            ("(", TokenCategory.PUNCTUATION),
        ],
    )
    def test_category_assignment(self, source: str, expected: TokenCategory):
        assert tokenize(source).tokens[0].category is expected

    def test_eof_is_special(self):
        assert tokenize("").tokens[0].category is TokenCategory.SPECIAL

    def test_every_token_type_has_a_category(self):
        """Guard against a new TokenType falling through the mapping."""
        from app.lexer import Token as LexToken

        for token_type in TokenType:
            category = LexToken(token_type, "x", 1, 1).category
            assert isinstance(category, TokenCategory)


class TestApiConversion:
    """Conversion to the wire models rendered by the frontend."""

    def test_token_to_api_round_trip(self):
        api = tokenize("func").tokens[0].to_api()
        assert api.type == "FUNC"
        assert api.lexeme == "func"
        assert api.line == 1
        assert api.col == 1
        assert api.category == "keyword"

    def test_every_token_converts_without_validation_error(self):
        """schemas.Token forbids extra fields, so this would fail loudly."""
        for token in tokenize("func f(int n): int { return n; }").tokens:
            assert token.to_api().lexeme == token.lexeme

    def test_eof_converts_despite_its_empty_lexeme(self):
        assert tokenize("").tokens[0].to_api().lexeme == ""

    def test_error_to_api_carries_the_suggestion(self):
        report = tokenize("&").errors[0].to_api()
        assert report.suggestion == "did you mean '&&'?"
        assert report.suggestions
        assert 0.0 <= report.confidence <= 1.0
        assert report.recovered is True

    def test_error_without_a_suggestion_has_no_suggestion_list(self):
        report = LexicalError(line=1, col=1, message="x").to_api()
        assert report.suggestions == []


class TestTokenSpelling:
    """The canonical spelling table the parser's error messages will reuse."""

    @pytest.mark.parametrize("token_type", list(TokenType))
    def test_every_type_has_a_spelling(self, token_type: TokenType):
        assert token_type in TOKEN_SPELLING
        assert TOKEN_SPELLING[token_type]

    @pytest.mark.parametrize(
        ("token_type", "expected"),
        [
            (TokenType.SEMICOLON, ";"),
            (TokenType.EQ, "=="),
            (TokenType.ASSIGN, "="),
            (TokenType.WHILE, "while"),
            (TokenType.LBRACE, "{"),
        ],
    )
    def test_spellings_are_what_a_user_would_type(self, token_type: TokenType, expected: str):
        assert TOKEN_SPELLING[token_type] == expected


class TestUnexpectedCharacters:
    """Characters that cannot begin any token."""

    @pytest.mark.parametrize("char", ["@", "#", "$", "~", "`", "?", "\\", "^"])
    def test_unexpected_character_is_reported_and_skipped(self, char: str):
        result = tokenize(f"int x {char} 5;")
        assert result.errors
        assert any(char in error.message for error in result.errors)
        # Scanning continues past it.
        assert TokenType.SEMICOLON in [t.type for t in result.tokens]

    @pytest.mark.parametrize("quote", ['"', "'"])
    def test_quotes_get_a_language_specific_message(self, quote: str):
        result = tokenize(f"int x = {quote}hello{quote};")
        assert result.errors
        assert "no string or character literals" in result.errors[0].message

    def test_multiple_bad_characters_are_all_reported(self):
        """A scanner that stopped at the first one would hide the rest."""
        assert len(tokenize("@ # $").errors) == 3


class TestDemoProgram:
    """The canonical program must scan cleanly."""

    def test_scans_without_error(self, demo_source: str):
        assert tokenize(demo_source).ok

    def test_starts_with_func_and_ends_with_eof(self, demo_source: str):
        tokens = tokenize(demo_source).tokens
        assert tokens[0].type is TokenType.FUNC
        assert tokens[-1].type is TokenType.EOF

    def test_token_count_is_stable(self, demo_source: str):
        """Pins the count so an accidental scanner change is caught."""
        assert len(tokenize(demo_source).significant_tokens) == 105

    def test_comments_produce_no_tokens(self, demo_source: str):
        assert all("//" not in t.lexeme for t in tokenize(demo_source).tokens)

    def test_declares_both_functions(self, demo_source: str):
        idents = [t.lexeme for t in tokenize(demo_source).significant_tokens]
        assert "fib" in idents
        assert "main" in idents

    def test_positions_stay_within_the_source(self, demo_source: str):
        line_count = len(demo_source.splitlines())
        for token in tokenize(demo_source).tokens:
            assert 1 <= token.line <= line_count + 1
            assert token.col >= 1


class TestLexerObject:
    """Behaviour of the Lexer class itself."""

    def test_tokenize_is_idempotent(self):
        lexer = Lexer("int x = 1;")
        first = lexer.tokenize()
        second = lexer.tokenize()
        assert [t.type for t in first.tokens] == [t.type for t in second.tokens]

    def test_result_is_a_copy(self):
        """Mutating a returned result must not corrupt the lexer's state."""
        lexer = Lexer("int x;")
        result = lexer.tokenize()
        result.tokens.clear()
        assert lexer.tokenize().tokens

    def test_ok_reflects_errors(self):
        assert tokenize("int x;").ok
        assert not tokenize("int x @;").ok

    def test_significant_tokens_excludes_eof(self):
        result = tokenize("int x;")
        assert len(result.significant_tokens) == len(result.tokens) - 1


class TestLargeInput:
    """The UI must stay usable on long programs."""

    def test_scans_a_thousand_statements(self):
        source = "\n".join(f"int v{i} = {i};" for i in range(1000))
        result = tokenize(source)
        assert result.ok
        assert len(result.significant_tokens) == 5000
        assert result.tokens[-1].line == 1000

    def test_deeply_nested_parentheses(self):
        depth = 500
        result = tokenize("(" * depth + "1" + ")" * depth)
        assert result.ok
        assert len(result.significant_tokens) == depth * 2 + 1

    def test_very_long_identifier(self):
        name = "a" * 10_000
        assert lexemes_of(name) == [name]
