#!/usr/bin/env python3
"""Fetch every Nomad outlet x brand Zomato order page, parse menu items and
ratings, and upload the result to the Apps Script dashboard.

Env vars:
  INGEST_URL   Apps Script web app URL (the one ending in /exec)
  INGEST_KEY   shared secret, must match the INGEST_KEY script property
  DRY_RUN=1    fetch and parse but do not upload
  MIN_OK       fraction of outlets that must succeed before uploading (default 0.9)
"""
import html as htmlmod
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

CITY_LABELS = {
    "ncr": "Delhi NCR", "mumbai": "Mumbai", "pune": "Pune", "bangalore": "Bengaluru",
    "hyderabad": "Hyderabad", "chennai": "Chennai", "kolkata": "Kolkata", "jaipur": "Jaipur",
    "chandigarh": "Chandigarh", "lucknow": "Lucknow", "dehradun": "Dehradun",
    "ahmedabad": "Ahmedabad", "indore": "Indore", "goa": "Goa", "visakhapatnam": "Visakhapatnam",
}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
NON_ITEMS = {"Report an error in this listing"}
H4_RE = re.compile(r"<h4[^>]*>([^<]+)</h4>")
SECTION_RE = re.compile(r"<section[^>]*>")
TYPE_RE = re.compile(r'<div type="(veg|non-veg|egg)"')
RATING_RE = re.compile(
    r'aggregate_rating\\?":\\?"([^"\\]*)\\?",\\?"rating_text\\?":\\?"[^"\\]*\\?",'
    r'\\?"rating_subtitle\\?":\\?"[^"\\]*\\?",\\?"rating_color\\?":\\?"[^"\\]*\\?",'
    r'\\?"votes\\?":\\?"?([0-9.,]+K?)\\?"?'
)


def field(page, key):
    m = re.search(r'\\?"' + key + r'\\?":\s*\\?"?([^,}\\"]*)', page)
    return m.group(1).strip() if m else ""


def page_status(page, n_items):
    """-> (state, reason) from the outlet's own Zomato page."""
    if n_items == 0:
        return "closed", "No menu on the Zomato link"
    if field(page, "is_perm_closed") == "true":
        return "closed", "Permanently closed"
    if field(page, "is_temp_closed") == "true":
        return "closed", "Temporarily closed"
    low = field(page, "res_status_text").lower()
    if "clos" in low or "not available" in low or "not accepting" in low:
        return "closed", "Offline now"
    return "live", "Live now"


def city_of(url):
    m = re.match(r"https://www\.zomato\.com/([^/]+)/", url)
    slug = m.group(1) if m else "other"
    return CITY_LABELS.get(slug, slug.title())


def fetch(url):
    base = url.rstrip("/")
    if not base.endswith("/order"):
        base += "/order"
    req = urllib.request.Request(base, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=40) as resp:
        return resp.status, resp.read().decode("utf-8", errors="ignore")


def parse_items(page):
    sections = [m.start() for m in SECTION_RE.finditer(page)]
    cats, items = [], []
    for m in H4_RE.finditer(page):
        pos, name = m.start(), htmlmod.unescape(m.group(1)).strip()
        if name in NON_ITEMS:
            continue
        (cats if any(0 <= pos - s <= 80 for s in sections) else items).append((pos, name))
    types = [m.group(1) for m in TYPE_RE.finditer(page)]
    out, ci = [], 0
    for i, (pos, name) in enumerate(items):
        while ci + 1 < len(cats) and cats[ci + 1][0] < pos:
            ci += 1
        cat = cats[ci][1] if cats else ""
        out.append((cat.strip().rstrip(".").strip() or "Uncategorized", name,
                    types[i] if i < len(types) else "na"))
    return out


def work(o):
    err = ""
    for attempt in range(4):
        try:
            status, page = fetch(o["url"])
            if status != 200:
                raise RuntimeError(f"HTTP {status}")
            m = RATING_RE.search(page)
            rating = (m.group(1) or None, m.group(2).rstrip(",")) if m else (None, None)
            items = parse_items(page)
            return o, items, rating, None, page_status(page, len(items))
        except Exception as e:  # noqa: BLE001
            err = str(e)
            time.sleep((12 if "429" in err else 2) * (attempt + 1))
    return o, [], (None, None), err, ("closed", "Could not read the page")


def main():
    outlets = json.load(open(os.path.join(os.path.dirname(__file__), "zomato_outlet_links.json")))
    started = time.time()
    with ThreadPoolExecutor(max_workers=3) as ex:
        results = list(ex.map(work, outlets))
    # second, slower pass over anything that failed
    for i, r in enumerate(results):
        if r[3]:
            time.sleep(5)
            results[i] = work(outlets[i])

    menu, ratings, statuses, ok, failed = [], [], [], 0, []
    for o, items, (rating, votes), err, status in results:
        if err:
            failed.append(f"{o['brand']}/{o['outlet']}: {err}")
            continue
        ok += 1
        statuses.append([o["brand"], o["outlet"], status[0], status[1]])
        c = city_of(o["url"])
        for cat, name, typ in items:
            menu.append([o["brand"], o["outlet"], c, cat, name, typ])
        if rating:
            ratings.append([o["brand"], o["outlet"], rating, votes or ""])

    keep = [[o["brand"], o["outlet"]] for o, _items, _r, err, _s in results if err]
    frac = ok / len(outlets)
    live_n = sum(1 for s in statuses if s[2] == "live")
    print(f"fetched {ok}/{len(outlets)} outlets ({frac:.0%}) in {time.time()-started:.0f}s; "
          f"{len(menu)} menu rows, {len(ratings)} ratings; {live_n} live / {len(statuses)-live_n} closed; {len(keep)} kept from previous data")
    for f in failed[:10]:
        print("  FAILED", f)

    if os.environ.get("DRY_RUN"):
        print("DRY_RUN set: not uploading")
        return 0
    if frac < float(os.environ.get("MIN_OK", "0.9")):
        print("Too many failures; not uploading so existing dashboard data is kept.")
        return 1
    min_rows = int(os.environ.get("MIN_ROWS", "10000"))
    if len(menu) < min_rows:
        print(f"Only {len(menu)} menu rows (expected at least {min_rows}); the site may be "
              "serving empty pages. Not uploading so existing dashboard data is kept.")
        return 1

    url, key = os.environ["INGEST_URL"], os.environ["INGEST_KEY"]
    body = json.dumps({"key": key, "platform": "zomato", "menu": menu, "ratings": ratings,
                       "status": statuses,
                       "keep": keep}).encode()
    reply = ""
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "text/plain"})
            with urllib.request.urlopen(req, timeout=180) as resp:
                reply = resp.read().decode()
            break
        except Exception as e:  # noqa: BLE001
            print(f"upload attempt {attempt + 1} failed: {e}")
            if attempt == 3:
                raise
            time.sleep(20 * (attempt + 1))
    print("upload reply:", reply[:300])
    return 0 if '"ok":true' in reply.replace(" ", "") else 1


if __name__ == "__main__":
    sys.exit(main())
