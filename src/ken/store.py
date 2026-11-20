# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""SQLite store: schema, bulk insert, reference resolution, queries."""
from __future__ import annotations

import os
import sqlite3

from .langs import doc_links

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

LIBC = frozenset("""
malloc calloc realloc free memcpy memmove memset memcmp memchr strlen strnlen
strcpy strncpy strcat strncat strcmp strncmp strchr strrchr strstr strtok strdup
strerror sprintf snprintf vsprintf vsnprintf sscanf printf fprintf vfprintf
puts fputs putchar fputc fgetc fgets fopen fclose fread fwrite fflush fseek
ftell rewind feof ferror remove rename tmpfile tmpnam perror
atoi atol atoll atof strtol strtoul strtoll strtoull strtod strtof
abs labs llabs div ldiv qsort bsearch rand srand rand_r exit abort atexit
getenv setenv unsetenv system sleep usleep getcwd chdir mkdir rmdir unlink
isalpha isdigit isalnum isspace isupper islower isprint ispunct isxdigit
toupper tolower toascii
sin cos tan asin acos atan atan2 sinh cosh tanh exp log log10 log2 pow sqrt
ceil floor fabs fmod round trunc fmin fmax fdim hypot
time clock difftime mktime localtime gmtime strftime ctime
va_start va_end va_arg va_copy assert offsetof
open close read write lseek stat fstat ioctl socket bind listen accept connect
send recv pthread_create pthread_join pthread_mutex_lock pthread_mutex_unlock
""".split())

# Names that belong to the Java platform rather than to any given tree.  Only
# types and the handful of static/framework entry points that show up as bare
# call targets are listed; everything else is still "unresolved", which is the
# honest answer.
JAVA_PLATFORM = frozenset("""
Object String Integer Long Short Byte Double Float Boolean Character Number
StringBuilder StringBuffer CharSequence Comparable Iterable Runnable Thread
Exception RuntimeException Error Throwable IllegalArgumentException
IllegalStateException NullPointerException UnsupportedOperationException
IndexOutOfBoundsException NumberFormatException AssertionError Class Object
ArrayList LinkedList HashMap LinkedHashMap TreeMap HashSet LinkedHashSet
TreeSet Arrays Collections List Map Set Collection Iterator Optional Objects
BigDecimal BigInteger Date Calendar Locale UUID
StringWriter StringReader BufferedReader InputStreamReader File InputStream
OutputStream PrintStream IOException
Character StringJoiner Enum EnumSet EnumMap Comparator Spliterator Stream
Collectors Matcher Pattern AtomicInteger AtomicLong AtomicReference
WeakHashMap IdentityHashMap Properties Random Scanner Formatter
requireNonNull getClass hashCode toString equals clone notifyAll
""".split())

# Assertion and mocking DSLs are the other half of a Java test corpus: the call
# target is a static import, so nothing in the tree carries the name.
JAVA_DSL = frozenset("""
assertThat assertThrows assertDoesNotThrow assertThatCode assertThatException
assertThatThrownBy fail failBecauseExceptionWasNotThrown
isEqualTo isNotEqualTo isSameAs isNotSameAs isNull isNotNull isTrue isFalse
isInstanceOf isNotInstanceOf isAnything isA isIn isNotIn isGreaterThan
isLessThan isAtLeast isAtMost isEmpty isNotEmpty hasSize hasMessage
hasMessageThat hasMessageContaining hasCause hasToString hasSameHashCodeAs
contains containsExactly containsExactlyInAnyOrder containsAll containsInAnyOrder
doesNotContain startsWith endsWith matches startsWithIgnoringCase
containsExactlyElementsIn inOrder not isNot that named describedAs
assertEquals assertNotEquals assertTrue assertFalse assertNull assertNotNull
assertArrayEquals assertSame assertNotSame assertSameElements
verify when thenReturn thenThrow mock spy given expectAny
""".split())


CALLEE = frozenset((
    "function", "method", "decl_function", "decl_method", "macro",
    "class", "struct", "interface", "enum",
))


def _outside(fam: str, target: str) -> bool:
    """True when the name provably comes from outside the tree's language runtime."""
    if target in BUILTINS:
        return True
    if target.startswith("std::") or target.startswith("::std::"):
        return True
    if fam in ("c", "cpp") and target in LIBC:
        return True
    if fam == "java" and (target in JAVA_PLATFORM or target in JAVA_DSL):
        return True
    return False


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
    fam = lambda l: "c" if l in ("c", "cpp") else l  # C and C++ interoperate

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
        if cands and kind == "call":
            # A name can be carried by something that cannot be called: a
            # typedef, a field, a namespace.  Finding the name is then not
            # finding the callee, and saying "ambiguous" would hide that.
            cands = [c for c in cands if ent_kind.get(c) in CALLEE]
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
            kind_of = "external" if (target not in by_name or _outside(mine, target)) else "unresolved"
            updates.append((None, kind_of, 0, rid))
        elif n == 1:
            updates.append((cands[0], how, 1, rid))
        else:
            updates.append((None, "ambiguous", n, rid))
        stats[kind_of if not cands else ("ambiguous" if n > 1 else how)] += 1
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
    for fid, p in paths.items():
        parts = p.split("/")
        for k in range(1, min(6, len(parts)) + 1):
            by_suffix.setdefault("/".join(parts[-k:]), []).append(fid)
        by_base.setdefault(parts[-1], []).append(fid)
    stats = {"resolved": 0, "unresolved": 0}
    updates = []
    for did, fid, raw in con.execute("SELECT id,src_file,raw FROM file_deps WHERE dst_file IS NULL").fetchall():
        cands: list[int] = []
        probe = raw.replace(".", "/") if "." in raw and "/" not in raw else raw
        # python/java modules name a file or package, not a path suffix
        for suffix in (raw, probe, probe + ".py", probe + ".java", probe + "/__init__.py"):
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
