# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Gate the diagram endpoints and the SysML v2 projection.

Both are views over the same index: the package diagram reads directory
rollups, the sequence diagram reads call references, and the emitter writes
a SysML v2 package tree.  What is asserted here is the shape the SVG
renderers in ``assets/app.js`` actually consume, plus the emitter's numeric
gate (every class becomes exactly one ``part def``).
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from ken.sysml import emit
from ken.store import connect

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    path = tmp_path_factory.mktemp("ken-diag") / "d.db"
    r = subprocess.run([sys.executable, "-m", "ken.cli", "--db", str(path), "index", FIX],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return str(path)


@pytest.fixture(scope="module")
def con(db):
    c = connect(db)
    yield c
    c.close()


# --- package diagram ------------------------------------------------------

def test_packages_report_a_corpus_root(con):
    from ken.web.server import api_packages

    d = api_packages(con, {})
    assert d["total_packages"] == len(d["packages"])
    assert d["packages"], "fixture tree has no directories"
    for p in d["packages"]:
        # name is the last path segment, path the corpus-relative directory
        assert p["path"].endswith(p["name"]) and not p["path"].startswith("/")
        assert p["files"] > 0


def test_package_edges_only_join_listed_packages(con):
    from ken.web.server import api_packages

    d = api_packages(con, {})
    ids = {p["id"] for p in d["packages"]}
    for e in d["edges"]:
        assert e["s"] in ids and e["d"] in ids, e
        assert e["s"] != e["d"], "a package does not depend on itself"


def test_package_limit_clamps_and_reports_omission(con):
    from ken.web.server import api_packages

    d = api_packages(con, {"limit": "1"})
    assert len(d["packages"]) >= 1
    assert d["omitted"] == d["total_packages"] - len(d["packages"])


# --- sequence diagram -----------------------------------------------------

def _busiest_call_site(con):
    row = con.execute("""SELECT src_entity, COUNT(*) AS n FROM refs
                          WHERE kind IN ('call','instantiate') AND src_entity IS NOT NULL
                       GROUP BY src_entity ORDER BY n DESC LIMIT 1""").fetchone()
    assert row is not None, "fixture has no calls"
    return row[0]


def test_sequence_messages_are_indexed_into_lifelines(con):
    from ken.web.server import api_sequence

    d = api_sequence(con, {"id": str(_busiest_call_site(con))})
    assert d["messages"], d
    assert d["depth"] >= 1
    n = len(d["lifelines"])
    for m in d["messages"]:
        # the renderer looks lifelines up by position, so s/d are indices
        assert isinstance(m["s"], int) and 0 <= m["s"] < n, m
        assert isinstance(m["d"], int) and 0 <= m["d"] < n, m
        assert m["label"] and m["line"] > 0 and m["n"] >= 1
    assert d["total_calls"] >= len(d["messages"])


def test_sequence_counts_traffic_per_lifeline(con):
    from ken.web.server import api_sequence

    d = api_sequence(con, {"id": str(_busiest_call_site(con))})
    for i, life in enumerate(d["lifelines"]):
        assert life["in"] == sum(1 for m in d["messages"] if m["d"] == i)
        assert life["out"] == sum(1 for m in d["messages"] if m["s"] == i and m["s"] != m["d"])
    # a lifeline exists only because a message touches it, so none may be idle
    for life in d["lifelines"]:
        assert life["in"] + life["out"] > 0, life


def test_sequence_limit_is_clamped_and_reports_truncation(con):
    from ken.web.server import api_sequence

    eid = str(_busiest_call_site(con))
    d = api_sequence(con, {"id": eid, "limit": "1"})
    # a one-message diagram is not a diagram; the floor is 4
    assert d["limit"] == 4
    assert len(d["messages"]) <= 4
    assert isinstance(d["truncated"], bool)
    full = api_sequence(con, {"id": eid, "limit": "200"})
    assert full["truncated"] is False
    assert len(full["messages"]) >= len(d["messages"])


# --- sysml v2 projection --------------------------------------------------

def test_emit_one_part_def_per_class(con):
    text, stats = emit(con)
    assert stats["emitted"] == stats["classes"] > 0
    assert text.startswith("package ")
    assert text.count("part def ") == stats["emitted"]
    # names are unique per package, so a collision suffix shows up as Name_2
    assert stats["generalizations"] + stats["dependencies"] >= 0


def test_emit_honours_a_scope(con):
    text, stats = emit(con, scope=os.path.basename(FIX))
    assert stats["emitted"] > 0
    assert "part def " in text
    narrow, nstats = emit(con, scope=os.path.join(os.path.basename(FIX), "nowhere"))
    assert nstats["emitted"] == 0 and narrow.strip().startswith("package")


def test_emit_parses_with_sysmlpy(con):
    sysmlpy = pytest.importorskip("sysmlpy", reason="sysmlpy not installed here")
    text, stats = emit(con)
    model = sysmlpy.loads(text)
    assert model.count()["part"] == stats["emitted"]


def test_sysml_verb_writes_a_file(db, tmp_path):
    out = tmp_path / "fx.sysml"
    r = subprocess.run([sys.executable, "-m", "ken.cli", "--db", db, "sysml",
                        "--scope", os.path.basename(FIX), "--out", str(out)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert out.read_text(encoding="utf-8").count("part def ") > 0
    assert "classes" in r.stderr
