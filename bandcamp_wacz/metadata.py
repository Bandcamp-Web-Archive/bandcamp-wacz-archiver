"""
metadata.py - Step 2 of the archival pipeline.

Called automatically by crawl.py after each successful WACZ is produced.
Does two things:

  1. Writes a single-release JSON alongside the WACZ. This file is a raw
     extract of the album dict from the artist JSON, plus the archive.org
     identifier. It is consumed by Step 7 (upload) and deleted afterwards.

  2. Marks the album as archived in the artist JSON
     (archived=True, archived_at=<ISO timestamp>).

  3. Embeds band_id, item_id, and ia_identifier into the WACZ's
     datapackage.json so the archive is self-describing and not reliant
     on filename conventions.

Archive.org identifier format: wacz-{band_id}-{item_id}-{YYYYMMDD}
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


def _ia_identifier(band_id: int, item_id: int) -> str:
    date = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"wacz-{band_id}-{item_id}-{date}"


def embed_metadata_in_wacz(wacz_path: Path, band_id: int, item_id: int, ia_identifier: str) -> bool:
    """
    Embed band_id, item_id, and ia_identifier into the WACZ's datapackage.json.

    WACZ files are ZIP archives. datapackage.json follows the Frictionless Data
    spec which allows arbitrary extra fields, making this the correct place to
    store provenance metadata. This makes the archive self-describing and not
    reliant on filename conventions.

    Returns True on success, False on failure.
    """
    DATAPACKAGE = "datapackage.json"

    try:
        # Read all existing entries
        with zipfile.ZipFile(wacz_path, "r") as zin:
            names = zin.namelist()
            entries: dict[str, bytes] = {name: zin.read(name) for name in names}

        if DATAPACKAGE not in entries:
            logger.warning("No %s found in %s - skipping metadata embed.", DATAPACKAGE, wacz_path.name)
            return False

        # Patch datapackage.json
        try:
            pkg = json.loads(entries[DATAPACKAGE].decode("utf-8"))
        except Exception as exc:
            logger.warning("Could not parse %s in %s: %s", DATAPACKAGE, wacz_path.name, exc)
            return False

        pkg["bandcamp_band_id"]    = band_id
        pkg["bandcamp_item_id"]    = item_id
        pkg["bandcamp_ia_identifier"] = ia_identifier

        entries[DATAPACKAGE] = json.dumps(pkg, indent=2, ensure_ascii=False).encode("utf-8")

        # Rewrite the ZIP preserving all other entries and their compression
        tmp_path = wacz_path.with_suffix(".wacz.tmp")
        with zipfile.ZipFile(wacz_path, "r") as zin:
            infos = {info.filename: info for info in zin.infolist()}
            with zipfile.ZipFile(tmp_path, "w") as zout:
                for name, data in entries.items():
                    orig_info = infos[name]
                    # Use DEFLATED for datapackage.json, preserve original compression for rest
                    compress = zipfile.ZIP_DEFLATED if name == DATAPACKAGE else orig_info.compress_type
                    zout.writestr(zipfile.ZipInfo(
                        filename=orig_info.filename,
                        date_time=orig_info.date_time,
                    ), data, compress_type=compress)

        tmp_path.replace(wacz_path)
        logger.info("Embedded band_id=%s item_id=%s into %s", band_id, item_id, wacz_path.name)
        return True

    except Exception as exc:
        logger.error("Could not embed metadata into %s: %s", wacz_path.name, exc)
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        return False


def write_release_json(album: dict, band_id: int, item_id: int, wacz_path: Path) -> Path:
    """Write a single-release JSON alongside the WACZ."""
    release = {k: v for k, v in album.items() if k not in ("archived", "uploaded")}
    release["band_id"]       = band_id
    release["ia_identifier"] = _ia_identifier(band_id, item_id)

    path = wacz_path.with_suffix(".json")
    path.write_text(json.dumps(release, indent=4, ensure_ascii=False), encoding="utf-8")
    logger.info("Release JSON written: %s", path)
    return path


def mark_archived(json_path: Path, artist_key: str, album_index: int, data: dict) -> None:
    """Set archived=True and archived_at on the album in the artist JSON."""
    now = datetime.now(timezone.utc).isoformat()
    data[artist_key][album_index]["archived"]    = True
    data[artist_key][album_index]["archived_at"] = now
    json_path.write_text(json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8")
    logger.info("Marked archived: %s [index %d] at %s", json_path.name, album_index, now)


def process_archived_wacz(wacz_path: Path, band_id: int, item_id: int) -> Optional[Path]:
    """
    Full Step 2 pipeline for one successfully crawled album.
    Returns the release JSON path, or None if the artist JSON was not found.
    """
    artist_json_path = _find_artist_json(band_id)
    if not artist_json_path:
        logger.warning(
            "No artist JSON found for band_id=%s - run fetch_metadata.py first.",
            band_id,
        )
        return None

    try:
        data = json.loads(artist_json_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("Could not read %s: %s", artist_json_path, exc)
        return None

    result = _find_album(data, item_id)
    if not result:
        logger.warning("item_id=%s not found in %s", item_id, artist_json_path.name)
        return None

    artist_key, album_index, album = result
    identifier = _ia_identifier(band_id, item_id)

    # Embed provenance metadata directly into the WACZ
    embed_metadata_in_wacz(wacz_path, band_id, item_id, identifier)

    try:
        release_path = write_release_json(album, band_id, item_id, wacz_path)
    except OSError as exc:
        logger.error("Could not write release JSON: %s", exc)
        return None

    try:
        mark_archived(artist_json_path, artist_key, album_index, data)
    except OSError as exc:
        logger.error("Could not update artist JSON: %s", exc)

    return release_path
