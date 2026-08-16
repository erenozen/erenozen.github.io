/* Programming Blog Finder -- main thread.
 * Owns UI, facet state and URL sync. All matching happens in search-worker.js.
 */
const $ = (s) => document.querySelector(s);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

const PAGE = 40;
const state = {
  q: "",
  mode: "posts",
  sort: "relevance",
  topics: new Set(),
  kinds: new Set(),
  hideNews: true,
  blog: -1,
  limit: PAGE,
  seq: 0,
  lastSeq: -1,
  meta: null,
  blogs: [],
  ready: false,
};

const worker = new Worker("search-worker.js");
worker.postMessage({ type: "load", base: "data/" });

/* ---------- boot ---------- */

worker.onmessage = (e) => {
  const m = e.data;
  if (m.type === "ready") {
    state.meta = m.meta;
    state.blogs = m.blogs;
    state.ready = true;
    onIndexReady();
  } else if (m.type === "results") {
    if (m.seq < state.lastSeq) return; // a newer query already landed
    state.lastSeq = m.seq;
    render(m);
  }
};

worker.onerror = (err) => {
  $("#status").textContent = "Search failed to start: " + err.message;
};

function onIndexReady() {
  const m = state.meta;
  $("#tagline").innerHTML =
    `<strong>${m.n_posts.toLocaleString()}</strong> posts from ` +
    `<strong>${m.n_blogs.toLocaleString()}</strong> blogs, ranked by how Hacker News ` +
    `actually received them. Newsroom and vendor posts are hidden by default.`;
  $("#stat-stories").textContent = m.n_stories_scanned.toLocaleString();
  $("#stat-built").textContent =
    "index built " + new Date(m.built * 1000).toISOString().slice(0, 10);

  buildChips($("#topics"), m.topics, state.topics, "topic");
  buildChips($("#kinds"), m.kinds, state.kinds, "kind");

  document.querySelectorAll("#suggests button").forEach((b) => {
    b.addEventListener("click", () => {
      state.q = b.dataset.q;
      $("#q").value = state.q;
      state.limit = PAGE;
      run(true);
      $("#q").focus();
    });
  });

  const q = $("#q");
  q.disabled = false;
  readURL();
  q.value = state.q;
  q.focus();
  run();
}

function buildChips(host, items, set, kind) {
  host.textContent = "";
  items.forEach((it, i) => {
    const b = el("button", "chip", it.name);
    b.setAttribute("aria-pressed", "false");
    b.dataset.idx = i;
    b.dataset.kind = kind;
    b.addEventListener("click", () => {
      set.has(i) ? set.delete(i) : set.add(i);
      b.setAttribute("aria-pressed", String(set.has(i)));
      state.limit = PAGE;
      run();
    });
    host.appendChild(b);
  });
}

/* ---------- query ---------- */

function filters() {
  let topicMask = 0;
  for (const i of state.topics) topicMask |= 1 << i;
  let kindMask = 0;
  for (const i of state.kinds) kindMask |= 1 << i;
  return {
    topicMask,
    kindMask,
    blogId: state.blog,
    hideNews: state.hideNews,
    hiddenSourceMask: state.meta ? state.meta.hidden_source_mask : 0,
  };
}

let timer = null;
function run(immediate) {
  clearTimeout(timer);
  const go = () => {
    worker.postMessage({
      type: "query",
      seq: ++state.seq,
      q: state.q.trim(),
      mode: state.mode,
      filters: filters(),
      sort: state.sort,
      limit: state.limit,
    });
    writeURL();
  };
  // Narrowed keystrokes are ~1.5ms, so a short debounce keeps typing smooth
  // without the UI feeling laggy.
  immediate ? go() : (timer = setTimeout(go, 45));
}

/* ---------- render ---------- */

function highlight(text, q) {
  const frag = document.createDocumentFragment();
  const terms = q.trim().toLowerCase().split(/\s+/).filter((t) => t.length > 1);
  if (!terms.length) {
    frag.appendChild(document.createTextNode(text));
    return frag;
  }
  const lower = text.toLowerCase();
  const marks = [];
  for (const t of terms) {
    let from = 0, at;
    while ((at = lower.indexOf(t, from)) !== -1) {
      marks.push([at, at + t.length]);
      from = at + t.length;
    }
  }
  if (!marks.length) {
    frag.appendChild(document.createTextNode(text));
    return frag;
  }
  marks.sort((a, b) => a[0] - b[0]);
  const merged = [marks[0]];
  for (const m of marks.slice(1)) {
    const last = merged[merged.length - 1];
    if (m[0] <= last[1]) last[1] = Math.max(last[1], m[1]);
    else merged.push(m);
  }
  let cur = 0;
  for (const [s, e] of merged) {
    if (s > cur) frag.appendChild(document.createTextNode(text.slice(cur, s)));
    frag.appendChild(el("mark", null, text.slice(s, e)));
    cur = e;
  }
  if (cur < text.length) frag.appendChild(document.createTextNode(text.slice(cur)));
  return frag;
}

function render(m) {
  const list = $("#results");
  list.textContent = "";
  const status = $("#status");
  const anyFilter =
    state.topics.size || state.kinds.size || !state.hideNews ||
    state.q.trim() || state.blog >= 0;
  $("#reset").hidden = !anyFilter;

  if (!m.rows.length) {
    $("#suggests").hidden = !!state.q.trim() || state.blog >= 0;
    renderPin();
    status.textContent = "";
    list.appendChild(noResults());
    $("#more").hidden = true;
    return;
  }

  const ORDER = {
    relevance: state.q.trim() ? "best match" : "top ranked",
    points: "most upvoted",
    date: "newest first",
    oldest: "oldest first",
  };
  status.innerHTML =
    `<span class="hl">${m.total.toLocaleString()}</span> ${state.mode === "blogs" ? "blogs" : "posts"}` +
    ` · ${ORDER[state.sort]} · ${m.ms.toFixed(1)}ms`;

  $("#suggests").hidden = !!state.q.trim() || state.blog >= 0;
  renderPin();

  const frag = document.createDocumentFragment();
  for (const r of m.rows) {
    frag.appendChild(state.mode === "blogs" ? blogRow(r) : postRow(r));
  }
  list.appendChild(frag);
  $("#more").hidden = m.rows.length >= m.total;
  selectRow(-1);
}

function postRow(r) {
  const b = state.blogs[r.b] || { n: "?", h: "#" };
  const li = el("li", "post-li");
  const a = el("a", "row");
  a.href = r.u ? b.h.replace(/\/$/, "") + r.u : b.h;
  a.target = "_blank";
  a.rel = "noopener noreferrer";

  const t = el("div", "r-title");
  t.appendChild(highlight(r.t, state.q));
  a.appendChild(t);

  const meta = el("div", "r-meta");
  const bits = [];
  bits.push(el("span", "r-blog", b.n));

  const pts = el("span", "r-pts", "▲ " + r.p);
  pts.setAttribute("aria-label", `${r.p} points on Hacker News`);
  bits.push(pts);

  const year = new Date(r.d).getUTCFullYear();
  const yr = el("span", "r-year", String(year));
  yr.setAttribute("aria-label", `posted ${year}`);
  bits.push(yr);

  // Only label the kind when a title rule actually fired. Roughly three
  // quarters of posts fall back to their publisher's default, and stamping
  // "Release" on every one of those presents a guess as a fact -- it read as
  // noise on almost every row.
  const kind = state.meta.kinds[r.k];
  if (kind && r.kr) bits.push(el("span", "r-tag", kind.name));
  const src = state.meta.sources[r.s];
  if (src && src.hidden_by_default) bits.push(el("span", "r-tag", src.name));
  if (r.dead) {
    const d = el("span", "r-tag r-dead", "link may be dead");
    d.title = "This URL did not respond when last checked";
    bits.push(d);
  }
  bits.forEach((node, i) => {
    if (i) meta.appendChild(el("span", "dot", " · "));
    meta.appendChild(node);
  });
  a.appendChild(meta);
  li.appendChild(a);

  if (r.dead) {
    const arc = el("a", "hn-link arc-link");
    arc.href = "https://web.archive.org/web/" + a.href;
    arc.target = "_blank";
    arc.rel = "noopener noreferrer";
    arc.textContent = "archived copy";
    li.appendChild(arc);
  }

  if (r.h) {
    const hn = el("a", "hn-link");
    hn.href = "https://news.ycombinator.com/item?id=" + r.h;
    hn.target = "_blank";
    hn.rel = "noopener noreferrer";
    hn.textContent = "HN discussion";
    hn.title = "Read the Hacker News thread";
    li.appendChild(hn);
  }
  return li;
}

function blogRow(r) {
  const li = el("li");
  const a = el("a", "row");
  a.href = r.h;
  a.target = "_blank";
  a.rel = "noopener noreferrer";

  const t = el("div", "r-title");
  t.appendChild(highlight(r.n, state.q));
  a.appendChild(t);

  if (r.o) a.appendChild(el("div", "r-desc", r.o));

  const meta = el("div", "r-meta");
  meta.appendChild(el("span", "r-pts", `${r.c} posts · median ▲${r.m}`));
  const src = state.meta.sources[r.s];
  if (src) meta.appendChild(el("span", "r-tag", src.name));
  for (const ti of state.meta.topics.keys()) {
    if ((r.tm >> ti) & 1) meta.appendChild(el("span", "r-tag", state.meta.topics[ti].name));
  }
  a.appendChild(meta);
  li.appendChild(a);

  const open = el("button", "hn-link blog-open", `${r.c} posts →`);
  open.title = `Show ${r.n}'s posts`;
  open.addEventListener("click", () => pinBlog(r.i));
  li.appendChild(open);
  return li;
}

function pinBlog(idx) {
  state.blog = idx;
  state.mode = "posts";
  state.q = "";
  $("#q").value = "";
  state.limit = PAGE;
  document.querySelectorAll(".mode-switch button").forEach((x) =>
    x.classList.toggle("active", x.dataset.mode === "posts"),
  );
  run(true);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function renderPin() {
  const host = $("#pin");
  host.textContent = "";
  if (state.blog < 0) {
    host.hidden = true;
    return;
  }
  const b = state.blogs[state.blog];
  if (!b) {
    host.hidden = true;
    return;
  }
  host.hidden = false;
  const name = el("a", "pin-name", b.n);
  name.href = b.h;
  name.target = "_blank";
  name.rel = "noopener noreferrer";
  host.appendChild(name);
  if (b.o) host.appendChild(el("span", "pin-desc", b.o));
  const clear = el("button", "pin-clear", "✕ all blogs");
  clear.addEventListener("click", () => {
    state.blog = -1;
    state.limit = PAGE;
    run(true);
  });
  host.appendChild(clear);
}

function noResults() {
  const box = el("div", "empty");
  const q = state.q.trim();
  box.appendChild(el("h3", null, q ? `No matches for “${q}”` : "Nothing matches these filters"));
  const ul = el("ul");
  if (state.topics.size || state.kinds.size)
    ul.appendChild(el("li", null, "Your topic/kind filters may be too narrow — try clearing them."));
  if (state.hideNews)
    ul.appendChild(el("li", null, "Newsrooms and vendor posts are hidden; unticking that widens the corpus a lot."));
  if (q && q.length > 18)
    ul.appendChild(el("li", null, "Long queries match less — try two or three distinctive words."));
  if (q && state.mode === "posts")
    ul.appendChild(el("li", null, "Searching post titles only. Switch to Blogs to find a source by name."));
  if (!ul.childElementCount)
    ul.appendChild(el("li", null, "Try a broader term, or switch between Posts and Blogs."));
  box.appendChild(ul);
  return box;
}

/* ---------- keyboard ---------- */

let sel = -1;
function selectRow(i) {
  const rows = [...document.querySelectorAll("#results li")];
  rows.forEach((r) => r.classList.remove("sel"));
  sel = i;
  if (i >= 0 && rows[i]) {
    rows[i].classList.add("sel");
    rows[i].scrollIntoView({ block: "nearest" });
  }
}

document.addEventListener("keydown", (e) => {
  const rows = document.querySelectorAll("#results li");
  if (e.key === "/" && document.activeElement !== $("#q")) {
    e.preventDefault();
    $("#q").focus();
    $("#q").select();
  } else if (e.key === "Escape") {
    if (state.q) {
      state.q = "";
      $("#q").value = "";
      state.limit = PAGE;
      run(true);
    }
    selectRow(-1);
  } else if (e.key === "ArrowDown") {
    if (!rows.length) return;
    e.preventDefault();
    selectRow(Math.min(sel + 1, rows.length - 1));
  } else if (e.key === "ArrowUp") {
    if (!rows.length) return;
    e.preventDefault();
    if (sel <= 0) {
      selectRow(-1);
      $("#q").focus();
    } else selectRow(sel - 1);
  } else if (e.key === "Enter" && sel >= 0 && rows[sel]) {
    const a = rows[sel].querySelector("a");
    if (a) window.open(a.href, "_blank", "noopener");
  }
});

/* ---------- controls ---------- */

$("#q").addEventListener("input", (e) => {
  state.q = e.target.value;
  state.limit = PAGE;
  run();
});

document.querySelectorAll(".mode-switch button").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelectorAll(".mode-switch button").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    state.mode = b.dataset.mode;
    state.limit = PAGE;
    run(true);
  });
});

document.querySelectorAll(".sort-switch button").forEach((b) => {
  b.addEventListener("click", () => {
    document
      .querySelectorAll(".sort-switch button")
      .forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    state.sort = b.dataset.sort;
    state.limit = PAGE;
    run(true);
  });
});

$("#hide-news").addEventListener("change", (e) => {
  state.hideNews = e.target.checked;
  state.limit = PAGE;
  run(true);
});

$("#more").addEventListener("click", () => {
  state.limit += PAGE * 2;
  run(true);
});

$("#reset").addEventListener("click", () => {
  state.topics.clear();
  state.kinds.clear();
  state.blog = -1;
  state.sort = "relevance";
  document.querySelectorAll(".sort-switch button").forEach((x) =>
    x.classList.toggle("active", x.dataset.sort === "relevance"),
  );
  state.hideNews = true;
  state.q = "";
  $("#q").value = "";
  $("#hide-news").checked = true;
  document.querySelectorAll(".chip").forEach((c) => c.setAttribute("aria-pressed", "false"));
  state.limit = PAGE;
  run(true);
});

/* ---------- URL state ---------- */

function writeURL() {
  const p = new URLSearchParams();
  if (state.q.trim()) p.set("q", state.q.trim());
  if (state.mode !== "posts") p.set("mode", state.mode);
  if (state.sort !== "relevance") p.set("sort", state.sort);
  if (state.topics.size) p.set("t", [...state.topics].join(","));
  if (state.kinds.size) p.set("k", [...state.kinds].join(","));
  if (!state.hideNews) p.set("news", "1");
  if (state.blog >= 0 && state.blogs[state.blog]) p.set("b", state.blogs[state.blog].n);
  const s = p.toString();
  history.replaceState(null, "", s ? "?" + s : location.pathname);
}

function readURL() {
  const p = new URLSearchParams(location.search);
  state.q = p.get("q") || "";
  state.mode = p.get("mode") === "blogs" ? "blogs" : "posts";
  const sortParam = p.get("sort");
  state.sort = ["points", "date", "oldest"].includes(sortParam)
    ? sortParam
    : "relevance";
  document.querySelectorAll(".sort-switch button").forEach((b) => {
    b.classList.toggle("active", b.dataset.sort === state.sort);
  });
  state.hideNews = p.get("news") !== "1";
  $("#hide-news").checked = state.hideNews;
  document.querySelectorAll(".mode-switch button").forEach((b) => {
    b.classList.toggle("active", b.dataset.mode === state.mode);
  });
  const apply = (key, set, host) => {
    (p.get(key) || "")
      .split(",")
      .filter((x) => x !== "")
      .forEach((x) => {
        const i = +x;
        if (!Number.isNaN(i)) {
          set.add(i);
          const c = host.querySelector(`[data-idx="${i}"]`);
          if (c) c.setAttribute("aria-pressed", "true");
        }
      });
  };
  const bkey = p.get("b");
  state.blog = bkey ? state.blogs.findIndex((x) => x.n === bkey) : -1;
  apply("t", state.topics, $("#topics"));
  apply("k", state.kinds, $("#kinds"));
}

/* ---------- theme (mirrors the portfolio's toggle) ---------- */

const ICON = {
  sun: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="5"></circle><line x1="12" y1="1" x2="12" y2="3"></line><line x1="12" y1="21" x2="12" y2="23"></line><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"></line><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"></line><line x1="1" y1="12" x2="3" y2="12"></line><line x1="21" y1="12" x2="23" y2="12"></line><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"></line></svg>`,
  moon: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path></svg>`,
};
const toggle = el("button", "theme-toggle");
toggle.setAttribute("aria-label", "Toggle dark mode");
if (localStorage.getItem("theme") === "dark") {
  document.documentElement.setAttribute("data-theme", "dark");
  toggle.innerHTML = ICON.moon;
} else {
  toggle.innerHTML = ICON.sun;
}
toggle.addEventListener("click", () => {
  const next =
    (document.documentElement.getAttribute("data-theme") || "light") === "light"
      ? "dark"
      : "light";
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem("theme", next);
  toggle.innerHTML = next === "dark" ? ICON.moon : ICON.sun;
});
document.body.appendChild(toggle);
