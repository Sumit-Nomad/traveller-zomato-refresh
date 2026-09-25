#!/usr/bin/env python3
"""Read every outlet's real Online/Offline state from the Zomato partner portal
and upload it to the Apps Script dashboard.

The session cookie is saved in the dashboard's Admin bar (Zomato session) and
fetched here with the ingest key, so no Zomato secret lives in GitHub.

Env vars:
  INGEST_URL   Apps Script web app URL (the one ending in /exec)
  INGEST_KEY   shared secret, must match the INGEST_KEY script property
  DRY_RUN=1    read and map but do not upload
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

LIST_URL = "https://api.zomato.com/merchant-gw/web/restaurant/get-all-minimal-lite"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
MIN_ENTITIES = 100


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
            post_dashboard({"key": os.environ["INGEST_KEY"], "platform": "zomato_partner_error", "message": message})
        except Exception as e:  # noqa: BLE001
            print("could not report the error to the dashboard:", e)
    return 1


def fetch_list(cookie):
    req = urllib.request.Request(LIST_URL, headers={
        "Cookie": cookie, "User-Agent": UA, "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.zomato.com", "Referer": "https://www.zomato.com/partners/onlineordering",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def build_status(entities, mapping):
    by_id = {int(e["id"]): e for e in entities if "id" in e and "delivery_status" in e}
    rows, missing = [], []
    for m in mapping:
        e = by_id.get(int(m["res_id"]))
        if e is None:
            missing.append(f"{m['brand']}/{m['outlet']}")
            continue
        online = int(e["delivery_status"]) == 1
        rows.append([m["brand"], m["outlet"], "live" if online else "closed",
                     "Online in the Zomato partner app" if online else "Offline in the Zomato partner app"])
    return rows, missing


def main():
    key = os.environ["INGEST_KEY"]
    cfg = post_dashboard({"key": key, "platform": "zomato_partner_config"})
    if not cfg.get("ok"):
        return fail(f"Could not get the saved Zomato session from the dashboard: {cfg}")
    cookie = (cfg.get("cookie") or "").strip()
    if not cookie:
        print("No Zomato session saved yet. Paste it in the dashboard Admin bar; nothing to do.")
        return 0
    try:
        data = fetch_list(cookie)
    except urllib.error.HTTPError as e:
        return fail(f"Zomato rejected the saved session (HTTP {e.code}); it has probably expired. Paste a fresh Cookie header in the dashboard Admin bar.")
    except Exception as e:  # noqa: BLE001
        return fail(f"Could not read the Zomato outlet list: {e}")
    entities = data.get("entities") if isinstance(data, dict) else None
    if not isinstance(entities, list) or len(entities) < MIN_ENTITIES:
        got = len(entities) if isinstance(entities, list) else "none"
        return fail(f"Zomato returned {got} outlets (expected at least {MIN_ENTITIES}); the session may have expired.")
    mapping = json.load(open(os.path.join(os.path.dirname(__file__), "zomato_res_ids.json")))
    rows, missing = build_status(entities, mapping)
    if len(rows) < len(mapping) * 0.5:
        return fail(f"Only {len(rows)} of {len(mapping)} outlets are visible to this Zomato login.")
    online = sum(1 for r in rows if r[2] == "live")
    print(f"{len(entities)} outlets in the portal; matched {len(rows)}/{len(mapping)} "
          f"({online} online / {len(rows) - online} offline); {len(missing)} not visible to this login")
    for m in missing[:10]:
        print("  not in portal:", m)
    if os.environ.get("DRY_RUN"):
        print("DRY_RUN set: not uploading")
        return 0
    reply = post_dashboard({"key": key, "platform": "zomato_partner", "status": rows})
    print("upload reply:", json.dumps(reply)[:300])
    return 0 if reply.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
