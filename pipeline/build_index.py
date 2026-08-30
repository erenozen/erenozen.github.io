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
    hnId       Uint32Array(n)   HN objectID -> news.ycombinator.com/item?id=
"""
import html, json, math, os, re, struct, sys, time
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

# topicMask uses bits 0-11 for the 12 topics; the top bits carry flags.
TOPIC_BITS = 0x0FFF
FLAG_KIND_RULE = 1 << 14   # a title rule actually fired (vs source fallback)
FLAG_FEED = 1 << 13        # came from the blog's own feed, not from HN
FLAG_DEAD = 1 << 15        # URL failed a link check
FEED_CAP = int(os.environ.get("FEED_CAP", "12"))  # most-recent entries per blog

# Forum feeds emit one entry per thread, which drowns real posts in the Newest
# view (spacebattles was posting "Yu-Gi-Oh! GX: World Tour"). Their HN-surfaced
# stories are kept -- those cleared 25 points and are genuinely interesting --
# but the raw firehose is not indexed.
FORUM_HOST = re.compile(
    r"^(forums?|discuss|community|board|talk|answers|support)\.|"
    r"\.(forums?|discourse)\.|phpbb|vbulletin|lists\.", re.I)

# Hostname patterns miss vogons.org (a forum) and fossil-scm.org (a commit log).
# Two content signals catch those generically:
#   - a reply prefix is a forum thread, never an article
#   - twelve entries inside a week is a firehose, not a blog's publishing rate
# vogons.org prefixes its category: "Video • Re: Radeon X700 ...", so the reply
# marker is not anchored at the start. fossil-scm.org tags commits "(tags: trunk)".
REPLY_TITLE = re.compile(
    r"(^|[•·|»–—-]\s*)(re|aw|fwd)\s*[:：]|\(tags?:\s*[\w./-]+\)", re.I)
FIREHOSE_WINDOW_DAYS = 7
HIDDEN_MIN_POINTS = int(os.environ.get("HIDDEN_MIN_POINTS", "150"))


def blog_quality(median, n):
    """Shrunk log-median: a blog with 3 posts at median 400 should not outrank
    one with 200 posts at median 150. k=8 against the corpus prior."""
    k, prior = 8.0, 88.0
    return (n * math.log(max(median, 1)) + k * math.log(prior)) / (n + k)


def main():
    dedup, cand_path, cls_dir, outdir = sys.argv[1:5]
    feeds_path = sys.argv[5] if len(sys.argv) > 5 else None
    links_path = sys.argv[6] if len(sys.argv) > 6 else None

    # Link-check results, if a crawl has been run.
    #
    # Only genuinely-gone responses count as dead. A 403 is nearly always bot
    # blocking, 429 is our own crawler's rate limit, 401 is a paywall and 5xx is
    # transient -- a human following any of those links arrives fine, and
    # flagging them would make the warning noise that readers learn to ignore.
    # Two accepted formats. The raw crawl log is 24 MB of JSONL and stays out of
    # the repo; pipeline/dead_urls.txt is the 1.4 MB distillation of it that CI
    # actually consumes, so a scheduled rebuild keeps warning about rot without
    # re-crawling 154k URLs inside a 90-minute job.
    dead_urls = set()
    if links_path and os.path.exists(links_path):
        GONE = {404, 410, 451}
        checked = 0
        for line in open(links_path):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if not line.startswith("{"):
                dead_urls.add(line)
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            checked += 1
            st = r.get("status", 0)
            if st in GONE or st < 0:
                dead_urls.add(r.get("url"))
        if checked:
            print(f"link check: {checked:,} urls checked, {len(dead_urls):,} unreachable")
        else:
            print(f"link check: {len(dead_urls):,} known-dead urls loaded")
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

        # Treating these sources as "inherently technical" and skipping the
        # is_programming_blog check let forums.spacebattles.com in -- a sci-fi
        # fan forum the classifier labelled project/prog=False at confidence
        # 0.4. Trust the flag: it is right 92-99% of the time for these sources.
        if src in ("engineering", "project", "trade") and c.get("is_programming_blog"):
            keep[key] = c
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
            "l": time.gmtime(st["last_seen"]).tm_year,
            "q": round(blog_quality(st["median_points"], st["n_stories"]), 3),
        })

    # ---- feed URLs ----
    #
    # Carried into blogs.json so the UI can offer a subscribe link and an OPML
    # export. This is a separate pass from feed-post ingestion below, which
    # skips blogs whose entries are unusable -- a blog can have a perfectly good
    # feed to subscribe to while contributing no posts to the index. Forum hosts
    # are excluded: a thread firehose is not something to hand a feed reader.
    if feeds_path and os.path.exists(feeds_path):
        by_key = {}
        for line in open(feeds_path):
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            u = r.get("feed")
            if u and not FORUM_HOST.search((r.get("key") or "").split("/")[0]):
                by_key[r["key"]] = u
        n_feedurl = 0
        for b in blogs_json:
            u = by_key.get(b["n"])
            if u:
                b["f"] = u
                n_feedurl += 1
        print(f"feed URLs attached: {n_feedurl:,} of {len(blogs_json):,} blogs "
              f"({100*n_feedurl/max(len(blogs_json),1):.0f}%)")

    # ---- posts ----
    import sys as _s
    _s.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from aggregate_domains import blog_key

    titles, paths = [], []
    col_blog, col_pts, col_day, col_tm, col_ks, col_score = [], [], [], [], [], []
    col_hn = []
    now = time.time()
    n_scanned = 0

    for line in open(dedup):
        s = json.loads(line)
        n_scanned += 1
        k = blog_key(s["url"])
        if not k or k[0] not in blog_id:
            continue
        key = k[0]

        title = html.unescape(s["title"]).replace("\n", " ").replace("\r", " ").strip()
        if not title:
            continue
        try:
            p = urlparse(s.get("canonical_url") or s["url"])
        except ValueError:
            continue
        path = (p.path or "/") + (("?" + p.query) if p.query else "") + \
               (("#" + p.fragment) if p.fragment else "")
        path = path.replace("\n", "").replace("\r", "").strip()

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
        kind_flag = 0
        for ki, rx in KIND_RULES:
            if rx.search(title):
                kind = ki
                kind_flag = FLAG_KIND_RULE
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
        dead_flag = FLAG_DEAD if (
            dead_urls and
            (cands[key]["home"].rstrip("/") + path) in dead_urls) else 0
        col_tm.append((blog_topic_mask[key] & TOPIC_BITS) | kind_flag | dead_flag)
        col_ks.append((blog_source[key] << 3) | kind)
        try:
            col_hn.append(int(s["objectID"]))
        except (KeyError, TypeError, ValueError):
            col_hn.append(0)

    n_hn = len(titles)
    print(f"HN posts indexed: {n_hn:,} (from {n_scanned:,} deduped stories)")

    # ---- feed-sourced posts ----
    #
    # HN only ever surfaces the posts that went viral; measured, 86% of feed
    # entries are absent from the HN index entirely. These carry no score, so
    # they rank below HN posts by default but are findable by search and make
    # "Newest" reflect what good blogs actually published rather than only what
    # reached the front page.
    last_feed_year = {}
    if feeds_path and os.path.exists(feeds_path):
        from dedupe import canonical_url
        have = set()
        for i in range(n_hn):
            have.add(canonical_url(blogs_json[col_blog[i]]["h"].rstrip("/") + paths[i]))

        # Merge records by blog before capping.
        #
        # A monthly refresh refetches stale blogs, so feeds.jsonl legitimately
        # holds several records per key. Iterating lines directly reset the
        # per-blog cap on each one -- a twice-fetched blog could contribute 24
        # posts under a "cap 12/blog" rule whose entire job is stopping one
        # publisher from owning the view. Entries are unioned so a post that has
        # since scrolled out of the feed window is not lost, and deduped by URL.
        merged = {}
        for line in open(feeds_path):
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = r.get("key")
            if key not in blog_id or not r.get("entries"):
                continue
            cur = merged.get(key)
            if cur is None:
                merged[key] = r
                continue
            if (r.get("fetched_at") or 0) >= (cur.get("fetched_at") or 0):
                newer, older = r, cur
            else:
                newer, older = cur, r
            urls = {e.get("url") for e in newer["entries"]}
            newer["entries"] = newer["entries"] + [
                e for e in older["entries"] if e.get("url") not in urls]
            merged[key] = newer

        n_feed = skipped_date = skipped_forum = firehose = 0
        for r in merged.values():
            key = r["key"]
            if FORUM_HOST.search(key.split("/")[0]):
                skipped_forum += 1
                continue
            ents = []
            for e in r["entries"]:
                ts = e.get("published")
                # No date, or a date outside HN's lifetime, would sort as 2006
                # and poison the Oldest view. 2.5% of entries; drop them.
                if not ts or ts < DAY0 or ts > now + 172800:
                    skipped_date += 1
                    continue
                ents.append((ts, e))
            ents.sort(key=lambda x: -x[0])

            # Cadence guard: if the most recent FEED_CAP entries all landed
            # inside a week, this feed is a commit log, forum or status page
            # rather than a blog. Take a token 2 so the blog still shows some
            # recency without owning the Newest view.
            cap = FEED_CAP
            if len(ents) >= FEED_CAP:
                span_days = (ents[0][0] - ents[FEED_CAP - 1][0]) / 86400.0
                if span_days < FIREHOSE_WINDOW_DAYS:
                    # Skip outright rather than admitting a token 2: a commit
                    # log contributes nothing a reader wants, and these blogs
                    # keep all of their upvote-vetted HN posts regardless.
                    cap = 0
                    firehose += 1

            taken = 0
            for ts, e in ents:
                if taken >= cap:
                    break
                title = html.unescape(e.get("title") or "").replace("\n", " ").strip()
                if not title or REPLY_TITLE.search(title):   # search, not match: the
                    # commit marker "(tags: trunk)" sits at the END of the title
                    continue
                cu = canonical_url(e["url"])
                if not cu or cu in have:
                    continue
                try:
                    pu = urlparse(e["url"])
                except ValueError:
                    continue
                path = (pu.path or "/") + (("?" + pu.query) if pu.query else "")
                path = path.replace("\n", "").replace("\r", "").strip()
                have.add(cu)
                taken += 1
                yr = time.gmtime(ts).tm_year
                if yr > last_feed_year.get(key, 0):
                    last_feed_year[key] = yr

                kind, kind_flag = None, 0
                for ki, rx in KIND_RULES:
                    if rx.search(title):
                        kind, kind_flag = ki, FLAG_KIND_RULE
                        break
                if kind is None:
                    src_slug = keep[key]["source"]
                    kind = FALLBACK_KIND.get(src_slug)
                    if kind is None:
                        kind = KIND_IDX.get(keep[key].get("kind"), KIND_IDX["deep-dive"])

                age_yr = (now - ts) / 31_557_600
                recency = 1.0 / (1.0 + age_yr / 6.0)
                # No HN score exists, so rank on recency alone and cap below the
                # HN band -- an unvetted post must not outrank a 500-point one.
                titles.append(title)
                paths.append(path)
                col_blog.append(blog_id[key])
                col_pts.append(0)
                col_day.append(max(0, min(int((ts - DAY0) / 86400), 65535)))
                dead_flag = FLAG_DEAD if (
                    dead_urls and
                    (cands[key]["home"].rstrip("/") + path) in dead_urls) else 0
                col_tm.append((blog_topic_mask[key] & TOPIC_BITS) | kind_flag
                              | FLAG_FEED | dead_flag)
                col_ks.append((blog_source[key] << 3) | kind)
                col_score.append(max(0, min(120, int(120 * recency))))
                col_hn.append(0)
                n_feed += 1
        print(f"feed posts added : {n_feed:,} (cap {FEED_CAP}/blog, "
              f"{skipped_date:,} skipped for unusable dates, "
              f"{skipped_forum} forum feeds skipped, "
              f"{firehose} firehose feeds throttled)")

    n = len(titles)
    if dead_urls:
        n_dead = sum(1 for t in col_tm if t & FLAG_DEAD)
        print(f"posts flagged dead: {n_dead:,} ({100*n_dead/max(n,1):.1f}%)")
    # A blog's "last seen" year drove the Blogs-mode Since filter, whose whole
    # purpose is separating live blogs from archives -- but it came only from
    # Hacker News. A blog posting weekly that had not been submitted since 2019
    # read as dead. Its own feed is the better evidence of life.
    if last_feed_year:
        revived = 0
        for b in blogs_json:
            y = last_feed_year.get(b["n"])
            if y and y > b["l"]:
                b["l"] = y
                revived += 1
        print(f"last-active year refreshed from feeds: {revived:,} blogs")

    print(f"posts indexed: {n:,}")

    # Order every column by descending baked score.
    #
    # This is what lets the worker stream titles.txt and search the part that
    # has arrived: the first chunk off the wire is the highest-ranked slice of
    # the corpus, not an arbitrary one, so early results are the ones a reader
    # would have seen anyway. Time-to-searchable on a 9 Mbps connection goes
    # from 5.4s to under 2s. Purely a permutation -- every column moves
    # together, and nothing downstream may assume the old order.
    perm = sorted(range(n), key=lambda i: -col_score[i])
    titles = [titles[i] for i in perm]
    paths = [paths[i] for i in perm]
    col_blog = [col_blog[i] for i in perm]
    col_pts = [col_pts[i] for i in perm]
    col_day = [col_day[i] for i in perm]
    col_tm = [col_tm[i] for i in perm]
    col_ks = [col_ks[i] for i in perm]
    col_score = [col_score[i] for i in perm]
    col_hn = [col_hn[i] for i in perm]

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
    # HN item ids live in their own file. They are 0.42MB gzipped -- 35% of
    # posts.bin -- and are used for exactly one thing: building the "HN
    # discussion" href. Nothing ranks, filters or sorts by them, so making the
    # first search wait on them was 35% of the binary payload spent on a link
    # most readers never click. Deferred like paths.txt; until it lands the
    # link is simply absent, which is safe in a way a wrong href would not be.
    with open(os.path.join(outdir, "hn.bin"), "wb") as f:
        f.write(struct.pack(f"<{n}I", *col_hn))
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

    for fn in ("titles.txt", "paths.txt", "posts.bin", "hn.bin", "blogs.json", "meta.json"):
        sz = os.path.getsize(os.path.join(outdir, fn))
        print(f"  {fn:<12} {sz/1e6:7.2f} MB")


if __name__ == "__main__":
    main()
