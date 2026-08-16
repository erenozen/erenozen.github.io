#!/usr/bin/env python3
"""Discover and fetch RSS/Atom feeds for a list of blogs.

Why this exists alongside the HN data: HN only ever surfaces the posts that
happened to go viral. A blog's best writing frequently never hits the front
page. Feeds give us the blog's own view of what it published.

Caveat baked into expectations: most feeds are truncated to the latest 10-50
entries, so this yields freshness and depth-of-recent, not full archives.
"""
import json, re, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import feedparser
import requests

UA = "Mozilla/5.0 (compatible; blogfinder/0.1; +https://erenozen.dev)"
TIMEOUT = 15
CANDIDATE_PATHS = [
    "/feed", "/feed/", "/rss", "/rss/", "/feed.xml", "/rss.xml", "/atom.xml",
    "/index.xml", "/feeds/all.atom.xml", "/blog/feed", "/blog/rss",
    "/blog/index.xml", "/posts/index.xml", "/feed/atom",
]
LINK_RE = re.compile(
    r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*>', re.I
)
HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)

session = requests.Session()
session.headers.update({"User-Agent": UA})


def _get(url, **kw):
    return session.get(url, timeout=TIMEOUT, allow_redirects=True, **kw)


def discover_feed(home):
    """Return a feed URL for a blog home page, or None."""
    host = urlparse(home).netloc.lower()

    # Platform shortcuts -- cheaper and more reliable than sniffing.
    if host.endswith(".substack.com"):
        return urljoin(home, "/feed")
    if host == "medium.com" or host.endswith(".medium.com"):
        p = urlparse(home).path.strip("/")
        return f"https://medium.com/feed/{p}" if p else None
    if host == "dev.to":
        p = urlparse(home).path.strip("/")
        return f"https://dev.to/feed/{p}" if p else None

    # 1) Ask the homepage what its feed is.
    try:
        r = _get(home)
        if r.ok and r.text:
            for tag in LINK_RE.findall(r.text[:200_000]):
                m = HREF_RE.search(tag)
                if m:
                    return urljoin(r.url, m.group(1))
    except requests.RequestException:
        pass

    # 2) Fall back to conventional locations.
    for path in CANDIDATE_PATHS:
        try:
            r = _get(urljoin(home, path))
            ctype = r.headers.get("content-type", "").lower()
            if r.ok and ("xml" in ctype or r.text.lstrip()[:200].startswith("<?xml")):
                return r.url
        except requests.RequestException:
            continue
    return None


def parse_feed(feed_url):
    """Return (entries, feed_title). Entries are dicts."""
    try:
        r = _get(feed_url)
        if not r.ok:
            return [], None
        d = feedparser.parse(r.content)
    except (requests.RequestException, Exception):
        return [], None

    out = []
    for e in d.entries[:200]:
        link = e.get("link")
        title = (e.get("title") or "").strip()
        if not link or not title:
            continue
        ts = None
        for key in ("published_parsed", "updated_parsed"):
            if e.get(key):
                try:
                    ts = int(time.mktime(e[key]))
                except (TypeError, ValueError, OverflowError):
                    pass
                break
        summary = re.sub(r"<[^>]+>", " ", e.get("summary", "") or "")
        summary = re.sub(r"\s+", " ", summary).strip()[:300]
        out.append({"title": title, "url": link, "published": ts, "summary": summary})
    return out, (d.feed.get("title") if d.get("feed") else None)


def handle(blog):
    home = blog["home"]
    try:
        feed_url = discover_feed(home)
        if not feed_url:
            return {**blog, "feed": None, "entries": [], "error": "no-feed"}
        entries, ftitle = parse_feed(feed_url)
        return {**blog, "feed": feed_url, "feed_title": ftitle,
                "entries": entries, "error": None if entries else "empty"}
    except Exception as e:  # never let one blog kill the crawl
        return {**blog, "feed": None, "entries": [], "error": f"{type(e).__name__}"}


def main():
    src, out_path = sys.argv[1], sys.argv[2]
    workers = int(sys.argv[3]) if len(sys.argv) > 3 else 24

    blogs = [json.loads(l) for l in open(src)]
    print(f"fetching feeds for {len(blogs)} blogs with {workers} workers", flush=True)

    done = ok = 0
    with open(out_path, "w") as out, ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(handle, b): b for b in blogs}
        for fut in as_completed(futs):
            r = fut.result()
            done += 1
            if r["entries"]:
                ok += 1
            out.write(json.dumps(r, ensure_ascii=False) + "\n")
            if done % 100 == 0:
                out.flush()
                print(f"  {done}/{len(blogs)} done, {ok} with entries", flush=True)

    print(f"DONE: {ok}/{len(blogs)} blogs yielded feed entries")


if __name__ == "__main__":
    main()
