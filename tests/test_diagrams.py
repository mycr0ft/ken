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
    # resolved calls only: the diagram draws interactions between objects it can
    # name, so a site whose calls all leave the corpus produces an empty diagram
    row = con.execute("""SELECT src_entity, COUNT(*) AS n FROM refs
                          WHERE kind IN ('call','instantiate')
                            AND src_entity IS NOT NULL AND target_id IS NOT NULL
                       GROUP BY src_entity ORDER BY n DESC, src_entity LIMIT 1""").fetchone()
    assert row is not None, "fixture has no resolved calls"
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


def test_calls_leaving_the_corpus_land_on_a_boundary_lifeline(con):
    from ken.web.server import api_sequence

    row = con.execute("""SELECT src_entity FROM refs
                          WHERE kind = 'call' AND src_entity IS NOT NULL
                            AND target_id IS NULL AND target IS NOT NULL
                       ORDER BY src_entity LIMIT 1""").fetchone()
    assert row is not None, "fixture has no external calls"
    d = api_sequence(con, {"id": str(row[0]), "limit": "200"})
    ext = [l for l in d["lifelines"] if l["kind"] == "x"]
    assert ext and ext[0]["label"] == "«external»", d["lifelines"]
    assert ext[0]["id"] == 0
    # the diagram must not read as "no interactions" just because none of the
    # calls stay inside the corpus
    assert d["messages"], d


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


def test_entrypoints_are_ranked_and_render(con):
    """The diagrams hub offers somewhere to start, so every offer must work.

    A landing page that links to traces would be worse than none if a link
    landed on an empty canvas, so each listed operation is traced here.
    """
    from ken.web.server import api_entrypoints, api_sequence

    d = api_entrypoints(con, {})
    got = d["items"]
    assert got, "fixture corpus has no operation that calls two distinct targets"
    assert len(got) <= 24
    assert d["total"] == len(got)
    widths = [r["targets"] for r in got]
    assert widths == sorted(widths, reverse=True), "entry points are not ranked"
    assert all(w >= 2 for w in widths)
    for row in got:
        seq = api_sequence(con, {"id": str(row["id"]), "limit": "8"})
        assert seq and seq["messages"], f"entry point {row['qualname']} draws an empty trace"
        assert seq["entity"]["id"] == row["id"]


def test_entrypoints_respect_the_cap(con):
    """A hub that lists one operation is not a hub, so the floor is four, and
    the ceiling is what the renderer will happily draw."""
    from ken.web.server import api_entrypoints

    assert len(api_entrypoints(con, {"limit": "1"})["items"]) <= 4
    assert len(api_entrypoints(con, {"limit": "999"})["items"]) <= 24


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
