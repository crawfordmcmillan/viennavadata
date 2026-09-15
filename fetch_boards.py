"""fetch_boards.py — pull Town of Vienna planning and land-use board records.

Same Legistar API and cache discipline as fetch.py, for the boards that decide
what gets built: Planning Commission, Board of Zoning Appeals, Board of
Architectural Review, and the Windover Heights Board of Review (the historic
district), plus their work sessions. Agenda items only; these pages show
decisions and outcomes, not per-member roll calls.
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

CLIENT = "vienna-va"
BASE = f"https://webapi.legistar.com/v1/{CLIENT}"
DATA = Path(__file__).parent / "data" / "boards"
SLEEP_SECONDS = 0.1
START_DATE = "2013-01-01"

BODIES = {
    173: "Planning Commission",
    174: "Board of Zoning Appeals",
    178: "Board of Architectural Review",
    181: "Planning Commission Work Session",
    182: "Board of Architectural Review Work Session",
    185: "Windover Heights Board of Review",
    200: "Windover Heights Board of Review Work Session",
}

session = requests.Session()
session.headers["Accept"] = "application/json"


RETRIES = 4


def get_retry(session, url, **kwargs):
    """Public servers drop connections and return transient 5xx errors;
    retry before giving up."""
    for attempt in range(RETRIES):
        try:
            resp = session.get(url, **kwargs)
            if resp.status_code < 500 or attempt == RETRIES - 1:
                return resp
            print(f"retry   attempt {attempt + 2} after HTTP {resp.status_code}")
        except requests.RequestException:
            if attempt == RETRIES - 1:
                raise
            print(f"retry   attempt {attempt + 2} after a failed request")
        time.sleep(20)

def get_cached(cache_name: str, path: str, params: dict | None = None):
    cache_file = DATA / cache_name
    if cache_file.exists():
        print(f"cached  {cache_name}")
        return json.loads(cache_file.read_text(encoding="utf-8"))
    url = f"{BASE}{path}"
    print(f"GET     {url}" + (f"  {params}" if params else ""))
    resp = get_retry(session, url, params=params, timeout=30)
    resp.raise_for_status()
    cache_file.write_text(resp.text, encoding="utf-8")
    time.sleep(SLEEP_SECONDS)
    return resp.json()


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    n_events = 0
    n_items = 0
    for body_id, name in BODIES.items():
        events = get_cached(
            f"events_{body_id}.json",
            "/events",
            {"$filter": f"EventBodyId eq {body_id} and EventDate ge datetime'{START_DATE}'"},
        )
        print(f"        {name}: {len(events)} meetings")
        n_events += len(events)
        for event in events:
            items = get_cached(
                f"eventitems_{event['EventId']}.json",
                f"/events/{event['EventId']}/eventitems",
            )
            n_items += len(items)

    (DATA / "boards_meta.json").write_text(
        json.dumps(
            {
                "client": CLIENT,
                "bodies": BODIES,
                "events_since": START_DATE,
                "fetched_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"done    {n_events} meetings, {n_items} agenda items")


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException as e:
        print(f"error   {e}", file=sys.stderr)
        sys.exit(1)
