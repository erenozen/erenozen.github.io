#!/usr/bin/env python3
"""Build the static search index consumed by blogs/search-worker.js.

Layout is columnar because the alternative -- 150k JSON objects each repeating
the keys "title"/"url"/"points" -- spends most of its bytes on punctuation and
forces a multi-hundred-ms JSON.parse before the first keystroke can be served.
Typed arrays are parse-free: the worker just points views at the ArrayBuffer.

posts.bin (little-endian, n = meta.n_posts), in this exact order:
    blogId     Uint32Array(n)
    points     Uint16Array(n)
    day        Uint16Array(n)   days since 2006-01-01
    topicMask  Uint16Array(n)   bit i set => topic i
    kindSource Uint8Array(n)    kind | source << 3
    score      Uint8Array(n)    baked rank, 0-255
"""
import json, math, os, re, struct, sys, time
from collections import defaultdict
from urllib.parse import urlparse

TOPICS = [
    ("systems", "Systems"), ("languages", "Languages"), ("web", "Web & Frontend"),
    ("data-infra", "Data & Infra"), ("ai", "AI & ML"), ("security", "Security"),
    ("hardware", "Hardware"), ("graphics-games", "Graphics & Games"),
    ("practice", "Practice"), ("science", "Science"), ("policy", "Policy"),
    ("society", "Society"),
]
SOURCES = [
    ("personal", "Personal", False), ("engineering", "Engineering", False),
    ("trade", "Trade press", False), ("project", "Project", False),
    ("newsroom", "Newsroom", True), ("vendor", "Vendor", True),
    ("institution", "Institution", True),
]
KINDS = [
    ("deep-dive", "How it works"), ("opinion", "Argument"),
    ("announcement", "Release"), ("incident", "War story"),
]
TOPIC_IDX = {s: i for i, (s, _) in enumerate(TOPICS)}
SOURCE_IDX = {s: i for i, (s, _, _) in enumerate(SOURCES)}
KIND_IDX = {s: i for i, (s, _) in enumerate(KINDS)}
HIDDEN_MASK = sum(1 << i for i, (_, _, h) in enumerate(SOURCES) if h)

# Post-level kind rules, first match wins (spec Stage 3).
KIND_RULES = [
    (KIND_IDX["incident"], re.compile(
        r"post-?mortem|\boutage\b|\bincident\b|breach\b|CVE-\d|\bRCE\b|0-?day|"
        r"backdoor|supply.chain|root cause|what went wrong|hacked|data leak", re.I)),
    (KIND_IDX["announcement"], re.compile(
        r"^\S+ v?\d+\.\d+|\brelease[ds]?\b|announcing|introducing|now (available|open.source)"
        r"|\bis out\b|\bGA\b|acquires|shuts down|has died|launches", re.I)),
    (KIND_IDX["deep-dive"], re.compile(
        r"how .{2,30} works|under the hood|internals\b|deep dive|anatomy of|"
        r"writing (a|your own)|building (a|my own)|from scratch|in \d+ lines|"
        r"reverse.engineering|demystif|implementing", re.I)),
    (KIND_IDX["opinion"], re.compile(
        r"^(Why|Stop|Don't|Should|I |We |You )|considered harmful|"
        r"is (dead|broken|a mistake|underrated)|lessons (from|learned)|"
        r"I was wrong|\?$", re.I)),
]
FALLBACK_KIND = {
    "newsroom": KIND_IDX["announcement"], "trade": KIND_IDX["announcement"],
    "institution": KIND_IDX["announcement"], "vendor": KIND_IDX["announcement"],
    "project": KIND_IDX["announcement"], "engineering": KIND_IDX["deep-dive"],
}
DAY0 = 1136073600  # 2006-01-01 UTC
HIDDEN_MIN_POINTS = int(os.environ.get("HIDDEN_MIN_POINTS", "150"))


def blog_quality(median, n):
    """Shrunk log-median: a blog with 3 posts at median 400 should not outrank
    one with 200 posts at median 150. k=8 against the corpus prior."""
    k, prior = 8.0, 88.0
    return (n * math.log(max(median, 1)) + k * math.log(prior)) / (n + k)


def main():
    dedup, cand_path, cls_dir, outdir = sys.argv[1:5]
    os.makedirs(outdir, exist_ok=True)

    cands = {json.loads(l)["key"]: json.loads(l) for l in open(cand_path)}

    cls = {}
    bad = 0
    for fn in sorted(os.listdir(cls_dir)):
        if not fn.endswith(".jsonl"):
            continue
        for line in open(os.path.join(cls_dir, fn)):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                if r.get("key") and r.get("source") in SOURCE_IDX:
                    cls[r["key"]] = r
                else:
                    bad += 1
            except json.JSONDecodeError:
                bad += 1
    print(f"classifications loaded: {len(cls)} (malformed skipped: {bad})")

    # Hand corrections, applied before any inclusion decision.
    ov_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "overrides.json")
    ov = json.load(open(ov_path)) if os.path.exists(ov_path) else {}
    ov_source, ov_deny = ov.get("source", {}), set(ov.get("deny", []))
    for key, src in ov_source.items():
        if key in cls and src in SOURCE_IDX:
            cls[key]["source"] = src

    # Inclusion.
    #
    # The classifier's is_programming_blog flag behaves as "tech-adjacent": it
    # fired on 91.8% of personal blogs, including startup/productivity/essay
    # sites with no software topic at all. Requiring an actual software topic
    # is the correction -- a blog about attention management is not a
    # programming blog however tech-adjacent its author.
    SOFTWARE = {"systems", "languages", "web", "data-infra", "ai", "security",
                "hardware", "graphics-games"}
    keep, hidden_keys = {}, set()
    for key, c in cls.items():
        if key not in cands or key in ov_deny:
            continue
        src = c["source"]
        topics = {t.get("slug") for t in c.get("topics", [])}
        technical = bool(topics & SOFTWARE)

        if src in ("engineering", "project", "trade"):
            keep[key] = c                      # inherently technical publishers
        elif src in ("personal", "vendor") and c.get("is_programming_blog") and technical:
            keep[key] = c
        elif src == "newsroom" and cands[key]["n_stories"] >= 20:
            keep[key] = c                      # kept for the toggle, hidden by default
            hidden_keys.add(key)
        else:
            continue
        if src in ("newsroom", "vendor", "institution"):
            hidden_keys.add(key)

    order = sorted(
        keep, key=lambda k: -blog_quality(cands[k]["median_points"], cands[k]["n_stories"])
    )
    blog_id = {k: i for i, k in enumerate(order)}
    print(f"blogs included: {len(order)}")

    blogs_json = []
    blog_topic_mask, blog_source = {}, {}
    for k in order:
        c, st = keep[k], cands[k]
        tm = 0
        for t in c.get("topics", []):
            if t.get("slug") in TOPIC_IDX and t.get("weight", 0) >= 0.25:
                tm |= 1 << TOPIC_IDX[t["slug"]]
        if not tm and c.get("topics"):
            first = c["topics"][0].get("slug")
            if first in TOPIC_IDX:
                tm = 1 << TOPIC_IDX[first]
        blog_topic_mask[k] = tm
        blog_source[k] = SOURCE_IDX[c["source"]]
        blogs_json.append({
            "n": k, "h": st["home"], "s": SOURCE_IDX[c["source"]], "tm": tm,
            "o": (c.get("one_line") or "")[:110],
            "c": st["n_stories"], "m": st["median_points"],
            "q": round(blog_quality(st["median_points"], st["n_stories"]), 3),
        })

    # ---- posts ----
    import sys as _s
    _s.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from aggregate_domains import blog_key

    titles, paths = [], []
    col_blog, col_pts, col_day, col_tm, col_ks, col_score = [], [], [], [], [], []
    now = time.time()
    n_scanned = 0

    for line in open(dedup):
        s = json.loads(line)
        n_scanned += 1
        k = blog_key(s["url"])
        if not k or k[0] not in blog_id:
            continue
        key = k[0]

        title = s["title"].replace("\n", " ").replace("\r", " ").strip()
        if not title:
            continue
        try:
            p = urlparse(s.get("canonical_url") or s["url"])
        except ValueError:
            continue
        path = (p.path or "/") + (("?" + p.query) if p.query else "") + \
               (("#" + p.fragment) if p.fragment else "")
        path = path.replace("\n", "").replace("\r", "")

        pts = min(s.get("points") or 0, 65535)

        # Sources hidden by default are the bulk of the corpus (newsrooms alone
        # were 59% of posts) but are invisible until the toggle is flipped.
        # Carrying every routine wire story costs megabytes to serve something
        # nobody sees; keeping only the genuinely notable ones makes the toggle
        # reveal the best of the news rather than all of it.
        if key in hidden_keys and pts < HIDDEN_MIN_POINTS:
            continue

        day = max(0, min(int((s["created_at_i"] - DAY0) / 86400), 65535))

        kind = None
        for ki, rx in KIND_RULES:
            if rx.search(title):
                kind = ki
                break
        if kind is None:
            src_slug = keep[key]["source"]
            kind = FALLBACK_KIND.get(src_slug)
            if kind is None:  # personal -> the blog's own dominant mode
                kind = KIND_IDX.get(keep[key].get("kind"), KIND_IDX["deep-dive"])

        age_yr = (now - s["created_at_i"]) / 31_557_600
        base = math.log10(max(pts, 1)) / math.log10(3000)
        recency = 1.0 / (1.0 + age_yr / 6.0)
        col_score.append(max(0, min(255, int(255 * (0.78 * base + 0.22 * recency)))))

        titles.append(title)
        paths.append(path)
        col_blog.append(blog_id[key])
        col_pts.append(pts)
        col_day.append(day)
        col_tm.append(blog_topic_mask[key])
        col_ks.append((blog_source[key] << 3) | kind)

    n = len(titles)
    print(f"posts indexed: {n:,} (from {n_scanned:,} deduped stories)")

    with open(os.path.join(outdir, "titles.txt"), "w") as f:
        f.write("\n".join(titles))
    with open(os.path.join(outdir, "paths.txt"), "w") as f:
        f.write("\n".join(paths))
    with open(os.path.join(outdir, "posts.bin"), "wb") as f:
        f.write(struct.pack(f"<{n}I", *col_blog))
        f.write(struct.pack(f"<{n}H", *col_pts))
        f.write(struct.pack(f"<{n}H", *col_day))
        f.write(struct.pack(f"<{n}H", *col_tm))
        f.write(struct.pack(f"<{n}B", *col_ks))
        f.write(struct.pack(f"<{n}B", *col_score))
    with open(os.path.join(outdir, "blogs.json"), "w") as f:
        json.dump(blogs_json, f, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(outdir, "meta.json"), "w") as f:
        json.dump({
            "built": int(now),
            "n_posts": n,
            "n_blogs": len(order),
            "n_stories_scanned": n_scanned,
            "hidden_source_mask": HIDDEN_MASK,
            "topics": [{"slug": s, "name": nm} for s, nm in TOPICS],
            "sources": [{"slug": s, "name": nm, "hidden_by_default": h}
                        for s, nm, h in SOURCES],
            "kinds": [{"slug": s, "name": nm} for s, nm in KINDS],
        }, f, separators=(",", ":"))

    for fn in ("titles.txt", "paths.txt", "posts.bin", "blogs.json", "meta.json"):
        sz = os.path.getsize(os.path.join(outdir, fn))
        print(f"  {fn:<12} {sz/1e6:7.2f} MB")


if __name__ == "__main__":
    main()
