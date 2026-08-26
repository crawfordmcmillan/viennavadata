"""fetch.py — pull Town of Vienna, VA council data from the Legistar Web API.

Writes raw, unmodified JSON responses to data/, one file per call.
Skips any call whose cache file already exists, except for meetings in the
last REFRESH_DAYS: their items and votes are re-fetched every run, because
the clerk enters roll calls after the meeting (once minutes are finalized)
and a first fetch during the draft period would otherwise cache an empty
vote list forever. Never renders anything.
"""
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

CLIENT = "vienna-va"
BASE = f"https://webapi.legistar.com/v1/{CLIENT}"
COUNCIL_BODY_NAME = "Town Council Meeting"
DATA = Path(__file__).parent / "data"
SLEEP_SECONDS = 0.1
# Vienna's Legistar history begins 2013-10-28; this captures all of it.
START_DATE = "2013-01-01"
REFRESH_DAYS = 180  # re-fetch items and votes for meetings this recent, every run

session = requests.Session()
session.headers["Accept"] = "application/json"


def get_cached(cache_name: str, path: str, params: dict | None = None, refresh: bool = False):
    """GET BASE+path unless data/{cache_name} already exists (or refresh is
    set). Store the raw body verbatim."""
    cache_file = DATA / cache_name
    if cache_file.exists() and not refresh:
        print(f"cached  {cache_name}")
        return json.loads(cache_file.read_text(encoding="utf-8"))
    url = f"{BASE}{path}"
    print(f"GET     {url}" + (f"  {params}" if params else ""))
    resp = session.get(url, params=params, timeout=30)
    resp.raise_for_status()
    cache_file.write_text(resp.text, encoding="utf-8")
    time.sleep(SLEEP_SECONDS)
    return resp.json()


def main():
    DATA.mkdir(exist_ok=True)

    bodies = get_cached("bodies.json", "/bodies")
    council = next(b for b in bodies if b["BodyName"] == COUNCIL_BODY_NAME)
    body_id = council["BodyId"]
    print(f"        {COUNCIL_BODY_NAME} -> BodyId {body_id}")

    get_cached("persons.json", "/persons")
    get_cached(f"officerecords_{body_id}.json", f"/bodies/{body_id}/officerecords")

    events = get_cached(
        "events.json",
        "/events",
        {"$filter": f"EventBodyId eq {body_id} and EventDate ge datetime'{START_DATE}'"},
    )
    print(f"        {len(events)} events since {START_DATE}")

    refresh_after = (datetime.now(timezone.utc) - timedelta(days=REFRESH_DAYS)).strftime("%Y-%m-%d")
    n_items = 0
    n_votes = 0
    for event in events:
        event_id = event["EventId"]
        recent = event["EventDate"][:10] >= refresh_after
        items = get_cached(f"eventitems_{event_id}.json", f"/events/{event_id}/eventitems",
                           refresh=recent)
        n_items += len(items)
        for item in items:
            item_id = item["EventItemId"]
            votes = get_cached(f"votes_{item_id}.json", f"/eventitems/{item_id}/votes",
                               refresh=recent)
            n_votes += len(votes)

    (DATA / "fetch_meta.json").write_text(
        json.dumps(
            {
                "client": CLIENT,
                "body_id": body_id,
                "events_since": START_DATE,
                "fetched_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"done    {len(events)} events, {n_items} agenda items, {n_votes} vote records")


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException as e:
        print(f"error   {e}", file=sys.stderr)
        sys.exit(1)
