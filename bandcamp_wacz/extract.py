#!/usr/bin/env python3
"""
extract.py - extract audio and cover art from Bandcamp WACZ files.

Reads directly from the WACZ (no re-downloading), applies ID3 tags from the
release or artist JSON, and writes files to a per-album folder.

Output layout
─────────────
  <output_root>/
    <Album Title> [<item_id>]/
      cover.jpg
      01 - Track Title [<track_id>].mp3
      02 - Track Title [<track_id>].mp3
      02_cover.jpg          ← only with --track-covers, only if different from album cover

Metadata is located automatically:
  1. Release JSON alongside the WACZ  (<wacz_stem>.json, written by archive.py)
  2. Artist JSON in artists/           (searched by item_id from the WACZ filename)
  3. Manual path via --ask

Usage
─────
  python bandcamp_wacz/extract.py path/to/album.wacz
  python bandcamp_wacz/extract.py wacz_output/           # process entire directory
  python bandcamp_wacz/extract.py album.wacz --output ~/Music/
  python bandcamp_wacz/extract.py album.wacz --track-covers
  python bandcamp_wacz/extract.py album.wacz --ask       # prompt if JSON not found
  python bandcamp_wacz/extract.py album.wacz --auto-pick # non-interactive duplicate handling
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import logging
import os
import re
import sys
import zipfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ── Filename sanitisation ─────────────────────────────────────────────────────

def safe_filename(name: str) -> str:
    """
    Return a filesystem-safe filename component.
    Strips control characters and characters forbidden on Linux/Windows.
    Collapses repeated spaces/hyphens.
    """
    name = re.sub(r"[\x00-\x1f\x7f\u200b-\u200d\ufeff]", "", name)
    name = name.replace(" | ", " - ")
    name = re.sub(r'[\\/?%*:|"<>]+', "-", name)
    name = re.sub(r" +", " ", name)
    name = re.sub(r"-+", "-", name)
    name = name.strip(" .-")
    return name or "untitled"


# ── WACZ / WARC helpers ───────────────────────────────────────────────────────

def _load_cdx(wacz: zipfile.ZipFile) -> list[dict]:
    """Parse indexes/index.cdx.gz and return all CDX records as dicts."""
    with wacz.open("indexes/index.cdx.gz") as raw:
        with gzip.open(raw) as gz:
            lines = gz.read().decode("utf-8", errors="replace").splitlines()

    records = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # CDX-J format: <surt> <timestamp> <json>
        # Find the JSON object at the end of the line
        brace = line.find("{")
        if brace == -1:
            continue
        try:
            records.append(json.loads(line[brace:]))
        except json.JSONDecodeError:
            pass
    return records


def _extract_record_body(wacz: zipfile.ZipFile, cdx: dict) -> Optional[bytes]:
    """
    Extract the response body from a WARC record using the CDX offset/length.

    The WARC file is gzip-compressed inside the WACZ zip.  Each WARC record
    is independently gzip-compressed (WARC/1.1 with chunked gzip), so we seek
    to the offset, read `length` compressed bytes, decompress them, then skip
    past the WARC headers to get the raw response body.
    """
    filename = cdx.get("filename")
    offset   = int(cdx.get("offset", 0))
    length   = int(cdx.get("length", 0))

    if not filename or not length:
        return None

    warc_path = f"archive/{filename}"
    try:
        with wacz.open(warc_path) as warc_file:
            warc_file.read(offset)          # seek by reading (ZipExtFile is not seekable)
            chunk = warc_file.read(length)
    except (KeyError, Exception) as exc:
        logger.error("Could not read WARC record from %s at offset %d: %s", warc_path, offset, exc)
        return None

    # Decompress the individual gzip record
    try:
        raw = gzip.decompress(chunk)
    except Exception as exc:
        logger.error("Could not decompress WARC record: %s", exc)
        return None

    # Skip WARC headers (two CRLF-terminated header blocks: WARC then HTTP)
    # Format: WARC-header-block \r\n\r\n HTTP-header-block \r\n\r\n body
    separator = b"\r\n\r\n"
    pos = raw.find(separator)          # end of WARC headers
    if pos == -1:
        return None
    pos += len(separator)
    pos2 = raw.find(separator, pos)    # end of HTTP headers
    if pos2 == -1:
        return None
    pos2 += len(separator)

    return raw[pos2:]


# ── Metadata loading ──────────────────────────────────────────────────────────

def _find_release_json_in_wacz(wacz_path: Path) -> Optional[dict]:
    """Extract release.json from inside the WACZ zip, if present."""
    try:
        with zipfile.ZipFile(wacz_path, "r") as zf:
            if "release.json" in zf.namelist():
                return json.loads(zf.read("release.json").decode("utf-8"))
    except Exception as exc:
        logger.debug("Could not read release.json from %s: %s", wacz_path.name, exc)
    return None


def _find_release_json(wacz_path: Path) -> Optional[Path]:
    """Look for <wacz_stem>.json alongside the WACZ (legacy sidecar fallback)."""
    candidate = wacz_path.with_suffix(".json")
    return candidate if candidate.exists() else None


def _find_artist_json(band_id: int, search_roots: list[Path]) -> Optional[Path]:
    """
    Search for the artist JSON by matching [band_id] in folder names.
    Searches under each root in search_roots (project artists/ dirs).
    """
    for root in search_roots:
        if not root.exists():
            continue
        for folder in root.iterdir():
            if folder.is_dir() and folder.name.endswith(f"[{band_id}]"):
                candidate = folder / f"{folder.name}.json"
                if candidate.exists():
                    return candidate
    return None


def _extract_album_from_artist_json(artist_json: dict, item_id: int) -> Optional[dict]:
    """Pull the matching album dict out of the artist JSON structure."""
    for key, value in artist_json.items():
        if key.startswith("_") or not isinstance(value, list):
            continue
        for album in value:
            if album.get("item_id") == item_id:
                return album
    return None


def _search_artist_jsons(item_id: int, artists_dirs: list[Path], auto_pick: bool = False) -> Optional[dict]:
    """
    Scan all artist JSONs in artists_dirs looking for an album with the given item_id.
    Returns the album dict, or None. Warns if item_id appears in more than one JSON.
    """
    matches: list[tuple[Path, dict]] = []

    for root in artists_dirs:
        if not root.exists():
            continue
        for folder in root.iterdir():
            if not folder.is_dir():
                continue
            json_path = folder / f"{folder.name}.json"
            if not json_path.exists():
                continue
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            album = _extract_album_from_artist_json(data, item_id)
            if album:
                matches.append((json_path, album))

    if not matches:
        return None
    if len(matches) == 1:
        json_path, album = matches[0]
        logger.info("Found metadata in artist JSON: %s", json_path)
        return album

    # Multiple matches - sort alphabetically for determinism
    matches.sort(key=lambda x: str(x[0]))

    if auto_pick:
        json_path, album = matches[0]
        logger.warning(
            "item_id=%s found in multiple artist JSONs - auto-picking: %s",
            item_id, json_path,
        )
        return album

    # Interactive: let the user choose
    print(f"\nitem_id={item_id} was found in multiple artist JSONs:")
    for i, (p, _) in enumerate(matches, 1):
        print(f"  {i}. {p}")
    while True:
        choice = input("Enter number to select, or press Enter to skip: ").strip()
        if not choice:
            return None
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(matches):
                json_path, album = matches[idx]
                logger.info("Using: %s", json_path)
                return album
            print(f"  Please enter a number between 1 and {len(matches)}.")
        except ValueError:
            print("  Please enter a valid number.")


def _load_metadata(
    wacz_path: Path,
    ask: bool,
    artists_dirs: list[Path],
    auto_pick: bool = False,
) -> Optional[dict]:
    """
    Load album metadata dict from release JSON or artist JSON.
    Falls back to --ask prompt if both are missing and ask=True.
    """
    # 1. release.json embedded inside the WACZ (preferred)
    embedded = _find_release_json_in_wacz(wacz_path)
    if embedded:
        logger.info("Using release.json embedded in WACZ: %s", wacz_path.name)
        return embedded

    # 2. Legacy sidecar JSON alongside the WACZ
    release_json_path = _find_release_json(wacz_path)
    if release_json_path:
        logger.info("Using sidecar release JSON: %s", release_json_path)
        try:
            return json.loads(release_json_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Could not read sidecar release JSON: %s", exc)

    logger.warning("No release JSON found for %s - searching artist JSONs...", wacz_path.name)

    # 2. Search all artist JSONs for the item_id parsed from the WACZ filename
    item_id = _guess_item_id_from_wacz(wacz_path)
    if item_id:
        album = _search_artist_jsons(item_id, artists_dirs, auto_pick=auto_pick)
        if album:
            return album

    # 3. --ask fallback
    if ask:
        path_str = input(
            "\nCould not find metadata JSON.\n"
            "Enter path to release JSON or artist JSON (no shell escaping needed,\n"
            "just type the path with spaces as-is, e.g.:\n"
            "  artists/Hel Vy [1534269872]/Hel Vy [1534269872].json\n"
            "Press Enter to skip: "
        ).strip()
        if path_str:
            p = Path(path_str)
            if not p.exists():
                logger.error("Path not found: %s", p)
            else:
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    if item_id:
                        album = _extract_album_from_artist_json(data, item_id)
                        if album:
                            return album
                    # Assume it's a release JSON directly
                    return data
                except Exception as exc:
                    logger.error("Could not read provided JSON: %s", exc)
        logger.error("No metadata provided - skipping %s", wacz_path.name)
        return None

    logger.error(
        "No metadata JSON found for %s. "
        "Run fetch_metadata.py first, or use --ask to provide the path manually.",
        wacz_path.name,
    )
    return None


def _guess_item_id_from_wacz(wacz_path: Path) -> Optional[int]:
    """Try to parse item_id from the WACZ stem: '<title> [<item_id>]'."""
    m = re.search(r'\[(\d+)\]$', wacz_path.stem)
    return int(m.group(1)) if m else None


# ── Audio / image extraction ──────────────────────────────────────────────────

def _find_audio_records(cdx_records: list[dict]) -> dict[int, dict]:
    """
    Return {track_id: cdx_record} for all audio/mpeg records from t4.bcbits.com.
    Track ID is the path component after mp3-128/.
    """
    audio = {}
    pattern = re.compile(r't4\.bcbits\.com/stream/[^/]+/mp3-128/(\d+)', re.IGNORECASE)
    for rec in cdx_records:
        url = rec.get("url", "")
        m = pattern.search(url)
        if m and rec.get("mime") == "audio/mpeg" and rec.get("status") == "200":
            track_id = int(m.group(1))
            audio[track_id] = rec
    return audio


def _find_image_record(cdx_records: list[dict], art_id: int) -> Optional[dict]:
    """
    Return the CDX record for the _0 cover image matching art_id.
    Matches URLs like f4.bcbits.com/img/a{art_id}_0
    """
    pattern = re.compile(rf'f4\.bcbits\.com/img/a{art_id}_0', re.IGNORECASE)
    for rec in cdx_records:
        url = rec.get("url", "")
        if pattern.search(url) and rec.get("status") == "200":
            return rec
    return None


def _image_hash(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


# ── ID3 tagging ───────────────────────────────────────────────────────────────

def _apply_id3_tags(
    mp3_data: bytes,
    track: dict,
    album: dict,
) -> bytes:
    """
    Apply ID3v2 tags to raw MP3 bytes and return the tagged bytes.
    Uses mutagen. If mutagen is not installed, returns the original bytes.
    """
    try:
        from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TPE2, TALB, TRCK, TDRC
        from mutagen.mp3 import MP3
    except ImportError:
        logger.warning("mutagen not installed - skipping ID3 tagging. Run: pip install mutagen")
        return mp3_data

    buf = io.BytesIO(mp3_data)

    try:
        audio = MP3(buf)
    except Exception as exc:
        logger.warning("Could not parse MP3 data for tagging: %s", exc)
        return mp3_data

    try:
        tags = ID3(buf)
    except ID3NoHeaderError:
        tags = ID3()

    # Track artist
    track_artist = track.get("artist") or album.get("artist") or "Unknown Artist"
    # Album artist (may differ from track artist on compilations)
    album_artist = album.get("artist") or track_artist
    # Titles
    track_title  = track.get("title") or "Untitled"
    album_title  = album.get("title") or "Untitled"
    # Track number
    track_num    = track.get("track_num")
    # Year from datePublished ("02 Jan 2026 17:48:23 GMT" → "2026")
    year = None
    date_str = album.get("datePublished") or ""
    m = re.search(r'\b(\d{4})\b', date_str)
    if m:
        year = m.group(1)

    tags.delall("TIT2"); tags.add(TIT2(encoding=3, text=track_title))
    tags.delall("TPE1"); tags.add(TPE1(encoding=3, text=track_artist))
    tags.delall("TPE2"); tags.add(TPE2(encoding=3, text=album_artist))
    tags.delall("TALB"); tags.add(TALB(encoding=3, text=album_title))
    if track_num is not None:
        try:
            track_num = max(1, int(track_num))
        except (ValueError, TypeError):
            track_num = None
    if track_num:
        tags.delall("TRCK"); tags.add(TRCK(encoding=3, text=str(track_num)))
    if year:
        tags.delall("TDRC"); tags.add(TDRC(encoding=3, text=year))

    out = io.BytesIO()
    tags.save(out, v2_version=3)
    tag_bytes = out.getvalue()

    # Strip any existing ID3 header from the raw MP3, prepend our new tags
    raw = mp3_data
    if raw[:3] == b"ID3":
        # Skip existing ID3 block: header is 10 bytes, size in bytes 6-9 (syncsafe)
        size_bytes = raw[6:10]
        tag_size = (
            (size_bytes[0] & 0x7F) << 21 |
            (size_bytes[1] & 0x7F) << 14 |
            (size_bytes[2] & 0x7F) << 7  |
            (size_bytes[3] & 0x7F)
        ) + 10
        raw = raw[tag_size:]

    return tag_bytes + raw


# ── Per-WACZ extraction ───────────────────────────────────────────────────────

def extract_wacz(
    wacz_path: Path,
    output_root: Path,
    album: dict,
    track_covers: bool,
) -> None:
    """Extract all audio and cover art from a single WACZ file."""

    title   = album.get("title", "untitled")
    item_id = album.get("item_id", "unknown")
    art_id  = album.get("art_id")

    # Output folder: <output_root>/<Album Title> [<item_id>]/
    folder_name = f"{safe_filename(title)} [{item_id}]"
    out_dir = output_root / folder_name
    out_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Output folder: %s", out_dir)

    with zipfile.ZipFile(wacz_path, "r") as wacz:
        cdx_records = _load_cdx(wacz)
        audio_map   = _find_audio_records(cdx_records)

        # ── Album cover ───────────────────────────────────────────────────────
        if art_id:
            cover_rec = _find_image_record(cdx_records, art_id)
            if cover_rec:
                cover_data = _extract_record_body(wacz, cover_rec)
                if cover_data:
                    # Determine extension from URL (usually no extension on _0)
                    cover_url = cover_rec.get("url", "")
                    ext = "jpg"  # Bandcamp _0 covers are always JPEG
                    cover_path = out_dir / f"cover.{ext}"
                    cover_path.write_bytes(cover_data)
                    logger.info("Saved album cover: %s (%d bytes)", cover_path.name, len(cover_data))
                else:
                    logger.warning("Could not extract album cover body from WARC")
            else:
                logger.warning("Album cover (art_id=%s) not found in WACZ index", art_id)

        # ── Tracks ────────────────────────────────────────────────────────────
        trackinfo = album.get("trackinfo", [])
        if not trackinfo:
            logger.warning("No trackinfo in metadata - cannot match tracks")
            return

        # Track covers: collect seen image hashes to avoid saving duplicates
        saved_cover_hashes: set[str] = set()
        if track_covers and art_id:
            # Pre-register album cover hash so we don't re-save it as a track cover
            cover_rec = _find_image_record(cdx_records, art_id)
            if cover_rec:
                cover_data = _extract_record_body(wacz, cover_rec)
                if cover_data:
                    saved_cover_hashes.add(_image_hash(cover_data))

        for track in trackinfo:
            track_id  = track.get("track_id")
            track_num = track.get("track_num")
            track_title = track.get("title", "untitled")
            track_art_id = track.get("art_id")

            if not track_id:
                logger.warning("Track '%s' has no track_id - skipping", track_title)
                continue

            # ── Audio ─────────────────────────────────────────────────────────
            audio_rec = audio_map.get(int(track_id))
            if not audio_rec:
                logger.warning(
                    "No audio found in WACZ for track_id=%s ('%s')",
                    track_id, track_title,
                )
                continue

            mp3_data = _extract_record_body(wacz, audio_rec)
            if not mp3_data:
                logger.warning("Could not extract audio body for track_id=%s", track_id)
                continue

            mp3_data = _apply_id3_tags(mp3_data, track, album)

            # Filename: <track_num> - <title> [<track_id>].mp3
            # track_num 0 means a standalone track page - treat as track 1
            if track_num is not None:
                try:
                    track_num = max(1, int(track_num))
                except (ValueError, TypeError):
                    track_num = None
            num_str = str(track_num).zfill(2) if track_num else "01"
            mp3_name = f"{num_str} - {safe_filename(track_title)} [{track_id}].mp3"
            mp3_path = out_dir / mp3_name
            mp3_path.write_bytes(mp3_data)
            logger.info("Saved: %s (%d bytes)", mp3_name, len(mp3_data))

            # ── Per-track cover ───────────────────────────────────────────────
            if track_covers and track_art_id and track_art_id != art_id:
                track_cover_rec = _find_image_record(cdx_records, track_art_id)
                if track_cover_rec:
                    tc_data = _extract_record_body(wacz, track_cover_rec)
                    if tc_data:
                        h = _image_hash(tc_data)
                        if h not in saved_cover_hashes:
                            tc_name = f"{num_str}_cover.jpg"
                            (out_dir / tc_name).write_bytes(tc_data)
                            saved_cover_hashes.add(h)
                            logger.info("Saved track cover: %s", tc_name)
                        else:
                            logger.debug(
                                "Track cover for '%s' is a duplicate - skipping", track_title
                            )


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="extract.py",
        description="Extract audio and covers from Bandcamp WACZ files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        metavar="PATH",
        help="One or more WACZ files or directories containing WACZ files.",
    )
    parser.add_argument(
        "--output", "-o",
        metavar="DIR",
        default=None,
        help=(
            "Root directory for extracted files. "
            "Defaults to a subfolder alongside each WACZ."
        ),
    )
    parser.add_argument(
        "--track-covers",
        action="store_true",
        help=(
            "Save per-track covers when they differ from the album cover "
            "(compared by art_id and image hash)."
        ),
    )
    parser.add_argument(
        "--ask",
        action="store_true",
        help=(
            "If metadata JSON cannot be found automatically, "
            "prompt for the path instead of aborting."
        ),
    )
    parser.add_argument(
        "--auto-pick",
        action="store_true",
        dest="auto_pick",
        help=(
            "If the same item_id appears in multiple artist JSONs, "
            "automatically pick the first (alphabetically) instead of prompting."
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

    # Collect all WACZ files from inputs
    wacz_files: list[Path] = []
    for inp in args.inputs:
        p = Path(inp)
        if p.is_dir():
            found = sorted(p.glob("*.wacz"))
            if not found:
                logger.warning("No WACZ files found in directory: %s", p)
            wacz_files.extend(found)
        elif p.is_file() and p.suffix.lower() == ".wacz":
            wacz_files.append(p)
        else:
            logger.warning("Not a WACZ file or directory: %s", p)

    if not wacz_files:
        print("No WACZ files to process.")
        sys.exit(0)

    # Possible artists/ dirs to search for artist JSONs.
    # Always include: CWD/artists and the project root (two levels up from this
    # script, i.e. bandcamp-wacz-archiver/artists/) so the script works regardless
    # of which directory it is invoked from.
    _script_root = Path(__file__).resolve().parent.parent

    # Collect unique artists/ dirs by resolved path to avoid scanning same dir twice
    _seen_resolved: set[Path] = set()
    artists_dirs: list[Path] = []

    def _add_artists_dir(p: Path) -> None:
        r = p.resolve()
        if r not in _seen_resolved:
            _seen_resolved.add(r)
            artists_dirs.append(p)

    _add_artists_dir(Path.cwd() / "artists")
    _add_artists_dir(_script_root / "artists")

    ok = 0
    fail = 0
    for wacz_path in wacz_files:
        logger.info("Processing: %s", wacz_path)

        # Add WACZ parent's artists/ sibling as a search root
        _add_artists_dir(wacz_path.parent.parent / "artists")

        album = _load_metadata(wacz_path, ask=args.ask, artists_dirs=artists_dirs, auto_pick=args.auto_pick)
        if not album:
            fail += 1
            continue

        output_root = Path(args.output) if args.output else wacz_path.parent

        try:
            extract_wacz(
                wacz_path=wacz_path,
                output_root=output_root,
                album=album,
                track_covers=args.track_covers,
            )
            ok += 1
        except Exception as exc:
            logger.error("Failed to extract %s: %s", wacz_path.name, exc, exc_info=args.debug)
            fail += 1

    print(f"\n── Summary ─────────────────────────────")
    print(f"  Succeeded : {ok}")
    print(f"  Failed    : {fail}")
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
