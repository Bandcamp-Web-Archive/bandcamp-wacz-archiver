# `config.py` — Usage Guide

## Overview

`config.py` is the single source of truth for all tuneable values and secrets in the project. Every constant is read from a `.env` file at the project root via `python-dotenv`, with a sensible hardcoded default for non-sensitive values. Nothing secret is ever hardcoded.

Other modules import constants directly from this file — you should never need to call any functions, as `config.py` exposes only module-level constants.

---

## Setup

Create a `.env` file in the project root (alongside the `bandcamp_wacz/` package directory). Any value you omit will fall back to its default.

```
# .env
IA_ACCESS_KEY=your_access_key
IA_SECRET_KEY=your_secret_key
EMAIL_ADDRESS=you@gmail.com
EMAIL_PASSWORD=your_app_password
```

---

## Configuration Reference

### Browsertrix / Crawl Container

These settings control how the Browsertrix crawler container is launched and how long it is allowed to run.

| Constant | Env Var | Default | Description |
|---|---|---|---|
| `BROWSERTRIX_IMAGE` | `BROWSERTRIX_IMAGE` | `ghcr.io/webrecorder/browsertrix-crawler:latest` | The container image used for crawling |
| `CONTAINER_RUNTIME` | `CONTAINER_RUNTIME` | `podman` | Container runtime to use. Set to `docker` if you are not using Podman |
| `WACZ_OUTPUT_DIR` | `WACZ_OUTPUT_DIR` | `wacz_output/` (project root) | Directory where `.wacz` files are written, relative to the project root |
| `CRAWL_WAIT_UNTIL` | `CRAWL_WAIT_UNTIL` | `networkidle2` | Page load event Browsertrix waits for before running behaviors. Options: `load`, `domcontentloaded`, `networkidle0`, `networkidle2` |
| `CRAWL_PAGE_LOAD_TIMEOUT` | `CRAWL_PAGE_LOAD_TIMEOUT` | `90` | Seconds Browsertrix will wait for a page to load before giving up |
| `CRAWL_BEHAVIOR_TIMEOUT` | `CRAWL_BEHAVIOR_TIMEOUT` | `1800` | Seconds allowed for page behaviors (e.g. the audio-fetch script). Set this high enough to cover your largest expected album |

**Example `.env` overrides:**
```
CONTAINER_RUNTIME=docker
CRAWL_BEHAVIOR_TIMEOUT=3600
WACZ_OUTPUT_DIR=output/wacz
```

---

### Bandcamp HTTP Requests

These settings control how `bandcamp.py` fetches Bandcamp pages, including rate-limit avoidance and retry behaviour.

| Constant | Env Var | Default | Description |
|---|---|---|---|
| `BC_REQUEST_DELAY` | `BC_REQUEST_DELAY` | `1000-3000` | Delay between Bandcamp requests in milliseconds. Use `"min-max"` for a random range or a fixed number like `"2000"` |
| `BC_MAX_RETRIES` | `BC_MAX_RETRIES` | `12` | How many times to retry a failed HTTP request before giving up |
| `BC_RETRY_DELAY` | `BC_RETRY_DELAY` | `5` | Base retry delay in seconds, multiplied by attempt number (e.g. attempt 3 waits 15 s) |
| `BC_REQUEST_TIMEOUT` | `BC_REQUEST_TIMEOUT` | `30` | HTTP request timeout in seconds |

The retry back-off is linear: `BC_RETRY_DELAY × attempt_number`. With the defaults, attempt 1 waits 5 s, attempt 2 waits 10 s, and so on up to attempt 12 (60 s).

---

### Crawl Retries

Separate retry settings for the Browsertrix crawl process itself, independent of the HTTP request retries above.

| Constant | Env Var | Default | Description |
|---|---|---|---|
| `CRAWL_MAX_RETRIES` | `CRAWL_MAX_RETRIES` | `3` | How many times to retry a failed Browsertrix crawl |
| `CRAWL_RETRY_DELAY` | `CRAWL_RETRY_DELAY` | `30` | Base retry delay in seconds (linear back-off: `delay × attempt`) |

---

### Filename Limits

Controls how output filenames are built to stay within archive.org's path length limit.

| Constant | Env Var | Default | Description |
|---|---|---|---|
| `FILENAME_MAX_BYTES` | `FILENAME_MAX_BYTES` | `210` | Maximum filename length in UTF-8 bytes. archive.org's hard limit is 230 bytes; the default of 210 provides a safety margin |
| `FILENAME_TRUNCATION` | `FILENAME_TRUNCATION` | `middle` | Strategy used when a filename exceeds `FILENAME_MAX_BYTES`. See below |

**Truncation styles for `FILENAME_TRUNCATION`:**

| Value | Behaviour |
|---|---|
| `middle` | Removes characters from the centre of the title, keeping both the start and end. Good readability. **(default)** |
| `end` | Cuts the title at the byte limit from the right |
| `hash` | Replaces the entire title with a 16-character SHA-1 hex digest. Unambiguous but not human-readable |

---

### archive.org Upload

Credentials and settings for uploading `.wacz` files to archive.org via the Internet Archive API.

| Constant | Env Var | Default | Description |
|---|---|---|---|
| `IA_ACCESS_KEY` | `IA_ACCESS_KEY` | `None` | Your archive.org S3-like access key (**required for uploads**) |
| `IA_SECRET_KEY` | `IA_SECRET_KEY` | `None` | Your archive.org S3-like secret key (**required for uploads**) |
| `IA_COLLECTION` | `IA_COLLECTION` | `opensource_media` | The archive.org collection items are uploaded into |
| `IA_MAX_RETRIES` | `IA_MAX_RETRIES` | `5` | How many times to retry a failed upload |
| `IA_RETRY_DELAY` | `IA_RETRY_DELAY` | `10` | Base retry delay in seconds (linear back-off: `delay × attempt`) |

You can get your archive.org API keys from [archive.org/account/s3.php](https://archive.org/account/s3.php).

---

### Email Watcher

Settings for the IMAP email watcher (`email_watcher.py`), which monitors an inbox for Bandcamp purchase confirmation emails to trigger crawls automatically.

| Constant | Env Var | Default | Description |
|---|---|---|---|
| `EMAIL_ADDRESS` | `EMAIL_ADDRESS` | `None` | Email address to monitor |
| `EMAIL_PASSWORD` | `EMAIL_PASSWORD` | `None` | Password or app-specific password for the account |
| `IMAP_SERVER` | `IMAP_SERVER` | `imap.gmail.com` | IMAP server hostname |
| `IMAP_PORT` | `IMAP_PORT` | `993` | IMAP port (993 is standard for IMAP over SSL) |

For Gmail, `EMAIL_PASSWORD` should be an [App Password](https://support.google.com/accounts/answer/185833), not your account password, as Google blocks direct password access for accounts with 2FA enabled.

---

### Filesystem Layout

| Constant | Value | Description |
|---|---|---|
| `ARTISTS_DIR` | `{project_root}/artists/` | Root directory for per-artist data (not configurable via `.env`) |
| `USER_AGENT` | `bandcamp-wacz-archiver/1.0 (…)` | User-Agent header sent with all plain HTTP requests. Not the browser UA used during crawls |

---

## Importing

```python
from bandcamp_wacz.config import (
    WACZ_OUTPUT_DIR,
    IA_ACCESS_KEY,
    IA_SECRET_KEY,
    FILENAME_MAX_BYTES,
    FILENAME_TRUNCATION,
    # etc.
)
```

Constants are plain Python values (strings, ints, `Path` objects) and can be used directly. There are no functions to call.

---

## Full `.env` Template

```dotenv
# ── Browsertrix ───────────────────────────────────────────────
BROWSERTRIX_IMAGE=ghcr.io/webrecorder/browsertrix-crawler:latest
CONTAINER_RUNTIME=podman
WACZ_OUTPUT_DIR=wacz_output
CRAWL_WAIT_UNTIL=networkidle2
CRAWL_PAGE_LOAD_TIMEOUT=90
CRAWL_BEHAVIOR_TIMEOUT=1800

# ── Crawl retries ─────────────────────────────────────────────
CRAWL_MAX_RETRIES=3
CRAWL_RETRY_DELAY=30

# ── Bandcamp HTTP ─────────────────────────────────────────────
BC_REQUEST_DELAY=1000-3000
BC_MAX_RETRIES=12
BC_RETRY_DELAY=5
BC_REQUEST_TIMEOUT=30

# ── Filename limits ───────────────────────────────────────────
FILENAME_MAX_BYTES=210
FILENAME_TRUNCATION=middle

# ── archive.org (required for uploads) ───────────────────────
IA_ACCESS_KEY=
IA_SECRET_KEY=
IA_COLLECTION=opensource_media
IA_MAX_RETRIES=5
IA_RETRY_DELAY=10

# ── Email watcher (required for auto-crawl from email) ────────
EMAIL_ADDRESS=
EMAIL_PASSWORD=
IMAP_SERVER=imap.gmail.com
IMAP_PORT=993
```
