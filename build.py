"""build.py — render static HTML from data/ into site/. No network calls.

Reads the raw JSON cached by fetch.py and writes five page types:
index, one page per council member, one per meeting, one per agenda item,
and one per topic (if categories.csv exists). VoteValueName is carried
verbatim everywhere; topic categories are this site's own unofficial layer,
kept in categories.csv and labeled as such.
"""
import csv
import json
import math
import re
import shutil
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).parent
DATA = ROOT / "data"
SITE = ROOT / "site"
TEMPLATES = ROOT / "templates"
CATEGORIES = ROOT / "categories.csv"
ALIASES = ROOT / "person_aliases.json"
LEGISTAR = "https://vienna-va.legistar.com"
REPO = "https://github.com/crawfordmcmillan/viennavadata"
BASE_URL = "https://viennavadata.org"
CNAME_DOMAIN = "viennavadata.org"


def load(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")


def pdf_url(url: str | None) -> str | None:
    # Older cached records carry http:// Granicus links; the host serves https.
    return url.replace("http://", "https://", 1) if url else None


def last_name(name: str) -> str:
    suffixes = {"jr", "sr", "ii", "iii", "iv"}
    parts = [p for p in name.replace(",", "").split() if p.rstrip(".").lower() not in suffixes]
    return parts[-1] if parts else name


def fmt_date(iso: str | None) -> str:
    return iso[:10] if iso else ""


ELECTIONS = ROOT / "data" / "elections"
OFFICE_ORDER = ["President", "U.S. Senate", "U.S. House", "Governor",
                "Lieutenant Governor", "Attorney General", "State Senate",
                "House of Delegates",
                "Mayor, Town of Vienna", "Town Council, Town of Vienna"]


def parse_contest(contest):
    """Parse one raw contest CSV into Vienna-precinct rows, verbatim values."""
    rows = list(csv.reader((ELECTIONS / f"contest_{contest['id']}.csv").open(encoding="utf-8-sig")))
    header, parties = rows[0], rows[1]
    cols, candidates = [], []
    for i in range(2, len(header)):
        name = header[i].strip()
        if not name or name.startswith("Total") or name in ("Undervotes", "Overvotes"):
            continue
        cols.append(i)
        party = parties[i].strip() if i < len(parties) else ""
        candidates.append({"name": name, "party": party})
    stripped = [h.strip() for h in header]
    total_idx = (stripped.index("Total Ballots Cast")
                 if "Total Ballots Cast" in stripped
                 else stripped.index("Total Votes Cast"))

    def num(r, i):
        s = (r[i] if i < len(r) else "").replace(",", "").strip()
        return int(s) if s else 0

    is_town = contest.get("town", False)
    # Council races let a voter choose several candidates, so share-of-votes
    # is the wrong denominator. Use share-of-ballots when the county reported
    # ballot counts; otherwise show no shares for that race.
    multi_seat = contest.get("office", "").startswith("Town Council")
    has_ballots = "Total Ballots Cast" in stripped
    in_scope = False
    precincts = []
    central_absentee = 0
    for r in rows[2:]:
        if len(r) < 2:
            continue
        kind, name = r[0], r[1]
        if kind in ("Locality", "Town", "Congressional District"):
            in_scope = ("Fairfax County" in name) or (kind == "Town" and name == "Vienna")
        elif kind == "Precinct" and in_scope:
            if "Vienna #" in name or (is_town and name == "Provisional"):
                precincts.append({
                    "name": name,
                    "votes": [num(r, i) for i in cols],
                    "total": num(r, total_idx),
                })
            elif "Absentee" in name:
                central_absentee += num(r, total_idx)
    totals = [sum(p["votes"][j] for p in precincts) for j in range(len(cols))]
    if multi_seat:
        # A real ballot count must be smaller than the votes cast in a
        # choose-several race; some county files just duplicate the votes
        # column under the ballots heading, which is unusable.
        ballots = sum(p["total"] for p in precincts)
        valid_ballots = has_ballots and 0 < ballots < sum(totals)
        pcts = [round(t / ballots * 100, 1) if valid_ballots else None
                for t in totals]
        for p in precincts:
            p["pcts"] = [round(v / p["total"] * 100, 1)
                         if valid_ballots and p["total"] else None
                         for v in p["votes"]]
    else:
        valid_ballots = False
        denom = sum(totals)
        pcts = [round(t / denom * 100, 1) if denom else None for t in totals]
        for p in precincts:
            p_denom = sum(p["votes"])
            p["pcts"] = [round(v / p_denom * 100, 1) if p_denom else None
                         for v in p["votes"]]
    return {
        **contest,
        "candidates": candidates,
        "precincts": precincts,
        "totals": totals,
        "pcts": pcts,
        "multi_seat": multi_seat,
        "share_basis": "ballots" if (multi_seat and valid_ballots) else None,
        "ballots": sum(p["total"] for p in precincts),
        "total_label": "ballots" if "Total Ballots Cast" in stripped else "total votes",
        "central_absentee": central_absentee,
        "source_url": f"https://historical.elections.virginia.gov/contest/{contest['id']}",
    }


GIS = ROOT / "data" / "gis"
MAP_PRECINCTS = {213: "Vienna #1", 214: "Vienna #2", 216: "Vienna #4", 218: "Vienna #6"}
MAP_FILLS = {213: "#ead9b7", 214: "#cfd8c2", 216: "#e5c6b2", 218: "#c9d3da"}
# Local roads worth drawing for orientation (TIGER BASENAME values); secondary
# roads (Rt 123/Maple, Nutley, Chain Bridge, Leesburg Pike) are all drawn.
MAP_LOCAL_ROADS = {"Church", "Beulah", "Park", "Courthouse", "Lawyers",
                   "Cedar", "Old Courthouse", "Follin", "Glyndon", "Center",
                   "Washington and Old Dominion"}
# (name prefix to match in the data, label shown on the map, fraction
# along the drawn line where the label sits; None = point nearest map center).
# Labels rotate to follow the road's bearing at that point.
MAP_ROAD_LABELS = [("Maple Ave", "Maple Ave", 0.5),
                   ("Nutley St", "Nutley St", 0.5),
                   ("Church St", "Church St", 0.5),
                   ("Beulah Rd", "Beulah Rd", 0.5),
                   ("Lawyers Rd", "Lawyers Rd", 0.5),
                   ("Courthouse Rd", "Courthouse Rd", 0.5),
                   ("Chain Bridge Rd", "Chain Bridge Rd", 0.35),
                   ("Park St", "Park St", 0.5),
                   ("Center St", "Center St", 0.3),
                   ("Cedar Ln", "Cedar Ln", 0.5),
                   ("Old Courthouse Rd", "Old Courthouse Rd", 0.2),
                   ("Follin Ln", "Follin Ln", 0.5),
                   ("Glyndon St", "Glyndon St", 0.5),
                   ("Washington and Old Dominion", "W&amp;OD Trail", 0.12)]


def build_precinct_map(crashes=None, include_polling=True):
    """Render the four Vienna precincts + town boundary as a static inline SVG.

    With crashes, adds one dot per crash (sized and colored by severity, with
    data attributes for client-side year/severity filtering) and skips the
    polling-place markers to keep the map readable.
    """
    precincts_file = GIS / "fairfax_precincts.geojson"
    boundary_file = GIS / "vienna_boundary.geojson"
    if not (precincts_file.exists() and boundary_file.exists()):
        return None

    precincts = {}
    polling = []
    for f in json.loads(precincts_file.read_text(encoding="utf-8"))["features"]:
        ident = f["properties"].get("PREC_IDENT")
        if ident in MAP_PRECINCTS:
            geom = f["geometry"]
            rings = geom["coordinates"] if geom["type"] == "Polygon" else \
                [r for poly in geom["coordinates"] for r in poly]
            precincts[ident] = rings
            p = f["properties"]
            polling.append({
                "name": MAP_PRECINCTS[ident],
                "place": p.get("POLLING_PLACE"),
                "address": f"{p.get('ADDRESS')}, {p.get('CITY')}, VA {p.get('ZIP')}",
            })
    polling.sort(key=lambda p: p["name"])
    boundary = json.loads(boundary_file.read_text(encoding="utf-8"))["features"][0]["geometry"]["coordinates"]

    all_pts = [pt for rings in list(precincts.values()) + [boundary] for ring in rings for pt in ring]
    lons = [p[0] for p in all_pts]
    lats = [p[1] for p in all_pts]
    mid_lat = (min(lats) + max(lats)) / 2
    kx = math.cos(math.radians(mid_lat))
    span_x = (max(lons) - min(lons)) * kx
    span_y = max(lats) - min(lats)
    width, pad = 760.0, 16.0
    scale = (width - 2 * pad) / span_x
    height = span_y * scale + 2 * pad

    def xy(pt):
        x = (pt[0] - min(lons)) * kx * scale + pad
        y = (max(lats) - pt[1]) * scale + pad
        return x, y

    def path(rings):
        parts = []
        for ring in rings:
            parts.append("M" + "L".join(f"{x:.1f} {y:.1f}" for x, y in (xy(p) for p in ring)) + "Z")
        return "".join(parts)

    def interior_point(ring):
        """Approximate the point deepest inside the ring (grid-sampled), so
        labels sit centrally even in concave shapes where the centroid drifts
        to an edge."""
        pts = [xy(p) for p in ring]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        step = 26
        best, best_d = pts[0], -1.0
        for i in range(1, step):
            for j in range(1, step):
                px = min(xs) + (max(xs) - min(xs)) * i / step
                py = min(ys) + (max(ys) - min(ys)) * j / step
                if not point_in_ring(px, py, pts):
                    continue
                d = min((px - vx) ** 2 + (py - vy) ** 2 for vx, vy in pts)
                if d > best_d:
                    best, best_d = (px, py), d
        return best

    def road_lines(feature):
        geom = feature["geometry"]
        return geom["coordinates"] if geom["type"] == "MultiLineString" else [geom["coordinates"]]

    def line_path(lines):
        return "".join(
            "M" + "L".join(f"{x:.1f} {y:.1f}" for x, y in (xy(p) for p in line))
            for line in lines
        )

    secondary = json.loads((GIS / "roads_secondary.geojson").read_text(encoding="utf-8"))["features"]
    local = [
        f for f in json.loads((GIS / "roads_local.geojson").read_text(encoding="utf-8"))["features"]
        if f["properties"].get("BASENAME") in MAP_LOCAL_ROADS
    ]

    svg = [f'<svg viewBox="0 0 {width:.0f} {height:.0f}" role="img" '
           f'aria-label="Map of the four Vienna voting precincts, the town boundary, '
           f'major roads, and polling places" xmlns="http://www.w3.org/2000/svg">']
    for ident, rings in sorted(precincts.items()):
        svg.append(f'<path d="{path(rings)}" fill="{MAP_FILLS[ident]}" '
                   f'stroke="#211d13" stroke-width="1.2" stroke-linejoin="round"/>')
    for f in local:
        if "Old Dominion" in (f["properties"].get("NAME") or ""):
            svg.append(f'<path d="{line_path(road_lines(f))}" fill="none" stroke="#7d8a6a" '
                       f'stroke-width="2.2" stroke-dasharray="7 4" stroke-linecap="round"/>')
        else:
            svg.append(f'<path d="{line_path(road_lines(f))}" fill="none" stroke="#aca38f" '
                       f'stroke-width="1.4" stroke-linecap="round"/>')
    for f in secondary:
        svg.append(f'<path d="{line_path(road_lines(f))}" fill="none" stroke="#9b917c" '
                   f'stroke-width="2.4" stroke-linecap="round"/>')
    svg.append(f'<path d="{path(boundary)}" fill="none" stroke="#9c2f1d" '
               f'stroke-width="3" stroke-linejoin="round"/>')
    for ident, rings in sorted(precincts.items()):
        cx, cy = interior_point(max(rings, key=len))
        nx, ny = {216: (-60, -50)}.get(ident, (0, 0))
        cx, cy = cx + nx, cy + ny
        svg.append(f'<text x="{cx:.0f}" y="{cy:.0f}" text-anchor="middle" '
                   f'font-family="Libre Franklin, Arial, sans-serif" font-weight="900" '
                   f'font-size="26" fill="#211d13">#{MAP_PRECINCTS[ident][-1]}</text>')

    # Road labels: set along each road at a tunable fraction of its drawn
    # line, rotated to the road's local bearing so names read like a map.
    def label_angle(pts, idx):
        a = pts[max(0, idx - 2)]
        b = pts[min(len(pts) - 1, idx + 2)]
        ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
        if ang > 90:
            ang -= 180
        if ang < -90:
            ang += 180
        return ang

    all_roads = secondary + local
    boundary_xy = [xy(p) for p in boundary[0]]
    for prefix, display, frac in MAP_ROAD_LABELS:
        candidates = [f for f in all_roads if (f["properties"].get("NAME") or "").startswith(prefix)]
        if not candidates:
            continue
        lines_xy = [[xy(p) for p in line] for f in candidates for line in road_lines(f)]
        if frac is None:
            # The W&OD trail mostly runs outside town; label it at whichever
            # of its points is nearest the map middle.
            line_pts, idx = min(
                ((lp, i) for lp in lines_xy for i in range(len(lp))),
                key=lambda t: (t[0][t[1]][0] - width / 2) ** 2
                              + (t[0][t[1]][1] - height / 2) ** 2)
        else:
            # Position along the stretch of road inside the town boundary,
            # since many of these roads run well beyond it.
            inside = [(lp, [i for i, q in enumerate(lp) if point_in_ring(q[0], q[1], boundary_xy)])
                      for lp in lines_xy]
            inside = [(lp, ix) for lp, ix in inside if ix]
            if inside:
                line_pts, ix = max(inside, key=lambda t: len(t[1]))
                idx = ix[round((len(ix) - 1) * frac)]
            else:
                line_pts = max(lines_xy, key=len)
                idx = round((len(line_pts) - 1) * frac)
        mx, my = line_pts[idx]
        ang = label_angle(line_pts, idx)
        mx = min(max(mx, 50), width - 50)
        my = min(max(my, 16), height - 10)
        color = "#5d6b4d" if "OD Trail" in display else "#6d675c"
        svg.append(f'<text x="{mx:.0f}" y="{my - 4:.0f}" text-anchor="middle" '
                   f'transform="rotate({ang:.0f} {mx:.0f} {my:.0f})" '
                   f'font-family="Libre Franklin, Arial, sans-serif" font-weight="600" font-size="10.5" '
                   f'fill="{color}" stroke="#f5eedd" stroke-width="2.5" '
                   f'paint-order="stroke" letter-spacing="0.03em">{display}</text>')

    if crashes:
        for c in crashes:
            r, fill = SEVERITY_DOTS.get(c["sev"], SEVERITY_DOTS["O"])
            x, y = xy((c["x"], c["y"]))
            label = f"{c['date']} · {SEVERITY_LABELS.get(c['sev'], c['sev'])}"
            if c["type"]:
                label += f" · {c['type']}"
            svg.append(
                f'<circle class="crash-dot" data-year="{c["year"]}" '
                f'data-sev="{c["sev"]}" cx="{x:.1f}" cy="{y:.1f}" r="{r}" '
                f'fill="{fill}" fill-opacity="0.75" stroke="#f5eedd" stroke-width="0.6">'
                f'<title>{label}</title></circle>')

    # Polling place markers.
    pp_file = GIS / "fairfax_polling_places.geojson"
    if include_polling and pp_file.exists():
        for f in json.loads(pp_file.read_text(encoding="utf-8"))["features"]:
            p = f["properties"]
            idents = {p.get("PREC_IDENT"), p.get("PREC_IDENT2")}
            if idents & set(MAP_PRECINCTS):
                x, y = xy(f["geometry"]["coordinates"])
                name = (p.get("DESCRIPTION") or "").title() \
                    .replace("Elementary School", "ES").replace("High School", "HS")
                # End-anchored labels drop below the dot so they don't collide
                # with the big precinct number sitting to their left.
                if x > width * 0.7:
                    anchor, tx, ty = "end", x - 2, y + 20
                else:
                    anchor, tx, ty = "start", x + 9, y + 4
                svg.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5.5" fill="#211d13" '
                           f'stroke="#f5eedd" stroke-width="2"/>')
                svg.append(f'<text x="{tx:.0f}" y="{ty:.0f}" text-anchor="{anchor}" '
                           f'font-family="Libre Franklin, Arial, sans-serif" font-weight="700" '
                           f'font-size="12" fill="#211d13" stroke="#f5eedd" stroke-width="3" '
                           f'paint-order="stroke">{name}</text>')

    svg.append("</svg>")
    return {"svg": "".join(svg), "polling": polling}


HOUSES = ROOT / "data" / "houses"


def point_in_ring(x, y, ring):
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


UNIT_RE = re.compile(r"\s+(CND|STE|APT|UNIT|BLDG|FL|RM|#)\s*\S*$")


def build_address_book(timeline_addrs=None):
    """All town addresses with their precinct, for the in-browser checker."""
    precincts_file = GIS / "fairfax_precincts.geojson"
    if not precincts_file.exists():
        return None
    rings = {}
    for f in json.loads(precincts_file.read_text(encoding="utf-8"))["features"]:
        ident = f["properties"].get("PREC_IDENT")
        if ident in MAP_PRECINCTS:
            geom = f["geometry"]
            rings[ident] = geom["coordinates"] if geom["type"] == "Polygon" else \
                [r for poly in geom["coordinates"] for r in poly]

    book = {}
    for f in sorted(HOUSES.glob("addresses_*.json")):
        for feat in json.loads(f.read_text(encoding="utf-8"))["features"]:
            address = (feat["attributes"].get("ADDRESS_1") or "").strip()
            base = UNIT_RE.sub("", address)
            while UNIT_RE.search(base):
                base = UNIT_RE.sub("", base)
            if not base or base in book:
                continue
            precinct = 0
            geom = feat.get("geometry")
            if geom:
                for ident, rs in rings.items():
                    if any(point_in_ring(geom["x"], geom["y"], r) for r in rs):
                        precinct = int(MAP_PRECINCTS[ident][-1])
                        break
            book[base] = precinct
    timeline_addrs = timeline_addrs or set()
    return sorted([a, p, 1 if a in timeline_addrs else 0] for a, p in book.items())


def load_houses():
    """Join county sales rows to Vienna addresses on parcel ID."""
    meta_file = HOUSES / "houses_meta.json"
    if not meta_file.exists():
        return None
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    addresses = {}
    for f in sorted(HOUSES.glob("addresses_*.json")):
        for feat in json.loads(f.read_text(encoding="utf-8"))["features"]:
            a = feat["attributes"]
            pin = (a.get("PARCEL_PIN") or "").strip()
            if pin and pin not in addresses:
                unit = (a.get("UNIT_NUMBER") or "").strip()
                address = (a.get("ADDRESS_1") or "").strip()
                if unit and unit not in address:
                    address = f"{address} #{unit}"
                addresses[pin] = {"address": address, "zip": a.get("ZIP")}

    land_use = {}
    for f in sorted(HOUSES.glob("parcels_*.json")):
        for feat in json.loads(f.read_text(encoding="utf-8"))["features"]:
            a = feat["attributes"]
            pin = (a.get("PARID") or "").strip()
            year = a.get("TAXYR") or 0
            if pin not in land_use or year > land_use[pin][1]:
                land_use[pin] = ((a.get("LUC_DESC") or "").strip(), year)

    sales = []
    seen = set()
    no_consideration = 0
    for f in sorted(HOUSES.glob("sales_*.json")):
        for feat in json.loads(f.read_text(encoding="utf-8"))["features"]:
            s = feat["attributes"]
            pin = (s.get("PARID") or "").strip()
            if pin not in addresses:
                continue
            price = s.get("PRICE") or 0
            desc = (s.get("SALEVAL_DESC") or "").strip()
            # The county lists a sale once per tax year; keep one row.
            key = (pin, s.get("SALEDT"), price)
            if key in seen:
                continue
            seen.add(key)
            if price <= 0:
                no_consideration += 1
                continue
            sales.append({
                "date": datetime.fromtimestamp(s["SALEDT"] / 1000, tz=timezone.utc).date().isoformat(),
                "address": addresses[pin]["address"],
                "zip": addresses[pin]["zip"],
                "price": price,
                "classification": desc,
                "type": land_use.get(pin, ("", 0))[0],
            })
    sales.sort(key=lambda s: (s["date"], s["address"]), reverse=True)
    valid_prices = sorted(s["price"] for s in sales
                          if s["classification"] == "Valid and verified sale")
    median_valid = None
    if valid_prices:
        mid = len(valid_prices) // 2
        median_valid = (valid_prices[mid] if len(valid_prices) % 2
                        else (valid_prices[mid - 1] + valid_prices[mid]) // 2)
    return {
        "meta": meta,
        "sales": sales,
        "types": sorted({s["type"] for s in sales if s["type"]}),
        "n_valid": len(valid_prices),
        "median_valid": median_valid,
        "no_consideration": no_consideration,
    }


BOARDS = ROOT / "data" / "boards"


def load_boards():
    """Cases that came before the town's planning and land-use boards."""
    meta_file = BOARDS / "boards_meta.json"
    if not meta_file.exists():
        return None
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    events = {}
    for f in sorted(BOARDS.glob("events_*.json")):
        for e in json.loads(f.read_text(encoding="utf-8")):
            events[e["EventId"]] = e
    cases = []
    for f in sorted(BOARDS.glob("eventitems_*.json")):
        for it in json.loads(f.read_text(encoding="utf-8")):
            if not it.get("EventItemMatterFile"):
                continue
            event = events.get(it.get("EventItemEventId"))
            if not event:
                continue
            cases.append({
                "date": event["EventDate"][:10],
                "body": event["EventBodyName"],
                "case": it["EventItemMatterFile"],
                "title": (it.get("EventItemTitle") or "").strip() or "(untitled)",
                "source_url": event["EventInSiteURL"],
                "minutes_pdf": pdf_url(event.get("EventMinutesFile")),
            })
    cases.sort(key=lambda c: (c["date"], c["case"]), reverse=True)
    return {
        "meta": meta,
        "cases": cases,
        "bodies": sorted({c["body"] for c in cases}),
        "n_meetings": len(events),
    }


CRASHES = ROOT / "data" / "crashes"
SEVERITY_LABELS = {"K": "Fatal", "A": "Severe injury", "B": "Minor injury",
                   "C": "Possible injury", "O": "Property damage only"}
SEVERITY_DOTS = {"K": (7.0, "#6e1a0e"), "A": (5.5, "#9c2f1d"),
                 "B": (4.0, "#e08a52"), "C": (4.0, "#e08a52"),
                 "O": (2.6, "#9b917c")}


def load_crashes():
    meta_file = CRASHES / "crashes_meta.json"
    if not meta_file.exists():
        return None
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    crashes = []
    for f in sorted(CRASHES.glob("crashes_*.json")):
        if f.name == "crashes_meta.json":
            continue
        for feat in json.loads(f.read_text(encoding="utf-8"))["features"]:
            a = feat["attributes"]
            geom = feat.get("geometry")
            if not geom:
                continue
            ctype = (a.get("COLLISION_TYPE") or "").split(". ", 1)[-1]
            crashes.append({
                "year": int(a.get("CRASH_YEAR") or 0),
                "date": (datetime.fromtimestamp(a["CRASH_DT"] / 1000, tz=timezone.utc)
                         .date().isoformat() if a.get("CRASH_DT") else ""),
                "sev": (a.get("CRASH_SEVERITY") or "O").strip() or "O",
                "type": ctype,
                "ped": a.get("PED_NONPED") == "Yes",
                "bike": a.get("BIKE_NONBIKE") == "Yes",
                "x": geom["x"], "y": geom["y"],
            })
    years = sorted({c["year"] for c in crashes})
    by_year = {}
    for y in years:
        row = Counter(c["sev"] for c in crashes if c["year"] == y)
        by_year[y] = {s: row.get(s, 0) for s in "KABCO"}
    n_injury = sum(1 for c in crashes if c["sev"] in "KABC")
    current_year = max(years)
    default_year = current_year - 1 if len(years) > 1 else current_year
    return {
        "meta": meta,
        "crashes": crashes,
        "years": years,
        "by_year": by_year,
        "n_injury": n_injury,
        "n_ped": sum(1 for c in crashes if c["ped"]),
        "n_bike": sum(1 for c in crashes if c["bike"]),
        "current_year": current_year,
        "default_year": default_year,
        "severity_labels": SEVERITY_LABELS,
    }


POPULATION = ROOT / "data" / "population"


def load_population():
    meta_file = POPULATION / "population_meta.json"
    if not meta_file.exists():
        return None
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    latest = meta.get("latest_acs")
    if not latest:
        return None

    def rows(name):
        f = POPULATION / f"{name}.json"
        if not f.exists():
            return None
        data = json.loads(f.read_text(encoding="utf-8"))["response"]["data"]
        return dict(zip(data[0], data[1]))

    def num(d, k):
        v = str((d or {}).get(k) or "").replace(",", "").rstrip("+")
        try:
            return int(float(v))
        except ValueError:
            return 0

    decennial = []
    for year, name, var in [(2000, "dec_2000", "P001001"),
                            (2010, "dec_2010", "P001001"),
                            (2020, "dec_2020", "P1_001N")]:
        d = rows(name)
        if d:
            decennial.append({"year": year, "pop": num(d, var)})

    series = []
    for year in range(2010, latest + 1):
        merged = {}
        for table in ["B01003", "B01002", "B25003", "B25077", "B25064"]:
            merged.update(rows(f"acs_{table}_{year}") or {})
        if not merged.get("B01003_001E"):
            continue
        series.append({
            "year": year,
            "pop": num(merged, "B01003_001E"),
            "median_age": float(str(merged.get("B01002_001E") or 0).replace(",", "")),
            "owner": num(merged, "B25003_002E"),
            "renter": num(merged, "B25003_003E"),
            "home_value": num(merged, "B25077_001E"),
            "rent": num(merged, "B25064_001E"),
        })

    detail = {}
    for table in ["B01002", "B11001", "B08301", "B25003"]:
        detail.update(rows(f"acs_{table}_{latest}") or {})
    age = rows(f"acs_B01001_{latest}")
    brackets = None
    if age:
        def s(ids):
            return sum(num(age, f"B01001_{i:03d}E") for i in ids)
        brackets = [
            ("Under 18", s(range(3, 7)) + s(range(27, 31))),
            ("18 to 34", s(range(7, 13)) + s(range(31, 37))),
            ("35 to 49", s(range(13, 16)) + s(range(37, 40))),
            ("50 to 64", s(range(16, 20)) + s(range(40, 44))),
            ("65 and over", s(range(20, 26)) + s(range(44, 50))),
        ]
    households = commute = None
    if detail:
        households = [
            ("All households", num(detail, "B11001_001E")),
            ("Family households", num(detail, "B11001_002E")),
            ("Married-couple families", num(detail, "B11001_003E")),
            ("Nonfamily households", num(detail, "B11001_007E")),
            ("Living alone", num(detail, "B11001_008E")),
        ]
        main_modes = ["B08301_003E", "B08301_004E", "B08301_010E",
                      "B08301_018E", "B08301_019E", "B08301_021E"]
        other = num(detail, "B08301_001E") - sum(num(detail, m) for m in main_modes)
        commute = [
            ("Workers 16 and over", num(detail, "B08301_001E")),
            ("Drove alone", num(detail, "B08301_003E")),
            ("Carpooled", num(detail, "B08301_004E")),
            ("Public transportation", num(detail, "B08301_010E")),
            ("Bicycle", num(detail, "B08301_018E")),
            ("Walked", num(detail, "B08301_019E")),
            ("Worked from home", num(detail, "B08301_021E")),
            ("Other means", other),
        ]
    year_built = education = travel = median_built = None
    he = {}
    for table in ["B25034", "B25035", "B15003", "B08303"]:
        he.update(rows(f"acs_{table}_{latest}") or {})
    if he:
        def hnum(k):
            return num(he, k)
        median_built = num(he, "B25035_001E") or None
        built_labels = [("Built 2020 or later", [2]), ("2010s", [3]), ("2000s", [4]),
                        ("1990s", [5]), ("1980s", [6]), ("1970s", [7]), ("1960s", [8]),
                        ("1950s", [9]), ("1940s", [10]), ("1939 or earlier", [11])]
        year_built = [(label, sum(hnum(f"B25034_{i:03d}E") for i in ids))
                      for label, ids in built_labels]
        edu_groups = [("Less than high school", range(2, 17)),
                      ("High school graduate", range(17, 19)),
                      ("Some college or associate degree", range(19, 22)),
                      ("Bachelor's degree", range(22, 23)),
                      ("Graduate or professional degree", range(23, 26))]
        education = [(label, sum(hnum(f"B15003_{i:03d}E") for i in ids))
                     for label, ids in edu_groups]
        travel_groups = [("Under 15 minutes", range(2, 5)),
                         ("15 to 29 minutes", range(5, 8)),
                         ("30 to 44 minutes", range(8, 11)),
                         ("45 to 59 minutes", range(11, 12)),
                         ("An hour or more", range(12, 14))]
        travel = [(label, sum(hnum(f"B08303_{i:03d}E") for i in ids))
                  for label, ids in travel_groups]
    return {
        "meta": meta,
        "latest": latest,
        "decennial": decennial,
        "series": series,
        "brackets": brackets,
        "households": households,
        "commute": commute,
        "year_built": year_built,
        "median_built": median_built,
        "education": education,
        "travel": travel,
        "latest_row": series[-1] if series else None,
    }


CHART_ACCENT = "#9c2f1d"
CHART_INK = "#211d13"
CHART_MUTED = "#6d6553"
CHART_RULE = "#d5cab2"
CHART_HALO = "#f5eedd"
CHART_FONT = "Libre Franklin, Arial, sans-serif"


def _chart_ticks(lo, hi):
    """Three round tick values spanning the padded domain."""
    span = hi - lo
    step = 10 ** math.floor(math.log10(span / 2)) if span > 0 else 1
    for mult in (5, 2.5, 2, 1):
        if span / (step * mult) >= 2:
            step *= mult
            break
    t0 = math.ceil(lo / step) * step
    ticks = []
    t = t0
    while t <= hi and len(ticks) < 4:
        ticks.append(t)
        t += step
    return ticks


def svg_line_chart(points, fmt, w=740, h=220, aria=""):
    """Single-series line: 2px stroke, recessive grid, first/last direct labels,
    native-title hover targets on every point."""
    pad_l, pad_r, pad_t, pad_b = 58, 16, 14, 26
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    lo = min(ys) - (max(ys) - min(ys) or max(ys) * 0.1) * 0.08
    hi = max(ys) + (max(ys) - min(ys) or max(ys) * 0.1) * 0.08
    px = lambda x: pad_l + (x - min(xs)) / (max(xs) - min(xs) or 1) * (w - pad_l - pad_r)
    py = lambda y: pad_t + (hi - y) / (hi - lo) * (h - pad_t - pad_b)
    s = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{aria}" '
         f'xmlns="http://www.w3.org/2000/svg" font-family="{CHART_FONT}">']
    for t in _chart_ticks(lo, hi):
        y = py(t)
        s.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{w - pad_r}" y2="{y:.1f}" '
                 f'stroke="{CHART_RULE}" stroke-width="1"/>')
        s.append(f'<text x="{pad_l - 8}" y="{y + 3.5:.1f}" text-anchor="end" '
                 f'font-size="11" fill="{CHART_MUTED}">{fmt(t)}</text>')
    label_every = max(1, len(xs) // 7)
    for i, x in enumerate(xs):
        if i % label_every == 0 or i == len(xs) - 1:
            s.append(f'<text x="{px(x):.1f}" y="{h - 8}" text-anchor="middle" '
                     f'font-size="11" fill="{CHART_MUTED}">{x}</text>')
    path = "M" + "L".join(f"{px(x):.1f} {py(y):.1f}" for x, y in points)
    s.append(f'<path d="{path}" fill="none" stroke="{CHART_ACCENT}" '
             f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
    for i, (x, y) in enumerate(points):
        s.append(f'<circle cx="{px(x):.1f}" cy="{py(y):.1f}" r="3" fill="{CHART_ACCENT}"/>')
        s.append(f'<circle cx="{px(x):.1f}" cy="{py(y):.1f}" r="9" fill="transparent">'
                 f'<title>{x}: {fmt(y)}</title></circle>')
        if i in (0, len(points) - 1):
            anchor = "start" if i == 0 else "end"
            s.append(f'<text x="{px(x):.1f}" y="{py(y) - 9:.1f}" text-anchor="{anchor}" '
                     f'font-size="12" font-weight="700" fill="{CHART_INK}" '
                     f'stroke="{CHART_HALO}" stroke-width="3" paint-order="stroke">{fmt(y)}</text>')
    s.append("</svg>")
    return "".join(s)


def svg_bar_chart(labels, values, fmt, w=740, h=220, partial_idx=None, aria=""):
    """Single-series bars: zero baseline, 2px gaps, rounded data-ends,
    native-title hover per bar; a partial period renders lighter."""
    pad_l, pad_r, pad_t, pad_b = 48, 16, 14, 26
    hi = max(values) * 1.08
    plot_w = w - pad_l - pad_r
    plot_h = h - pad_t - pad_b
    n = len(values)
    gap = max(2, plot_w * 0.012)
    bw = (plot_w - gap * (n - 1)) / n
    py = lambda v: pad_t + (hi - v) / hi * plot_h
    s = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{aria}" '
         f'xmlns="http://www.w3.org/2000/svg" font-family="{CHART_FONT}">']
    for t in _chart_ticks(0, hi):
        y = py(t)
        s.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{w - pad_r}" y2="{y:.1f}" '
                 f'stroke="{CHART_RULE}" stroke-width="1"/>')
        s.append(f'<text x="{pad_l - 8}" y="{y + 3.5:.1f}" text-anchor="end" '
                 f'font-size="11" fill="{CHART_MUTED}">{fmt(t)}</text>')
    base = py(0)
    for i, (label, v) in enumerate(zip(labels, values)):
        x = pad_l + i * (bw + gap)
        top = py(v)
        r = min(4, bw / 2, (base - top) / 2)
        opacity = "0.45" if i == partial_idx else "1"
        title = f"{label}: {fmt(v)}" + (" (partial year)" if i == partial_idx else "")
        s.append(
            f'<path d="M{x:.1f} {base:.1f}L{x:.1f} {top + r:.1f}'
            f'Q{x:.1f} {top:.1f} {x + r:.1f} {top:.1f}L{x + bw - r:.1f} {top:.1f}'
            f'Q{x + bw:.1f} {top:.1f} {x + bw:.1f} {top + r:.1f}L{x + bw:.1f} {base:.1f}Z" '
            f'fill="{CHART_ACCENT}" fill-opacity="{opacity}"><title>{title}</title></path>')
        s.append(f'<text x="{x + bw / 2:.1f}" y="{h - 8}" text-anchor="middle" '
                 f'font-size="11" fill="{CHART_MUTED}">{label}</text>')
    s.append(f'<line x1="{pad_l}" y1="{base:.1f}" x2="{w - pad_r}" y2="{base:.1f}" '
             f'stroke="{CHART_INK}" stroke-width="1.5"/>')
    s.append("</svg>")
    return "".join(s)


PROPERTIES = ROOT / "data" / "properties"
ADDR_WORDS = {"STREET": "ST", "AVENUE": "AVE", "ROAD": "RD", "DRIVE": "DR",
              "LANE": "LN", "COURT": "CT", "PLACE": "PL", "CIRCLE": "CIR",
              "TERRACE": "TER", "BOULEVARD": "BLVD", "HIGHWAY": "HWY",
              "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE",
              "SOUTHWEST": "SW"}
ADDR_RE = re.compile(
    r"\b(\d{1,5})\s+((?:[A-Za-z\.']+\s+){0,4}?"
    r"(?:St|Street|Ave|Avenue|Rd|Road|Dr|Drive|Ln|Lane|Ct|Court|Pl|Place|"
    r"Cir|Circle|Ter|Terrace|Blvd|Boulevard|Hwy|Pike|Way))"
    r"\.?,?(?:\s+(NE|NW|SE|SW|N\.?|S\.?|E\.?|W\.?))?(?=[\s,.;:)\-]|$)",
    re.IGNORECASE)


def norm_address(s):
    words = re.sub(r"[.,#]", " ", s.upper()).split()
    return " ".join(ADDR_WORDS.get(w, w) for w in words)


def addresses_in(text, book, book_no_dir):
    """Find known town addresses mentioned in free text."""
    found = set()
    for m in ADDR_RE.finditer(text or ""):
        candidate = norm_address(" ".join(p for p in m.groups() if p))
        if candidate in book:
            found.add(candidate)
        elif candidate in book_no_dir:
            found.add(book_no_dir[candidate])
    return found


def load_properties(boards, council_items):
    meta_file = PROPERTIES / "properties_meta.json"
    if not meta_file.exists():
        return None
    meta = json.loads(meta_file.read_text(encoding="utf-8"))

    # PIN -> base address, plus lookup sets for text matching.
    pin_addr = {}
    for f in sorted(HOUSES.glob("addresses_*.json")):
        for feat in json.loads(f.read_text(encoding="utf-8"))["features"]:
            a = feat["attributes"]
            pin = (a.get("PARCEL_PIN") or "").strip()
            base = UNIT_RE.sub("", (a.get("ADDRESS_1") or "").strip())
            while UNIT_RE.search(base):
                base = UNIT_RE.sub("", base)
            if pin and base and pin not in pin_addr:
                pin_addr[pin] = base
    book = set(pin_addr.values())
    directional = re.compile(r"\s+(NE|NW|SE|SW|N|S|E|W)$")
    stripped = defaultdict(set)
    for addr in book:
        stripped[directional.sub("", addr)].add(addr)
    book_no_dir = {k: next(iter(v)) for k, v in stripped.items() if len(v) == 1}

    events = defaultdict(list)

    # Sales: full parcel history joined by PIN.
    seen_sales = set()
    for f in sorted(PROPERTIES.glob("history_*.json")):
        for feat in json.loads(f.read_text(encoding="utf-8"))["features"]:
            s = feat["attributes"]
            addr = pin_addr.get((s.get("PARID") or "").strip())
            price = s.get("PRICE") or 0
            if not addr or not s.get("SALEDT") or price <= 0:
                continue
            date = (datetime(1970, 1, 1, tzinfo=timezone.utc)
                    + timedelta(milliseconds=s["SALEDT"])).date().isoformat()
            key = (addr, date, price)
            if key in seen_sales:
                continue
            seen_sales.add(key)
            desc = (s.get("SALEVAL_DESC") or "").strip()
            events[addr].append({
                "date": date, "kind": "Sale",
                "text": f"Sold for ${price:,}" + (f" ({desc.lower()})" if desc else ""),
                "href": None, "href_label": None,
            })

    # Planning and land-use board cases, matched by address in the title.
    if boards:
        for c in boards["cases"]:
            found = addresses_in(c["title"], book, book_no_dir)
            c["prop_addrs"] = sorted(found)
            for addr in found:
                events[addr].append({
                    "date": c["date"], "kind": c["body"],
                    "text": f"{c['case']}: {c['title']}",
                    "href": c["minutes_pdf"] or c["source_url"],
                    "href_label": "minutes" if c["minutes_pdf"] else "agenda",
                })

    # Council agenda items, matched the same way, linking to our item pages.
    for item_id, entry in council_items.items():
        title = entry["item"].get("EventItemTitle") or ""
        for addr in addresses_in(title, book, book_no_dir):
            result = entry["item"].get("EventItemPassedFlagName")
            events[addr].append({
                "date": entry["meeting"]["date"], "kind": "Town Council",
                "text": title + (f" — {result}" if result else ""),
                "href": f"item/{item_id}.html", "href_label": "details",
                "internal": True,
            })

    properties = []
    for addr, evs in events.items():
        evs.sort(key=lambda e: e["date"], reverse=True)
        properties.append({
            "address": addr,
            "slug": slugify(addr),
            "events": evs,
            "n_cases": sum(1 for e in evs if e["kind"] != "Sale"),
            "n_sales": sum(1 for e in evs if e["kind"] == "Sale"),
            "last": evs[0]["date"],
        })
    properties.sort(key=lambda p: p["last"], reverse=True)
    return {"meta": meta, "properties": properties}


def load_elections():
    meta_file = ELECTIONS / "elections_meta.json"
    if not meta_file.exists():
        return None, []
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    contests = [parse_contest(c) for c in meta["contests"]]
    by_date = defaultdict(list)
    for c in contests:
        by_date[c["date"]].append(c)
    elections = []
    for date in sorted(by_date, reverse=True):
        group = sorted(by_date[date], key=lambda c: OFFICE_ORDER.index(c["office"]))
        elections.append({"date": date, "contests": group})
    return meta, elections


PHOTOS_DIR = ROOT / "assets" / "photos"

# One photo per page, warm-duotone treatment from photos_prepare.py.
# Captions are place + date, italic serif, per the newspaper design.
PHOTOS = {
    "index": {
        "file": "caboose.jpg",
        "alt": "A bright red caboose behind a bed of black-eyed Susans "
               "and coneflowers",
        "caption": "The caboose on the Town Green, a relic of the Washington "
                   "& Old Dominion line. August 2026.",
    },
    "council": {
        "file": "town-hall.jpg",
        "alt": "The wooden Vienna Town Hall sign, reading Settled 1754, "
               "Incorporated 1890, with the brick town hall behind",
        "caption": "Vienna Town Hall on Center Street, where the council "
                   "meets. August 2026.",
    },
    "elections": {
        "file": "community-center.jpg",
        "alt": "The Vienna Community Center entrance at dusk, beside a "
               "bronze statue of two children on a stack of books",
        "caption": "The Vienna Community Center on Cherry Street SE, the "
                   "polling place for precinct Vienna #2. August 2026.",
    },
    "houses": {
        "file": "knock-down.jpg",
        "alt": "A one-story brick house next to a much larger newly built "
               "two-story house on the same street",
        "caption": "A brick rambler and its newly built neighbor, side by "
                   "side. August 2026.",
    },
    "crashes": {
        "file": "maple-ave.jpg",
        "alt": "Cars queued at a light on Maple Avenue beneath a Vienna "
               "Babe Ruth baseball banner",
        "caption": "Evening traffic on Maple Avenue, the town's main road. "
                   "August 2026.",
    },
    "population": {
        "file": "w-od-trail.jpg",
        "alt": "The paved W&OD Trail beside its regional park sign, flanked "
               "by summer flowers",
        "caption": "The W&OD Trail where it passes through downtown Vienna. "
                   "August 2026.",
    },
}


def load_photos():
    """Copy processed photos into site/ and return per-page template
    context. Pages render without photos until the assets exist."""
    meta_file = PHOTOS_DIR / "photos_meta.json"
    if not meta_file.exists():
        return {}
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    (SITE / "photos").mkdir(exist_ok=True)
    photos = {}
    for page, p in PHOTOS.items():
        src = PHOTOS_DIR / p["file"]
        name = src.stem
        if not src.exists() or name not in meta:
            continue
        shutil.copy(src, SITE / "photos" / p["file"])
        photos[page] = {
            "src": f"photos/{p['file']}",
            "alt": p["alt"],
            "caption": p["caption"],
            "w": meta[name][0],
            "h": meta[name][1],
        }
    return photos


def write_exports(meetings, boards, aliases, today):
    """Machine-readable copies of the record: CSVs for spreadsheets, one
    markdown file sized for AI tools (NotebookLM and the like), and llms.txt
    describing the site. Linked from the About page only."""
    out = SITE / "data"
    out.mkdir(parents=True, exist_ok=True)

    def canon(vote):
        # Same duplicate-person merge the member pages apply.
        alias = aliases.get(vote["VotePersonId"])
        return alias["canonical_name"] if alias else vote["VotePersonName"].strip()

    def value(vote):
        return vote["VoteValueName"] or "(no value recorded)"

    with (out / "council-items.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["meeting_date", "item_id", "title", "topic_unofficial",
                    "vote_tally", "meeting_url"])
        for m in meetings:
            for e in m["items"]:
                w.writerow([m["date"], e["item"]["EventItemId"],
                            (e["item"].get("EventItemTitle") or "").strip(),
                            e["category"] or "", e["tally"],
                            f"{BASE_URL}/meeting/{m['slug']}.html"])

    with (out / "council-votes.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["meeting_date", "item_id", "item_title", "member", "vote"])
        for m in meetings:
            for e in m["items"]:
                title = (e["item"].get("EventItemTitle") or "").strip()
                for v in e["votes"]:
                    w.writerow([m["date"], e["item"]["EventItemId"], title,
                                canon(v), value(v)])

    if boards:
        with (out / "planning-cases.csv").open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["meeting_date", "body", "case", "title"])
            for c in boards["cases"]:
                w.writerow([c["date"], c["body"], c["case"], c["title"]])

    lines = [
        "# Town of Vienna, Virginia: the Town Council record",
        "",
        f"Compiled by Vienna VA Data ({BASE_URL}) from the town's public records "
        f"system (Legistar). Data as of {today}. Meetings are listed most recent "
        "first.",
        "Vote values are exactly as the town recorded them: Aye, Nay, Absent, "
        "Abstain. A few records have no vote value entered in the town's system.",
        "Topic labels are unofficial, added by the site to make browsing easier.",
        f"Each item has a page at {BASE_URL}/item/<item id>.html with links to "
        "the official meeting record.",
        "",
    ]
    for m in meetings:
        lines.append(f"## {m['event']['EventBodyName']}, {m['date']}")
        lines.append(f"Meeting page: {BASE_URL}/meeting/{m['slug']}.html")
        for e in m["items"]:
            title = (e["item"].get("EventItemTitle") or "").strip() or "(untitled)"
            topic = f" [topic: {e['category']}]" if e["category"] else ""
            lines.append(f"- Item {e['item']['EventItemId']}: {title}{topic}")
            if e["votes"]:
                roll = "; ".join(f"{canon(v)}: {value(v)}" for v in e["votes"])
                lines.append(f"  Result: {e['tally']}. Roll call: {roll}.")
        lines.append("")
    (out / "council-record.md").write_text("\n".join(lines) + "\n",
                                           encoding="utf-8")

    (SITE / "llms.txt").write_text(f"""# Vienna VA Data

> Public records about the Town of Vienna, Virginia, in one place: every
> recorded Town Council vote since October 2013, planning and land-use board
> dockets, precinct-level election results, property sales and per-property
> timelines, crash records, and population figures. Everything comes from
> official sources, links back to the originals, and refreshes weekly.

Vote values are shown exactly as recorded (Aye, Nay, Absent, Abstain) and are
never scored, ranked, or interpreted. Topic categories are the one unofficial
layer, and they are labeled as such wherever they appear.

## Pages

- [Council votes]({BASE_URL}/council.html): members, meetings, and every
  recorded vote since 2013, browsable by member, meeting, and topic
- [Elections]({BASE_URL}/elections.html): how Vienna's four precincts voted
  in every November election since 2013
- [Planning and zoning]({BASE_URL}/planning.html): the case dockets of the
  town's planning and land-use boards
- [Property timelines]({BASE_URL}/properties.html): sales history, planning
  cases, and council items joined per address
- [House prices]({BASE_URL}/house-prices.html): the last 12 months of
  recorded sales
- [Crashes]({BASE_URL}/crashes.html): VDOT-reported crashes in town since 2017
- [Population]({BASE_URL}/population.html): census counts and estimates
- [About]({BASE_URL}/about.html): sources, methods, and corrections

## Machine-readable data

- [council-record.md]({BASE_URL}/data/council-record.md): the full council
  record as one markdown file, suited to AI tools
- [council-items.csv]({BASE_URL}/data/council-items.csv): one row per agenda
  item
- [council-votes.csv]({BASE_URL}/data/council-votes.csv): one row per
  recorded vote
- [planning-cases.csv]({BASE_URL}/data/planning-cases.csv): one row per
  planning board case
- [Raw source files]({REPO}): the verbatim API responses the site is built
  from, in the public repository
""", encoding="utf-8")


def main():
    meta = load("fetch_meta.json")
    today = meta["fetched_at_utc"][:10]
    events = sorted(load("events.json"), key=lambda e: e["EventDate"], reverse=True)

    # Optional unofficial topic layer: categories.csv (id,date,title,category).
    category_of = {}
    if CATEGORIES.exists():
        with CATEGORIES.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                category_of[int(row["id"])] = row["category"]

    # Assemble meetings -> items -> votes, all verbatim from the cache.
    meetings = []
    items_by_id = {}
    for event in events:
        items = sorted(
            load(f"eventitems_{event['EventId']}.json"),
            key=lambda i: (i.get("EventItemMinutesSequence") or i.get("EventItemAgendaSequence") or 0),
        )
        date = event["EventDate"][:10]
        meeting = {
            "event": event,
            "date": date,
            "slug": date,
            "items": [],
            "minutes_pdf": pdf_url(event.get("EventMinutesFile")),
            "agenda_pdf": pdf_url(event.get("EventAgendaFile")),
        }
        for item in items:
            votes = load(f"votes_{item['EventItemId']}.json")
            tally = Counter(v["VoteValueName"] for v in votes)
            entry = {
                "item": item,
                "votes": votes,
                "tally": ", ".join(
                    f"{n} {value if value is not None else '(no value recorded)'}"
                    for value, n in tally.items()
                ),
                # Vienna's InSite site keys legislation pages by internal web IDs
                # the public API doesn't expose, so per-matter deep links can't be
                # built; the meeting record page (from the API verbatim) shows the
                # item with its roll call and always resolves.
                "source_url": event["EventInSiteURL"],
                "meeting": meeting,
                "category": category_of.get(item["EventItemId"]),
            }
            meeting["items"].append(entry)
            items_by_id[item["EventItemId"]] = entry
        meetings.append(meeting)

    # Two meetings on the same date would collide on /meeting/{date}.html.
    date_counts = Counter(m["date"] for m in meetings)
    for m in meetings:
        if date_counts[m["date"]] > 1:
            m["slug"] = f"{m['date']}-{m['event']['EventId']}"

    # Office terms and contact details, straight from the public record.
    persons = {p["PersonId"]: p for p in load("persons.json")}
    terms_by_person = defaultdict(list)
    for rec in load(f"officerecords_{meta['body_id']}.json"):
        terms_by_person[rec["OfficeRecordPersonId"]].append(rec)
    for terms in terms_by_person.values():
        terms.sort(key=lambda t: t["OfficeRecordStartDate"] or "")

    # Members: everyone who actually appears in a vote record, keyed by PersonId.
    # person_aliases.json merges Legistar's duplicate person records for the
    # same human into one page, transparently.
    aliases = {}
    if ALIASES.exists():
        raw_aliases = json.loads(ALIASES.read_text(encoding="utf-8"))
        aliases = {int(k): v for k, v in raw_aliases.items() if not k.startswith("_")}

    members = {}
    for meeting in meetings:
        for entry in meeting["items"]:
            for vote in entry["votes"]:
                pid = vote["VotePersonId"]
                alias = aliases.get(pid)
                canonical_pid = alias["canonical"] if alias else pid
                member = members.setdefault(
                    canonical_pid,
                    {
                        "person_id": canonical_pid,
                        "name": (alias["canonical_name"] if alias else vote["VotePersonName"].strip()),
                        "slug": None,
                        "votes": [],
                        "merged_from": set(),
                    },
                )
                if alias:
                    member["merged_from"].add(alias["recorded_as"])
                else:
                    member["name"] = member["name"] or vote["VotePersonName"].strip()
                member["votes"].append({"vote": vote, "entry": entry})

    for member in members.values():
        member["slug"] = slugify(member["name"])
        member["votes"].sort(
            key=lambda v: (
                v["entry"]["meeting"]["date"],
                v["entry"]["item"].get("EventItemMinutesSequence") or 0,
            ),
            reverse=True,
        )
        by_year = defaultdict(list)
        for v in member["votes"]:
            by_year[v["entry"]["meeting"]["date"][:4]].append(v)
        member["votes_by_year"] = sorted(by_year.items(), reverse=True)
        member["first_vote"] = member["votes"][-1]["entry"]["meeting"]["date"]
        member["last_vote"] = member["votes"][0]["entry"]["meeting"]["date"]

        terms = terms_by_person.get(member["person_id"], [])
        member["terms"] = [
            {
                "title": t["OfficeRecordTitle"] or "(title not recorded)",
                "start": fmt_date(t["OfficeRecordStartDate"]),
                "end": fmt_date(t["OfficeRecordEndDate"]),
            }
            for t in terms
        ]
        current = [t for t in member["terms"] if t["end"] >= today]
        member["current_title"] = current[-1]["title"] if current else None
        member["is_current"] = bool(current)
        person = persons.get(member["person_id"], {})
        member["email"] = person.get("PersonEmail") or None

    members = sorted(members.values(), key=lambda m: last_name(m["name"]))
    current_members = [m for m in members if m["is_current"]]
    former_members = [m for m in members if not m["is_current"]]

    # Topic index (unofficial layer).
    topics = defaultdict(list)
    for entry in items_by_id.values():
        if entry["category"]:
            topics[entry["category"]].append(entry)
    topic_pages = [
        {
            "name": name,
            "slug": slugify(name),
            "entries": sorted(entries, key=lambda e: e["meeting"]["date"], reverse=True),
            "voted": sum(1 for e in entries if e["votes"]),
        }
        for name, entries in sorted(topics.items())
    ]
    for tp in topic_pages:
        for entry in tp["entries"]:
            entry["category_slug"] = tp["slug"]

    n_votes = sum(len(e["votes"]) for m in meetings for e in m["items"])
    date_range = (meetings[-1]["date"], meetings[0]["date"]) if meetings else ("", "")
    by_year = defaultdict(list)
    for m in meetings:
        by_year[m["date"][:4]].append(m)
    meetings_by_year = sorted(by_year.items(), reverse=True)

    # Homepage lede: the most recent substantive recorded vote, verbatim —
    # the last non-procedural roll call of the newest meeting that has one.
    lede = None
    for meeting in meetings:  # newest first
        candidates = [
            e for e in meeting["items"]
            if e["votes"] and e["category"] not in ("Procedural", "Minutes")
            and (e["item"].get("EventItemTitle") or "").strip()
        ]
        if candidates:
            entry = candidates[-1]
            d = datetime.strptime(meeting["date"], "%Y-%m-%d")
            title = entry["item"]["EventItemTitle"].strip()
            if len(title) > 240:
                title = title[:237].rstrip() + "…"
            lede = {
                "date": f"{d:%B} {d.day}, {d.year}",
                "title": title,
                "tally": entry["tally"],
                "id": entry["item"]["EventItemId"],
                "slug": meeting["slug"],
            }
            break

    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    common = {
        "legistar": LEGISTAR,
        "repo": REPO,
        "fetched_at": today,
        "date_range": date_range,
    }

    sitemap_paths = []

    def render(template, out_path: Path, **ctx):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rel = out_path.relative_to(SITE).as_posix()
        sitemap_paths.append(rel)
        html = env.get_template(template).render(
            **common, canonical=f"{BASE_URL}/{rel}", **ctx
        )
        out_path.write_text(html, encoding="utf-8")

    if SITE.exists():
        shutil.rmtree(SITE)
    SITE.mkdir()
    shutil.copy(TEMPLATES / "style.css", SITE / "style.css")
    # Custom-domain marker for GitHub Pages; must survive the site/ wipe.
    (SITE / "CNAME").write_text(f"{CNAME_DOMAIN}\n", encoding="utf-8")
    (SITE / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\n\nSitemap: {BASE_URL}/sitemap.xml\n",
        encoding="utf-8",
    )

    photos = load_photos()
    elections_meta, elections = load_elections()
    precinct_map = build_precinct_map()
    render("about.html", SITE / "about.html", root="")
    if elections:
        render("elections.html", SITE / "elections.html", root="",
               elections=elections, elections_meta=elections_meta,
               precinct_map=precinct_map, photo=photos.get("elections"))
    stats = {"votes": n_votes, "meetings": len(meetings), "members": len(members)}
    election_stats = {
        "contests": sum(len(e["contests"]) for e in elections),
        "elections": len(elections),
    }
    houses = load_houses()
    boards = load_boards()
    write_exports(meetings, boards, aliases, today)
    props = load_properties(boards, items_by_id)
    if props:
        # Cross-link sales rows and board cases to their property pages.
        slug_of = {p["address"]: p["slug"] for p in props["properties"]}
        if houses:
            for s in houses["sales"]:
                s["prop_slug"] = slug_of.get(s["address"].split(" #")[0])
        if boards:
            for c in boards["cases"]:
                c["props"] = [(a, slug_of[a]) for a in c.get("prop_addrs", [])
                              if a in slug_of][:2]
        render("properties.html", SITE / "properties.html", root="", props=props)
        for prop in props["properties"]:
            render("property.html", SITE / "property" / f"{prop['slug']}.html",
                   root="../", prop=prop)
    if boards:
        render("planning.html", SITE / "planning.html", root="", boards=boards)
    crashes = load_crashes()
    if crashes:
        crashes["map_svg"] = build_precinct_map(
            crashes=crashes["crashes"], include_polling=False)["svg"]
        year_totals = [sum(crashes["by_year"][y].values()) for y in crashes["years"]]
        crashes["chart_years"] = svg_bar_chart(
            crashes["years"], year_totals, lambda v: f"{v:,.0f}",
            partial_idx=len(crashes["years"]) - 1,
            aria="Reportable crashes per year")
        render("crashes.html", SITE / "crashes.html", root="", crashes=crashes,
               photo=photos.get("crashes"))
    pop = load_population()
    if pop:
        fmt_k = lambda v: f"{v / 1000:.1f}k" if v >= 10000 else f"{v:,.0f}"
        fmt_money = lambda v: (f"${v / 1000000:.1f}M" if v >= 1000000
                               else f"${v / 1000:.0f}k" if v >= 10000 else f"${v:,.0f}")
        pop["chart_pop"] = svg_line_chart(
            [(r["year"], r["pop"]) for r in pop["series"]], fmt_k,
            aria="Population estimate by year")
        value_pts = [(r["year"], r["home_value"]) for r in pop["series"] if r["home_value"]]
        rent_pts = [(r["year"], r["rent"]) for r in pop["series"] if r["rent"]]
        if value_pts:
            pop["chart_value"] = svg_line_chart(value_pts, fmt_money, w=360, h=200,
                                                aria="Median home value by year")
        if rent_pts:
            pop["chart_rent"] = svg_line_chart(rent_pts, fmt_money, w=360, h=200,
                                               aria="Median rent by year")
        render("population.html", SITE / "population.html", root="", pop=pop,
               photo=photos.get("population"))
    address_book = build_address_book(
        {p["address"] for p in props["properties"]} if props else None)
    if address_book:
        (SITE / "addresses.json").write_text(
            json.dumps(address_book, separators=(",", ":")), encoding="utf-8")
    render("index.html", SITE / "index.html", root="",
           stats=stats, election_stats=election_stats, houses=houses,
           boards=boards, crashes=crashes, pop=pop, props=props,
           address_book=bool(address_book),
           polling=(precinct_map or {}).get("polling"),
           photo=photos.get("index"), lede=lede)
    if houses:
        render("houses.html", SITE / "house-prices.html", root="",
               houses=houses, photo=photos.get("houses"))
    render(
        "council.html",
        SITE / "council.html",
        root="",
        current_members=current_members,
        former_members=former_members,
        meetings_by_year=meetings_by_year,
        stats=stats,
        topics=topic_pages,
        photo=photos.get("council"),
    )
    planned_pages_all = [
        {"slug": "population", "name": "Population",
         "pitch": "How many people call Vienna home, decade by decade.",
         "source": "U.S. Census Bureau — decennial census and American Community "
                   "Survey for the Town of Vienna (place 51-81072)"},
    ]
    planned_pages = [p for p in planned_pages_all
                     if not (p["slug"] == "population" and pop)]
    if not houses:
        planned_pages.insert(0, {
            "slug": "house-prices", "name": "House prices",
            "pitch": "What homes in Vienna have sold for over the years.",
            "source": "Fairfax County Department of Tax Administration sales records"})
    for page in planned_pages:
        render("planned.html", SITE / f"{page['slug']}.html", root="", page=page)
    for member in members:
        render("member.html", SITE / "member" / f"{member['slug']}.html", root="../", member=member)
    for meeting in meetings:
        render("meeting.html", SITE / "meeting" / f"{meeting['slug']}.html", root="../", meeting=meeting)
    for entry in items_by_id.values():
        render("item.html", SITE / "item" / f"{entry['item']['EventItemId']}.html", root="../", entry=entry)
    for tp in topic_pages:
        render("topic.html", SITE / "topic" / f"{tp['slug']}.html", root="../", topic=tp)

    sitemap = ['<?xml version="1.0" encoding="UTF-8"?>',
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for rel in sitemap_paths:
        sitemap.append(
            f"  <url><loc>{BASE_URL}/{rel}</loc><lastmod>{today}</lastmod></url>"
        )
    sitemap.append("</urlset>")
    (SITE / "sitemap.xml").write_text("\n".join(sitemap) + "\n", encoding="utf-8")

    print(
        f"built site/: {len(members)} members ({len(current_members)} current), "
        f"{len(meetings)} meetings, {len(items_by_id)} items, {n_votes} vote records, "
        f"{len(topic_pages)} topics"
    )


if __name__ == "__main__":
    main()
