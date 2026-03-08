#!/usr/bin/env python3
"""
update_metadata.py - check Bandcamp for new or changed releases and update artist JSONs.

Compares live Bandcamp pages against stored artist JSONs. For new releases, adds
them and appends their URLs to bandcamp-dump.lst. For changed releases, updates
the fields in place and records the old values in a _history list.

Watched fields: title, artist, coverUrl_0, trackinfo
  (trackinfo compared on: title, duration, track_id, url, trackCoverUrl_0, track count)

Pipeline state fields (archived, uploaded, archived_at, uploaded_at, pd_wacz_id) are never overwritten.

Also used by the email watcher (Step 6): when a Bandcamp new-release email is
received, the album URL is passed here, which strips it to the artist root and
runs a full update. If the artist is not yet in artists/, it aborts with a message
to run fetch_metadata.py first.

Usage
─────
  python update_metadata.py https://artist.bandcamp.com/
  python update_metadata.py https://artist.bandcamp.com/album/some-album
  python update_metadata.py https://artist-a.bandcamp.com/ https://artist-b.bandcamp.com/
  python update_metadata.py https://artist.bandcamp.com/ --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urlunparse

# fetch_metadata.py lives at the project root alongside this script.
# Add the project root to sys.path so we can import from it.
_PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_PROJECT_ROOT))

from fetch_metadata import (
    Bandcamp,
    artist_folder_name,
    create_safe_filename,
    is_artist_page,
)
from bandcamp_wacz.config import ARTISTS_DIR, BC_MAX_RETRIES

logger = logging.getLogger(__name__)

# Fields on the album dict that are compared for change detection
WATCHED_FIELDS = {"title", "artist", "coverUrl_0", "is_preorder"}

# Fields within each trackinfo entry that are compared
WATCHED_TRACK_FIELDS = {"title", "duration", "track_id", "url", "trackCoverUrl_0"}

# Pipeline state fields - never overwritten by metadata updates
STATE_FIELDS = {"archived", "uploaded", "archived_at", "uploaded_at", "pd_wacz_id"}


# ── URL helpers ───────────────────────────────────────────────────────────────

def to_artist_root_url(url: str) -> str:
    """
    Strip any path beyond the subdomain to get the artist root URL.
    e.g. https://artist.bandcamp.com/album/foo → https://artist.bandcamp.com/
    """
    parsed = urlparse(url)
    return urlunparse(parsed._replace(path="/", query="", fragment=""))


# ── Artist folder lookup ──────────────────────────────────────────────────────

def find_artist_folder(band_id: int) -> Optional[Path]:
    """Find the artist folder in ARTISTS_DIR whose name ends with [band_id]."""
    if not ARTISTS_DIR.exists():
        return None
    for folder in ARTISTS_DIR.iterdir():
        if folder.is_dir() and folder.name.endswith(f"[{band_id}]"):
            return folder
    return None


def load_artist_json(folder: Path) -> Optional[tuple[Path, dict]]:
    """Load the artist JSON from the folder. Returns (path, data) or None."""
    json_path = folder / f"{folder.name}.json"
    if not json_path.exists():
        logger.error("Artist JSON not found: %s", json_path)
        return None
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        return json_path, data
    except Exception as exc:
        logger.error("Could not read artist JSON %s: %s", json_path, exc)
        return None


# ── Change detection ──────────────────────────────────────────────────────────

def _trackinfo_changed(old: list, new: list) -> tuple[bool, dict]:
    """
    Compare two trackinfo lists.
    Returns (changed, {field: old_value}) where field is "trackinfo" if changed.
    """
    if len(old) != len(new):
        return True, {"trackinfo": old}

    for old_t, new_t in zip(old, new):
        for field in WATCHED_TRACK_FIELDS:
            if old_t.get(field) != new_t.get(field):
                return True, {"trackinfo": old}

    return False, {}


def detect_changes(existing: dict, fresh: dict) -> dict:
    """
    Compare existing and fresh album dicts on watched fields.
    Returns a dict of {field: old_value} for every field that changed.
    """
    changed = {}

    for field in WATCHED_FIELDS:
        if existing.get(field) != fresh.get(field):
            changed[field] = existing.get(field)

    ti_changed, ti_old = _trackinfo_changed(
        existing.get("trackinfo") or [],
        fresh.get("trackinfo") or [],
    )
    if ti_changed:
        changed.update(ti_old)

    return changed


def apply_changes(existing: dict, fresh: dict, changed_fields: dict) -> dict:
    """
    Update existing album dict with new values for changed fields.
    Preserves STATE_FIELDS. Appends old values to _history.
    Resets archived, uploaded, archived_at, uploaded_at, and pd_wacz_id so
    the album is re-queued for archival and re-upload. Moves the previous state
    to history so the old IA item and timestamps are traceable.
    """
    now = datetime.now(timezone.utc).isoformat()

    history_entry = {
        "changed_at": now,
        "archived_at_change": bool(existing.get("archived")),
        "uploaded_at_change": bool(existing.get("uploaded")),
        "pd_wacz_id_at_change": existing.get("pd_wacz_id"),
        "fields": changed_fields,
    }
    existing.setdefault("_history", []).append(history_entry)

    all_fields = WATCHED_FIELDS | {"trackinfo"}
    for field in all_fields:
        if field in changed_fields:
            existing[field] = fresh.get(field)

    # Reset pipeline state so the updated release is re-queued for crawling and upload
    existing["archived"]      = False
    existing["uploaded"]      = False
    existing["archived_at"]   = None
    existing["uploaded_at"]   = None
    existing["pd_wacz_id"]  = None

    return existing


# ── Core update logic ─────────────────────────────────────────────────────────

def update_artist(
    artist_url: str,
    scraper: Bandcamp,
    dry_run: bool,
    original_urls: list[str] | None = None,
) -> bool:
    """
    Fetch the artist page, compare against stored JSON, write updates.
    Returns True on success, False on failure.

    original_urls: if provided and the /music page returns nothing (single-release
    artist), these are used as the live URL list instead.
    """
    artist_root = to_artist_root_url(artist_url)
    logger.info("Updating artist: %s", artist_root)

    # ── Fetch band_id from the artist page ───────────────────────────────────
    try:
        resp = scraper._get(artist_root)
        soup = scraper._make_soup(resp.text)
        page_json = scraper._merged_json(soup)
        band_id = scraper.get_band_id(soup, page_json)
    except Exception as exc:
        logger.error("Could not fetch artist page %s: %s", artist_root, exc)
        return False

    if not band_id:
        logger.error(
            "Could not determine band_id from %s. "
            "Cannot locate artist folder without it.",
            artist_root,
        )
        return False

    # ── Locate existing artist folder ────────────────────────────────────────
    folder = find_artist_folder(band_id)
    if not folder:
        logger.error(
            "No artist folder found for band_id=%s (%s). "
            "Run fetch_metadata.py first to create it.",
            band_id, artist_root,
        )
        return False

    result = load_artist_json(folder)
    if not result:
        return False
    json_path, data = result

    # Artist key is the first non-underscore key in the JSON
    artist_key = next((k for k in data if not k.startswith("_")), None)
    if not artist_key:
        logger.error("Malformed artist JSON - no artist key found in %s", json_path)
        return False

    existing_releases: list[dict] = data[artist_key]
    existing_by_id: dict[int, dict] = {
        r["item_id"]: r for r in existing_releases if r.get("item_id")
    }

    # ── Discover all current releases on Bandcamp ────────────────────────────
    try:
        live_urls = scraper.get_album_urls_from_artist_page(artist_root)
    except Exception as exc:
        logger.error("Could not fetch release list for %s: %s", artist_root, exc)
        return False

    if not live_urls:
        # Single-release artists have no /music grid - fall back to original URLs
        if original_urls:
            logger.info(
                "No music grid found - using provided URLs as release list: %s",
                original_urls,
            )
            live_urls = original_urls
        else:
            logger.warning("No releases found on %s", artist_root)
            return True

    # ── Process each live release ─────────────────────────────────────────────
    new_releases: list[dict] = []
    updated_count = 0
    dry_run_new_count = 0
    new_listable_urls: list[str] = []

    for i, url in enumerate(sorted(live_urls), 1):
        print(f"\n--- Release {i}/{len(live_urls)}: {url} ---")
        try:
            fresh = scraper.parse(url)
        except KeyboardInterrupt:
            raise KeyboardInterrupt() from None
        except Exception as exc:
            logger.warning("Could not parse %s: %s", url, exc)
            continue

        if not fresh:
            continue

        item_id = fresh.get("item_id")

        if item_id and item_id in existing_by_id:
            # ── Existing release: check for changes ───────────────────────────
            existing = existing_by_id[item_id]
            changed = detect_changes(existing, fresh)

            if changed:
                field_names = ", ".join(changed.keys())
                if dry_run:
                    print(f"  [DRY RUN] Would update '{fresh.get('title')}': {field_names}")
                else:
                    apply_changes(existing, fresh, changed)
                    updated_count += 1
                    logger.info(
                        "Updated '%s' (item_id=%s): %s",
                        fresh.get("title"), item_id, field_names,
                    )
            else:
                logger.debug("No changes for '%s' (item_id=%s)", fresh.get("title"), item_id)

        else:
            # ── New release ───────────────────────────────────────────────────
            fresh.setdefault("archived", False)
            fresh.setdefault("uploaded", False)

            if dry_run:
                status = "pre-order" if fresh.get("is_preorder") else "new"
                print(f"  [DRY RUN] Would add '{fresh.get('title')}' [{item_id}] ({status})")
                dry_run_new_count += 1
            else:
                new_releases.append(fresh)
                logger.info("New release: '%s' (item_id=%s)", fresh.get("title"), item_id)

            new_listable_urls.append(url)

    if dry_run:
        print(f"\n  [DRY RUN] {dry_run_new_count} new, {updated_count} updated - nothing written.")
        return True

    # ── Write updates ─────────────────────────────────────────────────────────
    if not new_releases and updated_count == 0:
        logger.info("Nothing to update for %s", artist_key)
        return True

    data[artist_key] = existing_releases + new_releases

    try:
        json_path.write_text(
            json.dumps(data, indent=4, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info(
            "Saved artist JSON: %s (%d new, %d updated)",
            json_path, len(new_releases), updated_count,
        )
    except OSError as exc:
        logger.error("Could not write artist JSON %s: %s", json_path, exc)
        return False

    # ── Append new listable URLs to bandcamp-dump.lst ────────────────────────
    if new_listable_urls:
        lst_path = folder / "bandcamp-dump.lst"
        try:
            existing_lst: set[str] = set()
            if lst_path.exists():
                existing_lst = set(lst_path.read_text(encoding="utf-8").splitlines())

            truly_new = [u for u in new_listable_urls if u not in existing_lst]
            if truly_new:
                with lst_path.open("a", encoding="utf-8") as f:
                    for u in truly_new:
                        f.write(u + "\n")
                logger.info("Appended %d URL(s) to %s", len(truly_new), lst_path)
        except OSError as exc:
            logger.error("Could not update bandcamp-dump.lst: %s", exc)

    return True


def update_release(
    release_url: str,
    scraper: Bandcamp,
    dry_run: bool,
) -> bool:
    """
    Fetch and update metadata for a single release URL only, without fetching
    the full artist discography. Useful for re-queuing a specific release after
    its WACZ goes missing, or for targeted manual updates.

    The release must already exist in the artist JSON (band_id is extracted
    from the page to locate the folder).
    Returns True on success, False on failure.
    """
    logger.info("Updating single release: %s", release_url)

    # Parse the release page to get fresh metadata and band_id
    print(f"\n--- Release: {release_url} ---")
    try:
        fresh = scraper.parse(release_url)
    except KeyboardInterrupt:
        raise KeyboardInterrupt() from None
    except Exception as exc:
        logger.error("Could not parse %s: %s", release_url, exc)
        return False

    if not fresh:
        logger.error("No data returned for %s", release_url)
        return False

    band_id = fresh.get("band_id")
    item_id = fresh.get("item_id")

    if not band_id:
        logger.error("Could not determine band_id from %s", release_url)
        return False

    folder = find_artist_folder(band_id)
    if not folder:
        logger.error(
            "No artist folder found for band_id=%s. Run fetch_metadata.py first.",
            band_id,
        )
        return False

    result = load_artist_json(folder)
    if not result:
        return False
    json_path, data = result

    artist_key = next((k for k in data if not k.startswith("_")), None)
    if not artist_key:
        logger.error("Malformed artist JSON - no artist key found in %s", json_path)
        return False

    existing_releases: list[dict] = data[artist_key]
    existing_by_id: dict[int, dict] = {
        r["item_id"]: r for r in existing_releases if r.get("item_id")
    }

    if item_id and item_id in existing_by_id:
        existing = existing_by_id[item_id]
        changed = detect_changes(existing, fresh)
        if changed:
            field_names = ", ".join(changed.keys())
            if dry_run:
                print(f"  [DRY RUN] Would update '{fresh.get('title')}': {field_names}")
            else:
                apply_changes(existing, fresh, changed)
                logger.info("Updated '%s' (item_id=%s): %s", fresh.get("title"), item_id, field_names)
        else:
            logger.info("No changes for '%s' (item_id=%s)", fresh.get("title"), item_id)
    else:
        # Release not in JSON yet - add it
        fresh.setdefault("archived", False)
        fresh.setdefault("uploaded", False)
        if dry_run:
            print(f"  [DRY RUN] Would add '{fresh.get('title')}' [{item_id}]")
        else:
            existing_releases.append(fresh)
            logger.info("Added new release: '%s' (item_id=%s)", fresh.get("title"), item_id)

    if dry_run:
        return True

    try:
        json_path.write_text(
            json.dumps(data, indent=4, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("Saved artist JSON: %s", json_path)
    except OSError as exc:
        logger.error("Could not write artist JSON %s: %s", json_path, exc)
        return False

    return True




def main() -> None:
    parser = argparse.ArgumentParser(
        prog="update_metadata.py",
        description="Check Bandcamp for new/changed releases and update the artist JSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "urls",
        nargs="+",
        metavar="URL",
        help="One or more Bandcamp artist or album URLs.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing anything.",
    )
    parser.add_argument(
        "-d", "--delay",
        type=str,
        default=None,
        metavar="MS",
        help="Delay between requests in ms. Single value or range e.g. '1000-3000'.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=None,
        help=f"Max retries on failed requests (default: BC_MAX_RETRIES from env, currently {BC_MAX_RETRIES}).",
    )
    parser.add_argument(
        "--release",
        action="store_true",
        help=(
            "Treat each URL as a specific release to update individually, "
            "without fetching the full artist discography. "
            "Useful for re-queuing a release after its WACZ goes missing."
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose debug logging.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    scraper = Bandcamp(delay_arg=args.delay, retries=args.retries)

    ok = 0
    fail = 0

    if args.release:
        # Per-release mode: update each URL individually without full artist fetch
        for url in args.urls:
            success = update_release(url, scraper, dry_run=args.dry_run)
            if success:
                ok += 1
            else:
                fail += 1
    else:
        # Default: deduplicate to artist root URLs, update full discography
        artist_roots: dict[str, str] = {}  # root_url → original_url
        for url in args.urls:
            root = to_artist_root_url(url)
            if root not in artist_roots:
                artist_roots[root] = url

        for root_url in artist_roots:
            success = update_artist(root_url, scraper, dry_run=args.dry_run)
            if success:
                ok += 1
            else:
                fail += 1

    print(f"\n── Summary ─────────────────────────────")
    print(f"  Succeeded : {ok}")
    print(f"  Failed    : {fail}")
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
