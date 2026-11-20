# Adding languages

C, C++, Python and Java are in. The next three are **Rust**, **Go** and
**JavaScript/TypeScript**, in that order of engineering difficulty, not
popularity. Fortran and COBOL are out of scope and stay out: fixed-form source
and column rules aside, their call graphs are dominated by COMMON blocks,
EQUIVALENCE and preprocessor abuse, which is a different tool.

Everything below was checked against the actual grammars
(`tree-sitter-rust`, `tree-sitter-go`, `tree-sitter-javascript`,
`tree-sitter-typescript`), not against documentation, by parsing the snippets
in the "node shapes" sections and dumping the field names. That check is a
test: `tests/test_language_plan.py` parses each snippet and fails if a node type
or field name cited below stops existing, so a grammar upgrade breaks the build
instead of the index. The versions the tables below were verified against are
`tree-sitter` 0.26.1 with `tree-sitter-rust` 0.24.2, `tree-sitter-go` 0.25.0,
`tree-sitter-javascript` 0.25.0 and `tree-sitter-typescript` 0.23.2. A rule written from memory of a grammar is a rule that
silently indexes nothing.

## What a language costs today

Nine places know a language exists. Only two of them are the interesting work.

| # | where | what | per language |
| --- | --- | --- | --- |
| 1 | `langs.py:21` `EXTS` | file extension to language | one line per extension |
| 2 | `langs.py:29` `MODULES` | language to tree-sitter binding | one line, or one function for a grammar that ships two |
| 3 | `langs.py:352` `SPEC[lang]` | node type to `("ent", kind, name_fn[, guard])` or `("ref", rkind, target_fn)` | **the real work** |
| 4 | `langs.py:413` `DECISIONS[lang]` | node types that add a branch to complexity | a set of 6-12 types |
| 5 | `langs.py` helpers | name and target extraction, declaration-scope guards | 1-3 small functions |
| 6 | `langs.py` `_DECL_SCOPES`, `CALLABLE` | which parents make a declaration real; which kinds can be called | new tuple per language |
| 7 | `store.py:178` `_outside(fam, target)` | names that are external by definition | one name set |
| 8 | `store.py` `CALLEE` | entity kinds that can be a call target | add this language's kinds |
| 9 | `store.py:311` file-dep probing | how an `import`/`#include`/`mod` string names a file | new probe per module style |

Free, because it works off entity *kind* and target *text* rather than syntax:
the SQLite schema, the iterative walker, FTS, the whole resolution pass, the
complexity metric, `summary`, every CLI verb, and all four diagrams. That is
the payoff of the `SPEC`-table design and the reason adding a language is a
table plus a resolver hook rather than a new indexer.

## Node shapes (verified)

### Rust

```
use std::collections::{HashMap, HashSet as HS};   use_declaration
  argument=scoped_use_list  path=scoped_identifier('std::collections')  list=use_list[HashMap, use_as_clause]
use crate::model::Widget;                          argument=scoped_identifier  path=, name=
mod inner;                                         mod_item  name=identifier
trait Shape { fn area(&self) -> f64; ... }         trait_item  body=declaration_list
  fn area(&self) -> f64;                           function_signature_item   <- a declaration
  fn name(&self) -> String { .. }                  function_item             <- a definition
struct Circle { r: f64 }                           struct_item  body=field_declaration_list[field_declaration name=field_identifier]
impl Circle { fn new(..) }                         impl_item  type=type_identifier  body=declaration_list
impl Shape for Circle { .. }                       impl_item  trait=type_identifier  type=type_identifier
macro_rules! square { .. }                         macro_definition  name=identifier
Circle::new(2.0)                                   call_expression  function=scoped_identifier[path=, name=]
println!("{}", x);                                 macro_invocation  macro=identifier  token_tree
```

### Go

```
package demo                                       package_clause  package_identifier
import ( "fmt" )                                   import_declaration  import_spec_list  import_spec path=interpreted_string_literal
type Shape interface { Area() float64 }            type_declaration  type_spec  name=type_identifier  type=interface_type[method_elem]
type Circle struct { r float64 }                   type_declaration  type_spec  type=struct_type
func (c Circle) Area() float64 { .. }              method_declaration  receiver=parameter_list[parameter_declaration type=type_identifier]
func New(..) Circle { .. }                         function_declaration  name=identifier
fmt.Println(c.Area())                              call_expression  function=selector_expression[operand=, field=]
```

### JavaScript

```
import express from 'express';                     import_statement  import_clause[identifier]  source=string
import { readFile, writeFile as wf } from 'm'      import_clause  named_imports  import_specifier  name=, alias=
const path = require('path');                      lexical_declaration  variable_declarator  value=call_expression[function=identifier('require')]
export function handler() { .. }                   export_statement  declaration=function_declaration
class Server { listen() { .. } }                   class_declaration  body=class_body  method_definition name=property_identifier
const fmt = (x) => x;                              variable_declarator  value=arrow_function
const obj = { area(r) { .. } };                     object  method_definition
o.p()                                              call_expression  function=member_expression[object=, property=]
new Thing()                                        new_expression  constructor=identifier
```

## Rust

| | |
| --- | --- |
| entities | `function_item`→function, `function_signature_item`→function (`decl=1`), `struct_item`→struct, `enum_item`→enum, `trait_item`→trait, `union_item`→union, `mod_item`→module, `macro_definition`→macro, `const_item`/`static_item`→const, `field_declaration`→field |
| refs | `call_expression`→call (`scoped_identifier` target text), `macro_invocation`→call (macro name, `!` stripped), `impl_item` with `trait=`→inherit (type inherits trait) |
| scopes | `source_file`, `mod_item`, `impl_item`, `trait_item`, `function_item.body`, `extern_block` |
| platform | `std`, `core`, `alloc`, `Vec`, `String`, `Option`, `Result`, `Box`, `println!`/`format!`/`vec!` and the rest of the prelude |
| deps | `use` path → `crate::m::T` ⇒ `m.rs` or `m/mod.rs`; `mod x;` ⇒ `x.rs`/`x/mod.rs` |

Two things make Rust harder than its grammar suggests. First, `impl` blocks:
a method's owner is the `type` field of the enclosing `impl`, which may name a
type defined in another file, so the parent has to be resolved by name at index
time and left parentless when it is not found. Second, derive macros: the real
API of a `serde` type is `#[derive(Serialize)]`, and there is no body to index;
those files will show as empty and that is honest, but it means "zero-entity
file" is a weaker defect signal in Rust than in C.

Gate: `unresolved` ≤ 2% of refs, and the trait/impl edges present
(`impl Shape for Circle` ⇒ one `inherit` ref Circle→Shape).

## Go

| | |
| --- | --- |
| entities | `function_declaration`→function, `method_declaration`→method (owner = receiver type), `type_spec`→struct/interface/alias by `type` child, `method_elem`→method (`decl=1`, interface method set), `const_spec`/`var_spec` |
| refs | `call_expression`→call; `selector_expression` where `operand` is a package alias→call with target `pkg.Field`; `composite_literal` of a named type→instantiate |
| scopes | `source_file` only (everything legal lives there) |
| platform | `fmt`, `strings`, `os`, `errors`, `bytes`, `sort`, `strconv`, `sync`, `time`, `io`, `context`, `regexp`, `encoding/*` |
| deps | `import_spec` path → directory under the module root; one package = one directory = many files |

Go is the language where name-based resolution nearly works, for structural
reasons: no overloading, no prototypes, one package per directory, and a
package qualifier in front of every cross-package call. The one thing it cannot
have is an interface edge: Go has no `implements` clause, satisfaction is
structural. Either infer it (type has the interface's whole method set, which
`method_elem` makes checkable) or leave it out and say so. Inferring it is a
post-pass over two method sets, no type checker needed, and it is the only
place in this plan where the indexer would produce an edge the source does not
spell.

Gate: `unresolved` ≤ 1%; `selector_expression` calls resolve ≥ 95%.

## JavaScript, then TypeScript

| | |
| --- | --- |
| entities | `function_declaration`→function, `method_definition`→method or constructor, `class_declaration`/`class`→class, `variable_declarator` whose value is `arrow_function`/`function`→function, `object`'s `method_definition`, `import_specifier` |
| refs | `call_expression`→call (`identifier`, `member_expression.property`), `new_expression`→instantiate, `class_heritage`→inherit, `require(...)`→dep |
| scopes | `program`, `class_body`, `statement_block`, `switch_body` |
| platform | `console`, `JSON`, `Math`, `Object`, `Array`, `Promise`, `String`, `Date`, `process`, `require`, `module`, `exports`, `document`, `window`, `fetch` |
| deps | `import ... from './x'` ⇒ `x.js`/`x.ts`/`x/index.js`; bare specifier ⇒ `node_modules`, never indexed |

JavaScript is the first language here that name matching cannot do at all. In
C a name is a name; in JS a name is a *binding*: `readFile` in one file is
`fs.readFile`, in another it is a local arrow function, and `express` is a
module alias with no definition anywhere in the tree. The resolution pass needs
a per-file binding table built from `import_statement`/`require` and consulted
before the global name buckets, which means a new small table (`file_bindings`:
file, local name, imported name, source) and a resolution step in front of the
existing ones. That work is worth doing once and it is also what TypeScript
needs, which is why JS and TS are one step, not two.

Practical rules: exclude `node_modules`, `dist`, `build`, `*.min.js` in the
file walker (indexing a dependency tree tells you nothing about the codebase);
`.jsx`/`.tsx` need the `tsx` grammar, so `MODULES["typescript"]` is a function
that picks `language_typescript` or `language_tsx` from the extension.

Gate: calls whose target came from an import binding resolve ≥ 80%, and
`node_modules` contributes zero files.

## Engine changes, in order

0. **Instrumentation first.** `scripts/corpus_stats.py` already reports the
   resolution mix per index; extend it with a per-language line and keep it in
   the repo. Every step below has a number attached, and a step that does not
   move its number is not finished.
1. **Registry.** Nine seams is fine for four languages and not for seven.
   Collapse 1-8 into one `LangSpec` dataclass (extensions, grammar loader,
   SPEC, DECISIONS, decl scopes, callable kinds, platform names, dep probes)
   and let `store.py` ask the registry instead of `if fam == "java"`. Do this
   before Rust, not after: three languages added one at a time to a dict and
   three if-chains is how the resolver becomes unreviewable.
2. **Rust.** SPEC table, `impl`/`trait` parenting, `mod`/`use` dep probing,
   prelude name set. Expect the smallest amount of new engine code and the most
   new table code. The `tree-sitter-rust` dependency goes into `pyproject.toml`
   in the same commit as the SPEC, not before: a declared dependency on a grammar
   with no rules is an install cost for a feature that does not exist, and it
   makes the metadata claim support the indexer cannot deliver. All three
   grammars are installed in the development `.venv` already.
3. **Go.** SPEC table, receiver-derived method owner, package-qualified
   resolution in the dependency pass, stdlib name set. The new engine concept is
   "package": an entity's home is a directory, not a file.
4. **JavaScript.** File walker exclusions, `file_bindings` table, binding-first
   resolution, global name set.
5. **TypeScript.** Second grammar selection, `.tsx`, and type annotations as a
   future source of receiver types. Deliberately last: it is JS plus types, and
   the types are only useful after the binding layer exists.
6. **Front end.** A language badge on files and symbols, per-language colour in
   the call graph, and the empty-file list annotated with *why* it is empty
   (macro-only, `module-info`, `node_modules`). The diagrams need no changes.

## Corpora

One small project per language, same criterion as the existing four: small
enough to re-index in a second, shaped to break something specific.

Three of these are cloned already, in `~/proj/ken-corpora`, with the sizes
measured rather than guessed:

| language | corpus | pin | files | loc | why it is the right kind of hard |
| --- | --- | --- | --- | --- | --- |
| Rust | `BurntSushi/memchr` | `bd6068c` | 45 in `src/` | 15,854 | plain `fn`s, `unsafe`, trait impls, several modules declared with `mod`; no macro wall worth speaking of. `benchmarks/` holds another 53k lines of generated lookup tables and should be indexed deliberately or not at all |
| Go | `spf13/pflag` | `7b3c6eb` | 76 | 13,577 | interfaces with several implementations, method values, and one package spread across 76 files in a single directory: the package model without a directory tree |
| JavaScript | `expressjs/express` | `43fca52` | 6 in `lib/` (142 total) | 2,765 (`lib/`), 22,046 all | CommonJS, `module.exports`, prototype methods, a dense `require` graph. `lib/` is the library; `test/` is 17k lines of `require` plus assertion chains, which is the real binding-table test |

Stretch corpora, once the first three pass their gates: `dtolnay/thiserror`
(Rust, ~3k loc, an API that is entirely a derive macro, so it proves the
"honestly empty file" story), `pkg/errors` (Go, ~600 loc, checks that a clean
corpus really does resolve to ~100%), and `chalk/chalk` (JavaScript, ESM-only,
the other module system).

## Effort

| step | new code | risk |
| --- | --- | --- |
| registry refactor | ~150 lines changed in `langs.py`/`store.py`, no behaviour change | moderate: it touches the resolution pass, so it needs a before/after table on the four existing corpora to prove nothing moved |
| Rust | SPEC ~12 rules, 2 helpers, 40 name set, 1 probe | low-moderate: `impl` parenting is the only genuinely new idea |
| Go | SPEC ~8 rules, 1 helper, 40 name set, 1 probe + package model | low: the language is the most indexer-friendly of the three |
| JS | SPEC ~12 rules, 2 helpers, 30 name set, walker exclusions, `file_bindings` table, binding resolution | high: this is the first change to the resolution pass itself |
| TS | grammar switch, `.tsx` | low once JS is done |

Order of magnitude for the table work: the four existing SPECs together are
about 60 rules in 120 lines; Rust, Go and JS add roughly 30 more.

## Non-goals

- **Fortran, COBOL.** Excluded as stated. So are Ada, Verilog/VHDL and
  assembly: different parse problem, different user.
- **Type inference.** The reason C++ member calls stay 38% ambiguous. Adding a
  type checker to fix it would reintroduce the clang/JVM dependency this tool
  exists to avoid. The honest alternative, if precision ever becomes the
  priority, is to consume SCIP or `rust-analyzer`/`gopls` output as an optional
  enrichment pass and keep the tree-sitter index as the fallback. Not planned.
- **Cross-language call edges** (JS→WASM, Go→cgo, Python→C extension). The
  refs are recorded per language; nothing tries to bridge them.
