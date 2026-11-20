"""ken command line: index a tree, then query the graph."""
from __future__ import annotations

import argparse
import os
import sys
import time

from . import store
from .langs import EXTS, extract

SKIP_DIRS = {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
             "build", "dist", ".mypy_cache", ".pytest_cache", ".tox", "target"}


def iter_sources(paths):
    for p in paths:
        if os.path.isfile(p):
            lang = EXTS.get(os.path.splitext(p)[1].lower())
            if lang:
                yield p, lang
            continue
        for root, dirs, files in os.walk(p):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            for f in files:
                lang = EXTS.get(os.path.splitext(f)[1].lower())
                if lang:
                    yield os.path.join(root, f), lang


def cmd_index(a) -> None:
    con = store.init(a.db)
    store.reset(con)
    t0 = time.time()
    n_files = n_ent = n_ref = 0
    failed = []
    for i, (path, lang) in enumerate(iter_sources(a.paths), 1):
        try:
            with open(path, "rb") as fh:
                src = fh.read()
            b = extract(path, lang, src)
        except Exception as exc:  # noqa: BLE001 - parser robustness
            failed.append((path, str(exc)[:80]))
            continue
        store.add_file(con, os.path.abspath(path), lang, src, b)
        n_files += 1
        n_ent += len(b.ents)
        n_ref += len(b.refs)
        if i % 500 == 0:
            con.commit()
            print(f"  {i} files, {n_ent} entities, {n_ref} refs", file=sys.stderr)
    con.commit()
    parse_s = time.time() - t0
    rs = store.resolve_refs(con)
    ds = store.resolve_file_deps(con)
    con.commit()
    total = sum(rs.values())
    print(f"indexed {n_files} files, {n_ent} entities, {n_ref} refs in {parse_s:.1f}s")
    print("refs  " + "  ".join(f"{k}={v}" for k, v in rs.items()) +
          f"  -> resolved {100.0 * (total - rs['unresolved'] - rs['ambiguous']) / max(total, 1):.1f}%")
    print("deps  " + "  ".join(f"{k}={v}" for k, v in ds.items()))
    if failed:
        print(f"{len(failed)} files failed to parse")
        for path, msg in failed[:5]:
            print(f"  {path}: {msg}")


def cmd_summary(a) -> None:
    con = store.connect(a.db)
    q = con.execute("SELECT COUNT(*), SUM(loc) FROM files").fetchone()
    print(f"files      {q[0]:>8}   loc {q[1] or 0}")
    for kind, n in con.execute("SELECT kind, COUNT(*) FROM entities GROUP BY 1 ORDER BY 2 DESC"):
        print(f"{kind:<10} {n:>8}")
    print(f"refs       {con.execute('SELECT COUNT(*) FROM refs').fetchone()[0]:>8}")
    print(f"file deps  {con.execute('SELECT COUNT(*) FROM file_deps').fetchone()[0]:>8}")


def _entity_ids(con, name):
    rows = con.execute(
        "SELECT id,kind,name,qualname,file_id,line FROM entities WHERE name=? OR qualname=? "
        "OR qualname LIKE ? ORDER BY kind, qualname", (name, name, f"%::{name}")).fetchall()
    return rows


def cmd_sym(a) -> None:
    con = store.connect(a.db)
    rows = con.execute(
        "SELECT e.kind,e.qualname,f.path,e.line,e.decl FROM entities e JOIN files f ON f.id=e.file_id "
        "WHERE e.name LIKE ? OR e.qualname LIKE ? ORDER BY e.name LIMIT ?",
        (f"%{a.pattern}%", f"%{a.pattern}%", a.limit)).fetchall()
    for kind, qual, path, line, decl in rows:
        print(f"{kind:<9} {qual:<45} {path}:{line}{'  (decl)' if decl else ''}")
    print(f"{len(rows)} matches")


def cmd_find(a) -> None:
    """Token search through the FTS index (fast on large trees)."""
    con = store.connect(a.db)
    toks = [t for t in "".join(ch if ch.isalnum() or ch == "_" else " " for ch in a.text).split() if t]
    if not toks:
        print("empty query")
        return
    match = " ".join(f'"{t}"*' for t in toks)
    rows = con.execute(
        "SELECT e.kind,e.qualname,f.path,e.line FROM ent_fts x JOIN entities e ON e.id=x.rowid "
        "JOIN files f ON f.id=e.file_id WHERE ent_fts MATCH ? ORDER BY e.qualname LIMIT ?",
        (match, a.limit)).fetchall()
    for kind, qual, path, line in rows:
        print(f"{kind:<9} {qual:<45} {path}:{line}")
    print(f"{len(rows)} matches")


def cmd_graph(a) -> None:
    """Call graph around a symbol, as an indented tree or Graphviz DOT."""
    con = store.connect(a.db)
    roots = [r[0] for r in _entity_ids(con, a.name)]
    if not roots:
        print(f"no entity named {a.name}")
        return
    up = a.direction in ("up", "both")
    down = a.direction in ("down", "both")

    def neighbours(eid):
        out = []
        if down:
            out += con.execute(
                "SELECT tgt.id,tgt.qualname FROM refs r JOIN entities tgt ON tgt.id=r.target_id "
                "WHERE r.src_entity=? AND r.kind IN ('call','instantiate','inherit') "
                "GROUP BY tgt.id", (eid,)).fetchall()
        if up:
            out += con.execute(
                "SELECT src.id,COALESCE(src.qualname,'(file scope)') FROM refs r "
                "JOIN entities src ON src.id=r.src_entity "
                "WHERE r.target_id=? AND r.kind IN ('call','instantiate') "
                "GROUP BY src.id", (eid,)).fetchall()
        return out

    edges, order = set(), []
    frontier = [(rid, 0) for rid in roots]
    seen = set(roots)
    while frontier:
        eid, depth = frontier.pop(0)
        if depth >= a.depth:
            continue
        for nid, nqual in neighbours(eid):
            if nid is None:
                continue
            edges.add((eid, nid))
            if nid not in seen:
                seen.add(nid)
                order.append((nid, nqual))
                frontier.append((nid, depth + 1))
    names = {eid: q for eid, q in con.execute(
        f"SELECT id,qualname FROM entities WHERE id IN ({','.join('?' * len(seen))})",
        list(seen))} if seen else {}
    if a.direction == "up":
        edges = {(tgt, src) for src, tgt in edges}
    if a.dot:
        print("digraph ken {")
        print('  rankdir=LR; node [shape=box, fontname="Helvetica", fontsize=10];')
        for eid, q in names.items():
            style = ' style=filled, fillcolor="#eef"' if eid in set(roots) else ""
            print(f'  n{eid} [label="{q}"{style}];')
        for s, d in sorted(edges):
            print(f"  n{s} -> n{d};")
        print("}")
        return
    adj = {}
    for s, d in edges:
        adj.setdefault(s, []).append(d)
    printed = set()

    def walk(eid, depth):
        pad = "  " * depth
        for nid in sorted(set(adj.get(eid, [])), key=lambda x: names.get(x, "")):
            print(f"{pad}{names.get(nid, '?')}")
            if nid not in printed and depth + 1 < a.depth:
                printed.add(nid)
                walk(nid, depth + 1)
    for rid in roots:
        print(names.get(rid, a.name))
        walk(rid, 1)
    print(f"{len(edges)} edges, {len(seen)} nodes")


def cmd_callers(a) -> None:
    con = store.connect(a.db)
    ids = [r[0] for r in _entity_ids(con, a.name)]
    if not ids:
        print(f"no entity named {a.name}")
        return
    marks = ",".join("?" * len(ids))
    rows = con.execute(
        f"SELECT COALESCE(src.qualname,'(file scope)'), f.path, r.line, r.resolution "
        f"FROM refs r JOIN files f ON f.id=r.src_file "
        f"LEFT JOIN entities src ON src.id=r.src_entity "
        f"WHERE r.target_id IN ({marks}) AND r.kind IN ('call','instantiate') "
        f"ORDER BY f.path, r.line", ids).fetchall()
    files = {r[1] for r in rows}
    print(f"{len(rows)} call sites across {len(files)} files -> {a.name}")
    for qual, path, line, res in rows[:a.limit]:
        print(f"  {path}:{line:<6} {qual}")
    if len(rows) > a.limit:
        print(f"  ... {len(rows) - a.limit} more")


def cmd_callees(a) -> None:
    con = store.connect(a.db)
    ids = [r[0] for r in _entity_ids(con, a.name)]
    if not ids:
        print(f"no entity named {a.name}")
        return
    marks = ",".join("?" * len(ids))
    rows = con.execute(
        f"SELECT tgt.qualname, COUNT(*), SUM(r.resolution='unresolved') FROM refs r "
        f"JOIN entities tgt ON tgt.id=r.target_id "
        f"WHERE r.src_entity IN ({marks}) AND r.kind='call' "
        f"GROUP BY tgt.id ORDER BY 2 DESC", ids).fetchall()
    unres = con.execute(
        f"SELECT COUNT(*) FROM refs WHERE src_entity IN ({marks}) AND kind='call' AND target_id IS NULL",
        ids).fetchone()[0]
    print(f"{a.name}: {len(rows)} distinct resolved callees, {unres} unresolved call sites")
    for qual, n, _ in rows[:a.limit]:
        print(f"  {n:>3}x {qual}")


def cmd_deps(a) -> None:
    con = store.connect(a.db)
    row = con.execute("SELECT id,path FROM files WHERE path LIKE ? LIMIT 1",
                      (f"%{a.file}%",)).fetchone()
    if not row:
        print(f"no indexed file matching {a.file}")
        return
    fid, path = row
    print(f"{path}")
    print("  depends on:")
    for raw, p, kind in con.execute(
            "SELECT d.raw, f.path, d.kind FROM file_deps d LEFT JOIN files f ON f.id=d.dst_file "
            "WHERE d.src_file=? ORDER BY d.kind, d.raw", (fid,)):
        print(f"    {kind:<8} {p or '(external) ' + raw}")
    print("  used by:")
    for p, kind in con.execute(
            "SELECT f.path, d.kind FROM file_deps d JOIN files f ON f.id=d.src_file "
            "WHERE d.dst_file=? ORDER BY f.path", (fid,)):
        print(f"    {kind:<8} {p}")


def cmd_metrics(a) -> None:
    con = store.connect(a.db)
    print("most complex functions")
    for qual, cx, loc, path in con.execute(
            "SELECT e.qualname,e.complexity,e.end_line-e.line+1,f.path FROM entities e "
            "JOIN files f ON f.id=e.file_id WHERE e.kind IN ('function','method') "
            "AND e.complexity>1 ORDER BY e.complexity DESC LIMIT ?", (a.top,)):
        print(f"  cx={cx:<4} loc={loc:<9} {qual}  ({path})")
    print("most called functions")
    for qual, n in con.execute(
            "SELECT t.qualname, COUNT(*) c FROM refs r JOIN entities t ON t.id=r.target_id "
            "WHERE r.kind='call' GROUP BY t.id ORDER BY c DESC LIMIT ?", (a.top,)):
        print(f"  {n:>4}x {qual}")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="ken", description="multi-language code comprehension")
    p.add_argument("--db", default="ken.db")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("index", help="parse source trees into the database")
    s.add_argument("paths", nargs="+")
    s.set_defaults(fn=cmd_index)

    s = sub.add_parser("summary")
    s.set_defaults(fn=cmd_summary)

    s = sub.add_parser("sym", help="find entities by name")
    s.add_argument("pattern")
    s.add_argument("--limit", type=int, default=60)
    s.set_defaults(fn=cmd_sym)

    s = sub.add_parser("graph", help="call graph around a symbol")
    s.add_argument("name")
    s.add_argument("--direction", choices=("up", "down", "both"), default="down")
    s.add_argument("--depth", type=int, default=2)
    s.add_argument("--dot", action="store_true", help="emit Graphviz DOT")
    s.set_defaults(fn=cmd_graph)

    s = sub.add_parser("find", help="token search over the symbol index")
    s.add_argument("text")
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(fn=cmd_find)

    s = sub.add_parser("callers", help="who calls this symbol")
    s.add_argument("name")
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(fn=cmd_callers)

    s = sub.add_parser("callees", help="what this symbol calls")
    s.add_argument("name")
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(fn=cmd_callees)

    s = sub.add_parser("deps", help="file-level include/import dependencies")
    s.add_argument("file")
    s.set_defaults(fn=cmd_deps)

    s = sub.add_parser("metrics")
    s.add_argument("--top", type=int, default=15)
    s.set_defaults(fn=cmd_metrics)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
