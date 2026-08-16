# Blog finder pipeline

Builds the static search index served at `/blogs/`. Everything is derived from
two sources: Hacker News (via the Algolia API) and the blogs' own RSS/Atom feeds.

## Why it is shaped this way

HN ranks **posts**, but we want **blogs**, so stage 2 groups stories by publisher
and ranks by *number of distinct stories that cleared the bar* — never by total
points, which one viral post would dominate.

The head of that ranking is almost entirely news sites (`arstechnica`, `nytimes`,
`theguardian`), while the blogs people actually want sit in the tail. No scalar
separates them: `netflixtechblog.com` is corporate *and* excellent, `lwn.net` is
structurally a newsroom *and* essential. So `source` is a discrete label assigned
by an LLM, and the UI hides `newsroom`/`vendor`/`institution` behind one toggle.

## Stages

| # | script | in → out |
|---|---|---|
| 1 | `pull_hn.py` | Algolia → `hn_stories.jsonl` (~507k stories ≥25 pts, 2006–now) |
| 2 | `dedupe.py` | → `hn_dedup.jsonl` (collapses ~4.5% duplicate submissions) |
| 3 | `aggregate_domains.py` | → `candidates.jsonl` (~18k candidate blogs, ≥3 stories) |
| 4 | `fetch_feeds.py` | → `feeds.jsonl` (~65% feed discovery rate) |
| 5 | `build_evidence.py` | → `evidence/ev_NNN.txt` LLM batch files |
| 6 | *(out of band)* | LLM classification → `classified/cls_NNN.jsonl` |
| 7 | `build_index.py` | → `blogs/data/*` |
| 8 | `check_index.py` | fails CI if the index is inconsistent |

`select_for_feeds.py` limits stage 4 to blogs that survived classification, so we
never crawl the ~12k candidates that were never going to be indexed.

## Classification is deliberately out of band

Stage 6 needs an LLM and is **not** run by CI. Its output is committed to
`classified/` and treated as an input. A newly discovered blog therefore stays out
of the index until the next classification pass — the correct failure mode, since
an unclassified blog has no `source`, and `source` decides what the newsroom
toggle hides.

To re-run it: `build_evidence.py` writes the batches, an agent labels each one to
`classified/cls_NNN.jsonl`, then `build_index.py` picks them up.

## Overrides

`overrides.json` is applied last and unconditionally. It corrects `source` where
the classifier was inconsistent between sibling domains (`sciencedaily.com` vs
`phys.org`; `tomshardware.com` vs `anandtech.com`) and denies hosting/paste
domains that are not blogs at all.

## Local run

```bash
python -m venv .venv && .venv/bin/pip install -r pipeline/requirements.txt
OUT=work/hn.jsonl .venv/bin/python pipeline/pull_hn.py       # ~15 min, ~510 requests
.venv/bin/python pipeline/dedupe.py work/hn.jsonl work/dedup.jsonl
.venv/bin/python pipeline/aggregate_domains.py work/hn.jsonl work/cand.jsonl 3
.venv/bin/python pipeline/build_index.py work/dedup.jsonl work/cand.jsonl pipeline/classified blogs/data
.venv/bin/python pipeline/check_index.py blogs/data
```
