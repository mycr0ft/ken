# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Gates for the four answers a name can have that are neither one definition
nor a coin toss.

An indexer that only knows "resolved" and "ambiguous" puts three different
failures under one word: a name reached through an object whose type it never
saw, a macro that is settled by textual inclusion order, and a declaration site
carrying several signatures.  Each is a different statement to a reader, and
each is measured on nlohmann-json; see docs/corpora.md.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys

import pytest

# `tick()` is inherited and reachable; `size()` collides with members of types
# in other directories, so nothing at the call site answers to it.
CPP_A = """\
namespace t {

struct base {
    int tick() { return 1; }
};

struct leaf : base {
    void call() {
        tick();
        size();
        clamp(1);
        clamp(2.0);
    }
};

int clamp(int v) { return v; }
int clamp(double v) { return (int)v; }

}
"""

# The same-named members the bare calls cannot reach: other directories, so the
# same-file and same-package shortcuts do not fire either.
CPP_B = """\
namespace other {

struct blob {
    int size() { return 0; }
    int tick() { return 0; }
};

}
"""

CPP_D = """\
namespace more {

struct thing {
    int size() { return 0; }
    int tick() { return 0; }
};

}
"""

# Two headers, one macro name: the last definition wins at inclusion time and
# nothing here knows that order.
LIMIT_H = "#define BOUND(x) (x)\n"
LIMIT2_H = "#define BOUND(x) ((x) + 1)\n"

CPP_C = """\
#include "limit.h"
#include "limit2.h"

namespace t {

int bounded(int x) { return BOUND(x); }

}
"""

JAVA = """\
public class Pt {
    public Pt(int x) { this.x = x; }
    public Pt(int x, int y) { this.x = x + y; }
    int x;
}

class Use {
    void f() {
        Pt p = new Pt(1);
    }
}
"""


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    root = tmp_path_factory.mktemp("classes")
    (root / "a.cpp").write_text(CPP_A)
    (root / "limit.h").write_text(LIMIT_H)
    (root / "limit2.h").write_text(LIMIT2_H)
    (root / "c.cpp").write_text(CPP_C)
    (root / "lib").mkdir()
    (root / "lib" / "b.cpp").write_text(CPP_B)
    (root / "lib" / "deeper").mkdir()
    (root / "lib" / "deeper" / "d.cpp").write_text(CPP_D)
    (root / "Pt.java").write_text(JAVA)
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


def resolutions(con, target, kind="call"):
    rows = q(con, "SELECT DISTINCT resolution FROM refs WHERE target = ? AND kind = ?",
             target, kind)
    return {r[0] for r in rows}


def test_bare_call_on_an_unknown_receiver_is_dispatch(con):
    """`size()` inside `leaf::call` names a member of something the parse never
    typed.  The corpus does define a `size`, in `other::blob`, which is not a
    candidate the call site could ever reach."""
    assert resolutions(con, "size") == {"dispatch"}, resolutions(con, "size")


def test_an_inherited_member_is_still_reachable(con):
    """The rung above must not swallow real inheritance: `tick` is declared in
    the base of the class the call is written in."""
    assert resolutions(con, "tick") == {"scoped"}, resolutions(con, "tick")


def test_a_macro_name_is_preprocessor_not_ambiguous(con):
    """A function-like macro reads as a call in the parse; its name is settled by
    inclusion order, not by scope, so no scope walk is entitled to an answer."""
    assert resolutions(con, "BOUND") == {"preprocessor"}, resolutions(con, "BOUND")


def test_one_declaration_site_with_several_signatures_is_overload(con):
    """Choosing between `clamp(int)` and `clamp(double)` is argument typing, so
    the site is known and the body is not."""
    assert resolutions(con, "clamp") == {"overload"}, resolutions(con, "clamp")


def test_a_type_reference_is_not_outvoted_by_its_constructors(con):
    """`new Pt(1)` names the class; the two constructors carry the same name and
    would otherwise make the reference look ambiguous."""
    rows = q(con, "SELECT r.resolution, e.kind FROM refs r "
                  "JOIN entities e ON e.id = r.target_id "
                  "WHERE r.target = 'Pt' AND r.kind = 'instantiate'")
    assert rows, "the instantiation of Pt was not indexed"
    assert {r[0] for r in rows} <= {"exact", "scoped", "unique"}, rows
    assert {r[1] for r in rows} == {"class"}, rows


def test_the_classes_are_counted_where_the_caller_can_read_them(con):
    """Whatever the resolver decides, the index has to agree with itself."""
    rows = q(con, "SELECT resolution, count(*) FROM refs GROUP BY 1")
    seen = {r[0] for r in rows}
    assert {"dispatch", "preprocessor", "overload"} <= seen, sorted(seen)
    assert sum(n for _, n in rows) == q(con, "SELECT count(*) FROM refs")[0][0]
