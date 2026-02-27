# `update_metadata.py` — Usage Guide

## Overview

`update_metadata.py` keeps artist JSONs in sync with Bandcamp after the initial `fetch_metadata.py` run. It fetches the live Bandcamp page for one or more artists, compares each release against what is stored locally, and:

- **Adds** new releases to the artist JSON and their URLs to `bandcamp-dump.lst`
- **Updates** changed releases in place, recording old values in a `_history` list and resetting pipeline state so the changed release is re-queued for archiving
- **Leaves unchanged releases untouched** — including all pipeline state fields (`archived`, `uploaded`, `ia_identifier`, etc.)

It has two operating modes: **full artist update** (default — re-checks the entire discography) and **single-release update** (`--release` — re-checks one specific URL without fetching the whole artist page).

---

## Command-Line Usage

```
python update_metadata.py URL [URL ...] [OPTIONS]
```

In default mode each URL is stripped to the artist root (e.g. `https://artist.bandcamp.com/album/foo` becomes `https://artist.bandcamp.com/`) and deduplicated, so passing multiple URLs for the same artist results in a single update pass. Multiple different artists can be updated in one invocation.

| Flag | Description |
|---|---|
| `--dry-run` | Show what would be added or changed without writing anything to disk |
| `--release` | Treat each URL as a specific release to update individually, without fetching the full artist discography |
| `-d MS`, `--delay MS` | Delay between requests in ms. Single value (`"2000"`) or range (`"1000-3000"`). Defaults to `BC_REQUEST_DELAY` from `.env` |
| `--retries N` | Max retry attempts on failed requests. Defaults to `BC_MAX_RETRIES` from `.env` |
| `--debug` | Enable verbose `DEBUG`-level logging |

**Examples:**

```bash
# Check an artist's full discography for new or changed releases
python update_metadata.py https://someartist.bandcamp.com/

# Check multiple artists in one pass
python update_metadata.py https://artist-a.bandcamp.com/ https://artist-b.bandcamp.com/

# URL from a notification email — stripped to artist root automatically
python update_metadata.py https://someartist.bandcamp.com/album/new-album

# Dry run to preview changes before writing
python update_metadata.py https://someartist.bandcamp.com/ --dry-run

# Re-queue a single release (e.g. its WACZ was deleted)
python update_metadata.py https://someartist.bandcamp.com/album/some-album --release

# Update a release and debug network calls
python update_metadata.py https://someartist.bandcamp.com/album/some-album --release --debug
```

---

## Operating Modes

### Default: Full Artist Update (`update_artist`)

Fetches the artist's `/music` page, discovers every current release URL, parses each one in full (including individual track page fetches — one request per track), and compares against the stored artist JSON.

For each live release:
- If the `item_id` **matches an existing entry** → run change detection; update if changed
- If the `item_id` is **not in the JSON** → add as a new release with `archived=False`, `uploaded=False`

After processing all releases, the updated artist JSON is written and new URLs are appended to `bandcamp-dump.lst`.

**Note:** Because this mode fetches every track page for every release, it is slow for large discographies. Use `--release` mode when you only need to re-check a specific album.

### `--release`: Single-Release Update (`update_release`)

Parses a single release URL, extracts its `band_id` from the page, locates the corresponding artist folder, and updates just that one entry. No `/music` page fetch, no discography scan.

This mode is particularly useful for:
- Re-queuing a release whose WACZ was deleted — it resets `archived=False` if the metadata has changed since the last crawl
- Manually adding a release that was missed during the initial `fetch_metadata.py` run
- Quickly checking a specific album for changes without a full artist scan

---

## Change Detection

### Watched Fields

Changes are detected on these album-level fields:

| Field | Description |
|---|---|
| `title` | Album title |
| `artist` | Artist name |
| `coverUrl_0` | Full-resolution cover image URL |
| `is_preorder` | Whether the release is a pre-order (triggers re-archive when it goes live) |

And within each `trackinfo` entry:

| Field | Description |
|---|---|
| `title` | Track title |
| `duration` | Track duration |
| `track_id` | Bandcamp track identifier |
| `url` | Track page URL |
| `trackCoverUrl_0` | Per-track cover image URL |

A change in **track count** (albums gaining or losing tracks) also counts as a `trackinfo` change.

### Protected Fields

Pipeline state fields are **never overwritten** by an update, even if the fresh page returns different values:

`archived`, `uploaded`, `archived_at`, `uploaded_at`, `ia_identifier`

---

## How Changes Are Applied

When a change is detected, `apply_changes` does the following:

1. **Records the old values in `_history`** — a list of history entries appended to the album dict:
```json
{
  "_history": [
    {
      "changed_at": "2026-01-15T12:34:56+00:00",
      "archived_at_change": true,
      "uploaded_at_change": true,
      "ia_identifier_at_change": "wacz-3774983561-3853844384-20260110",
      "fields": {
        "title": "Old Album Title"
      }
    }
  ]
}
```

2. **Writes new values** for all changed watched fields

3. **Resets pipeline state** so the changed release is re-queued:
   - `archived` → `False`
   - `uploaded` → `False`
   - `ia_identifier` → `null`

The old `ia_identifier` is preserved in the history entry so the previous archive.org item remains traceable.

---

## `bandcamp-dump.lst` Maintenance

New releases discovered during an update are appended to the artist's `bandcamp-dump.lst` file. Before appending, the script reads the existing file and deduplicates — a URL already in the list is never added again. URLs are only added for releases successfully parsed (parse failures are skipped with a warning).

---

## Prerequisite: Artist JSON Must Exist

`update_metadata.py` requires the artist to already have a folder and JSON under `ARTISTS_DIR`. If the artist folder is not found (identified by `band_id`), the script logs an error and returns `False` for that artist. Run `fetch_metadata.py` first to onboard a new artist.

This is by design — `update_metadata.py` is a maintenance tool, not an onboarding tool. The `email_watcher` respects this boundary: it calls `fetch_metadata.py`'s full pipeline for new artists and `update_metadata.py`'s quick path for known ones.

---

## Single-Release Artist Handling

Some Bandcamp artists have only one release and no `/music` grid page. In default mode, when `get_album_urls_from_artist_page` returns an empty list, the script falls back to the `original_urls` list — the original URL(s) passed on the command line. This ensures single-release artists can still be updated without switching to `--release` mode manually.

---

## Programmatic Usage

```python
from pathlib import Path
from fetch_metadata import Bandcamp
from update_metadata import update_artist, update_release

scraper = Bandcamp(delay_arg="1000-3000")

# Update full discography for an artist
success = update_artist(
    artist_url="https://someartist.bandcamp.com/",
    scraper=scraper,
    dry_run=False,
)

# Update a single release
success = update_release(
    release_url="https://someartist.bandcamp.com/album/some-album",
    scraper=scraper,
    dry_run=False,
)
```

Both functions return `True` on success and `False` on failure.

---

## Exit Codes

| Code | Meaning |
|---|---|
| `0` | All artist/release updates succeeded |
| `1` | One or more updates failed |

A summary is always printed:

```
── Summary ─────────────────────────────
  Succeeded : 2
  Failed    : 0
```

---

## Suggested Improvements

### Atomic artist JSON writes
Like `metadata.py`, `update_artist` and `update_release` both write the artist JSON with `Path.write_text` directly. An interrupted write (disk full, power loss) could corrupt the file. Using a `.tmp` + `Path.replace()` pattern — already used correctly in `fetch_metadata.py`'s partial file writes — would make these writes safe.

### `--all` flag to update every known artist
A `--all` flag that iterates over every folder in `ARTISTS_DIR` and runs a full update for each would make it trivial to keep the entire library fresh with a single cron job (`0 6 * * * python update_metadata.py --all`), rather than maintaining a separate list of artist URLs.
