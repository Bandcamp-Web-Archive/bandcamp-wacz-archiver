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
        # Read only the two small files that need patching — do NOT load the
        # entire ZIP into memory (WACZ files can exceed 10 GB).
        with zipfile.ZipFile(wacz_path, "r") as zin:
            all_infos = zin.infolist()
            info_map  = {info.filename: info for info in all_infos}

            if DATAPACKAGE not in info_map:
                logger.warning("No %s found in %s — skipping metadata embed.", DATAPACKAGE, wacz_path.name)
                return False

            # Patch datapackage.json (tiny — safe to read in full)
            try:
                pkg = json.loads(zin.read(DATAPACKAGE).decode("utf-8"))
            except Exception as exc:
                logger.warning("Could not parse %s in %s: %s", DATAPACKAGE, wacz_path.name, exc)
                return False

        pkg["bandcamp_band_id"] = band_id
        pkg["bandcamp_item_id"] = item_id
        patched_datapackage: bytes = json.dumps(pkg, indent=2, ensure_ascii=False).encode("utf-8")

        # Build release.json — strip pipeline-internal fields (also tiny)
        release = {k: v for k, v in album.items() if k not in ("archived", "uploaded", "archived_at", "uploaded_at")}
        release["band_id"] = band_id
        release_bytes: bytes = json.dumps(release, indent=4, ensure_ascii=False).encode("utf-8")

        # Rewrite the ZIP in one streaming pass.
        # Large entries are copied directly between ZipFile objects without
        # ever being fully buffered in Python — only the two patched entries
        # (both small JSON files) are held in memory as bytes.
        now = datetime.now(timezone.utc).timetuple()[:6]
        patched_names = {DATAPACKAGE, RELEASE_JSON_ENTRY}
        with zipfile.ZipFile(wacz_path, "r") as zin, \
             zipfile.ZipFile(tmp_path, "w") as zout:
            # First, stream every existing entry, substituting the patched
            # datapackage.json and skipping any stale release.json.
            for orig in zin.infolist():
                if orig.filename == DATAPACKAGE:
                    info = zipfile.ZipInfo(filename=orig.filename, date_time=orig.date_time)
                    zout.writestr(info, patched_datapackage, compress_type=zipfile.ZIP_DEFLATED)
                elif orig.filename == RELEASE_JSON_ENTRY:
                    pass  # Will be written fresh below
                else:
                    # Stream the entry without reading it into Python memory.
                    with zin.open(orig) as src:
                        info = zipfile.ZipInfo(filename=orig.filename, date_time=orig.date_time)
                        info.compress_type = orig.compress_type
                        with zout.open(info, "w") as dst:
                            while True:
                                chunk = src.read(1 << 20)  # 1 MiB chunks
                                if not chunk:
                                    break
                                dst.write(chunk)

            # Finally, append the (possibly new) release.json entry.
            rel_info = zipfile.ZipInfo(
                filename=RELEASE_JSON_ENTRY,
                date_time=info_map[RELEASE_JSON_ENTRY].date_time if RELEASE_JSON_ENTRY in info_map else now,
            )
            zout.writestr(rel_info, release_bytes, compress_type=zipfile.ZIP_DEFLATED)

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
