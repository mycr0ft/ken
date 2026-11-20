# SysML v2 as an export target for ken

Status: the emitter exists and is gated; everything below is the mapping it
implements plus what the next phases would need. Written to settle the
question "what would a SysML v2 architecture for ken look like".

## Position: output, not storage

The index is relational on purpose. `files`, `entities`, `refs`, `file_deps`
with a resolution class per row is a shape you can aggregate, join, and measure
error rates against: 202 files, 172k lines, 44% resolved, 26% unresolved, and
each of those numbers is a `GROUP BY`. A SysML v2 model is a tree of typed
definitions and usages with no schema for "this ref did not resolve"; the
resolver's diagnostics would have nowhere to live, and every query the web UI
runs would become a traversal.

So the direction is one-way:

```
tree-sitter  ->  entities/refs (SQLite)  ->  UML 2.5 diagrams (web UI)
                                        \->  SysML v2 package tree (.sysml)
```

SysML v2 earns its place as an *interchange* format: a `ken` model can be
handed to a systems-engineering tool, annotated with requirements and
allocations there, and reasoned about as a system rather than as source code.
It is a good fit for exactly the case stated up front, simple abstract models
of systems including software ones.

## What is emitted today

`ken --db code.db sysml --scope <dir> --out <file> [--max-classes N]`, MPL-2.0,
in `src/ken/sysml.py`.

| ken | SysML v2 | notes |
| --- | --- | --- |
| directory under `--scope` | `package` | nested blocks; dotted names are a syntax error |
| `class`, `struct`, `interface`, `enum` | `part def` | the same universe the UML class view uses (`_is_class`) |
| `loc`, `complexity` | `attribute` | plain typed attributes |
| `inherit` ref | `specializes` | target must be an emitted class, else dropped |
| `call` / `instantiate` ref | `dependency ... from A to B` | aggregated method -> class through `parent_id`, then deduplicated |
| name collision within a package | `Name_2` | disambiguation, never dropping a class |

Deliberate omissions: methods and fields are not emitted (a `part def` per
class keeps the model readable), unresolved refs are not emitted, and file
dependencies (`#include`, `import`) only appear where they also produced a
class-level dependency.

### Gate

Emission is checked numerically, not by eye:

1. the file loads in `sysmlpy.loads()` with zero syntax errors;
2. `model.count()['part']` equals the number of classes the emitter reports;
3. `stats['emitted'] == stats['classes']` (nothing lost to deduplication).

`tests/test_diagrams.py` asserts (1) and (2) when `sysmlpy` is importable and
skips that one case when it is not.

## Phase 2: a system model, not just an object model

The current projection answers "what classes are here and what uses what".
A genuine architecture needs more of the language. In order of usefulness:

1. **Interfaces from public method sets.** Methods are already in `entities`
   as children via `parent_id`. Emit an `interface def` per class that other
   classes call into, with `port`s for the entry points, and
   `connect ... to ...` from the `call` refs. This is the single change that
   would make the model say something the class diagram does not.
2. **Packages as `package` + `import`.** Directory structure exists; what is
   missing is emitting the dependency as a first-class `import` inside the
   package body rather than as a class-level `dependency`.
3. **Allocations to implementation.** `allocation def` from a `part` to the
   `part def` that implements it, which is how an MBSE tool records "this
   logical component is realised by these classes".
4. **Requirements.** Out of scope for ken: there is nothing in source code to
   derive them from. Requirement traceability belongs to the tool that consumes
   the model, and the model should leave room for it rather than invent ids.

## Phase 3: surfacing the export in the web UI

The emitter is a CLI verb today, and that is the right home for it: an export
is a file-producing operation with options, not a page. If it should appear in
the browser, the honest shape is a download link on the package and class
views that hits `GET /api/sysml?path=&limit=` and sets
`Content-Disposition: attachment`, so the model in hand matches the picture on
screen. Not started; no route exists.

## Open questions

* **Granularity of the class universe.** `MAX_UML_CLASSES` exists because a
  generated parser has hundreds of sibling context classes. The same problem
  hits the emitter (1578 parts at first spike on `sysmlpy/src/sysmlpy`), where
  `--max-classes` reports the omission. Whether the default should follow the
  diagram cap is undecided.
* **Do methods belong in the model?** Emitting them as `action def` inside the
  part would make the sequence diagram expressible in SysML terms, at a large
  cost in model size. Not planned.
* **Namespace hygiene.** Names are unique per package because SysML wants
  document-unique names. A corpus with a `Foo` in three modules emits `Foo`,
  `Foo_2`, `Foo_3`, which is honest but loses the path. A qualified-name
  attribute on each `part def` would restore it.
* **Emission size.** A full-scope run on `sysmlpy/src/sysmlpy` emits 1578 parts,
  78 specializations, and 3985 dependencies in 755 kB of text, and `sysmlpy`
  counts the same 1578 parts. That is a valid file and a poor model: nothing in
  it states what the system *does*. Phase 2 item 1 (interfaces and ports) is
  what makes the size worth reading, so the open question is only whether to
  cap by default or always emit everything.
