"""SQLite store: schema, bulk insert, reference resolution, queries."""
from __future__ import annotations

import os
import sqlite3

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, lang TEXT NOT NULL,
  loc INTEGER, bytes INTEGER);
CREATE TABLE IF NOT EXISTS entities(
  id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL, kind TEXT NOT NULL,
  name TEXT NOT NULL, qualname TEXT NOT NULL, parent_id INTEGER,
  line INTEGER, end_line INTEGER, complexity INTEGER DEFAULT 0,
  decl INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS refs(
  id INTEGER PRIMARY KEY, src_file INTEGER NOT NULL, src_entity INTEGER,
  kind TEXT NOT NULL, target TEXT NOT NULL, line INTEGER,
  target_id INTEGER, resolution TEXT, candidates INTEGER);
CREATE TABLE IF NOT EXISTS file_deps(
  id INTEGER PRIMARY KEY, src_file INTEGER NOT NULL, dst_file INTEGER,
  kind TEXT NOT NULL, raw TEXT);
CREATE INDEX IF NOT EXISTS ix_ent_name ON entities(name);
CREATE INDEX IF NOT EXISTS ix_ent_qual ON entities(qualname);
CREATE INDEX IF NOT EXISTS ix_ent_file ON entities(file_id);
CREATE INDEX IF NOT EXISTS ix_ref_target ON refs(target);
CREATE INDEX IF NOT EXISTS ix_ref_src ON refs(src_entity);
CREATE INDEX IF NOT EXISTS ix_dep_src ON file_deps(src_file);
CREATE INDEX IF NOT EXISTS ix_dep_dst ON file_deps(dst_file);
CREATE VIRTUAL TABLE IF NOT EXISTS ent_fts USING fts5(name, qualname, content='entities', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS ent_ai AFTER INSERT ON entities BEGIN
  INSERT INTO ent_fts(rowid,name,qualname) VALUES(new.id,new.name,new.qualname);
END;
CREATE TRIGGER IF NOT EXISTS ent_ad AFTER DELETE ON entities BEGIN
  INSERT INTO ent_fts(ent_fts,rowid,name,qualname) VALUES('delete',old.id,old.name,old.qualname);
END;
CREATE TRIGGER IF NOT EXISTS ent_au AFTER UPDATE ON entities BEGIN
  INSERT INTO ent_fts(ent_fts,rowid,name,qualname) VALUES('delete',old.id,old.name,old.qualname);
  INSERT INTO ent_fts(rowid,name,qualname) VALUES(new.id,new.name,new.qualname);
END;
"""


def connect(db: str) -> sqlite3.Connection:
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init(db: str) -> sqlite3.Connection:
    fresh = not os.path.exists(db)
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    if fresh:
        con.execute("INSERT INTO ent_fts(ent_fts) VALUES('rebuild')")
    return con


def reset(con: sqlite3.Connection) -> None:
    for t in ("refs", "file_deps", "entities", "files"):
        con.execute(f"DELETE FROM {t}")
    con.commit()


def add_file(con: sqlite3.Connection, path: str, lang: str, src: bytes, b) -> int:
    loc = sum(1 for line in src.splitlines() if line.strip())
    cur = con.execute(
        "INSERT INTO files(path,lang,loc,bytes) VALUES(?,?,?,?) "
        "ON CONFLICT(path) DO UPDATE SET lang=excluded.lang, loc=excluded.loc, bytes=excluded.bytes "
        "RETURNING id", (path, lang, loc, len(src)))
    fid = cur.fetchone()[0]

    ent_ids = []
    for e in b.ents:
        cur = con.execute(
            "INSERT INTO entities(file_id,kind,name,qualname,parent_id,line,end_line,complexity,decl)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (fid, e["kind"], e["name"], e["qualname"],
             ent_ids[e["parent"]] if e["parent"] is not None else None,
             e["line"], e["end_line"], e.get("complexity", 0), e.get("decl", 0)))
        ent_ids.append(cur.lastrowid)

    for r in b.refs:
        con.execute(
            "INSERT INTO refs(src_file,src_entity,kind,target,line) VALUES(?,?,?,?,?)",
            (fid, ent_ids[r["src"]] if r["src"] is not None else None,
             r["kind"], r["target"], r["line"]))
    for d in b.deps:
        con.execute("INSERT INTO file_deps(src_file,dst_file,kind,raw) VALUES(?,NULL,?,?)",
                    (fid, d["kind"], d["raw"]))
    return fid


# ------------------------------------------------------------------ resolution

# Language builtins worth naming: they dominate the unresolved list on Python
# and saying "external" is more accurate than "unresolved".
BUILTINS = frozenset("""
abs all any ascii bin bool breakpoint bytearray bytes callable chr classmethod
compile complex delattr dict dir divmod enumerate eval exec filter float format
frozenset getattr globals hasattr hash help hex id input int isinstance
issubclass iter len list locals map max memoryview min next object oct open ord
pow print property range repr reversed round set setattr slice sorted staticmethod
str sum super tuple type vars zip __import__ __name__
append extend insert remove pop keys values items get update split join strip
lstrip rstrip replace startswith endswith format encode decode read write close
""".split())

def resolve_refs(con: sqlite3.Connection) -> dict[str, int]:
    """Bind ref.target to entities.id using scope-aware, then global, matching."""
    lang_of = dict(con.execute("SELECT id,lang FROM files"))
    file_dir = {fid: p.rsplit("/", 1)[0] for fid, p in con.execute("SELECT id,path FROM files")}
    fam = lambda l: "c" if l in ("c", "cpp") else l  # C and C++ interoperate

    # Candidates are bucketed by language family up front.  Bucketing lazily
    # meant re-scanning huge same-name lists per reference (a name can live in
    # thousands of headers), which dominated the run on /usr/include.
    by_qual: dict[tuple[str, str], list[int]] = {}
    by_name: dict[tuple[str, str], list[int]] = {}
    ent_info: dict[int, tuple[int, str]] = {}
    ent_decl: dict[int, int] = {}
    for eid, fid, name, qual, parent, decl in con.execute(
            "SELECT id,file_id,name,qualname,parent_id,decl FROM entities"):
        f = fam(lang_of.get(fid, ""))
        by_name.setdefault((f, name), []).append(eid)
        by_qual.setdefault((f, qual), []).append(eid)
        ent_info[eid] = (fid, qual)
        ent_decl[eid] = decl

    stats = {"exact": 0, "scoped": 0, "unique": 0, "ambiguous": 0,
             "external": 0, "unresolved": 0}
    updates = []
    rows = con.execute("SELECT id,src_file,src_entity,kind,target FROM refs").fetchall()
    for rid, fid, sid, kind, target in rows:
        mine = fam(lang_of.get(fid, ""))

        cands = by_qual.get((mine, target)) or by_qual.get((mine, target.replace(".", "::")))
        how = "exact"
        if not cands and "::" not in target:
            cands = by_name.get((mine, target))
            how = "unique"
        if not cands and "::" in target:
            cands = by_name.get((mine, target.rsplit("::", 1)[-1]))
            how = "unique"
        if cands and len(cands) > 1:
            # a definition beats a same-name declaration in a header
            defs = [c for c in cands if not ent_decl.get(c)]
            if len(defs) == 1:
                cands = defs
        if cands and len(cands) > 1:
            same_file = [c for c in cands if ent_info[c][0] == fid]
            if len(same_file) == 1:
                cands, how = same_file, "scoped"
            else:
                # same directory (one package / module): stronger than a bare
                # global name match, weaker than an exact qualname
                here = file_dir.get(fid, "\x00")
                same_dir = [c for c in cands if file_dir.get(ent_info[c][0]) == here]
                if len(same_dir) == 1:
                    cands, how = same_dir, "scoped"
            if len(cands) > 1 and sid is not None:
                myq = ent_info.get(sid, (0, ""))[1]
                prefix = myq.rsplit("::", 1)[0] + "::" if "::" in myq else ""
                same_scope = [c for c in cands if prefix and ent_info[c][1].startswith(prefix)]
                if len(same_scope) == 1:
                    cands, how = same_scope, "scoped"
        n = len(cands) if cands else 0
        if not cands:
            # a call to a language builtin is not a failure to resolve, it is a
            # target that by definition lives outside the tree
            kind_of = "external" if target in BUILTINS else "unresolved"
            updates.append((None, kind_of, 0, rid))
        elif n == 1:
            updates.append((cands[0], how, 1, rid))
        else:
            updates.append((None, "ambiguous", n, rid))
        stats[kind_of if not cands else ("ambiguous" if n > 1 else how)] += 1
    con.executemany("UPDATE refs SET target_id=?,resolution=?,candidates=? WHERE id=?", updates)
    con.commit()
    return stats


def resolve_file_deps(con: sqlite3.Connection) -> dict[str, int]:
    paths = {fid: p for fid, p in con.execute("SELECT id,path FROM files")}
    # Trailing-component index instead of scanning every path per include
    # (/usr/include: 120k deps x 23k paths was the slowest stage by far).
    by_suffix: dict[str, list[int]] = {}
    by_base: dict[str, list[int]] = {}
    for fid, p in paths.items():
        parts = p.split("/")
        for k in range(1, min(4, len(parts)) + 1):
            by_suffix.setdefault("/".join(parts[-k:]), []).append(fid)
        by_base.setdefault(parts[-1], []).append(fid)
    stats = {"resolved": 0, "unresolved": 0}
    updates = []
    for did, fid, raw in con.execute("SELECT id,src_file,raw FROM file_deps WHERE dst_file IS NULL").fetchall():
        cands: list[int] = []
        probe = raw.replace(".", "/") if "." in raw and "/" not in raw else raw
        # python/java modules name a file or package, not a path suffix
        for suffix in (raw, probe, probe + ".py", probe + "/__init__.py"):
            parts = suffix.split("/")
            for k in range(min(4, len(parts)), 0, -1):
                cands = by_suffix.get("/".join(parts[-k:]), [])
                if cands:
                    break
            if cands:
                break
        if not cands:
            cands = by_base.get(raw.rsplit("/", 1)[-1], []) or \
                by_base.get(probe.rsplit("/", 1)[-1], [])
        if len(cands) > 1 and fid is not None:
            # prefer a target sitting in the including file's own directory
            here = paths.get(fid, "").rsplit("/", 1)[0]
            local = [c for c in cands if paths.get(c, "").rsplit("/", 1)[0] == here]
            if local:
                cands = local
        if len(cands) == 1:
            updates.append((cands[0], did))
            stats["resolved"] += 1
        else:
            stats["unresolved"] += 1
    con.executemany("UPDATE file_deps SET dst_file=? WHERE id=?", updates)
    con.commit()
    return stats
