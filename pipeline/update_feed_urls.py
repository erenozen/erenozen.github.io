#!/usr/bin/env python3
"""Merge this run's feed-discovery results into the committed seed.

Without this the seed is a one-off snapshot: every blog discovered after it was
written gets its feed re-derived from scratch, every month, at 14 timing-out
requests each. With it, each run teaches the next one.

Merge, never replace. A scheduled run works from a time-boxed, possibly partial
fetch, so rewriting the file from that alone would silently drop every blog the
run did not reach -- turning a cache miss into permanent data loss.
"""
import json, os, sys

HEADER = """# Feed discovery results, seeded into pipeline/fetch_feeds.py.
#
# key<TAB>feed URL, or key<TAB>- when discovery found nothing.
#
# The negatives are the point. A blog whose HTML advertises a feed is resolved
# in one request; a blog with no feed costs 14 requests that all time out, and
# there are thousands of them. Measured: 400 blogs WITH feeds take 4.4 min at 32
# workers, while 200 WITHOUT feeds had not finished after 10. Re-deriving that
# answer every month is what made a full pass take four hours.
#
# Maintained by pipeline/update_feed_urls.py. Regenerate from scratch with a
# full manual pass (SKIP_NEGATIVE=0) to pick up blogs that have added a feed.
"""


def main():
    seed_path, feeds_path = sys.argv[1], sys.argv[2]

    known = {}
    if os.path.exists(seed_path):
        for line in open(seed_path, encoding="utf-8"):
            line = line.rstrip("\n")
            if not line or line.startswith("#") or "\t" not in line:
                continue
            k, u = line.split("\t", 1)
            known[k] = u
    before = len(known)

    added = flipped = 0
    if os.path.exists(feeds_path):
        for line in open(feeds_path, encoding="utf-8"):
            try:
                r = json.loads(line)
            except Exception:
                continue
            k = r.get("key")
            if not k:
                continue
            if r.get("feed") and r.get("entries"):
                if known.get(k) != r["feed"]:
                    flipped += k in known and known[k] != r["feed"]
                    added += k not in known
                    known[k] = r["feed"]
            elif r.get("error") == "no-feed" and known.get(k, "-") == "-":
                # Only record a negative when we have no positive on file: a
                # blog that was briefly unreachable must not lose a good URL.
                added += k not in known
                known[k] = "-"

    with open(seed_path, "w", encoding="utf-8") as f:
        f.write(HEADER)
        for k in sorted(known):
            f.write(f"{k}\t{known[k]}\n")

    pos = sum(1 for v in known.values() if v != "-")
    print(f"feed seed: {before:,} -> {len(known):,} entries "
          f"({pos:,} positive, {len(known) - pos:,} negative; "
          f"{added:,} new, {flipped:,} updated)")


if __name__ == "__main__":
    main()
