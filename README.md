> [!CAUTION]
>
> **AI-generated code warning**
>
> This project was written entirely by [Claude](https://claude.ai). I do not condone the use of AI for coding, or for much else really, but I believe archival is important enough to warrant a degree of hypocrisy on my part. I am not a coder. Please read through the code and satisfy yourself that it does what it says before running it, especially anything that touches your filesystem or makes outbound network requests. Really keep in mind that everything outside of this block (documentation, scripts) is most likely written by AI, so don't believe it's doing exactly what it says is doing, this project was tested by me, on my machine, and my machine only. If you are a real coder, find anything that could be better and want to help, feel free to send a PR and I'll do my best to understand it.

---

# Bandcamp WACZ Archiver

A pipeline for archiving Bandcamp releases to the [Internet Archive](https://archive.org) as [WACZ](https://specs.webrecorder.net/wacz/1.1.1/) files — self-contained web archives that include the full Bandcamp page, all audio at streaming quality, and high-resolution cover art. Archives can be replayed offline using [ReplayWeb.page](https://replayweb.page).

The pipeline can be triggered manually, run as a batch job over an artist's entire discography, or run automatically in response to Bandcamp new-release notification emails.

---

## Why WACZ?

A WACZ file is a complete, self-describing snapshot of a web page and all its resources at a moment in time. For a Bandcamp release this means: the full page HTML, all artwork, and every audio stream captured as a proper HTTP 200 response — not partial chunks. The result is a file that can be handed to ReplayWeb.page years from now and played back exactly as it was, without any dependency on Bandcamp still being online.

---

## How It Works

At the core of each archive is [Browsertrix Crawler](https://github.com/webrecorder/browsertrix-crawler), run inside a Podman (or Docker) container. A custom browser behavior script (`behaviors/bandcamp.js`) reads audio stream URLs directly from the page's embedded JSON and force-fetches each one as a complete file, working around Bandcamp's normal HTTP 206 chunked streaming which would only capture partial audio.

The rest of the project is the scaffolding around that crawl: fetching and maintaining metadata, deciding what needs to be crawled, naming files consistently, and pushing finished archives to the Internet Archive.

---

## Pipeline Overview

```
fetch_metadata.py          ← run once per artist to build the artist JSON
        ↓
archive.py                 ← orchestrates everything below
  ├── update_metadata.py   ← checks for new/changed releases
  ├── crawl.py             ← runs Browsertrix via Podman
  ├── metadata.py          ← writes sidecar JSON, marks archived
  └── upload.py            ← uploads WACZ + JSON to archive.org
        ↓
extract.py                 ← optional: extract MP3s + covers from a WACZ
```

The `email_watcher.py` daemon sits alongside this pipeline and triggers `archive.py` automatically whenever a Bandcamp new-release notification arrives in a monitored inbox.

---

## Requirements

- Python 3.10+
- [Podman](https://podman.io) (or Docker — set `CONTAINER_RUNTIME=docker` in `.env`)
- The Browsertrix container image (pulled automatically on first run via `--check-podman`)
- An [archive.org](https://archive.org) account with S3 API keys (only required for uploading)

### Python dependencies

```bash
pip install -r requirements.txt
```

---

## Installation

```bash
git clone https://github.com/Bandcamp-Web-Archive/bandcamp-wacz-archiver.git
cd bandcamp-wacz-archiver

pip install -r requirements.txt

cp .env.example .env
# Edit .env and fill in your credentials
```

Then verify your environment:

```bash
python archive.py --check-podman
```

This confirms Podman is installed and pulls the Browsertrix image if it is not already available locally.

---

## Configuration

All settings are read from a `.env` file in the project root. Copy `.env.example` to `.env` and edit it. The only values that are strictly required to get started are the archive.org keys (only if you intend to upload) and the email credentials (only if you want the automatic watcher).

For a full reference of every setting see **[`usage/config_usage.md`](usage/config_usage.md)**.

Key settings:

| Setting | Default | Notes |
|---|---|---|
| `CONTAINER_RUNTIME` | `podman` | Set to `docker` if needed |
| `WACZ_OUTPUT_DIR` | `wacz_output` | Where finished WACZs are written |
| `CRAWL_BEHAVIOR_TIMEOUT` | `1800` | Must be long enough for your largest expected album |
| `BC_REQUEST_DELAY` | `1000-3000` | Random delay between Bandcamp requests (ms). Be polite |
| `IA_ACCESS_KEY` / `IA_SECRET_KEY` | — | Required for upload. Get from [archive.org/account/s3.php](https://archive.org/account/s3.php) |
| `IA_COLLECTION` | `opensource_media` | Use `test_collection` while testing — items auto-delete after ~30 days |

---

## Quick Start

### Archive a single album

```bash
python archive.py --url https://someartist.bandcamp.com/album/some-album
```

This runs the full pipeline: fetches metadata, crawls the page, writes a WACZ, and uploads to archive.org.

### Archive an artist's entire discography

```bash
python archive.py --url https://someartist.bandcamp.com/
```

Discovers every release on the artist's page, fetches metadata for all of them, and archives any that have not yet been archived. For large discographies, `--one-by-one` uploads each release immediately after crawling it to keep disk usage low.

### Use a slug instead of a full URL

```bash
python archive.py --slug someartist
```

Expanded to `https://someartist.bandcamp.com/` automatically. Accepts multiple slugs to archive several artists in one command:

```bash
python archive.py --slug artistone artisttwo artistthree --one-by-one
```

### Archive multiple artists in one command

```bash
python archive.py --url https://artistone.bandcamp.com/ https://artisttwo.bandcamp.com/
```

The smart pipeline runs once per artist in sequence. A progress header is printed between artists.

### Skip uploading (crawl only)

```bash
python archive.py --url https://someartist.bandcamp.com/ --no-upload
```

### Just crawl, touch nothing else

```bash
python archive.py --dumb --url https://someartist.bandcamp.com/album/some-album
```

No metadata files read or written. Just produces a WACZ.

### Extract audio from an existing WACZ

```bash
python bandcamp_wacz/extract.py wacz_output/Some\ Album\ \[12345\].wacz --output ~/Music/
```

Pulls MP3s and cover art out of the WACZ without re-downloading anything, and applies ID3 tags from the sidecar JSON.

---

## File Layout

```
bandcamp-wacz-archiver/
├── archive.py               ← main entry point
├── fetch_metadata.py        ← onboard a new artist
├── update_metadata.py       ← keep artist JSONs current
├── upload.py                ← upload WACZs to archive.org
│
├── bandcamp_wacz/
│   ├── bandcamp.py          ← Bandcamp HTTP fetcher and page parser
│   ├── config.py            ← all settings, loaded from .env
│   ├── crawl.py             ← Browsertrix container orchestration
│   ├── email_watcher.py     ← IMAP daemon for automatic crawl triggering
│   ├── extract.py           ← extract audio/art from WACZ files
│   └── metadata.py          ← post-crawl sidecar JSON and artist JSON updates
│
├── behaviors/
│   └── bandcamp.js          ← custom Browsertrix behavior for audio capture
│
├── artists/                 ← created on first run
│   └── Artist Name [band_id]/
│       ├── Artist Name [band_id].json
│       └── bandcamp-dump.lst
│
├── wacz_output/             ← created on first run
│   └── job_{pid}_{hex}/     ← per-job subdirectory (auto-created, auto-cleaned)
│       ├── Album Title [item_id].wacz
│       └── Album Title [item_id].json
│
├── .env                     ← your local config (never committed)
└── .env.example             ← template
```

---

## Artist JSON

The central data structure of the project. One file per artist, living at `artists/{Artist Name} [{band_id}]/`. It tracks every known release and their pipeline state:

```json
{
  "Some Artist": [
    {
      "title": "Some Album",
      "url": "https://someartist.bandcamp.com/album/some-album",
      "item_id": 3853844384,
      "band_id": 3774983561,
      "archived": true,
      "archived_at": "2026-01-15T12:34:56+00:00",
      "uploaded": true,
      "uploaded_at": "2026-01-15T13:00:00+00:00",
      "ia_identifier": "wacz-3774983561-3853844384-20260115",
      "trackinfo": [ ... ]
    }
  ],
  "_band_id": 3774983561
}
```

`archived` and `uploaded` are the two state flags the pipeline uses to decide what work remains. `ia_identifier` is the archive.org item identifier for that release. When `update_metadata.py` detects a change to a release, the old values are saved to a `_history` list before being overwritten, and both flags are reset to `False` so the release is re-queued.

---

## Automatic Mode: Email Watcher

If you subscribe to Bandcamp artist notifications, the email watcher can monitor your inbox and trigger the pipeline automatically whenever a new release arrives. It uses IMAP IDLE (server push, not polling) and reconnects automatically after any network interruption.

```bash
python bandcamp_wacz/email_watcher.py
```

For known artists it runs the quick pipeline (`--quick`). For artists not yet in `artists/` it runs the full pipeline including `fetch_metadata.py`. Successfully processed emails are moved to Trash; failed ones are left in the inbox for retry on the next cycle.

For Gmail, use an [App Password](https://support.google.com/accounts/answer/185833) rather than your account password.

See **[`usage/email_watcher_usage.md`](usage/email_watcher_usage.md)** for setup and all options, including how to run it as a persistent systemd service.

---

## Script Reference

Each script has a detailed usage document in `usage/`:

| Script | Description | Usage doc |
|---|---|---|
| `archive.py` | Main orchestrator — smart, quick, dumb, and list modes | [`usage/archive_usage.md`](usage/archive_usage.md) |
| `fetch_metadata.py` | Scrape and save full metadata for a new artist | [`usage/fetch_metadata_usage.md`](usage/fetch_metadata_usage.md) |
| `update_metadata.py` | Check for new or changed releases and update artist JSONs | [`usage/update_metadata_usage.md`](usage/update_metadata_usage.md) |
| `upload.py` | Upload WACZ + sidecar JSON pairs to archive.org | [`usage/upload_usage.md`](usage/upload_usage.md) |
| `bandcamp_wacz/bandcamp.py` | Bandcamp HTTP fetcher, page parser, filename utilities | [`usage/bandcamp_usage.md`](usage/bandcamp_usage.md) |
| `bandcamp_wacz/config.py` | All configuration constants loaded from `.env` | [`usage/config_usage.md`](usage/config_usage.md) |
| `bandcamp_wacz/crawl.py` | Browsertrix container orchestration and WACZ output management | [`usage/crawl_usage.md`](usage/crawl_usage.md) |
| `bandcamp_wacz/email_watcher.py` | IMAP IDLE daemon for automatic pipeline triggering | [`usage/email_watcher_usage.md`](usage/email_watcher_usage.md) |
| `bandcamp_wacz/extract.py` | Extract MP3s and cover art from WACZ files | [`usage/extract_usage.md`](usage/extract_usage.md) |
| `bandcamp_wacz/metadata.py` | Post-crawl sidecar JSON writing and artist JSON updates | [`usage/metadata_usage.md`](usage/metadata_usage.md) |
| `check_progress.py` | Inspect archival and upload progress; verify identifiers live on archive.org | [`usage/check_progress_usage.md`](usage/check_progress_usage.md) |

---

## The Browsertrix Behavior

`behaviors/bandcamp.js` is the custom browser behavior that runs inside the Browsertrix container when a Bandcamp page is crawled. It reads audio stream URLs from the page's embedded `data-tralbum` JSON and fetches each one using `fetch({ mode: 'no-cors' })`, which forces the browser to download the complete MP3 as a single HTTP 200 response rather than the partial HTTP 206 chunks that Bandcamp's normal player uses. Browsertrix captures these full responses in the WACZ.

The play button is clicked at the end of the behavior purely to capture the player UI in a playing state for the final page snapshot.

---

## Common Workflows

### Check progress across all followed artists

```bash
python check_progress.py --all
```

Prints archived and uploaded counts for every artist, with a progress bar and an aggregate summary. Use `--summary` for a compact one-liner per artist and `--warnings-only` to skip complete artists.

### Verify identifiers actually exist on archive.org

```bash
python check_progress.py --id 3774983561 --verify-ia
```

Checks every `ia_identifier` in the JSON (including `_history` snapshots) against archive.org. `item.exists = False` is a confirmed ghost; network exceptions are retried separately and never marked as missing. Ghost items can be reset in-place so `upload.py` re-queues them.

```bash
# Tune speed and resilience
python check_progress.py --id 3774983561 --verify-ia --delay 1 --retries 5

# Check everything — confirmation required (ETA shown before proceeding)
python check_progress.py --all --verify-ia
```

Requires `pip install internetarchive`. No credentials needed. See [`usage/check_progress_usage.md`](usage/check_progress_usage.md) for full details.

---

### Check progress across all followed artists

```bash
python check_progress.py --all
```

Prints archived and uploaded counts for every artist, with a progress bar and an aggregate summary at the end.

```bash
python check_progress.py --all --summary             # one-line overview per artist
python check_progress.py --all --warnings-only       # only show incomplete artists
python check_progress.py --id 3774983561 --verify-ia # confirm identifiers exist on archive.org
python check_progress.py --all --verify-ia           # verify all artists (requires confirmation)
```

### Onboard a new artist and archive their full discography

```bash
python archive.py --url https://newartist.bandcamp.com/
```

### Check a known artist for new releases and archive them

```bash
python archive.py --url https://someartist.bandcamp.com/
```

`update_metadata.py` is called automatically, new releases are detected, and anything unarchived is crawled.

### Archive a large discography one release at a time (low disk usage)

```bash
python archive.py --url https://someartist.bandcamp.com/ --one-by-one
```

### Crawl without uploading, then upload separately later

```bash
python archive.py --url https://someartist.bandcamp.com/ --no-upload
# ... review the WACZs in wacz_output/job_*/ ...
python upload.py wacz_output/job_<pid>_<hex>/
```

### Re-queue a release whose WACZ was deleted

```bash
python update_metadata.py https://someartist.bandcamp.com/album/some-album --release
python archive.py --quick --url https://someartist.bandcamp.com/album/some-album
```

### Extract audio from a WACZ into a music library

```bash
python bandcamp_wacz/extract.py wacz_output/ --output ~/Music/Bandcamp/
```

### Preview what would be uploaded without uploading

```bash
python upload.py wacz_output/ --dry-run
```

---

## archive.org Item Format

Each release is uploaded as its own archive.org item. The item identifier follows the format:

```
wacz-{band_id}-{item_id}-{YYYYMMDD}
```

For example: `wacz-3774983561-3853844384-20260115`

Each item contains two files: the `.wacz` archive and a `.json` sidecar with the release metadata. The `band_id` and `item_id` are also embedded directly into `datapackage.json` inside the WACZ itself, making each archive self-describing.

---

## Limitations and Known Issues

- **Audio quality**: Bandcamp's streaming API serves MP3 at 128 kbps. This is the quality captured in the WACZ — the same quality available to any non-paying listener. Purchased downloads at higher quality are not captured.
- **Pre-orders**: Pre-order pages are archived as-is. Tracks that are locked before release will not have audio in the WACZ. `update_metadata.py` detects when `is_preorder` changes to `False` and re-queues the release automatically.
- **Custom domains**: Artists who use a custom domain instead of `artist.bandcamp.com` may not be detected or parsed correctly in all cases.
- **Rate limiting**: The configurable request delay (`BC_REQUEST_DELAY`) helps avoid triggering Bandcamp's rate limiter. If you see frequent 429 errors, increase the delay.

---

## Replaying Archives

WACZ files can be replayed using [ReplayWeb.page](https://replayweb.page):

1. Go to [replayweb.page](https://replayweb.page)
2. Click **Load Archive** and select your `.wacz` file
3. Navigate to the Bandcamp URL stored in the archive

Audio playback works because the full MP3 responses are stored inside the WACZ — no internet connection required after loading the file.
