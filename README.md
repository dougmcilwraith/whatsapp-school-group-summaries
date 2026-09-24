# WhatsApp Summariser

Too many messages in the class WhatsApp groups and no time to read them? This
polls your local WhatsApp Desktop database, pulls out anything that looks like
an action point or reminder (trips, non-uniform days, permission slips, etc.),
and emails you a compact summary with anything happening today or tomorrow
highlighted at the top.

Everything runs locally on your own Mac — messages are only sent to Google's
Gemini API (for summarising) and to Gmail (for sending you the email).

## How it works

- **`watcher.py`** — polls WhatsApp Desktop's local database
  (`ChatStorage.sqlite`) for new messages. Requires the official **WhatsApp
  Desktop app** to be installed and logged in on this Mac, since that's what
  keeps the database up to date. The database itself isn't encrypted, but
  macOS sandboxing means your terminal needs **Full Disk Access** to read it
  (see Setup below).
- **`summarise_group.py`** — the main script you run. It:
  1. Finds the WhatsApp group chats configured in `config.json`.
  2. Grabs messages from the last N days (default 7).
  3. Sends the transcript to Google Gemini (AI Studio) and asks it to pull
     out action points/reminders per day.
  4. Prints a compact table to the terminal, and optionally emails it as a
     nicely formatted HTML table with today/tomorrow's items highlighted.

## Requirements

- macOS with **WhatsApp Desktop** installed and signed in.
- Python 3.10+
- A free **Gemini API key** from [aistudio.google.com](https://aistudio.google.com/apikey).
- A **Gmail account** with an **App Password** (used to send the summary
  emails — see Setup below).

Install dependencies:

```bash
pip install google-genai python-dotenv
```

## Setup

### 1. Full Disk Access (required to read the WhatsApp database)

Go to **System Settings → Privacy & Security → Full Disk Access** and enable
it for your terminal app (or whatever runs `python3`). Without this you'll
get a `PermissionError` reading WhatsApp's files.

### 2. Configure your groups

Copy the template and edit it — `config.json` is gitignored so your real
group names and email address never get committed:

```bash
cp config.example.json config.json
```

Edit `config.json` and list the WhatsApp group chat(s) you want summarised.
`groups` maps a name substring (used to find the chat) to a short
abbreviation used in the output table:

```json
{
  "groups": {
    "Example Group A": "GRA",
    "Example Group B": "GRB"
  },
  "recipient_emails": [
    "you@example.com"
  ]
}
```

### 3. Configure secrets (`.env`)

Copy the template and fill in your real keys — `.env` is gitignored so your
keys never get committed:

```bash
cp .env.example .env
```

Then edit `.env`:

```
GEMINI_API_KEY=your-gemini-api-key

# Gmail sending — this needs configuring for --send-email to work.
# Use a Gmail App Password, NOT your normal Gmail password:
# https://myaccount.google.com/apppasswords (requires 2-Step Verification enabled)
SMTP_USERNAME=you@gmail.com
SMTP_PASSWORD=your-16-character-app-password
```

By default `config.json` is set up to use Gmail's SMTP server
(`smtp.gmail.com:587`), so no changes are needed there unless you use a
different email provider.

## Usage

Print a summary of the last 7 days to the terminal:

```bash
python3 summarise_group.py
```

Also email the summary (as an HTML table) to the addresses in
`recipient_emails`:

```bash
python3 summarise_group.py --send-email
```

Other options:

```bash
python3 summarise_group.py --days 14           # look back further
python3 summarise_group.py --name "Some Group"  # summarise a group not in config.json
```

## Automating it

To get a daily summary without running it manually, schedule it with
`launchd` (macOS) or `cron` to run `python3 summarise_group.py --send-email`
once a day, e.g. every morning before school.
