# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Gate the read-only JSON API behind the web front end.

Every endpoint is a pure function of the index, so these tests talk to
them directly over a real HTTP server on an ephemeral port rather than
importing internals: the contract that matters is the wire format the
SPA consumes.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.request

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    from ken.web.server import Handler, KenServer

    path = tmp_path_factory.mktemp("ken-web") / "t.db"
    r = subprocess.run([sys.executable, "-m", "ken.cli", "--db", str(path), "index", FIX],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr

    httpd = KenServer(("127.0.0.1", 0), Handler, str(path))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def get(server, path):
    with urllib.request.urlopen(server + path, timeout=10) as r:
        assert r.status == 200
        return json.loads(r.read().decode())


def test_overview_counts_files(server):
    d = get(server, "/api/uml")
    assert len(d["classes"]) == d["shown"] > 0
    assert d["total_classes"] >= d["shown"]


def test_uml_edge_endpoints_are_selected_classes(server):
    # An edge to a class the selection dropped would draw a dangling line, so
    # every endpoint has to be one of the boxes on the canvas.
    d = get(server, "/api/uml?limit=8")
    ids = {c["id"] for c in d["classes"]}
    assert d["edges"], "fixture classes share no relation"
    for e in d["edges"]:
        assert e["s"] in ids and e["d"] in ids, e


def test_uml_omits_a_whole_directory_when_asked(server):
    whole = get(server, "/api/uml?limit=40")
    scoped = get(server, "/api/uml?path=cpp&limit=40")
    assert scoped["total_classes"] < whole["total_classes"]
    assert all("cpp" in c["path"] for c in scoped["classes"]), scoped["classes"]


def test_uml_limit_is_clamped(server):
    d = get(server, "/api/uml?limit=999")
    assert d["shown"] <= 80
    assert len(d["classes"]) == d["shown"]


def test_graph_route_reports_edges(server):
    seed = get(server, "/api/search?q=main&in=symbols")["items"][0]["id"]
    d = get(server, f"/api/graph?id={seed}&dir=down&depth=2")
    assert d["nodes"] and d["edges"]
    ids = {n["id"] for n in d["nodes"]}
    assert all(e["s"] in ids and e["d"] in ids for e in d["edges"])


def test_symbol_and_source_round_trip(server):
    hit = get(server, "/api/search?q=main&in=symbols")["items"][0]
    sym = get(server, f"/api/symbol?id={hit['id']}")
    assert sym["entity"]["name"] == hit["name"]
    assert "out" in sym and "callers" in sym
    page = get(server, f"/api/file?id={hit['file_id']}")
    assert page["file"]["path"].endswith(hit["path"].rsplit("/", 1)[-1])
    assert page["source"], "file page must carry the source the SPA renders"
