#!/usr/bin/env python3
"""Fetch every Nomad outlet x brand Swiggy menu + rating and upload the result
to the Apps Script dashboard.

The outlet list comes from the dashboard itself, so there is one source of truth.

Env vars:
  INGEST_URL   Apps Script web app URL (the one ending in /exec)
  INGEST_KEY   shared secret, must match the INGEST_KEY script property
  DRY_RUN=1    fetch and parse but do not upload
  MIN_OK       fraction of outlets that must be fetched without error (default 0.9)
"""
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
MENU_URL = ("https://www.swiggy.com/mapi/menu/pl?page-type=REGULAR_MENU&complete-menu=true"
            "&lat=28.6327&lng=77.2198&restaurantId={}")
REST_TYPE = "type.googleapis.com/swiggy.presentation.food.v2.Restaurant"


def post_dashboard(payload):
    url = os.environ["INGEST_URL"]
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "text/plain"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode())


def fetch_menu(rid):
    req = urllib.request.Request(MENU_URL.format(rid), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=40) as resp:
        return json.loads(resp.read().decode("utf-8"))


def veg_type(info):
    """Swiggy marks veg items with isVeg=1 and leaves it out for non-veg, so use the
    itemAttribute.vegClassifier field first."""
    c = str(((info.get("itemAttribute") or {}).get("vegClassifier")) or "").upper().replace("_", "").replace("-", "")
    if c == "VEG":
        return "veg"
    if c == "NONVEG":
        return "non-veg"
    if c == "EGG":
        return "egg"
    return "veg" if info.get("isVeg") in (1, "1", True) else "na"


def status_of(info, n_items):
    """-> (state, reason) from the outlet's own Swiggy link."""
    if n_items == 0:
        return "closed", "Disabled on Swiggy"
    t = (info or {}).get("timingsInfo") or {}
    st = (t.get("status") or "").strip()
    if st.lower().startswith("clos"):
        return "closed", "Offline now"
    return "live", "Live now"


def extract(j):
    """-> (rating, total_ratings, items, (state, reason)); items empty when the store has no live menu."""
    cards = (j.get("data") or {}).get("cards") or []
    if j.get("statusCode") != 0 or not cards:
        return None, None, [], ("closed", "Disabled on Swiggy")
    info = None
    for c in cards:
        inner = (c.get("card") or {}).get("card")
        if inner and inner.get("@type") == REST_TYPE:
            info = inner.get("info") or {}
            break
    rating = info.get("avgRatingString") if info else None
    total = info.get("totalRatingsString") if info else None
    grouped = next((c for c in cards if "groupedCard" in c), None)
    regular = grouped["groupedCard"]["cardGroupMap"]["REGULAR"]["cards"] if grouped else []
    items = []
    for c in regular:
        inner = (c.get("card") or {}).get("card") or {}
        t = inner.get("@type", "")
        if "ItemCategory" not in t and "NestedItemCategory" not in t:
            continue
        for cat in inner.get("categories") or [inner]:
            for ic in cat.get("itemCards", []):
                i = ic["card"]["info"]
                if i.get("inStock") == 0 or not i.get("name"):
                    continue
                category = (i.get("category") or cat.get("title") or inner.get("title") or "")
                items.append((category.strip().rstrip(".").strip() or "Uncategorized",
                              i["name"].strip(), veg_type(i)))
    return rating, total, items, status_of(info, len(items))


def work(p):
    err = ""
    for attempt in range(4):
        try:
            time.sleep(0.8)
            return p, extract(fetch_menu(p["restaurantId"])), None
        except Exception as e:  # noqa: BLE001
            err = str(e)
            time.sleep((10 if "429" in err else 3) * (attempt + 1))
    return p, (None, None, [], ("closed", "Could not read the link")), err


def main():
    started = time.time()
    reply = post_dashboard({"key": os.environ["INGEST_KEY"], "platform": "swiggy_pairs"})
    if not reply.get("ok"):
        print("Could not get the outlet list:", reply)
        return 1
    pairs = reply["pairs"]
    with ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(work, pairs))

    menu, ratings, statuses, failed, live = [], [], [], [], 0
    for p, (rating, total, items, status), err in results:
        if err:
            failed.append(f"{p['brand']}/{p['outlet']}: {err}")
            continue
        statuses.append([p["brand"], p["outlet"], status[0], status[1]])
        if not items:
            continue  # no menu on the link
        live += 1
        for cat, name, typ in items:
            menu.append([p["brand"], p["outlet"], cat, name, typ])
        if rating:
            ratings.append([p["brand"], p["outlet"], rating, total or ""])

    ok_frac = 1 - len(failed) / len(pairs)
    open_n = sum(1 for x in statuses if x[2] == "live")
    print(f"{len(pairs)} outlets: {live} with a menu, {len(pairs) - live - len(failed)} without, "
          f"{len(failed)} errors; {open_n} open now / {len(statuses) - open_n} closed; "
          f"{len(menu)} menu rows, {len(ratings)} ratings in {time.time()-started:.0f}s")
    for f in failed[:10]:
        print("  FAILED", f)

    if os.environ.get("DRY_RUN"):
        print("DRY_RUN set: not uploading")
        return 0
    if ok_frac < float(os.environ.get("MIN_OK", "0.9")) or live == 0:
        print("Too many failures; not uploading so existing dashboard data is kept.")
        return 1
    min_live = int(os.environ.get("MIN_LIVE", "150"))
    min_rows = int(os.environ.get("MIN_ROWS", "15000"))
    if live < min_live or len(menu) < min_rows:
        print(f"Only {live} live outlets / {len(menu)} menu rows (expected at least {min_live} / "
              f"{min_rows}); the site may be serving empty pages. Not uploading so existing "
              "dashboard data is kept.")
        return 1
    reply = post_dashboard({"key": os.environ["INGEST_KEY"], "platform": "swiggy",
                            "menu": menu, "ratings": ratings, "status": statuses})
    print("upload reply:", json.dumps(reply)[:300])
    return 0 if reply.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
