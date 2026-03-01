#!/usr/bin/env python3
"""
upload.py - upload Bandcamp WACZ files and release JSONs to archive.org.

Finds WACZ/JSON pairs in the input path, uploads both files to a single IA item
per release, marks the release as uploaded in the artist JSON, then deletes the
local WACZ and JSON.

Usage
─────
  # Upload everything in wacz_output/:
  python upload.py wacz_output/

  # Upload a single WACZ (its .json must be alongside it):
  python upload.py "wacz_output/Album Title [item_id].wacz"

  # Preview without uploading or deleting:
  python upload.py wacz_output/ --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from bandcamp_wacz.config import IA_ACCESS_KEY, IA_SECRET_KEY, IA_COLLECTION, ARTISTS_DIR, IA_MAX_RETRIES, IA_RETRY_DELAY

logger = logging.getLogger(__name__)


# ── IA helpers ────────────────────────────────────────────────────────────────

def _ia_session():
    """Return an authenticated internetarchive session."""
    try:
        import internetarchive as ia
    except ImportError:
        logger.error("internetarchive not installed. Run: pip install internetarchive")
        sys.exit(1)

    if not IA_ACCESS_KEY or not IA_SECRET_KEY:
        logger.error(
            "IA_ACCESS_KEY and IA_SECRET_KEY must be set in .env to upload."
        )
        sys.exit(1)

    return ia.get_session(config={
        "s3": {"access": IA_ACCESS_KEY, "secret": IA_SECRET_KEY}
    })


def _build_ia_metadata(release: dict) -> dict:
    """Map release JSON fields to IA item metadata."""
    item_id     = release.get("item_id", "")
    album_title = release.get("title", "")
    date        = ""
    date_str    = release.get("datePublished") or ""
    m = re.search(r'\b(\d{4})\b', date_str)
    if m:
        date = m.group(1)

    return {
        "title":        release.get("ia_identifier", "Untitled"),
        "creator":      release.get("artist", ""),
        "artist":       release.get("artist", ""),
        "album_title":  album_title,
        "mediatype":    "data",
        "collection":   IA_COLLECTION,
        "date":         date,
        "original_url": release.get("url", ""),
        "band_id":      str(release.get("band_id", "")),
        "item_id":      str(item_id),
    }


# ── Artist JSON update ────────────────────────────────────────────────────────

def _find_artist_json(band_id: int) -> Optional[Path]:
    if not ARTISTS_DIR.exists():
        return None
    for folder in ARTISTS_DIR.iterdir():
        if folder.is_dir() and folder.name.endswith(f"[{band_id}]"):
            candidate = folder / f"{folder.name}.json"
            if candidate.exists():
                return candidate
    return None


def _find_release_in_artist_json(band_id: int, item_id: int) -> Optional[dict]:
    """Look up and return a release dict from the artist JSON by item_id."""
    json_path = _find_artist_json(band_id)
    if not json_path:
        logger.warning("Could not find artist JSON for band_id=%s.", band_id)
        return None

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("Could not read artist JSON: %s", exc)
        return None

    for key, releases in data.items():
        if key.startswith("_") or not isinstance(releases, list):
            continue
        for release in releases:
            if release.get("item_id") == item_id:
                return release

    logger.warning("item_id=%s not found in artist JSON for band_id=%s.", item_id, band_id)
    return None


def _mark_uploaded(band_id: int, item_id: int, identifier: str) -> None:
    """Set uploaded=True, uploaded_at, and ia_identifier on the album in the artist JSON."""
    json_path = _find_artist_json(band_id)
    if not json_path:
        logger.warning("Could not find artist JSON for band_id=%s - skipping uploaded mark.", band_id)
        return

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("Could not read artist JSON: %s", exc)
        return

    now = datetime.now(timezone.utc).isoformat()
    found = False
    for key, releases in data.items():
        if key.startswith("_") or not isinstance(releases, list):
            continue
        for release in releases:
            if release.get("item_id") == item_id:
                release["uploaded"]      = True
                release["uploaded_at"]   = now
                release["ia_identifier"] = identifier
                found = True
                break
        if found:
            break

    if not found:
        logger.warning("item_id=%s not found in artist JSON - skipping uploaded mark.", item_id)
        return

    try:
        json_path.write_text(json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8")
        logger.info("Marked uploaded: item_id=%s at %s", item_id, now)
    except OSError as exc:
        logger.error("Could not write artist JSON: %s", exc)


# ── Per-release upload ────────────────────────────────────────────────────────

def _band_item_id_from_filename(wacz_path: Path) -> tuple[int | None, int | None]:
    """
    Parse band_id and item_id from a WACZ filename as a last-resort fallback.
    Expects patterns like 'Title [item_id].wacz' — only item_id is recoverable
    from the filename; band_id is not encoded there.
    """
    import re
    m = re.search(r'\[(\d+)\]\.wacz$', wacz_path.name)
    if m:
        return None, int(m.group(1))
    return None, None


def _read_wacz_datapackage(wacz_path: Path) -> dict:
    """
    Read the embedded datapackage.json from a WACZ file.
    Returns the parsed dict, or an empty dict on any failure.
    """
    try:
        import zipfile
        with zipfile.ZipFile(wacz_path, "r") as zf:
            if "datapackage.json" in zf.namelist():
                return json.loads(zf.read("datapackage.json").decode("utf-8"))
    except Exception as exc:
        logger.debug("Could not read datapackage.json from %s: %s", wacz_path.name, exc)
    return {}


def upload_release(wacz_path: Path, dry_run: bool) -> bool:
    """
    Upload one WACZ + JSON pair to archive.org.
    Returns True on success, False on failure.
    """
    json_path = wacz_path.with_suffix(".json")

    if not json_path.exists():
        logger.warning(
            "No sidecar JSON for %s — attempting to reconstruct from artist JSON.",
            wacz_path.name,
        )
        datapackage = _read_wacz_datapackage(wacz_path)
        band_id = datapackage.get("bandcamp_band_id")
        item_id = datapackage.get("bandcamp_item_id")
        identifier = datapackage.get("bandcamp_ia_identifier")

        if not band_id or not item_id or not identifier:
            logger.error(
                "No sidecar and no usable datapackage for %s — skipping.",
                wacz_path.name,
            )
            return False

        album = _find_release_in_artist_json(int(band_id), int(item_id))
        if not album:
            logger.error(
                "Could not find item_id=%s in artist JSON — skipping %s.",
                item_id, wacz_path.name,
            )
            return False

        # Reconstruct the sidecar using the same logic as metadata.py:
        # strip archived/uploaded tracking fields, add band_id and ia_identifier.
        reconstructed = {k: v for k, v in album.items() if k not in ("archived", "uploaded", "archived_at", "uploaded_at")}
        reconstructed["band_id"]       = int(band_id)
        reconstructed["ia_identifier"] = identifier

        try:
            json_path.write_text(json.dumps(reconstructed, indent=4, ensure_ascii=False), encoding="utf-8")
            logger.info("Reconstructed sidecar JSON written: %s", json_path.name)
        except OSError as exc:
            logger.error("Could not write reconstructed sidecar JSON: %s", exc)
            return False

    try:
        release = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("Could not read release JSON %s: %s", json_path.name, exc)
        return False

    # Read band_id and item_id: prefer embedded WACZ datapackage, fall back to sidecar JSON,
    # then fall back to filename parsing.
    datapackage = _read_wacz_datapackage(wacz_path)

    band_id = (
        datapackage.get("bandcamp_band_id")
        or release.get("band_id")
        or _band_item_id_from_filename(wacz_path)[0]
    )
    item_id = (
        datapackage.get("bandcamp_item_id")
        or release.get("item_id")
        or _band_item_id_from_filename(wacz_path)[1]
    )
    identifier = (
        datapackage.get("bandcamp_ia_identifier")
        or release.get("ia_identifier")
    )

    if not identifier:
        logger.error("No ia_identifier in %s or its datapackage.json - skipping.", wacz_path.name)
        return False
    metadata = _build_ia_metadata(release)

    upload_files = [str(wacz_path), str(json_path)]

    logger.info("Uploading: %s → %s", wacz_path.name, identifier)
    if dry_run:
        print(f"  [DRY RUN] Would upload {wacz_path.name} + {json_path.name}")
        print(f"            identifier : {identifier}")
        for k, v in metadata.items():
            print(f"            {k:<16}: {v}")
        return True

    item_created = False
    last_exc: Exception | None = None
    try:
        for attempt in range(1, IA_MAX_RETRIES + 2):  # +2: first try + retries
            try:
                session  = _ia_session()
                item     = session.get_item(identifier)
                response = item.upload(
                    files=upload_files,
                    metadata=metadata,
                    checksum=True,
                    verbose=True,
                )
                item_created = True  # noqa: F841 (kept for clarity)

                # internetarchive returns a list of responses; check all succeeded
                failures = [r for r in response if r.status_code not in (200, 201)]
                if failures:
                    for r in failures:
                        logger.error("Upload failed for %s: HTTP %s", r.url, r.status_code)
                    last_exc = RuntimeError(f"HTTP error(s) from IA: {[r.status_code for r in failures]}")
                    raise last_exc

                # Success — break out of retry loop
                break

            except KeyboardInterrupt:
                raise

            except Exception as exc:
                last_exc = exc
                if attempt <= IA_MAX_RETRIES:
                    wait = IA_RETRY_DELAY * attempt
                    logger.warning(
                        "Upload attempt %d/%d failed for %s: %s  — retrying in %ds...",
                        attempt, IA_MAX_RETRIES + 1, identifier, exc, wait,
                    )
                    time.sleep(wait)
                else:
                    logger.error(
                        "Upload failed after %d attempt(s) for %s: %s",
                        attempt, identifier, exc,
                    )
                    return False

    except KeyboardInterrupt:
        print()
        logger.warning("Upload interrupted for %s.", identifier)
        if item_created:
            logger.warning("Partial upload detected - deleting IA item %s...", identifier)
            try:
                import internetarchive as _ia
                _ia.delete(identifier, access_key=IA_ACCESS_KEY, secret_key=IA_SECRET_KEY,
                           cascade_delete=True, verbose=True)
                logger.info("Deleted partial IA item: %s", identifier)
            except Exception as del_exc:
                logger.error(
                    "Could not delete partial IA item %s: %s  "
                    "Please delete it manually at https://archive.org/delete/%s",
                    identifier, del_exc, identifier,
                )
        raise KeyboardInterrupt() from None

    logger.info("Upload succeeded: %s", identifier)

    # Mark uploaded in artist JSON
    if band_id and item_id:
        _mark_uploaded(int(band_id), int(item_id), identifier)

    # Delete local WACZ and release JSON
    try:
        wacz_path.unlink()
        json_path.unlink()
        logger.info("Deleted local files: %s, %s", wacz_path.name, json_path.name)
    except OSError as exc:
        logger.warning("Could not delete local files: %s", exc)

    return True


# ── Input collection ──────────────────────────────────────────────────────────

def collect_wacz_files(inputs: list[str]) -> list[Path]:
    """Collect WACZ files from a mix of file and directory paths."""
    result: list[Path] = []
    for inp in inputs:
        p = Path(inp)
        if p.is_dir():
            found = sorted(p.glob("*.wacz"))
            if not found:
                logger.warning("No WACZ files found in: %s", p)
            result.extend(found)
        elif p.is_file() and p.suffix.lower() == ".wacz":
            result.append(p)
        else:
            logger.warning("Not a WACZ file or directory: %s", p)
    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="upload.py",
        description="Upload Bandcamp WACZ files to archive.org.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "inputs", nargs="+", metavar="PATH",
        help="WACZ files or directories to upload.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be uploaded without actually uploading.",
    )
    parser.add_argument(
        "--debug", "-d", action="store_true",
        help="Enable verbose debug logging.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    wacz_files = collect_wacz_files(args.inputs)
    if not wacz_files:
        print("No WACZ files to upload.")
        sys.exit(0)

    ok = 0
    fail = 0
    for wacz_path in wacz_files:
        logger.info("Processing: %s", wacz_path.name)
        if upload_release(wacz_path, dry_run=args.dry_run):
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
