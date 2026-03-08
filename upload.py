#!/usr/bin/env python3
"""
upload.py - upload Bandcamp WACZ files to Pixeldrain.

Finds WACZ files in the input path, uploads each one to Pixeldrain,
marks the release as uploaded in the artist JSON, then deletes the
local WACZ.

Release metadata is read directly from the release.json embedded inside
each WACZ — no sidecar file is needed.

Usage
─────
  # Upload everything in wacz_output/:
  python upload.py wacz_output/

  # Upload a single WACZ:
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
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from bandcamp_wacz.config import PD_API_KEY, PD_MAX_RETRIES, PD_RETRY_DELAY, ARTISTS_DIR

logger = logging.getLogger(__name__)

PD_UPLOAD_URL = "https://pixeldrain.com/api/file/{name}"


# ── WACZ helpers ──────────────────────────────────────────────────────────────

def _read_wacz_release_json(wacz_path: Path) -> dict:
    """
    Read the release.json embedded inside the WACZ zip.
    Returns the parsed dict, or an empty dict on any failure.
    """
    try:
        with zipfile.ZipFile(wacz_path, "r") as zf:
            if "release.json" in zf.namelist():
                return json.loads(zf.read("release.json").decode("utf-8"))
    except Exception as exc:
        logger.debug("Could not read release.json from %s: %s", wacz_path.name, exc)
    return {}


def _read_wacz_datapackage(wacz_path: Path) -> dict:
    """
    Read datapackage.json from the WACZ zip.
    Returns the parsed dict, or an empty dict on any failure.
    """
    try:
        with zipfile.ZipFile(wacz_path, "r") as zf:
            if "datapackage.json" in zf.namelist():
                return json.loads(zf.read("datapackage.json").decode("utf-8"))
    except Exception as exc:
        logger.debug("Could not read datapackage.json from %s: %s", wacz_path.name, exc)
    return {}


def _band_item_id_from_filename(wacz_path: Path) -> tuple[Optional[int], Optional[int]]:
    """Parse item_id from filename as a last-resort fallback."""
    m = re.search(r'\[(\d+)\]\.wacz$', wacz_path.name)
    if m:
        return None, int(m.group(1))
    return None, None


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


def _mark_uploaded(band_id: int, item_id: int, pd_wacz_id: str) -> None:
    """Set uploaded=True, uploaded_at, and pd_wacz_id on the album in the artist JSON."""
    json_path = _find_artist_json(band_id)
    if not json_path:
        logger.warning("Could not find artist JSON for band_id=%s — skipping uploaded mark.", band_id)
        return

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("Could not read artist JSON: %s", exc)
        return

    now   = datetime.now(timezone.utc).isoformat()
    found = False
    for key, releases in data.items():
        if key.startswith("_") or not isinstance(releases, list):
            continue
        for release in releases:
            if release.get("item_id") == item_id:
                release["uploaded"]    = True
                release["uploaded_at"] = now
                release["pd_wacz_id"]  = pd_wacz_id
                found = True
                break
        if found:
            break

    if not found:
        logger.warning("item_id=%s not found in artist JSON — skipping uploaded mark.", item_id)
        return

    try:
        json_path.write_text(json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8")
        logger.info("Marked uploaded: item_id=%s pd_wacz_id=%s", item_id, pd_wacz_id)
    except OSError as exc:
        logger.error("Could not write artist JSON: %s", exc)


# ── Per-WACZ upload ───────────────────────────────────────────────────────────

def upload_release(wacz_path: Path, dry_run: bool) -> bool:
    """
    Upload one WACZ to Pixeldrain.
    Returns True on success, False on failure.
    """
    if not PD_API_KEY:
        logger.error("PD_API_KEY is not set in .env — cannot upload.")
        return False

    # Read metadata from inside the WACZ
    release     = _read_wacz_release_json(wacz_path)
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

    if not item_id:
        logger.error("Could not determine item_id for %s — skipping.", wacz_path.name)
        return False

    logger.info("Uploading: %s", wacz_path.name)

    if dry_run:
        print(f"  [DRY RUN] Would upload {wacz_path.name}")
        print(f"            artist : {release.get('artist', '?')}")
        print(f"            title  : {release.get('title', '?')}")
        print(f"            size   : {wacz_path.stat().st_size:,} bytes")
        return True

    # Upload to Pixeldrain via PUT /api/file/{filename}
    pd_wacz_id: Optional[str] = None

    for attempt in range(1, PD_MAX_RETRIES + 2):
        try:
            with wacz_path.open("rb") as fh:
                resp = requests.put(
                    PD_UPLOAD_URL.format(name=wacz_path.name),
                    data=fh,
                    auth=("", PD_API_KEY),
                    timeout=None,  # large files — no timeout
                )

            if resp.status_code == 201:
                pd_wacz_id = resp.json()["id"]
                logger.info("Upload succeeded: %s → pixeldrain.com/u/%s", wacz_path.name, pd_wacz_id)
                break
            else:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        except KeyboardInterrupt:
            print()
            logger.warning("Upload interrupted for %s.", wacz_path.name)
            raise

        except Exception as exc:
            if attempt <= PD_MAX_RETRIES:
                wait = PD_RETRY_DELAY * attempt
                logger.warning(
                    "Upload attempt %d/%d failed for %s: %s — retrying in %ds...",
                    attempt, PD_MAX_RETRIES + 1, wacz_path.name, exc, wait,
                )
                time.sleep(wait)
            else:
                logger.error(
                    "Upload failed after %d attempt(s) for %s: %s",
                    attempt, wacz_path.name, exc,
                )
                return False

    if not pd_wacz_id:
        logger.error("Upload did not return a file ID for %s.", wacz_path.name)
        return False

    # Mark uploaded in artist JSON
    if band_id and item_id:
        _mark_uploaded(int(band_id), int(item_id), pd_wacz_id)

    # Delete local WACZ
    try:
        wacz_path.unlink()
        logger.info("Deleted local file: %s", wacz_path.name)
    except OSError as exc:
        logger.warning("Could not delete local WACZ: %s", exc)

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
        description="Upload Bandcamp WACZ files to Pixeldrain.",
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

    ok   = 0
    fail = 0
    for wacz_path in wacz_files:
        logger.info("Processing: %s", wacz_path.name)
        try:
            if upload_release(wacz_path, dry_run=args.dry_run):
                ok += 1
            else:
                fail += 1
        except KeyboardInterrupt:
            print("\nInterrupted.")
            sys.exit(130)

    print(f"\n── Summary ─────────────────────────────")
    print(f"  Succeeded : {ok}")
    print(f"  Failed    : {fail}")
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
