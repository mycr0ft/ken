# ken

A small, open-source code comprehension index: parse a C / C++ / Python / Java
tree, store every entity, reference and dependency in SQLite, then ask questions
about it from the shell.

`ken` is a from-scratch answer to the archived Sourcetrail and to commercial
Understand: no clang, no JVM, no compile database at index time. Parsing is
tree-sitter; the whole state is one SQLite file you can copy, diff or ship.

## Install

```sh
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -e .
ken --help
```

## Use

```sh
ken --db my.db index src include          # index one or more trees
ken --db my.db summary                    # counts by language, kind, loc
ken --db my.db find helper                # token search (FTS5)
ken --db my.db sym scale                 # declarations + definitions
ken --db my.db callers helper            # who calls it, with file:line
ken --db my.db callees main              # what it calls, resolved + unresolved
ken --db my.db metrics --top 20          # complexity + fan-in leaders
ken --db my.db deps calc.cpp             # #include / import graph for one file
ken --db my.db graph main --depth 3      # call graph tree (--dot for Graphviz)
ken --db my.db graph helper --direction up
```

Index output also reports resolution quality:

```
indexed 7 files, 23 entities, 18 refs in 0.0s
refs  exact=11  scoped=0  unique=6  ambiguous=1  external=1  unresolved=4  -> resolved 77.3%
deps  resolved=2  unresolved=5
```

`external` is a call to a language builtin (`len`, `hasattr`, `super`): a target
that by definition lives outside the tree, so it is neither resolved nor a
failure. `unresolved` is a real miss (typically a library call such as
`printf` or `List.of`). Both are counted, never silently dropped. `resolved%`
is computed over references that have an in-tree candidate at all.

Measured on two real trees: 202 files / 172k lines of Python and C++ gives
44% resolved, 24% external, 16% ambiguous, 16% unresolved in 4 s; 23.5k headers
of `/usr/include` gives 22% resolved in 1m53s (a declarations-only tree is the
worst case for name-based resolution, and the reason `ambiguous` is recorded
with its candidate count rather than guessed at).

## What is in the database

| table | contents |
| --- | --- |
| `files` | path, language, loc, bytes |
| `entities` | kind, name, `qualname` (`Foo::bar`), parent, line span, complexity, `decl` flag |
| `refs` | call / instantiate / inherit edges, target name, resolved `target_id`, resolution class, candidate count |
| `file_deps` | `#include` / `import` edges, raw target, resolved destination file |

Indexes on name, qualname, file and ref target; FTS5 mirror of the symbol table
(`ent_fts`) kept in sync by triggers; WAL so readers never block an index run.

## Resolution

A post-pass over the finished database, in this order:

1. exact `qualname` match;
2. unique name match, filtered to the source file's **language family** (C and
   C++ are one family because they interoperate; Python and Java are separate);
3. same-file candidates, then same-directory (one package or module), then
   same enclosing scope, then definition beats a same-name declaration in a
   header;
4. otherwise `ambiguous` (candidates recorded) or `unresolved`.

Every ref stores its resolution class, so you can measure the index instead of
trusting it.

## Design

* `langs.py` - one generic AST walker plus a per-language spec table mapping
  tree-sitter node type to an action: entity, reference, dependency, or
  transparent wrapper. Node types are the only language-specific knowledge.
* `store.py` - SQLite schema, bulk insert, reference resolution, queries.
* `cli.py` - argparse front end and the shell verbs.

The walker is iterative with an explicit work list: generated headers (LLVM
intrinsic tables) nest deeper than Python's recursion limit.

## Browse the index in a browser

`ken web` serves a read-only single-page front end over an existing index. No
extra dependencies, no build step: the server is `http.server` plus one JSON
endpoint per question, and the assets ship inside the package.

    ken --db code.db web --port 8000 --open

`--db` is a global option, so it precedes the subcommand. Every view is
DB-derived and re-reads on navigation:

| route | what it answers |
| --- | --- |
| `#/` | corpus shape: languages, line counts, resolution mix, biggest packages |
| `#/files?path=` | the directory tree, one row per file with entity and LOC counts |
| `#/file/<id>` | source with entity bands, imports, dependents, exports |
| `#/sym/<id>` | one symbol: declaration site, children, calls in and out, references |
| `#/graph/<id>?dir=up\|down&depth=` | call graph, bucketed by file so 200 edges stay readable |
| `#/uml?path=&limit=` | class diagram: packages as panels, inheritance solid, use dashed |
| `#/search?q=&in=symbols\|files` | full-text search over names, qualified names, and paths |

Two things worth knowing about the diagrams. The class diagram selects a
*connected* neighbourhood by breadth-first walk from the structurally busiest
classes, capped per file, per directory, and per class name: a generated
parser's three hundred sibling context classes would otherwise take every slot.
And the call graph buckets by file rather than drawing one node per symbol,
because a two-hundred-node graph is a hairball at any zoom level.

## Limits, honestly

* Name-based resolution, no type inference: overloads, virtual dispatch, and
  macros are not distinguished. `obj.method()` binds to the unique same-name
  method in the file or scope.
* C++ out-of-line definitions (`int Foo::bar()`) are indexed as `function` on
  the defining file; they resolve correctly by qualname but read as free
  functions in `sym`.
* No call-site column, no dataflow, no preprocessor evaluation. Conditional
  compilation is ignored: every `#ifdef` branch is indexed.
* Inheritance edges come from an explicit base list (`class A : public B`,
  `class A(B)`, `extends`); interfaces mixed in by macro or by generated code
  are missed.
* Files that fail to parse are listed and skipped rather than killing the run.

## Related work

Sourcetrail (GPLv3, archived 2021) covered the same four languages with a GUI
and a clang-based C/C++ front end. Commercial Understand (SciTools) is the
feature benchmark. SCIP/LSIF and Glean solve the same problem by consuming real
compiler output; `ken` deliberately trades precision for a single-command
install that works on any tree.

## License

Mozilla Public License 2.0. File-level copyleft: you may link `ken` into
anything, including proprietary tools, but modifications to `ken`'s own files
stay under the MPL. See `LICENSE` for the full text; each source file carries
the short notice.
