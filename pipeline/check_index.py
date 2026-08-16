#!/usr/bin/env python3
"""Fail loudly if the built index is internally inconsistent.

This runs in CI before the index is committed. Every check here corresponds to a
way the index can be silently wrong -- silently, because the UI would still load
and search would still return rows; they would just be the wrong rows, or point
at the wrong URLs. A column that is off by one is invisible until a user clicks a
result and lands on an unrelated post.
"""
import json, os, struct, sys

# Recall canaries. The platform-hosted entries are deliberate: substack and
# wordpress blogs were once dropped entirely (a substack URL is
# foo.substack.com/p/slug, and "p" was treated as a generic route segment) or
# fragmented into date archives. Both failures were silent -- the index built
# fine, it was just missing Terence Tao and Michal Zalewski.
FAMOUS = [
    "danluu.com", "jvns.ca", "simonwillison.net", "rachelbythebay.com",
    "lwn.net", "blog.cloudflare.com", "antirez.com", "nullprogram.com",
    "randomascii.wordpress.com", "lcamtuf.substack.com",
    "terrytao.wordpress.com", "fgiesen.wordpress.com",
]

def main():
    d = sys.argv[1]
    fail = []
    def check(ok, msg):
        print(("  ok   " if ok else "  FAIL ") + msg)
        if not ok:
            fail.append(msg)

    meta = json.load(open(os.path.join(d, "meta.json")))
    blogs = json.load(open(os.path.join(d, "blogs.json")))
    n, nb = meta["n_posts"], meta["n_blogs"]
    print(f"index: {n:,} posts / {nb:,} blogs")

    check(n > 10_000, f"post count plausible ({n:,} > 10,000)")
    check(nb > 500, f"blog count plausible ({nb:,} > 500)")
    check(len(blogs) == nb, f"blogs.json rows == meta.n_blogs ({len(blogs)} vs {nb})")

    with open(os.path.join(d, "titles.txt"), encoding="utf-8") as f:
        titles = f.read().split("\n")
    with open(os.path.join(d, "paths.txt"), encoding="utf-8") as f:
        paths = f.read().split("\n")
    check(len(titles) == n, f"titles.txt lines == n_posts ({len(titles)} vs {n})")
    check(len(paths) == n, f"paths.txt lines == n_posts ({len(paths)} vs {n})")
    check(all(t.strip() for t in titles), "no empty titles")

    size = os.path.getsize(os.path.join(d, "posts.bin"))
    check(size == n * 16, f"posts.bin is n*16 bytes ({size} vs {n*16})")

    with open(os.path.join(d, "posts.bin"), "rb") as f:
        buf = f.read()
    blog_ids = struct.unpack_from(f"<{n}I", buf, 0)
    pts = struct.unpack_from(f"<{n}H", buf, n * 4)
    tm = struct.unpack_from(f"<{n}H", buf, n * 8)
    ks = struct.unpack_from(f"<{n}B", buf, n * 10)
    hn = struct.unpack_from(f"<{n}I", buf, n * 12)

    check(max(blog_ids) < nb, f"all blogId in range (max {max(blog_ids)} < {nb})")
    # Feed-sourced posts carry no HN score, so the bar applies to HN posts only.
    hn_pts = [p for p, t in zip(pts, tm) if not (t & (1 << 13))]
    check(min(hn_pts) >= 25,
          f"all HN posts clear the 25-point bar (min {min(hn_pts)})")
    n_feed = sum(1 for t in tm if t & (1 << 13))
    check(n_feed > 0, f"feed posts present ({n_feed:,})")
    check(all(p == 0 for p, t in zip(pts, tm) if t & (1 << 13)),
          "feed posts carry no fabricated HN score")
    check(max((t & 0x0FFF).bit_length() for t in tm) <= 12, "topic mask within 12 topics")
    n_rule = sum(1 for t in tm if t & (1 << 14))
    check(0.10 < n_rule / n < 0.60,
          f"kind rules fire on a sane share ({n_rule/n:.1%} rule-derived)")
    check(max(k >> 3 for k in ks) < len(meta["sources"]), "source index in range")
    check(max(k & 7 for k in ks) < len(meta["kinds"]), "kind index in range")
    check(sum(1 for t in tm if (t & 0x0FFF) == 0) < n * 0.05,
          f"under 5% of posts have no topic ({sum(1 for t in tm if (t&0x0FFF)==0)/n:.1%})")

    missing_hn = sum(1 for h, t in zip(hn, tm) if h == 0 and not (t & (1 << 13)))
    check(missing_hn < n * 0.01, f"HN ids present on HN posts ({missing_hn} missing)")

    names = {b["n"] for b in blogs}
    missing = [f for f in FAMOUS if f not in names]
    check(not missing, f"famous blogs present (missing: {missing or 'none'})")

    # The newsroom toggle is the product's main promise; if it hides nothing or
    # everything, something upstream broke.
    hidden = meta["hidden_source_mask"]
    nh = sum(1 for k in ks if (hidden >> (k >> 3)) & 1)
    check(0.10 < nh / n < 0.85, f"hidden-source share sane ({nh/n:.1%} of posts)")

    print()
    if fail:
        print(f"{len(fail)} CHECK(S) FAILED")
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
