#!/usr/bin/env python3
"""Split candidate blogs into compact batches for LLM classification.

Each batch is a plain-text file: one numbered line per blog carrying just enough
signal to judge it -- identity, how often HN liked it, the median score (the best
single discriminator between a news site and a blog), and its top titles.
"""
import json, os, sys

src, outdir = sys.argv[1], sys.argv[2]
per_batch = int(sys.argv[3]) if len(sys.argv) > 3 else 150

os.makedirs(outdir, exist_ok=True)
rows = [json.loads(l) for l in open(src)]

# Stable order so re-runs produce identical batches (cache-friendly, resumable).
rows.sort(key=lambda r: r["key"])

batches = [rows[i:i + per_batch] for i in range(0, len(rows), per_batch)]
for i, batch in enumerate(batches):
    path = os.path.join(outdir, f"batch_{i:03d}.txt")
    with open(path, "w") as f:
        for j, r in enumerate(batch):
            titles = " :: ".join(t[:110] for t in r["sample_titles"][:4])
            f.write(
                f"{j+1}. key={r['key']} | home={r['home']} | "
                f"hn_stories={r['n_stories']} median_pts={r['median_points']} "
                f"max_pts={r['max_points']}\n   top_titles: {titles}\n"
            )

manifest = os.path.join(outdir, "manifest.json")
with open(manifest, "w") as f:
    json.dump({"n_blogs": len(rows), "n_batches": len(batches),
               "per_batch": per_batch}, f, indent=2)

print(f"{len(rows)} blogs -> {len(batches)} batches of {per_batch} in {outdir}")
