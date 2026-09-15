"""fetch_elections.py — pull precinct-level election results for federal and
statewide contests from the Virginia Department of Elections historical database.

Writes raw, unmodified CSV responses to data/elections/, one file per contest.
Skips any file that already exists. Never renders anything.

Contest IDs were identified from the state's own search API
(va2.elstats.civera.com/api/download_search.csv) filtered to November general
elections for: President, Governor, Lieutenant Governor, Attorney General,
U.S. Senate, U.S. House district 11 (all four Vienna precincts sit in CD-11
throughout), and the Town of Vienna's own Mayor and Town Council races, which
appear in the state database from 2023 when town elections moved to November.
2015, 2019, and 2023 had no statewide or federal contest
(state-legislature-only years).
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = "https://va2.elstats.civera.com/api/download_contest"
DATA = Path(__file__).parent / "data" / "elections"
SLEEP_SECONDS = 0.1

CONTESTS = [
    {"id": 43843, "date": "2013-11-05", "office": "Governor"},
    {"id": 43840, "date": "2013-11-05", "office": "Lieutenant Governor"},
    {"id": 43839, "date": "2013-11-05", "office": "Attorney General"},
    {"id": 43971, "date": "2013-11-05", "office": "House of Delegates", "district": "35"},
    {"id": 66271, "date": "2015-11-03", "office": "State Senate", "district": "34"},
    {"id": 66259, "date": "2015-11-03", "office": "House of Delegates", "district": "35"},
    {"id": 44423, "date": "2014-11-04", "office": "U.S. Senate"},
    {"id": 44490, "date": "2014-11-04", "office": "U.S. House", "district": "11"},
    {"id": 80871, "date": "2016-11-08", "office": "President"},
    {"id": 80920, "date": "2016-11-08", "office": "U.S. House", "district": "11"},
    {"id": 87708, "date": "2017-11-07", "office": "Governor"},
    {"id": 87710, "date": "2017-11-07", "office": "Lieutenant Governor"},
    {"id": 87709, "date": "2017-11-07", "office": "Attorney General"},
    {"id": 87866, "date": "2017-11-07", "office": "House of Delegates", "district": "35"},
    {"id": 134846, "date": "2019-11-05", "office": "State Senate", "district": "34"},
    {"id": 134855, "date": "2019-11-05", "office": "House of Delegates", "district": "35"},
    {"id": 134055, "date": "2018-11-06", "office": "U.S. Senate"},
    {"id": 134109, "date": "2018-11-06", "office": "U.S. House", "district": "11"},
    {"id": 144567, "date": "2020-11-03", "office": "President"},
    {"id": 144564, "date": "2020-11-03", "office": "U.S. Senate"},
    {"id": 144619, "date": "2020-11-03", "office": "U.S. House", "district": "11"},
    {"id": 147466, "date": "2021-11-02", "office": "Governor"},
    {"id": 147467, "date": "2021-11-02", "office": "Lieutenant Governor"},
    {"id": 147393, "date": "2021-11-02", "office": "Attorney General"},
    {"id": 150494, "date": "2021-11-02", "office": "House of Delegates", "district": "35"},
    {"id": 164698, "date": "2023-11-07", "office": "State Senate", "district": "37"},
    {"id": 164711, "date": "2023-11-07", "office": "House of Delegates", "district": "12"},
    {"id": 156424, "date": "2022-11-08", "office": "U.S. House", "district": "11"},
    {"id": 164835, "date": "2023-11-07", "office": "Mayor, Town of Vienna", "town": True},
    {"id": 164836, "date": "2023-11-07", "office": "Town Council, Town of Vienna", "town": True},
    {"id": 161256, "date": "2024-11-05", "office": "President"},
    {"id": 161257, "date": "2024-11-05", "office": "U.S. Senate"},
    {"id": 161738, "date": "2024-11-05", "office": "U.S. House", "district": "11"},
    {"id": 164996, "date": "2025-11-04", "office": "Governor"},
    {"id": 164997, "date": "2025-11-04", "office": "Lieutenant Governor"},
    {"id": 164998, "date": "2025-11-04", "office": "Attorney General"},
    {"id": 165204, "date": "2025-11-04", "office": "House of Delegates", "district": "12"},
    {"id": 165205, "date": "2025-11-04", "office": "Mayor, Town of Vienna", "town": True},
    {"id": 165206, "date": "2025-11-04", "office": "Town Council, Town of Vienna", "town": True},
]


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

def main():
    DATA.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    for contest in CONTESTS:
        cid = contest["id"]
        out = DATA / f"contest_{cid}.csv"
        if out.exists():
            print(f"cached  contest_{cid}.csv")
            continue
        url = f"{BASE}/{cid}_table.csv?split_party=false"
        print(f"GET     {url}")
        resp = get_retry(session, url, timeout=60)
        resp.raise_for_status()
        out.write_bytes(resp.content)
        time.sleep(SLEEP_SECONDS)

    (DATA / "elections_meta.json").write_text(
        json.dumps(
            {
                "source": "Virginia Department of Elections historical database "
                          "(historical.elections.virginia.gov, data API at va2.elstats.civera.com)",
                "contests": CONTESTS,
                "fetched_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"done    {len(CONTESTS)} contests")


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException as e:
        print(f"error   {e}", file=sys.stderr)
        sys.exit(1)
