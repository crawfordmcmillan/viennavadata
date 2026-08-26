"""fetch_properties.py — pull the full sales history for Vienna parcels.

Reads the Town of Vienna parcel PINs from the cached address points
(fetch_houses.py must run first) and queries the county's sales table for
each parcel's complete history, in chunks of 50 PINs. Raw JSON pages are
cached in data/properties/. This feeds the property timeline pages, which
join sales, planning cases, and council items on addresses.
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

HOUSES = Path(__file__).parent / "data" / "houses"
DATA = Path(__file__).parent / "data" / "properties"
SALES_URL = ("https://services1.arcgis.com/ioennV6PpG5Xodq0/ArcGIS/rest/"
             "services/OpenData_A5/FeatureServer/1/query")
SLEEP_SECONDS = 0.15
CHUNK = 50
RETRIES = 4


def get_retry(session, url, **kwargs):
    """Public GIS servers stall occasionally; retry before giving up."""
    for attempt in range(RETRIES):
        try:
            resp = session.get(url, **kwargs)
            # A 5xx (e.g. 504 Gateway Timeout) is just as transient as a
            # dropped connection; retry it instead of failing on it.
            if resp.status_code < 500 or attempt == RETRIES - 1:
                return resp
            print(f"retry   attempt {attempt + 2} after HTTP {resp.status_code}")
        except requests.RequestException:
            if attempt == RETRIES - 1:
                raise
            print(f"retry   attempt {attempt + 2} after a failed request")
        time.sleep(20)


def main():
    pins = set()
    for f in HOUSES.glob("addresses_*.json"):
        for feat in json.loads(f.read_text(encoding="utf-8"))["features"]:
            pin = (feat["attributes"].get("PARCEL_PIN") or "").strip()
            if pin:
                pins.add(pin)
    if not pins:
        print("error   no cached addresses; run fetch_houses.py first", file=sys.stderr)
        sys.exit(1)
    pins = sorted(pins)
    print(f"        {len(pins)} Vienna parcels, {len(pins) // CHUNK + 1} chunks")

    DATA.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    for i in range(0, len(pins), CHUNK):
        out = DATA / f"history_{i}.json"
        if out.exists():
            print(f"cached  {out.name}")
            continue
        chunk = "','".join(p.replace("'", "''") for p in pins[i:i + CHUNK])
        print(f"GET     sales history chunk {i}")
        resp = get_retry(session, SALES_URL, params={
            "where": f"PARID IN ('{chunk}')",
            "outFields": "PARID,SALEDT,PRICE,SALEVAL_DESC,TAXYR",
            "returnGeometry": "false",
            "orderByFields": "OBJECTID",
            "resultRecordCount": 2000,
            "f": "json",
        }, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(data["error"])
        out.write_text(resp.text, encoding="utf-8")
        time.sleep(SLEEP_SECONDS)

    (DATA / "properties_meta.json").write_text(
        json.dumps(
            {
                "source": "Fairfax County Department of Tax Administration sales "
                          "table, complete history for Town of Vienna parcels",
                "fetched_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print("done")


if __name__ == "__main__":
    try:
        main()
    except (requests.RequestException, RuntimeError) as e:
        print(f"error   {e}", file=sys.stderr)
        sys.exit(1)
