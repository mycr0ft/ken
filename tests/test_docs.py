# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Gates for reading documentation out of source, not just code.

An indexer that knows a function exists but not what its author said about it
answers the wrong question.  These tests pin one doc convention per grammar:
where the comment sits, what the cleaned text looks like, and whether a
`@see`/`@link` becomes an edge in the graph.

Nothing here runs Doxygen or Javadoc.  The claim under test is that the
comment shapes are regular enough to read with tree-sitter alone.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))

C_H = """\
/**
 * @brief Open the device by name.
 *
 * The name is copied, so the caller keeps ownership.
 *
 * @param name NUL-terminated device name.
 * @return a handle, or -1 when the name is unknown.
 * @see dev_close
 */
int dev_open(const char *name);

/** Close a handle from dev_open(). */
int dev_close(int handle);
"""

C_C = """\
#include "dev.h"

int dev_open(const char *name) {
    return dev_close(0) + (int)name[0];
}

int dev_close(int handle) { return handle; }
"""

CPP = r"""\
/// @file box.cpp
/// @brief Boxes in a header-only library.

#include "dev.h"

namespace geo {

struct Box {
    /// Grow the box on every side.  \sa shrink
    void grow(int by);
    void shrink(int by);
};

}  // namespace geo
"""

JAVA = """\
package demo;

/** A counter that saturates instead of wrapping. */
public class Counter {

    /**
     * Add one, unless the counter is already at its maximum.
     *
     * @param strict throw when saturated rather than returning
     * @return the new value
     * @see #reset()
     * @deprecated use {@link Tally} instead
     */
    @Deprecated
    public int bump(boolean strict) {
        return 0;
    }

    /** Put the counter back to zero. */
    public void reset() {}
}
"""

PY = '''\
def parse(text):
    """Parse a document.

    The text is copied, so the caller keeps ownership.

    Args:
        text: the document to parse.

    Returns:
        a node tree.
    """
    return text
'''

GO = """\
package demo

// Bump adds one to n.
//
// Bump is safe for concurrent use.
func Bump(n int) int {
	return n + 1
}
"""

JS = """\
/**
 * Join two path segments.
 *
 * @param {string} a the left side
 * @param {string} b the right side
 * @returns {string} the joined path
 */
function join(a, b) {
  return a + '/' + b;
}
"""


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    root = tmp_path_factory.mktemp("docs")
    (root / "dev.h").write_text(C_H)
    (root / "dev.c").write_text(C_C)
    (root / "box.cpp").write_text(CPP)
    (root / "Counter.java").write_text(JAVA)
    (root / "parse.py").write_text(PY)
    (root / "bump.go").write_text(GO)
    (root / "join.js").write_text(JS)
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


def doc_of(con, name):
    rows = q(con, "SELECT doc FROM entities WHERE name = ? AND doc <> '' "
                  "ORDER BY decl DESC", name)
    assert rows, f"no doc attached to {name}"
    return rows[0][0]


def test_c_block_comment_becomes_doc(con):
    """A Doxygen block above a prototype is that prototype's documentation."""
    doc = doc_of(con, "dev_open")
    assert "Open the device by name" in doc
    assert "caller keeps ownership" in doc
    # the tag lines survive as text; the renderer decides how to present them
    assert "@param" in doc and "@return" in doc
    # decoration is gone: no leading asterisks on every line
    assert "* " not in doc.replace(" * ", " ? "), doc


def test_cpp_triple_slash_is_doc(con):
    """`///` is a doc comment in Doxygen; keep it out of the code path."""
    doc = doc_of(con, "grow")
    assert "Grow the box on every side" in doc
    assert "///" not in doc, doc
    assert "\\sa shrink" in doc, doc


def test_java_javadoc_keeps_params_and_links(con):
    doc = doc_of(con, "bump")
    assert "Add one" in doc
    assert "@param strict" in doc
    assert "@deprecated" in doc
    # {@link Tally} is a reference this corpus does not define: text stays,
    # no edge is invented
    links = q(con, "SELECT target, target_id FROM doc_links WHERE target LIKE '%Tally%'")
    assert not links or all(r[1] is None for r in links), links


def test_java_see_resolves_to_the_sibling_method(con):
    """`@see #reset()` is a real edge and the corpus defines the target."""
    rows = q(con, """
        SELECT d.target, d.target_id FROM doc_links d
        JOIN entities s ON s.id = d.src_entity
        WHERE s.name = 'bump'""")
    assert rows, "no doc links recorded for bump"
    reset = [r for r in rows if "reset" in r[0]]
    assert reset, rows
    assert all(r[1] is not None for r in reset), reset


def test_python_docstring_is_doc_not_body(con):
    doc = doc_of(con, "parse")
    assert "Parse a document" in doc
    assert "caller keeps ownership" in doc
    assert '"""' not in doc, doc


def test_godoc_text_arrives_with_the_grammar(con):
    """GoDoc is the same comment shape as Javadoc's `//`, so once `.go` has a
    language record its comments are read by the existing pipeline."""
    rows = q(con, "SELECT name, doc FROM entities WHERE name = 'Bump'")
    assert rows, "bump.go is not indexed even though go is wired"
    assert "adds one to n" in rows[0][1], rows


def test_jsdoc_still_waits_for_its_grammar(con):
    """JSDoc is regular enough too, but `.js` has no language record yet, and a
    file that is silently indexed as empty teaches the user something false."""
    rows = q(con, "SELECT 1 FROM files WHERE path LIKE '%join.js'")
    assert not rows, "join.js is indexed without a language table entry"


def test_entities_without_docs_say_so(con):
    """Absence must be an empty string, never the neighbouring comment."""
    rows = q(con, "SELECT name, doc FROM entities WHERE name IN ('shrink', 'reset')")
    assert rows, rows
    for name, doc in rows:
        assert doc == "" or name not in ("shrink",), (name, doc)


def test_prose_is_searchable(con):
    """`ken web` doc search: find a symbol by what was written about it."""
    hits = q(con, "SELECT rowid FROM doc_fts WHERE doc_fts MATCH 'ownership'")
    assert hits, "doc_fts returned nothing for a word that only appears in prose"
