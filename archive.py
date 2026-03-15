#!/usr/bin/env python3
"""
archive.py - crawl Bandcamp releases to WACZ and upload to the Internet Archive.

Usage
─────
  # Smart pipeline (resolves artist, fetches/updates metadata, archives new/updated):
  python archive.py --url https://artist.bandcamp.com/album/album-name
  python archive.py --url https://artist.bandcamp.com

  # Quick mode (single-release smart pipeline - checks JSON, detects changes, crawls if needed):
  python archive.py --quick --url https://artist.bandcamp.com/album/album-name

  # Dumb mode (just crawl and write WACZ + sidecar, no JSON interaction at all):
  python archive.py --dumb --url https://artist.bandcamp.com/album/album-name

  # Archive every URL in a list file (no artist resolution):
  python archive.py --list artists/Artist [id]/bandcamp-dump.lst

  # Check that Podman and the Browsertrix image are available:
  python archive.py --check-podman

  # Skip upload after archiving:
  python archive.py --url https://artist.bandcamp.com --no-upload
"""

from __future__ import annotations

import argparse
import atexit
import logging
import os
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from bandcamp_wacz.crawl import crawl_album, crawl_list
from bandcamp_wacz.config import WACZ_OUTPUT_DIR, ARTISTS_DIR

DEFAULT_LIST_FILE = "bandcamp-dump.lst"


# ── Logging ───────────────────────────────────────────────────────────────────

class _ColorFormatter(logging.Formatter):
    """Logging formatter that tints upload-thread log lines cyan.

    Any log line emitted from a thread whose name starts with ``upload-``
    (e.g. ``upload-12345``) is rendered in cyan so it stands out from the main
    crawl output when both are interleaved on the terminal.  Checking the
    thread name rather than the logger name means we catch every logger that
    runs inside the upload thread — including the logger inside upload.py itself
    which has its own module-level name.

    Color codes are only emitted when stderr is a real TTY; they are suppressed
    automatically when output is redirected to a file or pipe.
    """
    _CYAN  = "\033[36m"
    _RESET = "\033[0m"
    _is_tty = hasattr(sys.stderr, "isatty") and sys.stderr.isatty()

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        if self._is_tty and threading.current_thread().name.startswith("upload-"):
            return f"{self._CYAN}{msg}{self._RESET}"
        return msg


# ── List file I/O ─────────────────────────────────────────────────────────────

def read_url_list(path: Path) -> list[str]:
    """Return non-empty, non-comment lines from a .lst file."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]


# ── Podman check ──────────────────────────────────────────────────────────────

def check_podman() -> None:
    """Verify Podman is installed and the Browsertrix image is available."""
    from bandcamp_wacz.config import BROWSERTRIX_IMAGE

    print("── Podman version ───────────────────────────────────")
    result = subprocess.run(["podman", "--version"], capture_output=True, text=True)
    if result.returncode != 0:
        print("  ERROR: podman not found or not runnable.")
        print("  Install it with:  sudo pacman -S podman")
        sys.exit(1)
    print(" ", result.stdout.strip())

    print(f"\n── Browsertrix image: {BROWSERTRIX_IMAGE} ──")
    result = subprocess.run(
        ["podman", "image", "inspect", BROWSERTRIX_IMAGE],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print("  Image is already available locally.")
    else:
        print("  Image not found locally. Pulling now (this may take a while)...")
        pull = subprocess.run(["podman", "pull", BROWSERTRIX_IMAGE])
        if pull.returncode != 0:
            print("  ERROR: could not pull image.")
            sys.exit(1)
        print("  Pull complete.")


# ── Artist resolution ─────────────────────────────────────────────────────────

def _to_artist_root(url: str) -> str:
    """Strip an album/track URL down to the artist root."""
    from urllib.parse import urlparse, urlunparse
    p = urlparse(url)
    return urlunparse(p._replace(path="/", query="", fragment=""))


def _fetch_band_id(artist_root: str) -> int | None:
    """Fetch the artist page and extract band_id."""
    from bandcamp_wacz.bandcamp import fetch_url
    import bs4, json, re
    try:
        resp = fetch_url(artist_root)
        soup = bs4.BeautifulSoup(resp.text, "lxml")
    except Exception:
        try:
            soup = bs4.BeautifulSoup(resp.text, "html.parser")
        except Exception:
            return None

    div = soup.find("div", {"id": "pagedata"})
    if not div:
        return None
    try:
        blob = json.loads(div.get("data-blob", "{}"))
    except Exception:
        return None

    band_id = blob.get("id")
    if band_id:
        return int(band_id)

    lo = blob.get("lo_querystr", "") or ""
    m = re.search(r'band_id=(\d+)', lo)
    if m:
        return int(m.group(1))

    return None


def _find_artist_folder(band_id: int) -> Path | None:
    """Return the artist folder whose name ends with [band_id], or None."""
    if not ARTISTS_DIR.exists():
        return None
    for folder in ARTISTS_DIR.iterdir():
        if folder.is_dir() and folder.name.endswith(f"[{band_id}]"):
            return folder
    return None


def _urls_to_archive(artist_folder: Path) -> list[str]:
    """
    Read the artist JSON and return URLs for all releases that need archiving
    (archived=False). Pre-orders and releases with no tracks are included —
    they have archival value and update_metadata will re-queue them if they
    change (e.g. pre-order goes live with full tracks).
    """
    import json as _json
    json_path = artist_folder / f"{artist_folder.name}.json"
    if not json_path.exists():
        return []

    try:
        data = _json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:
        return []

    urls = []
    for key, releases in data.items():
        if key.startswith("_") or not isinstance(releases, list):
            continue
        for r in releases:
            if r.get("archived"):
                continue
            url = r.get("url")
            if url:
                urls.append(url)
    return urls


def _has_unuploaded(artist_folder: Path) -> bool:
    """Return True if any archived-but-not-yet-uploaded releases exist in the artist JSON."""
    import json as _json
    json_path = artist_folder / f"{artist_folder.name}.json"
    if not json_path.exists():
        return False
    try:
        data = _json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    for key, releases in data.items():
        if key.startswith("_") or not isinstance(releases, list):
            continue
        for r in releases:
            if r.get("archived") and not r.get("uploaded"):
                return True
    return False


def _item_id_from_wacz(wacz_path: Path) -> int | None:
    """
    Extract item_id from a WACZ file. Tries embedded datapackage.json first,
    then falls back to parsing the filename for the [item_id] suffix pattern.
    """
    import zipfile, json as _json, re as _re
    # Try embedded metadata first
    try:
        with zipfile.ZipFile(wacz_path, "r") as zf:
            if "datapackage.json" in zf.namelist():
                pkg = _json.loads(zf.read("datapackage.json").decode("utf-8"))
                item_id = pkg.get("bandcamp_item_id")
                if item_id is not None:
                    return int(item_id)
    except Exception:
        pass
    # Fallback: parse filename for trailing [item_id] pattern
    m = _re.search(r'\[(\d+)\]\.wacz$', wacz_path.name)
    if m:
        return int(m.group(1))
    return None



def _releases_missing_wacz(artist_folder: Path, output_dir: Path) -> list[dict]:
    """
    Return releases that are archived=True but have no WACZ file on disk
    and have not been uploaded yet. These need to be re-crawled.

    Scans WACZ_OUTPUT_DIR recursively so that WACZs produced by previous
    interrupted runs (which live in their own job subdirectories) are found.
    """
    import json as _json
    json_path = artist_folder / f"{artist_folder.name}.json"
    if not json_path.exists():
        return []
    try:
        data = _json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:
        return []

    # Build a set of item_ids present in any WACZ anywhere under WACZ_OUTPUT_DIR,
    # falling back to the current job output_dir if WACZ_OUTPUT_DIR doesn't exist.
    search_root = WACZ_OUTPUT_DIR if WACZ_OUTPUT_DIR.exists() else output_dir
    present_item_ids: set[int] = set()
    for wacz_path in search_root.rglob("*.wacz"):
        item_id = _item_id_from_wacz(wacz_path)
        if item_id is not None:
            present_item_ids.add(item_id)

    missing = []
    for key, releases in data.items():
        if key.startswith("_") or not isinstance(releases, list):
            continue
        for r in releases:
            if not r.get("archived"):
                continue
            if r.get("uploaded"):
                continue
            item_id = r.get("item_id")
            if not item_id:
                continue
            if item_id not in present_item_ids:
                missing.append(r)
    return missing


def _reset_archived(artist_folder: Path, item_ids: list[int]) -> None:
    """
    Reset archived=False for specific item_ids so they re-enter the crawl queue.
    """
    import json as _json
    json_path = artist_folder / f"{artist_folder.name}.json"
    try:
        data = _json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:
        return
    changed = False
    for key, releases in data.items():
        if key.startswith("_") or not isinstance(releases, list):
            continue
        for r in releases:
            if r.get("item_id") in item_ids:
                r["archived"] = False
                changed = True
    if changed:
        json_path.write_text(_json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8")


# ── Artist grouping ───────────────────────────────────────────────────────────

def _group_urls_by_artist(
    urls: list[str],
    logger: logging.Logger,
) -> dict[int, tuple[str, list[str]]]:
    """
    Resolve every URL to its artist root + band_id and group them.

    Returns {band_id: (artist_root, [original_urls])} so the smart pipeline
    can be called once per artist when multiple artists are supplied.
    """
    groups: dict[int, tuple[str, list[str]]] = {}
    for url in urls:
        root = _to_artist_root(url)
        logger.info("Resolving artist for: %s", root)
        band_id = _fetch_band_id(root)
        if band_id is None:
            logger.debug("Could not get band_id from root, trying original URL: %s", url)
            band_id = _fetch_band_id(url)
        if band_id is None:
            logger.error("Could not determine band_id for %s - aborting.", url)
            sys.exit(1)
        logger.info("band_id=%s", band_id)
        if band_id not in groups:
            groups[band_id] = (root, [])
        groups[band_id][1].append(url)
    return groups


# ── Smart pipeline ────────────────────────────────────────────────────────────

def run_smart_pipeline(
    urls: list[str],
    output_dir: Path | None,
    logger: logging.Logger,
    skip_metadata: bool = False,
    skip_update: bool = False,
    one_by_one: bool = False,
    no_upload: bool = False,
    fetch_only: bool = False,
) -> None:
    """
    Full pipeline:
      1. Resolve all URLs to artist roots and fetch band_ids
      2. Abort if multiple artists detected
      3. Run fetch_metadata or update_metadata as appropriate
      4. Archive all releases with archived=False
    """
    # Step 1: resolve artist roots and band_ids.
    # Try the artist root first; if Bandcamp redirects it to an album page
    # (single-release artists have no dedicated artist page), fall back to
    # fetching band_id from the original URL directly.
    artist_roots: dict[str, int | None] = {}  # root_url → band_id
    original_urls: list[str] = list(urls)      # keep originals for fetch_metadata fallback

    for url in urls:
        root = _to_artist_root(url)
        if root in artist_roots:
            continue
        logger.info("Resolving artist for: %s", root)
        band_id = _fetch_band_id(root)
        if band_id is None:
            # Root may redirect to the album - try the original URL
            logger.debug("Could not get band_id from root, trying original URL: %s", url)
            band_id = _fetch_band_id(url)
        if band_id is None:
            logger.error("Could not determine band_id for %s - aborting.", url)
            sys.exit(1)
        artist_roots[root] = band_id
        logger.info("band_id=%s", band_id)

    # Step 2: abort if multiple artists
    unique_band_ids = set(artist_roots.values())
    if len(unique_band_ids) > 1:
        logger.error(
            "URLs resolve to multiple artists (band_ids: %s). "
            "Pass URLs from a single artist, or use --quick to bypass resolution.",
            ", ".join(str(b) for b in unique_band_ids),
        )
        sys.exit(1)

    band_id = next(iter(unique_band_ids))
    artist_root = next(iter(artist_roots))

    # Step 3: fetch or update metadata (unless --skip-metadata)
    artist_folder = _find_artist_folder(band_id)
    # A folder with no real JSON (e.g. fetch was interrupted before finishing)
    # should be treated the same as no folder — run fetch_metadata, which will
    # auto-resume from the .partial.json if one exists.
    artist_json_exists = (
        artist_folder is not None
        and (artist_folder / f"{artist_folder.name}.json").exists()
    )

    if skip_metadata:
        if not artist_folder:
            logger.error(
                "--skip-metadata was set but no artist folder found for band_id=%s. "
                "Run without --skip-metadata first to fetch metadata.",
                band_id,
            )
            sys.exit(1)
        if not artist_json_exists:
            logger.error(
                "--skip-metadata was set but the artist JSON is missing (fetch may have been "
                "interrupted). Run without --skip-metadata to resume the fetch.",
            )
            sys.exit(1)
        logger.info("Skipping metadata fetch/update (--skip-metadata).")
    elif artist_json_exists and skip_update:
        logger.info("Artist JSON is complete — skipping update_metadata (--skip-update).")
    elif artist_json_exists:
        logger.info("Artist folder found: %s - running update_metadata.", artist_folder.name)
        from update_metadata import update_artist
        from fetch_metadata import Bandcamp
        scraper = Bandcamp()
        success = update_artist(artist_root, scraper, dry_run=False, original_urls=original_urls)
        if not success:
            logger.error("update_metadata failed - aborting.")
            sys.exit(1)
        # Refresh folder reference after update
        artist_folder = _find_artist_folder(band_id)
    else:
        if artist_folder:
            partial = artist_folder / f"{artist_folder.name}.json.partial"
            if partial.exists():
                logger.info(
                    "Artist folder found but JSON is incomplete (interrupted fetch) — "
                    "resuming fetch_metadata from partial file."
                )
            else:
                logger.info(
                    "Artist folder found but JSON is missing — running fetch_metadata."
                )
        else:
            logger.info("No artist folder found - running fetch_metadata.")
        import subprocess as _sp
        # Always pass artist_root first so fetch_metadata discovers the full /music
        # grid. Also append any original album URLs as fallback: for true single-release
        # artists whose grid is empty they get picked up directly; for artists with a
        # full grid they are deduplicated away.
        fetch_urls = [artist_root] + [u for u in original_urls if u != artist_root]
        fetch_args = [sys.executable, "fetch_metadata.py"] + fetch_urls
        result = _sp.run(fetch_args, capture_output=False)
        if result.returncode != 0:
            logger.error("fetch_metadata failed - aborting.")
            sys.exit(1)
        artist_folder = _find_artist_folder(band_id)
        if not artist_folder:
            logger.error("Artist folder still not found after fetch_metadata - aborting.")
            sys.exit(1)

    # If fetch_only, stop here — crawling will happen in a separate pass.
    if fetch_only:
        logger.info("Metadata fetch complete for band_id=%s (--fetch-first).", band_id)
        return False

    # Step 4: detect releases marked archived but with no WACZ file on disk
    missing = _releases_missing_wacz(artist_folder, output_dir)
    if missing:
        logger.warning(
            "%d release(s) marked archived but WACZ file missing — refreshing metadata and re-queuing:",
            len(missing),
        )
        for r in missing:
            logger.warning("  %s (item_id=%s)", r.get("title"), r.get("item_id"))

        # Refresh metadata for each missing release individually
        from fetch_metadata import Bandcamp as _Bandcamp
        from update_metadata import update_release as _update_release
        scraper = _Bandcamp()
        for r in missing:
            release_url = r.get("url")
            if release_url:
                logger.info("Refreshing metadata for: %s", release_url)
                _update_release(release_url, scraper, dry_run=False)

        # Reset archived=False so they re-enter the crawl queue below
        _reset_archived(artist_folder, [r["item_id"] for r in missing if r.get("item_id")])

    # Step 5: archive all releases that need it
    to_archive = _urls_to_archive(artist_folder)
    if not to_archive:
        if _has_unuploaded(artist_folder):
            print("Nothing to crawl - all releases already archived. Proceeding to upload...")
            return True
        print("Nothing to archive - all releases are already up to date.")
        return False

    print(f"\nArchiving {len(to_archive)} release(s)...\n")

    ok: list[str] = []
    err: list[str] = []

    if one_by_one:
        for i, url in enumerate(to_archive, 1):
            print(f"[{i}/{len(to_archive)}] {url}")
            result = crawl_list([url], output_dir=output_dir, skip_errors=True)
            wacz = next(iter(result.values()))
            if isinstance(wacz, Path):
                ok.append(url)
                if not no_upload:
                    run_upload(output_dir, no_upload=False, logger=logger, artist_folder=artist_folder)
            else:
                err.append(url)
                logger.error("Failed: %s - %s", url, wacz)
    else:
        results = crawl_list(to_archive, output_dir=output_dir, skip_errors=True)
        ok  = [u for u, v in results.items() if isinstance(v, Path)]
        err = [u for u, v in results.items() if isinstance(v, Exception)]

    print(f"\n── Summary ────────────────────────────────────────")
    print(f"  Succeeded : {len(ok)}")
    print(f"  Failed    : {len(err)}")
    if err:
        print("\n  Failed URLs:")
        for u in err:
            print(f"    {u}")
    if err:
        sys.exit(1)
    return True


def run_quick_pipeline(
    urls: list[str],
    output_dir: Path | None,
    logger: logging.Logger,
    no_upload: bool = False,
) -> None:
    """
    Quick single-release pipeline for explicit album URLs.

    For each URL:
      1. Fetch fresh metadata from Bandcamp.
      2. Look the release up in the artist JSON by item_id.
         - Not found → add it to the JSON (archived=False, uploaded=False).
         - Found, no changes, already archived+uploaded → skip (nothing to do).
         - Found, no changes, not fully done → proceed to crawl.
         - Found, changes detected → apply_changes (resets archived/uploaded), crawl.
      3. Crawl the release (process_archived_wacz marks it archived in the JSON
         and writes the sidecar .json).
      4. Upload unless --no-upload.

    Unlike the smart pipeline this never touches the /music grid and never
    fetches the full discography — it operates on exactly the given URLs.
    """
    from fetch_metadata import Bandcamp
    from update_metadata import (
        find_artist_folder, load_artist_json,
        detect_changes, apply_changes,
    )
    import json as _json

    scraper = Bandcamp()

    for url in urls:
        logger.info("Quick pipeline: %s", url)

        # ── Step 1: fetch fresh metadata ──────────────────────────────────────
        try:
            fresh = scraper.parse(url)
        except KeyboardInterrupt:
            raise KeyboardInterrupt() from None
        except Exception as exc:
            logger.error("Could not parse %s: %s", url, exc)
            continue

        if not fresh:
            logger.error("No data returned for %s", url)
            continue

        band_id = fresh.get("band_id")
        item_id = fresh.get("item_id")

        if not band_id or not item_id:
            logger.error("Missing band_id or item_id for %s - skipping.", url)
            continue

        # ── Step 2: locate artist JSON ────────────────────────────────────────
        folder = find_artist_folder(band_id)
        need_crawl = True

        if folder:
            result = load_artist_json(folder)
            if result:
                json_path, data = result
                artist_key = next((k for k in data if not k.startswith("_")), None)
                if artist_key:
                    existing_releases: list[dict] = data[artist_key]
                    existing_by_id = {
                        r["item_id"]: r for r in existing_releases if r.get("item_id")
                    }

                    if item_id in existing_by_id:
                        existing = existing_by_id[item_id]
                        changed = detect_changes(existing, fresh)

                        if not changed:
                            if existing.get("archived") and existing.get("uploaded"):
                                logger.info(
                                    "'%s' already archived and uploaded with no changes - skipping.",
                                    fresh.get("title"),
                                )
                                need_crawl = False
                            else:
                                logger.info(
                                    "'%s' unchanged but not fully done (archived=%s uploaded=%s) - proceeding.",
                                    fresh.get("title"),
                                    existing.get("archived"), existing.get("uploaded"),
                                )
                        else:
                            field_names = ", ".join(changed.keys())
                            logger.info(
                                "'%s' has changes (%s) - updating metadata and re-crawling.",
                                fresh.get("title"), field_names,
                            )
                            apply_changes(existing, fresh, changed)
                            try:
                                json_path.write_text(
                                    _json.dumps(data, indent=4, ensure_ascii=False),
                                    encoding="utf-8",
                                )
                            except OSError as exc:
                                logger.error("Could not write artist JSON: %s", exc)
                                continue
                    else:
                        # Release not in JSON yet — add it before crawling so
                        # process_archived_wacz can find it by item_id.
                        fresh.setdefault("archived", False)
                        fresh.setdefault("uploaded", False)
                        existing_releases.append(fresh)
                        try:
                            json_path.write_text(
                                _json.dumps(data, indent=4, ensure_ascii=False),
                                encoding="utf-8",
                            )
                            logger.info(
                                "Added new release '%s' to artist JSON.", fresh.get("title")
                            )
                        except OSError as exc:
                            logger.error("Could not write artist JSON: %s", exc)
                            continue
                else:
                    logger.warning("Malformed artist JSON for band_id=%s - will crawl without JSON update.", band_id)
            else:
                logger.warning("Could not load artist JSON for band_id=%s - will crawl without JSON update.", band_id)
        else:
            logger.warning(
                "No artist folder found for band_id=%s. "
                "Run fetch_metadata.py first if you want full metadata tracking. "
                "Crawling anyway.",
                band_id,
            )

        if not need_crawl:
            continue

        # ── Step 3: crawl ─────────────────────────────────────────────────────
        try:
            wacz_path = crawl_album(url, output_dir=output_dir)
        except KeyboardInterrupt:
            raise KeyboardInterrupt() from None
        except Exception as exc:
            logger.error("Crawl failed for %s: %s", url, exc)
            continue

        # ── Step 4: upload ────────────────────────────────────────────────────
        if not no_upload:
            run_upload(output_dir, no_upload=False, logger=logger, artist_folder=folder)


def _get_unuploaded_item_ids(artist_folder: Path) -> list[int]:
    """Return item_ids for releases that are archived but not yet uploaded."""
    import json as _json
    json_path = artist_folder / f"{artist_folder.name}.json"
    if not json_path.exists():
        return []
    try:
        data = _json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    ids = []
    for key, releases in data.items():
        if key.startswith("_") or not isinstance(releases, list):
            continue
        for r in releases:
            if r.get("archived") and not r.get("uploaded"):
                item_id = r.get("item_id")
                if item_id:
                    ids.append(int(item_id))
    return ids


def run_upload(output_dir, no_upload: bool, logger, artist_folder: Path | None = None) -> None:
    """Upload all finished WACZs for the current run.

    Search strategy:
      1. Flat glob of the current job's output_dir — covers the normal case.
      2. If nothing found and artist_folder is known, search WACZ_OUTPUT_DIR
         recursively but only for the specific [item_id].wacz patterns of
         releases that are archived-but-not-uploaded. This recovers WACZs
         from a previous interrupted job without risking picking up files
         from a concurrently running job.
    """
    if no_upload:
        logger.info("Skipping upload (--no-upload).")
        return
    import importlib.util
    _upload_path = Path(__file__).resolve().parent / "upload.py"
    _spec = importlib.util.spec_from_file_location("upload", _upload_path)
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)

    # Step 1: check current job dir
    wacz_files = sorted(output_dir.glob("*.wacz"))

    # Step 2: targeted search by item_id across WACZ_OUTPUT_DIR for any that
    # weren't found in the job dir — covers WACZs from previous interrupted runs.
    if artist_folder and WACZ_OUTPUT_DIR.exists():
        item_ids = _get_unuploaded_item_ids(artist_folder)
        found_item_ids = set()
        for wacz_path in wacz_files:
            item_id = _item_id_from_wacz(wacz_path)
            if item_id is not None:
                found_item_ids.add(item_id)
        missing_ids = [iid for iid in item_ids if iid not in found_item_ids]
        if missing_ids:
            logger.debug(
                "%d WACZ(es) not in job dir — searching %s for specific item_id(s).",
                len(missing_ids), WACZ_OUTPUT_DIR,
            )
            seen = {p for p in wacz_files}
            for item_id in missing_ids:
                suffix = f"[{item_id}].wacz"
                for wacz_path in WACZ_OUTPUT_DIR.rglob("*.wacz"):
                    if wacz_path.name.endswith(suffix) and wacz_path not in seen:
                        seen.add(wacz_path)
                        wacz_files.append(wacz_path)
            wacz_files.sort()

    if not wacz_files:
        logger.info("No WACZ files to upload.")
        return
    logger.info("Uploading %d WACZ file(s)...", len(wacz_files))
    for wacz_path in wacz_files:
        try:
            _mod.upload_release(wacz_path, dry_run=False)
        except KeyboardInterrupt:
            raise KeyboardInterrupt() from None



# ── Pipeline upload helper ────────────────────────────────────────────────────

def _upload_thread_target(
    output_dir: Path,
    no_upload: bool,
    logger: logging.Logger,
    artist_folder: Path | None,
    error_sink: list,
) -> None:
    """Target for background upload threads spawned by --pipeline.

    Runs *run_upload* in a daemon thread so the main thread can immediately
    start crawling the next artist.  After a successful upload, cleans up the
    per-artist output subdirectory (the band_<id>/ dir created by the pipeline)
    so the parent job directory is left empty and atexit can remove it cleanly.

    Exceptions are appended to *error_sink* rather than propagated (daemon
    threads have no caller to propagate to); the main thread checks *error_sink*
    after joining all threads.
    """
    try:
        run_upload(output_dir, no_upload, logger, artist_folder=artist_folder)
        # After upload.py finishes, WACZ and sidecar files should have been
        # removed.  Clean up the now-empty per-artist subdir so the parent job
        # directory is empty and atexit can delete it without warnings.
        remaining = (
            list(output_dir.rglob("*.wacz")) +
            list(output_dir.rglob("*.json"))
        )
        if not remaining:
            try:
                shutil.rmtree(output_dir)
                logger.debug("Removed per-artist output directory: %s", output_dir)
            except OSError as exc:
                logger.warning("Could not remove output directory %s: %s", output_dir, exc)
        else:
            logger.warning(
                "Per-artist output directory %s still has %d file(s) after upload — not removing:\n  %s",
                output_dir, len(remaining),
                "\n  ".join(str(p) for p in remaining),
            )
    except KeyboardInterrupt:
        pass  # daemon thread — main thread owns KeyboardInterrupt handling
    except Exception as exc:
        logger.error("Background upload failed: %s", exc)
        error_sink.append(exc)


# ── JSON mode helpers ─────────────────────────────────────────────────────────

def _scan_artists_for_pending(artists_dir: Path) -> list[dict]:
    """
    Scan artists_dir for JSONs that have work remaining, or partial fetches.
    Returns a list of dicts with keys: folder, band_id, name, n_uncrawled, n_unuploaded, partial.
    """
    import json as _json
    import re as _re

    pending = []
    if not artists_dir.exists():
        return pending

    for folder in sorted(artists_dir.iterdir()):
        if not folder.is_dir():
            continue

        m = _re.search(r'\[(\d+)\]$', folder.name)
        band_id = int(m.group(1)) if m else None

        json_path = folder / f"{folder.name}.json"
        partial_path = folder / f"{folder.name}.json.partial"

        # Partial fetch in progress — no full JSON yet
        if not json_path.exists():
            if partial_path.exists():
                pending.append({
                    "folder":       folder,
                    "band_id":      band_id,
                    "name":         folder.name,
                    "n_uncrawled":  0,
                    "n_unuploaded": 0,
                    "partial":      True,
                })
            continue

        try:
            data = _json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        n_uncrawled = 0
        n_unuploaded = 0
        for key, releases in data.items():
            if key.startswith("_") or not isinstance(releases, list):
                continue
            for r in releases:
                if not r.get("archived"):
                    n_uncrawled += 1
                elif not r.get("uploaded"):
                    n_unuploaded += 1

        if n_uncrawled > 0 or n_unuploaded > 0 or partial_path.exists():
            pending.append({
                "folder":       folder,
                "band_id":      band_id,
                "name":         folder.name,
                "n_uncrawled":  n_uncrawled,
                "n_unuploaded": n_unuploaded,
                "partial":      partial_path.exists(),
            })

    return pending


def _prompt_json_selection(pending: list[dict]) -> list[dict]:
    """
    Show pending artists and let the user pick which ones to run.
    Returns the selected subset.
    """
    print("\nArtists with pending work:\n")
    for i, p in enumerate(pending, 1):
        parts = []
        if p["partial"]:
            parts.append("fetch incomplete")
        if p["n_uncrawled"]:
            parts.append(f"{p['n_uncrawled']} to crawl")
        if p["n_unuploaded"]:
            parts.append(f"{p['n_unuploaded']} to upload")
        print(f"  {i:>3}.  {p['name']}")
        print(f"          {', '.join(parts)}")

    print(f"\n  all.  Run all of the above ({len(pending)} artists)")
    print(f"    q.  Quit\n")

    while True:
        try:
            raw = input("Select artists (e.g. 1,3,5 or 'all'): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(0)

        if raw == "q":
            sys.exit(0)

        if raw == "all":
            return pending

        selected = []
        valid = True
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            if not part.isdigit():
                print(f"  Invalid input '{part}' — enter numbers, ranges, or 'all'.")
                valid = False
                break
            idx = int(part) - 1
            if idx < 0 or idx >= len(pending):
                print(f"  Number {part} is out of range.")
                valid = False
                break
            selected.append(pending[idx])

        if valid and selected:
            return selected

        if valid:
            print("  No artists selected.")


def _run_pipeline_for_folder(
    artist_folder: Path,
    output_dir: Path,
    logger: logging.Logger,
    one_by_one: bool = False,
    no_upload: bool = False,
) -> bool:
    """
    Run the crawl + upload pipeline for an already-fetched artist folder,
    skipping all URL resolution and metadata fetching.
    Mirrors steps 4-5 of run_smart_pipeline.
    """
    # Detect releases marked archived but with no WACZ on disk
    missing = _releases_missing_wacz(artist_folder, output_dir)
    if missing:
        logger.warning(
            "%d release(s) marked archived but WACZ file missing — re-queuing:",
            len(missing),
        )
        for r in missing:
            logger.warning("  %s (item_id=%s)", r.get("title"), r.get("item_id"))
        _reset_archived(artist_folder, [r["item_id"] for r in missing if r.get("item_id")])

    to_archive = _urls_to_archive(artist_folder)
    if not to_archive:
        if _has_unuploaded(artist_folder):
            print("Nothing to crawl - all releases already archived. Proceeding to upload...")
            return True
        print("Nothing to archive - all releases are already up to date.")
        return False

    print(f"\nArchiving {len(to_archive)} release(s)...\n")

    ok: list[str] = []
    err: list[str] = []

    if one_by_one:
        for i, url in enumerate(to_archive, 1):
            print(f"[{i}/{len(to_archive)}] {url}")
            result = crawl_list([url], output_dir=output_dir, skip_errors=True)
            wacz = next(iter(result.values()))
            if isinstance(wacz, Path):
                ok.append(url)
                if not no_upload:
                    run_upload(output_dir, no_upload=False, logger=logger, artist_folder=artist_folder)
            else:
                err.append(url)
                logger.error("Failed: %s - %s", url, wacz)
    else:
        results = crawl_list(to_archive, output_dir=output_dir, skip_errors=True)
        ok  = [u for u, v in results.items() if isinstance(v, Path)]
        err = [u for u, v in results.items() if isinstance(v, Exception)]

    print(f"\n── Summary ────────────────────────────────────────")
    print(f"  Succeeded : {len(ok)}")
    print(f"  Failed    : {len(err)}")
    if err:
        print("\n  Failed URLs:")
        for u in err:
            print(f"    {u}")

    return len(ok) > 0


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="archive.py",
        description="Archive Bandcamp releases to WACZ using Browsertrix + Podman.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--url", "-u", metavar="URL", nargs="+",
        help="One or more Bandcamp album or artist URLs.")
    source.add_argument("--slug", "-s", metavar="SLUG", nargs="+",
        help="One or more Bandcamp artist slugs (e.g. 'alyaserpentis'). "
             "Expanded to https://<slug>.bandcamp.com/ before processing.")
    source.add_argument("--list", "-l", nargs="?", const=DEFAULT_LIST_FILE, metavar="FILE",
        help=f"Archive all URLs in FILE directly (default: {DEFAULT_LIST_FILE}).")
    source.add_argument("--json", "-j", action="store_true",
        help="Scan the artists/ directory for artists with releases still to crawl or upload, "
             "present a numbered list, and let you pick which to resume. "
             "Skips metadata fetching by default — combine with --fetch-first to update first.")
    parser.add_argument("--quick", action="store_true",
        help="Single-release smart pipeline: check JSON for changes, skip if already archived+uploaded, "
             "update _history if changed, add to JSON if new, then crawl and upload.")
    parser.add_argument("--dumb", action="store_true",
        help="Just crawl the given URL(s) and write WACZ + sidecar. No JSON checking, no metadata updates.")
    parser.add_argument("--output", "-o", metavar="DIR", default=None,
        help=f"Output directory for WACZ files (default: {WACZ_OUTPUT_DIR}).")
    parser.add_argument("--check-podman", action="store_true",
        help="Verify Podman and the Browsertrix image are available, then exit.")
    parser.add_argument("--keep-on-error", action="store_true",
        help="Abort list processing on first error instead of skipping.")
    parser.add_argument("--fetch-first", action="store_true",
        help="Fetch metadata for all artists before starting any crawling. "
             "Useful when archiving multiple artists at once so all JSONs are "
             "up to date before the first crawl begins.")
    parser.add_argument("--skip-update", action="store_true",
        help="Skip update_metadata for artists whose JSON is already complete. "
             "Incomplete or missing JSONs are still fetched. "
             "Useful when you know metadata is current and want to go straight to crawling.")
    parser.add_argument("--skip-metadata", action="store_true",
        help="Skip fetch/update metadata step and archive only what is already queued (archived=False).")
    parser.add_argument("--one-by-one", action="store_true",
        help="Archive and upload one release at a time. Saves disc space for large discographies.")
    parser.add_argument("--pipeline", action="store_true",
        help="When archiving multiple artists, start crawling the next artist as soon as the "
             "current one finishes crawling, while its upload runs in the background. "
             "Reduces total wall-clock time because uploading and crawling are independent. "
             "Cannot be combined with --one-by-one.")
    parser.add_argument("--no-upload", action="store_true",
        help="Skip uploading to archive.org after archiving.")
    parser.add_argument("--filename-truncation", metavar="STYLE",
        choices=["end", "middle", "hash"], default=None,
        help="How to truncate filenames that exceed the archive.org 230-byte limit: "
             "'end' (cut the title short), 'middle' (keep start and end with ... in between), "
             "'hash' (replace title with a short SHA-1 digest). "
             "Overrides FILENAME_TRUNCATION in .env (default: end).")
    parser.add_argument("--debug", "-d", action="store_true",
        help="Enable verbose debug logging.")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    handler = logging.StreamHandler()
    handler.setFormatter(_ColorFormatter(
        fmt="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        handlers=[handler],
    )
    logger = logging.getLogger("archive")

    if getattr(args, "pipeline", False) and args.one_by_one:
        parser.error("--pipeline and --one-by-one are mutually exclusive.")

    # Override config with CLI flag if provided
    if args.filename_truncation:
        import bandcamp_wacz.config as _cfg
        _cfg.FILENAME_TRUNCATION = args.filename_truncation
        logger.debug("Filename truncation style overridden to: %s", args.filename_truncation)

    if args.check_podman:
        check_podman()
        sys.exit(0)

    if args.output:
        output_dir = Path(args.output)
        output_dir.mkdir(parents=True, exist_ok=True)
        _job_subdir = None  # user-specified path; don't auto-clean it up
    else:
        # Create a unique subdirectory under WACZ_OUTPUT_DIR so that concurrent
        # archive.py jobs never share an output folder.  band_id is embedded for
        # human readability when debugging; the PID + short UUID suffix makes it
        # unique even when the same artist is queued more than once at a time.
        _job_id = f"{os.getpid()}_{uuid.uuid4().hex[:8]}"
        output_dir = WACZ_OUTPUT_DIR / f"job_{_job_id}"
        output_dir.mkdir(parents=True, exist_ok=True)
        _job_subdir = output_dir
        logger.info("Job output directory: %s", output_dir)

        def _cleanup_job_dir() -> None:
            """Remove the auto-created job subdirectory if no .wacz or .json files remain."""
            if not _job_subdir.exists():
                return
            # Safety check: abort if any WACZ or JSON files are still present.
            # This means a crawl finished but upload didn't, so we leave the
            # directory intact for the operator to handle manually.
            leftover = list(_job_subdir.rglob("*.wacz")) + list(_job_subdir.rglob("*.json"))
            if leftover:
                logger.warning(
                    "Job directory %s still contains %d file(s) — not deleting:\n  %s",
                    _job_subdir,
                    len(leftover),
                    "\n  ".join(str(p) for p in leftover),
                )
                return
            try:
                shutil.rmtree(_job_subdir)
                logger.debug("Removed job directory: %s", _job_subdir)
            except OSError as exc:
                logger.warning("Could not remove job directory %s: %s", _job_subdir, exc)

        atexit.register(_cleanup_job_dir)

    # ── List mode (raw URL list, no artist resolution) ────────────────────────
    if args.list is not None:
        list_path = Path(args.list)
        if not list_path.exists():
            print(f"Error: list file not found: {list_path}", file=sys.stderr)
            sys.exit(1)
        urls = read_url_list(list_path)
        if not urls:
            print(f"No URLs found in {list_path}.")
            sys.exit(0)
        print(f"Found {len(urls)} URL(s) in {list_path}.\n")
        results = crawl_list(urls, output_dir=output_dir, skip_errors=not args.keep_on_error)
        ok  = [u for u, v in results.items() if isinstance(v, Path)]
        err = [u for u, v in results.items() if isinstance(v, Exception)]
        print(f"\n── Summary ────────────────────────────────────────")
        print(f"  Succeeded : {len(ok)}")
        print(f"  Failed    : {len(err)}")
        if err:
            print("\n  Failed URLs:")
            for u in err:
                print(f"    {u}  →  {results[u]}")
            sys.exit(1)
        return

    # ── Slug mode: expand slugs → artist root URLs, then fall through to URL mode ──
    if args.slug:
        args.url = [f"https://{slug.strip().rstrip('/')}.bandcamp.com/" for slug in args.slug]

    # ── URL mode ──────────────────────────────────────────────────────────────
    if args.url:
        if args.dumb:
            # Dumbest possible path: just crawl, write WACZ, done.
            results = crawl_list(args.url, output_dir=output_dir, skip_errors=not args.keep_on_error, update_json=False)
            ok  = [u for u, v in results.items() if isinstance(v, Path)]
            err = [u for u, v in results.items() if isinstance(v, Exception)]
            print(f"\n── Summary ────────────────────────────────────────")
            print(f"  Succeeded : {len(ok)}")
            print(f"  Failed    : {len(err)}")
            if err:
                print("\n  Failed URLs:")
                for u in err:
                    print(f"    {u}  →  {results[u]}")
                sys.exit(1)
        elif args.quick:
            # Single-release smart pipeline: check JSON, detect changes, crawl if needed.
            run_quick_pipeline(args.url, output_dir, logger, no_upload=args.no_upload)
        else:
            # Group URLs by artist so the smart pipeline (which processes one
            # artist at a time) can be called once per artist.
            artist_groups = _group_urls_by_artist(args.url, logger)
            if len(artist_groups) > 1:
                logger.info(
                    "Multiple artists detected (%d) — running smart pipeline for each.",
                    len(artist_groups),
                )
            if len(artist_groups) > 1:
                logger.info(
                    "Multiple artists detected (%d) — running smart pipeline for each.",
                    len(artist_groups),
                )
            if args.fetch_first:
                logger.info("--fetch-first: fetching metadata for all %d artist(s) before crawling.", len(artist_groups))
                for i, (band_id, (artist_root, artist_urls)) in enumerate(artist_groups.items(), 1):
                    if len(artist_groups) > 1:
                        print(f"\n── Fetching metadata {i}/{len(artist_groups)} (band_id={band_id}) ──────────────────────────")
                    run_smart_pipeline(
                        artist_urls, output_dir, logger,
                        skip_metadata=args.skip_metadata,
                        skip_update=args.skip_update,
                        fetch_only=True,
                    )
                logger.info("--fetch-first: all metadata fetched, starting crawl pass.")
                # Crawl pass: metadata already up to date, skip re-fetching
                if args.pipeline and len(artist_groups) > 1:
                    upload_threads: list[threading.Thread] = []
                    upload_errors: list[Exception] = []
                    for i, (band_id, (artist_root, artist_urls)) in enumerate(artist_groups.items(), 1):
                        print(f"\n── Artist {i}/{len(artist_groups)} (band_id={band_id}) ──────────────────────────")
                        artist_out = output_dir / f"band_{band_id}"
                        artist_out.mkdir(parents=True, exist_ok=True)
                        did_work = run_smart_pipeline(
                            artist_urls, artist_out, logger,
                            skip_metadata=True,
                            no_upload=True,
                        )
                        if did_work and not args.no_upload:
                            ul = logging.getLogger(f"upload[{band_id}]")
                            t = threading.Thread(
                                target=_upload_thread_target,
                                args=(artist_out, False, ul, _find_artist_folder(band_id), upload_errors),
                                name=f"upload-{band_id}",
                                daemon=True,
                            )
                            upload_threads.append(t)
                            t.start()
                    for t in upload_threads:
                        t.join()
                    if upload_errors:
                        logger.error("%d background upload(s) failed.", len(upload_errors))
                        sys.exit(1)
                else:
                    for i, (band_id, (artist_root, artist_urls)) in enumerate(artist_groups.items(), 1):
                        if len(artist_groups) > 1:
                            print(f"\n── Artist {i}/{len(artist_groups)} (band_id={band_id}) ──────────────────────────")
                        did_work = run_smart_pipeline(
                            artist_urls, output_dir, logger,
                            skip_metadata=True,
                            one_by_one=args.one_by_one,
                            no_upload=args.no_upload,
                        )
                        if did_work and not args.one_by_one:
                            run_upload(output_dir, args.no_upload, logger,
                                       artist_folder=_find_artist_folder(band_id))
            else:
                if args.pipeline and len(artist_groups) > 1:
                    upload_threads = []
                    upload_errors = []
                    for i, (band_id, (artist_root, artist_urls)) in enumerate(artist_groups.items(), 1):
                        print(f"\n── Artist {i}/{len(artist_groups)} (band_id={band_id}) ──────────────────────────")
                        artist_out = output_dir / f"band_{band_id}"
                        artist_out.mkdir(parents=True, exist_ok=True)
                        did_work = run_smart_pipeline(
                            artist_urls, artist_out, logger,
                            skip_metadata=args.skip_metadata,
                            skip_update=args.skip_update,
                            no_upload=True,
                        )
                        if did_work and not args.no_upload:
                            ul = logging.getLogger(f"upload[{band_id}]")
                            t = threading.Thread(
                                target=_upload_thread_target,
                                args=(artist_out, False, ul, _find_artist_folder(band_id), upload_errors),
                                name=f"upload-{band_id}",
                                daemon=True,
                            )
                            upload_threads.append(t)
                            t.start()
                    for t in upload_threads:
                        t.join()
                    if upload_errors:
                        logger.error("%d background upload(s) failed.", len(upload_errors))
                        sys.exit(1)
                else:
                    for i, (band_id, (artist_root, artist_urls)) in enumerate(artist_groups.items(), 1):
                        if len(artist_groups) > 1:
                            print(f"\n── Artist {i}/{len(artist_groups)} (band_id={band_id}) ──────────────────────────")
                        did_work = run_smart_pipeline(
                            artist_urls, output_dir, logger,
                            skip_metadata=args.skip_metadata,
                            skip_update=args.skip_update,
                            one_by_one=args.one_by_one,
                            no_upload=args.no_upload,
                        )
                        if did_work and not args.one_by_one:
                            run_upload(output_dir, args.no_upload, logger,
                                       artist_folder=_find_artist_folder(band_id))
        return

    # ── JSON mode (resume from already-fetched artist JSONs) ─────────────────
    if args.json:
        pending = _scan_artists_for_pending(ARTISTS_DIR)
        if not pending:
            print("No artists with pending work found in artists/.")
            sys.exit(0)

        selected = _prompt_json_selection(pending)
        print(f"\nResuming {len(selected)} artist(s)...\n")

        upload_threads: list[threading.Thread] = []
        upload_errors: list[Exception] = []
        use_pipeline = args.pipeline and len(selected) > 1

        for i, p in enumerate(selected, 1):
            if len(selected) > 1:
                print(f"\n── Artist {i}/{len(selected)}: {p['name']} ──────────────────────────")

            artist_folder = p["folder"]
            band_id = p["band_id"]

            # Per-artist output subdir when pipelining so upload threads only
            # see their own WACZs and never race with each other or with the
            # main crawl thread touching a different artist's files.
            if use_pipeline:
                subdir_name = f"band_{band_id}" if band_id else f"band_{p['name']}"
                artist_out = output_dir / subdir_name
                artist_out.mkdir(parents=True, exist_ok=True)
            else:
                artist_out = output_dir

            # Resolve artist root URL for metadata fetch.
            # Take the first release-level URL (not from trackinfo) that has no
            # ?label= param, then strip it to the artist root. Label-scoped URLs
            # belong to a different band_id and would resolve the wrong artist.
            # If only label-scoped URLs exist the user must supply --url/--slug.
            artist_root_url = None
            if not args.skip_metadata:
                import json as _json
                from urllib.parse import urlparse as _urlparse
                json_path = artist_folder / f"{artist_folder.name}.json"
                if json_path.exists():
                    try:
                        data = _json.loads(json_path.read_text(encoding="utf-8"))
                        for key, releases in data.items():
                            if key.startswith("_") or not isinstance(releases, list):
                                continue
                            for r in releases:
                                url = r.get("url", "")
                                if not url:
                                    continue
                                if "label=" in _urlparse(url).query:
                                    continue
                                artist_root_url = _to_artist_root(url)
                                break
                            if artist_root_url:
                                break
                    except Exception:
                        pass

                if not artist_root_url:
                    logger.warning(
                        "Could not find a non-label URL in %s to use for metadata fetch. "
                        "All release URLs appear to be label-scoped (?label=ID). "
                        "Use --url or --slug to supply the artist URL directly, "
                        "or pass --skip-metadata to skip the fetch and go straight to crawling.",
                        p["name"],
                    )
                    # Still proceed to crawl/upload whatever is already queued
                elif args.fetch_first:
                    logger.info("--fetch-first: updating metadata for %s", p["name"])
                    run_smart_pipeline(
                        [artist_root_url], output_dir, logger,
                        skip_metadata=args.skip_metadata,
                        skip_update=args.skip_update,
                        fetch_only=True,
                    )
                    # Refresh folder reference in case fetch moved things
                    artist_folder = p["folder"]
                else:
                    # Normal (non-fetch-first) metadata update inline
                    run_smart_pipeline(
                        [artist_root_url], output_dir, logger,
                        skip_metadata=args.skip_metadata,
                        skip_update=args.skip_update,
                        fetch_only=True,
                    )
                    artist_folder = p["folder"]

            did_work = _run_pipeline_for_folder(
                artist_folder, artist_out, logger,
                one_by_one=args.one_by_one,
                no_upload=True if use_pipeline else args.no_upload,
            )

            if use_pipeline:
                if did_work and not args.no_upload:
                    ul = logging.getLogger(f"upload[{band_id or p['name']}]")
                    t = threading.Thread(
                        target=_upload_thread_target,
                        args=(artist_out, False, ul, artist_folder, upload_errors),
                        name=f"upload-{band_id or p['name']}",
                        daemon=True,
                    )
                    upload_threads.append(t)
                    t.start()
            else:
                if did_work and not args.one_by_one:
                    run_upload(output_dir, args.no_upload, logger, artist_folder=artist_folder)

        if upload_threads:
            logger.info("Waiting for %d background upload(s) to finish...", len(upload_threads))
            for t in upload_threads:
                t.join()
        if upload_errors:
            logger.error("%d background upload(s) failed.", len(upload_errors))
            sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
