#!/usr/bin/env python3
"""Log in to Atlas headlessly and read every location's on/off state for every
platform (Zomato, Swiggy, ...), straight from Atlas's own Locations page. This
is the most trustworthy source: Atlas shows exactly what each platform has
reported to UrbanPiper, independent of whether a webhook fired for it.

Needs Playwright (pip install playwright && playwright install chromium).

Env vars:
  ATLAS_EMAIL, ATLAS_PASSWORD   Atlas login (email + password only, no OTP)
  INGEST_URL   Apps Script web app URL (the one ending in /exec)
  INGEST_KEY   shared secret, must match the INGEST_KEY script property
  DRY_RUN=1    read and map but do not upload
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

GRAPHQL_URL = "https://atlas-backend.svc.urbanpiper.com/graphql"
LOCATIONS_QUERY = """
query getLocationsList($limit: Int, $offset: Int, $filters: [ListFilterArgument], $sort: SortInput) {
  stores(limit: $limit, offset: $offset, filters: $filters, sort: $sort) {
    count
    objects {
      merchantRefId
      name
      locationPlatforms { platformName state }
    }
  }
}
"""
PAGE_SIZE = 100


def post_dashboard(payload):
    url = os.environ["INGEST_URL"]
    body = json.dumps(payload).encode()
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "text/plain"})
            with urllib.request.urlopen(req, timeout=180) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:  # noqa: BLE001
            print(f"dashboard call attempt {attempt + 1} failed: {e}")
            if attempt == 3:
                raise
            time.sleep(15 * (attempt + 1))


def fail(message):
    print("ERROR:", message)
    if not os.environ.get("DRY_RUN"):
        try:
            post_dashboard({"key": os.environ["INGEST_KEY"], "platform": "atlas_status_error", "message": message})
        except Exception as e:  # noqa: BLE001
            print("could not report the error to the dashboard:", e)
    return 1


def login_and_get_token(page):
    """Sign in with email + password, then (if shown) pick the business, and capture
    the bearer token Atlas sends to its own API. UrbanPiper's login is a few separate
    screens (identifier, then password, then sometimes a business picker), not one form."""
    token = {}

    def on_request(req):
        if GRAPHQL_URL in req.url:
            auth = req.headers.get("authorization")
            if auth:
                token["value"] = auth

    page.on("request", on_request)

    # Screen 1: email/mobile identifier (a bare text box, no type="email"/name="email").
    page.goto("https://login.urbanpiper.com/login/email-mobile/?redirect=atlas",
              wait_until="domcontentloaded", timeout=60000)
    email_box = page.locator('input[placeholder*="example.com"], input[type="text"]').first
    email_box.wait_for(state="visible", timeout=30000)
    email_box.fill(os.environ["ATLAS_EMAIL"])
    email_box.press("Enter")

    # Screen 2 (if shown): password - only when the identifier isn't already signed in.
    pw = page.locator('input[type="password"]').first
    try:
        pw.wait_for(state="visible", timeout=15000)
        pw.fill(os.environ["ATLAS_PASSWORD"])
        pw.press("Enter")
    except Exception:
        pass  # already authenticated from a saved session on this runner - straight to the next screen

    # Screen 3 (if shown): pick which business/outlet group to sign in as.
    try:
        page.wait_for_url(re.compile(r"login\.urbanpiper\.com/business"), timeout=15000)
        tile = page.locator('button:has-text("Nomad by UrbanPiper")').first
        tile.wait_for(state="visible", timeout=15000)
        tile.click()
    except Exception:
        pass  # no business picker shown for this account

    page.wait_for_url(re.compile(r"atlas\.urbanpiper\.com/(?!login)"), timeout=45000)
    page.goto("https://atlas.urbanpiper.com/locations", wait_until="networkidle", timeout=60000)
    for _ in range(20):
        if token.get("value"):
            return token["value"]
        page.wait_for_timeout(500)
    raise RuntimeError("Signed in but never saw an authenticated request to the locations API")


def fetch_all_stores(page, token):
    stores, offset = [], 0
    while True:
        resp = page.request.post(GRAPHQL_URL, headers={"authorization": token, "content-type": "application/json"},
                                  data=json.dumps({
                                      "operationName": "getLocationsList",
                                      "variables": {"limit": PAGE_SIZE, "offset": offset,
                                                    "filters": [{"field": "is_active", "value": "true"}],
                                                    "sort": {"field": "name", "order": "ASC"}},
                                      "query": LOCATIONS_QUERY,
                                  }))
        if resp.status != 200:
            raise RuntimeError(f"Atlas API returned HTTP {resp.status}")
        data = resp.json()
        block = (data.get("data") or {}).get("stores") or {}
        objects = block.get("objects") or []
        stores.extend(objects)
        if not objects or len(stores) >= (block.get("count") or 0):
            break
        offset += PAGE_SIZE
    return stores


def build_rows(stores, ref_map):
    """-> {platform: [[brand, outlet, ref, state, text], ...]} for refs we track."""
    out = {}
    for s in stores:
        loc = ref_map.get(s.get("merchantRefId"))
        if not loc:
            continue
        for lp in s.get("locationPlatforms") or []:
            platform = (lp.get("platformName") or "").strip()
            state = (lp.get("state") or "").lower()
            if not platform or state not in ("enabled", "disabled"):
                continue
            online = state == "enabled"
            out.setdefault(platform, []).append([
                loc["brand"], loc["store"], s["merchantRefId"],
                "live" if online else "closed",
                ("Online" if online else "Offline") + " on Atlas (platform: " + platform + ")",
            ])
    return out


def main():
    key = os.environ["INGEST_KEY"]
    ref_cfg = post_dashboard({"key": key, "platform": "location_ref_map_config"})
    if not ref_cfg.get("ok"):
        return fail(f"Could not get the outlet ref map from the dashboard: {ref_cfg}")
    ref_map = ref_cfg["map"]

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        try:
            token = login_and_get_token(page)
        except Exception as e:  # noqa: BLE001
            browser.close()
            return fail(f"Atlas login failed: {e}")
        try:
            stores = fetch_all_stores(page, token)
        except Exception as e:  # noqa: BLE001
            browser.close()
            return fail(f"Could not read the Atlas locations list: {e}")
        browser.close()

    if len(stores) < 100:
        return fail(f"Atlas returned only {len(stores)} locations (expected hundreds); the login may not have worked.")

    rows_by_platform = build_rows(stores, ref_map)
    total = sum(len(v) for v in rows_by_platform.values())
    print(f"{len(stores)} Atlas locations; matched {total} platform rows across "
          f"{', '.join(f'{p}:{len(v)}' for p, v in rows_by_platform.items())}")
    if os.environ.get("DRY_RUN"):
        print("DRY_RUN set: not uploading")
        return 0
    reply = post_dashboard({"key": key, "platform": "atlas_status", "byPlatform": rows_by_platform})
    print("upload reply:", json.dumps(reply)[:300])
    return 0 if reply.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
