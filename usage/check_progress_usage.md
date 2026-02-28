# `check_progress.py` — Usage Guide

## Overview

`check_progress.py` gives you a quick, colourful overview of how far along the archival pipeline each followed artist is. It reads the artist JSON files in `artists/` and reports, per artist, how many releases have been archived (WACZ produced) and how many have been uploaded to archive.org.

No network requests are made by default — it only reads local files.

It also warns you if any `.json.partial` files are found, which indicate an interrupted `fetch_metadata.py` run that left an artist's metadata incomplete.

---

## Command-Line Usage

```
python check_progress.py [SOURCE] [OPTIONS]
```

Exactly one `SOURCE` can be given. If none is given, an interactive picker is shown.

| Flag | Short | Description |
|---|---|---|
| `--all` | `-a` | Check every artist folder in the `artists/` directory |
| `--path PATH` | `-p` | Check a specific JSON file, artist folder, or the `artists/` root |
| `--id BAND_ID` | `-i` | Look up an artist by their Bandcamp `band_id` |
| *(none)* | | Interactive artist picker — choose from a numbered list |

| Option | Short | Description |
|---|---|---|
| `--summary` | `-s` | One-line-per-artist view instead of full detail |
| `--warnings-only` | `-w` | Only show artists that are not fully complete |
| `--verify-ia` | `-V` | Verify every `ia_identifier` against archive.org (see below) |
| `--delay SECONDS` | | Seconds between archive.org requests (default: `0.5`) |
| `--retries N` | | Network error retries per identifier (default: `3`) |
| `--artists-dir DIR` | | Override the `artists/` path (also read from `ARTISTS_DIR` in `.env`) |

---

## Examples

### Interactive picker (no flags)

```bash
python check_progress.py
```

Scans the `artists/` directory and prints a numbered list. Enter one or more numbers (space- or comma-separated), or `a` to select all. If you select all artists while `--verify-ia` is active, a confirmation prompt appears first (see below).

### Check everything

```bash
python check_progress.py --all
```

Runs through every artist folder and prints full detail for each, followed by an aggregate summary.

### Compact overview

```bash
python check_progress.py --all --summary
```

Prints one line per artist: a `DONE` or `PENDING` badge, the artist name, and the total release count.

### Only show what still needs work

```bash
python check_progress.py --all --warnings-only
python check_progress.py --all --warnings-only --summary
```

Skips complete artists entirely.

### Check a specific artist by band_id

```bash
python check_progress.py --id 3774983561
```

---

## `--verify-ia` — Live archive.org Verification

The `--verify-ia` flag (`-V`) makes one HTTP request to archive.org per `ia_identifier`
found in the artist's JSON — including identifiers stored in `_history` snapshots — and
reports whether each item actually exists there.

```bash
python check_progress.py --id 3774983561 --verify-ia
python check_progress.py --path "artists/Another Artist [3873639194]/" --verify-ia
python check_progress.py --all --verify-ia          # asks for confirmation first
```

### How it works

The `internetarchive` library calls `GET https://archive.org/metadata/{identifier}`.
That endpoint **always returns HTTP 200**, regardless of whether the item exists. The
body either contains full metadata (item exists) or an error payload (item absent).

The library exposes this as `item.exists`:

| `item.exists` | Meaning | Recorded as |
|---|---|---|
| `True` | Item confirmed present on archive.org | ✓ OK |
| `False` | Item confirmed absent on archive.org | ✗ **GHOST** |
| *(exception)* | Network/connectivity failure | ? **NETWORK ERROR** |

Only `False` is a definitive "missing" answer. A network exception is never treated as
missing — it means "we don't know yet."

### Retry logic

Transient network exceptions are retried up to `--retries` times (default: 3) with
**exponential back-off** (`delay × 2^attempt`). If the item is still unreachable after
all retries, it is recorded as `NETWORK ERROR` — not as a ghost. A `NETWORK ERROR` means
"re-run to try again", not "this item is gone."

`item.exists = False` (ghost) is **never retried** — it is a definitive answer from the
IA metadata API, not a transient failure.

### Controlling speed and politeness

```bash
# Faster — less polite
python check_progress.py --id 3774983561 --verify-ia --delay 0.1

# Slower — very polite, or for a shaky connection
python check_progress.py --id 3774983561 --verify-ia --delay 2

# More retries for an unreliable network
python check_progress.py --id 3774983561 --verify-ia --retries 5

# No retries (fail immediately on any network error)
python check_progress.py --id 3774983561 --verify-ia --retries 0
```

### ETA

The progress display shows a live ETA based on the number of remaining identifiers and
the configured delay:

```
  Verifying 87 identifiers on archive.org for Another Artist
  ETA: 43s  (0.5s between requests, up to 3 retries on network error)
  [████████████░░░░░░░░░░░░░░░░]  22/87 ETA 33s  ✓  wacz-3873639194-1268523331-...
```

The ETA re-estimates after each request based on elapsed wall time, so it stays accurate
even when retries add extra wait time.

### What it reports

- **✓ N/M confirmed** — N identifiers exist on archive.org.
- **NETWORK ERROR** — one or more identifiers could not be reached after all retries.
  These are *not* confirmed missing. Re-run to retry them.
- **HISTORY MISSING** — a `_history` snapshot's identifier is confirmed absent.
  Reported as a warning only; history entries are never modified.
- **NOT FOUND** — a current release is marked `uploaded=True` but its identifier is
  confirmed absent. These are the actionable "ghost" items.

### Fixing ghost items

For each `NOT FOUND` release you are prompted one-by-one:

```
  Fix missing items?
  Resetting uploaded=False and ia_identifier=null will re-queue these releases
  so upload.py picks them up on the next run.

  wacz-3774983561-3853844384-20260115
  Some Album Title
    Reset this item? [y/N]
```

Answering `y` sets `uploaded=False`, `uploaded_at=null`, `ia_identifier=null` and
writes the artist JSON atomically (via `.tmp` swap). After fixing:

```bash
python upload.py wacz_output/
# or, for a full re-run:
python archive.py --url https://artist.bandcamp.com/ --skip-metadata
```

### Warning when combined with `--all` or selecting all in the picker

Running across a large library can mean hundreds or thousands of requests. Whether you
pass `--all` explicitly or select everything in the interactive picker while `--verify-ia`
is active, a confirmation screen is shown first:

```
   WARNING

  --verify-ia with --all will make 1439 requests to archive.org,
  one per identifier across all 7 artists.

  Estimated time: 12m 00s  (0.5s between requests, up to 3 retries on network error)

  Tip: use --id BAND_ID or --path PATH to verify a single artist instead.

  Continue? [y/N]
```

### Keyboard interrupt handling

Pressing **Ctrl-C** at any point during verification is handled gracefully:

- The current identifier's progress is printed.
- Remaining unverified identifiers are recorded as `NETWORK ERROR` (not ghost).
- A summary shows how many artists were completed and how many were skipped.
- Any fix prompts already answered are written to disk before the script exits.
- Exit code `130` is returned (standard for Ctrl-C).

```
  ⚠  Verification interrupted after 24/87 identifiers.

  ⚠  Interrupted. 1 artist verified, 6 skipped.
  Re-run to resume from where you left off.
```

### Requirements

```bash
pip install internetarchive
```

No archive.org credentials are needed — existence checks are unauthenticated.

---

## Output

### Complete artist

```
  Artist
  ────────────────────────────────
   COMPLETE   All releases are archived and uploaded.
  50 releases total.
```

### Incomplete artist

```
  Some Artist
  ────────────────────────────────
  [██████████████░░░░░░░░░░░░░░]  archive   51%  23 not archived  (24/47)
  [██████████████░░░░░░░░░░░░░░]  upload    51%  23 not uploaded  (24/47)

  → run archive.py to queue unarchived releases.
```

### Summary mode (`--summary`)

```
   DONE      Artist  (50 releases)
   PENDING   23 not archived · 23 not uploaded   Some Artist  (47 releases)
```

---

## Partial File Warnings

If `fetch_metadata.py` was interrupted, a `.json.partial` file is left behind.
`check_progress.py` warns about these whenever they are found:

```
   PARTIAL    Some Artist [876012970]
   Some Artist [876012970].json.partial — metadata fetch was interrupted.
  Re-run fetch_metadata.py with the artist URL to finish.
```

If only a `.json.partial` exists and no complete `.json` is present, the artist is skipped entirely.

---

## Where It Looks for Artists

By default, `check_progress.py` reads `ARTISTS_DIR` from `.env`. If that is not set, it falls back to an `artists/` directory next to the script. Override anytime with `--artists-dir`.

---

## Exit Codes

| Code | Meaning |
|---|---|
| `0` | Completed normally |
| `1` | A required path was not found, could not be read, or `internetarchive` is missing |
| `130` | Interrupted by Ctrl-C |
