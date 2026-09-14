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
