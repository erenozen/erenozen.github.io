#!/usr/bin/env python3
"""Measure search recall against ground truth the search itself did not produce.

Everything else that tests ranking asks whether the results look plausible.
This asks a question with a checkable answer: for a query, find every title
that literally contains all its terms, run the query through the real worker,
and report what it failed to return.

It found that "writing a compiler" returned 34 rows on a 40-row page while 38
titles matched -- uFuzzy matches terms in order, so it missed "A Compiler
Writing Playground", and at 34 matches it sat one over the threshold that
would have widened the search.

Misses are only a bug when the page was not full. A query with 91 matching
titles and 40 slots is supposed to leave 51 behind.

Exits non-zero if any query returns a short page while matches remain.
"""
import http.server, json, os, re, socketserver, sys, threading
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent / "blogs"
PORT = 8801
QUERIES = ["sqlite internals", "rust async", "kernel scheduler", "dns works",
           "postmortem outage", "writing a compiler", "garbage collection",
           "tcp congestion", "docker networking", "regex engine"]

class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self,*a,**k): super().__init__(*a,directory=str(ROOT),**k)
    def log_message(self,*a): pass

def truth(titles, q):
    terms = [t for t in q.lower().split() if len(t) > 2]
    return {i for i, t in enumerate(titles)
            if all(re.search(r"\b" + re.escape(w), t.lower()) for w in terms)}

def main():
    bad = []
    titles = (ROOT / "data/titles.txt").read_text(encoding="utf-8").split("\n")
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    with sync_playwright() as p:
        exe = os.environ.get("CHROME_PATH") or (
            "/usr/bin/google-chrome" if os.path.exists("/usr/bin/google-chrome") else None)
        b = p.chromium.launch(executable_path=exe, args=["--no-sandbox"])
        pg = b.new_page()
        pg.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
        pg.wait_for_function("() => !document.querySelector('#q').disabled", timeout=90000)
        pg.wait_for_function("() => document.querySelector('#load-note').hidden", timeout=90000)
        print(f"       {'query':22s} {'truth':>6s} {'ret':>5s} {'hit':>5s}  "
              f"worst title not returned")
        for q in QUERIES:
            want = truth(titles, q)
            # news off, so the comparison is against the same corpus the UI shows
            got = pg.evaluate("""async (q) => new Promise(res => {
                const w = new Worker('search-worker.js');
                w.postMessage({type:'load', base:'data/'});
                w.onmessage = (e) => {
                    if (e.data.type === 'ready') {
                        w.onmessage = (e2) => { if (e2.data.type === 'results')
                            res(e2.data.rows.map(r => r.i)); };
                        w.postMessage({type:'query', seq:1, q, mode:'posts', sort:'relevance',
                            limit:40, filters:{topicMask:0,kindMask:0,blogId:-1,hideNews:false,
                            sinceDay:0,sinceYear:0,hideDead:false,hiddenSourceMask:0}});
                    }
                };
            })""", q)
            hit = len(want & set(got))
            missed = sorted(want - set(got))
            worst = titles[missed[0]][:44] if missed else "-"
            short = len(got) < 40 and missed
            if short:
                bad.append(f"{q!r}: {len(got)} rows returned, {len(missed)} "
                           f"matching titles left behind")
            print(("  FAIL " if short else "  ok   ") +
                  f"{q:22s} {len(want):6d} {len(got):5d} {hit:5d}  {worst}")
        b.close()
    httpd.shutdown()
    print()
    if bad:
        for m in bad:
            print("  " + m)
        sys.exit(1)
    print("every query filled its page with the titles that match")


if __name__ == "__main__":
    main()
