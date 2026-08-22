#!/usr/bin/env python3
"""Delete drafts addressed to specific recipients from the Drafts folder.

Connects via the same vet.toml/.env config as draft_invites.py, scans every
message in the Drafts folder, and permanently deletes any whose To header
matches one of the addresses in TARGET_ADDRESSES.

Usage:
  remove_drafts.py
"""

import email
import sys
from email.utils import getaddresses

from draft_invites import SCRIPT_DIR, connect, find_drafts_folder, get_credentials, load_config

TARGET_ADDRESSES = {
    # "someone@example.com",
}


def main():
    cfg = load_config(SCRIPT_DIR / "vet.toml")
    address, password = get_credentials(SCRIPT_DIR / ".env", cfg["account"]["active"])
    imap = connect(cfg, address, password)
    try:
        folder = find_drafts_folder(imap, cfg)
        status, _ = imap.select(f'"{folder}"')
        if status != "OK":
            sys.exit(f"Could not select Drafts folder {folder!r}")

        status, data = imap.uid("search", None, "ALL")
        if status != "OK":
            sys.exit("IMAP SEARCH failed")
        uids = data[0].split()

        deleted = 0
        for uid in uids:
            status, msg_data = imap.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (TO SUBJECT)])")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            header_bytes = msg_data[0][1]
            msg = email.message_from_bytes(header_bytes)
            to_addrs = {addr.lower() for _, addr in getaddresses(msg.get_all("To", []))}
            matched = to_addrs & TARGET_ADDRESSES
            if not matched:
                continue
            subject = msg.get("Subject", "(no subject)")
            print(f"Deleting: {subject}  ->  {', '.join(sorted(matched))}")
            imap.uid("store", uid, "+FLAGS", "\\Deleted")
            deleted += 1

        if deleted:
            imap.expunge()
            print(f"\nDeleted {deleted} draft(s).")
        else:
            print("No matching drafts found.")
    finally:
        try:
            imap.logout()
        except Exception:
            pass


if __name__ == "__main__":
    main()
