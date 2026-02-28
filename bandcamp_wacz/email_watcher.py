"""
email_watcher.py - Step 6: IMAP watcher for Bandcamp new-release notifications.

Connects to the configured IMAP inbox and waits for Bandcamp new-release emails
using IMAP IDLE (push, not polling). When one arrives, it extracts all album/track
URLs, passes them to the archive.py smart pipeline, then deletes the email by moving it to Trash.

Bandcamp sends two subject patterns:
  "New release from <Artist>"   (single release)
  "New releases from <Artist>"  (multiple releases in one email)

Both are handled identically — all URLs in the email are extracted and passed
to archive.py as a single invocation, which deduplicates to one artist naturally.

If the artist is not yet in artists/, archive.py's smart pipeline handles
onboarding automatically by running fetch_metadata.py first.

Usage
─────
  python bandcamp_wacz/email_watcher.py
  python bandcamp_wacz/email_watcher.py --no-upload
  python bandcamp_wacz/email_watcher.py --dry-run
  python bandcamp_wacz/email_watcher.py --debug
  python bandcamp_wacz/email_watcher.py --full   # force full pipeline even if artist JSON exists

Configuration (.env)
─────────────────────
  EMAIL_ADDRESS   IMAP login address
  EMAIL_PASSWORD  App password
  IMAP_SERVER     Hostname (default: imap.gmail.com)
  IMAP_PORT       Port     (default: 993)
"""

from __future__ import annotations

import argparse
import email
import email.policy
import imaplib
import logging
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse

# ── Project root on sys.path ──────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from bandcamp_wacz.config import (
    EMAIL_ADDRESS, EMAIL_PASSWORD, IMAP_SERVER, IMAP_PORT, ARTISTS_DIR,
)

# ── Artist JSON helpers ───────────────────────────────────────────────────────

def _artist_json_exists(artist_root: str) -> bool:
    """
    Return True if a <Name> [band_id]/<Name> [band_id].json file already exists
    for this artist root URL.

    Looks for any subdirectory of ARTISTS_DIR whose folder name ends with
    '[<band_id>]' where band_id is extracted from the artist's Bandcamp page —
    same logic used by archive.py's smart pipeline.  If ARTISTS_DIR doesn't
    exist yet, or if the network call fails, returns False so we always fall
    back to the full pipeline safely.
    """
    if not ARTISTS_DIR.exists():
        return False

    try:
        from urllib.request import urlopen
        import json as _json, re as _re

        html = urlopen(artist_root, timeout=15).read().decode("utf-8", errors="replace")

        # Try data-blob attribute first (newer Bandcamp pages)
        m = _re.search(r'data-blob="([^"]+)"', html)
        band_id: int | None = None
        if m:
            try:
                blob = _json.loads(m.group(1).replace("&quot;", '"'))
                band_id = int(blob.get("id", 0)) or None
            except Exception:
                pass

        # Fallback: band_id= query param in page source
        if band_id is None:
            m2 = _re.search(r'band_id=(\d+)', html)
            if m2:
                band_id = int(m2.group(1))

        if band_id is None:
            logger.debug("Could not extract band_id for %s — assuming no JSON.", artist_root)
            return False

        for folder in ARTISTS_DIR.iterdir():
            if folder.is_dir() and folder.name.endswith(f"[{band_id}]"):
                json_path = folder / f"{folder.name}.json"
                if json_path.exists():
                    logger.debug("Artist JSON found: %s", json_path)
                    return True

        logger.debug("No artist JSON found for band_id=%d (%s).", band_id, artist_root)
        return False

    except Exception as exc:
        logger.warning("Could not check artist JSON for %s: %s — will run full pipeline.", artist_root, exc)
        return False

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

BANDCAMP_SENDER = "noreply@bandcamp.com"
SUBJECT_RE      = re.compile(r"New releases? from .+", re.IGNORECASE)

# Matches both standard Bandcamp subdomains (artist.bandcamp.com) and custom
# domains (e.g. music.bucketheadpikes.com). The sender + subject checks
# upstream are the real guard against false positives — by the time this
# regex runs we already know the email is a genuine Bandcamp notification.
URL_RE          = re.compile(
    r"https?://[a-z0-9][a-z0-9\-]*(?:\.[a-z0-9\-]+)+/(?:album|track)/[^\s?#]+",
    re.IGNORECASE,
)

FOLDER_INBOX    = "INBOX"
FOLDER_TRASH   = "Trash"


RECONNECT_DELAY = 30
IDLE_TIMEOUT    = 25 * 60  # re-issue IDLE every 25 min (RFC recommends < 29 min)


# ── Desktop notifications ─────────────────────────────────────────────────────

class NotifyHandler(logging.Handler):
    """
    Logging handler that fires a desktop notification (via notify-send) for
    every ERROR or CRITICAL log record.  Silently does nothing if notify-send
    is not on PATH or if no display session is available (e.g. a headless run).
    Enable with --notify on the command line.
    """

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.ERROR:
            return
        try:
            import shutil, subprocess as _sp
            if not shutil.which("notify-send"):
                return
            urgency = "critical" if record.levelno >= logging.CRITICAL else "normal"
            summary = f"Bandcamp Watcher — {record.levelname}"
            body    = self.format(record)[:200]  # notify-send truncates long bodies anyway
            _sp.run(
                ["notify-send", "-u", urgency, "-a", "bandcamp-watcher", summary, body],
                timeout=5,
                capture_output=True,
            )
        except Exception:
            pass  # never let the notification handler crash the watcher


# ── Email parsing ─────────────────────────────────────────────────────────────

def _strip_query(url: str) -> str:
    p = urlparse(url)
    return urlunparse(p._replace(query="", fragment=""))


def _is_bandcamp_notification(msg: email.message.Message, lax: bool = False) -> bool:
    """
    Return True if this looks like a Bandcamp new-release notification.
    lax=True skips the sender check and accepts subjects containing the
    normal pattern anywhere (e.g. forwarded messages with "Fwd:" prefix).
    """
    sender  = msg.get("From", "")
    subject = msg.get("Subject", "")
    if lax:
        return bool(SUBJECT_RE.search(subject))
    return BANDCAMP_SENDER in sender and bool(SUBJECT_RE.match(subject))


def _extract_urls(msg: email.message.Message) -> list[str]:
    """Extract and deduplicate Bandcamp album/track URLs from the plain-text body."""
    body = ""
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            payload = part.get_payload(decode=True)
            if payload:
                body = payload.decode(
                    part.get_content_charset() or "utf-8", errors="replace"
                )
            break

    seen: set[str] = set()
    urls: list[str] = []
    for raw_url in URL_RE.findall(body):
        clean = _strip_query(raw_url)
        if clean not in seen:
            seen.add(clean)
            urls.append(clean)
    return urls


def _to_artist_root(url: str) -> str:
    p = urlparse(url)
    return urlunparse(p._replace(path="/", query="", fragment=""))


# ── IMAP helpers ──────────────────────────────────────────────────────────────

def _move_email(imap: imaplib.IMAP4_SSL, uid: bytes, dest_folder: str) -> None:
    """Copy email to dest_folder then delete the original."""
    try:
        imap.uid("COPY", uid, f'"{dest_folder}"')
        imap.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
        imap.expunge()
        logger.debug("Moved email uid=%s → %s", uid.decode(), dest_folder)
    except Exception as exc:
        logger.warning("Could not move email uid=%s to %s: %s", uid, dest_folder, exc)


def _fetch_inbox(imap: imaplib.IMAP4_SSL) -> list[tuple[bytes, email.message.Message]]:
    """Return (uid, message) pairs for all messages in the selected mailbox."""
    typ, data = imap.uid("SEARCH", None, "ALL")
    if typ != "OK" or not data[0]:
        return []

    results = []
    for uid in data[0].split():
        typ2, msg_data = imap.uid("FETCH", uid, "(RFC822)")
        if typ2 != "OK" or not msg_data or not msg_data[0]:
            continue
        raw = msg_data[0][1]
        if not isinstance(raw, bytes):
            continue
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        results.append((uid, msg))
    return results


def _idle(imap: imaplib.IMAP4_SSL, timeout: int) -> None:
    """
    Issue IMAP IDLE and block until the server signals new mail or timeout.
    Falls back to a plain sleep if the server doesn't advertise IDLE support.

    Uses imaplib's own tag generator so its internal state stays consistent —
    raw hardcoded tags like "A001" collide with imaplib's counter and cause
    "unexpected response" aborts on the next command.
    """
    typ, caps = imap.capability()
    cap_str = caps[0].decode() if caps and caps[0] else ""
    if "IDLE" not in cap_str.upper():
        logger.debug("Server does not support IDLE — sleeping %ds.", timeout)
        time.sleep(timeout)
        return

    # Borrow imaplib's tag so it stays in sync with its internal counter
    tag = imap._new_tag().decode()
    logger.debug("Entering IMAP IDLE tag=%s (timeout=%ds)…", tag, timeout)
    imap.send(f"{tag} IDLE\r\n".encode())
    imap.readline()  # consume "+ idling" continuation

    # IDLE manages its own timeout — clear the general one first
    imap.socket().settimeout(None)
    imap.socket().settimeout(timeout)
    try:
        while True:
            line = imap.readline()
            if not line:
                break
            decoded = line.decode("utf-8", errors="replace").strip()
            logger.debug("IDLE recv: %s", decoded)
            if "EXISTS" in decoded or "RECENT" in decoded:
                logger.debug("New mail signalled — leaving IDLE.")
                break
    except socket.timeout:
        logger.debug("IDLE timeout — re-issuing.")
    except Exception as exc:
        logger.debug("IDLE interrupted: %s", exc)
    finally:
        imap.socket().settimeout(None)
        # Send DONE and consume the tagged OK so imaplib's state stays clean
        try:
            imap.send(b"DONE\r\n")
            while True:
                line = imap.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").strip()
                logger.debug("IDLE done recv: %s", decoded)
                if decoded.startswith(tag):
                    break
        except Exception:
            pass
        # Restore the general command timeout after IDLE exits
        try:
            imap.socket().settimeout(60)
        except Exception:
            pass


# ── Pipeline ──────────────────────────────────────────────────────────────────

def _run_pipeline(
    urls: list[str],
    no_upload: bool,
    dry_run: bool,
    debug: bool,
    force_full: bool = False,
    artist_root: str | None = None,
    one_by_one: bool = True,
) -> bool:
    """
    Invoke archive.py --url <urls…> as a subprocess.

    Passes ``--quick`` when:
      - ``force_full`` is False, AND
      - the artist already has a JSON file in artists/

    If the artist JSON does not exist yet (new artist), the full pipeline is
    always used so that fetch_metadata.py runs and the JSON is created.
    ``--one-by-one`` is passed for new artists by default so that each release
    is uploaded immediately after crawling, avoiding accumulating a full
    discography's worth of WACZ files when the user may not be monitoring disk
    usage. Pass ``one_by_one=False`` (via --no-one-by-one) to disable this.

    Returns True on success, False on failure.
    """
    if dry_run:
        logger.info("[DRY RUN] Would run pipeline for: %s", urls)
        return True

    # Decide whether to pass --quick
    use_quick = False
    if not force_full and artist_root is not None:
        use_quick = _artist_json_exists(artist_root)
        logger.info(
            "Artist JSON %s for %s — running %s pipeline.",
            "found" if use_quick else "not found",
            artist_root,
            "quick" if use_quick else "full",
        )
    elif force_full:
        logger.info("--full flag set — running full pipeline for %s.", urls)

    cmd = [sys.executable, str(_PROJECT_ROOT / "archive.py"), "--url"] + urls
    if use_quick:
        cmd.append("--quick")
    else:
        # Full pipeline (new artist): archive one release at a time so the
        # user does not need to have a full discography's worth of free disk.
        if one_by_one:
            cmd.append("--one-by-one")
    if no_upload:
        cmd.append("--no-upload")
    if debug:
        cmd.append("--debug")

    logger.info("Launching pipeline: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, cwd=str(_PROJECT_ROOT))
        if result.returncode == 0:
            logger.info("Pipeline succeeded for %d URL(s).", len(urls))
            return True
        logger.error("Pipeline exited %d for: %s", result.returncode, urls)
        return False
    except Exception as exc:
        logger.error("Pipeline subprocess error: %s", exc)
        return False


# ── Inbox processing ──────────────────────────────────────────────────────────

def _process_inbox(
    imap: imaplib.IMAP4_SSL,
    no_upload: bool,
    dry_run: bool,
    debug: bool,
    lax: bool = False,
    force_full: bool = False,
    one_by_one: bool = True,
) -> None:
    """
    Fetch all messages in the inbox, group release URLs by artist, run one
    pipeline call per artist, then move successful emails to Trash.
    Non-Bandcamp emails and failures are left in the inbox untouched.

    Multiple emails from the same artist (e.g. two album releases on the same
    day) are merged into a single batched pipeline call so the artist JSON is
    only updated once and all new releases are archived together.
    """
    imap.select(FOLDER_INBOX)
    messages = _fetch_inbox(imap)
    if not messages:
        return

    logger.info("%d message(s) in inbox.", len(messages))

    # artist_root → list of release URLs
    batches: dict[str, list[str]] = {}
    # artist_root → list of UIDs (to move to Trash on success)
    uid_map: dict[str, list[bytes]] = {}

    for uid, msg in messages:
        subject = msg.get("Subject", "")
        sender  = msg.get("From", "")

        if not _is_bandcamp_notification(msg, lax=lax):
            logger.debug("Skipping (not a Bandcamp notification): '%s' from '%s'", subject, sender)
            continue

        logger.info("Email: '%s'", subject)
        urls = _extract_urls(msg)

        if not urls:
            logger.warning("No URLs found in '%s' — moving to Trash.", subject)
            _move_email(imap, uid, FOLDER_TRASH)
            continue

        logger.info("  URLs: %s", urls)

        # Group all URLs by their artist root
        # For a Body 13 email with 7 releases, all share the same root
        roots = {_to_artist_root(u) for u in urls}
        for root in roots:
            root_urls = [u for u in urls if _to_artist_root(u) == root]
            batches.setdefault(root, [])
            for u in root_urls:
                if u not in batches[root]:
                    batches[root].append(u)
            uid_map.setdefault(root, []).append(uid)

    # One pipeline call per artist
    for artist_root, release_urls in batches.items():
        logger.info("Artist root: %s (%d release(s))", artist_root, len(release_urls))
        success = _run_pipeline(
            release_urls,
            no_upload=no_upload,
            dry_run=dry_run,
            debug=debug,
            force_full=force_full,
            artist_root=artist_root,
            one_by_one=one_by_one,
        )
        if success:
            for uid in uid_map.get(artist_root, []):
                _move_email(imap, uid, FOLDER_TRASH)
        else:
            logger.error(
                "Pipeline failed for %s — email(s) left in inbox for retry on next cycle.",
                artist_root,
            )


# ── Main loop ─────────────────────────────────────────────────────────────────

def watch(no_upload: bool = False, dry_run: bool = False, debug: bool = False, lax: bool = False, force_full: bool = False, one_by_one: bool = True) -> None:
    """Connect, drain any waiting emails, then IDLE forever. Reconnects on error."""
    if not EMAIL_ADDRESS or not EMAIL_PASSWORD:
        logger.error(
            "EMAIL_ADDRESS and EMAIL_PASSWORD must be set in .env — see .env.example."
        )
        sys.exit(1)

    logger.info("Email watcher starting — %s @ %s:%d", EMAIL_ADDRESS, IMAP_SERVER, IMAP_PORT)
    if dry_run:
        logger.info("[DRY RUN] No pipeline calls or email moves will happen.")
    if force_full:
        logger.info("[FULL MODE] --quick will never be passed; full pipeline always runs.")
    if not one_by_one:
        logger.info("[NO-ONE-BY-ONE] Full discography will be crawled before uploading.")
    if lax:
        logger.warning(
            "[LAX MODE] Sender check disabled — any email whose subject contains "
            "'New release(s) from ...' will be processed. For testing only."
        )

    while True:
        imap = None
        try:
            logger.info("Connecting to %s:%d…", IMAP_SERVER, IMAP_PORT)
            imap = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
            # General command timeout — prevents any IMAP call from hanging forever.
            # IDLE overrides this with its own longer timeout during the wait loop.
            imap.socket().settimeout(60)
            imap.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
            logger.info("Connected.")


            # Drain anything that arrived while we were offline
            _process_inbox(imap, no_upload=no_upload, dry_run=dry_run, debug=debug, lax=lax, force_full=force_full, one_by_one=one_by_one)

            # Then IDLE and wake on new mail
            while True:
                imap.select(FOLDER_INBOX)
                _idle(imap, timeout=IDLE_TIMEOUT)
                _process_inbox(imap, no_upload=no_upload, dry_run=dry_run, debug=debug, lax=lax, force_full=force_full, one_by_one=one_by_one)

        except KeyboardInterrupt:
            logger.info("Shutting down.")
            break
        except imaplib.IMAP4.abort as exc:
            logger.warning("Connection aborted: %s — reconnecting in %ds…", exc, RECONNECT_DELAY)
        except imaplib.IMAP4.error as exc:
            logger.error("IMAP error: %s — reconnecting in %ds…", exc, RECONNECT_DELAY)
        except OSError as exc:
            logger.error("Network error: %s — reconnecting in %ds…", exc, RECONNECT_DELAY)
        except Exception as exc:
            logger.error("Unexpected error: %s — reconnecting in %ds…", exc, RECONNECT_DELAY, exc_info=True)
        finally:
            if imap:
                try:
                    imap.logout()
                except Exception:
                    pass

        try:
            time.sleep(RECONNECT_DELAY)
        except KeyboardInterrupt:
            logger.info("Shutting down.")
            break


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="email_watcher.py",
        description="Watch for Bandcamp new-release emails and trigger the archive pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--no-upload",
        action="store_true",
        help="Pass --no-upload to archive.py (crawl but skip archive.org upload).",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help=(
            "Always run the full pipeline (never pass --quick to archive.py), "
            "even when the artist JSON already exists. By default, --quick is "
            "used for known artists to skip the metadata re-fetch."
        ),
    )
    parser.add_argument(
        "--lax",
        action="store_true",
        help=(
            "Disable the sender check and match subjects containing 'New release(s) from ...' "
            "anywhere (including forwarded messages). For testing only — do not leave enabled."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse emails and log what would happen without running the pipeline or moving messages.",
    )
    parser.add_argument(
        "--no-one-by-one",
        action="store_true",
        dest="no_one_by_one",
        help=(
            "When onboarding a new artist, crawl the entire discography before "
            "uploading anything, instead of uploading each release immediately "
            "after crawling it. The default (one-by-one) is safer when you are "
            "not monitoring disk usage, but this flag is faster if you have "
            "plenty of free space."
        ),
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        help=(
            "Send a desktop notification (via notify-send) whenever an ERROR or "
            "CRITICAL message is logged. Requires libnotify and an active desktop "
            "session. No-op if notify-send is not on PATH."
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose debug logging (also passed through to archive.py).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.notify:
        _notify_handler = NotifyHandler()
        _notify_handler.setFormatter(logging.Formatter(
            "%(asctime)s  %(name)s  %(message)s", datefmt="%H:%M:%S"
        ))
        logging.getLogger().addHandler(_notify_handler)
        logging.getLogger(__name__).info("Desktop notifications enabled (notify-send).")

    watch(no_upload=args.no_upload, dry_run=args.dry_run, debug=args.debug, lax=args.lax, force_full=args.full, one_by_one=not args.no_one_by_one)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
