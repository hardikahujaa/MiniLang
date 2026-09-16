"""Lexical analysis: MiniLang source text to a stream of tokens.

This is phase 1 of the pipeline. It turns a flat string into a list of
:class:`Token` values, each carrying its type, its exact source text, and the
1-based line and column where it started.

Why hand-written rather than regex-driven
-----------------------------------------
A scanner built from a list of ``(regex, token_type)`` pairs is shorter, but it
is the wrong trade for this project:

1. **Positions.** Error recovery is this project's novelty feature, and it needs
   an exact line *and column* for every token. A regex alternation gives you a
   match offset; turning that back into a line/column means a second pass over
   the text. Here the position falls out of the scan for free, because
   :meth:`Lexer._advance` is the only thing that moves the cursor and it updates
   the line and column as it goes.

2. **Error messages.** A regex scanner that fails to match produces "no rule
   matched at offset 214". A hand-written one knows it was *in the middle of a
   number* and can say "invalid float literal '3.4.5': more than one decimal
   point". That difference is the whole point of the error-recovery work.

3. **Recovery.** When this scanner meets a lone ``&`` it reports the mistake,
   assumes ``&&`` was meant, and emits that token so later phases keep working.
   A regex scanner either matches or it does not.

4. **Explicability.** Every decision is a visible ``if``. There is no hidden
   backtracking to reason about in a viva.

Design notes
------------
* **Maximal munch.** The longest operator that matches wins: ``<=`` beats ``<``,
  ``==`` beats ``=``. Identifiers are scanned to their full extent before the
  keyword table is consulted, so ``integer`` lexes as one identifier and never
  as the keyword ``int`` followed by ``eger``.
* **ASCII by definition.** ``str.isalpha`` and ``str.isdigit`` are Unicode-aware,
  so they would silently accept ``é`` in an identifier and Arabic-Indic digits in
  a number. MiniLang's alphabet is ASCII, so the character classes here are
  explicit range checks rather than the standard-library predicates.
* **The scanner never raises.** Malformed input is recorded in
  :attr:`LexResult.errors` and scanning continues, because a compiler that stops
  at the first bad character cannot report the second one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Final

from app.schemas import Suggestion, SyntaxErrorReport
from app.schemas import Token as ApiToken


class TokenCategory(str, Enum):
    """Broad class of a token, used for colour-coding the token table."""

    KEYWORD = "keyword"
    IDENTIFIER = "identifier"
    LITERAL = "literal"
    OPERATOR = "operator"
    PUNCTUATION = "punctuation"
    SPECIAL = "special"


class TokenType(str, Enum):
    """Every token MiniLang can produce.

    The enum is fine-grained -- one member per keyword and per operator, rather
    than a single coarse ``KEYWORD`` member -- so the recursive descent parser
    can dispatch on ``token.type`` instead of comparing lexeme strings. It also
    makes the token table on the Lexical tab more informative to read.
    """

    # --- Keywords -------------------------------------------------------
    FUNC = "FUNC"
    INT = "INT"
    FLOAT = "FLOAT"
    BOOL = "BOOL"
    VOID = "VOID"
    IF = "IF"
    ELSE = "ELSE"
    WHILE = "WHILE"
    RETURN = "RETURN"
    PRINT = "PRINT"
    TRUE = "TRUE"
    FALSE = "FALSE"

    # --- Identifiers and literals ---------------------------------------
    IDENT = "IDENT"
    INT_LIT = "INT_LIT"
    FLOAT_LIT = "FLOAT_LIT"

    # --- Operators ------------------------------------------------------
    OR = "OR"  # ||
    AND = "AND"  # &&
    EQ = "EQ"  # ==
    NE = "NE"  # !=
    LT = "LT"  # <
    GT = "GT"  # >
    LE = "LE"  # <=
    GE = "GE"  # >=
    PLUS = "PLUS"  # +
    MINUS = "MINUS"  # -
    STAR = "STAR"  # *
    SLASH = "SLASH"  # /
    PERCENT = "PERCENT"  # %
    BANG = "BANG"  # !
    ASSIGN = "ASSIGN"  # =

    # --- Punctuation ----------------------------------------------------
    LPAREN = "LPAREN"  # (
    RPAREN = "RPAREN"  # )
    LBRACE = "LBRACE"  # {
    RBRACE = "RBRACE"  # }
    SEMICOLON = "SEMICOLON"  # ;
    COMMA = "COMMA"  # ,
    COLON = "COLON"  # :

    # --- End of input ---------------------------------------------------
    EOF = "EOF"


#: Reserved words. Consulted only after an identifier has been scanned to its
#: full extent, which is what makes ``integer`` an identifier rather than the
#: keyword ``int`` followed by ``eger``.
KEYWORDS: Final[dict[str, TokenType]] = {
    "func": TokenType.FUNC,
    "int": TokenType.INT,
    "float": TokenType.FLOAT,
    "bool": TokenType.BOOL,
    "void": TokenType.VOID,
    "if": TokenType.IF,
    "else": TokenType.ELSE,
    "while": TokenType.WHILE,
    "return": TokenType.RETURN,
    "print": TokenType.PRINT,
    "true": TokenType.TRUE,
    "false": TokenType.FALSE,
}

#: Single-character tokens with no longer form starting with the same character.
#: ``=``, ``!``, ``<``, ``>``, ``/``, ``&`` and ``|`` are deliberately absent:
#: each needs a look-ahead and is handled explicitly in :meth:`Lexer._scan_token`.
SINGLE_CHAR_TOKENS: Final[dict[str, TokenType]] = {
    "+": TokenType.PLUS,
    "-": TokenType.MINUS,
    "*": TokenType.STAR,
    "%": TokenType.PERCENT,
    "(": TokenType.LPAREN,
    ")": TokenType.RPAREN,
    "{": TokenType.LBRACE,
    "}": TokenType.RBRACE,
    ";": TokenType.SEMICOLON,
    ",": TokenType.COMMA,
    ":": TokenType.COLON,
}

#: Canonical source spelling of every fixed-text token.
#:
#: The parser and the error-recovery engine need this to render expected-token
#: sets in terms a user recognises -- "expected ';'" rather than "expected
#: SEMICOLON". Defined here because this module is where the spelling is
#: authoritative.
TOKEN_SPELLING: Final[dict[TokenType, str]] = {
    **{token_type: text for text, token_type in KEYWORDS.items()},
    **{token_type: text for text, token_type in SINGLE_CHAR_TOKENS.items()},
    TokenType.OR: "||",
    TokenType.AND: "&&",
    TokenType.EQ: "==",
    TokenType.NE: "!=",
    TokenType.LE: "<=",
    TokenType.GE: ">=",
    TokenType.LT: "<",
    TokenType.GT: ">",
    TokenType.BANG: "!",
    TokenType.ASSIGN: "=",
    TokenType.SLASH: "/",
    TokenType.IDENT: "identifier",
    TokenType.INT_LIT: "integer literal",
    TokenType.FLOAT_LIT: "float literal",
    TokenType.EOF: "end of file",
}

_KEYWORD_TYPES: Final[frozenset[TokenType]] = frozenset(KEYWORDS.values())
_LITERAL_TYPES: Final[frozenset[TokenType]] = frozenset(
    {TokenType.INT_LIT, TokenType.FLOAT_LIT, TokenType.TRUE, TokenType.FALSE}
)
_PUNCTUATION_TYPES: Final[frozenset[TokenType]] = frozenset(
    {
        TokenType.LPAREN,
        TokenType.RPAREN,
        TokenType.LBRACE,
        TokenType.RBRACE,
        TokenType.SEMICOLON,
        TokenType.COMMA,
        TokenType.COLON,
    }
)


def _is_digit(char: str) -> bool:
    """Return whether ``char`` is an ASCII digit.

    Deliberately not ``str.isdigit``, which is Unicode-aware and would accept
    characters such as the Arabic-Indic digit U+0660 that MiniLang's grammar
    does not define.

    Args:
        char: A single character.

    Returns:
        True for ``0``-``9`` only.
    """
    return "0" <= char <= "9"


def _is_ident_start(char: str) -> bool:
    """Return whether ``char`` may begin an identifier.

    Args:
        char: A single character.

    Returns:
        True for an ASCII letter or an underscore.
    """
    return ("a" <= char <= "z") or ("A" <= char <= "Z") or char == "_"


def _is_ident_part(char: str) -> bool:
    """Return whether ``char`` may continue an identifier.

    Args:
        char: A single character.

    Returns:
        True for an ASCII letter, an ASCII digit, or an underscore.
    """
    return _is_ident_start(char) or _is_digit(char)


@dataclass(frozen=True, slots=True)
class Token:
    """A single lexical token.

    Attributes:
        type: The token's fine-grained class.
        lexeme: The exact source text the token was matched from. For a token
            the scanner supplied during error recovery this is the *corrected*
            text, so it can differ from what was written.
        line: 1-based line of the token's first character.
        col: 1-based column of the token's first character.
    """

    type: TokenType
    lexeme: str
    line: int
    col: int

    @property
    def category(self) -> TokenCategory:
        """Return the broad class this token belongs to.

        Returns:
            The :class:`TokenCategory` used to colour the token table.
        """
        if self.type is TokenType.EOF:
            return TokenCategory.SPECIAL
        if self.type in _LITERAL_TYPES:
            return TokenCategory.LITERAL
        if self.type in _KEYWORD_TYPES:
            return TokenCategory.KEYWORD
        if self.type is TokenType.IDENT:
            return TokenCategory.IDENTIFIER
        if self.type in _PUNCTUATION_TYPES:
            return TokenCategory.PUNCTUATION
        return TokenCategory.OPERATOR

    def to_api(self) -> ApiToken:
        """Convert to the wire model rendered by the Lexical tab.

        Returns:
            An :class:`~app.schemas.Token` carrying the same data.
        """
        return ApiToken(
            type=self.type.value,
            lexeme=self.lexeme,
            line=self.line,
            col=self.col,
            category=self.category.value,
        )

    def __str__(self) -> str:
        """Return a compact ``TYPE('lexeme')@line:col`` form for test output."""
        return f"{self.type.value}({self.lexeme!r})@{self.line}:{self.col}"


@dataclass(frozen=True, slots=True)
class LexicalError:
    """A problem found while scanning, with an optional suggested repair.

    Attributes:
        line: 1-based line where the problem starts.
        col: 1-based column where the problem starts.
        message: What went wrong, in user-facing terms.
        lexeme: The offending source text, when there is one.
        suggestion: A proposed repair, when the scanner can infer one.
        confidence: How strongly the suggestion is believed, in [0, 1].
        recovered: Whether the scanner supplied a replacement token and
            continued, as opposed to discarding the input.
    """

    line: int
    col: int
    message: str
    lexeme: str | None = None
    suggestion: str | None = None
    confidence: float = 0.0
    recovered: bool = False

    def to_api(self) -> SyntaxErrorReport:
        """Convert to the wire model rendered in the Source tab's error list.

        Returns:
            A :class:`~app.schemas.SyntaxErrorReport` for this error.
        """
        suggestions = (
            [Suggestion(text=self.suggestion, confidence=self.confidence, rationale="lexical")]
            if self.suggestion
            else []
        )
        return SyntaxErrorReport(
            line=self.line,
            col=self.col,
            message=self.message,
            found=self.lexeme,
            suggestion=self.suggestion,
            confidence=self.confidence,
            suggestions=suggestions,
            recovered=self.recovered,
        )


@dataclass(slots=True)
class LexResult:
    """Everything one scan produced.

    Attributes:
        tokens: The token stream, always terminated by a single ``EOF`` token.
        errors: Lexical errors, in source order. Empty for valid input.
    """

    tokens: list[Token] = field(default_factory=list)
    errors: list[LexicalError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Return whether the scan completed without any lexical error."""
        return not self.errors

    @property
    def significant_tokens(self) -> list[Token]:
        """Return the token stream without its terminating ``EOF``.

        Returns:
            Every token except the final end-of-input marker.
        """
        return [token for token in self.tokens if token.type is not TokenType.EOF]


class Lexer:
    """A single-pass, hand-written scanner for MiniLang.

    One instance scans one source string. :meth:`tokenize` is idempotent: it
    scans on the first call and returns the cached result afterwards.

    Example:
        >>> Lexer("int x = 5;").tokenize().tokens[0].type
        <TokenType.INT: 'INT'>
    """

    def __init__(self, source: str) -> None:
        """Prepare to scan ``source``.

        Args:
            source: MiniLang source text.
        """
        self._source = source
        self._pos = 0
        self._line = 1
        self._col = 1
        self._start = 0
        self._start_line = 1
        self._start_col = 1
        self._tokens: list[Token] = []
        self._errors: list[LexicalError] = []
        self._done = False

    # -- Public API ------------------------------------------------------

    def tokenize(self) -> LexResult:
        """Scan the whole source and return the token stream and any errors.

        Never raises on malformed input: problems are collected into
        :attr:`LexResult.errors` and scanning continues, so one bad character
        does not hide every later one.

        Returns:
            A :class:`LexResult` whose token list always ends with ``EOF``.
        """
        if self._done:
            return LexResult(tokens=list(self._tokens), errors=list(self._errors))

        while True:
            self._skip_trivia()
            if self._at_end():
                break
            self._start = self._pos
            self._start_line = self._line
            self._start_col = self._col
            self._scan_token()

        self._tokens.append(Token(TokenType.EOF, "", self._line, self._col))
        self._done = True
        return LexResult(tokens=list(self._tokens), errors=list(self._errors))

    # -- Cursor ----------------------------------------------------------

    def _at_end(self) -> bool:
        """Return whether the cursor has consumed the whole source."""
        return self._pos >= len(self._source)

    def _peek(self) -> str:
        """Return the current character without consuming it, or ``""`` at end."""
        if self._at_end():
            return ""
        return self._source[self._pos]

    def _peek_next(self) -> str:
        """Return the character after the cursor, or ``""`` if there is none."""
        if self._pos + 1 >= len(self._source):
            return ""
        return self._source[self._pos + 1]

    def _advance(self) -> str:
        """Consume and return the current character, tracking line and column.

        This is the only method that moves the cursor, which is what keeps the
        reported positions correct for free.

        Returns:
            The consumed character.
        """
        char = self._source[self._pos]
        self._pos += 1
        if char == "\n":
            self._line += 1
            self._col = 1
        else:
            self._col += 1
        return char

    def _match(self, expected: str) -> bool:
        """Consume the current character only if it equals ``expected``.

        Args:
            expected: The character to look for.

        Returns:
            True if the character matched and was consumed.
        """
        if self._peek() != expected:
            return False
        self._advance()
        return True

    # -- Emitting --------------------------------------------------------

    def _add(self, token_type: TokenType, lexeme: str | None = None) -> None:
        """Append a token starting at the current token's recorded position.

        Args:
            token_type: The token's class.
            lexeme: Source text to record. Defaults to the text actually
                consumed; pass it explicitly when recovery substitutes a
                corrected spelling.
        """
        text = self._source[self._start : self._pos] if lexeme is None else lexeme
        self._tokens.append(Token(token_type, text, self._start_line, self._start_col))

    def _error(
        self,
        message: str,
        *,
        line: int | None = None,
        col: int | None = None,
        lexeme: str | None = None,
        suggestion: str | None = None,
        confidence: float = 0.0,
        recovered: bool = False,
    ) -> None:
        """Record a lexical error at the current token's position.

        Args:
            message: User-facing description of the problem.
            line: Override for the reported line. Defaults to the token start.
            col: Override for the reported column. Defaults to the token start.
            lexeme: The offending text, when there is one.
            suggestion: A proposed repair, when one can be inferred.
            confidence: Belief in the suggestion, in [0, 1].
            recovered: Whether a replacement token was emitted.
        """
        self._errors.append(
            LexicalError(
                line=self._start_line if line is None else line,
                col=self._start_col if col is None else col,
                message=message,
                lexeme=lexeme,
                suggestion=suggestion,
                confidence=confidence,
                recovered=recovered,
            )
        )

    # -- Trivia ----------------------------------------------------------

    def _skip_trivia(self) -> None:
        """Consume whitespace and comments until the next real token.

        A ``/`` is only the start of a comment when followed by ``/`` or ``*``;
        otherwise it is the division operator and is left for the scanner.
        """
        while not self._at_end():
            char = self._peek()
            if char in " \t\r\n":
                self._advance()
            elif char == "/" and self._peek_next() == "/":
                self._skip_line_comment()
            elif char == "/" and self._peek_next() == "*":
                self._skip_block_comment()
            else:
                return

    def _skip_line_comment(self) -> None:
        """Consume a ``//`` comment up to, but not including, the newline.

        Leaving the newline for :meth:`_skip_trivia` keeps line counting in one
        place. A comment that runs to end-of-input is not an error.
        """
        while not self._at_end() and self._peek() != "\n":
            self._advance()

    def _skip_block_comment(self) -> None:
        """Consume a ``/* ... */`` comment.

        Block comments do not nest: the first ``*/`` closes the comment,
        matching C and Java. Reaching end-of-input first is reported as an
        error against the position where the comment opened, which is the line
        the user needs to look at.
        """
        open_line, open_col = self._line, self._col
        self._advance()  # consume '/'
        self._advance()  # consume '*'
        while not self._at_end():
            if self._peek() == "*" and self._peek_next() == "/":
                self._advance()
                self._advance()
                return
            self._advance()
        self._error(
            "unterminated block comment: reached end of file before '*/'",
            line=open_line,
            col=open_col,
            lexeme="/*",
            suggestion="close the comment with '*/'",
            confidence=0.95,
        )

    # -- Token scanning --------------------------------------------------

    def _scan_token(self) -> None:
        """Scan exactly one token, starting at the recorded token position."""
        char = self._advance()

        if _is_ident_start(char):
            self._identifier()
            return
        if _is_digit(char):
            self._number()
            return

        simple = SINGLE_CHAR_TOKENS.get(char)
        if simple is not None:
            self._add(simple)
            return

        if char == "=":
            self._add(TokenType.EQ if self._match("=") else TokenType.ASSIGN)
            return
        if char == "!":
            self._add(TokenType.NE if self._match("=") else TokenType.BANG)
            return
        if char == "<":
            self._add(TokenType.LE if self._match("=") else TokenType.LT)
            return
        if char == ">":
            self._add(TokenType.GE if self._match("=") else TokenType.GT)
            return
        if char == "/":
            # Comments were already consumed by _skip_trivia, so this is division.
            self._add(TokenType.SLASH)
            return
        if char in "&|":
            self._logical_operator(char)
            return

        self._unexpected_character(char)

    def _identifier(self) -> None:
        """Scan an identifier or keyword.

        The run of identifier characters is consumed to its full extent *before*
        the keyword table is consulted (maximal munch), so ``integer`` lexes as
        one identifier rather than the keyword ``int`` followed by ``eger``.
        """
        while _is_ident_part(self._peek()):
            self._advance()
        lexeme = self._source[self._start : self._pos]
        self._add(KEYWORDS.get(lexeme, TokenType.IDENT))

    def _number(self) -> None:
        """Scan a numeric literal, reporting malformed ones precisely.

        The full run of digits, dots and identifier characters is consumed
        first, then classified. Consuming greedily is what lets ``3.4.5`` and
        ``123abc`` be reported as one specific error each, instead of
        decomposing into a valid token followed by confusing debris.
        """
        while _is_ident_part(self._peek()) or self._peek() == ".":
            self._advance()

        lexeme = self._source[self._start : self._pos]
        classified = _classify_number(lexeme)

        if classified is not None:
            self._add(classified)
            return

        # Malformed. Emit the closest plausible token anyway so that later
        # phases still see a value here and can keep going.
        self._error(
            _describe_bad_number(lexeme),
            lexeme=lexeme,
            suggestion=_suggest_for_bad_number(lexeme),
            confidence=0.6,
            recovered=True,
        )
        self._add(TokenType.FLOAT_LIT if "." in lexeme else TokenType.INT_LIT)

    def _logical_operator(self, char: str) -> None:
        """Scan ``&&`` or ``||``, recovering from a single ``&`` or ``|``.

        MiniLang has no bitwise operators, so a lone ``&`` is unambiguously a
        typo for ``&&``. The scanner says so and emits the intended token, which
        keeps the parse going instead of derailing every later phase.

        Args:
            char: The character already consumed, either ``&`` or ``|``.
        """
        doubled = TokenType.AND if char == "&" else TokenType.OR
        spelling = char * 2

        if self._match(char):
            self._add(doubled)
            return

        self._error(
            f"unexpected '{char}': MiniLang has no bitwise operators",
            lexeme=char,
            suggestion=f"did you mean '{spelling}'?",
            confidence=0.9,
            recovered=True,
        )
        self._add(doubled, lexeme=spelling)

    def _unexpected_character(self, char: str) -> None:
        """Report a character that cannot begin any token, and skip it.

        No token is emitted: unlike a lone ``&``, there is nothing sensible to
        substitute, and inventing one would mislead later phases.

        Args:
            char: The offending character, already consumed.
        """
        if char in "\"'":
            message = f"unexpected {char!r}: MiniLang has no string or character literals"
            suggestion = "remove the quoted text"
        else:
            message = f"unexpected character {char!r}"
            suggestion = f"remove the {char!r}"
        self._error(message, lexeme=char, suggestion=suggestion, confidence=0.5)


def _classify_number(lexeme: str) -> TokenType | None:
    """Classify a scanned numeric run.

    Implemented with explicit character checks rather than a regular
    expression, so that the scanner contains no regex at all and its behaviour
    can be read straight off the code.

    Args:
        lexeme: The consumed run of digits, dots and identifier characters.

    Returns:
        ``INT_LIT`` for digits only, ``FLOAT_LIT`` for ``digits.digits``, or
        ``None`` if the run is not a well-formed MiniLang literal.
    """
    if not lexeme:
        return None
    if all(_is_digit(char) for char in lexeme):
        return TokenType.INT_LIT
    if lexeme.count(".") == 1:
        whole, _, fraction = lexeme.partition(".")
        if whole and fraction and all(_is_digit(c) for c in whole + fraction):
            return TokenType.FLOAT_LIT
    return None


def _describe_bad_number(lexeme: str) -> str:
    """Explain why a numeric run is not a valid literal.

    Args:
        lexeme: The malformed run.

    Returns:
        A specific, user-facing message naming the actual defect.
    """
    if lexeme.count(".") > 1:
        return f"invalid float literal {lexeme!r}: more than one decimal point"
    if lexeme.endswith("."):
        return f"invalid float literal {lexeme!r}: expected digits after the decimal point"
    if any(_is_ident_start(char) for char in lexeme):
        return f"invalid number literal {lexeme!r}: identifiers cannot begin with a digit"
    return f"invalid number literal {lexeme!r}"


def _suggest_for_bad_number(lexeme: str) -> str:
    """Propose a repair for a malformed numeric literal.

    Args:
        lexeme: The malformed run.

    Returns:
        A short, actionable suggestion.
    """
    if lexeme.count(".") > 1:
        whole, _, rest = lexeme.partition(".")
        return f"did you mean '{whole}.{rest.replace('.', '')}'?"
    if lexeme.endswith("."):
        return f"did you mean '{lexeme}0'?"
    return "rename the identifier so it does not start with a digit"


def tokenize(source: str) -> LexResult:
    """Scan MiniLang source into tokens.

    The module's entry point; prefer this over constructing a :class:`Lexer`
    directly.

    Args:
        source: MiniLang source text.

    Returns:
        A :class:`LexResult` holding the token stream and any lexical errors.
    """
    return Lexer(source).tokenize()
