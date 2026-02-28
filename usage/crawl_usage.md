# `crawl.py` — Usage Guide

## Overview

`crawl.py` orchestrates the full archiving pipeline for a single Bandcamp album or track URL. It ties together page fetching, Browsertrix configuration, container execution, output file management, and metadata writing into one cohesive flow.

The pipeline for each URL is:

1. Fetch minimal page metadata (`band_id`, `artist`, `cover_url`, `item_id`, `title`) via `bandcamp.parse_page`
2. Build a Browsertrix YAML config seeding the album page and high-resolution cover image
3. Run Browsertrix inside a Podman (or Docker) container
4. Rename and move the output to `<Title> [<item_id>].wacz` in the output directory
5. Delete the `collections/` working directory left by Browsertrix
6. Write a release sidecar JSON and mark the album as archived in the artist JSON via `metadata.process_archived_wacz`

---

## Public API

### `crawl_album(album_url, output_dir=None, update_json=True) → Path`

Runs the complete crawl pipeline for a single Bandcamp album or track URL.

**Parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `album_url` | `str` | — | Full Bandcamp URL to archive, e.g. `https://artist.bandcamp.com/album/title` |
| `output_dir` | `Path \| None` | `WACZ_OUTPUT_DIR` from config | Directory to write the final `.wacz` file into |
| `update_json` | `bool` | `True` | If `False`, skips writing the release sidecar JSON and marking the album as archived in the artist JSON. Useful for quick/test runs ("dumb mode") |

**Returns:** `Path` to the finished `.wacz` file.

**Raises:**
- `Exception` (from `parse_page`) if the Bandcamp page cannot be fetched or parsed
- `RuntimeError` if Browsertrix exits with a non-zero return code and all retries are exhausted
- `FileNotFoundError` if the expected `.wacz` is not found after the crawl completes
- `KeyboardInterrupt` propagates cleanly (mid-crawl or mid-retry-wait) — the temp config file is always cleaned up via `finally`

**Example:**
```python
from bandcamp_wacz.crawl import crawl_album

wacz = crawl_album("https://someartist.bandcamp.com/album/some-album")
print(wacz)  # PosixPath('wacz_output/Some Album [3853844384].wacz')

# Custom output directory, skip metadata step
wacz = crawl_album(
    "https://someartist.bandcamp.com/album/some-album",
    output_dir=Path("/archive/wacz"),
    update_json=False,
)
```

---

### `crawl_list(urls, output_dir=None, skip_errors=True, update_json=True) → dict`

Crawls every URL in a list sequentially, returning a results dictionary.

**Parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `urls` | `list[str]` | — | List of Bandcamp URLs to crawl |
| `output_dir` | `Path \| None` | `WACZ_OUTPUT_DIR` from config | Output directory for all `.wacz` files |
| `skip_errors` | `bool` | `True` | If `True`, a failure on one URL is recorded and the list continues. If `False`, the first failure raises immediately |
| `update_json` | `bool` | `True` | Passed through to `crawl_album` for each URL |

**Returns:** `dict[str, Path | Exception]` — each URL maps to either the `Path` of its `.wacz` on success, or the `Exception` that caused its failure.

**Example:**
```python
from bandcamp_wacz.crawl import crawl_list
from pathlib import Path

urls = [
    "https://artistone.bandcamp.com/album/album-a",
    "https://artisttwo.bandcamp.com/album/album-b",
    "https://broken-url.bandcamp.com/album/nope",
]

results = crawl_list(urls, output_dir=Path("output/"), skip_errors=True)

for url, result in results.items():
    if isinstance(result, Exception):
        print(f"FAILED  {url}: {result}")
    else:
        print(f"OK      {result}")
```

To abort the entire batch on the first failure, pass `skip_errors=False`:
```python
results = crawl_list(urls, skip_errors=False)
```

---

## Pipeline Detail

### Step 1 — Page Metadata

`crawl_album` calls `bandcamp.parse_page(album_url)` to retrieve `band_id`, `artist`, `item_id`, `cover_url_0`, and `title`. If this fails, the crawl is aborted immediately with no container launched.

### Step 2 — Collection Name

Browsertrix requires a collection name that matches `[a-zA-Z0-9_-]`. `crawl.py` builds this from the sanitised title and `item_id` (or the URL subdomain as a fallback), strips any disallowed characters, truncates to 80 characters, and strips any leading or trailing hyphens and underscores:

```
collection_name = re.sub(r"[^a-zA-Z0-9_-]", "", f"{safe_title}_{item_id}")[:80]
collection_name = collection_name.strip("-_")
```

The trailing `.strip("-_")` is important for titles that begin with non-ASCII characters (e.g. Japanese or Chinese). For example, the title `宇宙の謎 / Kentaro Hirugami` sanitises to `宇宙の謎 - Kentaro Hirugami`, and after stripping non-ASCII characters the collection name would be `-KentaroHirugami_550048376` — a leading hyphen that Browsertrix interprets as a malformed CLI flag, causing it to silently fall back to its default `crawl-<timestamp>` name. Stripping leading/trailing hyphens and underscores prevents this.

If the result is an empty string (e.g. a title composed entirely of non-ASCII characters), it falls back to `release_{item_id}`.

### Step 3 — Browsertrix YAML Config

`_build_crawl_config` produces a YAML string with the following structure:

```yaml
seeds:
  - url: https://artist.bandcamp.com/album/title
    scopeType: page
  - url: https://f4.bcbits.com/img/a0737321838_0   # cover image, if available
    scopeType: page

waitUntil: networkidle2

pageLoadTimeout: 90
behaviorTimeout: 1800

limit: 2
depth: 0

generateWACZ: true
combineWACZ: true
```

`depth: 0` prevents Browsertrix from following any links — only the explicitly seeded URLs are archived. The cover image is included as a second seed so it is captured at full resolution inside the WACZ. All timing values come from `config.py`.

The config is written to a `tempfile` and cleaned up in a `finally` block whether the crawl succeeds or fails.

### Step 4 — Container Execution

`_run_container` builds and runs this command:

```
podman run --rm \
  -v /tmp/bc_crawl_XXXX.yaml:/crawls/crawl-config.yaml:ro \
  -v /path/to/output_dir:/crawls \
  -v /path/to/behaviors:/behaviors:ro \
  ghcr.io/webrecorder/browsertrix-crawler:latest \
  crawl \
  --config /crawls/crawl-config.yaml \
  --collection <collection_name> \
  --customBehaviors /behaviors/bandcamp.js
```

Three volumes are mounted:
- The temporary YAML config, read-only
- The output directory (Browsertrix writes its `collections/` tree here)
- The `behaviors/` directory from the project root, read-only — this is where `bandcamp.js` lives

Substitute `podman` with `docker` by setting `CONTAINER_RUNTIME=docker` in `.env`.

Browsertrix writes its output to `{output_dir}/collections/{collection_name}/{collection_name}.wacz`. A fallback path `{output_dir}/collections/{collection_name}.wacz` is also checked, as the nesting varies between Browsertrix versions.

### Step 5 — Retry Logic

If the container exits with a non-zero code or the expected `.wacz` is not found, the crawl is retried up to `CRAWL_MAX_RETRIES` times with linear back-off (`CRAWL_RETRY_DELAY × attempt`). With defaults this gives:

| Attempt | Wait before next retry |
|---|---|
| 1 (first try) | — |
| 2 | 30 s |
| 3 | 60 s |
| 4 (final) | 90 s, then raises |

`KeyboardInterrupt` during a crawl or during a retry wait is re-raised immediately, so Ctrl-C always works.

### Step 6 — Output File Naming

After a successful crawl the `.wacz` is renamed and moved from the nested Browsertrix output tree to the root of `output_dir`:

```
{output_dir}/collections/{collection_name}/{collection_name}.wacz
      →  {output_dir}/{safe_title} [{item_id}].wacz
```

If no `item_id` was found, the subdomain-derived `collection_name` is used as the filename instead. The filename is passed through `truncate_filename` to ensure it respects the archive.org path length limit (configured via `FILENAME_MAX_BYTES` and `FILENAME_TRUNCATION`). A warning is logged if truncation occurs.

The `collections/` working directory is then removed with `shutil.rmtree`.

### Step 7 — Metadata (`update_json=True`)

If both `band_id` and `item_id` were found and `update_json` is not disabled, `metadata.process_archived_wacz` is called with the final `.wacz` path. This writes a release sidecar JSON and marks the release as archived in the artist-level JSON. If either ID is missing, this step is skipped with a warning.

Pass `update_json=False` to skip this entirely — useful for one-off archiving where you don't want to touch the artist tracking files.

---

## Typical Usage Patterns

### Archive a single album
```python
from bandcamp_wacz.crawl import crawl_album

wacz = crawl_album("https://someartist.bandcamp.com/album/some-album")
```

### Archive a batch from a text file
```python
from pathlib import Path
from bandcamp_wacz.crawl import crawl_list

urls = Path("urls.txt").read_text().splitlines()
results = crawl_list(urls, output_dir=Path("output/wacz"))

failed = {url: exc for url, exc in results.items() if isinstance(exc, Exception)}
if failed:
    print(f"{len(failed)} crawl(s) failed:")
    for url, exc in failed.items():
        print(f"  {url}: {exc}")
```

### Quick test run without touching metadata
```python
wacz = crawl_album(
    "https://someartist.bandcamp.com/album/some-album",
    output_dir=Path("/tmp/test_output"),
    update_json=False,
)
```
