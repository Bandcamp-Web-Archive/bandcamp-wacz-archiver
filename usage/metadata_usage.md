# `metadata.py` — Usage Guide

## Overview

`metadata.py` is Step 2 of the archival pipeline. It is called automatically by `crawl.py` after each successful WACZ is produced, and does three things:

1. **Writes a release JSON** — a single-album metadata file placed alongside the WACZ, consumed by the upload step and by `extract.py`
2. **Marks the album as archived** — sets `archived=True` and `archived_at` (UTC ISO timestamp) in the artist JSON
3. **Embeds provenance metadata into the WACZ** — patches `datapackage.json` inside the WACZ ZIP with `band_id`, `item_id`, and the archive.org identifier so the archive is self-describing

You will not normally call this module directly — `crawl.py` invokes `process_archived_wacz` as the final step of every crawl. The individual functions are exposed for cases where you need to re-run or repair one part of the pipeline independently.

---

## Archive.org Identifier Format

```
wacz-{band_id}-{item_id}-{YYYYMMDD}
```

Example: `wacz-3774983561-3853844384-20260115`

The date component is the UTC date on which `process_archived_wacz` is called (i.e. the crawl date, not the Bandcamp release date).

---

## Public API

### `process_archived_wacz(wacz_path, band_id, item_id) → Path | None`

The main entry point. Runs the full Step 2 pipeline for one successfully crawled album.

**Parameters:**

| Parameter | Type | Description |
|---|---|---|
| `wacz_path` | `Path` | Path to the finished `.wacz` file |
| `band_id` | `int` | Bandcamp band/artist identifier |
| `item_id` | `int` | Bandcamp album/track identifier |

**Returns:** `Path` to the written release JSON, or `None` if the artist JSON could not be found or read.

**Steps performed (in order):**
1. Locates the artist JSON under `ARTISTS_DIR` by matching `[band_id]` in folder names
2. Finds the album entry in the artist JSON by `item_id`
3. Generates the archive.org identifier string
4. Calls `embed_metadata_in_wacz` to patch `datapackage.json` inside the WACZ
5. Calls `write_release_json` to write `<wacz_stem>.json` alongside the WACZ
6. Calls `mark_archived` to update the artist JSON

If the artist JSON is not found, a warning is logged and `None` is returned — this is the expected outcome when `crawl_album` is called with `update_json=False` or before `fetch_metadata.py` has been run for a new artist.

**Example:**
```python
from pathlib import Path
from bandcamp_wacz.metadata import process_archived_wacz

release_json = process_archived_wacz(
    wacz_path=Path("wacz_output/Some Album [3853844384].wacz"),
    band_id=3774983561,
    item_id=3853844384,
)

if release_json:
    print(f"Release JSON written to: {release_json}")
else:
    print("Artist JSON not found — run fetch_metadata.py first")
```

---

### `write_release_json(album, band_id, item_id, wacz_path) → Path`

Writes a single-release JSON file alongside the WACZ. The file is a copy of the album dict from the artist JSON with `archived` and `uploaded` fields stripped, plus two added fields:

| Added field | Value |
|---|---|
| `band_id` | The integer band/artist identifier |
| `ia_identifier` | The generated archive.org identifier string |

The output path is `wacz_path.with_suffix(".json")` — always adjacent to the WACZ.

**Example:**
```python
from pathlib import Path
from bandcamp_wacz.metadata import write_release_json
import json

album = json.loads(Path("artists/Some Artist [3774983561]/Some Artist [3774983561].json").read_text())
# Extract the relevant album dict from the artist JSON first...

release_json_path = write_release_json(
    album=album,
    band_id=3774983561,
    item_id=3853844384,
    wacz_path=Path("wacz_output/Some Album [3853844384].wacz"),
)
# Writes: wacz_output/Some Album [3853844384].json
```

---

### `embed_metadata_in_wacz(wacz_path, band_id, item_id, ia_identifier) → bool`

Patches `datapackage.json` inside the WACZ ZIP to add three provenance fields:

| Field added to `datapackage.json` | Value |
|---|---|
| `bandcamp_band_id` | Integer band ID |
| `bandcamp_item_id` | Integer item ID |
| `bandcamp_ia_identifier` | Archive.org identifier string |

**How it works:** WACZ files are ZIP archives. `datapackage.json` follows the [Frictionless Data spec](https://specs.frictionlessdata.io/data-package/), which allows arbitrary extra fields — making it the correct, spec-compliant place to store provenance metadata. The function reads the entire ZIP into memory, patches `datapackage.json`, then rewrites the ZIP to a `.wacz.tmp` temporary file before atomically replacing the original. All other entries and their original compression types are preserved.

**Returns:** `True` on success, `False` if `datapackage.json` is missing from the WACZ or cannot be parsed. The `.wacz.tmp` temporary file is cleaned up on any error.

**Example:**
```python
from pathlib import Path
from bandcamp_wacz.metadata import embed_metadata_in_wacz

success = embed_metadata_in_wacz(
    wacz_path=Path("wacz_output/Some Album [3853844384].wacz"),
    band_id=3774983561,
    item_id=3853844384,
    ia_identifier="wacz-3774983561-3853844384-20260115",
)
```

---

### `mark_archived(json_path, artist_key, album_index, data) → None`

Sets `archived=True` and `archived_at=<UTC ISO timestamp>` on a specific album entry in the artist JSON dict, then writes the updated dict back to disk.

This function operates on the already-parsed `data` dict (not re-reading from disk), so the caller is responsible for passing consistent values for `artist_key` and `album_index` — both of which come from `_find_album`.

**Parameters:**

| Parameter | Type | Description |
|---|---|---|
| `json_path` | `Path` | Path to the artist JSON file to write back |
| `artist_key` | `str` | The top-level key in the artist JSON under which the album list lives |
| `album_index` | `int` | Index of the album within `data[artist_key]` |
| `data` | `dict` | The full parsed artist JSON dict (will be mutated in place) |

---

## Artist JSON Structure

`metadata.py` expects the artist JSON to have this general shape:

```json
{
  "_some_private_key": "...",
  "some_release_key": [
    {
      "item_id": 3853844384,
      "title": "Some Album",
      "artist": "Some Artist",
      "art_id": 737321838,
      "trackinfo": [...],
      "archived": false
    },
    ...
  ]
}
```

Keys beginning with `_` are skipped during album search. Any other key whose value is a list is treated as a potential container of album dicts and searched for a matching `item_id`. `archived` and `uploaded` are stripped from the release JSON copy so they stay as internal tracking state only.

---

## File Layout After `process_archived_wacz`

```
wacz_output/
  Some Album [3853844384].wacz       ← datapackage.json patched in-place
  Some Album [3853844384].json       ← release JSON written here

artists/
  Some Artist [3774983561]/
    Some Artist [3774983561].json    ← archived=True, archived_at set here
```
