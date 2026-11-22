# Corpora

Six small real projects, one per indexed language, kept out of the tree in
`~/proj/ken-corpora`. The point is not a demo: each one is small enough to
re-index in seconds and awkward enough to break the parser. Numbers below are
the ones to move when a parsing or resolution rule changes.

| corpus | lang | pin | files | loc | entities | refs |
| --- | --- | --- | --- | --- | --- | --- |
| nlohmann/json | C++ | `098eebd` | 665 | 240,551 | 12,632 | 92,052 |
| google/gson | Java | `e5f02ab` | 268 | 51,846 | 4,304 | 24,310 |
| BurntSushi/memchr | Rust | `bd6068c` | 64 | 62,902 | 4,251 | 18,383 |
| pallets/click | Python | `08b803c` | 91 | 24,918 | 2,093 | 7,272 |
| spf13/pflag | Go | `7b3c6eb` | 76 | 11,883 | 1,055 | 4,706 |
| DaveGamble/cJSON | C | `6d9f244` | 101 | 19,286 | 2,058 | 4,489 |
| **all six** | | | **1,265** | **411,386** | **26,393** | **151,212** |

`files` counts every file the indexer read in that directory, not just the
headline language: nlohmann/json ships 29 Python files and 2 C files, cJSON
ships 2 Python files, and those are indexed too.

Why these six: cJSON is 3 C files plus a vendored test framework, so the
header/definition split and the macro layer are the whole program. nlohmann is
one header-only library plus a test suite written entirely in macros, which is
the hardest thing a C++ indexer built on a tree-sitter grammar can be asked to
read. click is a decorator-heavy Python package: functions made by decorators,
imports resolved across a package tree. gson is plain Java with interfaces,
generics and a deep package hierarchy. memchr is three tight generic
implementations of the same trait method, which is what makes `self.method()`
and turbofish `memchr::<u8>(...)` call sites worth having. pflag is a Go
command-line library whose whole surface is methods on structs plus a large
block of `const ( ... )`, which is where the Go rules fall over.

## Reproduce

```sh
git clone https://github.com/DaveGamble/cJSON      ~/proj/ken-corpora/DaveGamble-cJSON
git clone https://github.com/nlohmann/json         ~/proj/ken-corpora/nlohmann-json
git clone https://github.com/pallets/click         ~/proj/ken-corpora/pallets-click
git clone https://github.com/google/gson           ~/proj/ken-corpora/google-gson
git clone https://github.com/BurntSushi/memchr     ~/proj/ken-corpora/BurntSushi-memchr
git clone https://github.com/spf13/pflag           ~/proj/ken-corpora/spf13-pflag

cd ~/proj/ken-corpora/DaveGamble-cJSON && git checkout 6d9f244    # and so on, per the table

for d in ~/proj/ken-corpora/*; do
    ken --db /tmp/ken-$(basename $d).db index $d
    python scripts/corpus_stats.py /tmp/ken-$(basename $d).db
done
```

**One database per corpus, not one database holding all six.** Candidate sets are
built from the whole index, so a shared database lets one project resolve to
another's symbols: indexed together, 12,108 `CHECK` calls in nlohmann's test
suite resolve to the `CHECK` macro in cJSON's vendored Unity, and the C++ count
above moves by thirteen points. That is a defect, not a feature, and it is worth
fixing at the root (scope a reference's candidates to the index root it came
from) before publishing any multi-project index. Per-corpus indexing keeps the
measurement honest in the meantime.

`scripts/corpus_stats.py` prints, per index: files, lines, entity and ref
counts, the resolution mix with the three ladder rungs called out separately,
the top entity kinds, the ambiguous refs by kind, and the largest files that
produced no entity at all. That last list is the cheapest parser-defect detector
available: a file with code in it and nothing in the index is either a grammar
gap or a construct the SPEC deliberately ignores, and both are worth knowing
about.

## Resolution, as measured

```
corpus            one definition   one site   of in-tree refs   ambiguous   ambiguous%
cJSON                   82.0%        84.3%          88.8%            0         0.0%
click                   41.9%        43.3%          58.5%          526         7.2%
memchr                  28.8%        31.4%          60.2%        3,038        16.5%
nlohmann/json           26.7%        38.3%          69.3%       11,769        12.8%
gson                    26.5%        27.7%          57.5%          456         1.9%
pflag                   22.9%        22.9%          44.9%          980        20.8%
```

Two rates, because they answer different questions. **One definition** is the
fraction of references that name exactly one place (resolution `exact`, `scoped`
or `unique`): the reader can jump to a body. **One site** adds `overload`, where
a call names one qualified name but several bodies, so the call site is known
and the body is chosen by arity or type at runtime. An overload set locates the
site, not the body, and collapsing the two would let a header-only library look
better than it is.

`external` is a reference to a name no entity in the corpus carries: libc and
the C standard headers for cJSON (504), the JDK and its collections for gson
(10,320), the Python builtins and stdlib for click (3,018), Rust core and libc
for memchr (7,322), the Go runtime and stdlib for pflag (2,591), and for
nlohmann (28,268) mostly the doctest assertion macros, which the index does not
see because they are vendored in `tests/thirdparty`. `unresolved` is refs with a
name in the tree but no candidate the parser could attach: 2,521 in nlohmann, 30
in memchr, 53 in pflag, 16 in cJSON, zero in click and gson.

`preprocessor` (3,278 nlohmann, 665 memchr, 184 cJSON) is a call to a name the
corpus defines as a function-like macro. It is its own class because the right
answer is "this is the macro `GCC_ATOMIC_...`", not "this is a function", and
the two must not be averaged together in a rate.

`dispatch` (10,931 nlohmann, 6,802 gson, 1,547 memchr, 580 click) is the honest
miss: the callee is in the tree but its owner is a receiver whose type is not
inferred, so no lexical scope reaches it. In UML terms it is a call to an
operation on a type the index cannot name. It is not `ambiguous` (many
candidates, any of which could be right) and it is not `external` (the name is
here).

**These percentages use every reference as the denominator, and the previous
table on this page did not.** That version joined `refs → entities → files` and
so silently dropped the 54,032 references (36%) whose owning function is not an
entity, almost all of them in nlohmann's test suite, where a whole test case
lives inside a `TEST_CASE(...)` macro body and no C++ function owns it. On the
old basis (refs with an owning entity) C++ one-definition was 39.9% and is now
36.8%; the three-point fall is the in-scope gate described below, which stopped
bare member names from claiming a same-named global. Nothing here is a
before/after claim.

## What each corpus broke

- **Header prototypes were invisible (cJSON).** `int api_open(const char *);`
  is a `declaration` node, not a `function_definition`, so the C tree produced
  no entity for the API's own signatures and every call in the `.c` file had
  nothing to resolve to. Such declarations now index as `function` with
  `decl=1`, provided they sit at a declaration scope: in C++ a *statement* such
  as `CHECK(j == Json::object());` is a syntactically valid function
  declaration, so "it parses as a declaration" is not evidence that it is one.
- **Macro calls impersonated declarations (nlohmann).** The test suite is
  `TEST_CASE("...") { SECTION("...") { CHECK(...); } }`. Each of those reads as
  a function or member declaration, so the index filled with entities named
  after assertion macros, one per test case. Two rules now stop it: a callable
  whose declarator subtree contains a tree-sitter `ERROR` node is rejected, and
  `store.prune_macro_phantoms()` drops any entity whose name the corpus also
  defines as a function-like macro, re-parenting the survivors. 42 of the
  12,674 entities the C++ parser produces are pruned this way (118 across the
  six corpora).
- **typedef and using were dropped.** `json::value_t`, `api_id` are referenced
  everywhere and defined nowhere, so those references had no target. C
  `type_definition` and C++ `alias_declaration` now index as `kind="alias"`
  (37 in cJSON, 1,716 in nlohmann).
- **Function-like macros had no identity.** `API_MAX(a, b)` and
  `NLOHMANN_DEFINE_TYPE_INTRUSIVE(...)` are call expressions over a macro
  name; without a `macro` entity (C `preproc_function_def`) the call site can
  only ever be external. 656 macro entities in cJSON, 1,224 in nlohmann.
- **Module names were matched too shallowly (click, gson).** `import
  com.google.gson.internal.bind.TypeAdapters;` has six segments; the probe
  stopped at four. It now walks up to six, and also tries `.java` and
  `/__init__.py`, which is what makes a Python package resolve to a directory.
- **`self.method()` lost its owner (memchr).** The Rust grammar puts the
  receiver in a separate node from the call, so a `self.encode(...)` inside
  `impl Trait for X` came out as a bare `encode` with no owner and no scope. The
  receiver is now read from the call node and prepended to the target, and
  `memchr::<u8>(...)` loses its turbofish before the name is compared, so
  `Vec::with_capacity` no longer becomes `Vec::::with_capacity`.
- **A root-qualified path was split as text (nlohmann).** `::nlohmann::from_json(1)`
  parses as a nested `qualified_identifier` with the whole path in its `name`
  field and no `scope` field. Splitting the text produced `('::nlohmann',
  'nlohmann::from_json')`, and the caller then wrote the prefix and the name
  together, spelling the head twice: `nlohmann::nlohmann::from_json`. `name_of()`
  now recurses into the nested node, and `Builder.qual()` refuses to re-prefix a
  name that starts with `::`.
- **A bare member name claimed a global (all).** `size` inside a class resolved
  to any `size` in the tree with a matching qualified name, because the suffix
  match did not check that the candidate lived in an enclosing scope. The suffix
  fallback is now gated on the head of the target being in scope, which moves
  refs out of `exact` and into `ambiguous` or `dispatch`. Part of what reads as
  a C++ exact-rate fall is this gate working.

## Where it still fails

- **C++ member calls: 11,769 ambiguous (12.8%) plus 10,931 `dispatch` (11.9%).**
  A member call is indexed as the bare member name, and `get`, `size`, `begin`,
  `push_back` are each defined several times across a header-only library.
  Choosing between them needs the receiver's type, which is exactly the clang
  dependency this tool exists to avoid. The include closure does not rescue
  it: 3,216 of 3,773 include edges in nlohmann stay unresolved, so most
  `#include` lines cannot constrain anything either. Measured on the ambiguous
  set before the ladder existed: 51% has no declaration reachable by any
  lexical scope, and 78% of targets have six or more same-named candidates.
- **Go is the weakest language in the table (22.9% one definition, 20.8%
  ambiguous).** Two reasons, both measured. Block constants (`const ( A = 1;
  B = 2 )`) are not extracted at all, because the Go grammar nests them in a
  `const_declaration` with several `const_spec` children and only the
  single-spec form is handled. And pflag's API is almost entirely interface
  method calls on `*FlagSet`, so most of the 2,591 externals and 980 ambiguous
  refs are receiver-typed calls the index cannot type.
- **50 of 665 nlohmann files index zero entities (8%)**, the largest
  `tests/src/unit-msgpack.cpp` at 2,092 lines. Their whole content is
  `TEST_CASE("...") { ... }` with lambdas inside: the file is a macro argument,
  so there is no function to index. Minting an entity from the string literal
  would invent a name the code does not spell, so the file honestly shows up
  empty. The same shape is why 54,032 references have no owning entity.
- **18 of 64 memchr files index zero entities (28%)**, over the 5% ship gate.
  15 of those are legitimate facades and re-export stubs (`src/lib.rs` and
  friends) whose content is `pub use`. The remaining three are `mod` blocks
  holding only `cfg`-gated items, which the Rust record does not descend into;
  the largest is 176 lines.
- **8 gson files index zero entities** and all of them are `module-info.java`
  and `package-info.java`. Correct, but the front end should say so rather than
  listing them as ordinary empty files.
- **8 click files index zero entities**, including `docs/conf.py` at 53 lines.
  Sphinx builds a globals dict and never defines anything a tree-sitter Python
  grammar classifies as an entity; whether that is right is open.
- **`ambiguous` is visible in the front end now, but only as a colour.** The
  symbol view marks the nine classes; there is no per-reference drill-down
  listing the candidates, which is the next thing a reader asks for.

## Next

JavaScript. express is cloned at `43fca52` (6 files in `lib/`, 2,765 loc) and is
the next one to wire: it is the largest remaining gap, since it is the language
most people point at a codebase with, and `.js` is currently the single biggest
entry in the skip tally (151 files across the corpora, ahead of `.ejs` 21 and
`.rb` 16).

Pointing `ken` at a directory of an unwired language still says `indexed 0
files` and, on stderr, `skipped N files in languages this build does not index:
.js N`, which is the gap stated instead of implied. See `docs/languages.md` for
the plan and the per-language rules.
