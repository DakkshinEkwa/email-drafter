# Email Drafter

Generates personalized speaker-invitation email **drafts** for two panel programs — **VET** (Veterinary Business Institute) and **OBA** (Ophthalmology Business Academy). Reads partner data from a Google Sheet, fills email templates with panel details, and IMAP-appends each draft to a mail account's Drafts folder for manual review and sending.

**No send capability by design.** Every draft lands in your Drafts folder — you review, edit, and send each one manually.

Which campaign a run targets is chosen by **`--org {vet,oba}`** (required).

---

## First-Time Setup

### 1. Python environment

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

> If reinstalling into an existing `.venv`, use `.venv/bin/python -m pip` (not `.venv/bin/pip` — the shebang may be stale).

### 2. Google Sheets access

Place a Desktop App `credentials.json` (Google Cloud Console → APIs & Services → Credentials → OAuth 2.0 Client ID) in this directory. On first run the script opens a browser tab for Google sign-in and writes a local `token.json` — subsequent runs are silent.

```bash
.venv/bin/python draft_invites.py --org vet --list-sheets
```

### 3. Fill in `.env`

```bash
cp .env.example .env
```

```
YAHOO_EMAIL_ADDRESS=            # vet IMAP login
YAHOO_EMAIL_PASSWORD=           # Yahoo app password

RACKSPACE_EMAIL_ADDRESS=        # oba IMAP login
RACKSPACE_EMAIL_PASSWORD=       # Rackspace app password

OPENAI_API_KEY=                 # optional; enables GPT-4o-mini copy
```

### 4. Point the config at your sheet

Set `[gsheet].spreadsheet` in `vet.toml` / `oba.toml` to a Google Sheets URL or bare sheet ID. Set `[email].cc` if drafts should CC a team address.

---

## CLI Reference

Every run needs `--org {vet,oba}` (or a `--config FILE` override).

### Discover available sheet tabs

```bash
.venv/bin/python draft_invites.py --org vet --list-sheets
```

### Preview — render drafts to `.eml` files, no IMAP

```bash
.venv/bin/python draft_invites.py --org vet --dry-run --sheet "Tab Name"
.venv/bin/python draft_invites.py --org vet --dry-run --sheet "Tab Name" --start-row 12
.venv/bin/python draft_invites.py --org vet --dry-run --sheet "Tab Name" --limit 3
```

Outputs to `out/*.eml` — open in any email client to preview.

### Safe live test — one mock draft to your own mailbox

```bash
.venv/bin/python draft_invites.py --org vet --test
.venv/bin/python draft_invites.py --org vet --test someone@example.com
```

Uses built-in mock partner data. Never writes to `drafted_log.csv`.

### Real run — draft for every partner row with an email

```bash
.venv/bin/python draft_invites.py --org vet --sheet "Tab Name"
.venv/bin/python draft_invites.py --org vet --sheet "Tab Name" --start-row 12
.venv/bin/python draft_invites.py --org vet --sheet "Tab Name" --limit 5
.venv/bin/python draft_invites.py --org vet --sheet "Tab Name" --force
```

### All flags

| Flag | Description |
|------|-------------|
| `--org {vet,oba}` | Campaign to draft for (required unless `--config`) |
| `--sheet TAB` | Worksheet tab name to read partners from (required unless `--test` or `--list-sheets`) |
| `--start-row N` | Sheet row number to start from, 1-indexed as shown in Google Sheets (default: first data row) |
| `--list-sheets` | Print available worksheet tabs and exit |
| `--dry-run` | Write `.eml` files to `out/` instead of connecting to IMAP |
| `--test [ADDRESS]` | Draft one mock email; defaults to your own account address |
| `--limit N` | Process at most N rows |
| `--force` | Ignore `drafted_log.csv` and re-draft previously drafted addresses |
| `--config FILE` | Use a different config file instead of the `--org` default (`vet.toml`/`oba.toml`) |
| `--setup-oauth` | Run one-time Yahoo OAuth2 browser authorization (see Yahoo section below) |

---

## Per-Campaign Configuration (`vet.toml` / `oba.toml`)

Edit these sections for each new panel before running:

```toml
[gsheet]
spreadsheet = "https://docs.google.com/spreadsheets/d/..."

[panel]
category    = "..."
title       = "..."
short_title = "..."
date        = "Wednesday, July 8, 2026"
time        = "8:00 PM – 9:00 PM EDT"
speaker_count = "2–4"
description = """..."""
points = [
    "Point one.",
    "Point two.",
    "Point three.",
    "Point four.",
    # "Point five.",   ← optional fifth point
]

[email]
cc      = ""
subject = "Invitation to speak — {{SHORT_TITLE}}, {{SHORT_DATE}}"
```

`[account]` and `[account.yahoo]` / `[account.rackspace]` sections stay constant between campaigns.

---

## Idempotency

`drafted_log.csv` (vet) and `drafted_log_oba.csv` (oba) record every address that received a draft. Re-running the script skips those addresses automatically. Use `--force` to override.

---

## Removing Drafts by Recipient (`remove_drafts.py`)

Deletes any drafts in the Drafts folder whose **To** address matches a hardcoded list of addresses in the script — useful for clearing out bounces, opt-outs, or duplicates.

```bash
.venv/bin/python remove_drafts.py
```

Connects using the same `vet.toml` / `.env` config as `draft_invites.py`, scans every draft, deletes matches, and prints what it removed. No flags — running it again is safe and will simply report no matches once the targeted drafts are gone.

To change which addresses get removed, edit the `TARGET_ADDRESSES` set directly at the top of `remove_drafts.py`.

---

## Yahoo OAuth2 Setup (future use)

An OAuth2 path is implemented in the script as an alternative to app-password auth. To use it:

1. Fill `OAUTH_CLIENT_ID` and `OAUTH_CLIENT_SECRET` in `.env` from your Yahoo Developer App
2. Set the app's Redirect URI to `https://localhost:8080`
3. Run:
   ```bash
   .venv/bin/python draft_invites.py --setup-oauth
   ```
4. Sign in via the browser — Yahoo redirects to localhost (browser shows a connection error, that's expected). Copy the full URL from the address bar and paste it into the terminal.
5. `OAUTH_REFRESH_TOKEN` is saved to `.env` automatically. Switch `account.active = "yahoo"` and run normally.

---

## Email Templates

`template.html` and `template.txt` are the VET multipart email body. `template.md` is the human-readable master — if you edit the content, update both rendered templates to match. OBA uses `template_oba.html` / `template_oba.txt` (`oba-emails.md` is the source).

Available `{{TOKENS}}`:

| Token | Source |
|-------|--------|
| `{{GREETING_NAME}}` | Derived from Contact Name column |
| `{{LINKEDIN_INTRO}}` | Auto-generated from Expertise + Company columns |
| `{{EXPERTISE}}` | Raw expertise notes (for your reference during review) |
| `{{PANEL_CATEGORY}}` | `panel.category` |
| `{{PANEL_TITLE}}` | `panel.title` |
| `{{SHORT_TITLE}}` | `panel.short_title` |
| `{{DATE}}` | `panel.date` |
| `{{SHORT_DATE}}` | Derived from `panel.date` (e.g. "July 8") |
| `{{TIME}}` | `panel.time` |
| `{{SPEAKER_COUNT}}` | `panel.speaker_count` |
| `{{PANEL_DESCRIPTION}}` | `panel.description` |
| `{{POINT_1}}` … `{{POINT_5}}` | `panel.points` (POINT_5 line dropped if only 4 points) |

---

## Google Sheet Column Requirements

The script scans the first 10 rows for a header containing **Contact Name** and **Email**. Required columns:

| Column | Aliases accepted |
|--------|-----------------|
| Contact Name | "contact name" |
| Email | "email" |
| Expertise | "expertise", "plus points" |
| Company Name | "company name", "company" |

Column order does not matter. Rows without an email address are collected and reported at the end but not drafted.
