/* This Source Code Form is subject to the terms of the Mozilla Public
   License, v. 2.0. If a copy of the MPL was not distributed with this
   file, You can obtain one at https://mozilla.org/MPL/2.0/. */

'use strict';

// ---------------------------------------------------------------- utilities
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const nf = new Intl.NumberFormat('en-US');
const n = (v) => nf.format(v ?? 0);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const hash = (id, path) => `<a class="sym" href="#/sym/${id}">${esc(path)}</a>`;
const kindTag = (k) => `<span class="kind ${esc(k)}">${esc(k)}</span>`;
// In a call list every neighbour tends to share a class, so the leaf is what
// distinguishes them; the qualifier stays in the tooltip and the path:line line.
const leafName = (q) => {
  const m = /^(.*[.:$])([^.:$]+)$/.exec(q || '');
  return m ? m[2] : (q || '');
};
const shortPath = (p, root) => {
  if (!root || !p.startsWith(root)) return p;
  const r = p.slice(root.length).replace(/^\/+/, '');
  return r || p;
};

const RES_LABEL = {
  exact: 'exact', scoped: 'scoped', unique: 'unique',
  ambiguous: 'ambiguous', external: 'external', unresolved: 'unresolved'
};
const RES_COLOR = {
  exact: '#74b99a', scoped: '#8fb98a', unique: '#a9b47f',
  ambiguous: '#cfa04f', external: '#8f9fd8', unresolved: '#cf7f6c'
};
const KIND_COLOR = {
  function: '#e0a45e', method: '#c9975f', class: '#8f9fd8', struct: '#8f9fd8',
  interface: '#8f9fd8', enum: '#74b99a', field: '#7f8896', variable: '#7f8896'
};

async function api(path, params) {
  const q = params ? '?' + new URLSearchParams(params).toString() : '';
  const res = await fetch(path + q);
  const body = await res.json().catch(() => ({ error: 'bad json' }));
  if (!res.ok) throw new Error(body.error || res.statusText);
  return body;
}

// ---------------------------------------------------------------- syntax
const KW_C = new Set(('alignas alignof auto bool break case catch char class const constexpr ' +
  'continue decltype default delete do double else enum explicit extern false float for ' +
  'friend goto if inline int long mutable namespace new noexcept nullptr operator private ' +
  'protected public register reinterpret_cast return short signed sizeof static static_cast ' +
  'struct switch template this throw true try typedef typename union unsigned using virtual ' +
  'void volatile while dynamic_cast const_cast override final abstract assert synchronized ' +
  'extends implements import package interface instanceof super throws transient native ' +
  'boolean byte implements strictfp').split(' '));
const KW_PY = new Set(('and as assert async await break class continue def del elif else except ' +
  'False finally for from global if import in is lambda None nonlocal not or pass raise return ' +
  'True try while with yield match case type').split(' '));

const RES = new Map();
function tokenRe(lang) {
  if (RES.has(lang)) return RES.get(lang);
  const py = lang === 'python';
  const pre = py ? '(?!x)x' : '^[ \\t]*#[^\\n]*';
  const com = py ? '#[^\\n]*' : '\\/\\*[\\s\\S]*?\\*\\/|\\/\\/[^\\n]*';
  const str = py
    ? '"""[\\s\\S]*?"""|\'\'\'[\\s\\S]*?\'\'\'|"(?:\\\\.|[^"\\\\\\n])*"|\'(?:\\\\.|[^\'\\\\\\n])*\''
    : '"(?:\\\\.|[^"\\\\])*"|\'(?:\\\\.|[^\'\\\\])*\'';
  const num = '\\b(?:0[xX][0-9a-fA-F]+|\\d+\\.?\\d*(?:[eE][+-]?\\d+)?)[fFuUlL]*\\b';
  const id = '\\b[A-Za-z_]\\w*\\b';
  const ann = py ? '@[A-Za-z_][\\w.]*' : '(?!x)x';
  const re = new RegExp(`(${pre})|(${com})|(${str})|(${num})|(${id})|(${ann})`, 'gm');
  RES.set(lang, re);
  return re;
}

// tokens: [{a, b, cls}] offsets into text
function tokens(text, lang) {
  const py = lang === 'python';
  const re = tokenRe(lang);
  re.lastIndex = 0;
  const out = [];
  let m;
  while ((m = re.exec(text)) !== null) {
    if (!m[0]) { re.lastIndex++; continue; }
    const a = m.index, b = a + m[0].length;
    let cls = m[1] ? 'pre' : m[2] ? 'com' : m[3] ? 'str' : m[4] ? 'num' : m[6] ? 'ann' : null;
    if (!cls) {
      const w = m[0];
      if (py ? KW_PY.has(w) : KW_C.has(w)) cls = 'kw';
      else if (/^[ \t]*\(/.test(text.slice(b, b + 8))) cls = 'fn';
      else if (/^[A-Z]/.test(w) && w.length > 1) cls = 'type';
    }
    if (cls) out.push({ a, b, cls });
  }
  return out;
}

function highlightLines(chunk, lang) {
  const toks = tokens(chunk, lang);
  const lines = chunk.split('\n');
  const res = [];
  let pos = 0, ti = 0;
  for (const line of lines) {
    const s = pos, e = pos + line.length;
    pos = e + 1;
    const pieces = [];
    let cursor = s;
    while (ti < toks.length && toks[ti].b <= s) ti++;
    let j = ti;
    while (j < toks.length && toks[j].a < e) {
      const t = toks[j];
      const a = Math.max(t.a, s), b = Math.min(t.b, e);
      if (a > cursor) pieces.push([cursor, a, null]);
      pieces.push([a, b, t.cls]);
      cursor = Math.max(cursor, b);
      j++;
    }
    if (cursor < e) pieces.push([cursor, e, null]);
    res.push(pieces.map(([a, b, cls]) => {
      const txt = esc(chunk.slice(a, b));
      return cls ? `<span class="tok-${cls}">${txt}</span>` : txt;
    }).join(''));
  }
  return res;
}

function entityLines(ents, lo, hi) {
  const map = new Map();
  const spans = ents.filter((e) => e.end_line >= lo && e.line <= hi)
    .sort((a, b) => (b.end_line - b.line) - (a.end_line - a.line));
  for (const e of spans) {
    const a = Math.max(e.line, lo), b = Math.min(e.end_line, hi);
    for (let l = a; l <= b; l++) map.set(l, e);
  }
  return map;
}

// Renders a windowed, highlighted source view with an entity-tinted body.
function buildCode(host, text, lang, ents, opts = {}) {
  const all = text.split('\n');
  const total = all.length;
  const win = opts.window || 520;
  const target = Math.min(Math.max(opts.line || 1, 1), total);
  const box = document.createElement('div');
  box.className = 'codebox';
  box.innerHTML = `<header>
      <span>${esc(opts.title || lang)}</span>
      <span class="dim" data-range></span>
      <span class="sp"></span>
      <span class="dim hint">click a line number to open its symbol</span>
      <button class="btn" data-up hidden>↑ earlier</button>
      <button class="btn" data-down hidden>↓ later</button>
    </header>
    <div class="scroll"><pre class="code"></pre></div>`;
  host.appendChild(box);
  const pre = $('pre.code', box);
  const rangeEl = $('[data-range]', box);
  const upBtn = $('[data-up]', box), downBtn = $('[data-down]', box);

  function draw(start) {
    let s = Math.max(1, Math.min(start, total));
    let e = Math.min(total, s + win - 1);
    if (e - s + 1 < win) s = Math.max(1, e - win + 1);
    const chunk = all.slice(s - 1, e).join('\n');
    const hl = highlightLines(chunk, lang);
    const emap = entityLines(ents, s, e);
    let html = '';
    for (let i = 0; i < hl.length; i++) {
      const ln = s + i;
      const ent = emap.get(ln);
      const cls = ['line'];
      if (ent) cls.append ? cls.push('ent') : cls.push('ent');
      if (opts.line === ln) cls.push('hl');
      html += `<div class="${cls.join(' ')}" data-l="${ln}"${ent ? ` data-e="${ent.id}"` : ''}>` +
        `<span class="n">${ln}</span><span class="c">${hl[i] || ' '}</span></div>`;
    }
    pre.innerHTML = html;
    rangeEl.textContent = `lines ${s}–${e} of ${total}`;
    upBtn.hidden = s <= 1;
    downBtn.hidden = e >= total;
    upBtn.onclick = () => draw(Math.max(1, s - win + 40));
    downBtn.onclick = () => draw(Math.min(total, e - 40));
    const hit = opts.line && opts.line >= s && opts.line <= e ? $(`.line[data-l="${opts.line}"]`, pre) : null;
    if (hit && opts.scroll !== false) hit.scrollIntoView({ block: 'center' });
  }
  const start = Math.max(1, target - Math.floor(win / 3));
  draw(start);

  pre.addEventListener('click', (ev) => {
    const num = ev.target.closest('.n');
    if (!num) return;
    const line = num.closest('.line');
    if (line && line.dataset.e) location.hash = `#/sym/${line.dataset.e}?line=${line.dataset.l}`;
  });
  return box;
}

// ---------------------------------------------------------------- views
let OV = null;
async function overview() {
  if (!OV) OV = await api('/api/overview');
  return OV;
}

async function viewOverview(host) {
  const d = await overview();
  const resTotal = d.resolution.reduce((s, r) => s + r.n, 0) || 1;
  const maxLang = Math.max(1, ...d.langs.map((l) => l.loc));
  document.title = 'ken · ' + (d.db || 'index');
  host.innerHTML = `<div class="wrap">
    <p class="eyebrow">Index · ${esc(d.db || '')}</p>
    <h1>${esc(d.root || 'codebase')}</h1>
    <p class="mut" style="margin:8px 0 22px;max-width:74ch">
      Every symbol, reference and dependency below was extracted from source with tree-sitter.
      No compiler, no build system: point it at a tree and it reads what is there.
    </p>
    <div class="ledger">
      <div><b>${n(d.files)}</b><span>files</span></div>
      <div><b>${n(d.loc)}</b><span>lines</span></div>
      <div><b>${n(d.entities)}</b><span>entities</span></div>
      <div><b>${n(d.refs)}</b><span>references</span></div>
    </div>

    <div class="rule"></div>
    <div class="cols">
      <section>
        <h2>Languages</h2>
        <div class="bars" style="margin-top:14px">
          ${d.langs.map((l) => `<div class="bar">
            <span class="t">${esc(l.lang)}</span>
            <span class="track"><span class="fill" style="width:${(100 * l.loc / maxLang).toFixed(1)}%"></span></span>
            <span class="v">${n(l.files)} files · ${n(l.loc)} loc</span>
          </div>`).join('')}
        </div>
        <h2 style="margin-top:26px">Symbols</h2>
        <div class="bars" style="margin-top:14px">
          ${d.kinds.slice(0, 8).map((k) => `<div class="bar">
            <span class="t">${esc(k.kind)}</span>
            <span class="track"><span class="fill" style="width:${(100 * k.n / d.kinds[0].n).toFixed(1)}%;background:${KIND_COLOR[k.kind] || '#59616e'}"></span></span>
            <span class="v">${n(k.n)}</span>
          </div>`).join('')}
        </div>
      </section>

      <section>
        <h2>Reference resolution</h2>
        <div style="margin-top:14px">
          <div class="stackbar">
            ${d.resolution.map((r) => `<i title="${esc(r.resolution)} ${n(r.n)}"
              style="width:${(100 * r.n / resTotal).toFixed(2)}%;background:${RES_COLOR[r.resolution] || '#444'}"></i>`).join('')}
          </div>
          <div class="legend">
            ${d.resolution.map((r) => `<span><i style="background:${RES_COLOR[r.resolution] || '#444'}"></i>
              ${esc(r.resolution)} <b class="dim">${(100 * r.n / resTotal).toFixed(1)}%</b></span>`).join('')}
          </div>
          <p class="dim" style="font-size:11.5px;line-height:1.6;margin:12px 0 0">
            <b>exact</b> unique qualified match · <b>scoped</b> narrowed by file or directory ·
            <b>unique</b> single candidate · <b>external</b> builtin or library ·
            <b>ambiguous</b> several candidates · <b>unresolved</b> no candidate seen.
            Name-based resolution trades completeness for needing no compile database.
          </p>
        </div>

        <h2 style="margin-top:26px">Dependencies</h2>
        <div class="bars" style="margin-top:14px">
          <div class="bar"><span class="t">edges</span><span class="track"></span>
            <span class="v">${n(d.deps)}</span></div>
        </div>
        <div style="display:flex;gap:8px;margin-top:22px;flex-wrap:wrap">
          <a class="btn" href="#/files">Browse files</a>
          <a class="btn" href="#/metrics">Metrics</a>
        </div>
      </section>
    </div>

    <div class="rule"></div>
    <div class="cols">
      <section>
        <h2>Most complex functions</h2>
        <table class="grid" style="margin-top:10px">
          <thead><tr><th>symbol</th><th class="r">complexity</th><th class="r">loc</th></tr></thead>
          <tbody>${d.top_complex.map((e) => `<tr>
            <td>${hash(e.id, e.qualname)}<div class="path">${esc(shortPath(e.path, d.root))}:${e.line}</div></td>
            <td class="r mono">${e.complexity}</td><td class="r mono dim">${n(e.loc)}</td></tr>`).join('')}</tbody>
        </table>
      </section>
      <section>
        <h2>Most called</h2>
        <table class="grid" style="margin-top:10px">
          <thead><tr><th>symbol</th><th class="r">calls</th></tr></thead>
          <tbody>${d.top_called.map((e) => `<tr>
            <td>${hash(e.id, e.qualname)}<div class="path">${esc(shortPath(e.path, d.root))}:${e.line}</div></td>
            <td class="r mono">${n(e.n)}</td></tr>`).join('')}</tbody>
        </table>
      </section>
    </div>
  </div>`;
  $('#dbname').textContent = d.db || '';
}

async function viewFiles(host, params) {
  const d = await overview();
  const path = params.get('path') || '';
  const t = await api('/api/tree', { path, root: d.root });
  const parts = path ? path.split('/') : [];
  const crumbs = ['<a href="#/files">' + esc(d.root || 'root') + '</a>']
    .concat(parts.map((p, i) => `<a href="#/files?path=${encodeURIComponent(parts.slice(0, i + 1).join('/'))}">${esc(p)}</a>`))
    .join(' <span class="dim">/</span> ');
  host.innerHTML = `<div class="wrap wide">
    <div class="treehead">
      <p class="crumbs">${crumbs}</p>
      <span class="dim mono" style="font-size:11px">${n(t.total_files)} files below</span>
    </div>
    <table class="grid">
      <thead><tr><th style="width:60%">name</th><th>kind</th><th class="r">files</th><th class="r">lines</th></tr></thead>
      <tbody>${t.entries.map((e) => {
        if (e.type === 'dir') {
          const langs = Object.entries(e.langs).sort((a, b) => b[1] - a[1]).map((x) => x[0]).join(' ');
          return `<tr><td><a class="sym" href="#/files?path=${encodeURIComponent(e.path)}">${esc(e.name)}/</a>
            <div class="path">${esc(langs)}</div></td><td class="dim mono">dir</td>
            <td class="r mono">${n(e.files)}</td><td class="r mono dim">${n(e.loc)}</td></tr>`;
        }
        return `<tr><td><a class="sym" href="#/file/${e.id}">${esc(e.name)}</a>
          <div class="path">${esc(e.path)}</div></td><td>${kindTag(e.lang)}</td>
          <td class="r mono dim">—</td><td class="r mono">${n(e.loc)}</td></tr>`;
      }).join('') || '<tr><td colspan="4" class="empty">No files under this directory.</td></tr>'}</tbody>
    </table>
  </div>`;
}

async function viewFile(host, params, id) {
  const d = await overview();
  const fid = Number(id);
  const f = await api('/api/file', { id: fid });
  const line = Number(params.get('line') || 0);
  document.title = f.file.path.split('/').pop() + ' · ken';
  host.innerHTML = `<div class="wrap wide">
    <div class="filehead">
      <span class="p">${esc(shortPath(f.file.path, d.root))}</span>
      ${kindTag(f.file.lang)}
      <span class="dim mono" style="font-size:11.5px">${n(f.file.loc)} loc · ${n(f.file.bytes)} bytes · ${n(f.entities.length)} entities</span>
      <span class="dim mono" style="font-size:11.5px">${f.deps_out.length} deps out · ${f.deps_in.length} in</span>
    </div>
    <div class="filegrid">
      <div data-code></div>
      <aside class="side">
        <section>
          <h2>Outline</h2>
          <div class="outline" data-outline></div>
        </section>
        <section>
          <h2>Depends on</h2>
          <div class="list" data-depsout></div>
        </section>
        <section>
          <h2>Used by</h2>
          <div class="list" data-depsin></div>
        </section>
      </aside>
    </div>
  </div>`;

  buildCode($('[data-code]', host), f.source, f.file.lang, f.entities,
    { line: line || 1, title: f.file.path, scroll: !!line });

  // outline: nested by parent_id
  const byId = new Map(f.entities.map((e) => [e.id, e]));
  const depth = (e) => {
    let d2 = 0, p = e.parent_id;
    while (p && byId.has(p) && d2 < 6) { d2++; p = byId.get(p).parent_id; }
    return d2;
  };
  $('[data-outline]', host).innerHTML = f.entities.map((e) => `<a class="o d${depth(e)}" href="#/file/${fid}?line=${e.line}">
      <span>${e.kind === 'function' || e.kind === 'method' ? 'ƒ' : e.kind === 'class' || e.kind === 'struct' ? '◇' : '·'}</span>
      <span>${esc(e.name)}</span><span class="ln">${e.line}</span></a>`).join('') ||
    '<p class="dim">No entities.</p>';

  // one row = kind chip + the file it points at. The unresolved raw import text
  // (`sysmlpy.formatting`) is a different thing from a path, so it goes on a
  // second line rather than sharing a column with the path.
  const depRow = (kind, raw, target, href) => {
    const label = shortPath(target || raw, d.root);
    const tag = href ? `<a href="${href}">${esc(label)}</a>` : `<span>${esc(label)}</span>`;
    return `<div class="row dep"><span class="k">${esc(kind)}</span>
      <span class="t" title="${esc(target || raw)}">${tag}</span></div>`;
  };
  $('[data-depsout]', host).innerHTML = f.deps_out.map((x) =>
    depRow(x.kind, x.raw, x.dst_path, x.dst_file ? `#/file/${x.dst_file}` : '')).join('') ||
    '<p class="dim" style="font-size:11.5px">No dependencies recorded.</p>';
  $('[data-depsin]', host).innerHTML = f.deps_in.map((x) =>
    depRow(x.kind, '', x.src_path, `#/file/${x.src_file}`)).join('') ||
    '<p class="dim" style="font-size:11.5px">Nothing depends on this file.</p>';
}

async function viewSymbol(host, params, id) {
  const d = await overview();
  const s = await api('/api/symbol', { id });
  const e = s.entity;
  document.title = e.qualname + ' · ken';
  const calls = s.out.filter((r) => r.kind === 'call' || r.kind === 'instantiate');
  const inherits = s.out.filter((r) => r.kind === 'inherit');
  const others = s.out.filter((r) => !['call', 'instantiate', 'inherit'].includes(r.kind));
  const resCount = s.ref_kinds.reduce((a, r) => a + r.n, 0);

  host.innerHTML = `<div class="wrap">
    <div class="symhead">
      <div class="id">
        <div class="crumb">${s.parents.map((p) => `<a href="#/sym/${p.id}">${esc(p.qualname)}</a>`).join(' <span class="dim">›</span> ')}
          ${s.parents.length ? '<span class="dim">›</span> ' : ''}<span>${esc(e.name)}</span></div>
        <h1>${esc(e.qualname)} ${kindTag(e.kind)} ${e.decl ? '<span class="badge">decl</span>' : ''}</h1>
        <div class="loc">
          <span><a class="link" href="#/file/${e.file_id}?line=${e.line}">${esc(shortPath(e.path, d.root))}:${e.line}</a></span>
          <span>lines <b>${e.end_line - e.line + 1}</b></span>
          ${e.complexity ? `<span>complexity <b>${e.complexity}</b></span>` : ''}
          <span>refs out <b>${n(resCount)}</b></span>
        </div>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <a class="btn primary" href="#/graph/${e.id}?dir=down">Call graph ↓</a>
        <a class="btn" href="#/graph/${e.id}?dir=up">Callers ↑</a>
        <a class="btn" href="#/file/${e.file_id}?line=${e.line}">Source</a>
      </div>
    </div>
    <div class="rule"></div>
    <div class="symgrid">
      <div>
        <div data-code></div>

        ${others.length ? `<section class="sec">
          <header><h2>Other references</h2></header>
          <table class="grid"><thead><tr><th>kind</th><th>target</th><th>resolution</th></tr></thead>
            <tbody>${others.slice(0, 40).map((r) => `<tr><td class="dim mono">${esc(r.kind)}</td>
              <td class="mono">${esc(r.target)}</td><td class="dim">${esc(r.resolution || '')}</td></tr>`).join('')}</tbody></table>
        </section>` : ''}
      </div>

      <aside class="side">
        <section>
          <h2>Facts</h2>
          <dl class="kv">
            <dt>kind</dt><dd>${esc(e.kind)}</dd>
            <dt>lang</dt><dd>${esc(e.lang)}</dd>
            <dt>file</dt><dd><a class="link" href="#/file/${e.file_id}">${esc(e.path.split('/').pop())}</a></dd>
            <dt>lines</dt><dd>${e.line}–${e.end_line}</dd>
            <dt>loc</dt><dd>${e.end_line - e.line + 1}</dd>
            <dt>complexity</dt><dd>${e.complexity || '—'}</dd>
            <dt>definition</dt><dd>${e.decl ? 'declaration' : 'definition'}</dd>
          </dl>
        </section>

        <!-- the two lists that make a symbol page navigable sit beside the code,
             not under 800px of it -->
        <section>
          <h2>Calls<span class="c mono">${calls.length}</span>
            <a class="dkind" href="#/seq/${e.id}" title="sequence diagram from this method">sequence →</a></h2>
          <div class="list">${calls.map((r) => {
            const meta = [r.target_path ? shortPath(r.target_path, d.root) + (r.target_line ? ':' + r.target_line : '') : '',
              RES_LABEL[r.resolution] || r.resolution || '—', r.n > 1 ? '×' + n(r.n) : ''].filter(Boolean).join(' · ');
            const inner = `<span class="t">${esc(leafName(r.target_qual || r.target))}</span>
              <span class="s">${esc(meta)}</span>`;
            return r.target_id
              ? `<a class="row2" href="#/sym/${r.target_id}" title="${esc(r.target_qual || r.target)}">${inner}</a>`
              : `<div class="row2" title="${esc(r.target)}">${inner}</div>`;
          }).join('') || '<p class="dim" style="font-size:11.5px">No outgoing calls recorded.</p>'}</div>
        </section>

        <section>
          <h2>Called by<span class="c mono">${s.callers.length}</span></h2>
          <div class="list">${s.callers.map((r) => {
            const inner = `<span class="t">${esc(leafName(r.src_qual) || 'file scope')}</span>
              <span class="s">${esc(shortPath(r.path, d.root) + ':' + r.line + (r.n > 1 ? ' · ×' + n(r.n) : ''))}</span>`;
            return r.src_entity
              ? `<a class="row2" href="#/sym/${r.src_entity}" title="${esc(r.src_qual)}">${inner}</a>`
              : `<div class="row2">${inner}</div>`;
          }).join('') || '<p class="dim" style="font-size:11.5px">Nothing in the index calls this.</p>'}</div>
        </section>

        ${inherits.length ? `<section>
          <h2>Inherits from</h2>
          <div class="list">${inherits.map((r) => `<div class="row2">
            <span class="t">${r.target_qual ? hash(r.target_id, leafName(r.target_qual)) : `<span class="dim mono">${esc(r.target)}</span>`}</span>
            <span class="s">${esc(r.kind)} · ${esc(r.resolution || '')}</span></div>`).join('')}</div>
        </section>` : ''}

        ${s.subclasses.length ? `<section>
          <h2>Subclasses<span class="c mono">${s.subclasses.length}</span></h2>
          <div class="list">${s.subclasses.map((r) => `<div class="row2">
            <span class="t">${hash(r.id, r.qualname)}</span>
            <span class="s">${esc(shortPath(r.path, d.root))}:${r.line}</span></div>`).join('')}</div>
        </section>` : ''}
        ${s.children.length ? `<section>
          <h2>Children</h2>
          <div class="list">${s.children.slice(0, 60).map((c) => `<div class="row">
            <span class="k">${c.kind === 'method' ? 'ƒ' : '·'}</span>
            <span>${hash(c.id, c.name)}</span><span class="c">${c.line}</span></div>`).join('')}</div>
        </section>` : ''}
        <section>
          <h2>Reference mix</h2>
          <div class="list">${s.ref_kinds.map((r) => `<div class="row">
            <span class="k">·</span><span class="mut">${esc(r.kind)}</span><span class="c">${n(r.n)}</span></div>`).join('') ||
            '<p class="dim" style="font-size:11.5px">None.</p>'}</div>
        </section>
      </aside>
    </div>
  </div>`;

  if (e.file_id) {
    const f = await api('/api/file', { id: e.file_id });
    buildCode($('[data-code]', host), f.source, f.file.lang, f.entities,
      { line: e.line, title: 'source · ' + shortPath(e.path, d.root) });
  }
}

async function viewGraph(host, params, id) {
  const depth = Number(params.get('depth') || 2);
  const dir = params.get('dir') || 'down';
  const g = await api('/api/graph', { id, depth, dir });
  const node = g.nodes.find((x) => x.id === g.root) || {};
  document.title = 'graph · ' + (node.qualname || '');
  host.innerHTML = `<div class="wrap wide">
    <div class="graphhead">
      <div>
        <div class="crumb mono dim" style="font-size:11px;margin-bottom:6px">CALL GRAPH</div>
        <h1 style="font-size:17px">${esc(node.qualname || '')}</h1>
      </div>
      <span class="sp" style="margin-left:auto"></span>
      <div class="seg" data-dir>
        <button data-v="down" class="${dir === 'down' ? 'on' : ''}">callees</button>
        <button data-v="up" class="${dir === 'up' ? 'on' : ''}">callers</button>
        <button data-v="both" class="${dir === 'both' ? 'on' : ''}">both</button>
      </div>
      <div class="seg" data-depth>
        ${[1, 2, 3, 4].map((x) => `<button data-v="${x}" class="${x === depth ? 'on' : ''}">${x}</button>`).join('')}
      </div>
      <a class="btn" href="#/sym/${g.root}">Symbol</a>
    </div>
    ${g.truncated ? `<p class="dim mono" style="font-size:11px;margin:0 0 8px">Showing the first ${g.limit} nodes; lower the depth to see the rest.</p>` : ''}
    <div class="graphbox" data-graph>
      <div class="overlay">
        <button class="btn" data-fit>Fit</button>
        <span class="dim mono" style="font-size:10.5px">${g.nodes.length} nodes · ${g.edges.length} edges</span>
      </div>
      <div class="ghint">drag to pan · scroll to zoom · click a node</div>
    </div>
  </div>`;

  $$('[data-dir] button', host).forEach((b) => b.onclick = () => {
    location.hash = `#/graph/${id}?dir=${b.dataset.v}&depth=${depth}`;
  });
  $$('[data-depth] button', host).forEach((b) => b.onclick = () => {
    location.hash = `#/graph/${id}?dir=${dir}&depth=${b.dataset.v}`;
  });

  const box = $('[data-graph]', host);
  const svg = drawGraph(box, g);
  $('[data-fit]', host).onclick = () => fit(svg);
}

function drawGraph(box, g) {
  const W = 196, H = 36, GAPX = 70, GAPY = 14, SUBX = 14;

  // Forty callers of one helper are a smear at any zoom level, and the reading
  // question is usually "who in this file?" not "who exactly". Above a legible
  // number of neighbours, collapse the ones that share a file into one bucket
  // that keeps the count and opens the file - so the picture stays readable
  // without silently dropping anything.
  const MAXLEVEL = 16;
  const perLevel = new Map();
  for (const nd of g.nodes) if (nd.id !== g.root) perLevel.set(nd.depth, (perLevel.get(nd.depth) || 0) + 1);
  const show = [], map = new Map();
  for (const nd of g.nodes) {
    if (nd.id === g.root || (perLevel.get(nd.depth) || 0) <= MAXLEVEL) {
      map.set(nd.id, nd.id);
      show.push({ ...nd, count: 1 });
      continue;
    }
    const gid = `f:${nd.depth}:${nd.file_id}`;
    map.set(nd.id, gid);
    let b = show.find((x) => x.id === gid);
    if (!b) {
      b = { id: gid, kind: 'file', qualname: '', name: '', path: nd.path, file_id: nd.file_id, depth: nd.depth, count: 0, grouped: true };
      show.push(b);
    }
    b.count++;
  }
  const grouped = show.length < g.nodes.length;

  const cols = new Map();
  for (const nd of show) {
    if (!cols.has(nd.depth)) cols.set(nd.depth, []);
    cols.get(nd.depth).push(nd);
  }
  const pos = new Map();
  const depths = [...cols.keys()].sort((a, b) => a - b);
  const maxDepth = Math.max(0, ...depths);
  const mirror = g.direction === 'up';

  // A depth level can hold hundreds of symbols (every caller of a popular
  // helper). Stacking them in one column makes the graph 10k pixels tall, so
  // "fit" collapses it to an unreadable smear. Wrap each level into enough
  // sub-columns to keep the block roughly landscape instead.
  const shape = new Map();
  for (const dnum of depths) {
    const n = cols.get(dnum).length;
    const k = Math.max(1, Math.min(n, Math.round(Math.sqrt(1.5 * n * (H + GAPY) / (W + SUBX)))));
    shape.set(dnum, { k, rows: Math.ceil(n / k) });
  }
  const blockW = Math.max(...depths.map((dnum) => {
    const { k } = shape.get(dnum);
    return k * (W + SUBX) - SUBX;
  }));
  const pitch = blockW + GAPX * 2;
  const originX = 40;

  for (const dnum of depths) {
    const col = cols.get(dnum);
    if (dnum > 0) {
      const prev = (cols.get(dnum - 1) || []).map((x) => pos.get(x.id)?.y ?? 0);
      const mid = prev.length ? prev.reduce((a, b) => a + b, 0) / prev.length : 0;
      col.sort((a, b) => Math.abs((pos.get(a.id)?.y ?? 0) - mid) - Math.abs((pos.get(b.id)?.y ?? 0) - mid));
      if (!prev.length) col.sort((a, b) => a.qualname.localeCompare(b.qualname));
    }
    const { k, rows } = shape.get(dnum);
    const colX = originX + (mirror ? (maxDepth - dnum) : dnum) * pitch;
    const span = rows * (H + GAPY) - GAPY;
    col.forEach((nd, i) => {
      const sub = Math.floor(i / rows), row = i % rows;
      pos.set(nd.id, {
        x: colX + sub * (W + SUBX),
        y: -span / 2 + row * (H + GAPY),
        w: W, h: H,
      });
    });
  }

  const pts = [...pos.values()];
  const minX = Math.min(...pts.map((p) => p.x)), maxX = Math.max(...pts.map((p) => p.x + p.w));
  const minY = Math.min(...pts.map((p) => p.y)), maxY = Math.max(...pts.map((p) => p.y + p.h));
  const pad = 46;
  const vb = { x: minX - pad, y: minY - pad, w: (maxX - minX) + pad * 2, h: (maxY - minY) + pad * 2 };

  const edges = g.edges.map((e) => {
    const s = map.get(e.s), d = map.get(e.d);
    if (s === d) return '';           // a file calling itself says nothing
    const a = pos.get(s), b = pos.get(d);
    if (!a || !b) return '';
    const back = b.x < a.x;              // target sits to the left (callers view)
    const x1 = back ? a.x : a.x + a.w, y1 = a.y + a.h / 2;
    const x2 = back ? b.x + b.w : b.x, y2 = b.y + b.h / 2;
    const dx = Math.max(28, Math.abs(x2 - x1) / 2);
    return `<path class="gedge" data-s="${s}" data-d="${d}"
      d="M${x1} ${y1} C${x1 + dx} ${y1} ${x2 - dx} ${y2} ${x2} ${y2}"/>`;
  }).join('');

  const drawn = show.map((nd) => {
    const p = pos.get(nd.id);
    if (nd.grouped) {
      const base = nd.path.split('/').pop();
      const label = base.length > 24 ? '…' + base.slice(-23) : base;
      return `<g class="gnode bucket" data-id="${nd.id}" data-file="${nd.file_id}"
        transform="translate(${p.x} ${p.y})">
      <title>${esc(n(nd.count))} symbols in ${esc(nd.path)}</title>
      <rect width="${W}" height="${H}" rx="3"></rect>
      <circle class="kdot" cx="11" cy="${H / 2}" r="3" fill="#4a525e"></circle>
      <text x="22" y="15">${esc(label)}</text>
      <text class="q" x="22" y="27">${esc(n(nd.count))} symbols</text>
    </g>`;
    }
    // In a call graph most neighbours share a class, so the leaf name is what
    // tells them apart. Put the leaf on its own line and demote the qualifier
    // to a second, dimmer line instead of eliding one of the two away.
    const full = nd.qualname || nd.name || '';
    const m = /^(.*[.:$])([^.:$]+)$/.exec(full);
    const qual = m ? m[1].replace(/[.:$]+$/, '') : '';
    let leaf = m ? m[2] : full;
    if (leaf.length > 26) leaf = leaf.slice(0, 25) + '…';
    const qualCut = qual.length > 30 ? '…' + qual.slice(-29) : qual;
    const col = KIND_COLOR[nd.kind] || '#59616e';
    return `<g class="gnode ${nd.id === g.root ? 'root' : ''}" data-id="${nd.id}"
        transform="translate(${p.x} ${p.y})">
      <title>${esc(nd.qualname)} · ${esc(nd.path)}:${nd.line}</title>
      <rect width="${W}" height="${H}" rx="3"></rect>
      <circle class="kdot" cx="11" cy="${H / 2}" r="3" fill="${col}"></circle>
      <text x="22" y="${qual ? 15 : H / 2 + 4}">${esc(leaf)}</text>
      ${qual ? `<text class="q" x="22" y="27">${esc(qualCut)}</text>` : ''}
    </g>`;
  }).join('');

  box.insertAdjacentHTML('afterbegin', `
    <svg viewBox="${vb.x} ${vb.y} ${vb.w} ${vb.h}" preserveAspectRatio="xMidYMid meet">
      <g class="scene">
        <g class="gedges">${edges}</g>
        <g class="gnodes">${drawn}</g>
      </g>
    </svg>`);
  const svg = $('svg', box);
  const scene = $('g.scene', svg);
  const st = { k: 1, x: 0, y: 0 };

  const apply = () => scene.setAttribute('transform', `translate(${st.x} ${st.y}) scale(${st.k})`);
  // Fit content into the element box. `floor` keeps the initial view legible:
  // a graph whose bounding box is 10k pixels tall would otherwise open at
  // scale .04, i.e. a dot cloud. When the scale would drop below the floor we
  // clamp and centre on the root node instead, and the user pans from there.
  const fit = (floor) => {
    const r = box.getBoundingClientRect();
    if (!r.width || !r.height) return;
    const k0 = Math.min(r.width / vb.w, r.height / vb.h) * 0.96;
    svg.setAttribute('viewBox', `0 0 ${r.width} ${r.height}`);
    if (floor && k0 < floor) {
      st.k = floor;
      const root = pos.get(g.root) || { x: vb.x + vb.w / 2, y: vb.y + vb.h / 2, w: 0, h: 0 };
      st.x = r.width / 2 - (root.x + root.w / 2) * floor;
      st.y = r.height / 2 - (root.y + root.h / 2) * floor;
    } else {
      st.k = k0;
      st.x = (r.width - vb.w * k0) / 2 - vb.x * k0;
      st.y = (r.height - vb.h * k0) / 2 - vb.y * k0;
    }
    apply();
  };
  box.__fit = () => fit(0.12);       // the Fit button always gives the overview
  if (grouped) {
    const ov = $('.overlay', box);
    const cnt = $('.dim.mono', ov);
    if (cnt) cnt.textContent = `${show.length} files · ${n(g.nodes.length)} symbols`;
  }
  requestAnimationFrame(() => fit(0.62));
  // keep the user's pan/zoom across resizes; only the pixel viewport changes
  addEventListener('resize', () => {
    if (!document.body.contains(svg)) return;
    const r = box.getBoundingClientRect();
    if (r.width && r.height) svg.setAttribute('viewBox', `0 0 ${r.width} ${r.height}`);
  }, { passive: true });

  let drag = null;
  const pt = (ev) => {
    const r = svg.getBoundingClientRect();
    return { x: ev.clientX - r.left, y: ev.clientY - r.top };
  };
  svg.addEventListener('pointerdown', (ev) => {
    drag = { ...pt(ev), x0: st.x, y0: st.y };
    svg.classList.add('drag');
    svg.setPointerCapture(ev.pointerId);
  });
  svg.addEventListener('pointermove', (ev) => {
    if (!drag) return;
    const p = pt(ev);
    st.x = drag.x0 + (p.x - drag.x);
    st.y = drag.y0 + (p.y - drag.y);
    apply();
  });
  svg.addEventListener('pointerup', (ev) => {
    if (drag && Math.hypot(pt(ev).x - drag.x, pt(ev).y - drag.y) > 4) svg.dataset.moved = '1';
    drag = null;
    svg.classList.remove('drag');
  });
  svg.addEventListener('click', (ev) => {
    if (svg.dataset.moved) { svg.dataset.moved = ''; return; }
    const gn = ev.target.closest('.gnode');
    if (!gn) return;
    if (gn.dataset.file) location.hash = `#/file/${gn.dataset.file}`;
    else location.hash = `#/sym/${gn.dataset.id}`;
  });
  svg.addEventListener('wheel', (ev) => {
    ev.preventDefault();
    const p = pt(ev);
    const f = ev.deltaY < 0 ? 1.12 : 1 / 1.12;
    const k2 = Math.min(4, Math.max(0.12, st.k * f));
    st.x = p.x - (p.x - st.x) * (k2 / st.k);
    st.y = p.y - (p.y - st.y) * (k2 / st.k);
    st.k = k2;
    apply();
  }, { passive: false });
  $$('.gnode', svg).forEach((gn) => {
    gn.addEventListener('mouseenter', () => {
      const id = gn.dataset.id;
      $$('.gedge', svg).forEach((e) => e.classList.toggle('hot', e.dataset.s === id || e.dataset.d === id));
    });
    gn.addEventListener('mouseleave', () => $$('.gedge.hot', svg).forEach((e) => e.classList.remove('hot')));
  });
  return svg;
}

function fit(svg) {
  const box = svg.parentElement;
  if (box && box.__fit) box.__fit();
}

// ---------------------------------------------------------------- diagrams
async function viewUml(host, params) {
  const path = params.get('path') || '';
  const limit = Number(params.get('limit') || 32);
  const d = await api('/api/uml', { path, limit });
  const inh = d.edges.filter((e) => e.kind === 'inherit').length;
  document.title = 'diagrams · ken';
  host.innerHTML = `<div class="wrap wide">
    <div class="graphhead">
      <div>
        <div class="crumb mono dim" style="font-size:11px;margin-bottom:6px">CLASS DIAGRAM
          <a class="dkind" href="#/packages">packages →</a></div>
        <h1 style="font-size:17px">${path ? esc(path) : 'Whole index'}</h1>
      </div>
      <span class="sp" style="margin-left:auto"></span>
      <form class="scopeform" data-scope>
        <input name="path" value="${esc(path)}" placeholder="scope: a directory…" spellcheck="false" autocomplete="off">
      </form>
      <div class="seg" data-limit>
        ${[16, 32, 48, 64].map((x) => `<button data-v="${x}" class="${x === limit ? 'on' : ''}">${x}</button>`).join('')}
      </div>
    </div>
    <p class="dim mono" style="font-size:11px;margin:0 0 8px">
      ${n(d.shown)} of ${n(d.total_classes)} classes · <span class="ue-key"><i class="ue-line"></i>${inh} inherit</span> · <span class="ue-key"><i class="ue-line dash"></i>${d.edges.length - inh} use</span> · ranked by structure, not method count
    </p>
    <div class="graphbox" data-uml>
      <div class="overlay right"><button class="btn" data-fit>Fit</button></div>
      <div class="ghint">drag to pan · scroll to zoom · click a box for its file</div>
    </div>
  </div>`;

  $('[data-scope]', host).onsubmit = (ev) => {
    ev.preventDefault();
    const v = ev.target.path.value.trim().replace(/^\/+|\/+$/g, '');
    location.hash = `#/uml?path=${encodeURIComponent(v)}&limit=${limit}`;
  };
  $$('[data-limit] button', host).forEach((b) => {
    b.onclick = () => { location.hash = `#/uml?path=${encodeURIComponent(path)}&limit=${b.dataset.v}`; };
  });
  const box = $('[data-uml]', host);
  const svg = drawUml(box, d);
  const fb = $('[data-fit]', host);
  if (svg) fb.onclick = () => fit(svg); else fb.hidden = true;
}

function drawUml(box, d) {
  if (!d.classes.length) {
    box.insertAdjacentHTML('beforeend', '<p class="dim" style="padding:24px;margin:0">No classes in this scope.</p>');
    return null;
  }
  const W = 212, HEAD = 26, ROW = 14, MAXROW = 5;
  const COLGAP = 96, ROWGAP = 22, PAD = 40;
  const cls = new Map(d.classes.map((c) => [c.id, c]));
  const ext = new Map(d.externals.map((x) => [x.id, x]));

  // Layout is by package, not by inheritance depth: for someone reading a
  // codebase the first question is "what lives in this directory and what
  // does it build?", and deep hierarchies would otherwise thin the canvas
  // into eight nearly-empty columns. Same-directory classes also keep the
  // three generated parser copies visibly grouped instead of interleaved.
  const root = String(d.root || '').replace(/\/+$/, '');
  const rel = (p) => (root && String(p).startsWith(root) ? String(p).slice(root.length).replace(/^\//, '') : String(p));
  const dirOf = (p) => { const q = rel(p); const i = q.lastIndexOf('/'); return i < 0 ? '.' : q.slice(0, i); };
  const boxH = (c) => {
    const total = c.methods.length + c.other.length;
    const rows = Math.min(total, MAXROW) + (total > MAXROW ? 1 : 0);   // the "+N more" row
    return HEAD + Math.max(1, rows) * ROW + 8;
  };
  const PGAP = 14, PHDR = 26, PANELGAP = 42, EXTRA = 44;

  const groups = new Map();
  for (const c of d.classes) {
    const k = dirOf(c.path);
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(c);
  }

  // Each directory becomes one or more bounded stacks: a single generated
  // module can hold thirty classes, and an unbounded stack would push the
  // whole diagram to a 0.3 scale where nothing is legible. Stacks are packed
  // into columns of at most CHMAX pixels, so the drawing stays page-shaped.
  const ex = [...ext.values()].sort((a, b) => a.name.localeCompare(b.name));
  const STACK = 6;
  const stacks = [];
  for (const [dir, list] of groups) {
    const sorted = list.sort((a, b) => b.nmethods - a.nmethods || a.name.localeCompare(b.name));
    for (let i = 0; i < sorted.length; i += STACK) {
      const part = sorted.slice(i, i + STACK);
      stacks.push({
        dir, part, total: sorted.length, cont: i > 0,
        h: PHDR + part.reduce((s, c) => s + boxH(c) + PGAP, 0) + 4,
      });
    }
  }
  stacks.sort((a, b) => b.total - a.total || (a.cont - b.cont) || a.dir.localeCompare(b.dir));
  if (ex.length) {
    stacks.push({ dir: 'outside this index', part: [], ext: true, total: 0, cont: false,
      h: PHDR + ex.reduce((s, x) => s + 34 + PGAP, 0) + 4 });
  }

  // Pick the column count that best fills a wide viewport (about 1.9:1).
  // Fixing the column height instead lets a narrow diagram waste half the
  // canvas and pushes the scale down to where 10 px labels cannot be read.
  const GUT = 28, COLW = W + EXTRA;
  const Hsum = stacks.reduce((acc, st) => acc + st.h + PANELGAP, 0);
  const maxSt = Math.max(...stacks.map((st) => st.h));
  let nCols = 1, bestErr = Infinity;
  for (let n = 1; n <= 10; n += 1) {
    const w = GUT * 2 + n * COLW - EXTRA;
    const h = Math.max(Hsum / n, maxSt) + GUT * 2;
    const err = Math.abs(Math.log((w / h) / 1.9));
    if (err < bestErr) { bestErr = err; nCols = n; }
  }
  const CHMAX = Math.max(maxSt, Math.ceil(Hsum / nCols));

  const pos = new Map();
  const bottoms = [];
  for (const st of stacks) {
    let ci = bottoms.findIndex((b) => b + st.h <= CHMAX);
    if (ci < 0) { ci = bottoms.length; bottoms.push(PAD); }
    st.x = PAD + ci * COLW;
    st.y = bottoms[ci];
    let y = st.y + PHDR;
    for (const c of st.part) { pos.set(c.id, { x: st.x, y, w: W, h: boxH(c) }); y += boxH(c) + PGAP; }
    if (st.ext) for (const x of ex) { pos.set(x.id, { x: st.x, y, w: W, h: 34 }); y += 34 + PGAP; }
    bottoms[ci] = y + PANELGAP;
  }

  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const p of pos.values()) {
    minX = Math.min(minX, p.x); maxX = Math.max(maxX, p.x + p.w);
    minY = Math.min(minY, p.y); maxY = Math.max(maxY, p.y + p.h);
  }
  for (const st of stacks) {
    minX = Math.min(minX, st.x - 16); maxX = Math.max(maxX, st.x + W + 16);
    minY = Math.min(minY, st.y - 10); maxY = Math.max(maxY, st.y + st.h);
  }
  const pad = 24;
  const vb = { x: minX - pad, y: minY - pad, w: (maxX - minX) + pad * 2, h: (maxY - minY) + pad * 2 };


  const cut = (s, max) => (s.length > max ? s.slice(0, max - 1) + '…' : s);
  const name = (c) => leafName(c.name);
  const esc2 = (s) => esc(String(s));

  let chrome = '';
  for (const pn of stacks) {
    // Keep the tail of the path: "…/grammar/antlr4" distinguishes two sibling
    // parser packages, while the shared "src/sysmlpy" prefix distinguishes nothing.
    const full = pn.dir || '.';
    const segs = full.split('/').filter(Boolean);
    const label = segs.length > 3 ? '…/' + segs.slice(-3).join('/') : full;
    chrome += `<g class="umlpanel">
      <rect x="${pn.x - 14}" y="${pn.y - 8}" width="${W + 28}" height="${pn.h}" rx="5"></rect>
      <text x="${pn.x}" y="${pn.y + 8}">${esc2(label)}${pn.total ? `<tspan class="n" dx="6">${pn.cont ? '·' : ''}${pn.total}</tspan>` : ''}</text>
    </g>`;
  }

  let nodes = '';
  for (const c of d.classes) {
    const p = pos.get(c.id);
    const rows = [];
    const meth = c.methods.slice(0, MAXROW);
    for (const m of meth) rows.push(`<text class="mem" x="10" y="${HEAD + 12 + rows.length * ROW}">${esc2(cut(m.name, 30))}()</text>`);
    for (const a of c.other.slice(0, Math.max(0, MAXROW - meth.length))) {
      rows.push(`<text class="mem attr" x="10" y="${HEAD + 12 + rows.length * ROW}">${esc2(cut(a, 30))}</text>`);
    }
    const more = c.methods.length + c.other.length - meth.length - Math.min(c.other.length, Math.max(0, MAXROW - meth.length));
    if (more > 0) rows.push(`<text class="mem more" x="10" y="${HEAD + 12 + rows.length * ROW}">+${more} more</text>`);
    nodes += `<g class="umlcls" transform="translate(${p.x} ${p.y})" data-id="${c.id}" data-file="${c.file_id}">
      <title>${esc2(c.qualname)} — ${esc2(c.path)}:${c.line}</title>
      <rect class="body" width="${W}" height="${p.h}" rx="3"></rect>
      <text class="name" x="10" y="17">${esc2(cut(name(c), 28))}</text>
      ${rows.length ? `<line class="sep" x1="0" y1="${HEAD}" x2="${W}" y2="${HEAD}"></line>${rows.join('')}` : ''}
    </g>`;
  }
  for (const x of ex) {
    const p = pos.get(x.id);
    nodes += `<g class="umlcls ext" transform="translate(${p.x} ${p.y})" data-id="${esc2(x.id)}">
      <title>${esc2(x.name)} — not in this index</title>
      <rect class="body" width="${W}" height="${p.h}" rx="3"></rect>
      <text class="name" x="10" y="21">${esc2(cut(x.name, 28))}</text>
    </g>`;
  }

  let edges = '';
  for (const e of d.edges) {
    const a = pos.get(e.s), b = pos.get(e.d);
    if (!a || !b) continue;
    // Anchor on facing sides: a horizontal stub between columns reads as one
    // relation, while always exiting right and entering left turns a nearby
    // box into a full-width loop.
    const inh = e.kind === 'inherit';
    let d;
    if (b.x >= a.x + a.w) {
      const ax = a.x + a.w, ay = a.y + a.h / 2, bx = b.x, by = b.y + b.h / 2, mx = (ax + bx) / 2;
      d = `M${ax} ${ay} C${mx} ${ay} ${mx} ${by} ${bx} ${by}`;
    } else if (a.x >= b.x + b.w) {
      const ax = a.x, ay = a.y + a.h / 2, bx = b.x + b.w, by = b.y + b.h / 2, mx = (ax + bx) / 2;
      d = `M${ax} ${ay} C${mx} ${ay} ${mx} ${by} ${bx} ${by}`;
    } else {
      const ax = a.x + a.w / 2, ay = b.y >= a.y ? a.y + a.h : a.y;
      const bx = b.x + b.w / 2, by = b.y >= a.y ? b.y : b.y + b.h, my = (ay + by) / 2;
      d = `M${ax} ${ay} C${ax} ${my} ${bx} ${my} ${bx} ${by}`;
    }
    edges += `<path class="uedge ${inh ? 'inherit' : 'uses'}" data-s="${e.s}" data-d="${e.d}"
      d="${d}"
      marker-end="url(#${inh ? 'uml-tri' : 'uml-arrow'})"><title>${inh ? 'extends' : 'uses'} ×${e.n}</title></path>`;
  }

  box.insertAdjacentHTML('beforeend', `<svg viewBox="0 0 ${vb.w} ${vb.h}" preserveAspectRatio="xMidYMid meet">
    <defs>
      <marker id="uml-tri" viewBox="0 0 12 12" refX="11" refY="6" markerWidth="11" markerHeight="11" orient="auto">
        <path d="M0 1 L11 6 L0 11 z" fill="#0e1013" stroke="#6b7a92" stroke-width="1.2"></path>
      </marker>
      <marker id="uml-arrow" viewBox="0 0 12 12" refX="10" refY="6" markerWidth="9" markerHeight="9" orient="auto">
        <path d="M1 1 L10 6 L1 11" fill="none" stroke="#4d5766" stroke-width="1.2"></path>
      </marker>
    </defs>
    <g class="scene">${chrome}${edges}${nodes}</g>
  </svg>`);

  const svg = $('svg', box), scene = $('.scene', svg);
  const st = { k: 1, x: 0, y: 0 };
  const apply = () => scene.setAttribute('transform', `translate(${st.x} ${st.y}) scale(${st.k})`);
  const fitIt = () => {
    const r = box.getBoundingClientRect();
    if (!r.width || !r.height) return;
    const k0 = Math.min(r.width / vb.w, r.height / vb.h) * 0.96;
    svg.setAttribute('viewBox', `0 0 ${r.width} ${r.height}`);
    st.k = Math.min(1.35, k0);
    st.x = (r.width - vb.w * st.k) / 2 - vb.x * st.k;
    st.y = (r.height - vb.h * st.k) / 2 - vb.y * st.k;
    apply();
  };
  box.__fit = fitIt;
  requestAnimationFrame(fitIt);
  addEventListener('resize', () => {
    const r = box.getBoundingClientRect();
    if (document.body.contains(svg) && r.width) svg.setAttribute('viewBox', `0 0 ${r.width} ${r.height}`);
  }, { passive: true });

  let drag = null;
  const pt = (ev) => { const r = svg.getBoundingClientRect(); return { x: ev.clientX - r.left, y: ev.clientY - r.top }; };
  svg.addEventListener('pointerdown', (ev) => {
    drag = { ...pt(ev), x0: st.x, y0: st.y };
    svg.classList.add('drag');
    svg.setPointerCapture(ev.pointerId);
  });
  svg.addEventListener('pointermove', (ev) => {
    if (!drag) return;
    const p = pt(ev);
    st.x = drag.x0 + (p.x - drag.x); st.y = drag.y0 + (p.y - drag.y);
    apply();
  });
  svg.addEventListener('pointerup', (ev) => {
    if (drag && Math.hypot(pt(ev).x - drag.x, pt(ev).y - drag.y) > 4) svg.dataset.moved = '1';
    drag = null; svg.classList.remove('drag');
  });
  svg.addEventListener('click', (ev) => {
    if (svg.dataset.moved) { svg.dataset.moved = ''; return; }
    const g = ev.target.closest('.umlcls');
    if (g && g.dataset.file) location.hash = `#/file/${g.dataset.file}`;
  });
  svg.addEventListener('wheel', (ev) => {
    ev.preventDefault();
    const p = pt(ev);
    const k2 = Math.min(4, Math.max(0.1, st.k * (ev.deltaY < 0 ? 1.12 : 1 / 1.12)));
    st.x = p.x - (p.x - st.x) * (k2 / st.k);
    st.y = p.y - (p.y - st.y) * (k2 / st.k);
    st.k = k2; apply();
  }, { passive: false });
  $$('.umlcls', svg).forEach((g) => {
    g.addEventListener('mouseenter', () => {
      const id = g.dataset.id;
      $$('.uedge', svg).forEach((e) => e.classList.toggle('hot', e.dataset.s === id || e.dataset.d === id));
    });
    g.addEventListener('mouseleave', () => $$('.uedge.hot', svg).forEach((e) => e.classList.remove('hot')));
  });
  return svg;
}

async function viewMetrics(host) {
  const d = await api('/api/metrics', { top: 25 });
  const d2 = await overview();
  const maxC = Math.max(1, ...d.complex.map((x) => x.complexity));
  const maxN = Math.max(1, ...d.called.map((x) => x.n));
  const resTotal = d.resolution.reduce((a, r) => a + r.n, 0) || 1;
  host.innerHTML = `<div class="wrap wide">
    <p class="eyebrow">Metrics · ranked</p>
    <h1>Where the weight is</h1>
    <div class="rule"></div>
    <div class="mgrid">
      <section>
        <h2>Cyclomatic complexity</h2>
        <table class="grid" style="margin-top:10px">
          <thead><tr><th style="width:52%">function</th><th>score</th><th class="r">loc</th></tr></thead>
          <tbody>${d.complex.map((x) => `<tr>
            <td>${hash(x.id, x.qualname)}<div class="path">${esc(shortPath(x.path, d2.root))}:${x.line}</div></td>
            <td><div class="meter"><i style="width:${(100 * x.complexity / maxC).toFixed(1)}%"></i></div></td>
            <td class="r mono">${x.complexity} <span class="dim">/ ${n(x.loc)}</span></td></tr>`).join('')}</tbody>
        </table>
      </section>
      <section>
        <h2>Call fan-in</h2>
        <table class="grid" style="margin-top:10px">
          <thead><tr><th style="width:52%">symbol</th><th>calls</th><th class="r">callers</th></tr></thead>
          <tbody>${d.called.map((x) => `<tr>
            <td>${hash(x.id, x.qualname)}<div class="path">${esc(shortPath(x.path, d2.root))}:${x.line}</div></td>
            <td><div class="meter"><i style="width:${(100 * x.n / maxN).toFixed(1)}%"></i></div></td>
            <td class="r mono">${n(x.n)} <span class="dim">/ ${n(x.callers)}</span></td></tr>`).join('')}</tbody>
        </table>
      </section>
    </div>

    <div class="rule"></div>
    <div class="mgrid">
      <section>
        <h2>Largest files</h2>
        <table class="grid" style="margin-top:10px">
          <thead><tr><th>file</th><th class="r">loc</th><th class="r">symbols</th><th class="r">refs</th></tr></thead>
          <tbody>${d.biggest.map((f) => `<tr>
            <td><a class="sym" href="#/file/${f.file_id}">${esc(shortPath(f.path, d2.root))}</a></td>
            <td class="r mono">${n(f.loc)}</td><td class="r mono dim">${n(f.entities)}</td>
            <td class="r mono dim">${n(f.refs)}</td></tr>`).join('')}</tbody>
        </table>
      </section>
      <section>
        <h2>Unknown targets</h2>
        <p class="dim" style="font-size:11.5px;margin:6px 0 10px">Names no entity in the index could bind to: library calls, macros, macros-as-functions.</p>
        <table class="grid">
          <thead><tr><th>target</th><th class="r">refs</th><th class="r">files</th></tr></thead>
          <tbody>${d.missing.map((x) => `<tr><td class="mono">${esc(x.target)}</td>
            <td class="r mono">${n(x.n)}</td><td class="r mono dim">${n(x.files)}</td></tr>`).join('')}</tbody>
        </table>
      </section>
    </div>

    <div class="rule"></div>
    <h2>Resolution by language</h2>
    <table class="grid" style="margin-top:12px">
      <thead><tr><th>lang</th><th>references</th><th>resolved</th><th>external</th><th>ambiguous</th><th>unresolved</th></tr></thead>
      <tbody>${d.by_lang.map((l) => {
        const pct = (x) => (100 * (x || 0) / (l.refs || 1)).toFixed(1) + '%';
        return `<tr><td class="mono">${esc(l.lang)}</td><td class="mono dim">${n(l.refs)}</td>
          <td class="mono" style="color:var(--ok)">${pct(l.strong)}</td>
          <td class="mono" style="color:var(--ext)">${pct(l.external)}</td>
          <td class="mono" style="color:var(--amb)">${pct(l.ambiguous)}</td>
          <td class="mono" style="color:var(--bad)">${pct(l.unresolved)}</td></tr>`;
      }).join('')}</tbody>
    </table>
    <p class="dim" style="font-size:11.5px;margin-top:12px">
      ${n(resTotal)} references total. A low “resolved” share is normal for headers-only trees:
      the callee is not in the index, so the name cannot bind.
    </p>
  </div>`;
}

async function viewSearch(host, params) {
  const q = params.get('q') || '';
  const scope = params.get('in') || 'symbols';
  const d = await overview();
  const r = q ? await api('/api/search', { q, limit: 200, in: scope }) : { items: [], total: 0 };
  document.title = (q ? q + ' · ' : '') + 'search · ken';
  const symTab = scope === 'symbols';
  host.innerHTML = `<div class="wrap">
    <p class="eyebrow">Search</p>
    <h1>${q ? esc(q) : 'Everything'}</h1>
    <div class="tabs" style="margin-top:16px">
      <button data-scope="symbols" class="${symTab ? 'on' : ''}">Symbols</button>
      <button data-scope="files" class="${symTab ? '' : 'on'}">Files</button>
    </div>
    <p class="dim mono" style="font-size:11px;margin-bottom:12px">${n(r.total)} ${r.total === 1 ? 'match' : 'matches'}${r.total > r.items.length ? ` · showing first ${r.items.length}` : ''}</p>
    ${symTab ? `<table class="grid">
      <thead><tr><th>symbol</th><th>kind</th><th class="r">line</th></tr></thead>
      <tbody>${r.items.map((x) => `<tr>
        <td>${hash(x.id, x.qualname)}<div class="path">${esc(shortPath(x.path, d.root))}</div></td>
        <td>${kindTag(x.kind)}${x.decl ? ' <span class="badge">decl</span>' : ''}</td>
        <td class="r mono dim">${x.line}</td></tr>`).join('') ||
        '<tr><td colspan="3" class="empty">Nothing matched.</td></tr>'}</tbody></table>`
      : `<table class="grid">
      <thead><tr><th>file</th><th>lang</th><th class="r">symbols</th><th class="r">loc</th></tr></thead>
      <tbody>${r.items.map((x) => `<tr>
        <td><a class="sym" href="#/file/${x.file_id}">${esc(x.path)}</a></td>
        <td>${kindTag(x.lang)}</td><td class="r mono dim">${n(x.entities)}</td>
        <td class="r mono">${n(x.loc)}</td></tr>`).join('') ||
        '<tr><td colspan="4" class="empty">Nothing matched.</td></tr>'}</tbody></table>`}
  </div>`;
  $$('[data-scope]', host).forEach((b) => b.onclick = () => {
    location.hash = `#/search?q=${encodeURIComponent(q)}&in=${b.dataset.scope}`;
  });
}


// ------------------------------------------------------------------ packages
async function viewPackages(host) {
  const d = await api('/api/packages', { limit: 60 });
  document.title = 'packages · ken';
  const shown = d.packages.length;
  host.innerHTML = `<div class="wrap wide">
    <div class="graphhead">
      <div class="crumb">PACKAGE DIAGRAM <a class="dkind" href="#/uml">← classes</a></div>
      <div class="sub">${shown} of ${d.total_packages} packages${d.omitted ? ` · ${d.omitted} left out` : ''}</div>
      <div class="overlay right"><span class="ue-key"><svg width="26" height="10"><line x1="1" y1="5" x2="25" y2="5" class="ue-line"/></svg>«use»</span>
        <span class="ue-key"><svg width="26" height="10"><line x1="1" y1="5" x2="25" y2="5" class="ue-line dash"/></svg>«import»</span></div>
    </div>
    <div class="graphbox seqbox"><svg class="pkgscene"></svg></div>
    <p class="dim" style="font-size:11.5px;margin-top:10px">
      A dependency means a file in the source package names something defined in the target package.
      «import» is a resolved include or import; «use» is any other cross-package reference.
    </p>
  </div>`;
  drawPackages($('.pkgscene', host), d);
}

function sceneSize(svg, W, H) {
  // a four-message trace should not be blown up to fill 800 px; draw it at
  // its own size and only scale down when the scene is genuinely larger
  const box = svg.parentElement.getBoundingClientRect();
  const fit = W > box.width - 2 || H > box.height - 2;
  svg.style.width = fit ? '100%' : Math.round(W) + 'px';
  svg.style.height = fit ? '100%' : Math.round(H) + 'px';
}

function drawPackages(svg, d) {
  const BW = 196, BH = 46, GAPX = 40, GAPY = 34, ASPECT = 1.86;
  const nodes = d.packages;
  const byId = new Map(nodes.map((p) => [p.id, p]));
  const edges = d.edges.filter((e) => byId.has(e.s) && byId.has(e.d));
  // layer by longest dependency path, so a package sits below the things it
  // uses; cycles are broken at their first back edge
  const out = new Map(nodes.map((p) => [p.id, []]));
  const indeg = new Map(nodes.map((p) => [p.id, 0]));
  edges.forEach((e) => { out.get(e.s).push(e.d); indeg.set(e.d, indeg.get(e.d) + 1); });
  const layer = new Map(nodes.map((p) => [p.id, 0]));
  const q = nodes.filter((p) => indeg.get(p.id) === 0).map((p) => p.id);
  const touched = new Set(q);
  while (q.length) {
    const id = q.shift();
    for (const t of out.get(id)) {
      layer.set(t, Math.max(layer.get(t), layer.get(id) + 1));
      indeg.set(t, indeg.get(t) - 1);
      if (indeg.get(t) === 0 && !touched.has(t)) { touched.add(t); q.push(t); }
    }
  }
  const order = nodes.slice().sort((a, b) =>
    (layer.get(a.id) - layer.get(b.id)) || (b.files - a.files) || a.path.localeCompare(b.path));
  // pick the column count whose canvas comes closest to the box's aspect
  let nCols = 1, best = Infinity;
  for (let k = 1; k <= Math.min(8, nodes.length); k++) {
    const rows = Math.ceil(nodes.length / k);
    const w = k * BW + (k - 1) * GAPX, h = rows * BH + (rows - 1) * GAPY;
    const score = Math.abs(Math.log((w / h) / ASPECT));
    if (score < best) { best = score; nCols = k; }
  }
  const pos = new Map();
  order.forEach((p, i) => {
    const r = Math.floor(i / nCols), col = i % nCols;
    const inRow = Math.min(nCols, nodes.length - r * nCols);
    const w = inRow * BW + (inRow - 1) * GAPX;
    pos.set(p.id, { x: -w / 2 + col * (BW + GAPX), y: r * (BH + GAPY) });
  });
  const edgeD = edges.map((e) => {
    const a = pos.get(e.s), b = pos.get(e.d);
    const ac = { x: a.x + BW / 2, y: a.y + BH / 2 }, bc = { x: b.x + BW / 2, y: b.y + BH / 2 };
    let p1, p2, c1, c2;
    const sameRow = Math.abs(a.y - b.y) < 1;
    if (sameRow) {
      const right = bc.x > ac.x;
      p1 = { x: right ? a.x + BW : a.x, y: ac.y };
      p2 = { x: right ? b.x : b.x + BW, y: bc.y };
      const mx = (p1.x + p2.x) / 2;
      c1 = { x: mx, y: p1.y }; c2 = { x: mx, y: p2.y };
    } else {
      const down = bc.y > ac.y;
      p1 = { x: ac.x, y: down ? a.y + BH : a.y };
      p2 = { x: bc.x, y: down ? b.y : b.y + BH };
      const my = (p1.y + p2.y) / 2;
      c1 = { x: p1.x, y: my }; c2 = { x: p2.x, y: my };
    }
    const dash = e.imports > e.uses ? ' dash' : '';
    // a dependency is directed; without a head the layered layout reads as a mesh
    const ang = Math.atan2(p2.y - c2.y, p2.x - c2.x);
    const ux = Math.cos(ang), uy = Math.sin(ang);
    const head = `${p2.x},${p2.y} ${p2.x - ux * 7 + uy * 3.4},${p2.y - uy * 7 - ux * 3.4} ${p2.x - ux * 7 - uy * 3.4},${p2.y - uy * 7 + ux * 3.4}`;
    return `<path class="uedge${dash}" d="M${p1.x} ${p1.y} C${c1.x} ${c1.y} ${c2.x} ${c2.y} ${p2.x} ${p2.y}"/><polygon class="uehead" points="${head}"/>`;
  }).join('');
  const label = (p) => {
    const parts = String(p.path).split('/').filter((s) => s && s !== '.');
    const t = parts.length > 1 ? parts.slice(-2).join('/') : (parts[0] || p.name);
    return t.length > 22 ? '…' + t.slice(-21) : t;
  };
  const boxD = nodes.map((p) => {
    const o = pos.get(p.id);
    return `<g class="umlcls pkg" data-path="${esc(p.path)}" transform="translate(${o.x} ${o.y})">
      <rect class="body" width="${BW}" height="${BH}"/>
      <path class="tab" d="M0 0 v-8 h34 l7 8 z"/>
      <text x="9" y="19">${esc(label(p))}</text>
      <text class="n" x="9" y="35">${p.files} files · ${p.classes} types · ${n(p.loc)} loc</text></g>`;
  }).join('');
  svg.innerHTML = `<g class="scene">${edgeD}${boxD}</g>`;
  const bb = svg.firstChild.getBBox();
  const pad = 24;
  svg.setAttribute('viewBox', `${bb.x - pad} ${bb.y - pad} ${bb.width + 2 * pad} ${bb.height + 2 * pad}`);
  svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
  svg.style.width = '100%';
  svg.style.height = '100%';
  $$('g.pkg', svg).forEach((g) => {
    g.style.cursor = 'pointer';
    g.onclick = () => { location.hash = `#/files?path=${encodeURIComponent(g.dataset.path)}`; };
  });
}

// ------------------------------------------------------------------ sequence
async function viewSequence(host, eid) {
  const d = await api('/api/sequence', { id: eid, limit: 24, depth: 2 });
  if (!d) throw new Error('no such symbol');
  const e = d.entity;
  document.title = `${e.name} · sequence · ken`;
  host.innerHTML = `<div class="wrap wide">
    <div class="graphhead">
      <div class="crumb">SEQUENCE DIAGRAM</div>
      <div class="h1line"><a class="sym" href="#/sym/${e.id}">${esc(e.qualname || e.name)}</a>
        <span class="dim mono" style="font-size:11px">${esc(shortPath(e.path))}:${e.line}</span></div>
      <div class="sub">${d.messages.length} messages · ${d.lifelines.length} lifelines · depth ${d.depth}${d.truncated ? ` · showing ${d.messages.length} of ${n(d.total_calls)} call sites` : ''}</div>
    </div>
    <div class="graphbox seqbox"><svg class="seqscene"></svg></div>
    <p class="dim" style="font-size:11.5px;margin-top:10px">
      Messages follow call-site order, not runtime order: loops and branches make the two differ.
      Calls into the caller's own class are drawn as self-messages. ${n(d.total_calls)} call sites were examined.
    </p>
  </div>`;
  drawSequence($('.seqscene', host), d);
}

function drawSequence(svg, d) {
  const TOP = 54, ROW = 30, HEAD = 40;
  const lives = d.lifelines;
  // spread lifelines across the box when there are only a few of them, but
  // never squeeze below the width at which a message label stops fitting
  const boxW = (svg.parentElement ? svg.parentElement.clientWidth : 720) - 40;
  const COL = lives.length > 1
    ? Math.max(172, Math.min(240, Math.floor((boxW - 60) / (lives.length - 1))))
    : 172;
  const W = Math.max((lives.length - 1) * COL + 120, 360);
  const H = TOP + HEAD + d.messages.length * ROW + 46;
  const cx = (i) => 60 + i * COL;  // first lifeline sits 60 px in, so the left margin is shared
  const parts = [];
  lives.forEach((l, i) => {
    parts.push(`<g class="seqhead" data-id="${l.id}" data-kind="${l.kind}" transform="translate(${cx(i) - 62} ${TOP})">
      <rect width="124" height="${HEAD}" rx="3"/>
      <text x="62" y="17" text-anchor="middle">${esc(l.label.length > 20 ? l.label.slice(0, 19) + '…' : l.label)}</text>
      <text class="n" x="62" y="31" text-anchor="middle">${l.in} in · ${l.out} out</text></g>`);
    parts.push(`<line class="lifeline" x1="${cx(i)}" y1="${TOP + HEAD}" x2="${cx(i)}" y2="${H - 20}"/>`);
  });
  d.messages.forEach((m, i) => {
    const y = TOP + HEAD + 34 + i * ROW;
    const a = m.s, b = m.d;  // s/d arrive as lifeline indices, not entity ids
    const self = m.self || a === b;
    // «create» is the UML stereotype for construction. A construction arrives
    // as kind 'call' on a class lifeline whose label is the class itself, since
    // most languages have no separate instantiation syntax.
    const dl = lives[b] || {};
    const create = dl.kind === 'c' && m.label === dl.label;
    const label = `${m.n}. ${create ? '«create» ' : ''}${m.label}`;
    if (self) {
      const x = cx(a) + 8;
      parts.push(`<path class="msg" d="M${x} ${y} h38 v16 h-38"/>
        <polygon class="msghead" points="${x},${y + 16} ${x + 8},${y + 12} ${x},${y + 8}"/>
        <text class="mlabel" x="${x + 48}" y="${y + 12}">${esc(label)}</text>`);
    } else {
      const x1 = cx(a), x2 = cx(b);
      const dir = x2 > x1 ? 1 : -1;
      parts.push(`<line class="msg" x1="${x1}" y1="${y}" x2="${x2 - dir * 6}" y2="${y}"/>
        <polygon class="msghead" points="${x2},${y} ${x2 - dir * 9},${y - 4} ${x2 - dir * 9},${y + 4}"/>
        <text class="mlabel" x="${(x1 + x2) / 2}" y="${y - 6}" text-anchor="middle">${esc(label)}</text>`);
    }
  });
  // one activation bar per lifeline spanning the rows it takes part in, the
  // way a UML sequence diagram shows a live call, instead of a tick per arrow
  const span = new Map();
  d.messages.forEach((m, i) => {
    const y = TOP + HEAD + 34 + i * ROW;
    for (const who of [m.s, m.d]) {
      const cur = span.get(who);
      span.set(who, cur ? [Math.min(cur[0], y), Math.max(cur[1], y)] : [y, y]);
    }
  });
  for (const [who, [y0, y1]] of span) {
    parts.push(`<rect class="act" x="${cx(who) - 5}" y="${y0 - 8}" width="10" height="${Math.max(y1 - y0 + 16, 16)}" rx="1"/>`);
  }
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
  // sequence diagrams are read line by line, so keep labels at their real
  // size and let the box scroll rather than shrinking 10 px text to 7 px
  svg.style.width = Math.round(W) + 'px';
  svg.style.height = Math.round(H) + 'px';
  svg.innerHTML = `<g class="scene">${parts.join('')}</g>`;
  $$('g.seqhead', svg).forEach((g) => {
    // «external» is a boundary, not an entity: there is nothing to open
    if (g.dataset.kind === 'x') return;
    g.style.cursor = 'pointer';
    g.onclick = () => { location.hash = g.dataset.kind === 'c' ? `#/sym/${g.dataset.id}` : `#/file/${g.dataset.id}`; };
  });
}

// ---------------------------------------------------------------- router
const NAV = { '': 'overview', files: 'files', file: 'files', metrics: 'metrics', uml: 'uml', packages: 'packages', seq: 'uml' };

async function render() {
  const raw = location.hash.replace(/^#/, '') || '/';
  const [pathPart, qs] = raw.split('?');
  const params = new URLSearchParams(qs || '');
  const seg = pathPart.split('/').filter(Boolean);
  const host = $('#view');
  const name = seg[0] || '';
  $$('#topnav a').forEach((a) => a.classList.toggle('on', a.dataset.nav === NAV[name]));
  host.innerHTML = '<div class="wrap"><p class="dim mono" style="font-size:12px">loading…</p></div>';
  try {
    if (!name) await viewOverview(host, params);
    else if (name === 'files') await viewFiles(host, params);
    else if (name === 'file') await viewFile(host, params, seg[1]);
    else if (name === 'sym') await viewSymbol(host, params, Number(seg[1]));
    else if (name === 'graph') {
      await viewGraph(host, params, Number(seg[1]));
    } else if (name === 'uml') await viewUml(host, params);
    else if (name === 'packages') await viewPackages(host);
    else if (name === 'seq') await viewSequence(host, Number(seg[1]));
    else if (name === 'metrics') await viewMetrics(host, params);
    else if (name === 'search') await viewSearch(host, params);
    else host.innerHTML = `<div class="wrap"><div class="err">No route for ${esc(raw)}</div></div>`;
  } catch (err) {
    host.innerHTML = `<div class="wrap"><div class="err">${esc(err.message || err)}</div></div>`;
    console.error(err);
  }
  if (!location.hash.includes('?line=')) window.scrollTo(0, 0);
}

addEventListener('hashchange', render);

// ---------------------------------------------------------------- search box
(function searchBox() {
  const form = $('#searchform'), input = $('#q'), box = $('#results');
  let items = [], sel = -1, timer = 0, token = 0;

  const close = () => { box.hidden = true; box.innerHTML = ''; sel = -1; };
  const paint = () => {
    if (!items.length) { box.innerHTML = '<div class="empty">No symbol matches.</div>'; box.hidden = false; return; }
    box.innerHTML = items.map((x, i) => `<div class="row ${i === sel ? 'sel' : ''}" data-i="${i}">
      <span>${kindTag(x.kind)}</span>
      <span class="q">${esc(x.qualname)}</span>
      <span class="p">${esc(x.path.split('/').slice(-2).join('/'))}:${x.line}</span></div>`).join('');
    box.hidden = false;
  };
  const go = (x) => { close(); input.blur(); location.hash = `#/sym/${x.id}`; };

  input.addEventListener('input', () => {
    const q = input.value.trim();
    clearTimeout(timer);
    if (!q) return close();
    timer = setTimeout(async () => {
      const my = ++token;
      try {
        const r = await api('/api/search', { q, limit: 9 });
        if (my !== token) return;
        items = r.items; sel = 0; paint();
      } catch { close(); }
    }, 110);
  });
  input.addEventListener('keydown', (ev) => {
    if (ev.key === 'ArrowDown' && items.length) { ev.preventDefault(); sel = (sel + 1) % items.length; paint(); }
    else if (ev.key === 'ArrowUp' && items.length) { ev.preventDefault(); sel = (sel - 1 + items.length) % items.length; paint(); }
    else if (ev.key === 'Enter') {
      ev.preventDefault();
      if (sel >= 0 && items[sel]) return go(items[sel]);
      const q = input.value.trim();
      if (q) { close(); input.blur(); location.hash = `#/search?q=${encodeURIComponent(q)}`; }
    } else if (ev.key === 'Escape') { close(); input.blur(); }
  });
  box.addEventListener('mousedown', (ev) => {
    const row = ev.target.closest('.row');
    if (row) { ev.preventDefault(); go(items[Number(row.dataset.i)]); }
  });
  input.addEventListener('blur', () => setTimeout(close, 140));
  form.addEventListener('submit', (ev) => ev.preventDefault());

  addEventListener('keydown', (ev) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || '');
    if ((ev.key === '/' && !typing) || ((ev.metaKey || ev.ctrlKey) && ev.key === 'k')) {
      ev.preventDefault(); input.focus(); input.select();
    }
  });
})();

render();