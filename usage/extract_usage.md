# `extract.py` — Usage Guide

## Overview

`extract.py` extracts audio tracks and cover art from Bandcamp `.wacz` archive files without re-downloading anything from the internet. It reads directly from the WACZ, matches tracks to metadata from a sidecar JSON, applies ID3 tags via `mutagen`, and writes files to a clean per-album folder.

---

## Output Layout

```
<output_root>/
  <Album Title> [<item_id>]/
    cover.jpg
    01 - Track Title [<track_id>].mp3
    02 - Track Title [<track_id>].mp3
    02_cover.jpg        ← only with --track-covers, only if different from album cover
```

Track numbers are zero-padded to two digits. The `[track_id]` suffix in each filename is Bandcamp's internal track identifier. Track cover images are only saved when their image hash differs from the album cover — duplicates are suppressed automatically.

---

## Requirements

`mutagen` must be installed for ID3 tagging. Without it, audio is still extracted but tags are not applied:

```bash
pip install mutagen
```

---

## Command-Line Usage

```
python bandcamp_wacz/extract.py PATH [PATH ...] [OPTIONS]
```

`PATH` can be one or more `.wacz` files, directories containing `.wacz` files, or any mix of both. When given a directory, all `.wacz` files in that directory are processed.

| Flag | Description |
|---|---|
| `--output DIR`, `-o DIR` | Root directory for extracted files. Defaults to the same directory as each WACZ file |
| `--track-covers` | Save per-track cover images when they differ from the album cover |
| `--ask` | If metadata cannot be found automatically, prompt for a JSON path instead of skipping |
| `--auto-pick` | If the same `item_id` appears in multiple artist JSONs, automatically pick the first alphabetically instead of prompting |
| `--debug` | Enable verbose `DEBUG`-level logging |

**Examples:**

```bash
# Extract a single album
python bandcamp_wacz/extract.py wacz_output/Some\ Album\ \[3853844384\].wacz

# Extract all WACZs in a directory
python bandcamp_wacz/extract.py wacz_output/

# Extract to a specific music library folder
python bandcamp_wacz/extract.py album.wacz --output ~/Music/Bandcamp/

# Include per-track covers for releases with different art per track
python bandcamp_wacz/extract.py album.wacz --track-covers

# Prompt for metadata if the JSON cannot be found automatically
python bandcamp_wacz/extract.py album.wacz --ask

# Process a whole directory, non-interactively, with debug output
python bandcamp_wacz/extract.py wacz_output/ --auto-pick --debug
```

---

## Metadata Discovery

`extract.py` needs album/track metadata (titles, track IDs, art IDs, etc.) to match audio records in the WACZ to their correct filenames and tags. It searches in this order:

### 1. Release JSON (alongside the WACZ)
Looks for `<wacz_stem>.json` in the same directory as the WACZ. This file is written automatically by `archive.py` / `metadata.py` during the crawl pipeline and is the preferred source.

```
wacz_output/
  Some Album [3853844384].wacz
  Some Album [3853844384].json   ← used automatically
```

### 2. Artist JSON (searched by `item_id`)
If no release JSON is found, the `item_id` is parsed from the WACZ filename (the number inside `[...]` at the end of the stem). All artist JSON files under the following directories are scanned for an album entry with a matching `item_id`:

- `./artists/` (current working directory)
- `<project_root>/artists/`
- `<wacz_parent>/../artists/` (relative to the WACZ location)

If the same `item_id` is found in multiple artist JSONs (e.g. a split release), the user is prompted to choose. Pass `--auto-pick` to suppress this prompt and always take the first match.

### 3. Manual path (`--ask`)
If both automatic sources fail and `--ask` is set, the script prompts for a file path to a release JSON or artist JSON. The path is accepted as-is (no shell escaping needed). If an artist JSON is provided, the script extracts the matching album entry by `item_id` automatically.

If all three sources fail and `--ask` is not set, the WACZ is skipped and counted as a failure.

---

## ID3 Tags Applied

The following ID3v2.3 frames are written to each MP3:

| Frame | Source |
|---|---|
| `TIT2` (Title) | `trackinfo[n].title` |
| `TPE1` (Track Artist) | `trackinfo[n].artist` → falls back to `album.artist` |
| `TPE2` (Album Artist) | `album.artist` |
| `TALB` (Album) | `album.title` |
| `TRCK` (Track Number) | `trackinfo[n].track_num` (clamped to minimum 1) |
| `TDRC` (Year) | 4-digit year parsed from `album.datePublished` |

Any existing ID3 header in the raw MP3 bytes is stripped and replaced cleanly. If `mutagen` is not installed, the original bytes are written unmodified with a warning logged.

---

## How WACZ Extraction Works

A WACZ file is a ZIP archive containing WARC files (web archive records) and a compressed CDX index. `extract.py` reads these directly without unpacking to disk:

1. **CDX index** (`indexes/index.cdx.gz`) — a gzipped newline-delimited JSON file where each record describes one captured HTTP response: its URL, MIME type, HTTP status, and the byte offset and length of the corresponding WARC record.

2. **Audio records** — identified by matching CDX URLs against the pattern `t4.bcbits.com/stream/*/mp3-128/<track_id>` with MIME type `audio/mpeg` and status `200`.

3. **Image records** — identified by matching CDX URLs against `f4.bcbits.com/img/a{art_id}_0` with status `200`.

4. **WARC extraction** — for each matched record, `extract.py` opens the WARC file inside the ZIP, seeks to the byte offset by reading past it (ZipExtFile is not randomly seekable), reads the compressed chunk, decompresses it, then skips past both the WARC header block and the HTTP header block to get the raw response body.

This means extraction is entirely offline — no network requests are made.

---

## Programmatic Usage

```python
from pathlib import Path
from bandcamp_wacz.extract import extract_wacz
import json

album = json.loads(Path("Some Album [3853844384].json").read_text())

extract_wacz(
    wacz_path=Path("wacz_output/Some Album [3853844384].wacz"),
    output_root=Path("~/Music/Bandcamp").expanduser(),
    album=album,
    track_covers=False,
)
```

### Loading metadata manually

```python
from pathlib import Path
from bandcamp_wacz.extract import _load_metadata

album = _load_metadata(
    wacz_path=Path("Some Album [3853844384].wacz"),
    ask=False,
    artists_dirs=[Path("artists/")],
    auto_pick=True,
)
```

---

## Exit Codes

| Code | Meaning |
|---|---|
| `0` | All WACZ files processed successfully |
| `1` | One or more WACZ files failed (metadata not found, extraction error, etc.) |

A summary is always printed at the end:

```
── Summary ─────────────────────────────
  Succeeded : 5
  Failed    : 1
```

---

## Suggested Improvements

### Embed cover art into MP3 ID3 tags
Currently the album cover is saved as `cover.jpg` alongside the MP3s but is not embedded into each track's ID3 tags as an `APIC` frame. Most music players and portable devices expect the cover to be embedded. This would be a straightforward addition using `mutagen.id3.APIC`.

### `--skip-existing` / `--overwrite` flags
When re-running extraction against a directory that already has some tracks extracted, there is currently no way to skip files that already exist. A `--skip-existing` flag would avoid redundant work on large libraries; `--overwrite` (the current implicit behaviour) would remain the default or an explicit opt-in.

### `--dry-run`
A dry-run mode that logs what would be extracted (track count, cover presence, output paths) without writing anything to disk — useful for verifying metadata resolution before committing to a full extraction.

### `--format` / output format options
Support for an artist/album directory hierarchy option, e.g. `--structure artist/album` to organise output as `<Artist>/<Album Title> [item_id]/` instead of a flat output root. This would make the extracted files drop straight into a standard music library layout.

### Machine-readable output
An optional `--report <file>.json` flag that writes a structured summary of what was extracted, what was skipped, and what failed — useful for scripting or integrating with a media library manager.

### Partial extraction recovery
If a track's audio record is missing from the WACZ (e.g. the behavior script timed out mid-album), the script currently logs a warning and moves on. A `--missing-report` flag could output a list of missing track IDs that could then be re-crawled individually.
