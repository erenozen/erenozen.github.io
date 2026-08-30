#!/usr/bin/env python3
"""Rewrite the hand-written post/blog counts from the built index.

Three places quote the size of the corpus in prose: the finder's meta
description, the portfolio's call-to-action, and the finder's own footer (which
already reads them from meta.json at runtime, so it is not touched here). The
first two were static, and both had drifted -- the portfolio advertised "111k
posts from 5,979 blogs" of a corpus that had grown to 165k and 10,535.

Every substitution asserts it matched. A count-sync that silently does nothing
is worse than no count-sync, because it looks like the numbers are maintained.
"""
import json, re, sys


def sub_once(text, pattern, repl, label):
    # Exactly one match, not "at least one". re.subn(count=1) reports success
    # after replacing the first of several, which is how a page ends up with a
    # fresh number in one place and a stale one three lines below -- the
    # og:description sits right under the meta description and reads almost
    # identically.
    hits = re.findall(pattern, text)
    if len(hits) != 1:
        sys.exit(f"sync_counts: pattern for {label} matched {len(hits)} times, "
                 "expected exactly 1 -- the wording changed; update this script")
    return re.sub(pattern, repl, text, count=1)


def main():
    meta = json.load(open("blogs/data/meta.json"))
    posts, blogs = meta["n_posts"], meta["n_blogs"]
    approx = f"{round(posts / 1000):,}k"

    p = "blogs/index.html"
    s = open(p, encoding="utf-8").read()
    s = sub_once(s, r"Search [\d.,]+k? posts from [\d.,]+ programming blogs",
                 f"Search {approx} posts from {blogs:,} programming blogs",
                 "finder meta description")
    open(p, "w", encoding="utf-8").write(s)

    p = "index.html"
    s = open(p, encoding="utf-8").read()
    s = sub_once(s, r"[\d.,]+k? posts from [\d.,]+ programming blogs",
                 f"{approx} posts from {blogs:,} programming blogs",
                 "portfolio call-to-action")
    open(p, "w", encoding="utf-8").write(s)

    # og.png is a rendered image, so it cannot be regenerated in CI without a
    # browser. It quotes round-down floors instead of live numbers for exactly
    # that reason; this asserts the floors are still true rather than letting
    # the card quietly start understating -- or worse, overstating -- the corpus.
    FLOOR_POSTS, FLOOR_BLOGS = 160_000, 10_000
    if posts < FLOOR_POSTS or blogs < FLOOR_BLOGS:
        sys.exit(f"sync_counts: og.png claims {FLOOR_POSTS:,}+ posts and "
                 f"{FLOOR_BLOGS:,}+ blogs, but the index has {posts:,} and "
                 f"{blogs:,}. Re-render the card.")

    print(f"counts synced: {approx} posts, {blogs:,} blogs")


if __name__ == "__main__":
    main()
