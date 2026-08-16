#!/usr/bin/env python3
"""Assemble per-blog evidence bundles and write them out as LLM batch files.

Evidence beyond raw HN titles matters because a large share of candidates have
very few stories. Where a feed was fetched we fold in entry titles and summary
text; everywhere we add URL path slugs and two cheap title-shape rates that are
independent of HN score (so they add signal rather than re-encoding the median).
"""
import json, math, os, re, sys, time
from collections import defaultdict
from urllib.parse import urlparse

# "How X works", "Building a Y from scratch" -- the shape of craft writing.
CRAFT = re.compile(
    r"how .{2,30} works|under the hood|internals|deep.dive|anatomy of|"
    r"writing (a|your own)|building (a|my own)|from scratch|in \d+ lines|"
    r"reverse.engineer|i built|i made|lessons|why i|implementing", re.I)
# Dated, event-shaped headlines -- the shape of a newsroom.
EVENT = re.compile(
    r"\b(announces?|launches?|acquires?|raises?|files?|sues?|says?|reports?|"
    r"confirms?|unveils?|shuts? down|lays? off|hits?|warns?|accuses?)\b|"
    r"\b(ceo|q[1-4]|billion|million|lawsuit|investors?|funding|ipo)\b", re.I)


def slugs(url, limit=6):
    try:
        parts = [p for p in urlparse(url).path.split("/") if p][:limit]
    except ValueError:
        return ""
    return "/".join(p[:40] for p in parts)


def main():
    cand_path, feeds_path, outdir = sys.argv[1], sys.argv[2], sys.argv[3]
    min_n = int(sys.argv[4]) if len(sys.argv) > 4 else 5
    per_batch = int(sys.argv[5]) if len(sys.argv) > 5 else 150

    cands = [json.loads(l) for l in open(cand_path)]
    cands = [c for c in cands if c["n_stories"] >= min_n]
    cands.sort(key=lambda c: c["key"])

    feeds = {}
    if os.path.exists(feeds_path):
        for l in open(feeds_path):
            r = json.loads(l)
            if r.get("entries"):
                feeds[r["key"]] = r

    os.makedirs(outdir, exist_ok=True)
    batches = [cands[i:i + per_batch] for i in range(0, len(cands), per_batch)]

    for bi, batch in enumerate(batches):
        lines = []
        for i, c in enumerate(batch):
            titles = c["sample_titles"]
            craft = sum(bool(CRAFT.search(t)) for t in titles) / max(len(titles), 1)
            event = sum(bool(EVENT.search(t)) for t in titles) / max(len(titles), 1)
            months = max(
                (c["last_seen"] - c["first_seen"]) / 2_629_800.0, 1.0)
            cadence = c["n_stories"] / months
            yr = lambda t: time.strftime("%Y", time.gmtime(t))

            L = [f"### {i+1}. {c['key']}",
                 f"home={c['home']} stories={c['n_stories']} median_pts={c['median_points']} "
                 f"max_pts={c['max_points']} per_month={cadence:.2f} "
                 f"active={yr(c['first_seen'])}-{yr(c['last_seen'])} "
                 f"craft_rate={craft:.2f} event_rate={event:.2f}"]
            L.append("top_hn_titles: " + " | ".join(t[:95] for t in titles[:8]))
            sl = [slugs(u) for u in c["sample_urls"][:4]]
            sl = [s for s in sl if s]
            if sl:
                L.append("url_slugs: " + " | ".join(sl))
            f = feeds.get(c["key"])
            if f:
                if f.get("feed_title"):
                    L.append(f"feed_title: {f['feed_title'][:90]}")
                ents = f["entries"][:6]
                for e in ents[:4]:
                    s = (e.get("summary") or "")[:130]
                    L.append(f"  feed: {e['title'][:85]}" + (f" -- {s}" if s else ""))
            lines.append("\n".join(L))

        path = os.path.join(outdir, f"ev_{bi:03d}.txt")
        with open(path, "w") as fh:
            fh.write("\n\n".join(lines))

    n_feed = sum(1 for c in cands if c["key"] in feeds)
    print(f"blogs (n>={min_n}): {len(cands)}  with feed evidence: {n_feed}")
    print(f"batches: {len(batches)} x {per_batch} -> {outdir}")
    sz = os.path.getsize(os.path.join(outdir, "ev_000.txt"))
    print(f"batch 0: {sz} chars ~= {sz//4} tokens")


if __name__ == "__main__":
    main()
