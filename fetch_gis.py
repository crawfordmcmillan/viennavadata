"""fetch_gis.py — pull boundary geometry for the precinct map.

Raw GeoJSON sources, cached verbatim in data/gis/:
- Fairfax County voting precincts (county open data portal, all ~266
  precincts; the build filters to the four Vienna precincts by PREC_IDENT)
- Fairfax County polling places (points; filtered the same way at build)
- Town of Vienna corporate boundary (Census TIGERweb, incorporated place
  GEOID 5181072)
- Roads around the town (Census TIGERweb secondary + local road layers,
  clipped server-side to the map's bounding box)
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

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

DATA = Path(__file__).parent / "data" / "gis"

# Bounding box: Vienna town boundary plus a small margin.
BBOX = "-77.28876%2C38.87549%2C-77.23699%2C38.92483"
ROADS_QUERY = (
    f"query?geometry={BBOX}&geometryType=esriGeometryEnvelope&inSR=4326"
    "&spatialRel=esriSpatialRelIntersects&outFields=NAME%2CBASENAME%2CMTFCC"
    "&outSR=4326&f=geojson&where=1%3D1"
)

SOURCES = {
    "fairfax_precincts.geojson": (
        "https://data-fairfaxcountygis.opendata.arcgis.com/api/download/v1/"
        "items/7727de78b3984f4daa1ff0960d0da8cb/geojson?layers=1"
    ),
    "fairfax_polling_places.geojson": (
        "https://data-fairfaxcountygis.opendata.arcgis.com/api/download/v1/"
        "items/9c08b886c6fa44a0922ea1e1f89ad907/geojson?layers=0"
    ),
    "vienna_boundary.geojson": (
        "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
        "Places_CouSub_ConCity_SubMCD/MapServer/4/query"
        "?where=GEOID%3D%275181072%27&outFields=GEOID,NAME&outSR=4326&f=geojson"
    ),
    "roads_secondary.geojson": (
        "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
        f"Transportation/MapServer/6/{ROADS_QUERY}"
    ),
    "roads_local.geojson": (
        "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
        f"Transportation/MapServer/8/{ROADS_QUERY}"
    ),
}


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    for name, url in SOURCES.items():
        out = DATA / name
        if out.exists():
            print(f"cached  {name}")
            continue
        print(f"GET     {url}")
        resp = get_retry(session, url, timeout=120)
        resp.raise_for_status()
        # Strip a UTF-8 BOM if present so downstream json.load is simple.
        out.write_bytes(resp.content.lstrip(b"\xef\xbb\xbf"))
    (DATA / "gis_meta.json").write_text(
        json.dumps(
            {
                "sources": SOURCES,
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
    except requests.RequestException as e:
        print(f"error   {e}", file=sys.stderr)
        sys.exit(1)
