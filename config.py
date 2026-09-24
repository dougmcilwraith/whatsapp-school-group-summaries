"""Loads non-secret settings from config.json and secrets from .env."""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

CONFIG_DIR = Path(__file__).parent
load_dotenv(CONFIG_DIR / ".env")

with open(CONFIG_DIR / "config.json") as f:
    _CONFIG = json.load(f)

MODEL = _CONFIG["model"]
GROUP_ABBREVIATIONS = _CONFIG["groups"]
GROUP_NAMES = list(GROUP_ABBREVIATIONS)
DB_PATH = Path(_CONFIG["db_path"]).expanduser()
RECIPIENT_EMAILS = _CONFIG["recipient_emails"]
SMTP_HOST = _CONFIG["smtp_host"]
SMTP_PORT = _CONFIG["smtp_port"]

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
# SMTP_USERNAME is also used as the "From" address.
SMTP_USERNAME = os.environ.get("SMTP_USERNAME")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
