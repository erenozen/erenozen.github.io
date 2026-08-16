#!/usr/bin/env python3
"""Pick which blogs to fetch feeds for.

Only blogs that survived classification: crawling all 18k candidates would mean
tens of thousands of requests to sites that were never going into the index.
"""
import json, os, sys

cand_path, cls_dir, out = sys.argv[1], sys.argv[2], sys.argv[3]

keep = set()
for fn in sorted(os.listdir(cls_dir)):
    if not fn.endswith(".jsonl"):
        continue
    for line in open(os.path.join(cls_dir, fn)):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("is_programming_blog") or r.get("source") in ("newsroom", "trade"):
            keep.add(r.get("key"))

n = 0
with open(out, "w") as f:
    for line in open(cand_path):
        c = json.loads(line)
        if c["key"] in keep:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
            n += 1
print(f"{n} classified blogs selected for feed fetch (of {len(keep)} kept labels)")
