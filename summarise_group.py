#!/usr/bin/env python3
"""Generates per-day action points/reminders for one or more WhatsApp groups.

Each group is matched by a name substring (e.g. "Example Group A" or
"Example Group B"), and its messages are grouped by calendar day. An LLM
(Google Gemini / AI Studio) then extracts action points and reminders raised
each day, so each group is summarised separately.

Requires:
    pip install google-genai python-dotenv
    Copy .env.example to .env and set GEMINI_API_KEY (get one at aistudio.google.com)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import config
from watcher import CORE_DATA_EPOCH_OFFSET, open_readonly_snapshot

GROUP_MESSAGES_QUERY = """
SELECT
    c.Z_PK AS chat_pk,
    c.ZPARTNERNAME AS chat_name,
    m.Z_PK AS pk,
    m.ZTEXT AS text,
    m.ZISFROMME AS is_from_me,
    m.ZMESSAGEDATE AS message_date,
    COALESCE(NULLIF(gm.ZCONTACTNAME, ''), NULLIF(gm.ZFIRSTNAME, ''), gm.ZMEMBERJID) AS sender
FROM ZWAMESSAGE m
JOIN ZWACHATSESSION c ON c.Z_PK = m.ZCHATSESSION
LEFT JOIN ZWAGROUPMEMBER gm ON gm.Z_PK = m.ZGROUPMEMBER
WHERE c.ZPARTNERNAME LIKE ? COLLATE NOCASE
  AND m.ZTEXT IS NOT NULL
  AND m.ZMESSAGEDATE >= ?
ORDER BY c.Z_PK ASC, m.ZSORT ASC
"""


@dataclass
class GroupMessages:
    chat_name: str
    # date string (YYYY-MM-DD) -> list of (timestamp, sender, text)
    by_day: dict[str, list[tuple[float, str, str]]] = field(default_factory=lambda: defaultdict(list))


def fetch_group_messages(db_path: Path, snapshot_path: Path, name_query: str, days: int) -> dict[int, GroupMessages]:
    since_core_data = time.time() - (days * 86400) - CORE_DATA_EPOCH_OFFSET
    conn = open_readonly_snapshot(db_path, snapshot_path)
    try:
        rows = conn.execute(GROUP_MESSAGES_QUERY, (f"%{name_query}%", since_core_data)).fetchall()
    finally:
        conn.close()

    groups: dict[int, GroupMessages] = {}
    for row in rows:
        chat_pk = row["chat_pk"]
        group = groups.setdefault(chat_pk, GroupMessages(chat_name=row["chat_name"]))
        sender = "You" if row["is_from_me"] else (row["sender"] or "Unknown")
        timestamp = row["message_date"] + CORE_DATA_EPOCH_OFFSET
        day = time.strftime("%Y-%m-%d", time.localtime(timestamp))
        group.by_day[day].append((timestamp, sender, row["text"]))

    return groups


def summarise_with_gemini(chat_name: str, by_day: dict[str, list[tuple[float, str, str]]], model: str) -> dict[str, list[dict]]:
    """Returns {sent_date: [{"event_date": "YYYY-MM-DD" | None, "text": str}, ...]}.

    event_date is the actual date the action point applies to, resolved from relative
    references ("today", "tomorrow", "next Friday") using the message's own send date
    as the reference point - NOT the date this script happens to run.
    """
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=config.GEMINI_API_KEY)

    transcript_lines = []
    for day in sorted(by_day):
        transcript_lines.append(f"--- {day} ---")
        for timestamp, sender, text in by_day[day]:
            transcript_lines.append(f"[{sender}] {text}")
    transcript = "\n".join(transcript_lines)

    days = sorted(by_day)
    prompt = (
        f"Below is a transcript of messages from the WhatsApp group \"{chat_name}\", "
        f"split into sections per day (--- YYYY-MM-DD ---, this is the date the messages in that "
        f"section were SENT). Each line is prefixed with the sender's name.\n\n"
        f"For each day, extract concrete action points and reminders (e.g. things parents/members "
        f"need to bring, do, remember, or dates/deadlines mentioned). Ignore small talk. "
        f"For each action point, also resolve the date it actually applies to (event_date), as a "
        f"YYYY-MM-DD string. Resolve relative references like \"today\", \"tomorrow\", \"this Friday\", "
        f"or \"the 26th\" using the section's own send date (--- YYYY-MM-DD ---) as the reference "
        f"point, NOT today's real-world date. If the action point has no specific date or deadline, "
        f"set event_date to null. "
        f"If a day has no action points, return an empty list for it. "
        f"Respond ONLY with a JSON object mapping each send date (YYYY-MM-DD) to a list of objects "
        f"with keys \"event_date\" and \"text\". "
        f"Days: {', '.join(days)}\n\n"
        f"Transcript:\n{transcript}"
    )

    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    return json.loads(response.text)


def make_abbreviation(chat_name: str) -> str:
    words = [w for w in chat_name.replace("&", " ").split() if w.isalnum()]
    return "".join(w[0] for w in words[:3]).upper()


def dedupe_similar_rows(rows: list[tuple[str, str, str]], similarity_threshold: float = 0.7) -> list[tuple[str, str, str]]:
    """Drops rows whose text overlaps heavily with an earlier row sharing the same (label, abbr) key."""
    from difflib import SequenceMatcher

    kept: list[tuple[str, str, str]] = []
    for label, abbr, text in rows:
        is_duplicate = any(
            label == kept_label and abbr == kept_abbr
            and SequenceMatcher(None, text.lower(), kept_text.lower()).ratio() >= similarity_threshold
            for kept_label, kept_abbr, kept_text in kept
        )
        if not is_duplicate:
            kept.append((label, abbr, text))
    return kept


def merge_group_results(group_results: list[tuple[str, str, dict[str, list[dict]], dict[str, int]]]):
    """Merges per-group action points keyed by send date, resolving each group's display abbreviation.

    Each point is (abbr, event_date, text), where event_date is the resolved date the point
    applies to (falling back to the send date if Gemini couldn't resolve one).
    """
    points_by_day: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    total_counts: dict[str, int] = defaultdict(int)
    legend: dict[str, str] = {}

    for name_query, chat_name, action_points, message_counts in group_results:
        abbr = config.GROUP_ABBREVIATIONS.get(name_query) or make_abbreviation(chat_name)
        legend[abbr] = chat_name
        total_counts[abbr] += sum(message_counts.values())
        for sent_date, points in action_points.items():
            points_by_day[sent_date]  # ensure the day shows up even with no action points
            for point in points:
                event_date = point.get("event_date") or sent_date
                points_by_day[sent_date].append((abbr, event_date, point["text"]))

    return points_by_day, total_counts, legend


def build_combined_report(group_results: list[tuple[str, str, dict[str, list[dict]], dict[str, int]]]) -> str:
    """Merges per-group action points into one compact table, newest day first, tagged by group abbreviation."""
    points_by_day, total_counts, legend = merge_group_results(group_results)

    lines = ["Key: " + ", ".join(f"{abbr}={name}" for abbr, name in legend.items()), ""]
    lines.append(f"{'Date':<6}{'Target':<7}{'Grp':<5}Action point")
    for day in sorted(points_by_day, reverse=True):
        short_day = day[5:]  # MM-DD, year omitted for brevity
        points = points_by_day[day]
        if not points:
            lines.append(f"{short_day:<6}{'':<7}{'':<5}(no action points)")
            continue
        for abbr, event_date, point in points:
            short_target = event_date[5:] if event_date != day else "-"
            lines.append(f"{short_day:<6}{short_target:<7}{abbr:<5}{point}")

    lines.append("")
    lines.append("Messages this period: " + ", ".join(f"{abbr} {count}" for abbr, count in total_counts.items()))
    return "\n".join(lines)


def build_html_report(group_results: list[tuple[str, str, dict[str, list[dict]], dict[str, int]]]) -> str:
    """HTML version with real <table> markup (so it doesn't wrap awkwardly) and a today/tomorrow highlight.

    Highlighting is based on each point's resolved event_date, not the date the message was sent.
    """
    import datetime
    from html import escape

    points_by_day, total_counts, legend = merge_group_results(group_results)

    today = datetime.date.today().isoformat()
    tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()

    parts = [
        "<html><body style='font-family: sans-serif; font-size: 14px;'>",
        "<p><b>Key:</b> " + ", ".join(f"{escape(abbr)}={escape(name)}" for abbr, name in legend.items()) + "</p>",
    ]

    highlight_rows = [
        ("Today" if event_date == today else "Tomorrow", abbr, point)
        for day in points_by_day
        for abbr, event_date, point in points_by_day[day]
        if event_date in (today, tomorrow)
    ]
    highlight_rows = dedupe_similar_rows(highlight_rows)
    if highlight_rows:
        parts.append("<h3 style='color:#c0392b;'>Happening today / tomorrow</h3>")
        parts.append("<table cellpadding='6' cellspacing='0' style='border-collapse:collapse;border:1px solid #ccc;'>")
        parts.append("<tr style='background:#f4d03f;'><th align='left'>When</th><th align='left'>Grp</th><th align='left'>Action point</th></tr>")
        for label, abbr, point in highlight_rows:
            parts.append(
                f"<tr style='background:#fdebd0;'><td>{escape(label)}</td><td>{escape(abbr)}</td><td>{escape(point)}</td></tr>"
            )
        parts.append("</table>")

    parts.append("<h3>All action points</h3>")
    parts.append("<table cellpadding='6' cellspacing='0' style='border-collapse:collapse;border:1px solid #ccc;'>")
    parts.append("<tr><th align='left'>Date sent</th><th align='left'>Target date</th><th align='left'>Grp</th><th align='left'>Action point</th></tr>")
    for day in sorted(points_by_day, reverse=True):
        points = points_by_day[day]
        if not points:
            parts.append(f"<tr><td>{day}</td><td>-</td><td></td><td><i>(no action points)</i></td></tr>")
            continue
        for abbr, event_date, point in points:
            row_style = " style='background:#fdebd0;'" if event_date in (today, tomorrow) else ""
            target = event_date if event_date != day else "-"
            parts.append(
                f"<tr{row_style}><td>{day}</td><td>{escape(target)}</td><td>{escape(abbr)}</td><td>{escape(point)}</td></tr>"
            )
    parts.append("</table>")

    parts.append(
        "<p><b>Messages this period:</b> "
        + ", ".join(f"{escape(abbr)} {count}" for abbr, count in total_counts.items())
        + "</p>"
    )
    parts.append("</body></html>")
    return "\n".join(parts)


def send_email(subject: str, plain_body: str, html_body: str, recipients: list[str]) -> None:
    import smtplib
    from email.message import EmailMessage

    if not (config.SMTP_USERNAME and config.SMTP_PASSWORD):
        sys.exit("Set SMTP_USERNAME and SMTP_PASSWORD in your .env file to send emails.")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.SMTP_USERNAME
    message["To"] = ", ".join(recipients)
    message.set_content(plain_body)
    message.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as smtp:
        smtp.starttls()
        smtp.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
        smtp.send_message(message)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", action="append", default=None,
                         help="Substring to match against a chat/group name (repeatable). "
                              "Defaults to the groups configured in config.json")
    parser.add_argument("--db-path", type=Path, default=config.DB_PATH,
                         help="Path to WhatsApp's ChatStorage.sqlite")
    parser.add_argument("--model", default=config.MODEL, help="Gemini model to use")
    parser.add_argument("--days", type=int, default=7,
                         help="Only summarise messages from the last N days (default: 7)")
    parser.add_argument("--send-email", action="store_true",
                         help="Email the summary to the recipients configured in config.json")
    args = parser.parse_args(argv)
    if args.name is None:
        args.name = config.GROUP_NAMES
    return args


def main() -> None:
    args = parse_args()

    if not config.GEMINI_API_KEY:
        sys.exit("Set GEMINI_API_KEY in your .env file before running this script.")

    if not args.db_path.exists():
        sys.exit(f"WhatsApp database not found at {args.db_path}")

    snapshot_path = Path("/tmp/whatsapp_summarise_snapshot.sqlite")
    group_results = []
    try:
        for name_query in args.name:
            groups = fetch_group_messages(args.db_path, snapshot_path, name_query, args.days)
            if not groups:
                print(f"No chats found matching '{name_query}'", file=sys.stderr)
                continue

            for group in groups.values():
                message_counts = {day: len(messages) for day, messages in group.by_day.items()}
                action_points = summarise_with_gemini(group.chat_name, group.by_day, args.model)
                group_results.append((name_query, group.chat_name, action_points, message_counts))
    finally:
        for path in (snapshot_path, snapshot_path.with_name(snapshot_path.name + "-wal"),
                     snapshot_path.with_name(snapshot_path.name + "-shm")):
            path.unlink(missing_ok=True)

    full_report = build_combined_report(group_results)
    print(full_report)

    if args.send_email and full_report:
        subject = f"WhatsApp group summary - last {args.days} days"
        html_report = build_html_report(group_results)
        send_email(subject, full_report, html_report, config.RECIPIENT_EMAILS)
        print(f"\nEmailed summary to {', '.join(config.RECIPIENT_EMAILS)}")


if __name__ == "__main__":
    main()
