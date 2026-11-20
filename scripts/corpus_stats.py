#!/usr/bin/env python3
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""One-pass quality table over several corpus DBs.

Usage:  python corpus_stats.py jsc=/tmp/kc/jsc.db cpp=/tmp/kc/cpp.db ...

Assumes the ken schema: files(id, path, lang, loc, bytes),
entities(id, file_id, kind, name, ...), refs(id, kind, target, target_id, resolution).
Adapt the SQL if the indexer under test names its columns differently; keep the
output shape, because the columns are the argument.
"""
from __future__ import annotations

import sqlite3
import sys

HITS = ("exact", "scoped", "unique")


def one(path: str) -> dict:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    q = lambda sql: con.execute(sql).fetchall()
    files, loc, ents, refs = q(
        "SELECT (SELECT COUNT(*) FROM files), (SELECT COALESCE(SUM(loc),0) FROM files),"
        "       (SELECT COUNT(*) FROM entities), (SELECT COUNT(*) FROM refs)")[0]
    res = dict(q("SELECT resolution, COUNT(*) FROM refs GROUP BY resolution"))
    kinds = q("SELECT kind, COUNT(*) FROM entities GROUP BY kind ORDER BY 2 DESC LIMIT 7")
    empty = q("SELECT f.path, f.loc FROM files f WHERE NOT EXISTS"
              "  (SELECT 1 FROM entities e WHERE e.file_id = f.id)"
              " ORDER BY f.loc DESC")
    amb = q("SELECT kind, COUNT(*) FROM refs WHERE resolution = 'ambiguous'"
            " GROUP BY kind ORDER BY 2 DESC")
    con.close()
    return {"files": files, "loc": loc, "entities": ents, "refs": refs,
            "res": res, "kinds": kinds, "empty": empty, "amb": amb}


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    for spec in argv:
        label, _, path = spec.partition("=")
        d = one(path)
        hits = sum(d["res"].get(k, 0) for k in HITS)
        intree = d["refs"] - d["res"].get("external", 0)
        print(f"{label}: {d['files']} files, {d['loc']:,} loc | "
              f"{d['entities']:,} entities, {d['refs']:,} refs")
        print("  res   " + "  ".join(f"{k}={v:,}" for k, v in
                                    sorted(d["res"].items(), key=lambda kv: -kv[1])))
        print(f"        {100.0 * hits / max(d['refs'], 1):.1f}% of all refs, "
              f"{100.0 * hits / max(intree, 1):.1f}% of the {intree:,} in-tree refs")
        print("  kinds " + "  ".join(f"{k}={v:,}" for k, v in d["kinds"]))
        if d["amb"]:
            print("  amb   " + "  ".join(f"{k}={v:,}" for k, v in d["amb"]))
        print(f"  empty {len(d['empty'])} files with no entities")
        for path_, loc_ in d["empty"][:6]:
            print(f"        {loc_:>6} loc {path_}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
