"""fetch_houses.py — pull property sales data for the Town of Vienna.

Two raw sources from Fairfax County's public ArcGIS services, cached as
verbatim JSON pages in data/houses/:
- Address points where JURISDICTION = 'TOWN OF VIENNA' (the county's own
  flag for addresses inside the town), which carry the parcel PIN
- The Department of Tax Administration's real estate sales table, filtered
  to sales in the last 12 months county-wide; the build joins the two on
  parcel ID

Delete data/houses/ to refetch a fresh 12-month window.
"""
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

DATA = Path(__file__).parent / "data" / "houses"
SLEEP_SECONDS = 0.1
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
CUTOFF = (date.today() - timedelta(days=365)).isoformat()

ADDRESS_URL = ("https://services1.arcgis.com/ioennV6PpG5Xodq0/ArcGIS/rest/"
               "services/Address_Points/FeatureServer/0/query")
SALES_URL = ("https://services1.arcgis.com/ioennV6PpG5Xodq0/ArcGIS/rest/"
             "services/OpenData_A5/FeatureServer/1/query")
PARCEL_URL = ("https://services1.arcgis.com/ioennV6PpG5Xodq0/ArcGIS/rest/"
              "services/OpenData_A6/FeatureServer/1/query")


def fetch_pages(session, name, url, params, page_size):
    offset = 0
    while True:
        out = DATA / f"{name}_{offset}.json"
        if out.exists():
            print(f"cached  {out.name}")
            data = json.loads(out.read_text(encoding="utf-8"))
        else:
            page = {**params, "resultOffset": offset, "resultRecordCount": page_size,
                    "orderByFields": "OBJECTID", "f": "json"}
            print(f"GET     {url} offset={offset}")
            resp = get_retry(session, url, params=page, timeout=120)
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                raise RuntimeError(data["error"])
            out.write_text(resp.text, encoding="utf-8")
            time.sleep(SLEEP_SECONDS)
        n = len(data.get("features", []))
        if n < page_size:
            break
        offset += page_size


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    session = requests.Session()

    fetch_pages(session, "addresses", ADDRESS_URL, {
        "where": "JURISDICTION = 'TOWN OF VIENNA'",
        "outFields": "PARCEL_PIN,ADDRESS_1,CITY,ZIP,UNIT_TYPE,UNIT_NUMBER",
        "returnGeometry": "true",
        "outSR": 4326,
    }, 2000)

    fetch_pages(session, "sales", SALES_URL, {
        "where": f"SALEDT >= DATE '{CUTOFF}'",
        "outFields": "PARID,SALEDT,PRICE,SALEVAL_DESC,TAXYR",
        "returnGeometry": "false",
    }, 1000)

    # Parcel records (land-use description) for the Vienna parcels that sold.
    vienna_pins = set()
    for f in DATA.glob("addresses_*.json"):
        for feat in json.loads(f.read_text(encoding="utf-8"))["features"]:
            pin = (feat["attributes"].get("PARCEL_PIN") or "").strip()
            if pin:
                vienna_pins.add(pin)
    sold_pins = sorted({
        (feat["attributes"].get("PARID") or "").strip()
        for f in DATA.glob("sales_*.json")
        for feat in json.loads(f.read_text(encoding="utf-8"))["features"]
    } & vienna_pins)
    for i in range(0, len(sold_pins), 50):
        out = DATA / f"parcels_{i}.json"
        if out.exists():
            print(f"cached  {out.name}")
            continue
        chunk = "','".join(p.replace("'", "''") for p in sold_pins[i:i + 50])
        print(f"GET     {PARCEL_URL} chunk {i}")
        resp = get_retry(session, PARCEL_URL, params={
            "where": f"PARID IN ('{chunk}')",
            "outFields": "PARID,TAXYR,LUC_DESC,LIVUNIT",
            "returnGeometry": "false", "f": "json",
        }, timeout=120)
        resp.raise_for_status()
        if "error" in resp.json():
            raise RuntimeError(resp.json()["error"])
        out.write_text(resp.text, encoding="utf-8")
        time.sleep(SLEEP_SECONDS)

    (DATA / "houses_meta.json").write_text(
        json.dumps(
            {
                "source": "Fairfax County Department of Tax Administration sales "
                          "table and county address points, via the county's "
                          "public ArcGIS services",
                "sales_since": CUTOFF,
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
