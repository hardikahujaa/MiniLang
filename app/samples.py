"""Canonical MiniLang sample programs.

The plan (section 2) specifies one canonical demo program that is used in every
screenshot, in the report, and in the viva, because it exercises every phase and
every optimisation:

* ``while`` loop and function call  -> control flow, CFG with a back edge
* ``5 * 2``                         -> constant folding
* ``int unused = 42;``              -> dead code elimination
* ``(3 + 4) * (3 + 4)``             -> common subexpression elimination

Keeping it here as a constant (rather than reading a file at request time) means
the ``GET /api/demo`` endpoint works no matter what directory the server was
started from. ``tests/programs/demo.ml`` holds a byte-identical copy, and
``tests/test_samples.py`` asserts the two never drift apart.
"""

from __future__ import annotations

from typing import Final

DEMO_PROGRAM: Final[str] = """func fib(int n): int {
    int a = 0;
    int b = 1;
    int i = 0;
    while (i < n) {
        int t = a + b;
        a = b;
        b = t;
        i = i + 1;
    }
    return a;
}

func main(): void {
    int limit = 5 * 2;         // folds to 10
    int unused = 42;           // eliminated by DCE
    int x = (3 + 4) * (3 + 4); // CSE target
    print(fib(limit));
    print(x);
}
"""

#: Default grammar preloaded into the Parser Theory Lab tab: the classic
#: expression grammar, which is left-recursive (so not LL(1)) but SLR(1).
#: Pasting it demonstrates both table generators and the LL(1) conflict report.
DEMO_GRAMMAR: Final[str] = """E -> E + T | T
T -> T * F | F
F -> ( E ) | id
"""

#: A deliberately ambiguous grammar, used on demo day to make a shift/reduce
#: conflict light up red in the SLR table (plan section 9, step 4).
AMBIGUOUS_GRAMMAR: Final[str] = """E -> E + E | E * E | ( E ) | id
"""

#: MiniLang's own grammar (plan section 2), in the LL(1)-ready form
#: :mod:`app.table_parser` builds a predictive-parsing table from.
#:
#: Two textbook transformations on top of the plan's grammar, both load-
#: bearing: expressions are already left-recursion-eliminated there (the
#: ``E -> T Ep`` style shown in Report 1); what this constant adds is
#: **left factoring** (Aho, Lam, Sethi & Ullman, section 4.3.4) for ``stmt``,
#: whose ``assign`` and ``exprStmt`` alternatives both start with an
#: identifier in the plan's grammar -- the real recursive-descent parser
#: resolves that with two tokens of lookahead (see ``Parser._statement``'s
#: own docstring), which a genuinely single-token-lookahead table-driven
#: parser cannot do. ``stmt -> ident stmtIdTail`` factors the shared prefix
#: out; ``unaryNoId``/``primaryNoId`` then restrict ``exprStmt``'s own entry
#: point so it never reintroduces ``ident`` into ``FIRST(stmt)`` a second
#: time. Every terminal is spelled as ``TokenType.X.value.lower()`` (e.g.
#: ``IDENT`` -> ``ident``), except ``app.lexer``'s ``EOF`` token, which this
#: grammar never names directly -- the end marker is implicit, exactly as
#: section 4.4.2 treats ``$``. Confirmed conflict-free LL(1) by
#: ``tests/test_grammar.py::TestLl1Table::test_minilang_full_grammar_is_ll1``.
MINILANG_GRAMMAR: Final[str] = """program      -> funcDecl programTail
programTail  -> funcDecl programTail | eps

funcDecl     -> func ident lparen paramsOpt rparen colon type block
paramsOpt    -> params | eps
params       -> type ident paramsTail
paramsTail   -> comma type ident paramsTail | eps
type         -> int | float | bool | void

block        -> lbrace stmtStar rbrace
stmtStar     -> stmt stmtStar | eps

stmt         -> varDecl
             | ident stmtIdTail
             | ifStmt
             | whileStmt
             | returnStmt
             | printStmt
             | exprStmt

varDecl      -> type ident varDeclInit semicolon
varDeclInit  -> assign expr | eps

stmtIdTail   -> assign expr semicolon
             | primaryTail factorTail termTail cmpTail eqTail andTail orTail semicolon

ifStmt       -> if lparen expr rparen block ifTail
ifTail       -> else block | eps
whileStmt    -> while lparen expr rparen block
returnStmt   -> return returnValue semicolon
returnValue  -> expr | eps
printStmt    -> print lparen expr rparen semicolon
exprStmt     -> unaryNoId factorTail termTail cmpTail eqTail andTail orTail semicolon

expr         -> logicOr
logicOr      -> logicAnd orTail
orTail       -> or logicAnd orTail | eps
logicAnd     -> equality andTail
andTail      -> and equality andTail | eps
equality     -> compare eqTail
eqTail       -> eq compare eqTail | ne compare eqTail | eps
compare      -> term cmpTail
cmpTail      -> lt term cmpTail | gt term cmpTail | le term cmpTail | ge term cmpTail | eps
term         -> factor termTail
termTail     -> plus factor termTail | minus factor termTail | eps
factor       -> unary factorTail
factorTail   -> star unary factorTail | slash unary factorTail | percent unary factorTail | eps
unary        -> bang unary | minus unary | primary
primary      -> int_lit | float_lit | true | false | ident primaryTail | lparen expr rparen
primaryTail  -> lparen callArgs rparen | eps
callArgs     -> expr callArgsTail | eps
callArgsTail -> comma expr callArgsTail | eps

unaryNoId    -> bang unary | minus unary | primaryNoId
primaryNoId  -> int_lit | float_lit | true | false | lparen expr rparen
"""
