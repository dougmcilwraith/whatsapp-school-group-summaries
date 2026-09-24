#!/usr/bin/env python3
"""Polls the local WhatsApp Desktop (Mac) ChatStorage.sqlite for new messages.

The database itself is a plain, unencrypted SQLite (Core Data) file - the
`enc-key.dat` next to it is only used for encrypting iCloud/local backups, not
the live database. Reading it just requires the OS-level permissions below.

macOS setup:
  Grant your terminal (or `python3`) "Full Disk Access" in
  System Settings > Privacy & Security > Full Disk Access, otherwise reading
  files under ~/Library/Group Containers/... will raise a PermissionError.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Core Data stores timestamps as seconds since 2001-01-01, SQLite/Unix since 1970-01-01.
CORE_DATA_EPOCH_OFFSET = 978307200

DEFAULT_DB_PATH = (
    Path.home()
    / "Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite"
)
DEFAULT_STATE_PATH = Path.home() / ".whatsapp_watcher_state.json"

NEW_MESSAGES_QUERY = """
SELECT
    m.Z_PK AS pk,
    m.ZTEXT AS text,
    m.ZISFROMME AS is_from_me,
    m.ZFROMJID AS from_jid,
    m.ZMESSAGEDATE AS message_date,
    COALESCE(c.ZPARTNERNAME, c.ZCONTACTJID) AS chat_name,
    c.ZCONTACTJID AS chat_jid
FROM ZWAMESSAGE m
JOIN ZWACHATSESSION c ON c.Z_PK = m.ZCHATSESSION
WHERE m.Z_PK > ?
ORDER BY m.Z_PK ASC
"""


@dataclass
class Message:
    pk: int
    text: str | None
    is_from_me: bool
    from_jid: str | None
    timestamp: float
    chat_name: str
    chat_jid: str

    def format(self) -> str:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.timestamp))
        sender = "you" if self.is_from_me else (self.from_jid or self.chat_name)
        body = self.text if self.text else "<no text / media or system message>"
        return f"[{when}] {self.chat_name} - {sender}: {body}"


def load_last_seen_pk(state_path: Path) -> int:
    if not state_path.exists():
        return 0
    try:
        return json.loads(state_path.read_text()).get("last_pk", 0)
    except (json.JSONDecodeError, OSError):
        return 0


def save_last_seen_pk(state_path: Path, last_pk: int) -> None:
    state_path.write_text(json.dumps({"last_pk": last_pk}))


def open_readonly_snapshot(db_path: Path, snapshot_path: Path) -> sqlite3.Connection:
    """Copies the db (and its WAL) so we never hold a lock on WhatsApp's live file."""
    shutil.copyfile(db_path, snapshot_path)
    for suffix in ("-wal", "-shm"):
        src = db_path.with_name(db_path.name + suffix)
        if src.exists():
            shutil.copyfile(src, snapshot_path.with_name(snapshot_path.name + suffix))
    conn = sqlite3.connect(f"file:{snapshot_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_new_messages(db_path: Path, snapshot_path: Path, since_pk: int) -> list[Message]:
    conn = open_readonly_snapshot(db_path, snapshot_path)
    try:
        rows = conn.execute(NEW_MESSAGES_QUERY, (since_pk,)).fetchall()
    finally:
        conn.close()

    return [
        Message(
            pk=row["pk"],
            text=row["text"],
            is_from_me=bool(row["is_from_me"]),
            from_jid=row["from_jid"],
            timestamp=row["message_date"] + CORE_DATA_EPOCH_OFFSET,
            chat_name=row["chat_name"] or row["chat_jid"],
            chat_jid=row["chat_jid"],
        )
        for row in rows
    ]


def on_new_messages(messages: list[Message]) -> None:
    for message in messages:
        print(message.format(), flush=True)


def run(db_path: Path, state_path: Path, interval_seconds: float) -> None:
    if not db_path.exists():
        sys.exit(f"WhatsApp database not found at {db_path}")

    snapshot_path = state_path.with_suffix(".snapshot.sqlite")
    last_pk = load_last_seen_pk(state_path)
    print(f"Watching {db_path} every {interval_seconds}s (starting after pk={last_pk})")

    try:
        while True:
            try:
                messages = fetch_new_messages(db_path, snapshot_path, last_pk)
            except sqlite3.OperationalError as exc:
                print(f"Warning: could not read database this cycle: {exc}", file=sys.stderr)
                messages = []

            if messages:
                on_new_messages(messages)
                last_pk = messages[-1].pk
                save_last_seen_pk(state_path, last_pk)

            time.sleep(interval_seconds)
    finally:
        for path in (snapshot_path, snapshot_path.with_name(snapshot_path.name + "-wal"),
                     snapshot_path.with_name(snapshot_path.name + "-shm")):
            path.unlink(missing_ok=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH,
                         help="Path to WhatsApp's ChatStorage.sqlite")
    parser.add_argument("--state-path", type=Path, default=DEFAULT_STATE_PATH,
                         help="Where to persist the last-seen message id")
    parser.add_argument("--interval", type=float, default=15.0,
                         help="Seconds between polls")
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    run(args.db_path, args.state_path, args.interval)


if __name__ == "__main__":
    main()
