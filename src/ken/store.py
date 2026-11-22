# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""SQLite store: schema, bulk insert, reference resolution, queries."""
from __future__ import annotations

import os
import sqlite3

from .langs import SPECS, doc_links

SCHEMA_TABLES = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, lang TEXT NOT NULL,
  loc INTEGER, bytes INTEGER);
CREATE TABLE IF NOT EXISTS entities(
  id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL, kind TEXT NOT NULL,
  name TEXT NOT NULL, qualname TEXT NOT NULL, parent_id INTEGER,
  line INTEGER, end_line INTEGER, complexity INTEGER DEFAULT 0,
  decl INTEGER DEFAULT 0, doc TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS refs(
  id INTEGER PRIMARY KEY, src_file INTEGER NOT NULL, src_entity INTEGER,
  kind TEXT NOT NULL, target TEXT NOT NULL, line INTEGER,
  target_id INTEGER, resolution TEXT, candidates INTEGER);
CREATE TABLE IF NOT EXISTS doc_links(
  id INTEGER PRIMARY KEY, src_entity INTEGER NOT NULL,
  target TEXT NOT NULL, target_id INTEGER);
CREATE TABLE IF NOT EXISTS file_deps(
  id INTEGER PRIMARY KEY, src_file INTEGER NOT NULL, dst_file INTEGER,
  kind TEXT NOT NULL, raw TEXT);
"""

SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS ix_ent_name ON entities(name);
CREATE INDEX IF NOT EXISTS ix_ent_qual ON entities(qualname);
CREATE INDEX IF NOT EXISTS ix_ent_file ON entities(file_id);
CREATE INDEX IF NOT EXISTS ix_ref_target ON refs(target);
CREATE INDEX IF NOT EXISTS ix_ref_src ON refs(src_entity);
CREATE INDEX IF NOT EXISTS ix_doclink_src ON doc_links(src_entity);
CREATE INDEX IF NOT EXISTS ix_doclink_target ON doc_links(target);
CREATE INDEX IF NOT EXISTS ix_dep_src ON file_deps(src_file);
CREATE INDEX IF NOT EXISTS ix_dep_dst ON file_deps(dst_file);
CREATE VIRTUAL TABLE IF NOT EXISTS ent_fts USING fts5(name, qualname, content='entities', content_rowid='id');
CREATE VIRTUAL TABLE IF NOT EXISTS doc_fts USING fts5(doc, content='entities', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS ent_ai AFTER INSERT ON entities BEGIN
  INSERT INTO ent_fts(rowid,name,qualname) VALUES(new.id,new.name,new.qualname);
  INSERT INTO doc_fts(rowid,doc) VALUES(new.id,new.doc);
END;
CREATE TRIGGER IF NOT EXISTS ent_ad AFTER DELETE ON entities BEGIN
  INSERT INTO ent_fts(ent_fts,rowid,name,qualname) VALUES('delete',old.id,old.name,old.qualname);
  INSERT INTO doc_fts(doc_fts,rowid,doc) VALUES('delete',old.id,old.doc);
END;
CREATE TRIGGER IF NOT EXISTS ent_au AFTER UPDATE ON entities BEGIN
  INSERT INTO ent_fts(ent_fts,rowid,name,qualname) VALUES('delete',old.id,old.name,old.qualname);
  INSERT INTO doc_fts(doc_fts,rowid,doc) VALUES('delete',old.id,old.doc);
  INSERT INTO ent_fts(rowid,name,qualname) VALUES(new.id,new.name,new.qualname);
  INSERT INTO doc_fts(rowid,doc) VALUES(new.id,new.doc);
END;
"""

SCHEMA = SCHEMA_TABLES + SCHEMA_INDEXES


def connect(db: str) -> sqlite3.Connection:
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def _migrate(con: sqlite3.Connection) -> None:
    """Add the doc column to an index built before doc comments were read.

    An index is a cache of a corpus, never the corpus itself, so widening the
    schema in place and letting the next `index` fill the column is the honest
    move: refusing to open an old DB would make `ken web` fail after an upgrade
    for no reason a reader would guess.
    """
    cols = {r[1] for r in con.execute("PRAGMA table_info(entities)")}
    if cols and "doc" not in cols:
        con.execute("ALTER TABLE entities ADD COLUMN doc TEXT NOT NULL DEFAULT ''")


def init(db: str) -> sqlite3.Connection:
    fresh = not os.path.exists(db)
    con = sqlite3.connect(db)
    con.executescript(SCHEMA_TABLES)
    _migrate(con)
    con.executescript(SCHEMA_INDEXES)
    if fresh:
        con.execute("INSERT INTO ent_fts(ent_fts) VALUES('rebuild')")
        con.execute("INSERT INTO doc_fts(doc_fts) VALUES('rebuild')")
    return con


def reset(con: sqlite3.Connection) -> None:
    for t in ("refs", "doc_links", "file_deps", "entities", "files"):
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
            "INSERT INTO entities(file_id,kind,name,qualname,parent_id,line,end_line,complexity,decl,doc)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (fid, e["kind"], e["name"], e["qualname"],
             ent_ids[e["parent"]] if e["parent"] is not None else None,
             e["line"], e["end_line"], e.get("complexity", 0), e.get("decl", 0),
             e.get("doc", "")))
        ent_ids.append(cur.lastrowid)

    for idx, e in enumerate(b.ents):
        for target in doc_links(e.get("doc", ""), e["qualname"]):
            con.execute("INSERT INTO doc_links(src_entity,target) VALUES(?,?)",
                        (ent_ids[idx], target))

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

# Language builtins and platform names live in the language records (langs.py),
# because "which names are external" is a fact about a language, not about the
# resolver.  The resolver stays language-blind: it asks the record.
def _outside(spec, target: str) -> bool:
    """True when the name provably comes from outside the tree's language runtime."""
    if spec is not None and target in spec.external:
        return True
    if target.startswith("std::") or target.startswith("::std::"):
        return True
    return False


CALLEE = frozenset((
    "function", "method", "decl_function", "decl_method", "macro",
    "class", "struct", "interface", "enum",
))
# The kinds a member call can be written inside.
TYPE_KINDS = frozenset(("class", "struct", "interface", "union", "enum"))


def prune_macro_phantoms(con: sqlite3.Connection) -> int:
    """Delete the declarations that are really macro invocations.

    tree-sitter does not expand macros, so `FOO(x) { ... }` is read as a
    function named FOO with a body, and `CHECK(v);` inside that body is read as
    a member function declaration.  Catch2/doctest style suites then grow a
    phantom function per test case and a phantom class per section, and every
    `CHECK` call in the corpus resolves against those fakes.

    The one reliable signal is that the same corpus also defines FOO as a
    function-like macro: a name that is a macro cannot be a declaration in a
    scope where the macro is visible, and macro args are exactly where the
    grammar guesses wrong.  Entities that survive keep their children by
    re-parenting, so a real class nested in a phantom section is not lost.
    """
    macros = {r[0] for r in con.execute("SELECT name FROM entities WHERE kind='macro'")}
    if not macros:
        return 0
    rows = con.execute("SELECT id,name,parent_id FROM entities "
                       "WHERE kind IN ('function','decl_function','decl_method','method')").fetchall()
    doomed = [(i, p) for i, n, p in rows if n in macros]
    for eid, parent in doomed:
        con.execute("UPDATE entities SET parent_id=? WHERE parent_id=?", (parent, eid))
        con.execute("UPDATE refs SET src_entity=NULL WHERE src_entity=?", (eid,))
        con.execute("DELETE FROM entities WHERE id=?", (eid,))
    return len(doomed)


def resolve_refs(con: sqlite3.Connection) -> dict[str, int]:
    """Bind ref.target to entities.id using scope-aware, then global, matching."""
    lang_of = dict(con.execute("SELECT id,lang FROM files"))
    file_dir = {fid: p.rsplit("/", 1)[0] for fid, p in con.execute("SELECT id,path FROM files")}

    def fam(l):
        s = SPECS.get(l)
        return (s.family or l) if s else l

    # One record per family answers "is this name external"; C and C++ carry
    # the same external set, so whichever of the two is picked is the same
    # answer, and a future second family will register its own.
    spec_by_fam: dict = {}
    for _name, _s in SPECS.items():
        spec_by_fam.setdefault(fam(_name), _s)

    # Candidates are bucketed by language family up front.  Bucketing lazily
    # meant re-scanning huge same-name lists per reference (a name can live in
    # thousands of headers), which dominated the run on /usr/include.
    by_qual: dict[tuple[str, str], list[int]] = {}
    by_name: dict[tuple[str, str], list[int]] = {}
    ent_info: dict[int, tuple[int, str]] = {}
    ent_decl: dict[int, int] = {}
    ent_kind: dict[int, str] = {}
    for eid, fid, name, qual, parent, decl, ekind in con.execute(
            "SELECT id,file_id,name,qualname,parent_id,decl,kind FROM entities"):
        f = fam(lang_of.get(fid, ""))
        by_name.setdefault((f, name), []).append(eid)
        by_qual.setdefault((f, qual), []).append(eid)
        ent_info[eid] = (fid, qual)
        ent_decl[eid] = decl
        ent_kind[eid] = ekind

    stats = {"exact": 0, "scoped": 0, "unique": 0, "ambiguous": 0,
             "dispatch": 0, "preprocessor": 0, "overload": 0, "external": 0, "unresolved": 0}
    updates = []
    rows = con.execute("SELECT id,src_file,src_entity,kind,target FROM refs").fetchall()

    # Inheritance is resolved first.  `Base::tick` called from `Derived::run`
    # can only be judged once the class hierarchy exists, and that hierarchy is
    # itself a set of refs, so a single pass would judge every member call
    # against a graph that is still half empty.
    ancestors: dict[str, set[str]] = {}
    chain_cache: dict[str, set[str]] = {}
    id_by_qual: dict[str, int] = {}
    for eid, (_fid, _q) in ent_info.items():
        id_by_qual.setdefault(_q, eid)

    def caller_type(sid) -> str:
        """The class-like entity the reference is written inside, if any.

        A bare call inside a method is aimed at a member of whatever the
        receiver is.  A bare call inside a free function is aimed at a name in
        a namespace, and the same-file test is the right one there.
        """
        myq = ent_info.get(sid, (0, ""))[1] if sid is not None else ""
        owner = myq.rsplit("::", 1)[0] if "::" in myq else ""
        if not owner:
            return ""
        ok = ent_kind.get(id_by_qual.get(owner, -1))
        return owner if ok in TYPE_KINDS else ""

    def chain_of(owner: str) -> set[str]:
        hit = chain_cache.get(owner)
        if hit is not None:
            return hit
        out: set[str] = set()
        stack = [owner]
        while stack:
            x = stack.pop()
            for p in ancestors.get(x, ()):
                if p not in out:
                    out.add(p)
                    stack.append(p)
        chain_cache[owner] = out
        return out

    def _within(owner: str) -> set[str]:
        # The enclosing class answers to its own members: `f.Parse` written
        # inside a FlagSet method reaches FlagSet::Parse, which is not an
        # ancestor of FlagSet and would otherwise look out of reach.
        return chain_of(owner) | {owner}

    def in_scope(cands, fid, sid) -> bool:
        """Is any candidate reachable from the call site at all?

        Same file, same package directory, or a member of the enclosing class
        or one of its bases.  `False` means every candidate is a coincidence of
        naming, which is a different statement from "several choices".
        """
        here = file_dir.get(fid, "\x00")
        myq = ent_info.get(sid, (0, ""))[1] if sid is not None else ""
        owner = myq.rsplit("::", 1)[0] if "::" in myq else ""
        chain = _within(owner) if owner else set()
        for c in cands:
            cf, cq = ent_info[c]
            if cf == fid or file_dir.get(cf) == here:
                return True
            if cq.rsplit("::", 1)[0] in chain:
                return True
        return False

    order = ([r for r in rows if r[3] == "inherit"],
             [r for r in rows if r[3] != "inherit"])
    for batch in order:
        for rid, fid, sid, kind, target in batch:
            mine = fam(lang_of.get(fid, ""))

            spec = spec_by_fam.get(mine)
            spec_sep = (spec.member_sep if spec else "") or ""
            # An unqualified target is a name, not a path.  `x.data()` names a
            # member of whatever x is; it is not the global `data` of some
            # unrelated header, and looking a bare name up in the
            # qualified-name index is how a member call came to be pinned to
            # one.  Only a qualified target may claim an exact qualname; a bare
            # one is settled by its name and by the scopes around the call.
            qualified = "::" in target or bool(spec_sep and spec_sep in target)
            cands = ((by_qual.get((mine, target))
                      or by_qual.get((mine, target.replace(".", "::"))))
                     if qualified else None)
            how = "exact"
            # `String::new` in Rust must not resolve to an in-tree `new`: the head of
            # a qualified target decides whether the tail is worth a global lookup.
            head = target.rsplit("::", 1)[0] if "::" in target else ""
            outside_head = bool(head) and spec is not None and bool(spec.member_sep) \
                and _outside(spec, head)
            if not cands and "::" in target and not outside_head:
                # A Rust path states the module it goes through (`crate::tests::memchr::
                # Runner::new`), which the qualname in the index does not repeat.  Try
                # the path minus its head, longest first, and take the longest answer:
                # a shorter suffix that matches several scopes says nothing.
                parts = target.split("::")
                for i in range(1, len(parts)):
                    hit = by_qual.get((mine, "::".join(parts[i:])))
                    if hit:
                        cands, how = hit, "scoped"
                        break

            if not cands and "::" not in target:
                cands = by_name.get((mine, target))
                how = "unique"
            if not cands and "::" in target and not outside_head:
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
                if len(cands) > 1 and sid is not None:
                    # One level up the hierarchy: a method inherited from a base
                    # class is declared once, in the base, and is neither a file
                    # nor a scope match when the call is written in the derived
                    # class.
                    myq = ent_info.get(sid, (0, ""))[1]
                    owner = myq.rsplit("::", 1)[0] if "::" in myq else ""
                    chain = chain_of(owner) if owner else set()
                    near = [c for c in cands if ent_info[c][1].rsplit("::", 1)[0] in chain]
                    if len(near) == 1:
                        cands, how = near, "scoped"
            if cands and kind == "call":
                # A name can be carried by something that cannot be called: a
                # typedef, a field, a namespace.  Finding the name is then not
                # finding the callee, and saying "ambiguous" would hide that.
                cands = [c for c in cands if ent_kind.get(c) in CALLEE]
            elif cands:
                # Every non-call reference names a type: `new C()`, `extends C`,
                # `struct S : B`.  Constructors carry their class's name in
                # Java and C++, so without this a type reference is outvoted by
                # its own constructors and reads as an ambiguity about scope.
                typed = [c for c in cands if ent_kind.get(c) in TYPE_KINDS]
                if typed:
                    cands = typed
            n = len(cands) if cands else 0
            if not cands:
                # A call the tree does not contain is only a failure if the name is
                # meant to be in the tree.  Platform names (libc, the C++ standard
                # library, the JDK, assertion DSLs) are targets that by definition
                # live outside it.  This test runs only after the in-tree lookup
                # fails, so a corpus that ships its own copy of libc still wins.
                # Nothing in the corpus answers to this name at all, so whatever
                # the call is aimed at was never in the tree (Catch2, the JDK, a
                # system header).  Only a name the corpus *does* define, but which
                # cannot be pinned to one definition, is a gap in the analysis.
                in_tree = (mine, target.rsplit("::", 1)[-1]) in by_name
                kind_of = ("external" if (outside_head or not in_tree
                                          or _outside(spec, target))
                           else "unresolved")
                updates.append((None, kind_of, 0, rid))
            elif n == 1:
                # "exact" means the resolved entity answers to exactly the text
                # of the reference.  A bare name that landed on a global of that
                # name is a name match, not a path match, and saying otherwise
                # would flatter the numbers.
                kind_of = how if ent_info[cands[0]][1] == target else (how if how != "exact" else "unique")
                updates.append((cands[0], kind_of, 1, rid))
                if kind == "inherit" and sid is not None:
                    ancestors.setdefault(ent_info[sid][1], set()).add(ent_info[cands[0]][1])
            else:
                # A many-candidate name that is none of them, twice over.
                if all(ent_kind.get(c) == "macro" for c in cands):
                    # A macro is text, not a name: the last #define wins at
                    # inclusion time, and nothing in the index knows that order.
                    kind_of = "preprocessor"
                elif len({ent_info[c][1] for c in cands}) == 1 and kind == "call":
                    # Every candidate carries the same qualified name: one
                    # declaration site seen through several signatures.  Which
                    # signature a call reaches is a question about argument
                    # types, so the site is known even though the body is not,
                    # and calling that "ambiguous" would say we do not know
                    # where the name lives when we do.
                    kind_of = "overload"
                elif len({ent_info[c][1] for c in cands}) == 1:
                    # A non-call reference has no signature to pick: several rows
                    # under one qualified name (two `impl` blocks, a declaration
                    # and its definition) are one place, not an overload set.
                    kind_of = "unique"
                elif kind == "call" and caller_type(sid) and not any(
                        ent_info[c][1].rsplit("::", 1)[0] in _within(caller_type(sid))
                        for c in cands):
                    # Written in a method, and nothing in the enclosing class or
                    # its bases answers to the name: the receiver's type lives
                    # outside the tree (`size` on a std::string).  Calling that
                    # ambiguous claims a choice between candidates that were
                    # never candidates.
                    kind_of = "dispatch"
                elif kind == "call" and not in_scope(cands, fid, sid):
                    # The name exists in the corpus but nothing in scope declares
                    # it: the receiver's type lives outside it (`size` called on
                    # a std::string from a header-only library).  Calling that
                    # ambiguous claims a choice between candidates that were
                    # never candidates.
                    kind_of = "dispatch"
                else:
                    kind_of = "ambiguous"
                updates.append((None, kind_of, n, rid))
            stats[kind_of] = stats.get(kind_of, 0) + 1

    con.executemany("UPDATE refs SET target_id=?,resolution=?,candidates=? WHERE id=?", updates)
    con.commit()
    return stats


def resolve_doc_links(con: sqlite3.Connection) -> int:
    """Point each `@see`/`{@link}` cross-reference at the entity it names.

    Only the ones the corpus actually defines get a target; the rest keep their
    text so the symbol page can still say "documented as referring to Foo" when
    Foo is a JDK class nobody indexed.
    """
    rows = con.execute("SELECT id,target FROM doc_links WHERE target_id IS NULL").fetchall()
    n = 0
    for link_id, target in rows:
        base = target.replace("::", ".").split(".")[-1]
        row = con.execute(
            "SELECT id FROM entities WHERE qualname=? OR name=? "
            "ORDER BY (kind IN ('function','method','class','struct','interface','enum')) DESC,"
            " decl ASC LIMIT 1", (target, base)).fetchone()
        if row:
            con.execute("UPDATE doc_links SET target_id=? WHERE id=?", (row[0], link_id))
            n += 1
    con.commit()
    return n


def resolve_file_deps(con: sqlite3.Connection) -> dict[str, int]:
    paths = {fid: p for fid, p in con.execute("SELECT id,path FROM files")}
    # Trailing-component index instead of scanning every path per include
    # (/usr/include: 120k deps x 23k paths was the slowest stage by far).
    by_suffix: dict[str, list[int]] = {}
    by_base: dict[str, list[int]] = {}
    by_dir: dict[str, list[int]] = {}
    for fid, p in paths.items():
        parts = p.split("/")
        for k in range(1, min(6, len(parts)) + 1):
            by_suffix.setdefault("/".join(parts[-k:]), []).append(fid)
        by_base.setdefault(parts[-1], []).append(fid)
        # A package directory, for languages where an import names a directory
        # rather than a file stem (Go).  Indexed by suffixes so a module path
        # can be matched without knowing where the module is checked out.
        for k in range(1, min(6, len(parts) - 1)):
            by_dir.setdefault("/".join(parts[-k - 1:-1]), []).append(fid)
    stats = {"resolved": 0, "unresolved": 0}
    updates = []
    lang_of = dict(con.execute("SELECT id,lang FROM files"))
    for did, fid, raw in con.execute(
            "SELECT id,src_file,raw FROM file_deps WHERE dst_file IS NULL").fetchall():
        cands: list[int] = []
        spec = SPECS.get(lang_of.get(fid, ""))
        # How a language spells a module is a fact in its record: Java and
        # Python name `a.b` for the file a/b, C and C++ include a path suffix,
        # Rust writes `crate::model::Widget` and Go writes a package directory.
        probe = raw
        if spec is not None:
            if spec.module_paths and spec.module_sep != "/" and "/" not in probe:
                probe = probe.replace(spec.module_sep, "/")
            while spec.module_roots:
                head, sep, rest = probe.partition("/")
                if sep and head in spec.module_roots:
                    probe = rest
                else:
                    break
            if spec.import_item_drop:
                parts = probe.split("/")
                if len(parts) > spec.import_item_drop:
                    probe = "/".join(parts[:-spec.import_item_drop])
        if spec is not None and spec.dep_is_dir:
            parts = probe.split("/")
            for k in range(min(6, len(parts)), 0, -1):
                cands = by_dir.get("/".join(parts[-k:]), [])
                if cands:
                    break
        if not cands:
            suffixes = (raw,) + ((probe,) if probe != raw else ()) + (
                tuple(probe + s for s in spec.import_suffixes) if spec else ())
            for suffix in suffixes:
                parts = suffix.split("/")
                for k in range(min(6, len(parts)), 0, -1):
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
