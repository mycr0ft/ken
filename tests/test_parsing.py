# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Gates for the C and C++ shapes a real corpus throws at an indexer.

Each test names one thing tree-sitter gets wrong on its own: a macro call that
looks like a declaration, a typedef nobody can call, a prototype that is the
only definition of an API.  The numbers these protect are the ones measured on
cJSON and nlohmann-json; see docs/corpora.md.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))

C_H = """\
#ifndef API_H
#define API_H

typedef unsigned long api_id;

#define API_MAX(a, b) ((a) > (b) ? (a) : (b))

int api_open(const char *name);
int api_shut(int handle);

#endif
"""

C_C = """\
#include "api.h"

int api_open(const char *name) { return (int)name[0]; }

int api_run(void) {
    struct local { int x; };
    struct local v;
    v.x = api_shut(1);
    return API_MAX(v.x, 2);
}
"""

CPP = """\
namespace t {

struct box {
    int w;
    int area() const { return w; }
    int grow(int by) { w += by; return w; }
};

SECTION("group") {
    CHECK(box{1}.area() == 1);
    box b;
    b.grow(3);
}

}
"""


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    root = tmp_path_factory.mktemp("corpus")
    (root / "api.h").write_text(C_H)
    (root / "api.c").write_text(C_C)
    (root / "box.cpp").write_text(CPP)
    path = root / "k.db"
    r = subprocess.run([sys.executable, "-m", "ken.cli", "--db", str(path),
                        "index", str(root)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return str(path)


@pytest.fixture(scope="module")
def con(db):
    c = sqlite3.connect(db)
    yield c
    c.close()


def q(con, sql, *args):
    return con.execute(sql, args).fetchall()


def test_header_prototype_is_an_entity(con):
    """The header is the half of the API a reader meets first."""
    rows = q(con, "SELECT e.name, e.decl, f.path FROM entities e JOIN files f "
                  "ON f.id = e.file_id WHERE e.name = 'api_open' ORDER BY e.decl DESC")
    protos = [r for r in rows if r[0] == "api_open" and r[2].endswith("api.h")]
    assert protos, f"api_open prototype missing from api.h: {rows}"
    assert all(int(r[1]) == 1 for r in protos), rows
    # and the definition in the .c is not marked as a declaration
    impl = [r for r in rows if r[2].endswith("api.c")]
    assert impl and all(int(r[1]) == 0 for r in impl), rows


def test_block_scope_lookalike_is_not_a_declaration(con):
    """`struct local v;` and `v.x = ...` are statements, not entity names."""
    names = {r[0] for r in q(con, "SELECT name FROM entities")}
    assert "v" not in names, sorted(names)
    # the nested type itself is real, and it keeps its own kind
    kinds = {r[0] for r in q(con, "SELECT kind FROM entities WHERE name = 'local'")}
    assert kinds <= {"struct", "class"}, kinds


def test_typedef_is_an_alias(con):
    rows = q(con, "SELECT kind FROM entities WHERE name = 'api_id'")
    assert rows and rows[0][0] == "alias", rows


def test_function_macro_is_an_entity(con):
    rows = q(con, "SELECT kind, name FROM entities WHERE name = 'API_MAX'")
    assert rows and rows[0][0] == "macro", rows


def test_macro_call_at_class_scope_makes_no_entities(con):
    """`SECTION("g") { ... }` is a macro call, not a function with a body.

    Reading it as a declaration invents a function named SECTION and a member
    per assertion, and then every CHECK in the corpus resolves to a fake.
    """
    names = {r[0] for r in q(con, "SELECT name FROM entities")}
    assert "SECTION" not in names, sorted(names)
    assert "CHECK" not in names, sorted(names)


def test_real_members_survive_next_to_the_macro_call(con):
    rows = q(con, "SELECT e.name, e.kind FROM entities e WHERE e.name IN ('area','grow')")
    assert {r[0] for r in rows} == {"area", "grow"}, rows
    assert all(r[1] == "method" for r in rows), rows


def test_in_tree_calls_resolve(con):
    rows = q(con, "SELECT target, resolution, target_id FROM refs "
                  "WHERE kind = 'call' AND target IN ('api_shut','area','grow')")
    assert rows, "no calls indexed at all"
    for target, resolution, tid in rows:
        assert tid is not None, f"{target} -> {resolution}"
        assert resolution in ("exact", "scoped", "unique"), (target, resolution)


def test_a_name_the_corpus_never_defines_is_external(con):
    """`CHECK` is defined outside the tree, so it is external, not a failure."""
    rows = q(con, "SELECT resolution FROM refs WHERE target = 'CHECK'")
    assert rows, "the CHECK call was not indexed"
    assert {r[0] for r in rows} == {"external"}, rows
