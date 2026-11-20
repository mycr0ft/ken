# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The node shapes docs/languages.md promises for Rust, Go and JavaScript.

A SPEC table written from memory of a grammar indexes nothing and says nothing
about having done so, so the plan's per-language tables are executable here:
each snippet below is parsed, and every node type and field name the plan cites
has to be present in the tree.  When a grammar is upgraded and a name moves,
this fails instead of the index quietly going empty.

The three grammars are not runtime dependencies yet (see docs/languages.md,
"Engine changes"), so this module skips where they are absent.
"""
from __future__ import annotations

import importlib

import pytest

from ken import langs

CASES = []


def case(lang, src, need_types=(), need_fields=()):
    CASES.append((lang, src, tuple(need_types), tuple(need_fields)))


case(
    "rust",
    """
use std::collections::{HashMap, HashSet as HS};
use crate::model::Widget;
mod inner;
trait Shape { fn area(&self) -> f64; fn name(&self) -> String { String::new() } }
struct Circle { r: f64 }
impl Circle { fn new(r: f64) -> Self { Circle { r } } }
impl Shape for Circle { fn area(&self) -> f64 { 3.14 * self.r * self.r } }
macro_rules! square { ($x:expr) => { $x * $x }; }
fn main() { let c = Circle::new(2.0); println!("{}", c.area()); square!(3); }
""",
    need_types=(
        "use_declaration", "scoped_use_list", "use_as_clause", "mod_item",
        "trait_item", "function_signature_item", "function_item", "struct_item",
        "impl_item", "field_declaration", "macro_definition", "macro_invocation",
        "call_expression", "scoped_identifier",
    ),
    need_fields=(
        ("use_declaration", b"argument"),
        ("mod_item", b"name"),
        ("impl_item", b"type"),
        ("impl_item", b"trait"),
        ("function_item", b"name"),
        ("macro_definition", b"name"),
        ("macro_invocation", b"macro"),
        ("call_expression", b"function"),
        ("field_declaration", b"name"),
    ),
)

case(
    "go",
    """
package demo
import ( "fmt"; "strings" )
type Shape interface { Area() float64 }
type Circle struct { r float64 }
func (c Circle) Area() float64 { return 3.14 * c.r * c.r }
func New(r float64) Circle { return Circle{r: r} }
func main() { c := New(1.0); fmt.Println(strings.ToUpper("x"), c.Area()) }
""",
    need_types=(
        "package_clause", "import_declaration", "import_spec", "type_declaration",
        "type_spec", "interface_type", "method_elem", "struct_type",
        "method_declaration", "function_declaration", "call_expression",
        "selector_expression", "composite_literal",
    ),
    need_fields=(
        ("type_spec", b"name"),
        ("type_spec", b"type"),
        ("method_declaration", b"receiver"),
        ("method_declaration", b"name"),
        ("function_declaration", b"name"),
        ("call_expression", b"function"),
        ("selector_expression", b"operand"),
        ("selector_expression", b"field"),
        ("import_spec", b"path"),
    ),
)

case(
    "javascript",
    """
import express from 'express';
import { readFile, writeFile as wf } from './m.js';
const path = require('path');
export function handler() { return 1; }
class Server { listen(port) { return new Promise(() => {}); } }
const fmt = (x) => x + 1;
const obj = { area(r) { return r * r; } };
path.join('a', 'b'); obj.area(2);
""",
    need_types=(
        "import_statement", "import_clause", "named_imports", "import_specifier",
        "lexical_declaration", "variable_declarator", "arrow_function",
        "export_statement", "function_declaration", "class_declaration",
        "class_body", "method_definition", "new_expression", "call_expression",
        "member_expression", "object",
    ),
    need_fields=(
        ("import_statement", b"source"),
        ("import_specifier", b"name"),
        ("import_specifier", b"alias"),
        ("variable_declarator", b"value"),
        ("method_definition", b"name"),
        ("call_expression", b"function"),
        ("member_expression", b"object"),
        ("member_expression", b"property"),
        ("new_expression", b"constructor"),
        ("export_statement", b"declaration"),
    ),
)


BINDING = {"rust": "tree_sitter_rust", "go": "tree_sitter_go",
           "javascript": "tree_sitter_javascript"}


def _grammar(lang):
    """The grammar object, from `langs` once the SPEC exists and from the
    binding directly until then: the point of this test is the grammar, not
    the registry."""
    try:
        return langs.language(lang)
    except Exception:  # noqa: BLE001 - language not registered yet
        pass
    try:
        mod = importlib.import_module(BINDING[lang])
    except ImportError as exc:
        pytest.skip(f"{lang} grammar not installed: {exc}")
    capsule = mod.language()
    if isinstance(capsule, langs.ts.Language):
        return capsule
    return langs.ts.Language(capsule)


def _tree(lang, src):
    return langs.ts.Parser(_grammar(lang)).parse(src.encode()).root_node


def _pairs(root):
    types, fields = set(), set()
    stack = [root]
    while stack:
        n = stack.pop()
        types.add(n.type)
        for i in range(n.child_count):
            f = n.field_name_for_child(i)
            if f:
                fields.add((n.type, str(f).encode()))
        stack.extend(n.children)
    return types, fields


@pytest.mark.parametrize("lang,src,need_types,need_fields", CASES,
                         ids=[c[0] for c in CASES])
def test_plan_names_exist_in_the_grammar(lang, src, need_types, need_fields):
    types, fields = _pairs(_tree(lang, src))
    missing_types = [t for t in need_types if t not in types]
    missing_fields = [f"{t}.{f.decode()}" for t, f in need_fields if (t, f) not in fields]
    assert not missing_types, f"{lang}: node types absent from the grammar: {missing_types}"
    assert not missing_fields, f"{lang}: fields absent from the grammar: {missing_fields}"
