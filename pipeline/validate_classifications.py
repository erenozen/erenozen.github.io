#!/usr/bin/env python3
"""Verify a classification run covers exactly the blogs it was asked to.

Classification is done by LLM agents, one per evidence batch, each writing its
own file. That is a fan-out with no shared state, so a single agent reading the
wrong evidence file produces output that looks perfectly valid -- correct schema,
plausible labels, right line count -- while silently dropping 200 blogs and
duplicating another batch. This is exactly what happened once, and it was only
caught by hand. Run this after every classification pass.

Usage: validate_classifications.py <expected.jsonl> <classified_dir> [evidence_dir]
"""
import json, os, re, sys
from collections import Counter

SOURCES = {"personal", "engineering", "trade", "project", "newsroom", "vendor",
           "institution"}
KINDS = {"deep-dive", "opinion", "announcement", "incident"}
TOPICS = {"systems", "languages", "web", "data-infra", "ai", "security",
          "hardware", "graphics-games", "practice", "science", "policy", "society"}

failures = []


def check(ok, msg):
    print(("  ok   " if ok else "  FAIL ") + msg)
    if not ok:
        failures.append(msg)


def main():
    expected_path, cls_dir = sys.argv[1], sys.argv[2]
    ev_dir = sys.argv[3] if len(sys.argv) > 3 else None

    expected = [json.loads(l)["key"] for l in open(expected_path) if l.strip()]
    exp = set(expected)

    rows, per_file, bad_json = [], {}, 0
    for fn in sorted(os.listdir(cls_dir)):
        if not fn.endswith(".jsonl"):
            continue
        keys = []
        for line in open(os.path.join(cls_dir, fn)):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                bad_json += 1
                continue
            rows.append((fn, r))
            keys.append(r.get("key"))
        per_file[fn] = keys

    got = [k for _, r in rows for k in [r.get("key")]]
    seen = set(got)
    print(f"{len(rows)} labels across {len(per_file)} files "
          f"for {len(expected)} expected blogs")

    check(bad_json == 0, f"every line parses ({bad_json} malformed)")
    dupes = [k for k, c in Counter(got).items() if c > 1]
    check(not dupes, f"no duplicate keys ({len(dupes)} repeated, e.g. {dupes[:3]})")
    extra = seen - exp
    check(not extra, f"no keys outside the input set ({len(extra)}, e.g. {list(extra)[:3]})")
    missing = exp - seen
    check(not missing, f"every expected blog classified ({len(missing)} missing)")

    # Pinpoint which batch failed, since a wrong-file read shows up as one file
    # whose keys belong to a different batch.
    if missing and ev_dir and os.path.isdir(ev_dir):
        blame = Counter()
        order = sorted(expected)
        # Infer the batch size from the largest file, NOT from how many files
        # exist: a missing or deleted batch shifts that divisor and mislabels
        # every later file as mismatched.
        size = max((len(v) for v in per_file.values()), default=200) or 200
        pos = {k: i for i, k in enumerate(order)}
        for k in missing:
            if k in pos:
                blame[pos[k] // size] += 1
        print(f"     missing keys cluster in batch(es): {dict(blame.most_common(3))}")
        for fn, keys in per_file.items():
            m = re.search(r"(\d+)", fn)
            if not m:
                continue
            want = set(order[int(m.group(1)) * size:(int(m.group(1)) + 1) * size])
            if want and len(want & set(keys)) < len(keys) * 0.5:
                print(f"     {fn} does not match its expected batch -- likely read "
                      f"the wrong evidence file")

    enum_bad = [r.get("key") for _, r in rows
                if r.get("source") not in SOURCES or r.get("kind") not in KINDS]
    check(not enum_bad, f"source/kind within enums ({len(enum_bad)} bad)")
    topic_bad = [r.get("key") for _, r in rows
                 if any(t.get("slug") not in TOPICS for t in r.get("topics") or [])
                 or not r.get("topics")]
    check(not topic_bad, f"topic slugs valid and non-empty ({len(topic_bad)} bad)")

    conf = [r.get("confidence", 0) for _, r in rows]
    low = sum(1 for c in conf if c < 0.55)
    print(f"     confidence below 0.55: {low} ({100*low/max(len(conf),1):.0f}%)")

    print()
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED")
        sys.exit(1)
    print("classification run is complete and consistent")


if __name__ == "__main__":
    main()
