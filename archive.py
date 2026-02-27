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
import logging
import subprocess
import sys
from pathlib import Path

from bandcamp_wacz.crawl import crawl_album, crawl_list
from bandcamp_wacz.config import WACZ_OUTPUT_DIR, ARTISTS_DIR

DEFAULT_LIST_FILE = "bandcamp-dump.lst"


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
        print("  Image not found locally. Pulling now (this may take a while)…")
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
    """
    import json as _json
    json_path = artist_folder / f"{artist_folder.name}.json"
    if not json_path.exists():
        return []
    try:
        data = _json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:
        return []

    # Build a set of item_ids present in any wacz in the output dir
    # (reads embedded datapackage.json first, falls back to filename parsing)
    present_item_ids: set[int] = set()
    for wacz_path in output_dir.glob("*.wacz"):
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


# ── Smart pipeline ────────────────────────────────────────────────────────────

def run_smart_pipeline(
    urls: list[str],
    output_dir: Path | None,
    logger: logging.Logger,
    skip_metadata: bool = False,
    one_by_one: bool = False,
    no_upload: bool = False,
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

    # Step 4: detect releases marked archived but with no WACZ file on disk
    out = output_dir or WACZ_OUTPUT_DIR
    missing = _releases_missing_wacz(artist_folder, out)
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
        if not no_upload and _has_unuploaded(artist_folder):
            print("Nothing to crawl - all releases already archived. Proceeding to upload…")
            return True
        print("Nothing to archive - all releases are already up to date.")
        return False

    print(f"\nArchiving {len(to_archive)} release(s)…\n")

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
                    run_upload(output_dir, no_upload=False, logger=logger)
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
            run_upload(output_dir, no_upload=False, logger=logger)


def run_upload(output_dir, no_upload: bool, logger) -> None:
    """Upload all finished WACZs in the output directory."""
    if no_upload:
        logger.info("Skipping upload (--no-upload).")
        return
    import importlib.util
    _upload_path = Path(__file__).resolve().parent / "upload.py"
    _spec = importlib.util.spec_from_file_location("upload", _upload_path)
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    from bandcamp_wacz.config import WACZ_OUTPUT_DIR as _DEFAULT_OUT
    search_dir = output_dir or _DEFAULT_OUT
    wacz_files = _mod.collect_wacz_files([str(search_dir)])
    if not wacz_files:
        logger.info("No WACZ files to upload.")
        return
    logger.info("Uploading %d WACZ file(s)…", len(wacz_files))
    for wacz_path in wacz_files:
        try:
            _mod.upload_release(wacz_path, dry_run=False)
        except KeyboardInterrupt:
            raise KeyboardInterrupt() from None


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
    parser.add_argument("--skip-metadata", action="store_true",
        help="Skip fetch/update metadata step and archive only what is already queued (archived=False).")
    parser.add_argument("--one-by-one", action="store_true",
        help="Archive and upload one release at a time. Saves disc space for large discographies.")
    parser.add_argument("--no-upload", action="store_true",
        help="Skip uploading to archive.org after archiving.")
    parser.add_argument("--filename-truncation", metavar="STYLE",
        choices=["end", "middle", "hash"], default=None,
        help="How to truncate filenames that exceed the archive.org 230-byte limit: "
             "'end' (cut the title short), 'middle' (keep start and end with … in between), "
             "'hash' (replace title with a short SHA-1 digest). "
             "Overrides FILENAME_TRUNCATION in .env (default: end).")
    parser.add_argument("--debug", "-d", action="store_true",
        help="Enable verbose debug logging.")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logger = logging.getLogger("archive")

    # Override config with CLI flag if provided
    if args.filename_truncation:
        import bandcamp_wacz.config as _cfg
        _cfg.FILENAME_TRUNCATION = args.filename_truncation
        logger.debug("Filename truncation style overridden to: %s", args.filename_truncation)

    if args.check_podman:
        check_podman()
        sys.exit(0)

    output_dir = Path(args.output) if args.output else None

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
            did_work = run_smart_pipeline(
                args.url, output_dir, logger,
                skip_metadata=args.skip_metadata,
                one_by_one=args.one_by_one,
                no_upload=args.no_upload,
            )
            # In one_by_one mode uploads happen inside the pipeline per-release
            if did_work and not args.one_by_one:
                run_upload(output_dir, args.no_upload, logger)
        return

    parser.print_help()
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
