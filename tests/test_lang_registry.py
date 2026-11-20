# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The language registry: every record complete, and no language named elsewhere.

Adding a language used to mean remembering nine separate places in three files,
and forgetting one produced an index that silently omitted files.  The registry
makes a language a single record, so the questions worth testing are different:
is each record whole, do the fixtures actually parse, and does anything outside
langs.py still know a language by name?

That last one is the property the refactor exists for.  A resolver that branches
on `fam == "java"` cannot be extended without editing it, so if this test ever
fails on a second file the registry has started to rot.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ken import cli, langs

PKG = Path(langs.__file__).parent


def test_every_record_carries_a_whole_language():
    for name, s in langs.SPECS.items():
        assert s.exts, f"{name}: no file extensions"
        assert all(e.startswith(".") and e.islower() for e in s.exts), name
        assert s.grammar is not None, f"{name}: no grammar module"
        assert s.spec, f"{name}: empty parse table"
        assert s.decisions, f"{name}: complexity would always be 1"
        assert s.fixture.strip(), f"{name}: no fixture, so nothing proves it parses"


def test_no_extension_is_claimed_twice():
    seen: dict[str, str] = {}
    for name, s in langs.SPECS.items():
        for ext in s.exts:
            assert ext not in seen, f"{ext} claimed by both {seen[ext]} and {name}"
            seen[ext] = name


def test_derived_views_match_the_records():
    """EXTS and MODULES are conveniences over SPECS, not a second source of truth."""
    assert langs.EXTS == {e: n for n, s in langs.SPECS.items() for e in s.exts}
    assert langs.MODULES == {n: s.grammar for n, s in langs.SPECS.items()}


@pytest.mark.parametrize("name", sorted(langs.SPECS))
def test_fixture_indexes(name):
    """The record's own snippet goes through the real extractor and comes back."""
    s = langs.SPECS[name]
    b = langs.extract(f"<fixture>{s.exts[0]}", name, s.fixture.encode())
    assert b.ents, f"{name}: fixture produced no entities"
    assert b.refs, f"{name}: fixture produced no references"
    # Complexity is computed from the record's decision set, so a function that
    # parses but has no branch would still be a sign the wrong node types are
    # listed.  Every fixture here has at least one function body.
    assert any(e.get("complexity") for e in b.ents), f"{name}: no complexity computed"


def test_langs_check_reports_green():
    cli.cmd_langs(None)
    # and the same records fail loudly when one is incomplete
    broken = langs.SPECS["c"]
    import dataclasses

    with pytest.raises(SystemExit) as exc:
        langs.SPECS["zz"] = dataclasses.replace(broken, exts=(), fixture="")
        try:
            langs.SPECS["zz"] = dataclasses.replace(
                broken, exts=(), fixture="", spec=broken.spec)
            cli.cmd_langs(None)
        finally:
            del langs.SPECS["zz"]
    assert exc.value.code == 1


def test_only_langs_names_a_language():
    """No module outside the registry may branch on a language name."""
    names = set(langs.SPECS) | {"c", "cpp", "java", "python"}
    names = {n for n in names if len(n) > 1}          # 'c' collides with prose
    offenders: list[str] = []
    for path in sorted(PKG.rglob("*.py")):
        if path.name == "langs.py":
            continue
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            code = line.split("#", 1)[0]
            if any(f'"{n}"' in code or f"'{n}'" in code for n in names):
                offenders.append(f"{path.relative_to(PKG)}:{i}: {line.strip()}")
    assert not offenders, "language names belong in the registry:\n" + "\n".join(offenders)
