# Corpora

Four small real projects, one per supported language, kept out of the tree in
`~/proj/ken-corpora`. The point is not a demo: each one is small enough to
re-index in seconds and awkward enough to break the parser. Numbers below are
the ones to move when a parsing or resolution rule changes.

| corpus | lang | pin | files | loc | entities | refs | index |
| --- | --- | --- | --- | --- | --- | --- | --- |
| DaveGamble/cJSON | C | `6d9f244` | 101 | 19,286 | 2,058 | 4,489 | 0.34 s |
| nlohmann/json | C++ | `098eebd` | 664 | 239,623 | 12,558 | 91,571 | 5.19 s |
| pallets/click | Python | `08b803c` | 91 | 24,918 | 2,093 | 7,272 | 0.49 s |
| google/gson | Java | `e5f02ab` | 268 | 51,846 | 4,304 | 24,310 | 0.91 s |

Why these four: cJSON is 3 C files plus a vendored test framework, so the
header/definition split and the macro layer are the whole program. nlohmann is
one header-only library plus a test suite written entirely in macros, which is
the hardest thing a C++ indexer built on a tree-sitter grammar can be asked to
read. click is a decorator-heavy Python package: functions made by decorators,
imports resolved across a package tree. gson is plain Java with interfaces,
generics and a deep package hierarchy.

## Reproduce

```sh
git clone --depth 1 https://github.com/DaveGamble/cJSON      ~/proj/ken-corpora/DaveGamble-cJSON
git clone --depth 1 https://github.com/nlohmann/json         ~/proj/ken-corpora/nlohmann-json
git clone --depth 1 https://github.com/pallets/click        ~/proj/ken-corpora/pallets-click
git clone --depth 1 https://github.com/google/gson           ~/proj/ken-corpora/google-gson

for pair in "DaveGamble-cJSON:jsc" "nlohmann-json:cpp" "pallets-click:py" "google-gson:java"; do
  n=${pair%%:*}; l=${pair##*:}
  ken --db /tmp/kc/$l.db index ~/proj/ken-corpora/$n
done

python scripts/corpus_stats.py jsc=/tmp/kc/jsc.db cpp=/tmp/kc/cpp.db \
                               py=/tmp/kc/py.db  java=/tmp/kc/java.db
```

`scripts/corpus_stats.py` prints, per index: files, lines, entity and ref
counts, the resolution mix, the top entity kinds, the ambiguous refs by kind,
and the largest files that produced no entity at all. That last list is the
cheapest parser-defect detector available: a file with code in it and nothing
in the index is either a grammar gap or a construct the SPEC deliberately
ignores, and both are worth knowing about.

## Resolution, as measured

```
corpus            one definition    of in-tree refs   ambiguous    external
cJSON                   82.0%              92.8%          286         520
nlohmann/json           28.3%              42.5%       35,057      30,597
click                   46.8%              80.0%          852       3,018
gson                    25.5%              44.3%        7,786      10,320
```

`external` is a reference to a name no entity in the corpus carries: libc for
cJSON (520), the JDK and its collections for gson (10,320), the Python builtins
and stdlib for click (3,018). `unresolved` is zero in all four, because the
remaining misses all have a name in the tree but more than one plausible
definition; they are recorded as `ambiguous` with a candidate count instead of
being guessed at.

## What each corpus broke

- **Header prototypes were invisible (cJSON).** `int api_open(const char *);`
  is a `declaration` node, not a `function_definition`, so the C tree produced
  no entity for the API's own signatures and every call in the `.c` file had
  nothing to resolve to. Such declarations now index as `function` with
  `decl=1`, provided they sit at a declaration scope: in C++ a *statement*
  such as `CHECK(j == Json::object());` is a syntactically valid function
  declaration, so "it parses as a declaration" is not evidence that it is one.
- **Macro calls impersonated declarations (nlohmann).** The test suite is
  `TEST_CASE("...") { SECTION("...") { CHECK(...); } }`. Each of those reads as
  a function or member declaration, so the index filled with entities named
  after assertion macros, one per test case. Two rules now stop it: a callable
  whose declarator subtree contains a tree-sitter `ERROR` node is rejected, and
  `store.prune_macro_phantoms()` drops any entity whose name the corpus also
  defines as a function-like macro, re-parenting the survivors. 7 entities
  pruned in cJSON, 42 in nlohmann.
- **typedef and using were dropped.** `json::value_t`, `api_id` are referenced
  everywhere and defined nowhere, so those references had no target. C
  `type_definition` and C++ `alias_declaration` now index as `kind="alias"`
  (37 in cJSON, 1,711 in nlohmann).
- **Function-like macros had no identity.** `API_MAX(a, b)` and
  `NLOHMANN_DEFINE_TYPE_INTRUSIVE(...)` are call expressions over a macro
  name; without a `macro` entity (C `preproc_function_def`) the call site can
  only ever be external. 656 macro entities in cJSON, 1,222 in nlohmann.
- **Module names were matched too shallowly (click, gson).** `import
  com.google.gson.internal.bind.TypeAdapters;` has six segments; the probe
  stopped at four. It now walks up to six, and also tries `.java` and
  `/__init__.py`, which is what makes a Python package resolve to a directory.

## Where it still fails

- **C++ member calls: 35,057 ambiguous refs out of 91,571 (38%).** A member
  call is indexed as the bare member name, and `get`, `size`, `begin`,
  `push_back` are each defined several times across a header-only library.
  Choosing between them needs the receiver's type, which is exactly the clang
  dependency this tool exists to avoid. The include closure does not rescue
  it: 3,189 of nlohmann's 3,752 include edges stay unresolved, so most
  `#include` lines cannot constrain anything either.
- **50 nlohmann files index zero entities**, the largest `tests/src/unit-msgpack.cpp`
  at 2,092 lines. Their whole content is `TEST_CASE("...") { ... }` with
  lambdas inside: the file is a macro argument, so there is no function to
  index. Minting an entity from the string literal would invent a name the
  code does not spell, so the file honestly shows up empty.
- **8 gson files index zero entities** and all of them are `module-info.java`
  and `package-info.java`. Correct, but the front end should say so rather than
  listing them as ordinary empty files.
- **`docs/conf.py` in click indexes zero entities at 53 lines.** Sphinx builds
  a globals dict and never defines anything a tree-sitter Python grammar
  classifies as an entity; whether that is right is open.
- **`ambiguous` is not visible in the web front end.** You can see it in
  `summary` and in `scripts/corpus_stats.py`, not on a diagram.

## Next

Rust, Go and JavaScript corpora, one small project each, chosen the same way:
small, and shaped to break something. Cloned, not yet indexable:

| corpus | lang | pin | files | loc |
| --- | --- | --- | --- | --- |
| BurntSushi/memchr | Rust | `bd6068c` | 45 (`src/`) | 15,854 |
| spf13/pflag | Go | `7b3c6eb` | 76 | 13,577 |
| expressjs/express | JavaScript | `43fca52` | 6 (`lib/`) | 2,765 |

They stay unindexable until `SPEC["rust"]` and friends exist. Pointing `ken` at
one of them now says `indexed 0 files` and, on stderr, `skipped 45 files in
languages this build does not index: .rs 45`, which is the gap stated instead of
implied. See `docs/languages.md` for the plan and the per-language rules.
