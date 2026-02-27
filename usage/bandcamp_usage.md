# `bandcamp.py` — Usage Guide

## Overview

`bandcamp.py` is the **minimal Bandcamp page fetcher** for the crawl pipeline. Its single responsibility is to fetch a Bandcamp page (album, track, or artist) and extract just enough information for the rest of the pipeline to proceed — specifically, the data that `archive.py` needs before handing off to Browsertrix.

Full metadata parsing (track listings, lyrics, credits, etc.) is handled elsewhere in `metadata.py` / `fetch_metadata.py`. This module deliberately stays lean.

---

## Dependencies

| Package | Purpose |
|---|---|
| `requests` | HTTP client |
| `bs4` (BeautifulSoup) | HTML parsing |
| `demjson3` | Lenient JSON parsing fallback |
| `urllib3` | Custom SSL context |

Internal config values consumed from `config.py`:

| Name | Purpose |
|---|---|
| `BC_REQUEST_DELAY` | Delay between requests (ms or `"lo-hi"` range string) |
| `BC_MAX_RETRIES` | Maximum retry attempts for failed requests |
| `BC_RETRY_DELAY` | Base delay (seconds) between retries |
| `BC_REQUEST_TIMEOUT` | HTTP timeout in seconds |
| `USER_AGENT` | User-Agent header string |

---

## Public API

### `fetch_url(url, max_retries=None, retry_delay=None) → requests.Response`

A robust HTTP GET wrapper with rate-limit awareness and exponential back-off.

**Behaviour:**
- Applies a randomised delay before every request (controlled by `BC_REQUEST_DELAY`).
- On HTTP **429 (Too Many Requests)**, waits `retry_delay * (attempt + 1)` seconds and retries.
- On any `requests.RequestException`, retries up to `max_retries` times with the same back-off.
- Uses a custom SSL session (see [SSL Adapter](#ssl-adapter) below) to avoid fingerprint-based blocking.

**Parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `url` | `str` | — | Fully-qualified URL to fetch |
| `max_retries` | `int \| None` | `BC_MAX_RETRIES` | Override max retry count |
| `retry_delay` | `int \| None` | `BC_RETRY_DELAY` | Override base retry delay (seconds) |

**Raises:** `requests.RequestException` if all attempts are exhausted.

**Example:**
```python
from bandcamp_wacz.bandcamp import fetch_url

resp = fetch_url("https://someartist.bandcamp.com/album/some-album")
print(resp.status_code)  # 200
print(resp.text[:500])
```

---

### `parse_page(url) → dict`

The primary entry point. Fetches a Bandcamp URL, parses its HTML, and returns a minimal metadata dictionary.

**Returns a dict with these keys:**

| Key | Type | Description |
|---|---|---|
| `band_id` | `int \| None` | Bandcamp's internal band/artist identifier |
| `artist` | `str` | Artist display name (falls back to `"Unknown Artist"`) |
| `cover_url_0` | `str \| None` | Full-resolution cover image URL (`_0` variant, no file extension) |
| `item_id` | `int \| None` | Bandcamp's internal album/track identifier |
| `title` | `str` | Album or track title (falls back to `"Untitled"`) |
| `is_preorder` | `bool` | Whether the item is currently a pre-order |

**`band_id` extraction strategy** (tried in this priority order):
1. `tralbum["current"]["band_id"]` — most reliable on album/track pages
2. `pagedata["fan_tralbum_data"]["band_id"]`
3. `pagedata["id"]`
4. `data-band` HTML attribute on any element (common on artist pages)
5. `band_id=` query parameter inside `pagedata["lo_querystr"]`

**`artist` extraction strategy** (tried in this priority order):
1. `tralbum["artist"]`
2. `pagedata["artist"]`
3. `<p id="band-name-location"><span class="title">` — HTML fallback
4. Literal string `"Unknown Artist"`

**Example:**
```python
from bandcamp_wacz.bandcamp import parse_page

info = parse_page("https://someartist.bandcamp.com/album/some-album")
# {
#   'band_id': 3774983561,
#   'artist': 'Some Artist',
#   'cover_url_0': 'https://f4.bcbits.com/img/a0737321838_0',
#   'item_id': 3853844384,
#   'title': 'Some Album',
#   'is_preorder': False
# }
```

---

### `create_safe_filename(name) → str`

Sanitises an arbitrary string so it is safe to use as a filesystem path component. Logic is intentionally kept in sync with `fetch_metadata.py` so folder names remain consistent across the pipeline.

**Transformations applied (in order):**
1. Strips Unicode control characters (`\x00–\x1f`, `\x7f`, zero-width chars, BOM)
2. Replaces ` | ` with ` - `
3. Replaces shell-unsafe characters (`\ / ? % * : | " < >`) with `-`
4. Collapses multiple spaces to one
5. Strips leading dots
6. Collapses multiple consecutive `-` into one
7. Strips leading/trailing whitespace and trailing dots

**Example:**
```python
from bandcamp_wacz.bandcamp import create_safe_filename

create_safe_filename("My Album: Special Edition | 2024")
# 'My Album- Special Edition - 2024'

create_safe_filename("...Hidden Track")
# 'Hidden Track'
```

---

### `truncate_filename(display_name, suffix, max_bytes, style) → str`

Ensures a filename fits within a byte budget (for archive.org path limits).

**Parameters:**

| Parameter | Type | Description |
|---|---|---|
| `display_name` | `str` | The title portion of the filename, without extension |
| `suffix` | `str` | Everything after the title, e.g. `' [3853844384].wacz'` |
| `max_bytes` | `int` | Maximum total filename length in UTF-8 bytes |
| `style` | `str` | Truncation style: `'end'`, `'middle'`, or `'hash'` |

**Truncation styles:**

| Style | Behaviour |
|---|---|
| `'end'` | Cuts from the right end of the title. Default/safest option. |
| `'middle'` | Keeps the start and end of the title, inserting `...` in the middle. Good for preserving context from both ends. |
| `'hash'` | Replaces the title entirely with a 16-character SHA-1 hex digest of the original title. Guarantees uniqueness. |

If the suffix alone exceeds `max_bytes`, a warning is logged and the filename is returned unchanged.

**Example:**
```python
from bandcamp_wacz.bandcamp import truncate_filename

# Title fits — no truncation
truncate_filename("Short Title", " [1234].wacz", 255, "end")
# 'Short Title [1234].wacz'

# Title too long — truncate from end
truncate_filename("A" * 300, " [1234].wacz", 255, "end")
# '...AAA [1234].wacz'  (trimmed to fit)

# Middle truncation
truncate_filename("Very Long Title That Goes On Forever", " [1234].wacz", 40, "middle")
# 'Very Long...Forever [1234].wacz'

# Hash truncation — title replaced with digest
truncate_filename("Some Extremely Long Title", " [1234].wacz", 40, "hash")
# 'a3f9bc1200d4e1fa [1234].wacz'
```

---

### `artist_folder_name(artist, band_id) → str`

Returns the canonical folder name used to store an artist's content on disk.

**Format:** `{safe_artist_name} [{band_id}]`

If `band_id` is `None`, returns just the sanitised artist name.

**Example:**
```python
from bandcamp_wacz.bandcamp import artist_folder_name

artist_folder_name("Artist", 1234567890)
# 'Artist [1234567890]'

artist_folder_name("My/Artist: Name!", None)
# 'My-Artist- Name!'
```

---

### `subdomain_from_url(url) → str`

Extracts the subdomain from a Bandcamp URL. Since every Bandcamp artist lives at `{artist}.bandcamp.com`, this effectively returns the artist's URL slug.

**Example:**
```python
from bandcamp_wacz.bandcamp import subdomain_from_url

subdomain_from_url("https://someartist.bandcamp.com/album/title")
# 'someartist'
```

---

## Internal Components

### SSL Adapter

`_SSLAdapter` and `_build_session()` create a `requests.Session` with a custom TLS cipher suite. This prevents certain Bandcamp CDN/server configurations from rejecting requests based on TLS fingerprinting (a common anti-scraping measure). The session is module-level and reused across all requests for connection pooling efficiency.

### Request Delay (`_apply_delay`)

Called automatically inside `fetch_url` before every request. Reads `BC_REQUEST_DELAY` from config, which can be either:
- A plain number (milliseconds), e.g. `"1500"` → always waits 1.5 seconds
- A `"lo-hi"` range string, e.g. `"1000-3000"` → waits a random 1–3 seconds

This jitter is important for avoiding rate limits when crawling many pages.

### Page Data Extraction

Two private functions extract the main JSON payloads embedded in Bandcamp's HTML:

- `_pagedata_blob(soup)` — parses the `data-blob` attribute of `<div id="pagedata">`. Contains artist/band-level information.
- `_tralbum_blob(soup)` — parses the `data-tralbum` attribute of an embedded `<script>` tag. Contains album/track-level information including `current`, `is_preorder`, and `artist`.

Both functions attempt `json.loads` first, then fall back to `demjson3.decode` for pages with non-standard or slightly malformed JSON.

---

## Typical Pipeline Usage

```python
from bandcamp_wacz.bandcamp import parse_page, artist_folder_name, truncate_filename

# 1. Fetch minimal page data
info = parse_page("https://someartist.bandcamp.com/album/some-album")

# 2. Build the artist folder path
folder = artist_folder_name(info["artist"], info["band_id"])
# → 'Some Artist [3774983561]'

# 3. Build a safe, length-limited filename for the .wacz archive
suffix = f" [{info['item_id']}].wacz"
filename = truncate_filename(info["title"], suffix, max_bytes=255, style="end")
# → 'Some Album [3853844384].wacz'
```

The result of `parse_page` feeds directly into `archive.py` / `crawl.py`, which uses the `band_id`, `cover_url_0`, and `is_preorder` fields to configure the Browsertrix crawl job.

---

## Suggested Improvements

### URL validation helper
A `validate_bandcamp_url(url)` function that checks whether a URL is a recognisable Bandcamp URL (subdomain or custom domain, with an `/album/` or `/track/` path) before any HTTP request is made. This would give early, clear errors instead of a cryptic parse failure when a typo or wrong URL is passed.

### Pre-order filtering in the pipeline
`parse_page` already returns `is_preorder`, but the crawl pipeline does not currently act on it. A configurable option to skip pre-orders automatically (or queue them for re-crawl on release day) would be useful for users archiving label catalogues where pre-orders are common.

### Retry on `parse_page`
`parse_page` calls `fetch_url` (which retries on network errors) but does not retry if the returned HTML is missing the expected data blobs — for instance if Bandcamp returns a soft-error or geo-blocked page. A check for a minimum set of required keys with a retry would make the fetcher more robust against transient oddities.

### Custom domain support
Bandcamp artists with custom domains (e.g. `https://artist.com` rather than `https://artist.bandcamp.com`) are not currently handled by `subdomain_from_url`. A helper that detects and normalises custom domains — either by following a redirect to the `.bandcamp.com` canonical URL or by noting the custom domain in metadata — would improve coverage.
