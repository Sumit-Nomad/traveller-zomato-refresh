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
            # Before login completes the app fires GraphQL calls with a literal
            # "authorization: null" placeholder header - confirmed live in a failed
            # run (token came back as the 4-character string "null"). Only a real
            # bearer token is worth keeping.
            if auth and auth.lower().startswith("bearer "):
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
    # Enter does not submit this form; there is a separate "Login" button.
    pw = page.locator('input[type="password"]').first
    try:
        pw.wait_for(state="visible", timeout=15000)
    except Exception:
        pw = None  # already authenticated from a saved session on this runner - straight to the next screen
    if pw is not None:
        pw.fill(os.environ["ATLAS_PASSWORD"])
        login_btn = page.get_by_role("button", name="Login", exact=True)
        try:
            login_btn.wait_for(state="visible", timeout=25000)
            login_btn.click()
        except Exception as e:
            print(f"DIAG: could not click the Login button ({e}); pressing Enter instead")
            pw.press("Enter")
        page.wait_for_timeout(3000)
        print(f"DIAG: after submitting the password, url={page.url}")
        error_texts = page.locator('text=/incorrect|invalid|error|wrong/i').all_inner_texts()
        if error_texts:
            print(f"DIAG: possible error message(s) on page: {error_texts}")

    # Screen 3 (if shown): pick which business/outlet group to sign in as. This is a real
    # <button role> named "Nomad by UrbanPiper. Roles: Admin, Administrator" inside a
    # <ul role="list">, confirmed by inspecting the live page - use that exact semantic.
    try:
        page.wait_for_url(re.compile(r"login\.urbanpiper\.com/business"), timeout=15000)
        # "Your active businesses (N)" loads in after the page shell - wait for the list itself,
        # not just a fixed delay, since the API call behind it is sometimes slow.
        page.get_by_text("Your active businesses", exact=False).wait_for(state="visible", timeout=30000)
        tile = page.get_by_role("button", name="Nomad by UrbanPiper").first
        try:
            tile.wait_for(state="visible", timeout=30000)
            tile.scroll_into_view_if_needed()
            tile.hover()
            tile.click(timeout=10000)
            page.wait_for_timeout(1500)
            print(f"DIAG: after clicking the tile, url={page.url}")
            if page.url.rstrip("/").endswith("/business"):
                # Click landed but nothing moved - try once more with a forced click.
                print("DIAG: url did not change after the first click; trying a forced click")
                tile.click(force=True, timeout=10000)
                page.wait_for_timeout(1500)
                print(f"DIAG: after the forced click, url={page.url}")
        except Exception as e:
            print(f"DIAG: could not find/click the 'Nomad by UrbanPiper' tile ({e})")
            try:
                all_text = page.locator('body').inner_text()
                print(f"DIAG: business-page text: {all_text[:1500]!r}")
            except Exception:
                pass
            try:
                page.screenshot(path="atlas_business_page.png", full_page=True)
                print("DIAG: saved screenshot to atlas_business_page.png")
            except Exception:
                pass
    except Exception as e:
        print(f"DIAG: business-picker step raised before finding the tile: {e}")
        try:
            page.screenshot(path="atlas_business_page.png", full_page=True)
            print("DIAG: saved screenshot to atlas_business_page.png")
        except Exception:
            pass

    # After picking the business (now on login.urbanpiper.com/business/<id>) there is an
    # app picker - "Atlas" vs "Prime" - confirmed live. The business name is ALSO repeated
    # as a "Back to business selection" button on this screen; clicking that instead (an
    # earlier, wrong assumption) just bounced back to the business list forever.
    try:
        atlas_app = page.get_by_role("button", name="Atlas", exact=False)
        atlas_app.wait_for(state="visible", timeout=15000)
        atlas_app.click()
    except Exception as e:
        print(f"DIAG: could not find/click the 'Atlas' app tile ({e})")
        try:
            page.screenshot(path="atlas_app_picker.png", full_page=True)
            print("DIAG: saved screenshot to atlas_app_picker.png")
        except Exception:
            pass

    try:
        page.wait_for_url(re.compile(r"atlas\.urbanpiper\.com/(?!login)"), timeout=45000)
    except Exception as e:
        print(f"DIAG: stuck waiting for the atlas redirect. Current URL: {page.url}")
        try:
            print(f"DIAG: page title: {page.title()!r}")
            print(f"DIAG: visible buttons: {[b.inner_text()[:60] for b in page.locator('button').all()[:15]]}")
        except Exception as diag_e:
            print(f"DIAG: could not inspect page: {diag_e}")
        try:
            page.screenshot(path="atlas_login_stuck.png", full_page=True)
            print("DIAG: saved screenshot to atlas_login_stuck.png")
        except Exception as shot_e:
            print(f"DIAG: could not screenshot: {shot_e}")
        raise

    page.goto("https://atlas.urbanpiper.com/locations", wait_until="networkidle", timeout=60000)
    for _ in range(20):
        if token.get("value"):
            return token["value"]
        page.wait_for_timeout(500)
    raise RuntimeError("Signed in but never saw an authenticated request to the locations API")


def fetch_all_stores(page, token):
    # Call the GraphQL API as an in-page fetch() (so it carries the page's real Origin,
    # Referer and cookies) rather than Playwright's separate page.request client, which
    # sends a bare HTTP request with only the headers we set and got back 0 results.
    stores, offset = [], 0
    while True:
        result = page.evaluate(
            """async ({url, token, query, variables}) => {
                const r = await fetch(url, {
                    method: 'POST',
                    headers: { 'authorization': token, 'content-type': 'application/json' },
                    body: JSON.stringify({ operationName: 'getLocationsList', variables, query }),
                });
                const text = await r.text();
                return { status: r.status, text };
            }""",
            {
                "url": GRAPHQL_URL,
                "token": token,
                "query": LOCATIONS_QUERY,
                "variables": {"limit": PAGE_SIZE, "offset": offset,
                              "filters": [{"field": "is_active", "value": "true"}],
                              "sort": {"field": "name", "order": "ASC"}},
            },
        )
        if result["status"] != 200:
            raise RuntimeError(f"Atlas API returned HTTP {result['status']}: {result['text'][:300]}")
        data = json.loads(result["text"])
        if data.get("errors"):
            raise RuntimeError(f"Atlas API returned GraphQL errors: {json.dumps(data['errors'])[:500]}")
        if not offset:
            print(f"DIAG: first page raw response: {result['text'][:500]}")
        block = (data.get("data") or {}).get("stores") or {}
        objects = block.get("objects") or []
        stores.extend(objects)
        if not objects or len(stores) >= (block.get("count") or 0):
            break
        offset += PAGE_SIZE
    return stores


def build_rows(stores, ref_map):
    """-> {platform: [[brand, outlet, ref, state, text], ...]} for refs we track.

    The API returns `state` as a numeric code, not a word - confirmed live against the
    real Atlas account: "1" = enabled/online, "0" = disabled/offline. Other codes ("2",
    "3", ...) turned up on the large majority of locations for platforms they are not
    actually onboarded to (dine-in-only outlets showing an "urbanpiper"/"dotpe" code,
    for example) - their meaning isn't confirmed, so those rows are skipped rather than
    guessed at, and the outlet just falls back to its other status sources."""
    out = {}
    for s in stores:
        loc = ref_map.get(s.get("merchantRefId"))
        if not loc:
            continue
        for lp in s.get("locationPlatforms") or []:
            platform = (lp.get("platformName") or "").strip()
            state = str(lp.get("state") if lp.get("state") is not None else "")
            if not platform or state not in ("0", "1"):
                continue
            online = state == "1"
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
        print(f"DIAG: got a token (len={len(token)}, starts={token[:20]!r}); page is now at {page.url}")
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
