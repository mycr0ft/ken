# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Per-language extraction: tree-sitter AST -> entities, references, file deps.

One generic driver walks the tree; each language supplies a spec table mapping
node type -> action. Entity namers return the short name plus an optional
qualifying prefix (for out-of-line definitions such as ``void Foo::bar() {}``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import tree_sitter as ts
import tree_sitter_c
import tree_sitter_cpp
import tree_sitter_java
import tree_sitter_python

# Extensions and grammars are not listed here: they are fields of the language
# records at the bottom of this file, so a language cannot be half-registered
# (an extension with no grammar, a grammar with no parse table).  See "the
# registry" there.

_cache: dict[str, ts.Language] = {}


def language(lang: str) -> ts.Language:
    if lang not in _cache:
        _cache[lang] = ts.Language(MODULES[lang].language())
    return _cache[lang]


# ---------------------------------------------------------------- node helpers

def text(src: bytes, node) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def first_child(node, *types):
    for c in node.named_children:
        if c.type in types:
            return c
    return None


# ---------------------------------------------------------------- doc comments
#
# Two shapes, not one.  C, C++, Java, Rust, Go and JavaScript put the doc in a
# comment node immediately before the declaration, as a sibling; Python puts it
# inside the body as the first statement.  Both are read here so that one
# function can be called from `add_ent` for every language.

COMMENT_TYPES = ("comment", "line_comment", "block_comment")

#: tags whose argument is a cross-reference to another entity.  These are the
#: edges the parser cannot see: `@see`, Doxygen `\ref`, Javadoc `{@link}`.
LINK_TAGS = ("@see", "\\see", "\\sa", "@ref", "\\ref", "@link", "\\link",
             "@linkplain", "{@link", "{@linkplain")


def _clean_doc(raw: str) -> str:
    """Comment text without the comment syntax."""
    txt = raw.strip()
    for opener in ("/**", "/*!", "/*", "///", "//!", "//"):
        if txt.startswith(opener):
            txt = txt[len(opener):]
            break
    if txt.endswith("*/"):
        txt = txt[:-2]
    lines = []
    for line in txt.splitlines():
        s = line.strip()
        for pre in ("///", "//!", "//", "*"):
            if s.startswith(pre):
                s = s[len(pre):].strip()
                break
        lines.append(s)
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _clean_docstring(raw: str) -> str:
    """Python docstring text, dedented, without the quotes."""
    txt = raw.strip()
    for q in ('"""', "'''", 'r"""', "r'''"):
        if txt.startswith(q):
            txt = txt[len(q):]
            break
    for q in ('"""', "'''"):
        if txt.endswith(q):
            txt = txt[:-len(q)]
            break
    lines = txt.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return ""
    body = [l for l in lines[1:] if l.strip()]
    indent = min((len(l) - len(l.lstrip()) for l in body), default=0)
    out = [lines[0].strip()]
    out += [l[indent:].rstrip() for l in lines[1:]]
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


def attached_doc(node, lang: str, src: bytes) -> str:
    """The documentation written for *node*, or "" when there is none.

    A comment counts as attached only when it touches the declaration line: a
    paragraph of prose three lines above a function documents the file, not the
    function, and treating it as the function's doc is how a doc-coverage metric
    becomes a lie.
    """
    body = node.child_by_field_name("body")
    if body is not None:
        first = first_child(body, "expression_statement")
        if first is not None and body.named_children and body.named_children[0] is first:
            lit = first_child(first, "string", "concatenated_string")
            if lit is not None:
                return _clean_docstring(text(src, lit))
    parts = []
    cursor_line = node.start_point[0]
    prev = node.prev_named_sibling
    while prev is not None and prev.type in COMMENT_TYPES:
        if prev.end_point[0] + 1 < cursor_line:
            break
        parts.append(text(src, prev))
        cursor_line = prev.start_point[0]
        prev = prev.prev_named_sibling
    if not parts:
        return ""
    return _clean_doc("\n".join(reversed(parts)))


def doc_links(doc: str, entity_qualname: str = "") -> list[str]:
    """Cross-references named in a doc block: `@see`, `\\ref`, `{@link}`.

    Kept beside the doc text rather than folded into `refs`, because a see-also
    is a human's judgement about relevance, not a reference the compiler would
    resolve, and mixing the two would flatter the resolution figures.
    """
    out, seen = [], set()
    for line in doc.splitlines():
        low = line.lower()
        for tag in LINK_TAGS:
            pos = low.find(tag.lower())
            while pos >= 0:
                rest = line[pos + len(tag):].lstrip(" :")
                if rest.startswith("}"):
                    rest = rest[1:].lstrip(" :")
                tok = rest.split()[0] if rest.split() else ""
                tok = tok.strip("{}<>,;:()[]`'\"")
                if tok and not tok.startswith(("http://", "https://")):
                    # Javadoc writes a member as `#reset()`; C++/Python/JSDoc
                    # write a qualified name as `a.b` or `a::b`, so a leading
                    # hash is the member marker and any later one is a label.
                    if tok.startswith("#"):
                        tok = tok[1:]
                    else:
                        tok = tok.split("#")[0]
                    tok = tok.strip(".")
                if tok and tok != entity_qualname and tok not in seen and \
                        (tok[0].isalpha() or tok[0] == "_"):
                    seen.add(tok)
                    out.append(tok)
                nxt = low.find(tag.lower(), pos +
                                len(tag))
                pos = nxt
    return out


def strip_template(s: str) -> str:
    """Drop template arguments: 'std::vector<int>' -> 'std::vector'."""
    out, depth = [], 0
    for ch in s:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out).strip()


IDENT_TYPES = ("identifier", "field_identifier", "type_identifier",
               "namespace_identifier", "statement_identifier")


def name_of(node, src) -> tuple[str, str]:
    """Best-effort (prefix, short_name) for a declarator or expression node."""
    if node is None:
        return "", ""
    t = node.type
    if t in IDENT_TYPES:
        return "", text(src, node)
    if t == "qualified_identifier":
        # 'A::B::f' -> ('A::B', 'f')
        name = node.child_by_field_name("name")
        scope = node.child_by_field_name("scope")
        short = text(src, name) if name is not None else strip_template(text(src, node)).split("::")[-1]
        pre = strip_template(text(src, scope)) if scope is not None else strip_template(text(src, node)).rsplit("::", 1)[0]
        return pre, short
    if t == "destructor_name":
        return "", text(src, node)
    if t == "operator_name":
        return "", text(src, node)
    if t == "template_function":
        return name_of(node.child_by_field_name("name"), src)
    if t == "template_method":
        return name_of(node.child_by_field_name("name"), src)
    if t == "template_type":
        return name_of(node.child_by_field_name("name"), src)
    if t == "field_expression":
        # obj.method / ptr->method
        return name_of(node.child_by_field_name("field"), src)
    if t == "attribute":
        return name_of(node.child_by_field_name("attribute"), src)
    if t in ("pointer_declarator", "function_declarator", "reference_declarator",
             "parenthesized_declarator", "attributed_declarator", "init_declarator",
             "array_declarator"):
        inner = node.child_by_field_name("declarator") or first_child(
            node, "pointer_declarator", "function_declarator", "reference_declarator",
            "parenthesized_declarator", "attributed_declarator", "array_declarator",
            *IDENT_TYPES)
        return name_of(inner, src)
    # generic fallback: rightmost identifier-ish descendant
    for c in reversed(node.named_children):
        pre, short = name_of(c, src)
        if short:
            return pre, short
    return "", ""


# ---------------------------------------------------------------- the builder

class Builder:
    def __init__(self, path: str, lang: str, src: bytes):
        self.path = path
        self.lang = lang
        self.src = src
        self.ents: list[dict] = []
        self.refs: list[dict] = []
        self.deps: list[dict] = []
        self.stack: list[int] = []
        self._by_node: dict[int, int] = {}   # id(node) -> entity index

    # -- entities
    def qual(self, name: str, prefix: str = "") -> str:
        parts = [self.ents[i]["name"] for i in self.stack]
        if prefix:
            parts.append(prefix)
        parts.append(name)
        return "::".join(p for p in parts if p)

    def add_ent(self, kind: str, name: str, node, prefix: str = "") -> int:
        decl = 1 if kind.startswith("decl_") else 0
        if decl:
            kind = kind[5:]
        if kind in ("function", "method") and self.stack and \
                self.ents[self.stack[-1]]["kind"] in ("class", "struct", "interface", "enum"):
            kind = "method"
        e = {
            "kind": kind,
            "decl": decl,
            "name": name,
            "qualname": self.qual(name, prefix),
            "line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "parent": self.stack[-1] if self.stack else None,
            "_node": node,
            "complexity": 0,
            "doc": attached_doc(node, self.lang, self.src),
        }
        self.ents.append(e)
        self._by_node[id(node)] = len(self.ents) - 1
        return len(self.ents) - 1

    CLASSY = ("class", "struct", "interface", "enum")

    def class_of(self, node):
        """Index of the nearest enclosing class/struct/interface entity (or None)."""
        n = node
        while n is not None:
            idx = self._by_node.get(id(n))
            if idx is not None and self.ents[idx]["kind"] in self.CLASSY:
                return idx
            n = n.parent
        # The entity may hang off a child node (a name, or the class_body), so
        # fall back to the nearest class-ish ancestor's own name.
        n = node
        while n is not None:
            if n.type in ("class_definition", "class_declaration", "class_specifier",
                          "struct_specifier", "enum_declaration", "interface_declaration"):
                name = (n.child_by_field_name("name") or n.child_by_field_name("type")) \
                    if hasattr(n, "child_by_field_name") else None
                nm = text(self.src, name) if name is not None else ""
                if nm:
                    hit = [i for i, e in enumerate(self.ents)
                           if e["name"] == nm and e["kind"] in self.CLASSY]
                    if hit:
                        return hit[-1]
                break
            n = n.parent
        return None

    def add_ref(self, kind: str, target: str, line: int, src_idx=None) -> None:
        target = strip_template(target) if target else ""
        if not target or len(target) > 200:
            return
        self.refs.append({
            "kind": kind,
            "target": target,
            "line": line + 1,
            "src": self.stack[-1] if src_idx is None and self.stack else src_idx,
        })

    def add_dep(self, kind: str, raw: str) -> None:
        if raw:
            self.deps.append({"kind": kind, "raw": raw})


# ---------------------------------------------------------------- specs

def _cpp_fn_prefix(node, src):
    """(prefix, name) of a C/C++ function_definition, tolerating ptr/ref declarators."""
    d = node.child_by_field_name("declarator")
    return name_of(d, src)


def _py_entity(node, src):
    return "", text(src, node.child_by_field_name("name") or node)


def _java_entity(node, src):
    return "", text(src, node.child_by_field_name("name") or node)


def _call_target(node, src):
    return name_of(node.child_by_field_name("function"), src)

def _c_call_target(node, src):
    return name_of(node.child_by_field_name("function"), src)


def _fn_declarator(node):
    """The function_declarator under a C declaration, if this declaration declares one.

    A `.h` line and a `.c` definition say the same thing about the API, and the
    header is the half a reader meets first, so prototypes are indexed as
    entities. Local variables are `declaration` nodes too, so the test is
    structural rather than "does this file end in .h".
    """
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == "function_declarator":
            return n
        if n.type in ("declaration", "pointer_declarator", "parenthesized_declarator"):
            stack.extend(n.named_children)
    return None


def _has_proto(node):
    # `CHECK(Json(p).dump() == x);` at statement position is a valid C++
    # declaration of a function named CHECK, so a block-scope "prototype" is
    # almost always an unexpanded macro call, never API.  Headers declare at
    # file, namespace or linkage scope; that is the only place worth reading.
    return _at_decl_scope(node) and _fn_declarator(node) is not None


def _c_proto_name(node, src):
    fd = _fn_declarator(node)
    if fd is None:
        return "", ""
    return name_of(fd.child_by_field_name("declarator"), src)


def _c_macro_name(node, src):
    n = node.child_by_field_name("name")
    return "", text(src, n) if n is not None else ""


def _c_alias_name(node, src):
    d = node.child_by_field_name("declarator")
    if d is not None and d.type == "function_declarator":
        d = d.child_by_field_name("declarator")
    return "", name_of(d, src)[1] if d is not None else ""


def _cpp_alias_name(node, src):
    n = node.child_by_field_name("name")
    return "", text(src, n) if n is not None else ""


def _method_invocation(node, src):
    return name_of(node.child_by_field_name("name"), src)


def _new_target(node, src):
    return name_of(node.child_by_field_name("type"), src)


# Preprocessor conditionals hoist their contents into the surrounding
# declaration list, so a declaration's parent can legitimately be one of these.
_DECL_SCOPES = ("translation_unit", "declaration_list", "field_declaration_list",
                "linkage_specification", "preproc_if", "preproc_ifdef",
                "preproc_else", "preproc_elif", "namespace_definition",
                "class_specifier", "struct_specifier", "union_specifier",
                "template_declaration", "declaration")


CALLABLE = ("function", "decl_function", "method", "decl_method")


def _declarator_ok(node) -> bool:
    """False when tree-sitter could not actually read the declarator.

    `CHECK(j == Json::object());` and `SECTION("x") { ... }` are macro calls,
    but the C++ grammar accepts both as declarations and recovers by leaving
    ERROR nodes inside the declarator.  Real declarations never carry ERROR
    there, so a name recovered from such a declarator is a guess at a macro's
    spelling rather than a function anyone can call.
    """
    d = node.child_by_field_name("declarator")
    if d is None:
        return True
    stack = [d]
    while stack:
        x = stack.pop()
        if x.type == "ERROR":
            return False
        stack.extend(x.named_children)
    return True


def _at_decl_scope(node) -> bool:
    """True when a declaration-like node sits where declarations actually live.

    `CHECK(x)` on its own line is a call, but the C++ grammar also accepts it as
    a function declaration, and nlohmann's test suite is full of assertion
    macros at statement position.  A "declaration" inside a function body or a
    macro body is an unexpanded macro call, never API, so scope decides.
    """
    return node.parent is not None and node.parent.type in _DECL_SCOPES


def _has_fn_declarator(node) -> bool:
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == "function_declarator":
            return True
        stack.extend(n.named_children)
    return False


def _include_raw(node, src):
    p = node.child_by_field_name("path")
    s = text(src, p) if p is not None else text(src, node)
    return s.strip('<>"').strip()


def _cpp_base(node, src):
    return [text(src, c) for c in node.named_children if c.type in ("type_identifier", "qualified_identifier", "template_type")]


SPEC = {}

SPEC["c"] = {
    "function_definition": ("ent", "function", _cpp_fn_prefix),
    "struct_specifier": ("ent", "struct", lambda n, s: ("", text(s, n.child_by_field_name("name")) if n.child_by_field_name("name") else "")),
    "union_specifier": ("ent", "union", lambda n, s: ("", text(s, n.child_by_field_name("name")) if n.child_by_field_name("name") else "")),
    "enum_specifier": ("ent", "enum", lambda n, s: ("", text(s, n.child_by_field_name("name")) if n.child_by_field_name("name") else "")),
    "declaration": ("ent", "decl_function", _c_proto_name, _has_proto),
    # `typedef ... Foo;` names a type the corpus then uses everywhere; without
    # it every `Foo x;`-style reference lands in the unresolved pile.
    "type_definition": ("ent", "alias", _c_alias_name),
    # A function-like macro is the one call target tree-sitter hands you with
    # no way to follow: the invocation is a call_expression, the definition is
    # a preproc node.  Indexing the definition turns `CHECK(...)` and
    # `NLOHMANN_DEFINE_TYPE_INTRUSIVE(...)` from unresolvable calls into calls
    # to something the reader can actually open.
    "preproc_function_def": ("ent", "macro", _c_macro_name),
    "call_expression": ("ref", "call", _c_call_target),
    "preproc_include": ("dep", "include", _include_raw),
}
SPEC["c"] = SPEC["c"]

cpp = dict(SPEC["c"])
cpp.update({
    "class_specifier": ("ent", "class", lambda n, s: ("", text(s, n.child_by_field_name("name")) if n.child_by_field_name("name") else "")),
    "namespace_definition": ("ent", "namespace", lambda n, s: ("", text(s, n.child_by_field_name("name")) if n.child_by_field_name("name") else "")),
    "template_declaration": ("transparent", None, None),
    "field_declaration": ("ent", "decl_method",
                          lambda n, s: name_of(n.child_by_field_name("declarator"), s),
                          lambda n: _has_fn_declarator(n)
                          and n.parent is not None
                          and n.parent.type == "field_declaration_list"),
    # `SECTION("name") { ... }` is a macro that takes a block.  The grammar has
    # no way to know that, so it reads a function named SECTION with a body, and
    # every test section becomes a phantom function plus a phantom class scope.
    # Functions are declared at translation-unit, namespace, class or linkage
    # scope; a function definition directly inside a block is a macro.
    "function_definition": ("ent", "function", _cpp_fn_prefix, _at_decl_scope),
    "new_expression": ("ref", "instantiate", _new_target),
    "alias_declaration": ("ent", "alias", _cpp_alias_name),
})
SPEC["cpp"] = cpp

SPEC["python"] = {
    "function_definition": ("ent", "function", _py_entity),
    "class_definition": ("ent", "class", _py_entity),
    "call": ("ref", "call", lambda n, s: name_of(n.child_by_field_name("function"), s)),
    "decorated_definition": ("transparent", None, None),
}

SPEC["java"] = {
    "class_declaration": ("ent", "class", _java_entity),
    "interface_declaration": ("ent", "interface", _java_entity),
    "annotation_type_declaration": ("ent", "interface", _java_entity),
    "annotation_type_element_declaration": ("ent", "method", _java_entity),
    "enum_declaration": ("ent", "enum", _java_entity),
    "method_declaration": ("ent", "method", _java_entity),
    "constructor_declaration": ("ent", "method", _java_entity),
    "method_invocation": ("ref", "call", _method_invocation),
    "object_creation_expression": ("ref", "instantiate", _new_target),
}

# decision points for cyclomatic complexity
DECISIONS = {
    "c": {"if_statement", "for_statement", "while_statement", "do_statement",
          "case_statement", "conditional_expression", "catch_clause"},
    "cpp": {"if_statement", "for_statement", "while_statement", "do_statement",
            "case_statement", "conditional_expression", "catch_clause",
            "co_await_expression"},
    "python": {"if_statement", "elif_clause", "for_statement", "while_statement",
               "except_clause", "conditional_expression", "boolean_operator",
               "for_in_clause", "if_clause", "assert_statement", "case_pattern"},
    "java": {"if_statement", "for_statement", "enhanced_for_statement", "while_statement",
             "do_statement", "ternary_expression", "catch_clause", "switch_label",
             "switch_rule"},
}
DECISIONS["c"] |= {"binary_expression"}
DECISIONS["cpp"] |= {"binary_expression"}
DECISIONS["java"] |= {"binary_expression"}


def _complexity(b: Builder, node, decisions) -> int:
    """Cyclomatic complexity of an entity subtree, plus branch operators."""
    dec = decisions
    count = 1
    stack = [node]
    src = b.src
    while stack:
        n = stack.pop()
        t = n.type
        if t in dec:
            if t == "binary_expression":
                op = n.child_by_field_name("operator")
                if op is not None and text(src, op) in ("&&", "||", "and", "or"):
                    count += 1
            elif t in ("boolean_operator", "ternary_expression", "conditional_expression"):
                count += 1
            else:
                count += 1
        stack.extend(n.named_children)
    return count


# ---------------------------------------------------------------- driver

def _drive(b: Builder, node, spec) -> None:
    """Walk the AST with an explicit work list (generated headers nest too deep for
    Python recursion: llvm intrinsic tables blow the default limit)."""
    visit = lambda n: [("visit", c) for c in reversed(n.named_children)]
    work = visit(node)
    while work:
        op, n = work.pop()
        if op == "pop":  # leaving an entity scope
            b.stack.pop()
            continue
        act = spec.get(n.type)
        if act is None:
            work.extend(visit(n))
            continue
        kind = act[0]
        if kind == "ent":
            _, ekind, namer = act[:3]
            pred = act[3] if len(act) > 3 else None
            prefix, name = ("", "") if (pred is not None and not pred(n)) else namer(n, b.src)
            if not name or (ekind in CALLABLE and not _declarator_ok(n)):
                work.extend(visit(n))
                continue
            b.stack.append(b.add_ent(ekind, name, n, prefix))
            work.append(("pop", None))
            work.extend(visit(n))
        elif kind == "ref":
            _, rkind, namer = act
            prefix, target = namer(n, b.src)
            b.add_ref(rkind, (prefix + "::" + target) if prefix and target else target,
                      n.start_point[0])
            work.extend(visit(n))
        elif kind == "dep":
            _, _dkind, getter = act
            b.add_dep(_dkind, getter(n, b.src))
            work.extend(visit(n))
        else:  # transparent wrapper (template_declaration, decorated_definition)
            work.extend(visit(n))


# ---------------------------------------------------------------- python/java deps

def _py_imports(b: Builder, root) -> None:
    src = b.src
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type == "import_statement":
            body = text(src, n).split("import", 1)[-1]
            for part in body.split(","):
                mod = part.strip().split(" as ")[0].strip()
                if mod:
                    b.add_dep("import", mod)
        elif n.type == "import_from_statement":
            m = n.child_by_field_name("module_name")
            raw = text(src, m) if m is not None else ""
            if not raw:
                raw = text(src, n).split("import")[0].replace("from", "").strip()
            b.add_dep("import", raw.lstrip("."))
        stack.extend(n.named_children)


def _py_inherits(b: Builder, root) -> None:
    src = b.src
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type == "class_definition":
            sup = n.child_by_field_name("superclasses")
            if sup is not None:
                for c in sup.named_children:
                    if c.type in ("identifier", "attribute"):
                        _, t = name_of(c, src)
                        b.add_ref("inherit", t, c.start_point[0], b.class_of(n))
        stack.extend(n.named_children)


def _cpp_inherits(b: Builder, root) -> None:
    """class Square : public Shape -> an inherit edge owned by Square."""
    src = b.src
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type in ("class_specifier", "struct_specifier"):
            clause = next((c for c in n.named_children if c.type == "base_class_clause"), None)
            if clause is not None:
                owner = b.class_of(n)
                for c in clause.named_children:
                    if c.type in ("type_identifier", "qualified_identifier",
                                  "template_type", "identifier"):
                        _, nm = name_of(c, src)
                        b.add_ref("inherit", nm, c.start_point[0], owner)
        stack.extend(n.named_children)


def _java_imports(b: Builder, root) -> None:
    src = b.src
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type == "import_declaration":
            t = text(src, n)
            raw = t.replace("import", "").replace("static", "").strip().rstrip(";")
            b.add_dep("import", raw)
        elif n.type in ("superclass", "super_interfaces"):
            for c in n.named_children:
                if c.type in ("type_identifier", "scoped_type_identifier", "type_list"):
                    for t in ([c] if c.type != "type_list" else c.named_children):
                        _, nm = name_of(t, src)
                        b.add_ref("inherit", nm, t.start_point[0], b.class_of(t) or b.class_of(n))
        stack.extend(n.named_children)


def extract(path: str, lang: str, src: bytes) -> Builder:
    spec = SPECS[lang]
    parser = ts.Parser(language(lang))
    tree = parser.parse(src)
    root = tree.root_node
    b = Builder(path, lang, src)
    _drive(b, root, spec.spec)
    # imports/inheritance come after the main walk: inherit edges want the
    # owning class entity to already exist.  Which passes run, and in what
    # order, is the language record's business, not this function's.
    for post in spec.post:
        post(b, root)
    for e in b.ents:
        if e["kind"] in ("function", "method"):
            e["complexity"] = _complexity(b, e["_node"], spec.decisions)
    return b


# ---------------------------------------------------------------- externals
#
# Names that are never defined by any tree we will index, so a reference to one
# is "external" rather than "unresolved".  This is the honest answer to "where
# did this call go", and it is data about a language, not about the resolver.

# Runtime names common enough to appear in any corpus: Python builtins and
# container methods, POSIX I/O, and the handful of C entry points that every
# language's runtime happens to share.  Shared on purpose: `open`/`read`/`close`
# are as external in C as they are in Python, and a corpus that defines them
# defines them as entities anyway, which beats this set.
COMMON_EXTERNAL = frozenset("""
abs all any ascii bin bool breakpoint bytearray bytes callable chr classmethod
compile complex delattr dict dir divmod enumerate eval exec filter float format
frozenset getattr globals hasattr hash help hex id input int isinstance
issubclass iter len list locals map max memoryview min next object oct open ord
pow print property range repr reversed round set setattr slice sorted staticmethod
str sum super tuple type vars zip __import__ __name__
append extend insert remove pop keys values items get update split join strip
lstrip rstrip replace startswith endswith format encode decode read write close
""".split())

LIBC = frozenset("""
malloc calloc realloc free memcpy memmove memset memcmp memchr strlen strnlen
strcpy strncpy strcat strncat strcmp strncmp strchr strrchr strstr strtok strdup
strerror sprintf snprintf vsprintf vsnprintf sscanf printf fprintf vfprintf
puts fputs putchar fputc fgetc fgets fopen fclose fread fwrite fflush fseek
ftell rewind feof ferror remove rename tmpfile tmpnam perror
atoi atol atoll atof strtol strtoul strtoll strtoull strtod strtof
abs labs llabs div ldiv qsort bsearch rand srand rand_r exit abort atexit
getenv setenv unsetenv system sleep usleep getcwd chdir mkdir rmdir unlink
isalpha isdigit isalnum isspace isupper islower isprint ispunct isxdigit
toupper tolower toascii
sin cos tan asin acos atan atan2 sinh cosh tanh exp log log10 log2 pow sqrt
ceil floor fabs fmod round trunc fmin fmax fdim hypot
time clock difftime mktime localtime gmtime strftime ctime
va_start va_end va_arg va_copy assert offsetof
open close read write lseek stat fstat ioctl socket bind listen accept connect
send recv pthread_create pthread_join pthread_mutex_lock pthread_mutex_unlock
""".split())

# Names that belong to the Java platform rather than to any given tree.  Only
# types and the handful of static/framework entry points that show up as bare
# call targets are listed; everything else is still "unresolved", which is the
# honest answer.
JAVA_PLATFORM = frozenset("""
Object String Integer Long Short Byte Double Float Boolean Character Number
StringBuilder StringBuffer CharSequence Comparable Iterable Runnable Thread
Exception RuntimeException Error Throwable IllegalArgumentException
IllegalStateException NullPointerException UnsupportedOperationException
IndexOutOfBoundsException NumberFormatException AssertionError Class Object
ArrayList LinkedList HashMap LinkedHashMap TreeMap HashSet LinkedHashSet
TreeSet Arrays Collections List Map Set Collection Iterator Optional Objects
BigDecimal BigInteger Date Calendar Locale UUID
StringWriter StringReader BufferedReader InputStreamReader File InputStream
OutputStream PrintStream IOException
Character StringJoiner Enum EnumSet EnumMap Comparator Spliterator Stream
Collectors Matcher Pattern AtomicInteger AtomicLong AtomicReference
WeakHashMap IdentityHashMap Properties Random Scanner Formatter
requireNonNull getClass hashCode toString equals clone notifyAll
""".split())

# Assertion and mocking DSLs are the other half of a Java test corpus: the call
# target is a static import, so nothing in the tree carries the name.
JAVA_DSL = frozenset("""
assertThat assertThrows assertDoesNotThrow assertThatCode assertThatException
assertThatThrownBy fail failBecauseExceptionWasNotThrown
isEqualTo isNotEqualTo isSameAs isNotSameAs isNull isNotNull isTrue isFalse
isInstanceOf isNotInstanceOf isAnything isA isIn isNotIn isGreaterThan
isLessThan isAtLeast isAtMost isEmpty isNotEmpty hasSize hasMessage
hasMessageThat hasMessageContaining hasCause hasToString hasSameHashCodeAs
contains containsExactly containsExactlyInAnyOrder containsAll containsInAnyOrder
doesNotContain startsWith endsWith matches startsWithIgnoringCase
containsExactlyElementsIn inOrder not isNot that named describedAs
assertEquals assertNotEquals assertTrue assertFalse assertNull assertNotNull
assertArrayEquals assertSame assertNotSame assertSameElements
verify when thenReturn thenThrow mock spy given expectAny
""".split())


# ---------------------------------------------------------------- the registry
#
# One record per language, and the only place a language is named.  Adding one
# means adding a record here: extensions, grammar, parse table, complexity node
# types, post-walk passes, external names, module spelling and a fixture all
# arrive together, so a language cannot be half-registered.  Nothing outside
# this file may branch on a language name; the resolver, the CLI and the front
# end ask the record instead.  `ken langs --check` runs the fixture through
# extract() and reports, which makes "wired" a test result rather than a claim.


@dataclass(frozen=True)
class LangSpec:
    #: file extensions claimed, lower-case with the dot
    exts: tuple[str, ...]
    #: the tree-sitter grammar binding module
    grammar: object
    #: parse table: node type -> ("ent"|"ref"|"dep"|"transparent", ...)
    spec: dict
    #: node types counted as branch points by _complexity
    decisions: frozenset[str]
    #: node types in which a declaration may legitimately sit (C/C++ scope
    #: guards); empty when the language has no such ambiguity
    decl_scopes: tuple[str, ...] = ()
    #: passes to run after the main walk, in order (imports, inheritance)
    post: tuple = ()
    #: names that provably come from outside the tree's runtime
    external: frozenset[str] = frozenset()
    #: True when `.` separates module components, so `a.b` may be the file a/b
    module_paths: bool = False
    #: extra suffixes tried when resolving an import to a file
    import_suffixes: tuple[str, ...] = ()
    #: languages whose entities resolve against each other (C and C++ share a
    #: name space because they interoperate)
    family: str = ""
    #: minimal source that must yield entities and at least one reference
    fixture: str = ""


SPECS: dict[str, LangSpec] = {
    "c": LangSpec(
        exts=(".c", ".h"),
        grammar=tree_sitter_c,
        spec=SPEC["c"],
        decisions=frozenset(DECISIONS["c"]),
        decl_scopes=_DECL_SCOPES,
        post=(_cpp_inherits,),
        external=COMMON_EXTERNAL | LIBC,
        family="c",
        fixture="""
static int add(int a, int b);

int add(int a, int b) { return a + b; }

int main(void) { return add(1, 2); }
""",
    ),
    "cpp": LangSpec(
        exts=(".cc", ".cpp", ".cxx", ".c++", ".hpp", ".hh", ".hxx", ".h++", ".ipp"),
        grammar=tree_sitter_cpp,
        spec=SPEC["cpp"],
        decisions=frozenset(DECISIONS["cpp"]),
        decl_scopes=_DECL_SCOPES,
        post=(_cpp_inherits,),
        external=COMMON_EXTERNAL | LIBC,
        family="c",
        fixture="""
struct Shape { virtual double area() const = 0; };

struct Square : public Shape {
    double s;
    double area() const override { return s * s; }
};

int main() { Square q{2.0}; return (int)q.area(); }
""",
    ),
    "python": LangSpec(
        exts=(".py", ".pyi"),
        grammar=tree_sitter_python,
        spec=SPEC["python"],
        decisions=frozenset(DECISIONS["python"]),
        post=(_py_imports, _py_inherits),
        external=COMMON_EXTERNAL,
        module_paths=True,
        import_suffixes=(".py", "/__init__.py"),
        fixture="""
import os


class Base:
    def run(self):
        return helper()


def helper():
    return os.getcwd()
""",
    ),
    "java": LangSpec(
        exts=(".java",),
        grammar=tree_sitter_java,
        spec=SPEC["java"],
        decisions=frozenset(DECISIONS["java"]),
        post=(_java_imports,),
        external=COMMON_EXTERNAL | JAVA_PLATFORM | JAVA_DSL,
        module_paths=True,
        import_suffixes=(".java",),
        fixture="""
package demo;

import java.util.List;

public class Greeter extends Base {
    public String greet(List<String> names) { return names.get(0); }
}
""",
    ),
}


# Derived views.  Callers that only need "which language is this file" or "the
# grammar object" keep working; anything that wants a language fact should hold
# the record instead.

EXTS = {ext: name for name, s in SPECS.items() for ext in s.exts}
MODULES = {name: s.grammar for name, s in SPECS.items()}


def spec_for(lang: str) -> LangSpec:
    """The record for a language, or KeyError with a useful message."""
    try:
        return SPECS[lang]
    except KeyError:
        raise KeyError(f"unregistered language {lang!r}; "
                       f"registered: {sorted(SPECS)}") from None


