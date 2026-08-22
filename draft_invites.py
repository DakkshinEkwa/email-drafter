#!/usr/bin/env python3
"""Create speaker-invitation email DRAFTS via IMAP — never sends anything.

Reads partners from a Google Sheet, fills the org's HTML/text templates with
panel details from its config (vet.toml or oba.toml, chosen by --org), and
APPENDs each message to the account's Drafts
folder so it can be reviewed and sent manually from webmail or Thunderbird.

Usage:
  draft_invites.py --list-sheets                       list worksheet tabs and exit
  draft_invites.py --dry-run --sheet TAB [--start-row N]  write .eml files to out/, no IMAP
  draft_invites.py --test [address]                    ONE draft from a mock partner
  draft_invites.py --sheet TAB [--start-row N]         drafts for real partner rows
  draft_invites.py --force                             ignore drafted_log.csv skip-list
"""

import argparse
import base64
import csv
import getpass
import gspread
import html
import imaplib
import json
import os
import re
import sys
import time
import tomllib
import urllib.parse
import urllib.request
import webbrowser
from gspread.auth import READONLY_SCOPES
from datetime import datetime
from email.headerregistry import Address
from email.message import EmailMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import make_msgid, parseaddr
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
TOKEN_RE = re.compile(r"\{\{[A-Z0-9_]+\}\}")

# --org selects which campaign to draft for: its config file and its own
# idempotency log. Templates are resolved from each config's [templates] section.
ORG_CONFIGS = {
    "vet": {"config": "vet.toml", "log": "drafted_log.csv"},
    "oba": {"config": "oba.toml",   "log": "drafted_log_oba.csv"},
}

_YAHOO_AUTH_URL  = "https://api.login.yahoo.com/oauth2/request_auth"
_YAHOO_TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"

HEADER_ALIASES = {
    "name": ["contact name"],
    "email": ["email"],
    "expertise": ["expertise", "plus points"],
    "linkedin": ["linkedin profile"],
    "company": ["company name", "company"],
}

MOCK_ROW = {
    "row_no": 0,
    "name": "Dr. Test Example, DVM",
    "email": None,  # filled with the --test address
    "expertise": (
        "Practicing veterinary cancer specialist, international speaker, author, and "
        "video educator. Strong fit for senior-pet quality-of-life decisions around "
        "cancer, caregiver communication, and helping teams explain complex treatment "
        "choices clearly. (Guardian Veterinary Specialists)"
    ),
    "company": "Guardian Veterinary Specialists",
}


def load_config(path):
    name = Path(path).name
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    if "account" not in cfg:
        sys.exit(f"{name}: missing [account] section")
    active = cfg["account"].get("active")
    if not active:
        sys.exit(f"{name}: missing account.active (set to 'yahoo')")
    if active not in cfg["account"]:
        sys.exit(f"{name}: account.active = '{active}' but no [account.{active}] section found")
    cfg["account"] = {**cfg["account"][active], "active": active}
    for key in ("from_name", "imap_host"):
        if key not in cfg["account"]:
            sys.exit(f"{name}: missing account.{active}.{key}")
    for section, keys in {
        "gsheet": ["spreadsheet"],
        "panel": ["category", "title", "short_title", "date", "time", "speaker_count",
                  "description", "points"],
        "email": ["subject"],
    }.items():
        if section not in cfg:
            sys.exit(f"{name}: missing [{section}] section")
        for key in keys:
            if key not in cfg[section]:
                sys.exit(f"{name}: missing {section}.{key}")
    points = cfg["panel"]["points"]
    if not 4 <= len(points) <= 5:
        sys.exit(f"{name}: panel.points must have 4 or 5 entries, got {len(points)}")
    return cfg


def load_env(path):
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip("'\"")
    return env


def get_google_client():
    creds_path = SCRIPT_DIR / "credentials.json"
    if not creds_path.exists():
        creds_path = SCRIPT_DIR.parent / "credentials.json"
    token_path  = SCRIPT_DIR / "token.json"
    if not creds_path.exists():
        sys.exit(
            f"Google OAuth credentials not found: {creds_path}\n"
            "Download from Google Cloud Console: APIs & Services > Credentials > "
            "OAuth 2.0 Client ID > Desktop App > Download JSON, "
            "and place it in this directory as credentials.json."
        )
    return gspread.oauth(
        scopes=READONLY_SCOPES,
        credentials_filename=str(creds_path),
        authorized_user_filename=str(token_path),
    )


def open_spreadsheet(client, raw_id):
    sheet_id = raw_id.strip()
    if "/" in sheet_id:
        for part in sheet_id.split("/"):
            part = part.split("?")[0]
            if len(part) > 20 and re.match(r"^[A-Za-z0-9_-]+$", part):
                sheet_id = part
                break
    try:
        return client.open_by_key(sheet_id)
    except gspread.exceptions.APIError as exc:
        sys.exit(f"Could not open spreadsheet ({sheet_id}): {exc}")


def get_access_token(client_id, client_secret, refresh_token):
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(_YAHOO_TOKEN_URL, data=data)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())["access_token"]
    except Exception as e:
        sys.exit(f"OAuth2 token refresh failed: {e}\nRun --setup-oauth to re-authorize.")


_OAUTH_REDIRECT = "https://localhost:8080"


def setup_oauth(env_path):
    env = load_env(env_path)
    client_id     = env.get("OAUTH_CLIENT_ID")     or sys.exit("Add OAUTH_CLIENT_ID to .env first.")
    client_secret = env.get("OAUTH_CLIENT_SECRET") or sys.exit("Add OAUTH_CLIENT_SECRET to .env first.")

    auth_url = (
        f"{_YAHOO_AUTH_URL}?client_id={urllib.parse.quote(client_id)}"
        f"&redirect_uri={urllib.parse.quote(_OAUTH_REDIRECT)}"
        "&response_type=code&scope=mail-w"
    )

    print("Opening Yahoo authorization in your browser...")
    webbrowser.open(auth_url)
    print(
        "\nAfter you sign in, Yahoo will redirect to localhost and your browser\n"
        "will show a connection error. That is expected.\n"
        "Copy the FULL URL from your browser's address bar and paste it below.\n"
    )
    redirect_url = input("Paste the redirect URL: ").strip()
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(redirect_url).query)
    code = (qs.get("code") or [None])[0]
    if not code:
        sys.exit("No authorization code found in the URL — did you copy the full address bar URL?")

    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": _OAUTH_REDIRECT,
    }).encode()
    req = urllib.request.Request(_YAHOO_TOKEN_URL, data=data)
    try:
        with urllib.request.urlopen(req) as resp:
            tokens = json.loads(resp.read())
    except Exception as e:
        sys.exit(f"OAuth2 token exchange failed: {e}")
    refresh_token = tokens.get("refresh_token") or sys.exit(
        "No refresh_token in response — check Yahoo app has Mail Read/Write permission."
    )
    with open(env_path, "a") as f:
        f.write(f"\nOAUTH_REFRESH_TOKEN={refresh_token}\n")
    print("Refresh token saved to .env — setup complete.")


def get_credentials(env_path, account_type):
    env = load_env(env_path)
    prefix = account_type.upper() + "_"
    address = env.get(f"{prefix}EMAIL_ADDRESS") or input("Email address: ").strip()
    if not address:
        sys.exit(f"No {prefix}EMAIL_ADDRESS in .env")
    if account_type == "yahoo" and env.get("OAUTH_REFRESH_TOKEN"):
        return address, None  # None signals OAuth2 path to connect()
    password = env.get(f"{prefix}EMAIL_PASSWORD") or getpass.getpass(f"IMAP password for {address}: ")
    if not password:
        sys.exit(f"No password. Fill {prefix}EMAIL_PASSWORD in .env.")
    return address, password


def find_header_row(all_rows, search_depth=10):
    for row_idx, row in enumerate(all_rows[:search_depth]):
        cells = [str(c).strip().casefold() for c in row]
        if any("contact name" in v for v in cells) and any("email" in v for v in cells):
            columns = {}
            for canonical, needles in HEADER_ALIASES.items():
                for col_idx, value in enumerate(cells):
                    if value and any(n in value for n in needles):
                        columns[canonical] = col_idx
                        break
            missing = {"name", "email", "expertise"} - columns.keys()
            if missing:
                sys.exit(f"Header row found but missing columns: {', '.join(sorted(missing))}")
            return row_idx, columns
    sys.exit("Could not find a header row containing 'Contact Name' and 'Email' "
             "in the first 10 rows of the worksheet.")


def load_rows_from_gsheet(ws, start_row=1):
    all_rows = ws.get_all_values()
    if not all_rows:
        sys.exit("Worksheet is empty.")
    header_idx, columns = find_header_row(all_rows)
    first_data_sheet_row = header_idx + 2          # 1-indexed sheet row of first data row
    start_offset = max(0, start_row - first_data_sheet_row)
    data_rows = all_rows[header_idx + 1 + start_offset:]

    def cell(row, key):
        idx = columns.get(key)
        if idx is None or idx >= len(row):
            return ""
        return str(row[idx]).strip()

    rows, missing_email = [], []
    for i, row in enumerate(data_rows, start=first_data_sheet_row + start_offset):
        name       = cell(row, "name")
        email_addr = cell(row, "email")
        expertise  = cell(row, "expertise")
        company    = cell(row, "company")
        if not name and not email_addr:
            continue
        if not email_addr or "@" not in parseaddr(email_addr)[1]:
            missing_email.append((i, name or "(no name)"))
            continue
        rows.append({
            "row_no": i,
            "name": name,
            "email": parseaddr(email_addr)[1],
            "expertise": expertise,
            "company": company,
        })
    return rows, missing_email


_STRONG_FIT_RE = re.compile(
    r'(?:strong(?:\s+\w+)?\s+fit for|specializes? in|known for|expertise in'
    r'|well[-\s]suited for|focus(?:es|ed)? on|background in)\s+(.+)',
    re.IGNORECASE,
)


def craft_linkedin_intro(expertise: str, company: str = "") -> str:
    if not expertise:
        return ""
    # Prefer the explicit Company Name column over a parenthetical in the notes
    org = company.strip() if company.strip() else None
    if not org:
        org_match = re.search(r'\(([^)]+)\)\s*$', expertise.strip())
        if org_match:
            candidate = org_match.group(1).strip()
            # Skip if it looks like a URL
            if not re.search(r'[./]', candidate):
                org = candidate
    clean = re.sub(r'\([^)]+\)', '', expertise).strip()
    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', clean) if s.strip()]
    expertise_phrase = None
    for sentence in sentences:
        m = _STRONG_FIT_RE.match(sentence.rstrip('.'))
        if m:
            expertise_phrase = m.group(1).strip().rstrip('.')
            break
    if org and expertise_phrase:
        return (f"I came across your work through {org} and was especially drawn to "
                f"your expertise in {expertise_phrase}.")
    if org:
        first = (lambda s: s[0].lower() + s[1:] if s else s)(sentences[0].rstrip('.')) if sentences else expertise.rstrip('.')
        return (f"I came across your work through {org} and your background immediately "
                f"stood out — you're a {first}.")
    if expertise_phrase:
        return (f"I came across your profile on LinkedIn and was especially drawn to "
                f"your expertise in {expertise_phrase}.")
    first = sentences[0].rstrip('.').lower() if sentences else expertise.rstrip('.')
    return (f"I came across your profile on LinkedIn and your background immediately "
            f"stood out — you're a {first}.")


def generate_ai_copy(expertise: str, company: str, panel_title: str,
                     greeting_name_str: str, api_key: str,
                     org_name: str = "Veterinary Business Institute") -> dict:
    """Call GPT-4o-mini to produce LINKEDIN_INTRO and a polished EXPERTISE paragraph."""
    import openai
    client = openai.OpenAI(api_key=api_key)

    system = (
        "You are a warm, professional event coordinator writing personalized speaker-invitation "
        f"emails on behalf of the {org_name}. "
        "Return ONLY valid JSON with exactly two keys: "
        "\"linkedin_intro\" and \"expertise_paragraph\". No markdown, no extra text."
    )
    user = (
        f"Panel title: {panel_title}\n"
        f"Speaker first name / greeting: {greeting_name_str}\n"
        f"Company / practice name: {company or '(not provided)'}\n"
        f"Raw expertise notes from spreadsheet:\n{expertise}\n\n"
        "Generate:\n"
        "1. linkedin_intro — ONE sentence. If a company name is provided, open with "
        "\"I came across your work through [company]\"; otherwise open with "
        "\"I came across your profile on LinkedIn\". Then highlight the speaker's most "
        "distinctive credential from the notes. End with a short panel-specific hook such as "
        "\"— exactly the voice [brief topic phrase] conversations need.\"\n"
        "2. expertise_paragraph — 2–3 sentences in second person (\"Your X gives you…\") "
        "that explain why this speaker's background is a great fit for the panel. "
        "Synthesize the raw notes into polished, specific copy — do NOT copy-paste the raw notes."
    )

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        temperature=0.7,
        max_tokens=300,
        response_format={"type": "json_object"},
    )
    data = json.loads(response.choices[0].message.content)
    return {
        "linkedin_intro": data.get("linkedin_intro", "").strip(),
        "expertise_paragraph": data.get("expertise_paragraph", "").strip(),
    }


def greeting_name(full_name):
    base = full_name.split(",")[0].strip()
    parts = base.split()
    if parts and parts[0].rstrip(".").lower() in ("dr", "mr", "ms", "mrs", "prof"):
        for part in parts[1:]:
            if len(part.rstrip(".")) > 1:  # skip single-letter initials like "M."
                return f"{parts[0]} {part}"
        return parts[0] if len(parts) == 1 else f"{parts[0]} {parts[-1]}"
    return parts[0] if parts else base


def render(template, mapping, has_point5):
    lines = []
    for line in template.splitlines(keepends=True):
        if "{{POINT_5}}" in line and not has_point5:
            continue
        lines.append(line)
    text = "".join(lines)
    for token, value in mapping.items():
        text = text.replace("{{%s}}" % token, value)
    leftover = TOKEN_RE.findall(text)
    if leftover:
        raise ValueError(f"unreplaced placeholders: {', '.join(sorted(set(leftover)))}")
    return text


def build_mappings(cfg, row, openai_key=None):
    panel = cfg["panel"]
    points = [str(p).strip() for p in panel["points"]]
    gname = greeting_name(row["name"])
    expertise_raw = row["expertise"] or ""
    company_raw = row.get("company", "")

    org_name = cfg.get("org", {}).get("name", "Veterinary Business Institute")

    if openai_key and (expertise_raw or company_raw):
        try:
            ai = generate_ai_copy(
                expertise=expertise_raw,
                company=company_raw,
                panel_title=str(panel["title"]),
                greeting_name_str=gname,
                api_key=openai_key,
                org_name=org_name,
            )
            linkedin_intro = ai["linkedin_intro"] or "(craft a warm LinkedIn intro here)"
            expertise_display = ai["expertise_paragraph"] or expertise_raw or "(no expertise notes in the sheet — write this part yourself)"
        except Exception as e:
            print(f"  [AI] GPT call failed ({e}); falling back to regex intro", file=sys.stderr)
            linkedin_intro = craft_linkedin_intro(expertise_raw, company_raw) or "(craft a warm LinkedIn intro here)"
            expertise_display = expertise_raw or "(no expertise notes in the sheet — write this part yourself)"
    else:
        linkedin_intro = craft_linkedin_intro(expertise_raw, company_raw) or "(craft a warm LinkedIn intro here)"
        expertise_display = expertise_raw or "(no expertise notes in the sheet — write this part yourself)"

    date_str = str(panel["date"])
    try:
        short_date = datetime.strptime(date_str, "%A, %B %d, %Y").strftime("%B %-d")
    except ValueError:
        short_date = date_str

    base = {
        "GREETING_NAME": gname,
        "LINKEDIN_INTRO": linkedin_intro,
        "EXPERTISE": expertise_display,
        "PANEL_CATEGORY": str(panel["category"]),
        "PANEL_TITLE": str(panel["title"]),
        "SHORT_TITLE": str(panel["short_title"]),
        "DATE": date_str,
        "SHORT_DATE": short_date,
        "TIME": str(panel["time"]),
        "SPEAKER_COUNT": str(panel["speaker_count"]),
        "PANEL_DESCRIPTION": str(panel["description"]).strip(),
    }
    for i, point in enumerate(points, start=1):
        base[f"POINT_{i}"] = point
    text_map = dict(base)
    html_map = {k: html.escape(v, quote=False) for k, v in base.items()}
    return text_map, html_map, len(points) == 5


def build_message(cfg, row, account_email, text_tpl, html_tpl, openai_key=None):
    text_map, html_map, has_p5 = build_mappings(cfg, row, openai_key=openai_key)
    body_text = render(text_tpl, text_map, has_p5)
    body_html = render(html_tpl, html_map, has_p5)
    subject = render(cfg["email"]["subject"], text_map, has_p5)

    msg = MIMEMultipart("alternative")
    user, _, domain = account_email.partition("@")
    msg["From"] = str(Address(cfg["account"]["from_name"], user, domain))
    msg["To"] = row["email"]
    msg["Subject"] = subject
    msg["Date"] = datetime.now().astimezone().strftime("%a, %d %b %Y %H:%M:%S %z")
    msg["Message-ID"] = make_msgid(domain=domain)
    cc = cfg["email"].get("cc", "")
    if cc:
        msg["Cc"] = cc
    msg.attach(MIMEText(body_text, "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))
    return msg


def connect(cfg, address, password):
    host = cfg["account"]["imap_host"]
    port = int(cfg["account"].get("imap_port", 993))
    try:
        imap = imaplib.IMAP4_SSL(host, port)
        if password is None:
            env = load_env(SCRIPT_DIR / ".env")
            tok = get_access_token(
                env["OAUTH_CLIENT_ID"],
                env["OAUTH_CLIENT_SECRET"],
                env["OAUTH_REFRESH_TOKEN"],
            )
            xoauth2 = base64.b64encode(
                f"user={address}\x01auth=Bearer {tok}\x01\x01".encode()
            )
            imap.authenticate("XOAUTH2", lambda _: xoauth2)
        else:
            imap.login(address, password)
    except imaplib.IMAP4.error as e:
        sys.exit(f"IMAP login to {host}:{port} as {address} failed: {e}\n"
                 "Check credentials in .env or run --setup-oauth.")
    return imap


def find_drafts_folder(imap, cfg):
    override = cfg["account"].get("drafts_folder")
    if override:
        return override
    status, listing = imap.list()
    if status == "OK":
        for raw in listing or []:
            line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
            m = re.match(r'\((?P<flags>[^)]*)\)\s+"(?P<delim>[^"]*)"\s+(?P<name>.+)', line)
            if m and r"\Drafts" in m.group("flags"):
                return m.group("name").strip().strip('"')
        names = [re.sub(r'^.*"\s+', "", (r.decode() if isinstance(r, bytes) else str(r))).strip().strip('"')
                 for r in listing or []]
        for candidate in ("Draft", "Drafts", "INBOX.Drafts"):
            if candidate in names:
                return candidate
    sys.exit("Could not find a Drafts folder on the server. "
             "Set account.drafts_folder in the org's config explicitly.")


def append_draft(imap, folder, msg):
    status, data = imap.append(
        f'"{folder}"', r"(\Draft)",
        imaplib.Time2Internaldate(time.time()), msg.as_bytes()
    )
    if status != "OK":
        raise RuntimeError(f"APPEND failed: {data}")


def load_drafted_log(path):
    done = set()
    if path.exists():
        with open(path, newline="") as f:
            for entry in csv.DictReader(f):
                done.add(entry["email"].casefold())
    return done


def log_drafted(path, row, message_id):
    new_file = not path.exists()
    with open(path, "a", newline="") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(["timestamp", "email", "name", "message_id"])
        writer.writerow([datetime.now().isoformat(timespec="seconds"),
                         row["email"], row["name"], message_id])


def main():
    parser = argparse.ArgumentParser(description="Create invitation drafts via IMAP (never sends).")
    parser.add_argument("--dry-run", action="store_true",
                        help="write .eml files to out/ instead of touching IMAP")
    parser.add_argument("--test", nargs="?", const="SELF", metavar="ADDRESS",
                        help="create ONE draft from a mock partner, addressed to "
                             "ADDRESS (default: your own account email)")
    parser.add_argument("--limit", type=int, help="process at most N rows")
    parser.add_argument("--sheet", metavar="TAB",
                        help="worksheet tab name (e.g. 'VET Research - Aug 18')")
    parser.add_argument("--start-row", type=int, default=1, metavar="N",
                        help="sheet row to start from, 1-indexed as shown in Sheets (default: 1)")
    parser.add_argument("--list-sheets", action="store_true",
                        help="list available worksheet tabs and exit")
    parser.add_argument("--org", choices=sorted(ORG_CONFIGS),
                        help="which campaign to draft for (vet or oba); "
                             "selects its config file and drafted-log")
    parser.add_argument("--config",
                        help="override the config file (advanced; normally use --org)")
    parser.add_argument("--force", action="store_true",
                        help="ignore drafted_log.csv and draft everyone again")
    parser.add_argument("--setup-oauth", action="store_true",
                        help="authorize Yahoo OAuth2 and save refresh token to .env")
    args = parser.parse_args()

    if args.setup_oauth:
        setup_oauth(SCRIPT_DIR / ".env")
        sys.exit(0)

    # Resolve which campaign we're drafting for. --org is the normal selector;
    # --config is a low-level override. One of them is required.
    if args.config:
        config_path = args.config
        log_name = ORG_CONFIGS.get(args.org, {}).get("log", "drafted_log.csv")
    elif args.org:
        config_path = str(SCRIPT_DIR / ORG_CONFIGS[args.org]["config"])
        log_name = ORG_CONFIGS[args.org]["log"]
    else:
        parser.error("choose a campaign with --org {%s} (or pass --config FILE)"
                     % ",".join(sorted(ORG_CONFIGS)))

    cfg = load_config(config_path)
    tpl_cfg = cfg.get("templates", {})
    text_tpl = (SCRIPT_DIR / tpl_cfg.get("text", "template.txt")).read_text()
    html_tpl = (SCRIPT_DIR / tpl_cfg.get("html", "template.html")).read_text()
    log_path = SCRIPT_DIR / log_name

    # Google Sheets — open once; used for --list-sheets and row loading
    gc          = get_google_client()
    spreadsheet = open_spreadsheet(gc, cfg["gsheet"]["spreadsheet"])

    if args.list_sheets:
        print("Available worksheet tabs:")
        for ws in spreadsheet.worksheets():
            print(f"  {ws.title}")
        sys.exit(0)

    active = cfg["account"]["active"]
    address = password = None
    if not args.dry_run:
        address, password = get_credentials(SCRIPT_DIR / ".env", active)
    else:
        _env = load_env(SCRIPT_DIR / ".env")
        address = _env.get(f"{active.upper()}_EMAIL_ADDRESS") or ""

    env = load_env(SCRIPT_DIR / ".env")
    openai_key = env.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not openai_key:
        print("Note: OPENAI_API_KEY not set — using regex fallback for personalization.\n"
              "      Add OPENAI_API_KEY=sk-... to .env to enable GPT-4o-mini copy.\n")

    if args.test:
        row = dict(MOCK_ROW)
        row["email"] = address if args.test == "SELF" else args.test
        if args.dry_run and not row["email"]:
            row["email"] = "test@example.invalid"
        rows, missing_email = [row], []
        print(f"TEST MODE: one mock draft addressed to {row['email']} "
              "(no real partner data used)")
    else:
        if not args.sheet:
            parser.error("--sheet TAB is required (use --list-sheets to see available tabs)")
        try:
            ws = spreadsheet.worksheet(args.sheet)
        except gspread.exceptions.WorksheetNotFound:
            tabs = [w.title for w in spreadsheet.worksheets()]
            sys.exit(f"Worksheet '{args.sheet}' not found. Available: {', '.join(tabs)}")
        rows, missing_email = load_rows_from_gsheet(ws, args.start_row)

    already = set() if (args.force or args.test) else load_drafted_log(log_path)
    if args.limit:
        rows = rows[: args.limit]

    imap = folder = None
    out_dir = SCRIPT_DIR / "out"
    counts = {"ok": 0, "skip": 0, "error": 0}

    try:
        if not args.dry_run:
            imap = connect(cfg, address, password)
            folder = find_drafts_folder(imap, cfg)
            print(f"Connected to {cfg['account']['imap_host']}; drafts folder: {folder}\n")
        else:
            out_dir.mkdir(exist_ok=True)
            print(f"DRY RUN: writing .eml files to {out_dir}\n")

        for row in rows:
            label = f"row {row['row_no']:>3}  {row['name']:<40.40} {row['email']}"
            if row["email"].casefold() in already:
                print(f"SKIP(already-drafted)  {label}")
                counts["skip"] += 1
                continue
            try:
                msg = build_message(cfg, row, address,
                                    text_tpl, html_tpl, openai_key=openai_key)
                if args.dry_run:
                    safe = re.sub(r"[^\w.@-]", "_", row["email"])
                    (out_dir / f"{row['row_no']:03d}_{safe}.eml").write_bytes(msg.as_bytes())
                else:
                    append_draft(imap, folder, msg)
                    if not args.test:
                        log_drafted(log_path, row, msg["Message-ID"])
                print(f"OK                     {label}")
                counts["ok"] += 1
            except Exception as e:
                print(f"ERROR                  {label}  -> {e}")
                counts["error"] += 1
    finally:
        if imap is not None:
            try:
                imap.logout()
            except Exception:
                pass

    print(f"\nDone: {counts['ok']} drafted, {counts['skip']} skipped, "
          f"{counts['error']} errors.")
    if missing_email:
        print(f"\n{len(missing_email)} partner(s) have NO email in the sheet "
              "(no draft created):")
        for row_no, name in missing_email:
            print(f"  row {row_no:>3}  {name}")


if __name__ == "__main__":
    main()
