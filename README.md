# ken

A small, open-source code comprehension index: parse a C / C++ / Python / Java
tree, store every entity, reference and dependency in SQLite, then ask questions
about it from the shell, or browse it in the bundled web front end.

Rust, Go and JavaScript are the next languages in, and the plan (what a language
costs, what each one gets wrong, which corpus exercises it) is in
`docs/languages.md`. Fortran and COBOL are not coming.

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

Index output also reports resolution quality, on two denominators:

```
pruned 7 macro look-alikes
indexed 101 files, 2065 entities, 4489 refs in 0.3s
refs  exact=3473  scoped=208  unique=2  ambiguous=286  external=520  unresolved=0
      82.0% of all refs name one definition, 92.8% of the 3969 in-tree refs
deps  resolved=163  unresolved=226
```

Files in languages this build does not read are counted and named on stderr
rather than walked past: `indexed 0 files` is a true statement about a Rust tree
only if it says how many `.rs` files it saw. The first percentage counts every
reference, the second drops the ones naming
something the corpus never introduces, which is the only denominator on which
a 19k-line tree and a 240k-line tree are comparable. The classes mean what they
say: `external` is a name no entity anywhere in the index carries (`printf`,
the JDK collections, a Python builtin); `unresolved` is the narrower miss where
the name exists in the tree but nothing callable answers to it; `ambiguous`
means several definitions do, and the candidate count is stored instead of a
guess.

Four small corpora, one per language, with the numbers to move whenever a
parsing or resolution rule changes, are documented in `docs/corpora.md`. Share
of references naming exactly one definition: C 82%, C++ 28%, Python 47%, Java
26%; restricted to in-tree references, 93%, 43%, 80%, 44%. C++ is the outlier
for a structural reason: a member call is indexed as a bare member name, and
`get`, `size` and `begin` each have a dozen definitions in a header-only
library.

## What is in the database

| table | contents |
| --- | --- |
| `files` | path, language, loc, bytes |
| `entities` | kind, name, `qualname` (`Foo::bar`), parent, line span, complexity, `decl` flag, `doc` (the comment attached to it) |
| `refs` | call / instantiate / inherit edges, target name, resolved `target_id`, resolution class, candidate count |
| `doc_links` | a name the documentation mentions (`@see`, `\sa`, `{@link}`, `:func:`), resolved to an entity when the corpus defines it |
| `file_deps` | `#include` / `import` edges, raw target, resolved destination file |

Indexes on name, qualname, file and ref target; FTS5 mirrors of symbols
(`ent_fts`) and of documentation prose (`doc_fts`), both kept in sync by
triggers; WAL so readers never block an index run.

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

## Read what the author wrote

Most of what explains a function is not in its body. `ken` reads the comment
above each declaration as it parses, so a symbol page shows the author's own
words above the code, and so prose becomes searchable:

    docs  3832/13712 public entities documented (27.9%), 795 cross-references resolved

Four conventions, one code path: Doxygen (`/** */`, `///`, `\brief`), Javadoc
(`/** */`, `@param`, `{@link Foo}`), Python docstrings, and JSDoc/GoDoc when
their grammars arrive. No Doxygen run, no Javadoc tool, no docbook to keep in
sync with the source: the comment is a sibling node in the tree, and the grammar
already knows where it is. Text is cleaned at parse time (comment decoration
stripped, `{@link}` unwrapped), so nothing downstream needs to know the syntax.

Names the documentation points at - `@see dev_close`, `\sa shrink`,
`{@link Tally}`, `#reset()` - become `doc_links` rows and resolve through the
same post-pass as ordinary references. Unresolved ones are kept, not dropped:
"this refers to something outside the corpus" is a useful answer when the target
is `java.util.Map`. `ken web` renders the block as prose, with tag lines
(`@param`, `@throws`, `@since`) as a definition list rather than as paragraphs
that happen to start with an at-sign, and offers `in=docs` search that ranks on
prose (`bm25`) and highlights the matching phrase.

Measured on two doc-heavy trees (nlohmann/json + gson, 932 files, 16,904
entities): 27.9% of public entities carry a comment, 795 of the names mentioned
in documentation resolve inside the index. The remainder are JDK and stdlib
names, which is the expected shape.

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
| `#/sym/<id>` | one symbol: declaration site, its documentation, children, calls in and out, references |
| `#/graph/<id>?dir=up\|down&depth=` | call graph, bucketed by file so 200 edges stay readable |
| `#/uml?path=&limit=` | class diagram: packages as panels, inheritance solid, use dashed |
| `#/packages` | package diagram: one box per directory, arrows for cross-package dependencies |
| `#/seq/<id>?limit=&depth=` | sequence diagram: an interaction trace out of one operation, one lifeline per class |
| `#/diagrams` | hub for the three diagram views, with the entry points ranked by distinct call targets |
| `#/search?q=&in=symbols\|docs\|files` | full-text search over names, documentation prose, and paths |

Two things worth knowing about the diagrams. The class diagram selects a
*connected* neighbourhood by breadth-first walk from the structurally busiest
classes, capped per file, per directory, and per class name: a generated
parser's three hundred sibling context classes would otherwise take every slot. The call graph buckets by file rather than drawing one node per symbol,
because a two-hundred-node graph is a hairball at any zoom level.

The three diagram views cross-link the way an IDE's do: a package box opens its
files, a package panel's title opens the package diagram, and a method's calls
heading opens that method's sequence diagram. Each one is a different question
over the same index, not a different model:

* **Class** - `entities` of class-like kind, `inherit` refs as generalizations,
  `call`/`instantiate` refs rolled up through `parent_id` as associations.
* **Package** - one box per directory (`files.path`, cut at the corpus root),
  one arrow per `file_deps` row that crosses two directories. Boxes are layered
  by longest dependency path, so a package sits below the packages it uses.
  Directory granularity, not file granularity: a few dozen boxes fit on one
  canvas, five hundred do not.
* **Sequence** - a depth-limited walk from one operation. Lifelines are the
  classes that own the called methods (or the file, when a call leaves a class),
  messages are the `call`/`instantiate` refs in call-site order. Calls into the
  caller's own class are marked `self` and drawn as a loop rather than expanded,
  which keeps helper calls from turning the diagram into a tree. A call whose
  target is outside the index is still an interaction, so it lands on a single
  dashed `«external»` lifeline at the end rather than being dropped: otherwise a
  method that only calls library code draws as an empty canvas that reads as
  "no interactions".

The sequence view keeps its natural size and scrolls instead of scaling to the
box: 10 px labels shrunk to 7 px are worse than a scrollbar. Message count and
walk depth are controls in its header and live in the hash, so a trace is a link
you can hand to someone else. When the cap cuts the trace the sub-line says
`first N of the trace` - it never claims to show everything it hid.

## Export the model as SysML v2

`ken sysml` projects the index into a SysML v2 package tree, for interchange
with MBSE tooling. It is an output format only: the index stays relational, and
nothing reads SysML back into `ken`.

    ken --db code.db sysml --scope src/sysmlpy --out model.sysml --max-classes 400

| ken | SysML v2 |
| --- | --- |
| directory | `package`, nested to match the path |
| class / struct / interface / enum | `part def`, with `loc` and `complexity` as `attribute`s |
| `inherit` ref | `specializes` |
| `call` / `instantiate` ref, rolled up to the calling class | `dependency ... from A to B` |

The output is checked, not asserted: it is expected to load without syntax
errors and to produce exactly one `part def` per emitted class. Names are made
unique per package (`Name_2`) rather than dropping classes on collision, and
`--max-classes` reports what it omitted instead of silently truncating.

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
* C++ is the weak language, and measurably so: 35,057 of 91,571 references in a
  header-only library (38%) are ambiguous because member calls carry no receiver
  type. Fixing that needs a type checker, which is the clang dependency this tool
  exists to avoid. 50 files in that corpus also index zero entities, because
  their entire content is a macro argument (`TEST_CASE("...") { ... }`).
* Documentation is read where the convention is regular, which is most of the
  time but not all of it: a project that documents its API in a separate
  `.rst`/`.md`/docbook tree is invisible here, and a comment separated from its
  declaration by a preprocessor block attaches to whatever node follows it.
  `@see Foo` resolves to the unique in-scope `Foo`; when several definitions
  share the name the first declaration wins, with no ambiguity counter.
* Prototypes and definitions share an entity kind and are told apart by the
  `decl` flag, so `sym` on a header prototype and on its definition are the same
  symbol only when the qualname matches exactly.

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
