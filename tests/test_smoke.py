"""End-to-end gate: index the fixture tree, then assert cross-file call edges."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    path = tmp_path_factory.mktemp("ken") / "t.db"
    r = subprocess.run([sys.executable, "-m", "ken.cli", "--db", str(path), "index", FIX],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return str(path)


def run(db, *args):
    r = subprocess.run([sys.executable, "-m", "ken.cli", "--db", db, *args],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_every_language_indexed(db):
    out = run(db, "summary")
    assert "files" in out
    assert int(out.split("files")[1].split()[0]) == 7  # 2 c, 2 cpp, 2 py, 1 java


def test_callers_of_helper(db):
    out = run(db, "callers", "helper")
    assert "main" in out
    # four languages each define/call a helper
    for lang_dir in ("c/calc.c", "cpp/calc.cpp", "py/calc.py", "java/Calc.java"):
        assert lang_dir in out, f"missing caller in {lang_dir}\n{out}"


def test_callees_of_main(db):
    out = run(db, "callees", "main")
    assert "helper" in out


def test_java_branching_complexity(db):
    out = run(db, "metrics", "--top", "20")
    assert "Calc.main" in out or "helper" in out


def test_declaration_vs_definition(db):
    out = run(db, "sym", "scale")
    assert "(decl)" in out, out
    assert "Shape::scale" in out


def test_fts_find(db):
    out = run(db, "find", "scale")
    assert "Shape::scale" in out


def test_file_deps(db):
    out = run(db, "deps", "calc.c")
    assert "util.h" in out
