# `email_watcher.py` — Usage Guide

## Overview

`email_watcher.py` is a long-running daemon that monitors an IMAP inbox for Bandcamp new-release notification emails and automatically triggers the archive pipeline for every release URL it finds. It uses **IMAP IDLE** (server-push) rather than polling, so it reacts to new mail in real time without hammering the mail server.

The overall flow is:

1. Connect to the IMAP server and drain any emails that arrived while offline
2. Enter IMAP IDLE — block until the server signals new mail (or a 25-minute timeout)
3. On wake-up, process any Bandcamp notification emails in the inbox
4. For each email: extract release URLs, group by artist, run `archive.py` once per artist
5. Move successfully processed emails to Trash; leave failures in the inbox for the next cycle
6. Re-enter IDLE; reconnect automatically on any network or IMAP error

---

## Requirements

`EMAIL_ADDRESS` and `EMAIL_PASSWORD` must be set in `.env` before starting. See [`config.py` usage](config_usage.md) for details. For Gmail, use an [App Password](https://support.google.com/accounts/answer/185833) — direct password login is blocked when 2FA is enabled.

---

## Command-Line Usage

```
python bandcamp_wacz/email_watcher.py [OPTIONS]
```

| Flag | Description |
|---|---|
| *(none)* | Normal mode — watch inbox, run full or quick pipeline as appropriate, upload to archive.org |
| `--no-upload` | Crawl and produce `.wacz` files but skip the archive.org upload step |
| `--full` | Always run the full pipeline, even for artists that already have a JSON file. By default the watcher passes `--quick` to `archive.py` for known artists to avoid re-fetching metadata |
| `--dry-run` | Parse and log emails without running the pipeline or moving any messages. Safe to use against a live inbox |
| `--lax` | Disable the sender check — matches any email whose subject contains `"New release(s) from ..."` regardless of sender, including forwarded messages. **For testing only** |
| `--debug` | Enable verbose `DEBUG`-level logging. Also passed through to `archive.py` |

---

## How Email Matching Works

The watcher looks for two conditions on every inbox message:

- **Sender** contains `noreply@bandcamp.com`
- **Subject** matches the regex `New releases? from .+` (case-insensitive)

Both `"New release from Artist"` (single) and `"New releases from Artist"` (multi-release digest) are handled identically — all album/track URLs found in the plain-text body are extracted regardless.

With `--lax`, the sender check is skipped and the subject pattern is searched anywhere in the subject line (rather than matched from the start), which allows forwarded messages like `"Fwd: New release from Artist"` to be processed. This is intended only for testing.

---

## Pipeline Selection: Full vs Quick

For each artist, the watcher checks whether a `{artist} [{band_id}]/{artist} [{band_id}].json` file already exists under `ARTISTS_DIR`:

| Condition | Pipeline used |
|---|---|
| Artist JSON **not found** (new artist) | Full pipeline — `archive.py --url <urls>` — so `fetch_metadata.py` runs and the JSON is created |
| Artist JSON **found** (known artist) | Quick pipeline — `archive.py --url <urls> --quick` — skips metadata re-fetch |
| `--full` flag set | Always full pipeline regardless |

To determine the artist, the watcher fetches the artist root URL (e.g. `https://artist.bandcamp.com/`) and extracts the `band_id` from the page HTML, then looks for a matching directory under `ARTISTS_DIR`. If the network call fails for any reason, it safely falls back to the full pipeline.

---

## Batching by Artist

Multiple notification emails from the same artist (e.g. two albums released on the same day) are **merged into a single `archive.py` invocation**. This ensures the artist JSON is only updated once and all new releases are archived in one batch.

The grouping is done by artist root URL — `https://artist.bandcamp.com/` — derived from each release URL. If a single email contains releases from multiple artists (rare but possible), they are split into separate pipeline calls.

---

## Email Disposal

- **Success:** The email is moved to `Trash` (copied to `Trash` then the original is deleted and expunged).
- **Failure (pipeline exits non-zero):** The email is left in the inbox and will be retried on the next IDLE wake-up cycle.
- **No URLs found:** The email is moved to `Trash` immediately with a warning logged.
- **Non-Bandcamp email:** Silently skipped; left in inbox untouched.

---

## IMAP IDLE Detail

The `_idle` function issues a proper IMAP IDLE command and blocks until one of:
- The server sends an `EXISTS` or `RECENT` untagged response (new mail arrived)
- A 25-minute socket timeout elapses (RFC 2177 recommends re-issuing IDLE every < 29 minutes)

The implementation borrows imaplib's internal tag generator (`imap._new_tag()`) rather than using a hardcoded tag like `"A001"`, which would collide with imaplib's counter and corrupt its internal state on subsequent commands.

If the server does not advertise `IDLE` in its capabilities, the function falls back to `time.sleep(timeout)` so the watcher still works, just without push notification.

The general socket timeout (60 s) is temporarily cleared during the IDLE wait and restored afterwards, so ordinary IMAP commands cannot hang indefinitely while still allowing IDLE's long wait.

---

## Reconnection Behaviour

The main `watch()` loop catches all IMAP and network errors and reconnects after a 30-second delay (`RECONNECT_DELAY`). The categories handled are:

| Error | Behaviour |
|---|---|
| `imaplib.IMAP4.abort` | Warning logged; reconnects after 30 s |
| `imaplib.IMAP4.error` | Error logged; reconnects after 30 s |
| `OSError` (network) | Error logged; reconnects after 30 s |
| Any other `Exception` | Error + full traceback logged; reconnects after 30 s |
| `KeyboardInterrupt` | Clean shutdown; `imap.logout()` called |

On every reconnect the inbox is drained immediately before re-entering IDLE, so no emails are missed during an outage.

---

## Running as a Service

To keep the watcher running persistently, wrap it in a systemd unit or similar process supervisor.

**Example systemd unit (`/etc/systemd/system/bandcamp-watcher.service`):**

```ini
[Unit]
Description=Bandcamp WACZ email watcher
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/path/to/project
ExecStart=/path/to/venv/bin/python bandcamp_wacz/email_watcher.py
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now bandcamp-watcher
sudo journalctl -fu bandcamp-watcher
```

---

## Programmatic Usage

The `watch()` function can be called directly from Python if you want to embed the watcher in a larger program:

```python
from bandcamp_wacz.email_watcher import watch

# Block forever; Ctrl-C exits cleanly
watch(
    no_upload=False,
    dry_run=False,
    debug=True,
    lax=False,
    force_full=False,
)
```

All parameters map directly to the CLI flags of the same name.

---

## Internal Components Reference

| Function | Description |
|---|---|
| `watch(...)` | Main entry point. Connects, drains inbox, enters the IDLE loop, and reconnects on error |
| `_process_inbox(imap, ...)` | Fetches all inbox messages, groups by artist, calls `_run_pipeline` once per artist, moves successful emails to Trash |
| `_run_pipeline(urls, ...)` | Builds and runs the `archive.py` subprocess command. Decides `--quick` vs full based on `_artist_json_exists` |
| `_artist_json_exists(artist_root)` | Fetches the artist page, extracts `band_id`, checks whether a JSON already exists in `ARTISTS_DIR` |
| `_idle(imap, timeout)` | Issues IMAP IDLE, blocks until new mail or timeout, sends `DONE` and cleans up properly |
| `_fetch_inbox(imap)` | Returns all `(uid, message)` pairs from the currently selected mailbox |
| `_is_bandcamp_notification(msg, lax)` | Checks sender and subject to confirm a message is a Bandcamp release notification |
| `_extract_urls(msg)` | Extracts and deduplicates `bandcamp.com/album/` and `/track/` URLs from the plain-text body |
| `_move_email(imap, uid, dest)` | Copies an email to `dest`, marks the original deleted, and expunges |
| `_strip_query(url)` | Removes query string and fragment from a URL before archiving |
| `_to_artist_root(url)` | Derives the artist root URL from a release URL, e.g. `https://artist.bandcamp.com/` |
