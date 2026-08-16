#!/usr/bin/env python3
"""Collapse duplicate HN stories.

The same URL gets submitted to HN repeatedly -- measured at ~4% of the corpus.
Visible repetition is the most credibility-damaging bug available on a "best of"
list, so this runs before anything else touches the posts.

Two passes:
  1. canonical URL  -- strips tracking params and trailing slashes, but PRESERVES
     the remaining query (youtube.com/watch?v=... collides thousands of times
     without it).
  2. normalized title within a blog -- catches "Let's Not Encrypt" vs
     "Let's not encrypt" and re-launch variants that live at different URLs.

Keeps the highest-scoring row, sums comments, records repost_count.
"""
import json, re, sys
from collections import defaultdict
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

TRACKING = re.compile(
    r"^(utm_.*|ref|referrer|source|fbclid|gclid|mc_cid|mc_eid|ncid|igshid|"
    r"share|amp|__twitter_impression|s|si)$", re.I
)
YEAR_SUFFIX = re.compile(r"\s*\((?:19|20)\d{2}\)\s*$")
BRACKET_TAG = re.compile(r"\s*\[(pdf|video|audio|paywall|slides|2\d{3})\]\s*", re.I)
NON_ALNUM = re.compile(r"[^a-z0-9]+")


def canonical_url(url):
    if not isinstance(url, str):
        return ""
    try:
        p = urlparse(url)
    except ValueError:
        return url
    host = p.netloc.lower().removeprefix("www.")
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
         if not TRACKING.match(k)]
    path = p.path.rstrip("/") or "/"
    # Hashbang/hash-routed URLs carry their identity in the fragment. Stripping
    # it collapsed 163 unrelated Google Groups threads into one canonical
    # "groups.google.com/forum". Keep routing fragments, drop plain anchors.
    frag = p.fragment if p.fragment[:1] in ("!", "/") else ""
    return urlunparse(("https", host, path, "", urlencode(sorted(q)), frag))


def norm_title(t):
    t = YEAR_SUFFIX.sub("", t)
    t = BRACKET_TAG.sub(" ", t)
    return NON_ALNUM.sub("", t.lower())


def main():
    src, out = sys.argv[1], sys.argv[2]
    rows = []
    with open(src) as f:
        for line in f:
            try:
                s = json.loads(line)
            except json.JSONDecodeError:
                continue
            if s.get("url") and s.get("title"):
                rows.append(s)
    n0 = len(rows)

    # Pass 1: canonical URL
    by_url = defaultdict(list)
    for s in rows:
        by_url[canonical_url(s["url"])].append(s)

    kept = []
    for cu, group in by_url.items():
        group.sort(key=lambda s: -(s["points"] or 0))
        best = dict(group[0])
        best["canonical_url"] = cu
        best["repost_count"] = len(group) - 1
        best["num_comments"] = sum(g.get("num_comments") or 0 for g in group)
        kept.append(best)
    n1 = len(kept)

    # Pass 2: normalized title, scoped within a host so unrelated blogs with
    # generic titles ("Hello world") are never merged.
    by_title = defaultdict(list)
    for s in kept:
        host = urlparse(s["canonical_url"]).netloc
        by_title[(host, norm_title(s["title"]))].append(s)

    final = []
    for _, group in by_title.items():
        group.sort(key=lambda s: -(s["points"] or 0))
        best = dict(group[0])
        best["repost_count"] += len(group) - 1
        best["num_comments"] = sum(g.get("num_comments") or 0 for g in group)
        final.append(best)
    n2 = len(final)

    final.sort(key=lambda s: -(s["points"] or 0))
    with open(out, "w") as f:
        for s in final:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    print(f"input stories        : {n0:,}")
    print(f"after URL dedup      : {n1:,}  (-{n0-n1:,}, {100*(n0-n1)/n0:.2f}%)")
    print(f"after title dedup    : {n2:,}  (-{n1-n2:,}, {100*(n1-n2)/n0:.2f}%)")
    print(f"total removed        : {n0-n2:,}  ({100*(n0-n2)/n0:.2f}%)")


if __name__ == "__main__":
    main()
