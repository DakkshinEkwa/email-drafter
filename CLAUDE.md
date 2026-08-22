# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A tool that generates personalized speaker-invitation email **drafts** for two panel programs — **VET** (Veterinary Business Institute) and **OBA** (Ophthalmology Business Academy). It deliberately has **no send capability**: it builds MIME messages and IMAP-APPENDs them to a Drafts folder, where the user reviews, edits, and sends each one manually. Never add SMTP or any sending path to this script.

Which campaign a run targets is chosen by **`--org {vet,oba}`** (required). Each org has its own config file, template set, IMAP account, and drafted-log — see `ORG_CONFIGS` in `draft_invites.py`:

- **vet** → `vet.toml`, `template.html/.txt`, `drafted_log.csv`. IMAP: **yahoo** via `imap.mail.yahoo.com:993`.
- **oba** → `oba.toml`, `template_oba.html/.txt`, `drafted_log_oba.csv`. IMAP: **rackspace** via `secure.emailsrvr.com:993` (app-password login, no OAuth).

## Commands

```bash
# Setup
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
# NOTE: use .venv/bin/python -m pip, not .venv/bin/pip (stale shebang)

# Every run needs --org {vet,oba} (or a --config FILE override). Examples use vet;
# swap in --org oba to target the OBA campaign.

# List available worksheet tabs in the configured Google Sheet
.venv/bin/python draft_invites.py --org vet --list-sheets

# Preview: render rows to out/*.eml, no IMAP, no credentials needed
.venv/bin/python draft_invites.py --org vet --dry-run --sheet "Tab Name" --limit 3

# Safe live test: ONE draft from a built-in mock partner, to your own mailbox
.venv/bin/python draft_invites.py --org vet --test   # or --test someone@example.com

# Real run: drafts for every partner row with an email
.venv/bin/python draft_invites.py --org vet --sheet "Tab Name"
# with optional flags: --limit N, --start-row N, --force, --config FILE

# Yahoo OAuth2 one-time setup (vet/yahoo only; not used by oba/rackspace)
.venv/bin/python draft_invites.py --setup-oauth
```

There are no tests or linters. Verify changes with `--dry-run` (inspect `out/*.eml`) before any live run, and use `--test` before drafting to real partner addresses.

## Architecture

Single script `draft_invites.py` (stdlib + gspread + openai), driven by three inputs:

- **Google Sheet** — partner rows read via `gspread`. The spreadsheet URL/ID is set in the active config's `[gsheet].spreadsheet`. The script scans the first 10 rows for a header containing "Contact Name" and "Email", then maps columns by alias substrings (`HEADER_ALIASES`). Columns: `contact name`, `email`, `expertise`, `company name`.
- **Config file** (`vet.toml` for vet, `oba.toml` for oba) — per-campaign config:
  - `[account]` — `active = "yahoo"` (vet) or `"rackspace"` (oba), plus a matching `[account.<name>]` sub-table containing `from_name`, `imap_host`, `imap_port` (and optional `drafts_folder`).
  - `[org]` — `name`, used in the AI system prompt ("on behalf of {name}"). Defaults to "Veterinary Business Institute" if absent.
  - `[templates]` — optional `html`/`text` keys naming the template files. Defaults to `template.html`/`template.txt` if absent (so `vet.toml` can omit it). `oba.toml` points them at `template_oba.*`.
  - `[gsheet]` — `spreadsheet` key: full Google Sheets URL or bare sheet ID.
  - `[panel]` — category/title/short_title/date/time/speaker_count/description/4–5 discussion points. Edit per new campaign.
  - `[email]` — `subject` template and `cc`.
- **Template sets** — `template.html/.txt` (vet) and `template_oba.html/.txt` (oba); multipart/alternative bodies using `{{TOKEN}}` placeholders. Both sets share the same token set. `template.md` is the human-readable master for the vet copy; `oba-emails.md` is the OBA source. If a master changes, the corresponding rendered templates must be updated.

## Companion script — `remove_drafts.py`

A cleanup utility to undo mistaken drafts: it reuses `draft_invites.py`'s `load_config`/`connect`/`find_drafts_folder`, scans the Drafts folder, and permanently deletes (expunges) any message whose `To` matches the hardcoded `TARGET_ADDRESSES` set. No flags; edit the set in-file before running. **It is hardcoded to `vet.toml`/yahoo** — it does not take `--org`, so it only ever touches the vet mailbox. Point it at oba by changing the `load_config(SCRIPT_DIR / "vet.toml")` line.

## Google Sheets access

`get_google_client()` uses `gspread.oauth()` with read-only scopes. On first run it opens a browser tab for Google sign-in and writes a local `token.json` — subsequent runs are silent.

- `credentials.json` is looked up in this directory first, then one level up.
- `token.json` lives in this directory and is written automatically on first auth.

## Account config

`load_config()` reads `account.active`, merges the matching sub-table into a flat `cfg["account"]` dict, and validates it. The rest of the script sees a single account config.

## Credentials — `.env`

See `.env.example`. `get_credentials(env_path, account_type)` reads `<ACCOUNT_TYPE>_EMAIL_ADDRESS` / `<ACCOUNT_TYPE>_EMAIL_PASSWORD` — the prefix is the active account name (`YAHOO_` for vet, `RACKSPACE_` for oba). Only `account_type == "yahoo"` consults `OAUTH_REFRESH_TOKEN`; when present it returns `(address, None)` to signal the OAuth2 path in `connect()`. Rackspace always uses plain app-password `imap.login`.

## Yahoo OAuth2 path (for future use)

`connect()` detects `password is None` → calls `get_access_token()` to exchange the stored refresh token for a short-lived access token → authenticates via IMAP SASL XOAUTH2:

```
base64("user={email}\x01auth=Bearer {access_token}\x01\x01")
```

`--setup-oauth` runs a one-time browser auth flow: opens Yahoo's authorization URL, user signs in, Yahoo redirects to `https://localhost:8080` (browser shows connection error — expected), user pastes the full redirect URL back, script extracts the code and exchanges it for tokens. `OAUTH_REFRESH_TOKEN` is appended to `.env`.

**Note**: Yahoo's developer portal no longer offers Mail API permissions to new apps (`mail-w` scope returns `invalid_scope`). The OAuth2 path is fully implemented but currently unused — app-password auth is active.

## LinkedIn Intro Generation

`craft_linkedin_intro(expertise, company)` transforms raw spreadsheet notes into one warm sentence for `{{LINKEDIN_INTRO}}`. Priority: company name column > parenthetical in notes. Extracts an expertise phrase via `_STRONG_FIT_RE` regex. Falls back to first sentence of the notes. Zero cost, no API call.

If `OPENAI_API_KEY` is set, `generate_ai_copy()` calls GPT-4o-mini instead, returning both `linkedin_intro` and a polished `expertise_paragraph`. Failure falls back to the regex path silently.

## Key invariants

- `HEADER_ALIASES` maps canonical field names to spreadsheet column aliases (case-insensitive substring). Current: `name→"contact name"`, `email→"email"`, `expertise→["expertise","plus points"]`, `linkedin→"linkedin profile"`, `company→["company name","company"]`.
- Any template line containing `{{POINT_5}}` is dropped when only 4 points are configured. Any unreplaced `{{...}}` token after substitution is a hard error for that row.
- The drafted-log is the idempotency record, one per org (`drafted_log.csv` for vet, `drafted_log_oba.csv` for oba), resolved from `--org` via `ORG_CONFIGS`; `--force` overrides it. `--test` never writes to it.
- Drafts folder is auto-detected via `\Drafts` IMAP flag; fallbacks: `"Draft"`, `"Drafts"`, `"INBOX.Drafts"`. Override with `drafts_folder` in the account sub-table.
- `MOCK_ROW` is used for `--test` drafts and includes realistic expertise/company to verify `craft_linkedin_intro()` before live runs.
- HTML values in `build_mappings()` are escaped via `html.escape()`; plain-text values are not.
- `load_config()` validates all required keys in `[account]`, `[gsheet]`, `[panel]`, and `[email]` sections at startup.
