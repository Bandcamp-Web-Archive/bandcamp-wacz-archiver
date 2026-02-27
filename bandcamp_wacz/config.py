"""
config.py - project-wide constants loaded from .env.

All secrets and tuneable values are read from the .env file in the project root.
Nothing sensitive is ever hardcoded here.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

# ── Browsertrix container ─────────────────────────────────────────────────────

BROWSERTRIX_IMAGE: str = os.getenv(
    "BROWSERTRIX_IMAGE",
    "ghcr.io/webrecorder/browsertrix-crawler:latest",
)

# Container runtime to use. Override with CONTAINER_RUNTIME=docker if needed.
CONTAINER_RUNTIME: str = os.getenv("CONTAINER_RUNTIME", "podman")

WACZ_OUTPUT_DIR: Path = _PROJECT_ROOT / os.getenv("WACZ_OUTPUT_DIR", "wacz_output")

# How long Browsertrix waits for the page DOM to settle before running behaviors.
# Valid values: load, domcontentloaded, networkidle0, networkidle2
CRAWL_WAIT_UNTIL: str = os.getenv("CRAWL_WAIT_UNTIL", "networkidle2")

# Seconds Browsertrix will wait for a page to load before giving up.
CRAWL_PAGE_LOAD_TIMEOUT: int = int(os.getenv("CRAWL_PAGE_LOAD_TIMEOUT", "90"))

# Seconds Browsertrix allows for page behaviors (e.g. our audio-fetch script).
# Must be long enough for the largest album you expect to archive.
CRAWL_BEHAVIOR_TIMEOUT: int = int(os.getenv("CRAWL_BEHAVIOR_TIMEOUT", "1800"))

# ── Bandcamp HTTP ─────────────────────────────────────────────────────────────

# Random delay between Bandcamp requests, in milliseconds.
# Format: "min-max" for a range (e.g. "1000-3000") or a fixed value (e.g. "2000").
BC_REQUEST_DELAY: str = os.getenv("BC_REQUEST_DELAY", "1000-3000")

# Number of times to retry a failed HTTP request before giving up.
BC_MAX_RETRIES: int = int(os.getenv("BC_MAX_RETRIES", "12"))

# Initial retry delay in seconds; multiplied by attempt number (exponential back-off).
BC_RETRY_DELAY: int = int(os.getenv("BC_RETRY_DELAY", "5"))

# HTTP request timeout in seconds.
BC_REQUEST_TIMEOUT: int = int(os.getenv("BC_REQUEST_TIMEOUT", "30"))

# ── Crawl retries ─────────────────────────────────────────────────────────────

# Number of times to retry a failed Browsertrix crawl before giving up.
CRAWL_MAX_RETRIES: int = int(os.getenv("CRAWL_MAX_RETRIES", "3"))

# Seconds to wait before retrying a failed crawl (linear back-off: delay * attempt).
CRAWL_RETRY_DELAY: int = int(os.getenv("CRAWL_RETRY_DELAY", "30"))

# ── Filename limits ───────────────────────────────────────────────────────────

# Maximum filename length in bytes (archive.org limit is 230 bytes).
# Applies to the full filename including suffix, e.g. "Title [item_id].wacz".
FILENAME_MAX_BYTES: int = int(os.getenv("FILENAME_MAX_BYTES", "210"))

# How to truncate filenames that exceed FILENAME_MAX_BYTES.
# Options:
#   end    — cut the title at the byte limit, keeping the end suffix intact (default)
#   middle — remove characters from the middle, keeping both start and end of title
#   hash   — replace the entire title with a short SHA-1 hash (unambiguous, not readable)
FILENAME_TRUNCATION: str = os.getenv("FILENAME_TRUNCATION", "middle").lower()

# ── archive.org ───────────────────────────────────────────────────────────────

IA_ACCESS_KEY: str | None = os.getenv("IA_ACCESS_KEY")
IA_SECRET_KEY: str | None = os.getenv("IA_SECRET_KEY")

# archive.org collection to upload to.
IA_COLLECTION: str = os.getenv("IA_COLLECTION", "opensource_media")

# Number of times to retry a failed upload before giving up.
IA_MAX_RETRIES: int = int(os.getenv("IA_MAX_RETRIES", "5"))

# Seconds to wait before retrying a failed upload (linear back-off: delay * attempt).
IA_RETRY_DELAY: int = int(os.getenv("IA_RETRY_DELAY", "10"))

# ── Email watcher ─────────────────────────────────────────────────────────────

EMAIL_ADDRESS: str | None = os.getenv("EMAIL_ADDRESS")
EMAIL_PASSWORD: str | None = os.getenv("EMAIL_PASSWORD")
IMAP_SERVER: str = os.getenv("IMAP_SERVER", "imap.gmail.com")
IMAP_PORT: int = int(os.getenv("IMAP_PORT", "993"))

# ── Filesystem layout ─────────────────────────────────────────────────────────

ARTISTS_DIR: Path = _PROJECT_ROOT / "artists"

# User-agent for all plain HTTP requests (not the crawl browser)
USER_AGENT: str = (
    "bandcamp-wacz-archiver/1.0 "
    "(https://github.com/Bandcamp-Web-Archive/bandcamp-wacz-archiver)"
)
