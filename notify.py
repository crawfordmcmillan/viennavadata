"""notify.py — email a plain-text summary after a successful weekly refresh.

Run by the Actions workflow after the site deploys. Reads the mail settings
from the environment (MAIL_USERNAME, MAIL_PASSWORD, MAIL_TO — repo secrets)
and sends over Gmail SMTP. If they are not set, it prints the message and
exits 0, so an unconfigured notifier never fails a refresh.

Summary sources: data/fetch_meta.json (dateline), data/events.json (newest
meeting), build_summary.txt (build.py's output, captured by the workflow),
refresh_warnings.txt (written by the workflow when a GIS source fell back
to last week's data), uncategorized_titles.txt (from categorize.py).
"""
import json
import os
import smtplib
import ssl
import sys
import glob
import re
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).parent
SITE_URL = "https://viennavadata.org"


def read(path):
    p = ROOT / path
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


def source_line(name, meta_file):
    meta = ROOT / "data" / name / meta_file
    if not meta.exists():
        return f"  {name}: no data"
    fetched = json.loads(meta.read_text(encoding="utf-8")).get("fetched_at_utc", "")[:10]
    fresh = fetched == date.today().isoformat()
    return f"  {name}: {'refreshed today' if fresh else 'kept from ' + fetched}"


def epoch_date(ms):
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=ms)).date().isoformat()


def max_attr_date(pattern, field):
    """Latest epoch-ms date in an ArcGIS-style feature cache."""
    best = None
    for f in glob.glob(str(ROOT / pattern)):
        for feat in json.loads(Path(f).read_text(encoding="utf-8")).get("features", []):
            v = feat["attributes"].get(field)
            if v and (best is None or v > best):
                best = v
    return epoch_date(best) if best else "?"


def section_dates():
    """Latest record date per section of the site, from the cached data."""
    events = sorted(json.loads(read("data/events.json") or "[]"),
                    key=lambda e: e["EventDate"], reverse=True)
    newest_meeting = events[0]["EventDate"][:10] if events else "?"
    latest_roll_call = "?"
    for e in events:  # newest meeting that has at least one recorded vote
        items_file = ROOT / "data" / f"eventitems_{e['EventId']}.json"
        if not items_file.exists():
            continue
        for it in json.loads(items_file.read_text(encoding="utf-8")):
            vf = ROOT / "data" / f"votes_{it['EventItemId']}.json"
            if vf.exists() and json.loads(vf.read_text(encoding="utf-8")):
                latest_roll_call = e["EventDate"][:10]
                break
        if latest_roll_call != "?":
            break

    board_dates = [e["EventDate"][:10]
                   for f in glob.glob(str(ROOT / "data" / "boards" / "events_*.json"))
                   for e in json.loads(Path(f).read_text(encoding="utf-8"))]
    elections = json.loads(read("data/elections/elections_meta.json") or "{}").get("contests", [])
    pop_years = [int(m.group(1)) for f in glob.glob(str(ROOT / "data" / "population" / "*.json"))
                 if (m := re.search(r"_(\d{4})\.json$", f))]

    return [
        ("Council votes", f"newest meeting {newest_meeting}, latest recorded roll call {latest_roll_call}"),
        ("Planning & zoning", f"newest board meeting {max(board_dates) if board_dates else '?'}"),
        ("Elections", f"latest contest {max((c['date'] for c in elections), default='?')}"),
        ("House prices", f"latest recorded sale {max_attr_date('data/houses/sales_*.json', 'SALEDT')}"),
        ("Property timelines", f"latest recorded sale {max_attr_date('data/properties/history_*.json', 'SALEDT')}"),
        ("Crashes", f"latest crash {max_attr_date('data/crashes/crashes_*.json', 'CRASH_DT')}"),
        ("Population", f"latest census data year {max(pop_years) if pop_years else '?'}"),
    ]


def build_summary():
    dateline = json.loads(read("data/fetch_meta.json") or "{}").get("fetched_at_utc", "")[:10]
    build_line = read("build_summary.txt").replace("built site/: ", "")
    warnings = read("refresh_warnings.txt")
    uncategorized = read("uncategorized_titles.txt")
    run_url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
               f"{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/"
               f"{os.environ.get('GITHUB_RUN_ID', '')}")

    lines = [
        f"The weekly refresh ran and the site is live at {SITE_URL}.",
        "",
        f"Data as of {dateline}." + (f" Built: {build_line}" if build_line else ""),
        "",
        "Latest data by section:",
        *[f"  {name}: {detail}" for name, detail in section_dates()],
        "",
        "County and state sources:",
        source_line("houses", "houses_meta.json"),
        source_line("crashes", "crashes_meta.json"),
        source_line("properties", "properties_meta.json"),
    ]
    if warnings:
        lines += ["", "Warnings:", warnings]
    if uncategorized:
        n = len(uncategorized.splitlines())
        lines += ["", f"{n} new agenda item title(s) need a topic category:", uncategorized]
    lines += ["", f"Run log: {run_url}"]
    return "\n".join(l for l in lines if l is not None), dateline, bool(warnings or uncategorized)


def main():
    body, dateline, needs_attention = build_summary()
    subject = f"Vienna VA Data refreshed {dateline}" + (" (needs a look)" if needs_attention else "")
    # Always show the summary on the run page (GitHub's own notifications link there).
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        nl = chr(10)
        with Path(step_summary).open("a", encoding="utf-8") as fh:
            fh.write("## " + subject + nl + nl + "```" + nl + body + nl + "```" + nl)

    user, password, to = (os.environ.get(k) for k in ("MAIL_USERNAME", "MAIL_PASSWORD", "MAIL_TO"))
    if not (user and password and to):
        print("notify  mail secrets not set; printing summary instead\n")
        print(subject + "\n\n" + body)
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"Vienna VA Data <{user}>"
    msg["To"] = to
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)
    print(f"notify  sent to {to}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never fail the refresh over a notification
        print(f"notify  could not send: {e}", file=sys.stderr)
