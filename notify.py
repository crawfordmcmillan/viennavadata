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
from datetime import date
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


def build_summary():
    dateline = json.loads(read("data/fetch_meta.json") or "{}").get("fetched_at_utc", "")[:10]
    events = json.loads(read("data/events.json") or "[]")
    newest = max((e.get("EventDate", "")[:10] for e in events), default="?")
    build_line = read("build_summary.txt").replace("built site/: ", "")
    warnings = read("refresh_warnings.txt")
    uncategorized = read("uncategorized_titles.txt")
    run_url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
               f"{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/"
               f"{os.environ.get('GITHUB_RUN_ID', '')}")

    lines = [
        f"The weekly refresh ran and the site is live at {SITE_URL}.",
        "",
        f"Data as of {dateline}. Newest council meeting in the record: {newest}.",
        f"Built: {build_line}" if build_line else "",
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
