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
from urllib.parse import quote
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from bandcamp_wacz.config import PD_API_KEY, PD_MAX_RETRIES, PD_RETRY_DELAY, ARTISTS_DIR
from bandcamp_wacz.bandcamp import artist_folder_name

logger = logging.getLogger(__name__)

# Base URL for the pixeldrain filesystem API.
# All paths are relative to the authenticated user's personal root ("me").
PD_FS_BASE_URL = "https://pixeldrain.com/api/filesystem/me"

# Folders already confirmed to exist this session.
# Avoids a redundant POST for every WACZ belonging to the same artist.
_pd_folders_created: set[str] = set()


def _ensure_pd_folder(folder: str) -> bool:
    """
    Create a directory inside the user's personal Pixeldrain filesystem if it
    does not already exist.

    The filesystem API expects a multipart/form-data POST with action=mkdirall,
    which is idempotent — it succeeds whether or not the folder already exists.

    Returns True if the folder is ready to use, False on any error.
    """
    if folder in _pd_folders_created:
        return True

    url = f"{PD_FS_BASE_URL}/{folder}"

    try:
        resp = requests.post(
            url,
            data={"action": "mkdirall"},
            auth=("", PD_API_KEY),
            timeout=30,
        )
        if resp.status_code in (200, 201):
            logger.info("Pixeldrain folder ready: %s", folder)
        else:
            logger.error(
                "Failed to create Pixeldrain folder %s: HTTP %d %s",
                folder, resp.status_code, resp.text[:200],
            )
            return False
    except Exception as exc:
        logger.error("Error creating Pixeldrain folder %s: %s", folder, exc)
        return False

    _pd_folders_created.add(folder)
    return True


def _pd_share_file(file_url: str) -> Optional[str]:
    """
    Make a filesystem file publicly accessible and return its share ID.

    Pixeldrain's filesystem files are private by default. Sharing is done
    by POSTing action=update with shared=true to the file's path. The API
    responds with a FilesystemNode whose 'id' field is populated once the
    file is shared.

    If the update response doesn't include an 'id', a follow-up GET is made
    to retrieve the node details and extract the public ID from there.

    Returns the public file ID (usable as pixeldrain.com/u/{id}),
    or None on failure.
    """
    try:
        resp = requests.post(
            file_url,
            data={"action": "update", "shared": "true"},
            auth=("", PD_API_KEY),
            timeout=30,
        )
        if resp.status_code not in (200, 201):
            logger.error(
                "Failed to share file %s: HTTP %d %s",
                file_url, resp.status_code, resp.text[:200],
            )
            return None

        # Try to get the ID directly from the update response
        node = resp.json()
        file_id = node.get("id")
        if file_id:
            return file_id

        # The update response didn't include 'id' — do a GET to retrieve the
        # full node details, which should include the public file ID.
        logger.debug("Share response had no 'id'; fetching node details via GET: %s", file_url)
        get_resp = requests.get(
            file_url,
            auth=("", PD_API_KEY),
            timeout=30,
        )
        if get_resp.status_code == 200:
            node = get_resp.json()
            file_id = node.get("id")
            if file_id:
                return file_id
            logger.error("GET node response also contained no 'id': %s", get_resp.text[:200])
        else:
            logger.error(
                "GET node failed for %s: HTTP %d %s",
                file_url, get_resp.status_code, get_resp.text[:200],
            )

    except Exception as exc:
        logger.error("Error sharing file %s: %s", file_url, exc)
    return None


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


def _page_artist_from_band_id(band_id: int) -> Optional[str]:
    """
    Return the page artist name by inspecting the artist folder in ARTISTS_DIR.

    The folder is named '{page_artist} [{band_id}]', so we strip the
    ' [{band_id}]' suffix to recover the page artist. This is the artist whose
    Bandcamp page was crawled — distinct from the per-release 'artist' field,
    which can differ for split releases (e.g. 'Saint Elisabeth + Séraphitüs-Séraphîta').
    """
    if not ARTISTS_DIR.exists():
        return None
    suffix = f" [{band_id}]"
    for folder in ARTISTS_DIR.iterdir():
        if folder.is_dir() and folder.name.endswith(suffix):
            return folder.name[: -len(suffix)]
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

    if not band_id:
        logger.error("Could not determine band_id for %s — skipping.", wacz_path.name)
        return False

    band_id = int(band_id)
    # Use the page artist (from the ARTISTS_DIR folder name) so that split
    # releases don't create a separate folder per release artist.
    # E.g. a Saint Elisabeth page with a split release
    # "Saint Elisabeth + Séraphitüs-Séraphîta" must still land in
    # "Saint Elisabeth [<band_id>]".
    page_artist = (
        _page_artist_from_band_id(band_id)
        or release.get("artist")
        or datapackage.get("bandcamp_artist")
        or "unknown"
    )
    folder = artist_folder_name(page_artist, band_id)
    logger.info("Uploading: %s (folder=%s)", wacz_path.name, folder)

    if dry_run:
        print(f"  [DRY RUN] Would upload {wacz_path.name}")
        print(f"            folder : {folder}/")
        print(f"            artist : {page_artist}")
        print(f"            title  : {release.get('title', '?')}")
        print(f"            size   : {wacz_path.stat().st_size:,} bytes")
        return True

    # Ensure the per-artist folder exists on Pixeldrain
    if not _ensure_pd_folder(folder):
        return False

    # Upload to Pixeldrain via PUT /api/filesystem/me/{folder}/{filename}
    upload_url = f"{PD_FS_BASE_URL}/{folder}/{quote(wacz_path.name)}"
    pd_wacz_id: Optional[str] = None

    for attempt in range(1, PD_MAX_RETRIES + 2):
        try:
            with wacz_path.open("rb") as fh:
                resp = requests.put(
                    upload_url,
                    data=fh,
                    auth=("", PD_API_KEY),
                    timeout=None,  # large files — no timeout
                )

            if resp.status_code in (200, 201):
                # Upload succeeded. The file is private by default — share it
                # to get a public ID usable at pixeldrain.com/u/{id}.
                file_id = _pd_share_file(upload_url)
                if file_id:
                    pd_wacz_id = file_id
                    logger.info(
                        "Upload + share succeeded: %s/%s → pixeldrain.com/u/%s",
                        folder, wacz_path.name, pd_wacz_id,
                    )
                else:
                    logger.warning(
                        "Upload succeeded but sharing failed for %s — storing path as reference.",
                        wacz_path.name,
                    )
                    pd_wacz_id = f"me/{folder}/{wacz_path.name}"
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
    if item_id:
        _mark_uploaded(band_id, int(item_id), pd_wacz_id)

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
