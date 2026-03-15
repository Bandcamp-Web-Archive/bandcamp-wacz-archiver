"""
crawl.py - drive Browsertrix via Podman to produce a WACZ file.

Pipeline for each album URL:
  1. Fetch page metadata (band_id, artist, cover URL, item_id, title)
  2. Build a Browsertrix YAML config seeding the album page + high-res cover
  3. Run Browsertrix in a Podman container
  4. Rename output to  <Title> [<item_id>].wacz  and move to wacz_output/
  5. Delete the collections/ working directory
  6. Write release JSON and mark album as archived in the artist JSON (Step 2)
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from urllib.parse import parse_qs, urlparse
from .bandcamp import parse_page, subdomain_from_url, create_safe_filename, truncate_filename
from .config import (
    BROWSERTRIX_IMAGE, WACZ_OUTPUT_DIR,
    CRAWL_WAIT_UNTIL, CRAWL_PAGE_LOAD_TIMEOUT, CRAWL_BEHAVIOR_TIMEOUT,
    CRAWL_MAX_RETRIES, CRAWL_RETRY_DELAY,
    CRAWL_RATE_LIMIT_MAX_RETRIES, CRAWL_RATE_LIMIT_RETRY_DELAY,
    CRAWL_TRACK_DELAY_MS,
    CONTAINER_RUNTIME,
    FILENAME_MAX_BYTES, FILENAME_TRUNCATION,
)
from .metadata import process_archived_wacz

logger = logging.getLogger(__name__)

# Current inter-track delay passed to the behavior script via --behaviorOpts.
# Starts at CRAWL_TRACK_DELAY_MS, doubles after each rate-limit, resets on success.
_track_delay_ms: int = CRAWL_TRACK_DELAY_MS


class RateLimitError(RuntimeError):
    """Raised when Browsertrix reports a rate-limit response (HTTP 429) during a crawl."""


def _is_rate_limited(line: str) -> bool:
    """Return True if *line* is a JSON log entry with ``details.statusCode == 429``.

    Browsertrix emits JSON-Lines log output. We only detect rate limits via the
    structured ``details.statusCode`` field — never by scanning raw text — so
    that the number 429 or rate-limit-adjacent words appearing in a title, URL,
    or any other field cannot produce a false positive. Non-JSON lines (startup
    banners, plain-text warnings) are ignored entirely.
    """
    line = line.strip()
    if not line or not line.startswith("{"):
        return False

    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return False

    details = obj.get("details")
    return isinstance(details, dict) and details.get("status") == 429


def _track_cover_urls_from_json(band_id: int, item_id: int, album_art_id: Optional[int]) -> list[str]:
    """
    Look up the artist JSON for this release and return full-resolution seed
    URLs for any track covers whose art_id differs from the album cover.

    fetch_metadata.py visits every track page and stores art_id per track, so
    this is the only place those IDs are available without re-fetching pages.
    Returns an empty list if the artist JSON can't be found or has no trackinfo.
    """
    from .config import ARTISTS_DIR
    import json as _json

    if not ARTISTS_DIR.exists():
        return []

    # Find the artist folder by matching [band_id] suffix
    json_path: Optional[Path] = None
    for folder in ARTISTS_DIR.iterdir():
        if folder.is_dir() and folder.name.endswith(f"[{band_id}]"):
            candidate = folder / f"{folder.name}.json"
            if candidate.exists():
                json_path = candidate
                break

    if not json_path:
        return []

    try:
        data = _json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not read artist JSON for track covers: %s", exc)
        return []

    # Find the matching album by item_id
    album: Optional[dict] = None
    for key, releases in data.items():
        if key.startswith("_") or not isinstance(releases, list):
            continue
        for r in releases:
            if r.get("item_id") == item_id:
                album = r
                break
        if album:
            break

    if not album:
        return []

    seen: set = {album_art_id} if album_art_id else set()
    urls: list[str] = []
    for track in album.get("trackinfo", []):
        art_id = track.get("art_id")
        if art_id and art_id not in seen:
            seen.add(art_id)
            urls.append(f"https://f4.bcbits.com/img/a{art_id}_0")

    return urls


def _build_crawl_config(album_url: str, extra_urls: list[str]) -> str:
    """Return a Browsertrix YAML config string for the given album."""
    seeds = [{"url": album_url, "scopeType": "page"}]
    for url in extra_urls:
        seeds.append({"url": url, "scopeType": "page"})

    seed_block = "".join(
        f"  - url: \"{s['url']}\"\n    scopeType: {s['scopeType']}\n"
        for s in seeds
    )

    return (
        f"seeds:\n{seed_block}\n"
        f"waitUntil: {CRAWL_WAIT_UNTIL}\n\n"
        f"pageLoadTimeout: {CRAWL_PAGE_LOAD_TIMEOUT}\n"
        f"behaviorTimeout: {CRAWL_BEHAVIOR_TIMEOUT}\n\n"
        f"limit: {len(seeds)}\n"
        f"depth: 0\n\n"
        f"generateWACZ: true\n"
        f"combineWACZ: true\n"
    )


def _run_container(config_path: Path, output_dir: Path, collection_name: str, track_delay_ms: int = 100) -> Path:
    """Run the Browsertrix container and return the path to the produced WACZ.

    Streams container output to stdout in real time while also capturing it so
    that rate-limit signals (HTTP 429 / "Too Many Requests") can be detected.
    Raises :exc:`RateLimitError` when a rate limit is detected, and plain
    :exc:`RuntimeError` for any other non-zero exit.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    behaviors_dir = Path(__file__).resolve().parent.parent / "behaviors"

    cmd = [
        CONTAINER_RUNTIME, "run", "--rm",
        "-v", f"{config_path.resolve()}:/crawls/crawl-config.yaml:ro",
        "-v", f"{output_dir.resolve()}:/crawls",
        "-v", f"{behaviors_dir}:/behaviors:ro",
        BROWSERTRIX_IMAGE,
        "crawl",
        "--config", "/crawls/crawl-config.yaml",
        "--collection", collection_name,
        "--customBehaviors", "/behaviors/bandcamp.js",
        "--behaviorOpts", f'{{"trackDelayMs": {track_delay_ms}}}',
    ]

    logger.info("Starting container crawl: %s", " ".join(cmd))

    # Stream output to the terminal line-by-line while capturing it for
    # rate-limit detection. stderr is merged into stdout so a single pass
    # is enough and the interleaved output matches what the user sees.
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    captured: list[str] = []
    rate_limited = False
    for line in proc.stdout:
        print(line, end="", flush=True)
        captured.append(line)
        # Kill the container the moment a 429 is detected so we stop hammering
        # Bandcamp immediately and can start the back-off wait right away.
        if not rate_limited and _is_rate_limited(line):
            rate_limited = True
            logger.warning(
                "Rate limit detected in crawl output — terminating container to retry after back-off."
            )
            proc.kill()

    # Drain any remaining output after an early kill to avoid broken-pipe noise.
    try:
        for line in proc.stdout:
            captured.append(line)
    except Exception:
        pass
    proc.wait()

    if rate_limited:
        raise RateLimitError("Bandcamp rate-limited the crawl; retrying after back-off.")
    if proc.returncode != 0:
        raise RuntimeError(f"Browsertrix exited with code {proc.returncode}")

    # Browsertrix nests the WACZ inside collections/<n>/
    wacz = output_dir.resolve() / "collections" / collection_name / f"{collection_name}.wacz"
    if not wacz.exists():
        alt = output_dir.resolve() / "collections" / f"{collection_name}.wacz"
        if alt.exists():
            wacz = alt
        else:
            raise FileNotFoundError(f"Expected WACZ not found at {wacz}")

    return wacz


def crawl_album(album_url: str, output_dir: Optional[Path] = None, update_json: bool = True) -> Path:
    """
    Run the full crawl pipeline for one Bandcamp album or track URL.

    update_json: if False, skip writing the release sidecar JSON and marking
                 the release as archived in the artist JSON (--dumb mode).

    Returns the path to the finished .wacz file.
    """
    out = Path(output_dir or WACZ_OUTPUT_DIR)

    logger.info("Fetching page metadata: %s", album_url)
    try:
        info = parse_page(album_url)
    except Exception as exc:
        logger.error("Could not parse Bandcamp page %s: %s", album_url, exc)
        raise

    band_id   = info.get("band_id")
    artist    = info.get("artist", "Unknown Artist")
    item_id   = info.get("item_id")
    cover_url = info.get("cover_url_0")
    title     = info.get("title", "untitled")
    safe_title = create_safe_filename(title)

    # Collect all extra image URLs to seed (deduplicating, preserving order).
    # Order: album cover → artist photo → banner → unique track covers.
    _seen_urls: set[str] = {album_url}
    extra_urls: list[str] = []

    def _add_url(url: Optional[str], label: str) -> None:
        if url and url not in _seen_urls:
            _seen_urls.add(url)
            extra_urls.append(url)
            logger.debug("Extra seed (%s): %s", label, url)
        elif not url:
            logger.debug("No %s URL found for %s — skipping seed.", label, album_url)

    _add_url(cover_url,                      "album cover")
    _add_url(info.get("artist_image_url"),   "artist image")
    _add_url(info.get("banner_url"),         "banner")

    for tp_url in info.get("track_page_urls", []):
        _add_url(tp_url, "track page")

    # Track covers: read from the artist JSON (populated by fetch_metadata.py
    # which visits each track page). Falls back to nothing if the JSON doesn't
    # exist yet (e.g. --dumb mode or --quick without a prior fetch).
    if band_id and item_id:
        album_art_id = info.get("album_art_id")
        for tc_url in _track_cover_urls_from_json(band_id, item_id, album_art_id):
            _add_url(tc_url, "track cover")

    if item_id:
        display_name    = f"{safe_title} [{item_id}]"
        # Browsertrix only allows [a-zA-Z0-9_-] in collection names.
        # Strip non-safe chars, then strip any leading/trailing hyphens or
        # underscores — a name like '-KentaroHirugami_123' (produced when the
        # title starts with non-ASCII chars such as Japanese) causes Browsertrix
        # to reject --collection and fall back to crawl-<timestamp> naming,
        # making the expected WACZ path wrong.
        collection_name = re.sub(r"[^a-zA-Z0-9_-]", "", f"{safe_title}_{item_id}")[:80]
        collection_name = collection_name.strip("-_") or f"release_{item_id}"
    else:
        subdomain       = subdomain_from_url(album_url)
        collection_name = re.sub(r"[^a-zA-Z0-9_-]", "", f"{subdomain}_{safe_title}")[:80]
        collection_name = collection_name.strip("-_") or "unknown"
        display_name    = collection_name

    # Ensure the final filename fits within the archive.org byte limit
    suffix = ".wacz"
    full_filename = truncate_filename(display_name, suffix, FILENAME_MAX_BYTES, FILENAME_TRUNCATION)
    if full_filename != display_name + suffix:
        logger.warning(
            "Filename truncated (%s style): '%s' → '%s'",
            FILENAME_TRUNCATION, display_name + suffix, full_filename,
        )
    # Strip the suffix back off to get the (possibly truncated) display_name
    display_name = full_filename[: -len(suffix)]

    logger.info(
        "Album: %s - %s | item_id=%s | band_id=%s | cover=%s | +%d extra image(s)",
        artist, title, item_id, band_id, cover_url, len(extra_urls) - (1 if cover_url else 0),
    )

    if not cover_url:
        logger.warning("No cover URL found for %s - crawling without cover seed.", album_url)

    config_yaml = _build_crawl_config(album_url, extra_urls)
    logger.debug("Crawl config:\n%s", config_yaml)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", prefix="bc_crawl_", delete=False) as tmp:
        tmp.write(config_yaml)
        config_path = Path(tmp.name)

    global _track_delay_ms
    try:
        rate_limit_count = 0
        regular_count = 0
        while True:
            logger.debug("Inter-track delay for this attempt: %dms", _track_delay_ms)
            try:
                wacz_path = _run_container(config_path, out, collection_name, _track_delay_ms)
                # Success — reset the delay for the next album.
                _track_delay_ms = CRAWL_TRACK_DELAY_MS
                break
            except KeyboardInterrupt:
                logger.warning("Crawl interrupted by user.")
                raise KeyboardInterrupt() from None
            except RateLimitError as exc:
                rate_limit_count += 1
                # Double the inter-track delay for the next attempt so the
                # behavior script paces itself more conservatively.
                _track_delay_ms = min(_track_delay_ms * 2, 8000)
                logger.warning(
                    "Rate limit hit — inter-track delay increased to %dms for next attempt.",
                    _track_delay_ms,
                )
                if rate_limit_count <= CRAWL_RATE_LIMIT_MAX_RETRIES:
                    # Remove the partial collection written by the killed container
                    # so the retry starts with a clean slate and doesn't bundle
                    # incomplete WARCs into the final WACZ.
                    partial_dir = out / "collections" / collection_name
                    if partial_dir.exists():
                        shutil.rmtree(partial_dir, ignore_errors=True)
                        logger.debug("Removed partial collection directory: %s", partial_dir)
                    wait = CRAWL_RATE_LIMIT_RETRY_DELAY * rate_limit_count
                    logger.warning(
                        "Crawl rate-limited (attempt %d/%d) for %s — retrying in %ds...",
                        rate_limit_count, CRAWL_RATE_LIMIT_MAX_RETRIES + 1, album_url, wait,
                    )
                    try:
                        time.sleep(wait)
                    except KeyboardInterrupt:
                        logger.warning("Crawl retry wait interrupted by user.")
                        raise KeyboardInterrupt() from None
                else:
                    logger.error(
                        "Rate limit persisted after %d attempt(s) for %s — "
                        "skipping WACZ creation, upload, and JSON update.",
                        rate_limit_count, album_url,
                    )
                    raise
            except Exception as exc:
                regular_count += 1
                if regular_count <= CRAWL_MAX_RETRIES:
                    wait = CRAWL_RETRY_DELAY * regular_count
                    logger.warning(
                        "Crawl attempt %d/%d failed for %s: %s — retrying in %ds...",
                        regular_count, CRAWL_MAX_RETRIES + 1, album_url, exc, wait,
                    )
                    try:
                        time.sleep(wait)
                    except KeyboardInterrupt:
                        logger.warning("Crawl retry wait interrupted by user.")
                        raise KeyboardInterrupt() from None
                else:
                    logger.error(
                        "Crawl failed after %d attempt(s) for %s: %s",
                        regular_count + rate_limit_count, album_url, exc,
                    )
                    raise
    finally:
        config_path.unlink(missing_ok=True)

    # Move WACZ from the nested collections/ folder to the output root
    final_path = out / f"{display_name}.wacz"
    wacz_path.rename(final_path)

    # Remove the now-empty collections/ working directory
    collection_dir = out / "collections" / collection_name
    if collection_dir.exists():
        shutil.rmtree(collection_dir, ignore_errors=True)
        logger.debug("Removed working directory: %s", collection_dir)

    logger.info("WACZ saved: %s", final_path)

    # Step 2: write release JSON + mark archived in artist JSON (skipped in --dumb mode)
    if update_json:
        # If the URL carries a ?label=<id> param, the release belongs to a label's
        # JSON (band_id = label id), not the artist's own page. Override band_id so
        # process_archived_wacz associates the WACZ with the correct artist JSON.
        label_param = parse_qs(urlparse(album_url).query).get("label")
        if label_param:
            effective_band_id = int(label_param[0])
            logger.info(
                "Label URL detected — using label band_id=%s for metadata association "
                "(page band_id=%s).",
                effective_band_id, band_id,
            )
        else:
            effective_band_id = band_id

        if effective_band_id and item_id:
            process_archived_wacz(final_path, band_id=effective_band_id, item_id=item_id)
        else:
            logger.warning(
                "Skipping metadata step - band_id=%s item_id=%s (one or both missing).",
                effective_band_id, item_id,
            )
    else:
        logger.debug("Skipping metadata step (update_json=False).")

    return final_path


def crawl_list(
    urls: list[str],
    output_dir: Optional[Path] = None,
    skip_errors: bool = True,
    update_json: bool = True,
) -> dict[str, Path | Exception]:
    """
    Crawl every URL in *urls*, returning a dict of {url: Path | Exception}.
    """
    results: dict[str, Path | Exception] = {}
    for i, url in enumerate(urls, 1):
        logger.info("[%d/%d] Crawling: %s", i, len(urls), url)
        try:
            results[url] = crawl_album(url, output_dir=output_dir, update_json=update_json)
        except Exception as exc:
            logger.error("Failed: %s - %s", url, exc)
            if skip_errors:
                results[url] = exc
            else:
                raise
    return results
