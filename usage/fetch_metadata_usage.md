# `fetch_metadata.py` — Usage Guide

## Overview

`fetch_metadata.py` is the metadata bootstrap step for the archival pipeline. Before any WACZ can be produced or uploaded, the pipeline needs a rich metadata record for each release — track listings, IDs, cover art references, credits, lyrics, classification, and more. This script scrapes that data from Bandcamp and writes two files to disk:

- `artists/{Artist Name} [{band_id}]/{Artist Name} [{band_id}].json` — the artist JSON, containing all release metadata in a structured format
- `artists/{Artist Name} [{band_id}]/bandcamp-dump.lst` — a plain-text URL list of every release, consumed by the crawl pipeline

It is designed to be run **once per artist** to onboard them, after which the artist JSON is maintained incrementally by the rest of the pipeline. If the `email_watcher` detects a new artist it hasn't seen before, it runs the full pipeline which includes this step automatically.

---

## Output Layout

```
artists/
  Some Artist [3774983561]/
    Some Artist [3774983561].json    ← all release metadata
    bandcamp-dump.lst                ← one release URL per line
```

### Artist JSON Structure

```json
{
  "Some Artist": [
    {
      "url": "https://someartist.bandcamp.com/album/some-album",
      "title": "Some Album",
      "artist": "Some Artist",
      "label": "Some Label",
      "band_id": 3774983561,
      "classification": "paid",
      "tags": ["ambient", "electronic"],
      "item_id": 3853844384,
      "art_id": 737321838,
      "is_preorder": false,
      "datePublished": "02 Jan 2026 17:48:23 GMT",
      "about": "Album description text...",
      "credits": "Mastered by...",
      "license": "all rights reserved",
      "coverUrl_0": "https://f4.bcbits.com/img/a0737321838_0",
      "trackinfo": [
        {
          "title": "Track One",
          "duration": "04:32",
          "lyrics": null,
          "label": "Some Label",
          "track_id": 1234567890,
          "track_num": "1",
          "artist": "Some Artist",
          "url": "https://someartist.bandcamp.com/track/track-one",
          "trackCoverUrl_0": "https://f4.bcbits.com/img/a0737321838_0",
          "art_id": 737321838,
          "about": null,
          "credits": null,
          "license": null
        }
      ],
      "archived": false
    }
  ],
  "_band_id": 3774983561
}
```

The top-level key is the artist name string. `_band_id` is stored separately at the root for quick access without iterating releases. `archived` is set to `true` by `metadata.py` after a successful crawl.

---

## Command-Line Usage

```
python fetch_metadata.py URL [URL ...] [OPTIONS]
```

Each URL can be either an **artist/discography page** (which discovers all releases automatically) or a **direct album/track URL**. Both can be mixed freely in a single invocation.

| Flag | Description |
|---|---|
| `-d MS`, `--delay MS` | Delay between requests in milliseconds. Single value (`"2000"`) or range (`"1000-3000"`). Defaults to `BC_REQUEST_DELAY` from `.env` |
| `-r N`, `--retries N` | Max retry attempts on failed requests. Defaults to `BC_MAX_RETRIES` from `.env` |
| `-rd SECS`, `--retry-delay SECS` | Base retry delay in seconds (multiplied by attempt number). Defaults to `BC_RETRY_DELAY` from `.env` |
| `--band-id ID` | Manually override the `band_id` instead of extracting it from the page. Useful when the artist page does not expose it |
| `--debug` | Enable verbose `DEBUG`-level logging |

**Examples:**

```bash
# Fetch all releases from an artist's discography page
python fetch_metadata.py https://someartist.bandcamp.com

# Fetch a single album
python fetch_metadata.py https://someartist.bandcamp.com/album/some-album

# Fetch specific albums from the same artist
python fetch_metadata.py https://someartist.bandcamp.com/album/album-one \
                         https://someartist.bandcamp.com/album/album-two

# Fetch discography with custom delay and debug output
python fetch_metadata.py https://someartist.bandcamp.com --delay 2000-4000 --debug

# Override band_id when auto-detection fails
python fetch_metadata.py https://someartist.bandcamp.com --band-id 3774983561
```

---

## Discography Discovery

When given an artist root URL (path is `/`, empty, `/music`, or `/music/`), the script fetches the `/music` page and extracts all release URLs from the music grid.

It tries two methods in order:
1. **`data-client-items` JSON attribute** on the `<ol id="music-grid">` element — the most reliable source, present on most Bandcamp pages
2. **Anchor tag scraping** — fallback for pages where the data attribute is absent, scrapes `<li class="music-grid-item"> a` href values

All discovered URLs are deduplicated before fetching begins.

---

## Per-Release Fetching: What Gets Scraped

For each album or track URL, `Bandcamp.parse()` makes at minimum **one request per track** in addition to the album page itself, in order to capture track-level data that is only available on individual track pages (unique per-track cover art, track-specific credits, about text, and license).

**Album-level fields** (from the album page):

| Field | Source |
|---|---|
| `title` | `tralbum["current"]["title"]` → first track title → `ld+json["name"]` |
| `artist` | `tralbum["artist"]` |
| `label` | `a.back-to-label-link span.back-link-text` → `item_sellers[band_id]["name"]` |
| `band_id` | See [band_id extraction](#band_id-extraction) below |
| `classification` | See [release classification](#release-classification) below |
| `tags` | `tralbum["keywords"]` |
| `item_id` | `tralbum["current"]["id"]` |
| `art_id` | `tralbum["art_id"]` |
| `is_preorder` | `tralbum["is_preorder"]` |
| `datePublished` | `album_release_date` → `current["release_date"]` → `embed_info["item_public"]` |
| `about` | `.tralbumData.tralbum-about` |
| `credits` | `.tralbumData.tralbum-credits` |
| `license` | `#license.info.license` |
| `coverUrl_0` | `#tralbumArt a[href]` with size suffix replaced by `_0` |

**Track-level fields** (from individual track pages, per track):

| Field | Source |
|---|---|
| `title` | `trackinfo[n]["title"]` (with `"Artist - "` prefix stripped if present) |
| `duration` | `trackinfo[n]["duration"]` (formatted as `MM:SS` or `HH:MM:SS`) |
| `lyrics` | `tr#lyrics_row_{track_num} div` on the album page |
| `track_id` | `trackinfo[n]["id"]` |
| `track_num` | `trackinfo[n]["track_num"]` |
| `artist` | `trackinfo[n]["artist"]` → falls back to album artist |
| `url` | `trackinfo[n]["title_link"]` resolved against the album URL |
| `trackCoverUrl_0` | From individual track page — `#tralbumArt a[href]` with `_0` suffix |
| `art_id` | `tralbum["art_id"]` from the individual track page |
| `about` | `.tralbumData.tralbum-about` on the track page |
| `credits` | `.tralbumData.tralbum-credits` on the track page |
| `license` | `#license.info.license` on the track page |

Tracks without a streamable file (`trackinfo[n]["file"]` absent) are silently skipped — these are typically locked pre-order tracks.

---

## `band_id` Extraction

The `band_id` is extracted by trying four locations in order (artist pages and album pages expose it differently):

1. `tralbum["current"]["band_id"]` — most reliable on album/track pages
2. `#pagedata[data-blob]["id"]` — present on some artist pages
3. `data-band` HTML attribute parsed as JSON — most reliable on artist pages when logged out
4. `band_id=` query parameter in `pagedata["lo_querystr"]`

If all four fail, the fallback is to fetch the first discovered album URL and retry the same chain there. If that also fails, a warning is printed and `band_id` is stored as `null`. Use `--band-id` to supply it manually in this case.

---

## Release Classification

Each release is classified into one of three values stored in the `classification` field:

| Value | Meaning |
|---|---|
| `"nyp"` | Name Your Price — user sets their own price (including free) |
| `"free"` | Free download — no payment required or offered |
| `"paid"` | Paid release — fixed price with no free option |

Detection checks (in order): the NYP span text, the free download button text (including Japanese), `free_album_download` on any track, and `current["minimum_price"] == 0`.

---

## Interruption and Resume

`fetch_metadata.py` is designed to survive interruption gracefully. After every successfully parsed release, progress is written atomically to a `.partial` file (`{json_path}.partial`) using a `.tmp` + `os.replace()` pattern. If the script is interrupted (Ctrl-C, crash, network loss), re-running the same command will:

1. Detect the `.partial` file
2. Load already-fetched releases from it
3. Skip any URL whose `url` or `item_id` is already present
4. Continue from where it left off

Once all releases are processed and the final JSON is written, the `.partial` file is deleted. The `.partial` file has the same structure as the final JSON so it can be inspected or used directly if needed.

---

## Programmatic Usage

The `Bandcamp` class can be used directly without going through the CLI:

```python
from fetch_metadata import Bandcamp, is_artist_page

scraper = Bandcamp(delay_arg="1000-3000")

# Discover all release URLs for an artist
urls = scraper.get_album_urls_from_artist_page("https://someartist.bandcamp.com")

# Parse a single release
album = scraper.parse("https://someartist.bandcamp.com/album/some-album")
print(album["title"], album["band_id"], len(album["trackinfo"]))
```
