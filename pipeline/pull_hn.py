#!/usr/bin/env python3
"""Exhaustively pull HN stories above a points threshold via Algolia.

Algolia caps pagination at ~1000 hits per query, so we walk backwards in time
using created_at_i as a cursor. Resumable: appends to JSONL, tracks seen IDs.
"""
import json, os, sys, time, urllib.parse, urllib.request, urllib.error

THRESHOLD = int(os.environ.get("THRESHOLD", "25"))
OUT = os.environ.get("OUT", "hn_stories.jsonl")
API = "https://hn.algolia.com/api/v1/search_by_date"

seen = set()
if os.path.exists(OUT):
    with open(OUT) as f:
        for line in f:
            try:
                seen.add(json.loads(line)["objectID"])
            except Exception:
                pass
    print(f"resuming with {len(seen)} already-seen stories", flush=True)


def fetch(cursor):
    qs = urllib.parse.urlencode({
        "tags": "story",
        "numericFilters": f"points>={THRESHOLD},created_at_i<={cursor}",
        "hitsPerPage": "1000",
    })
    req = urllib.request.Request(f"{API}?{qs}", headers={"User-Agent": "blogfinder/0.1"})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.load(r)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            wait = 2 ** attempt
            print(f"  retry {attempt+1} after {wait}s ({e})", flush=True)
            time.sleep(wait)
    raise SystemExit("giving up after 6 retries")


# On a cached corpus we only need the new head. Stop once several consecutive
# pages are entirely known rather than walking back to 2006 every run.
STOP_AFTER_SEEN = int(os.environ.get("STOP_AFTER_SEEN", "0"))

cursor = int(time.time())
total_new = 0
rounds = 0
dry_rounds = 0
out = open(OUT, "a")

while True:
    rounds += 1
    data = fetch(cursor)
    hits = data.get("hits", [])
    if not hits:
        print("no more hits; done", flush=True)
        break

    new = 0
    oldest = cursor
    for h in hits:
        ts = h.get("created_at_i")
        if ts is None:
            continue
        oldest = min(oldest, ts)
        oid = h.get("objectID")
        if oid in seen:
            continue
        seen.add(oid)
        new += 1
        out.write(json.dumps({
            "objectID": oid,
            "title": h.get("title"),
            "url": h.get("url"),
            "points": h.get("points"),
            "num_comments": h.get("num_comments"),
            "author": h.get("author"),
            "created_at_i": ts,
        }, ensure_ascii=False) + "\n")
    out.flush()
    total_new += new

    if rounds % 10 == 0 or new == 0:
        yr = time.strftime("%Y-%m", time.gmtime(oldest))
        print(f"round {rounds}: +{new} (total {total_new}) now at {yr}", flush=True)

    if new == 0:
        dry_rounds += 1
        if STOP_AFTER_SEEN and dry_rounds >= STOP_AFTER_SEEN:
            print(f"{dry_rounds} consecutive known pages; stopping early", flush=True)
            break
    else:
        dry_rounds = 0

    # Guard: if a whole page was already seen, step the cursor back manually.
    cursor = oldest - 1 if new == 0 else oldest
    if oldest <= 1160000000:  # ~Oct 2006, before HN existed
        print("reached start of HN; done", flush=True)
        break

out.close()
print(f"DONE: {total_new} new stories, {len(seen)} total", flush=True)
