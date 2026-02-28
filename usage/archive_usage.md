# `archive.py` — Usage Guide

## Overview

`archive.py` is the top-level orchestrator for the entire pipeline. Every other script in the project is either called by it or exists as a standalone utility for when you need finer-grained control. In day-to-day use, `archive.py` is the only script you need.

It offers three distinct operating modes — **smart**, **quick**, and **dumb** — each making different trade-offs between thoroughness and speed, plus a raw **list mode** for direct crawling without any metadata management.

---

## Operating Modes at a Glance

| Mode | Flag | What it does |
|---|---|---|
| **Smart** | *(default)* | Full pipeline — resolves artist, fetches or updates metadata, archives everything unarchived, uploads |
| **Quick** | `--quick` | Single-release pipeline — checks JSON for changes, skips if already done, crawls and uploads |
| **Dumb** | `--dumb` | Bare crawl only — produces WACZ + sidecar, no JSON interaction whatsoever |
| **List** | `--list` | Crawls every URL in a `.lst` file directly, no artist resolution |

---

## Command-Line Usage

```
python archive.py [--url URL [URL ...] | --list [FILE]] [OPTIONS]
```

`--url` and `--list` are mutually exclusive.

| Flag | Description |
|---|---|
| `--url URL`, `-u URL` | One or more Bandcamp album or artist URLs |
| `--slug SLUG`, `-s SLUG` | One or more Bandcamp artist slugs (e.g. `alyaserpentis`). Expanded to `https://<slug>.bandcamp.com/` before processing. Accepts multiple space-separated slugs |
| `--list [FILE]`, `-l [FILE]` | Archive all URLs in FILE. Defaults to `bandcamp-dump.lst` if no file is specified |
| `--quick` | Run the quick single-release pipeline instead of the full smart pipeline |
| `--dumb` | Bare crawl with no JSON interaction |
| `--output DIR`, `-o DIR` | Output directory for WACZ files. If omitted, a unique subdirectory is created automatically under `WACZ_OUTPUT_DIR` (see [Concurrent Jobs](#concurrent-jobs)) |
| `--no-upload` | Skip uploading to archive.org after archiving |
| `--skip-metadata` | Skip the fetch/update metadata step and archive only what is already queued (`archived=False`) |
| `--one-by-one` | Archive and upload one release at a time. Saves disk space for large discographies |
| `--keep-on-error` | Abort list or batch processing on the first failure instead of skipping and continuing |
| `--filename-truncation STYLE` | Override the `FILENAME_TRUNCATION` setting from `.env` for this run. Options: `end`, `middle`, `hash` |
| `--check-podman` | Verify Podman is installed and the Browsertrix image is available locally (pulls it if not), then exit |
| `--debug`, `-d` | Enable verbose `DEBUG`-level logging |

---

## Smart Pipeline (default)

The most complete mode. Intended for onboarding a new artist or doing a full refresh of a known one.

```bash
# Full pipeline for an artist's entire discography
python archive.py --url https://someartist.bandcamp.com/

# Full pipeline triggered by a specific album URL — artist is resolved automatically
python archive.py --url https://someartist.bandcamp.com/album/some-album

# Multiple URLs from the same artist
python archive.py --url https://someartist.bandcamp.com/album/a \
                       https://someartist.bandcamp.com/album/b

# Using a slug instead of a full URL
python archive.py --slug someartist

# Multiple artists in one command — pipeline runs once per artist
python archive.py --slug artistone artisttwo artistthree
python archive.py --url https://artistone.bandcamp.com/ https://artisttwo.bandcamp.com/

# Archive without uploading
python archive.py --url https://someartist.bandcamp.com/ --no-upload

# Archive only what is already queued, skip metadata refresh
python archive.py --url https://someartist.bandcamp.com/ --skip-metadata

# Archive one release at a time to conserve disk space
python archive.py --url https://someartist.bandcamp.com/ --one-by-one
```

### Smart Pipeline Steps

**Step 1 — Artist resolution**

Each URL is stripped to its artist root (e.g. `https://artist.bandcamp.com/`). The artist page is fetched and `band_id` is extracted. If the root page does not expose a `band_id` (some single-release artists have no dedicated artist page and redirect to an album), the original URL is tried as a fallback.

URLs are grouped by `band_id` before any further processing. If multiple artists are detected, the pipeline runs once per artist group in sequence — each artist gets a full independent pipeline run. A header is printed between artists showing the current artist number and `band_id`.

**Step 2 — Metadata fetch or update**

Depending on whether the artist JSON already exists:

| Condition | Action |
|---|---|
| No artist folder or no JSON | Runs `fetch_metadata.py` (full discography scrape). Auto-resumes from `.partial` if an interrupted run exists |
| Folder exists but JSON is missing | Same as above — treated as an incomplete initial fetch |
| Artist JSON exists | Runs `update_metadata.update_artist` to check for new or changed releases |

Pass `--skip-metadata` to bypass this step entirely and proceed directly to archiving whatever is already queued in the JSON. The artist JSON must exist for this to work.

**Step 3 — Missing WACZ detection**

Scans for releases that are marked `archived=True` in the JSON but have no corresponding WACZ file on disk and have not yet been uploaded. These are releases whose WACZ was lost (e.g. manually deleted, disk failure). For each:
1. `update_metadata.update_release` is called to refresh the release metadata
2. `archived` is reset to `False` so the release re-enters the crawl queue

**Step 4 — Crawl**

All releases with `archived=False` are passed to `crawl_list`. In default mode all releases are crawled before uploading begins. With `--one-by-one`, each release is crawled and uploaded immediately before moving to the next — useful when disk space is tight and holding the entire discography's worth of WACZ files at once is not feasible.

**Step 5 — Upload**

After all crawls complete (or after each crawl in `--one-by-one` mode), finished WACZs are uploaded via `upload.py`. Skipped entirely with `--no-upload`.

---

## Quick Pipeline (`--quick`)

A lighter-weight mode for processing specific album URLs without touching the full discography. Used by `email_watcher.py` for known artists.

```bash
# Check a specific release, crawl only if new or changed
python archive.py --quick --url https://someartist.bandcamp.com/album/some-album

# Multiple specific releases
python archive.py --quick --url https://someartist.bandcamp.com/album/a \
                               https://someartist.bandcamp.com/album/b

# Quick pipeline without uploading
python archive.py --quick --url https://someartist.bandcamp.com/album/a --no-upload
```

### Quick Pipeline Steps (per URL)

1. **Fetch fresh metadata** from Bandcamp for the specific URL
2. **Look up the release** in the artist JSON by `item_id`:
   - **Not found** → add to the JSON with `archived=False`, `uploaded=False`, then crawl
   - **Found, unchanged, already `archived=True` and `uploaded=True`** → skip entirely
   - **Found, unchanged, not fully done** → proceed to crawl (e.g. was archived but upload failed)
   - **Found, changes detected** → call `apply_changes` (records old values in `_history`, resets `archived`/`uploaded`), then crawl
3. **Crawl** the release via `crawl_album`
4. **Upload** unless `--no-upload`

Unlike the smart pipeline, `--quick` never fetches the `/music` grid and never runs a full discography scan. It operates on exactly the URLs provided.

**Note:** If no artist folder exists for the `band_id`, the release is still crawled — a warning is logged but execution continues. This is intentional: `--quick` is designed to be resilient, not gating.

---

## Dumb Pipeline (`--dumb`)

The simplest possible path. Calls `crawl_list` directly with `update_json=False`. No artist resolution, no metadata lookup, no JSON updates. Produces a WACZ and sidecar JSON alongside it, and nothing else.

```bash
# Just crawl — no JSON interaction
python archive.py --dumb --url https://someartist.bandcamp.com/album/some-album

# Crawl to a specific directory
python archive.py --dumb --url https://someartist.bandcamp.com/album/some-album \
                  --output /tmp/test_wacz
```

Useful for: quick tests, archiving one-off releases outside your normal library, or any situation where you deliberately do not want the artist JSON touched.

---

## List Mode (`--list`)

Reads a plain-text URL list file and crawls every URL directly via `crawl_list`, with no artist resolution, no metadata management, and no upload step. Comments (lines starting with `#`) and blank lines are ignored.

```bash
# Archive all URLs in the default bandcamp-dump.lst
python archive.py --list

# Archive all URLs in a specific list file
python archive.py --list artists/Some\ Artist\ \[3774983561\]/bandcamp-dump.lst

# Abort on first failure instead of continuing
python archive.py --list bandcamp-dump.lst --keep-on-error

# Write WACZs to a different directory
python archive.py --list bandcamp-dump.lst --output /mnt/archive/wacz
```

This mode is the equivalent of running `crawl_list` over a file — it is most useful for batch-crawling without any artist JSON involvement.

---

## Environment Check (`--check-podman`)

Verifies the runtime environment before running any crawls.

```bash
python archive.py --check-podman
```

Checks:
1. `podman --version` (or whichever `CONTAINER_RUNTIME` is configured) — exits with an error if not found
2. Whether the `BROWSERTRIX_IMAGE` is already available locally — pulls it if not

Exits with code `0` on success, `1` on any failure. Run this once after first install to ensure everything is in order before attempting a crawl.

---

## `--filename-truncation` Override

Overrides `FILENAME_TRUNCATION` from `.env` for the duration of the run, without editing any config file:

```bash
# Use hash style for this run only
python archive.py --url https://someartist.bandcamp.com/ --filename-truncation hash
```

Options are `end`, `middle`, and `hash`. See [`config.py` usage](config_usage.md) for descriptions of each style.

---

## Decision Flow Summary

```
archive.py --url <URL>
    │
    ├── --dumb      → crawl_list (update_json=False) → done
    ├── --quick     → fetch fresh metadata
    │                 → check artist JSON by item_id
    │                 → crawl if new/changed/incomplete
    │                 → upload (unless --no-upload)
    │
    └── (default)   → resolve artist root + band_id
                      → group by artist, loop per artist:
                      → fetch_metadata.py  (if no JSON)
                        OR update_metadata  (if JSON exists)
                        OR skip            (if --skip-metadata)
                      → detect missing WACZs → re-queue
                      → crawl_list (all archived=False)
                      → upload (unless --no-upload)

archive.py --list <FILE>
    └── read URLs → crawl_list → done (no upload)
```

---

## Concurrent Jobs

Multiple `archive.py` processes can run simultaneously without interfering with each other. When `--output` is not specified, each job automatically creates its own isolated subdirectory under `WACZ_OUTPUT_DIR` rather than writing directly into the shared folder:

```
wacz_output/
├── job_12345_a3f9c12b/       ← process 1
│   ├── Album A [111].wacz
│   └── Album A [111].json
└── job_67890_b4e0d23f/       ← process 2
    ├── Album B [222].wacz
    └── Album B [222].json
```

The subdirectory name encodes the process ID and a short random suffix (`job_{pid}_{8-char hex}`), so it is unique even if the same artist is queued more than once at the same time. Each job's upload step only scans its own subdirectory, so there is no risk of two jobs racing to upload the same file.

The subdirectory is removed automatically when the job finishes, provided all uploads succeeded and no files remain. If uploads failed and files were left behind, the directory is kept for inspection and manual recovery.

If you pass `--output DIR` explicitly, that directory is used as-is with no subdirectory created and no automatic cleanup — the existing behaviour for scripted or manual runs is unchanged.

---

## Exit Codes

| Code | Meaning |
|---|---|
| `0` | All operations succeeded (or nothing to do) |
| `1` | One or more crawls failed, or a required prerequisite was missing |
| `130` | Interrupted by Ctrl-C |

---

## Suggested Improvements

### `--all` flag to archive every known artist
A `--all` flag that iterates over every folder in `ARTISTS_DIR` and runs the full smart pipeline for each would make it trivial to refresh the entire library in one command. Combined with `--no-upload` and a nightly cron job, this would keep all artist JSONs current and have WACZs ready for a separate upload pass. (Make sure to add conditionals, e.g. --all if # of releases < 100).

### `--dry-run` for the smart and quick pipelines
Neither `run_smart_pipeline` nor `run_quick_pipeline` has a dry-run mode. Passing `--dry-run` to `archive.py` currently has no effect beyond what `--check-podman` provides. A proper dry-run that prints what would be fetched, what would be crawled, and what would be uploaded — without running Browsertrix or touching any files — would be very useful before committing to a large batch run.

### Atomic artist JSON writes
The quick pipeline writes the artist JSON directly with `Path.write_text` in two places (when adding a new release and when applying changes). The same `.tmp` + `Path.replace()` pattern used in `fetch_metadata.py` should be applied here for consistency and safety.
