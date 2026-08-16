#!/usr/bin/env python3
"""Drive the blog finder in a real browser and assert it actually works.

Everything before this was static reasoning about code that had never been
rendered. This serves blogs/ over HTTP (the worker needs a real origin -- it
cannot load from file://) and exercises the paths a visitor takes.
"""
import http.server, json, socketserver, sys, threading, time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path("/home/eren/erenozen.github.io/blogs")
PORT = 8731

failures = []


def check(ok, label, detail=""):
    print(("  ok   " if ok else "  FAIL ") + label + (f"  [{detail}]" if detail else ""))
    if not ok:
        failures.append(f"{label} {detail}")


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def log_message(self, *a):
        pass


def serve():
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main():
    httpd = serve()
    base = f"http://127.0.0.1:{PORT}/"
    console, page_errors, failed_reqs = [], [], []

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path="/usr/bin/google-chrome",
                                    args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("console", lambda m: console.append((m.type, m.text)))
        page.on("pageerror", lambda e: page_errors.append(str(e)))
        page.on("requestfailed", lambda r: failed_reqs.append(r.url))

        t0 = time.time()
        page.goto(base, wait_until="load")
        print(f"\n== load ==  DOM ready in {time.time()-t0:.2f}s")

        # The input stays disabled until the worker signals ready.
        page.wait_for_function("() => !document.querySelector('#q').disabled",
                               timeout=60000)
        ready = time.time() - t0
        print(f"search ready in {ready:.2f}s")
        check(ready < 30, "index becomes searchable", f"{ready:.1f}s")

        check(not page_errors, "no uncaught page errors", "; ".join(page_errors[:2]))
        errs = [t for t in console if t[0] == "error"]
        check(not errs, "no console errors", "; ".join(t[1][:90] for t in errs[:2]))
        check(not failed_reqs, "no failed requests", "; ".join(failed_reqs[:2]))

        # --- default view ---
        page.wait_for_selector("#results li", timeout=20000)
        n = page.locator("#results li").count()
        check(n > 0, "default view renders results", f"{n} rows")
        print("  status:", page.locator("#status").inner_text())

        # --- typing ---
        page.fill("#q", "sqlite internals")
        page.wait_for_timeout(700)
        n = page.locator("#results li").count()
        first = page.locator("#results li").first.inner_text().replace("\n", " | ")
        check(n > 0, "query returns results", f"{n} rows")
        print("  top:", first[:104])

        # the multi-term fallback must be doing work here
        total = page.locator("#status").inner_text()
        check("1 posts" not in total, "multi-term fallback widens recall", total)

        # --- href correctness: the row must point at the real article ---
        href = page.locator("#results li a.row").first.get_attribute("href")
        check(bool(href) and href.startswith("http") and "undefined" not in href,
              "result links to a real URL", str(href)[:80])

        # --- HN discussion link ---
        hn = page.locator("#results li a.hn-link").first
        hn_href = hn.get_attribute("href") if hn.count() else None
        check(bool(hn_href) and "news.ycombinator.com/item?id=" in hn_href
              and not hn_href.endswith("id=0"), "HN discussion link is valid",
              str(hn_href)[:70])

        # --- nested anchors would be invalid HTML and break clicks ---
        nested = page.evaluate("() => document.querySelectorAll('a a').length")
        check(nested == 0, "no nested anchors", f"{nested} found")

        # --- sorting ---
        page.fill("#q", "")
        page.click('.sort-switch button[data-sort="points"]')
        page.wait_for_timeout(600)
        pts = page.evaluate("""() => [...document.querySelectorAll('#results .r-pts')]
            .map(e => parseInt(e.textContent.replace(/\\D/g,''))||0).slice(0,10)""")
        check(pts == sorted(pts, reverse=True), "sort by upvotes is descending", str(pts[:5]))
        check(pts and pts[0] > 1500, "top upvoted is genuinely top", str(pts[:1]))

        page.click('.sort-switch button[data-sort="date"]')
        page.wait_for_timeout(600)
        yrs = page.evaluate("""() => [...document.querySelectorAll('#results .r-meta')]
            .map(e => { const y = e.querySelector('.r-year'); return y?+y.textContent:0 })
            .filter(Boolean).slice(0,8)""")
        check(yrs == sorted(yrs, reverse=True), "sort by newest is descending", str(yrs[:5]))

        page.click('.sort-switch button[data-sort="oldest"]')
        page.wait_for_timeout(600)
        yrs_old = page.evaluate("""() => [...document.querySelectorAll('#results .r-meta')]
            .map(e => { const y = e.querySelector('.r-year'); return y?+y.textContent:0 })
            .filter(Boolean).slice(0,5)""")
        check(yrs_old and yrs_old[0] <= 2009, "sort by oldest reaches 2007-2009", str(yrs_old[:3]))

        # --- newsroom toggle ---
        page.click('.sort-switch button[data-sort="relevance"]')
        page.fill("#q", "chip")
        page.wait_for_timeout(700)
        before = page.locator("#status").inner_text()
        page.uncheck("#hide-news")
        page.wait_for_timeout(700)
        after = page.locator("#status").inner_text()
        nb = int(before.split()[0].replace(",", "")) if before else 0
        na = int(after.split()[0].replace(",", "")) if after else 0
        check(na > nb, "newsroom toggle widens the corpus", f"{nb} -> {na}")
        page.check("#hide-news")

        # --- topic facet ---
        page.fill("#q", "")
        page.wait_for_timeout(400)
        page.locator("#topics .chip").nth(5).click()   # security
        page.wait_for_timeout(700)
        n_sec = page.locator("#results li").count()
        check(n_sec > 0, "topic facet returns results", f"{n_sec} rows")
        page.locator("#topics .chip").nth(5).click()

        # --- blogs mode ---
        page.click('.mode-switch button[data-mode="blogs"]')
        page.wait_for_timeout(700)
        nb_rows = page.locator("#results li").count()
        first_blog = page.locator("#results li").first.inner_text().replace("\n", " | ")
        check(nb_rows > 0, "blogs mode renders", f"{nb_rows} rows")
        print("  top blog:", first_blog[:104])
        page.click('.mode-switch button[data-mode="posts"]')

        # --- REGRESSION: blogs-mode total must not equal the page size ---
        page.click('.mode-switch button[data-mode="blogs"]')
        page.fill("#q", "")
        page.wait_for_timeout(800)
        btxt = page.locator("#status").inner_text()
        btotal = int(btxt.split()[0].replace(",", "")) if btxt else 0
        brows = page.locator("#results > li").count()
        check(btotal > brows, "blogs mode reports the true total, not the page size",
              f"{brows} rows of {btotal}")
        check(not page.locator("#more").is_hidden(), "blogs mode offers Show more")
        page.click('.mode-switch button[data-mode="posts"]')
        page.wait_for_timeout(500)

        # --- REGRESSION: relevance must not collapse into popularity ---
        page.fill("#q", "rust")
        page.click('.sort-switch button[data-sort="relevance"]')
        page.wait_for_timeout(800)
        rel = page.evaluate(
            "() => [...document.querySelectorAll('#results > li .r-title')].slice(0,5).map(e=>e.textContent)")
        page.click('.sort-switch button[data-sort="points"]')
        page.wait_for_timeout(800)
        pop = page.evaluate(
            "() => [...document.querySelectorAll('#results > li .r-title')].slice(0,5).map(e=>e.textContent)")
        check(rel != pop, "relevance ordering differs from popularity ordering")
        on_topic = sum(1 for t in rel if "rust" in t.lower())
        check(on_topic >= 4, "relevance top 5 are on-topic for 'rust'", f"{on_topic}/5")
        page.click('.sort-switch button[data-sort="relevance"]')

        # --- contrast: matched text must not be the least legible thing on screen ---
        def lum(css_rgb):
            nums = [int(x) for x in css_rgb.replace("rgb(", "").replace(")", "").split(",")[:3]]
            def ch(c):
                c = c / 255
                return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
            r, g, b = (ch(v) for v in nums)
            return 0.2126 * r + 0.7152 * g + 0.0722 * b

        for theme in ("light", "dark"):
            if theme == "dark":
                page.click(".theme-toggle")
                page.wait_for_timeout(400)
            page.fill("#q", "rust")
            page.wait_for_timeout(700)
            cols = page.evaluate('''() => {
                const m = document.querySelector('#results > li .r-title mark');
                const body = getComputedStyle(document.body).backgroundColor;
                return m ? [getComputedStyle(m).color, body] : null;
            }''')
            if cols:
                l1, l2 = lum(cols[0]), lum(cols[1])
                ratio = (max(l1, l2) + 0.05) / (min(l1, l2) + 0.05)
                check(ratio >= 4.5, f"matched-text contrast passes AA ({theme})",
                      f"{ratio:.2f}:1")
        page.click(".theme-toggle")
        page.wait_for_timeout(300)

        # --- keyboard ---
        page.fill("#q", "rust")
        page.wait_for_timeout(600)
        page.keyboard.press("ArrowDown")
        page.wait_for_timeout(150)
        sel = page.locator("#results li.sel").count()
        check(sel == 1, "ArrowDown selects a row", f"{sel} selected")

        # --- URL state round-trip ---
        page.fill("#q", "kernel")
        page.click('.sort-switch button[data-sort="points"]')
        page.wait_for_timeout(600)
        url = page.url
        check("q=kernel" in url and "sort=points" in url, "URL reflects state", url[-58:])
        page2 = browser.new_page()
        page2.goto(url, wait_until="load")
        page2.wait_for_function("() => !document.querySelector('#q').disabled", timeout=60000)
        page2.wait_for_timeout(900)
        check(page2.input_value("#q") == "kernel", "shared URL restores query")
        check(page2.locator('.sort-switch button[data-sort="points"].active').count() == 1,
              "shared URL restores sort")
        page2.close()

        # --- suggestion chips (empty state) ---
        page.fill("#q", "")
        page.wait_for_timeout(500)
        check(page.locator("#suggests").is_visible(), "suggestions show on empty query")
        page.locator('#suggests button[data-q="sqlite internals"]').click()
        page.wait_for_timeout(800)
        check(page.input_value("#q") == "sqlite internals", "suggestion fills the query")
        check(not page.locator("#suggests").is_visible(),
              "suggestions hide once a query is active")

        # --- per-blog view ---
        page.fill("#q", "")
        page.wait_for_timeout(400)
        page.click('.mode-switch button[data-mode="blogs"]')
        page.wait_for_timeout(700)
        blog_name = page.locator("#results li .r-title").first.inner_text().strip()
        page.locator("#results li .blog-open").first.click()
        page.wait_for_timeout(800)
        check(page.locator("#pin").is_visible(), "pinning a blog shows its header")
        pin_text = page.locator("#pin .pin-name").inner_text().strip()
        check(pin_text == blog_name, "pinned blog matches the one clicked",
              f"{pin_text} vs {blog_name}")
        rows = page.locator("#results li").count()
        check(rows > 0, "pinned blog lists its posts", f"{rows} rows")
        shown = page.evaluate(
            "() => [...document.querySelectorAll('#results .r-blog')].map(e=>e.textContent)")
        check(len(set(shown)) == 1 and shown[0] == blog_name,
              "pinned view shows only that blog", str(set(shown))[:60])
        check("b=" in page.url, "pinned blog is in the URL", page.url[-46:])
        page.click("#pin .pin-clear")
        page.wait_for_timeout(700)
        check(not page.locator("#pin").is_visible(), "clearing the pin restores all blogs")

        # --- a11y: the meta line must not run numbers together ---
        page.fill("#q", "kernel")
        page.wait_for_timeout(700)
        labels = page.evaluate('''() => {
            const m = document.querySelector('#results .r-meta');
            return [...m.children].map(c => c.getAttribute('aria-label') || c.textContent);
        }''')
        joined = " ".join(labels)
        check("points on Hacker News" in joined and "posted " in joined,
              "meta line exposes labelled points and year", joined[:80])

        # --- dark theme ---
        page.click(".theme-toggle")
        page.wait_for_timeout(400)
        theme = page.get_attribute("html", "data-theme")
        check(theme == "dark", "theme toggle works", str(theme))
        page.screenshot(path="/tmp/claude-1000/-home-eren-erenozen-github-io/03753368-ccae-4d4c-acb7-2798081f5da3/scratchpad/dark.png")
        page.click(".theme-toggle")
        page.wait_for_timeout(400)
        page.screenshot(path="/tmp/claude-1000/-home-eren-erenozen-github-io/03753368-ccae-4d4c-acb7-2798081f5da3/scratchpad/light.png")

        # --- mobile ---
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(400)
        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth > document.documentElement.clientWidth")
        check(not overflow, "no horizontal overflow on mobile")

        # How much chrome sits above the first result on a phone?
        page.set_viewport_size({"width": 360, "height": 640})
        page.reload(wait_until="load")
        page.wait_for_function("() => !document.querySelector('#q').disabled", timeout=60000)
        page.wait_for_selector("#results > li", timeout=20000)
        top = page.evaluate(
            "() => document.querySelector('#results > li').getBoundingClientRect().top")
        visible = page.evaluate(
            "() => [...document.querySelectorAll('#results > li')].filter(e => e.getBoundingClientRect().top < 640).length")
        check(top < 480, "first result is reachable on a 360x640 phone", f"y={top:.0f}px")
        check(visible >= 1, "at least one result in the opening viewport", f"{visible} rows")
        page.screenshot(path="/tmp/claude-1000/-home-eren-erenozen-github-io/03753368-ccae-4d4c-acb7-2798081f5da3/scratchpad/mobile.png")

        browser.close()
    httpd.shutdown()

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("all browser checks passed")


if __name__ == "__main__":
    main()
