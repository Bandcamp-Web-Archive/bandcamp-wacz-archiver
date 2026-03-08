"""
metadata.py - Step 2 of the archival pipeline.

Called automatically by crawl.py after each successful WACZ is produced.
Does two things:

  1. Embeds band_id and item_id into the WACZ's datapackage.json, and writes
     a release.json entry directly inside the WACZ zip. This makes the archive
     fully self-describing — no sidecar file on disk is needed.

  2. Marks the album as archived in the artist JSON
     (archived=True, archived_at=<ISO timestamp>).
"""

from __future__ import annotations

import json
import logging
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import ARTISTS_DIR

logger = logging.getLogger(__name__)

# Name of the release metadata entry stored inside the WACZ zip.
RELEASE_JSON_ENTRY = "release.json"


def _find_artist_json(band_id: int) -> Optional[Path]:
    """Find the artist JSON whose folder name ends with [band_id]."""
    if not ARTISTS_DIR.exists():
        return None
    for folder in ARTISTS_DIR.iterdir():
        if folder.is_dir() and folder.name.endswith(f"[{band_id}]"):
            candidate = folder / f"{folder.name}.json"
            if candidate.exists():
                return candidate
    return None


def _find_album(data: dict, item_id: int) -> Optional[tuple[str, int, dict]]:
    """
    Find an album by item_id inside the artist JSON.
    Returns (artist_key, list_index, album_dict) or None.
    """
    for key, value in data.items():
        if key.startswith("_") or not isinstance(value, list):
            continue
        for i, album in enumerate(value):
            if album.get("item_id") == item_id:
                return key, i, album
    return None


def embed_metadata_in_wacz(wacz_path: Path, band_id: int, item_id: int, album: dict) -> bool:
    """
    Patch datapackage.json and add release.json inside the WACZ in one zip rewrite.

    WACZ files are ZIP archives. datapackage.json follows the Frictionless Data
    spec which allows arbitrary extra fields. release.json stores the full album
    metadata so the WACZ is self-describing without any sidecar file.

    Returns True on success, False on failure.
    """
    DATAPACKAGE = "datapackage.json"
    tmp_path = wacz_path.with_suffix(".wacz.tmp")

    try:
        with zipfile.ZipFile(wacz_path, "r") as zin:
            infos   = {info.filename: info for info in zin.infolist()}
            entries: dict[str, bytes] = {name: zin.read(name) for name in infos}

        if DATAPACKAGE not in entries:
            logger.warning("No %s found in %s — skipping metadata embed.", DATAPACKAGE, wacz_path.name)
            return False

        # Patch datapackage.json
        try:
            pkg = json.loads(entries[DATAPACKAGE].decode("utf-8"))
        except Exception as exc:
            logger.warning("Could not parse %s in %s: %s", DATAPACKAGE, wacz_path.name, exc)
            return False

        pkg["bandcamp_band_id"] = band_id
        pkg["bandcamp_item_id"] = item_id
        entries[DATAPACKAGE] = json.dumps(pkg, indent=2, ensure_ascii=False).encode("utf-8")

        # Build release.json — strip pipeline-internal fields
        release = {k: v for k, v in album.items() if k not in ("archived", "uploaded", "archived_at", "uploaded_at")}
        release["band_id"] = band_id
        entries[RELEASE_JSON_ENTRY] = json.dumps(release, indent=4, ensure_ascii=False).encode("utf-8")

        # Rewrite the ZIP in one pass, preserving compression of existing entries
        now = datetime.now(timezone.utc).timetuple()[:6]
        with zipfile.ZipFile(tmp_path, "w") as zout:
            for name, data in entries.items():
                if name in infos:
                    orig = infos[name]
                    compress = zipfile.ZIP_DEFLATED if name in (DATAPACKAGE, RELEASE_JSON_ENTRY) else orig.compress_type
                    info = zipfile.ZipInfo(filename=orig.filename, date_time=orig.date_time)
                else:
                    # New entry (release.json)
                    compress = zipfile.ZIP_DEFLATED
                    info = zipfile.ZipInfo(filename=name, date_time=now)
                zout.writestr(info, data, compress_type=compress)

        tmp_path.replace(wacz_path)
        logger.info("Embedded metadata (band_id=%s item_id=%s) into %s", band_id, item_id, wacz_path.name)
        return True

    except Exception as exc:
        logger.error("Could not embed metadata into %s: %s", wacz_path.name, exc)
        tmp_path.unlink(missing_ok=True)
        return False


def mark_archived(json_path: Path, artist_key: str, album_index: int, data: dict) -> None:
    """Set archived=True and archived_at on the album in the artist JSON."""
    now = datetime.now(timezone.utc).isoformat()
    data[artist_key][album_index]["archived"]    = True
    data[artist_key][album_index]["archived_at"] = now
    json_path.write_text(json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8")
    logger.info("Marked archived: %s [index %d] at %s", json_path.name, album_index, now)


def process_archived_wacz(wacz_path: Path, band_id: int, item_id: int) -> None:
    """Full Step 2 pipeline for one successfully crawled album."""
    artist_json_path = _find_artist_json(band_id)
    if not artist_json_path:
        logger.warning(
            "No artist JSON found for band_id=%s — run fetch_metadata.py first.",
            band_id,
        )
        return

    try:
        data = json.loads(artist_json_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("Could not read %s: %s", artist_json_path, exc)
        return

    result = _find_album(data, item_id)
    if not result:
        logger.warning("item_id=%s not found in %s", item_id, artist_json_path.name)
        return

    artist_key, album_index, album = result

    embed_metadata_in_wacz(wacz_path, band_id, item_id, album)

    try:
        mark_archived(artist_json_path, artist_key, album_index, data)
    except OSError as exc:
        logger.error("Could not update artist JSON: %s", exc)
