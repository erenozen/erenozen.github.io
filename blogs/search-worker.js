/* Search worker.
 *
 * Holds the whole index off the main thread so a cold scan can never drop a
 * frame. Two things here are load-bearing and were verified by benchmark
 * against the real corpus:
 *
 *   1. Narrowing MUST go through uf.filter(hay, needle, prevIdxs). Passing a
 *      prefiltered set to uf.search() does NOT re-test it -- it returns the set
 *      verbatim and then throws inside info(). Measured: cold filter on 400k
 *      titles is ~19ms, narrowing the same query is ~1.5ms.
 *   2. uFuzzy filters a real JS array of strings, not a joined blob, so we hold
 *      the split array and pay the split (~25ms) once at load.
 */
importScripts("vendor/uFuzzy.iife.min.js");

const uf = new uFuzzy({ intraMode: 1, intraIns: 1, interIns: Infinity });

let titles = [];      // string[] -- the haystack
let paths = [];       // string[] -- url path per post, parallel to titles
let blogId, points, day, topicMask, kindSource, score, hnId; // typed arrays
let blogs = [];       // per-blog metadata
let ready = false;

// Narrowing state: the last query and its PRE-FACET match set. Facets never
// affect the fuzzy set, so a prefix extension can always narrow from here.
let lastQuery = "";
let lastIdxs = null;
let termHits = null;   // set when the OR fallback fired: idx -> terms matched

const DAY0 = Date.UTC(2006, 0, 1) / 86400000;

async function load(base) {
  const [metaRes, blogsRes] = await Promise.all([
    fetch(base + "meta.json"),
    fetch(base + "blogs.json"),
  ]);
  const meta = await metaRes.json();
  blogs = await blogsRes.json();

  // Paths load with the rest rather than deferred: search going live before
  // links resolve would render rows nobody can click.
  const [titlesRes, binRes, pathsRes] = await Promise.all([
    fetch(base + "titles.txt"),
    fetch(base + "posts.bin"),
    fetch(base + "paths.txt"),
  ]);
  const text = await titlesRes.text();
  const buf = await binRes.arrayBuffer();
  paths = (await pathsRes.text()).split("\n");

  titles = text.split("\n");
  const n = meta.n_posts;
  let o = 0;
  blogId = new Uint32Array(buf, o, n); o += n * 4;
  points = new Uint16Array(buf, o, n); o += n * 2;
  day = new Uint16Array(buf, o, n); o += n * 2;
  topicMask = new Uint16Array(buf, o, n); o += n * 2;
  kindSource = new Uint8Array(buf, o, n); o += n;
  score = new Uint8Array(buf, o, n); o += n;
  hnId = new Uint32Array(buf, o, n);

  ready = true;
  postMessage({ type: "ready", meta, nTitles: titles.length, blogs });
}

/* Top-N by key without sorting the whole pool.
 *
 * Browse mode ("newest", no query) has a pool of every post in the corpus.
 * pool.sort(cmp).slice(0, limit) on 111k rows costs tens of ms for 40 visible
 * results. This keeps a sorted buffer of `limit` items and binary-inserts only
 * the rare candidate that beats the running threshold, so the common case is
 * one comparison per row. */
function topN(pool, key, limit) {
  if (pool.length <= limit) return pool.slice().sort((a, b) => key(b) - key(a));
  const best = [];
  let thresh = -Infinity;
  for (let k = 0; k < pool.length; k++) {
    const i = pool[k];
    const v = key(i);
    if (best.length === limit && v <= thresh) continue;
    let lo = 0, hi = best.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (key(best[mid]) >= v) lo = mid + 1;
      else hi = mid;
    }
    best.splice(lo, 0, i);
    if (best.length > limit) best.pop();
    if (best.length === limit) thresh = key(best[limit - 1]);
  }
  return best;
}

function passes(i, f) {
  if (f.blogId >= 0 && blogId[i] !== f.blogId) return false;
  if (f.topicMask && !(topicMask[i] & 0x0fff & f.topicMask)) return false;
  const ks = kindSource[i];
  if (f.blogId < 0 && f.hideNews && (f.hiddenSourceMask >> (ks >> 3)) & 1) return false;
  if (f.kindMask && !((f.kindMask >> (ks & 7)) & 1)) return false;
  return true;
}

function emit(ordered, total) {
  const rows = ordered.map((i) => ({
    i,
    t: titles[i],
    b: blogId[i],
    p: points[i],
    u: paths[i] || "",
    d: (day[i] + DAY0) * 86400000,
    k: kindSource[i] & 7,
    s: kindSource[i] >> 3,
    h: hnId[i],
    kr: (topicMask[i] >> 14) & 1,   // kind came from a title rule, not a fallback
    dead: (topicMask[i] >> 15) & 1,
  }));
  return { rows, total };
}

function searchPosts(q, f, limit, sortMode) {
  let idxs;
  if (!q) {
    idxs = null; // browse mode -- rank everything by baked score
  } else {
    const extend = lastQuery && q.startsWith(lastQuery) && lastIdxs;
    idxs = uf.filter(titles, q, extend ? lastIdxs : undefined);

    // uFuzzy requires EVERY term to be present, so "sqlite internals" matched
    // exactly one title even though hundreds are relevant. When the strict pass
    // is that thin, union the per-term matches instead and rank by how many
    // terms each title hit -- strict results still sort first because they hit
    // all of them.
    const terms = q.split(/\s+/).filter((t) => t.length > 2);
    if (terms.length > 1 && (!idxs || idxs.length < 25)) {
      const hits = new Map();
      for (const i of idxs || []) hits.set(i, terms.length + 1);
      for (const t of terms) {
        const ti = uf.filter(titles, t);
        if (!ti) continue;
        for (const i of ti) hits.set(i, (hits.get(i) || 0) + 1);
      }
      // With 3+ terms, a single-term match is mostly noise ("how"/"works"
      // match thousands of titles alone), so require at least two. With 2
      // terms, requiring two would just be the strict pass we are relaxing.
      const minHits = terms.length >= 3 ? 2 : 1;
      idxs = [...hits.keys()]
        .filter((i) => hits.get(i) >= minHits)
        .sort((a, b) => (hits.get(b) - hits.get(a)) || (a - b));
      termHits = hits;
      lastQuery = "";       // this set is not a strict prefix match; do not narrow from it
      lastIdxs = null;
    } else {
      termHits = null;
      lastQuery = q;
      lastIdxs = idxs;
    }
    if (!idxs || !idxs.length) return { rows: [], total: 0 };
  }

  // Facet pass. In browse mode this walks the corpus once; with a query it only
  // walks the match set, which is why filters stay cheap as you type.
  const pool = [];
  if (idxs) {
    for (let k = 0; k < idxs.length; k++) {
      const i = idxs[k];
      if (passes(i, f)) pool.push(i);
    }
  } else {
    for (let i = 0; i < titles.length; i++) {
      if (passes(i, f)) pool.push(i);
    }
  }
  const total = pool.length;
  if (!total) return { rows: [], total: 0 };

  // Explicit sorts bypass relevance ranking entirely: if you asked for "most
  // upvoted", match quality must not reorder the answer. uFuzzy already decided
  // membership; sort only decides order.
  if (sortMode === "points") {
    return emit(topN(pool, (i) => points[i], limit), total);
  }
  if (sortMode === "date") {
    return emit(topN(pool, (i) => day[i], limit), total);
  }
  if (sortMode === "oldest") {
    return emit(topN(pool, (i) => -day[i], limit), total);
  }

  let ordered;
  if (!q) {
    ordered = topN(pool, (i) => score[i], limit);
  } else {
    // info()/sort() cost scales with the match set, and a one-letter query
    // matches most of the corpus. Bound it: keep the highest-scoring INFO_CAP
    // matches, then rank those by match quality. We only ever render ~50 rows,
    // so the headroom is generous.
    const INFO_CAP = 3000;
    let cand = pool;
    if (cand.length > INFO_CAP) {
      cand = pool.slice().sort((a, b) => score[b] - score[a]).slice(0, INFO_CAP);
    }
    cand.sort((a, b) => a - b); // info() expects ascending haystack indices

    // uFuzzy's own ordering is match quality. Rather than replicate its
    // comparator, bucket by quality tier and rank by baked post score inside a
    // tier: best matches first, and popular posts first among equally good
    // matches. An exact-ish match can never be buried by a merely popular one.
    if (termHits) {
      // Fallback path: rank by terms matched, then by baked score. info()/sort()
      // would re-impose the all-terms ordering we just relaxed.
      ordered = cand
        .sort((a, b) => (termHits.get(b) - termHits.get(a)) || (score[b] - score[a]))
        .slice(0, limit);
      return emit(ordered, total);
    }
    const info = uf.info(cand, titles, q);
    const order = uf.sort(info, titles, q);
    const rank = new Map();
    for (let r = 0; r < order.length; r++) rank.set(cand[info.idx[order[r]]], r);
    const tier = (i) => {
      const r = rank.get(i);
      return r === undefined ? 3 : r < 200 ? 0 : r < 1000 ? 1 : 2;
    };
    ordered = cand
      .sort((a, b) => {
        const ta = tier(a), tb = tier(b);
        return ta !== tb ? ta - tb : score[b] - score[a];
      })
      .slice(0, limit);
  }

  return emit(ordered, total);
}

function searchBlogs(q, f, limit, sortMode) {
  let pool = [];
  for (let i = 0; i < blogs.length; i++) {
    const b = blogs[i];
    if (f.topicMask && !(b.tm & f.topicMask)) continue;
    if (f.hideNews && (f.hiddenSourceMask >> b.s) & 1) continue;
    pool.push(i);
  }
  if (q) {
    const hay = pool.map((i) => blogs[i].n + " " + blogs[i].o);
    const idxs = uf.filter(hay, q);
    if (!idxs || !idxs.length) return { rows: [], total: 0 };
    const info = uf.info(idxs, hay, q);
    const order = uf.sort(info, hay, q);
    pool = order.map((r) => pool[info.idx[r]]);
  }
  // For blogs, "upvotes" means the blog's median HN score and "newest" means
  // most recently seen on HN -- the nearest honest equivalents.
  if (sortMode === "points") pool = topN(pool, (i) => blogs[i].m, limit);
  else if (sortMode === "date") pool = topN(pool, (i) => blogs[i].l, limit);
  else if (sortMode === "oldest") pool = topN(pool, (i) => -blogs[i].l, limit);
  else if (!q) pool = topN(pool, (i) => blogs[i].q, limit);
  return {
    rows: pool.slice(0, limit).map((i) => ({ i, ...blogs[i] })),
    total: pool.length,
  };
}

onmessage = (e) => {
  const m = e.data;
  if (m.type === "load") return void load(m.base);
  if (!ready) return;
  if (m.type === "query") {
    const t0 = performance.now();
    const r =
      m.mode === "blogs"
        ? searchBlogs(m.q, m.filters, m.limit, m.sort)
        : searchPosts(m.q, m.filters, m.limit, m.sort);
    postMessage({
      type: "results",
      seq: m.seq,
      mode: m.mode,
      ms: performance.now() - t0,
      ...r,
    });
  }
};
