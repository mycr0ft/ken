# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Read-only HTTP front end over a ken database.

The server is deliberately dependency-free: stdlib http.server for transport,
one JSON endpoint per question the UI asks, and a static single-page app in
``assets/``.  Everything it knows comes out of the SQLite file, so pointing it
at somebody else's ``ken.db`` is the whole deployment story.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ASSETS = Path(__file__).resolve().parent / "assets"

_local = threading.local()
MAX_GRAPH_NODES = 220
MAX_SOURCE_BYTES = 2 * 1024 * 1024


# --------------------------------------------------------------------------
# database access
# --------------------------------------------------------------------------
def connect_ro(db: str) -> sqlite3.Connection:
    """One read-only connection per thread; the UI never writes."""
    key = (id(_local), db)
    con = getattr(_local, "con", None)
    if con is None or getattr(_local, "db", None) != db:
        uri = "file:" + os.path.abspath(db).replace("?", "%3f") + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only=1")
        _local.con, _local.db = con, db
    return con


def _rows(con, sql, args=()):
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def _one(con, sql, args=()):
    r = con.execute(sql, args).fetchone()
    return dict(r) if r else None


def _count(con, sql, args=()) -> int:
    row = con.execute(sql, args).fetchone()
    return _int(row[0]) if row else 0


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _corpus_root(con) -> str:
    """Deepest directory shared by every indexed path.

    MIN/MAX over the path unique index is the cheap exact answer: for a
    sorted set the common prefix of the two extremes is the common prefix of
    the whole set. Sampling the first page of rows would instead report a
    subdirectory whenever the corpus starts inside one subtree.
    """
    span = _one(con, "SELECT MIN(path) lo, MAX(path) hi FROM files") or {}
    if not span.get("lo"):
        return ""
    try:
        root = os.path.commonpath([span["lo"], span["hi"]])
    except ValueError:          # mixed absolute/relative or mixed drives
        return ""
    return root if os.path.isdir(root) else os.path.dirname(root)


def _db_name(con) -> str:
    """Basename of the open database file, for the UI's title bar."""
    try:
        for row in con.execute("PRAGMA database_list"):
            if row[1] == "main" and row[2]:
                return os.path.basename(row[2])
    except sqlite3.Error:
        pass
    return "ken.db"


# --------------------------------------------------------------------------
# endpoints
# --------------------------------------------------------------------------
def api_overview(con, params):
    langs = _rows(con, "SELECT lang, COUNT(*) files, COALESCE(SUM(loc),0) loc "
                        "FROM files GROUP BY lang ORDER BY loc DESC")
    kinds = _rows(con, "SELECT kind, COUNT(*) n FROM entities GROUP BY kind ORDER BY n DESC")
    resolution = _rows(con, "SELECT resolution, COUNT(*) n FROM refs GROUP BY resolution "
                            "ORDER BY n DESC")
    totals = _one(con, "SELECT COUNT(*) files, COALESCE(SUM(loc),0) loc, "
                       "COALESCE(SUM(bytes),0) bytes FROM files") or {}
    counts = {
        "entities": _count(con, "SELECT COUNT(*) FROM entities"),
        "refs": _count(con, "SELECT COUNT(*) FROM refs"),
        "deps": _count(con, "SELECT COUNT(*) FROM file_deps"),
    }
    top_complex = _rows(con, """
        SELECT e.id, e.qualname, e.name, e.complexity,
               e.end_line - e.line + 1 loc, e.line, f.id file_id, f.path
        FROM entities e JOIN files f ON f.id = e.file_id
        WHERE e.kind IN ('function','method') AND e.complexity > 0
        ORDER BY e.complexity DESC, loc DESC LIMIT 10""")
    top_called = _rows(con, """
        SELECT t.id, t.qualname, t.name, COUNT(*) n, f.path, t.line, t.file_id
        FROM refs r JOIN entities t ON t.id = r.target_id
        JOIN files f ON f.id = t.file_id
        WHERE r.kind = 'call' GROUP BY t.id ORDER BY n DESC, t.qualname LIMIT 10""")
    root = _corpus_root(con)
    return {
        **counts,
        "db": _db_name(con),
        "files": _int(totals.get("files")),
        "loc": _int(totals.get("loc")),
        "bytes": _int(totals.get("bytes")),
        "langs": langs,
        "kinds": kinds,
        "resolution": resolution,
        "top_complex": top_complex,
        "top_called": top_called,
        "root": root,
    }


def api_search(con, params):
    text = (params.get("q") or [""])[0].strip()
    limit = min(_int((params.get("limit") or ["60"])[0], 60), 300)
    scope = (params.get("in") or ["symbols"])[0]
    if not text:
        return {"items": [], "total": 0, "q": text}
    if scope == "files":
        pattern = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        items = _rows(con, """
            SELECT f.id file_id, f.path, f.lang, f.loc,
                   (SELECT COUNT(*) FROM entities e WHERE e.file_id = f.id) entities
            FROM files f WHERE f.path LIKE ? ESCAPE '\\'
            ORDER BY length(f.path), f.path LIMIT ?""", (pattern, limit))
        total = _count(con, "SELECT COUNT(*) FROM files WHERE path LIKE ? ESCAPE '\\'",
                       (pattern,))
        return {"items": items, "total": total, "q": text, "in": scope}

    items = {}
    # FTS for the token search, LIKE for substrings FTS cannot prefix-match
    match = '"' + re_sub(text) + '"*'
    try:
        for r in con.execute("""
                SELECT e.id, e.kind, e.name, e.qualname, e.line, e.complexity, e.decl,
                       f.id file_id, f.path, bm25(ent_fts) rank
                FROM ent_fts JOIN entities e ON e.id = ent_fts.rowid
                JOIN files f ON f.id = e.file_id
                WHERE ent_fts MATCH ? ORDER BY rank LIMIT ?""", (match, limit * 2)):
            items[r["id"]] = dict(r)
    except sqlite3.OperationalError:
        pass
    like = "%" + text + "%"
    for r in con.execute("""
            SELECT e.id, e.kind, e.name, e.qualname, e.line, e.complexity, e.decl,
                   f.id file_id, f.path, 0 rank
            FROM entities e JOIN files f ON f.id = e.file_id
            WHERE e.qualname LIKE ? OR e.name LIKE ?
            ORDER BY (e.name = ?) DESC, length(e.name), e.qualname LIMIT ?""",
            (like, like, text, limit * 2)):
        items.setdefault(r["id"], dict(r))

    def score(it):
        name = it["name"]
        return (
            0 if name == text else 1 if it["qualname"] == text else
            2 if name.lower().startswith(text.lower()) else 3,
            1 if it.get("decl") else 0,
            len(it["qualname"]),
            it["qualname"],
        )

    ranked = sorted(items.values(), key=score)[:limit]
    return {"items": ranked, "total": len(items), "q": text, "in": scope}


def re_sub(text: str) -> str:
    """FTS5 phrase-safe token: strip quoting characters, keep separators."""
    keep = "".join(ch for ch in text if ch.isalnum() or ch in "_ .:/")
    return keep.replace('"', " ").strip() or "x"


def api_symbol(con, params):
    eid = _int((params.get("id") or ["0"])[0])
    ent = _one(con, """
        SELECT e.*, f.path, f.lang, f.id file_id, f.loc file_loc
        FROM entities e JOIN files f ON f.id = e.file_id WHERE e.id = ?""", (eid,))
    if not ent:
        return None
    chain = []
    parent = ent.get("parent_id")
    for _ in range(8):
        if not parent:
            break
        row = _one(con, "SELECT id, kind, name, qualname, parent_id FROM entities WHERE id = ?",
                   (parent,))
        if not row:
            break
        chain.append({k: row[k] for k in ("id", "kind", "name", "qualname")})
        parent = row["parent_id"]
    children = _rows(con, """
        SELECT id, kind, name, qualname, line, end_line, complexity, decl
        FROM entities WHERE parent_id = ? ORDER BY line, id""", (eid,))
    out_refs = _rows(con, """
        SELECT r.kind, r.target, r.target_id, r.resolution,
               COUNT(*) n, MIN(r.line) line,
               t.qualname target_qual, t.kind target_kind, t.line target_line,
               t.file_id target_file, f.path target_path
        FROM refs r
        LEFT JOIN entities t ON t.id = r.target_id
        LEFT JOIN files f ON f.id = t.file_id
        WHERE r.src_entity = ?
        GROUP BY r.kind, COALESCE(r.target_id, 0), r.target
        ORDER BY (r.kind = 'inherit') DESC, n DESC, r.target LIMIT 200""", (eid,))
    callers = _rows(con, """
        SELECT r.src_entity, s.qualname src_qual, s.kind src_kind, s.line src_line,
               f.id file_id, f.path, COUNT(*) n, MIN(r.line) line,
               MIN(r.resolution) resolution
        FROM refs r
        LEFT JOIN entities s ON s.id = r.src_entity
        LEFT JOIN files f ON f.id = r.src_file
        WHERE r.target_id = ? AND r.kind IN ('call', 'instantiate')
        GROUP BY r.src_entity ORDER BY n DESC, src_qual LIMIT 200""", (eid,))
    subclasses = _rows(con, """
        SELECT s.id, s.qualname, s.kind, f.path, s.line FROM refs r
        JOIN entities s ON s.id = r.src_entity JOIN files f ON f.id = s.file_id
        WHERE r.target_id = ? AND r.kind = 'inherit' ORDER BY s.qualname""", (eid,))
    ref_kinds = _rows(con, "SELECT kind, COUNT(*) n FROM refs WHERE src_entity = ? GROUP BY kind",
                      (eid,))
    return {"entity": ent, "parents": list(reversed(chain)), "children": children,
            "out": out_refs, "callers": callers, "subclasses": subclasses,
            "ref_kinds": ref_kinds}


def api_file(con, params):
    fid = _int((params.get("id") or ["0"])[0])
    f = _one(con, "SELECT * FROM files WHERE id = ?", (fid,))
    if not f:
        return None
    entity_rows = _rows(con, """
        SELECT id, kind, name, qualname, parent_id, line, end_line, complexity, decl
        FROM entities WHERE file_id = ? ORDER BY line, end_line DESC, id""", (fid,))
    out = _rows(con, """
        SELECT d.kind, d.raw, f.id dst_file, f.path dst_path
        FROM file_deps d LEFT JOIN files f ON f.id = d.dst_file
        WHERE d.src_file = ? ORDER BY d.kind, d.raw LIMIT 400""", (fid,))
    inc = _rows(con, """
        SELECT d.kind, d.raw, f.id src_file, f.path src_path
        FROM file_deps d JOIN files f ON f.id = d.src_file
        WHERE d.dst_file = ? ORDER BY f.path LIMIT 400""", (fid,))
    text, truncated = "", False
    try:
        raw = Path(f["path"]).read_bytes()
        if len(raw) > MAX_SOURCE_BYTES:
            raw, truncated = raw[:MAX_SOURCE_BYTES], True
        text = raw.decode("utf-8", "replace")
    except OSError as exc:
        text, truncated = "", "unreadable: " + str(exc)
    return {"file": f, "source": text, "truncated": truncated,
            "entities": entity_rows, "deps_out": out, "deps_in": inc}


def api_tree(con, params):
    # only the trailing separator is stripped: these paths are absolute, so a
    # bare .strip("/") would turn /home/... into home/... and match nothing.
    path = (params.get("path") or [""])[0].strip("/")
    root = (params.get("root") or [""])[0].rstrip("/")
    base = (root + "/" if root else "") + (path + "/" if path else "")
    esc = base.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = _rows(con, "SELECT path, lang, loc FROM files WHERE path LIKE ? ESCAPE '\\'",
                 (esc + "%",))
    dirs, files = {}, []
    for r in rows:
        rest = r["path"][len(base):]
        if not rest:
            continue
        head, sep, _tail = rest.partition("/")
        if sep:
            d = dirs.setdefault(head, {"name": head, "path": (path + "/" if path else "") + head,
                                       "type": "dir", "files": 0, "loc": 0, "langs": {}})
            d["files"] += 1
            d["loc"] += _int(r["loc"])
            d["langs"][r["lang"]] = d["langs"].get(r["lang"], 0) + 1
        else:
            files.append({"name": head, "path": r["path"], "type": "file",
                          "lang": r["lang"], "loc": _int(r["loc"]), "id": None})
    ids = {r["path"]: r["id"] for r in con.execute(
        "SELECT id, path FROM files WHERE path LIKE ? ESCAPE '\\'", (esc + "%",))}
    for f in files:
        f["id"] = ids.get(f["path"])
    entries = sorted(dirs.values(), key=lambda d: (-d["files"], d["name"])) + \
        sorted(files, key=lambda f: f["name"])
    return {"path": path, "root": root, "entries": entries,
            "total_files": len(rows)}


def api_graph(con, params):
    eid = _int((params.get("id") or ["0"])[0])
    depth = max(0, min(_int((params.get("depth") or ["2"])[0], 2), 6))
    direction = (params.get("dir") or ["down"])[0]
    if direction not in ("down", "up", "both"):
        direction = "down"
    root = _one(con, "SELECT id, qualname FROM entities WHERE id = ?", (eid,))
    if not root:
        return None
    levels = {eid: 0}
    frontier, edges, truncated = [eid], set(), False
    for d in range(depth):
        nxt = []
        for nid in frontier:
            if direction in ("down", "both"):
                rows = con.execute(
                    "SELECT DISTINCT target_id FROM refs WHERE src_entity=? AND kind='call' "
                    "AND target_id IS NOT NULL", (nid,)).fetchall()
                for (t,) in rows:
                    edges.add((nid, t))
                    if t not in levels:
                        levels[t] = d + 1
                        nxt.append(t)
            if direction in ("up", "both"):
                rows = con.execute(
                    "SELECT DISTINCT src_entity FROM refs WHERE target_id=? AND kind='call' "
                    "AND src_entity IS NOT NULL", (nid,)).fetchall()
                for (s,) in rows:
                    edges.add((s, nid))
                    if s not in levels:
                        levels[s] = d + 1
                        nxt.append(s)
        frontier = nxt
        if len(levels) > MAX_GRAPH_NODES:
            truncated = True
            break
    keep = set(list(levels)[:MAX_GRAPH_NODES])
    nodes = _rows(con, """
        SELECT e.id, e.qualname, e.name, e.kind, e.line, e.complexity, e.file_id, f.path
        FROM entities e JOIN files f ON f.id = e.file_id
        WHERE e.id IN (%s)""" % ",".join("?" * len(keep)), tuple(keep)) if keep else []
    for n in nodes:
        n["depth"] = levels.get(n["id"], 0)
    node_ids = {n["id"] for n in nodes}
    return {"root": root["id"], "nodes": nodes, "direction": direction, "depth": depth,
            "truncated": truncated, "limit": MAX_GRAPH_NODES,
            "edges": [{"s": s, "d": d} for s, d in edges if s in node_ids and d in node_ids]}


MAX_UML_CLASSES = 80


def _is_class(kind: str) -> bool:
    return kind in ("class", "interface", "struct", "enum")


def api_uml(con, params):
    """Class diagram for a directory scope: the classes, what they inherit from,
    and which other classes their code touches.

    A directory can hold hundreds of classes; showing them all is a smear, so we
    rank by structural weight (inheritance first, then cross-class use, then
    size) and say how many were left out.
    """
    scope = (params.get("path") or [""])[0].strip("/")
    want = min(max(_int((params.get("limit") or ["40"])[0], 40), 4), MAX_UML_CLASSES)
    root = _corpus_root(con)
    # paths are absolute in the index; the UI speaks root-relative
    where, args = "", ()
    if scope:
        where, args = " AND f.path LIKE ?", (root.rstrip("/") + "/" + scope + "/%",)

    ents = _rows(con, """
        SELECT e.id, e.name, e.qualname, e.kind, e.parent_id, e.line, e.complexity,
               e.file_id, f.path
        FROM entities e JOIN files f ON f.id = e.file_id
        WHERE 1 = 1""" + where + " ORDER BY f.path, e.line", args)
    if not ents:
        return None
    by_id = {e["id"]: e for e in ents}
    memo = {}

    def class_of(eid):
        """Nearest lexically enclosing class, or None."""
        seen = []
        cur = eid
        while cur is not None:
            if cur in memo:
                hit = memo[cur]
                break
            seen.append(cur)
            e = by_id.get(cur)
            if e is None:
                hit = None
                break
            if _is_class(e["kind"]):
                hit = cur
                break
            cur = e["parent_id"]
        else:
            hit = None
        for s in seen:
            memo[s] = hit
        return hit

    classes = {e["id"]: e for e in ents if _is_class(e["kind"])}
    if not classes:
        return None
    members = {}
    for e in ents:
        cid = class_of(e["id"])
        if cid and cid != e["id"]:
            members.setdefault(cid, []).append(
                {"name": e["name"], "kind": e["kind"], "line": e["line"],
                 "complexity": e["complexity"] or 0})

    inh = _rows(con, """
        SELECT r.src_entity, r.target, r.target_id, r.resolution
        FROM refs r JOIN files f ON f.id = r.src_file
        WHERE r.kind = 'inherit'""" + where, args)
    uses = _rows(con, """
        SELECT r.src_entity, r.target_id, COUNT(*) n
        FROM refs r JOIN files f ON f.id = r.src_file
        WHERE r.kind IN ('call', 'instantiate') AND r.target_id IS NOT NULL""" + where + """
        GROUP BY r.src_entity, r.target_id""", args)

    edges, weight, externals = {}, {}, {}
    def bump(k, amount):
        weight[k] = weight.get(k, 0) + amount
    for r in inh:
        src = class_of(r["src_entity"]) if r["src_entity"] else None
        if src not in classes:
            continue
        dst = r["target_id"]
        if dst in classes:
            edges[(src, dst, "inherit")] = 1
            bump(src, 4); bump(dst, 4)
        elif r["target"]:
            # a base class we could not resolve (stdlib, third-party, vendored):
            # draw it as a dashed «external» so the hierarchy still reads
            name = r["target"].split(".")[-1]
            xid = "x:" + name
            externals[xid] = name
            edges[(src, xid, "inherit")] = 1
            bump(src, 2)
    for r in uses:
        src = class_of(r["src_entity"]) if r["src_entity"] else None
        dst = r["target_id"]
        if src in classes and dst in classes and src != dst:
            key = (src, dst, "uses")
            edges[key] = edges.get(key, 0) + r["n"]
            bump(src, 1); bump(dst, 1)

    def rank(cid):
        # structural weight first: a class nobody inherits from or calls is
        # decoration in a class diagram, however many methods it has
        return (weight.get(cid, 0), len(members.get(cid, [])), -classes[cid]["line"])

    # Pick connected neighbourhoods, not the top N individuals: eight isolated
    # classes draw eight boxes and zero arrows, which teaches nothing.
    adj = {}
    for (s, d, _k) in edges:
        if d in classes:
            adj.setdefault(s, set()).add(d)
            adj.setdefault(d, set()).add(s)
    ranked = sorted(classes, key=rank, reverse=True)

    def pick(cap_file, cap_dir, cap_name):
        """Breadth-first from the heaviest classes, with a per-file/per-directory cap.

        Without the cap a generated parser's 300 sibling context classes win
        every slot and the diagram shows one file instead of a codebase.
        """
        keep, queue, per_file, per_dir, per_name = [], [], {}, {}, {}
        for cid in ranked:
            if len(keep) >= want:
                break
            if cid in keep or cid in queue:
                continue
            queue.append(cid)
            while queue and len(keep) < want:
                cur = queue.pop(0)
                if cur in keep:
                    continue
                f, p = classes[cur]["file_id"], classes[cur]["path"]
                d = p.rsplit("/", 1)[0] if "/" in p else ""
                nm = classes[cur]["name"]
                if (per_file.get(f, 0) >= cap_file or per_dir.get(d, 0) >= cap_dir
                        or per_name.get(nm, 0) >= cap_name):
                    continue
                keep.append(cur)
                per_file[f] = per_file.get(f, 0) + 1
                per_dir[d] = per_dir.get(d, 0) + 1
                per_name[nm] = per_name.get(nm, 0) + 1
                for nb in sorted(adj.get(cur, ()), key=rank, reverse=True):
                    if nb not in keep and nb not in queue:
                        queue.append(nb)
        return keep

    keep = set(pick(3, 8, 1))
    if len(keep) < want:                      # thin index: relax the caps
        keep = set(pick(4, 12, 2))
    if len(keep) < want:
        keep = set(pick(want, want, want))
    out_classes = []
    for cid in sorted(keep, key=lambda i: (classes[i]["path"], classes[i]["line"])):
        c = classes[cid]
        mem = members.get(cid, [])
        meth = [m for m in mem if m["kind"] in ("method", "function")]
        attr = [m for m in mem if m["kind"] not in ("method", "function")]
        out_classes.append({
            "id": cid, "name": c["name"], "qualname": c["qualname"], "kind": c["kind"],
            "path": c["path"], "file_id": c["file_id"], "line": c["line"],
            "nmethods": len(meth), "nother": len(attr),
            "methods": meth[:8],
            "other": [a["name"] for a in attr[:8]],
        })
    used_ext = {d for (s, d, _) in edges if d in externals and s in keep}
    return {
        "scope": scope, "root": root, "total_classes": len(classes),
        "shown": len(out_classes),
        "classes": out_classes,
        "externals": [{"id": x, "name": externals[x]} for x in sorted(used_ext)],
        "edges": [{"s": s, "d": d, "kind": k, "n": v}
                  for (s, d, k), v in edges.items()
                  if s in keep and (d in keep or d in externals)],
    }


def api_metrics(con, params):
    top = min(max(_int((params.get("top") or ["25"])[0], 25), 5), 200)
    complex_ = _rows(con, """
        SELECT e.id, e.qualname, e.kind, e.complexity, e.line,
               e.end_line - e.line + 1 loc, f.path, f.id file_id
        FROM entities e JOIN files f ON f.id = e.file_id
        WHERE e.kind IN ('function','method') AND e.complexity > 0
        ORDER BY e.complexity DESC, loc DESC LIMIT ?""", (top,))
    called = _rows(con, """
        SELECT t.id, t.qualname, t.kind, COUNT(*) n, t.line, f.path, f.id file_id,
               COUNT(DISTINCT r.src_entity) callers
        FROM refs r JOIN entities t ON t.id = r.target_id JOIN files f ON f.id = t.file_id
        WHERE r.kind = 'call' GROUP BY t.id ORDER BY n DESC, t.qualname LIMIT ?""", (top,))
    biggest = _rows(con, """
        SELECT f.id file_id, f.path, f.lang, f.loc, f.bytes,
               (SELECT COUNT(*) FROM entities e WHERE e.file_id = f.id) entities,
               (SELECT COUNT(*) FROM refs r WHERE r.src_file = f.id) refs
        FROM files f ORDER BY f.loc DESC LIMIT ?""", (top,))
    missing = _rows(con, """
        SELECT r.target, COUNT(*) n, COUNT(DISTINCT r.src_file) files
        FROM refs r WHERE r.resolution = 'unresolved'
        GROUP BY r.target ORDER BY n DESC, r.target LIMIT ?""", (top,))
    resolution = _rows(con, "SELECT resolution, COUNT(*) n FROM refs GROUP BY resolution "
                            "ORDER BY n DESC")
    return {"root": _corpus_root(con), "complex": complex_, "called": called,
            "biggest": biggest,
            "missing": missing, "resolution": resolution,
            "by_lang": _rows(con, """
                SELECT f.lang, COUNT(*) refs,
                       SUM(r.resolution IN ('exact','scoped','unique')) strong,
                       SUM(r.resolution = 'external') external,
                       SUM(r.resolution = 'ambiguous') ambiguous,
                       SUM(r.resolution = 'unresolved') unresolved
                FROM refs r JOIN files f ON f.id = r.src_file GROUP BY f.lang
                ORDER BY refs DESC""")}


ROUTES = {
    "/api/overview": api_overview,
    "/api/search": api_search,
    "/api/symbol": api_symbol,
    "/api/file": api_file,
    "/api/tree": api_tree,
    "/api/graph": api_graph,
    "/api/metrics": api_metrics,
    "/api/uml": api_uml,
}

MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".json": "application/json"}


class KenServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, handler, db: str):
        self.db = db
        super().__init__(addr, handler)


class Handler(BaseHTTPRequestHandler):
    server: KenServer
    server_version = "ken"
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------
    def log_message(self, format, *args):  # noqa: A002 - keep the terminal usable
        pass

    def _send(self, status, body: bytes, ctype: str, cache: str = "no-store"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self._send(status, body, "application/json; charset=utf-8")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            url = urlparse(self.path)
            path, params = unquote(url.path), parse_qs(url.query)
            if path in ROUTES:
                con = connect_ro(self.server.db)
                try:
                    out = ROUTES[path](con, params)
                except sqlite3.Error as exc:
                    self._json({"error": "sqlite: " + str(exc)}, 500)
                    return
                if out is None:
                    self._json({"error": "not found"}, 404)
                else:
                    self._json(out)
                return
            if path in ("/", "/index.html"):
                return self._asset("index.html")
            if path.startswith("/assets/"):
                return self._asset(path[len("/assets/"):])
            self._json({"error": "no route " + path}, 404)
        except BrokenPipeError:
            pass
        except Exception:  # noqa: BLE001 - never take the server down for one request
            traceback.print_exc()
            try:
                self._json({"error": "internal error"}, 500)
            except Exception:  # noqa: BLE001
                pass

    def _asset(self, name: str):
        target = (ASSETS / name).resolve()
        if ASSETS not in target.parents or not target.is_file():
            return self._json({"error": "no asset"}, 404)
        self._send(200, target.read_bytes(),
                   MIME.get(target.suffix, "application/octet-stream"))


def serve(db: str, host: str = "127.0.0.1", port: int = 8000, open_browser: bool = False):
    if not os.path.exists(db):
        raise SystemExit(f"ken: no database at {db}")
    httpd = KenServer((host, port), Handler, os.path.abspath(db))
    real_port = httpd.server_address[1]
    url = f"http://{host}:{real_port}/"
    print(f"ken web  {os.path.basename(db)}  {url}")
    print("  ctrl-c to stop")
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        httpd.server_close()
    return httpd
