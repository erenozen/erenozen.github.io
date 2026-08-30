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
    new, n = re.subn(pattern, repl, text, count=1)
    if n != 1:
        sys.exit(f"sync_counts: pattern for {label} did not match -- "
                 "the wording changed; update this script")
    return new


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

    print(f"counts synced: {approx} posts, {blogs:,} blogs")


if __name__ == "__main__":
    main()
