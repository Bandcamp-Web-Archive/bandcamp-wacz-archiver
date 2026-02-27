# `upload.py` — Usage Guide

## Overview

`upload.py` is the final step of the archival pipeline. It takes completed WACZ files and their sidecar release JSONs, uploads both to a single archive.org item per release, marks the release as `uploaded` in the artist JSON, then deletes the local copies of both files.

Each release becomes its own archive.org item, identified by the `ia_identifier` string generated during the metadata step (`wacz-{band_id}-{item_id}-{YYYYMMDD}`).

---

## Requirements

The `internetarchive` Python library must be installed:

```bash
pip install internetarchive
```

`IA_ACCESS_KEY` and `IA_SECRET_KEY` must be set in `.env`. Both are required — the script exits immediately if either is missing. Keys can be obtained from [archive.org/account/s3.php](https://archive.org/account/s3.php).

---

## Command-Line Usage

```
python upload.py PATH [PATH ...] [OPTIONS]
```

`PATH` can be one or more `.wacz` files, directories containing `.wacz` files, or a mix of both. For each WACZ, a sidecar `.json` file must exist at the same path with the same stem (e.g. `Album Title [3853844384].wacz` requires `Album Title [3853844384].json` alongside it).

| Flag | Description |
|---|---|
| `--dry-run` | Preview what would be uploaded — prints the identifier and all IA metadata for each file without uploading or deleting anything |
| `--debug`, `-d` | Enable verbose `DEBUG`-level logging |

**Examples:**

```bash
# Upload everything in the default output directory
python upload.py wacz_output/

# Upload a single WACZ
python upload.py "wacz_output/Some Album [3853844384].wacz"

# Upload two specific files
python upload.py "wacz_output/Album A [1111].wacz" "wacz_output/Album B [2222].wacz"

# Preview without uploading
python upload.py wacz_output/ --dry-run
```

---

## What Gets Uploaded

Each archive.org item receives two files:

| File | Description |
|---|---|
| `<Title> [item_id].wacz` | The complete web archive of the Bandcamp page and audio |
| `<Title> [item_id].json` | The release metadata sidecar (track listing, credits, cover URL, etc.) |

---

## Archive.org Item Metadata

The following fields are set on each IA item at upload time, derived from the release JSON:

| IA Field | Source in release JSON |
|---|---|
| `title` | `ia_identifier` (the item's canonical identifier string) |
| `creator` | `artist` |
| `artist` | `artist` |
| `album_title` | `title` |
| `mediatype` | `"data"` (hardcoded) |
| `collection` | `IA_COLLECTION` from `.env` (default: `opensource_media`) |
| `date` | 4-digit year parsed from `datePublished` |
| `original_url` | `url` (the original Bandcamp page URL) |
| `band_id` | `band_id` (as string) |
| `item_id` | `item_id` (as string) |

---

## Identifier Resolution

The upload needs three values to proceed: `band_id`, `item_id`, and `ia_identifier`. These are resolved with a fallback chain to handle edge cases where one source may be missing:

**`band_id` and `item_id`:**
1. Embedded in `datapackage.json` inside the WACZ (`bandcamp_band_id`, `bandcamp_item_id`) — written there by `metadata.py`
2. Top-level fields in the sidecar release JSON
3. `item_id` only: parsed from the WACZ filename (`[item_id].wacz` suffix)

**`ia_identifier`:**
1. `bandcamp_ia_identifier` in `datapackage.json` inside the WACZ
2. `ia_identifier` field in the sidecar release JSON

If no `ia_identifier` can be resolved from either source, the upload is skipped with an error. This prevents creating orphaned IA items with no usable identifier.

---

## Retry Behaviour

Each upload is retried up to `IA_MAX_RETRIES` times (default `5`) with linear back-off (`IA_RETRY_DELAY × attempt`, default 10 s base). With defaults:

| Attempt | Wait before next retry |
|---|---|
| 1 (first try) | — |
| 2 | 10 s |
| 3 | 20 s |
| 4 | 30 s |
| 5 | 40 s |
| 6 (final) | 50 s, then marked as failed |

The `internetarchive` library's `checksum=True` option is used, which causes IA to skip re-uploading a file if an identical copy already exists, making retries and re-runs safe.

---

## Keyboard Interrupt / Partial Upload Handling

If the upload is interrupted mid-flight with Ctrl-C:

1. The interrupt is caught cleanly
2. If the IA item had already been created (even partially), `internetarchive.delete` is called immediately with `cascade_delete=True` to remove the incomplete item from archive.org
3. If the automatic deletion fails, the item identifier and a manual deletion URL (`https://archive.org/delete/{identifier}`) are logged so the user can clean up manually
4. `KeyboardInterrupt` is re-raised so the outer loop can exit cleanly

This prevents half-uploaded items from persisting on archive.org without their companion files.

---

## Post-Upload Actions

After a successful upload, two things happen:

### 1. Artist JSON updated

`_mark_uploaded` sets three fields on the matching album entry in the artist JSON:

| Field | Value |
|---|---|
| `uploaded` | `True` |
| `uploaded_at` | UTC ISO 8601 timestamp |
| `ia_identifier` | The archive.org identifier string |

The artist JSON is located by `band_id` under `ARTISTS_DIR`. If it cannot be found (e.g. the artist folder was moved), a warning is logged but the upload is still considered successful.

### 2. Local files deleted

Both the `.wacz` and `.json` files are deleted from disk after a confirmed successful upload. If deletion fails (permissions, file locked), a warning is logged but the upload result is not affected — the files simply remain locally.

---

## Dry Run Output

`--dry-run` prints a full preview for each file without making any network calls or writing anything:

```
  [DRY RUN] Would upload Some Album [3853844384].wacz + Some Album [3853844384].json
            identifier      : wacz-3774983561-3853844384-20260115
            title           : wacz-3774983561-3853844384-20260115
            creator         : Some Artist
            artist          : Some Artist
            album_title     : Some Album
            mediatype       : data
            collection      : opensource_media
            date            : 2026
            original_url    : https://someartist.bandcamp.com/album/some-album
            band_id         : 3774983561
            item_id         : 3853844384
```

---

## Programmatic Usage

```python
from pathlib import Path
from upload import upload_release, collect_wacz_files

# Upload a single release
success = upload_release(
    wacz_path=Path("wacz_output/Some Album [3853844384].wacz"),
    dry_run=False,
)

# Collect and upload all WACZs in a directory
wacz_files = collect_wacz_files(["wacz_output/"])
for wacz_path in wacz_files:
    upload_release(wacz_path, dry_run=False)
```

---

## Exit Codes

| Code | Meaning |
|---|---|
| `0` | All uploads succeeded |
| `1` | One or more uploads failed |

```
── Summary ─────────────────────────────
  Succeeded : 4
  Failed    : 1
```

---

## Suggested Improvements

### Atomic artist JSON writes
Like `metadata.py` and `update_metadata.py`, `_mark_uploaded` writes the artist JSON with `Path.write_text` directly, leaving it vulnerable to corruption on an interrupted write. The `.tmp` + `Path.replace()` pattern used elsewhere would make this safe.

### `--keep-local` flag
Currently, local files are always deleted after a successful upload. A `--keep-local` flag would preserve them — useful for users who want to keep a local backup alongside the archive.org copy, or who are uploading to a secondary collection and want to run `upload.py` again for a different target.

### `--collection` CLI override
`IA_COLLECTION` is read from `.env` with no way to override it per-run on the command line. A `--collection` flag would allow uploading a batch to a different collection without editing `.env` — for instance, uploading a test batch to a personal collection before committing to the main one.

### Resume / skip already-uploaded detection
If `upload_release` is interrupted after the IA upload succeeds but before `_mark_uploaded` writes to the artist JSON, re-running will attempt to re-upload the same files. Since `checksum=True` prevents the IA side from duplicating data, this is harmless but wasteful. Checking whether the IA item already exists (via `session.get_item(identifier).exists`) before uploading would make re-runs instantaneous for already-uploaded items.

### Configurable file deletion behaviour
The post-upload deletion is unconditional and has no dry-run equivalent. Separating it into a distinct step — or at least logging a clear summary of what was deleted — would make the operation more transparent, especially when running on a large batch for the first time.
