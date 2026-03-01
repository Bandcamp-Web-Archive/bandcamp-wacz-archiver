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
    CRAWL_MAX_RETRIES, CRAWL_RETRY_DELAY, CONTAINER_RUNTIME,
    FILENAME_MAX_BYTES, FILENAME_TRUNCATION,
)
from .metadata import process_archived_wacz

logger = logging.getLogger(__name__)


def _build_crawl_config(album_url: str, cover_url_0: Optional[str]) -> str:
    """Return a Browsertrix YAML config string for the given album."""
    seeds = [{"url": album_url, "scopeType": "page"}]
    if cover_url_0:
        seeds.append({"url": cover_url_0, "scopeType": "page"})

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


def _run_container(config_path: Path, output_dir: Path, collection_name: str) -> Path:
    """Run the Browsertrix container and return the path to the produced WACZ."""
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
    ]

    logger.info("Starting container crawl: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=False, text=True)

    if result.returncode != 0:
        raise RuntimeError(f"Browsertrix exited with code {result.returncode}")

    # Browsertrix nests the WACZ inside collections/<name>/
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
        "Album: %s - %s | item_id=%s | band_id=%s | cover=%s",
        artist, title, item_id, band_id, cover_url,
    )

    if not cover_url:
        logger.warning("No cover URL found for %s - crawling without cover seed.", album_url)

    config_yaml = _build_crawl_config(album_url, cover_url)
    logger.debug("Crawl config:\n%s", config_yaml)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", prefix="bc_crawl_", delete=False) as tmp:
        tmp.write(config_yaml)
        config_path = Path(tmp.name)

    try:
        last_exc: Exception | None = None
        for attempt in range(1, CRAWL_MAX_RETRIES + 2):  # +2: first try + retries
            try:
                wacz_path = _run_container(config_path, out, collection_name)
                break
            except KeyboardInterrupt:
                logger.warning("Crawl interrupted by user.")
                raise KeyboardInterrupt() from None
            except Exception as exc:
                last_exc = exc
                if attempt <= CRAWL_MAX_RETRIES:
                    wait = CRAWL_RETRY_DELAY * attempt
                    logger.warning(
                        "Crawl attempt %d/%d failed for %s: %s  — retrying in %ds...",
                        attempt, CRAWL_MAX_RETRIES + 1, album_url, exc, wait,
                    )
                    try:
                        time.sleep(wait)
                    except KeyboardInterrupt:
                        logger.warning("Crawl retry wait interrupted by user.")
                        raise KeyboardInterrupt() from None
                else:
                    logger.error(
                        "Crawl failed after %d attempt(s) for %s: %s",
                        attempt, album_url, exc,
                    )
                    raise last_exc
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
