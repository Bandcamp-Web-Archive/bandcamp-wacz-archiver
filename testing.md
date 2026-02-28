# Testing Checklist

Everything that should be verified before making the repository public. Work through these in order — each section builds on the one before it. A few tests require a real Bandcamp URL and a real archive.org account; use `IA_COLLECTION=test_collection` throughout so nothing gets permanently published until you are ready.

---

## 0. Before You Start

- [x] `.env` is in `.gitignore` and does **not** appear in `git status`
- [x] `.env.example` contains no real credentials — only placeholder strings like `your_access_key_here`
- [x] `wacz_output/` is in `.gitignore` and contains no files
- [x] `artists/` is in `.gitignore` (or is empty) — no personal data in the repo
- [x] `git log --all --full-history -- .env` returns nothing (the file was never committed, even in an earlier commit)
- [x] Run `grep -r "IA_ACCESS_KEY\|IA_SECRET_KEY\|EMAIL_PASSWORD" --include="*.py" .` — result should be empty (no hardcoded credentials anywhere in source)

---

## 1. Environment

### 1.1 Python dependencies

```bash
python3 -c "import requests, bs4, demjson3, dotenv, internetarchive, mutagen, lxml; print('OK')"
```
- [x] Prints `OK` with no import errors

### 1.2 Container runtime

```bash
python archive.py --check-podman
```
- [x] Reports the Podman (or Docker) version correctly
- [x] Either confirms the Browsertrix image is already present, or pulls it without error
- [x] Exits with code 0

### 1.3 Config loading

```bash
python -c "from bandcamp_wacz.config import *; print('band_id test:', BC_MAX_RETRIES, WACZ_OUTPUT_DIR)"
```
- [x] Prints values from your `.env`, not the defaults, confirming `.env` is being loaded

---

## 2. `bandcamp.py` — Page Fetcher

Use a real, publicly accessible album URL for these.

### 2.1 Standard subdomain

```bash
python3 -c "
from bandcamp_wacz.bandcamp import parse_page
r = parse_page('https://wtc-communications.bandcamp.com/album/100-positive-feedback')
print(r)
assert r['band_id'] is not None, 'band_id missing'
assert r['item_id'] is not None, 'item_id missing'
assert r['artist'] not in ('Unknown Artist', None), 'artist not found'
assert r['cover_url_0'] is not None, 'cover URL missing'
print('PASS')
"
```
- [x] Returns a dict with all five fields populated
- [x] `cover_url_0` ends with `_0` and contains no file extension

### 2.2 Custom domain (Bucketheadland regression)

```bash
python3 -c "
from bandcamp_wacz.bandcamp import parse_page
r = parse_page('https://music.bucketheadpikes.com/album/live-from-bucketstein-manor')
assert r['band_id'] is not None, 'band_id missing on custom domain'
assert r['item_id'] is not None, 'item_id missing on custom domain'
print('PASS:', r['artist'], '-', r['title'])
"
```
- [x] Parses correctly — custom domain is not treated differently from `.bandcamp.com`

### 2.3 `create_safe_filename`

```bash
python3 -c "
from bandcamp_wacz.bandcamp import create_safe_filename
cases = [
    ('Hello World', 'Hello World'),
    ('My Album: Special | Edition', 'My Album- Special - Edition'),
    ('...Hidden', 'Hidden'),
    ('trail...', 'trail'),
    ('a//b\\\\c', 'a-b-c'),
]
for inp, expected in cases:
    result = create_safe_filename(inp)
    assert result == expected, f'{inp!r} → {result!r}, expected {expected!r}'
print('PASS')
"
```
- [x] All cases pass

### 2.4 `truncate_filename` — archive.org byte limit

```bash
python3 -c "
from bandcamp_wacz.bandcamp import truncate_filename
# Title that is fine as-is
r = truncate_filename('Short', ' [123].wacz', 255, 'end')
assert r == 'Short [123].wacz', r

# Title that needs truncating
long = 'A' * 300
r = truncate_filename(long, ' [123].wacz', 60, 'end')
assert len(r.encode('utf-8')) <= 60, f'Too long: {len(r.encode())} bytes'

# Middle style
r = truncate_filename('Start and then some long bit and then End', ' [123].wacz', 40, 'middle')
assert '…' in r, 'No ellipsis in middle truncation'
assert len(r.encode('utf-8')) <= 40

# Hash style
r = truncate_filename(long, ' [123].wacz', 60, 'hash')
assert len(r.encode('utf-8')) <= 60
print('PASS')
"
```
- [x] All three truncation styles keep output within `max_bytes`

---

## 3. `fetch_metadata.py` — Initial Metadata Fetch

Use a small artist (few releases) to keep test time reasonable.

### 3.1 Single album URL

```bash
python fetch_metadata.py https://islandgirlvybz.bandcamp.com/album/fright-night-special --debug
```
- [x] Creates `artists/` directory
- [x] Creates `artists/{Artist} [{band_id}]/` folder with correct name format
- [x] JSON file contains the album entry with `trackinfo`, `art_id`, `item_id`, `band_id`
- [x] `bandcamp-dump.lst` is written with the URL
- [x] No `.partial` file left behind after successful completion

### 3.2 Full discography via artist page

Use an artist with a small, known catalogue.

```bash
python fetch_metadata.py https://islandgirlvybz.bandcamp.com/ --debug
```
- [x] Discovers all releases from the `/music` grid
- [x] Each release in the JSON has `trackinfo` populated (individual track pages were fetched)
- [x] `archived` is `false` for all entries
- [x] `_band_id` is present at the top level of the JSON

### 3.3 Interruption and resume

```bash
# Start a fetch of an artist with 5+ releases
python fetch_metadata.py https://wtc-communications.bandcamp.com/music &
PID=$!
sleep 15
kill $PID

# Confirm partial file exists
ls artists/*/  *.partial
```
- [x] A `.partial` file exists after the kill
- [x] Re-running the same command resumes: "Resuming from partial file — N release(s) already fetched" is printed
- [x] Final JSON contains all releases, no duplicates
- [x] `.partial` file is deleted after successful completion

### 3.4 `--band-id` override

Find an artist where auto-detection fails or use any artist to test the flag is respected:

```bash
python fetch_metadata.py https://someartist.bandcamp.com/ --band-id 9999999999 --debug
```
- [ ] Log confirms `band_id: 9999999999 (from --band-id)`
- [ ] Folder is created with the overridden `band_id` in its name

---

## 4. `update_metadata.py` — Metadata Updates

Requires an existing artist JSON from section 3.

### 4.1 No changes (stable artist)

```bash
python update_metadata.py https://someartist.bandcamp.com/ --dry-run
```
- [x] Reports `0 new, 0 updated` (or only shows new releases if there genuinely are some)
- [x] `--dry-run` writes nothing to disk — verify JSON `mtime` is unchanged

### 4.2 Simulated change detection

```bash
# Manually corrupt a field in the artist JSON
python3 -c "
import json
from pathlib import Path
p = next(Path('artists').glob('*/*.json'))
data = json.loads(p.read_text())
key = next(k for k in data if not k.startswith('_'))
data[key][0]['title'] = 'FAKE TITLE'
p.write_text(json.dumps(data, indent=4))
print('Corrupted:', p)
"

python update_metadata.py https://someartist.bandcamp.com/ --dry-run
```
- [x] `--dry-run` reports `Would update 'Real Title': title`
- [x] Run without `--dry-run` — `_history` entry appears in the JSON with the old title
- [x] `archived` and `uploaded` are reset to `false` after the change

### 4.3 `--release` mode

```bash
python update_metadata.py https://someartist.bandcamp.com/album/some-album --release --dry-run
```
- [x] Only the one release is checked — no `/music` grid fetch logged
- [x] Correctly identifies whether it is new or existing

### 4.4 Multi-artist deduplication

```bash
python update_metadata.py \
  https://artist-a.bandcamp.com/album/x \
  https://artist-a.bandcamp.com/album/y
```
- [x] Only **one** update pass runs for artist-a, not two

---

## 5. `crawl.py` / `archive.py` — Crawling

**Important:** These tests run Browsertrix and produce real WACZ files. Keep them small.

### 5.1 Environment check first

```bash
python archive.py --check-podman
```
- [x] Passes cleanly before any crawl test

### 5.2 Dumb mode — bare crawl

```bash
python archive.py --dumb --url https://someartist.bandcamp.com/album/some-album --no-upload
```
- [x] A `wacz_output/job_<pid>_<hex>/` subdirectory was created and contains `<Title> [item_id].wacz`
- [x] WACZ contains `datapackage.json` — `python3 -c "import zipfile; print(zipfile.ZipFile('wacz_output/job_*/...wacz').namelist())"`
- [x] No artist JSON was created or modified — `ls artists/` is empty or unchanged
- [x] The job subdirectory is removed automatically after the process exits (it is empty after `--no-upload` only if files were cleaned up; with `--dumb --no-upload` the WACZ remains and the dir is kept)

### 5.3 Smart mode — new artist

Use a small artist (1–3 releases) and `IA_COLLECTION=test_collection`:

```bash
python archive.py --url https://smallartist.bandcamp.com/ --no-upload
```
- [x] `fetch_metadata.py` ran (log: "No artist folder found - running fetch_metadata")
- [x] A `wacz_output/job_<pid>_<hex>/` subdirectory was created containing the WACZ file(s)
- [x] `datapackage.json` inside the WACZ contains `bandcamp_band_id`, `bandcamp_item_id`, `bandcamp_ia_identifier`
- [x] Artist JSON shows `archived: true` and `archived_at` timestamp for crawled releases
- [x] Sidecar `.json` exists alongside each `.wacz` in the job subdirectory
- [x] Job subdirectory is removed automatically after upload (empty on success)

### 5.4 Smart mode — known artist (update path)

Run the same command again immediately after 5.3:

```bash
python archive.py --url https://smallartist.bandcamp.com/ --no-upload
```
- [x] Log shows "Artist folder found — running update_metadata" (not fetch_metadata)
- [x] Log shows "Nothing to archive — all releases are already up to date" (no second crawl)

### 5.5 Quick mode

```bash
python archive.py --quick --url https://smallartist.bandcamp.com/album/some-album --no-upload
```
- [x] Does not fetch the `/music` grid (no "Discovering releases" in log)
- [x] If already `archived=true` and `uploaded=true`: skips with "already archived and uploaded"
- [x] If `archived=false`: crawls the release

### 5.6 `--one-by-one` mode

Use an artist with 2+ unarchived releases:

```bash
python archive.py --url https://smallartist.bandcamp.com/ --one-by-one --no-upload
```
- [x] Log alternates: crawl → upload → crawl → upload (rather than all crawls then all uploads)
- [x] At no point do more than one WACZ file exist simultaneously in the job subdirectory (verify with a second terminal watching `ls -lh wacz_output/job_*/`)

### 5.7 `--list` mode

```bash
python archive.py --list artists/Small\ Artist\ \[12345\]/bandcamp-dump.lst --no-upload
```
- [ ] All URLs in the file are crawled
- [ ] Lines starting with `#` and blank lines are skipped

### 5.8 Crawl retry behaviour

Temporarily set `CRAWL_MAX_RETRIES=2` and `CRAWL_RETRY_DELAY=2` in `.env`, then pass a URL that will fail (e.g. a nonexistent album path):

```bash
python archive.py --dumb --url https://someartist.bandcamp.com/album/this-does-not-exist
```
- [ ] Logs show retry attempts with increasing wait times
- [ ] Exits cleanly with a non-zero code after retries are exhausted, without hanging

### 5.9 Keyboard interrupt during crawl

```bash
python archive.py --url https://smallartist.bandcamp.com/ --no-upload &
PID=$!
sleep 10
kill -INT $PID
```
- [x] Process exits cleanly
- [x] No `.wacz.tmp` temp files left in the job subdirectory (`wacz_output/job_*/`)
- [x] No orphaned container running: `podman ps` (or `docker ps`) should be empty

---

## 6. `metadata.py` — Post-Crawl Metadata

These are exercised automatically by the crawl tests above, but verify explicitly:

### 6.1 WACZ embed

```bash
python3 -c "
import zipfile, json
wacz = next(__import__('pathlib').Path('wacz_output').rglob('*.wacz'))
pkg = json.loads(zipfile.ZipFile(wacz).read('datapackage.json'))
assert 'bandcamp_band_id' in pkg
assert 'bandcamp_item_id' in pkg
assert 'bandcamp_ia_identifier' in pkg
print('PASS:', pkg['bandcamp_ia_identifier'])
"
```
- [x] All three fields present with correct values

### 6.2 Sidecar JSON

```bash
python3 -c "
import json
from pathlib import Path
for p in Path('wacz_output').rglob('*.json'):
    data = json.loads(p.read_text())
    assert 'ia_identifier' in data, f'Missing ia_identifier in {p.name}'
    assert 'band_id' in data
    assert 'trackinfo' in data
    print('PASS:', p.name)
"
```
- [x] Each `.json` sidecar has `ia_identifier`, `band_id`, and `trackinfo`

### 6.3 Artist JSON state

```bash
python3 -c "
import json
from pathlib import Path
for p in Path('artists').glob('*/*.json'):
    data = json.loads(p.read_text())
    key = next(k for k in data if not k.startswith('_'))
    for r in data[key]:
        if r.get('archived'):
            assert 'archived_at' in r, f'archived_at missing for {r[\"title\"]}'
    print('PASS:', p.name)
"
```
- [x] Every release with `archived: true` also has `archived_at`

---

## 7. `extract.py` — Audio Extraction

Requires a WACZ from section 5 and its sidecar JSON.

### 7.1 Basic extraction

```bash
python bandcamp_wacz/extract.py wacz_output/Some\ Album\ \[12345\].wacz --output /tmp/extract_test/
```
- [ ] Output folder `Some Album [12345]/` is created under `/tmp/extract_test/`
- [ ] `cover.jpg` exists and is a valid JPEG: `file /tmp/extract_test/*/cover.jpg`
- [ ] MP3 files are present, numbered correctly (`01 - ...`, `02 - ...`)
- [ ] MP3 filenames contain `[track_id]`

### 7.2 ID3 tags

```bash
python3 -c "
from mutagen.mp3 import MP3
from mutagen.id3 import ID3
from pathlib import Path
mp3 = next(Path('/tmp/extract_test').rglob('*.mp3'))
tags = ID3(mp3)
assert 'TIT2' in tags, 'Missing title tag'
assert 'TPE1' in tags, 'Missing artist tag'
assert 'TALB' in tags, 'Missing album tag'
assert 'TRCK' in tags, 'Missing track number tag'
print('PASS:', tags['TIT2'], '-', tags['TALB'])
"
```
- [ ] Title, artist, album, and track number tags are all present and correct

### 7.3 Whole directory processing

```bash
python bandcamp_wacz/extract.py wacz_output/ --output /tmp/extract_test/
```
- [ ] All WACZs in the directory are processed
- [ ] Summary line shows correct succeeded/failed counts

### 7.4 `--track-covers`

Use a release known to have per-track artwork:

```bash
python bandcamp_wacz/extract.py some_release.wacz --output /tmp/extract_test/ --track-covers
```
- [ ] `01_cover.jpg` etc. appear only where track art differs from the album cover
- [ ] No duplicate covers saved (hash deduplication working)

### 7.5 Missing metadata fallback (`--ask`)

Remove the sidecar JSON temporarily:

```bash
mv wacz_output/Some\ Album\ \[12345\].json /tmp/
python bandcamp_wacz/extract.py wacz_output/Some\ Album\ \[12345\].wacz --ask
# When prompted, enter the path to the artist JSON
```
- [ ] Prompts for a path
- [ ] Accepts the artist JSON path and extracts correctly using `item_id` lookup
- [ ] Restore: `mv /tmp/Some\ Album\ \[12345\].json wacz_output/`

---

## 8. `upload.py` — Archive.org Upload

**Use `IA_COLLECTION=test_collection`** for all upload tests. Items in `test_collection` are automatically deleted after ~30 days.

### 8.1 Dry run

```bash
python upload.py wacz_output/job_<pid>_<hex>/job_<pid>_<hex>/ --dry-run
```
- [ ] Prints identifier and all metadata fields for each WACZ
- [ ] Nothing is uploaded — confirm by checking that the identifier does not appear at `https://archive.org/details/<identifier>`
- [ ] No local files deleted

### 8.2 Real upload

```bash
python upload.py wacz_output/job_<pid>_<hex>/Some\ Album\ \[12345\].wacz
```
- [x] Upload log shows HTTP 200 responses for both `.wacz` and `.json`
- [x] Item appears at `https://archive.org/details/<ia_identifier>` within a few minutes
- [x] Both files visible in the item's file list on archive.org
- [x] Local `.wacz` and `.json` are deleted after successful upload
- [x] Artist JSON updated: `uploaded: true`, `uploaded_at` timestamp present, `ia_identifier` matches

### 8.3 Identifier resolution fallback

Manually remove `ia_identifier` from a sidecar JSON and confirm the error path:

```bash
python3 -c "
import json; from pathlib import Path
p = next(Path('wacz_output').rglob('*.json'))
d = json.loads(p.read_text())
del d['ia_identifier']
p.write_text(json.dumps(d))
"
python upload.py wacz_output/
```
- [ ] Logs `No ia_identifier in ... — skipping`
- [ ] File is not deleted
- [ ] Exits with code 1

### 8.4 Keyboard interrupt / partial upload cleanup

This is hard to test deterministically, but at minimum:

```bash
python upload.py wacz_output/ &
PID=$!
sleep 3
kill -INT $PID
```
- [ ] If the item was created before interrupt: log warns about partial upload and attempts deletion
- [ ] No `.wacz.tmp` files left behind
- [ ] Check `https://archive.org/details/<identifier>` — if it exists, delete it manually and note that the cleanup code needs real-world verification

---

## 9. `email_watcher.py` — Email Watcher

### 9.1 URL extraction regression (custom domain)

```bash
python3 -c "
import email, email.policy, re, sys
sys.path.insert(0, '.')
from bandcamp_wacz.email_watcher import URL_RE, _extract_urls

# Simulate the Bucketheadland email body
body = '''
Live From Bucketstein Manor by Bucketheadland
https://music.bucketheadpikes.com/album/live-from-bucketstein-manor?from=fanpub_fnb

Unfollow: https://music.bucketheadpikes.com/fan_unsubscribe?band_id=123&sig=abc
'''
# Test regex directly
urls = [u for u in URL_RE.findall(body)]
assert len(urls) == 1, f'Expected 1 URL, got {len(urls)}: {urls}'
assert 'unsubscribe' not in urls[0], 'Unsubscribe URL incorrectly matched'
assert urls[0] == 'https://music.bucketheadpikes.com/album/live-from-bucketstein-manor', urls[0]
print('PASS')
"
```
- [x] Exactly 1 URL matched
- [x] Unsubscribe link not matched
- [x] Query string stripped correctly

### 9.2 Sender and subject matching

```bash
python3 -c "
import email, email.policy
from bandcamp_wacz.email_watcher import _is_bandcamp_notification

# Real notification
raw = b'From: Bandcamp <noreply@bandcamp.com>\r\nSubject: New releases from Some Artist\r\n\r\n'
msg = email.message_from_bytes(raw, policy=email.policy.default)
assert _is_bandcamp_notification(msg) == True

# Wrong sender
raw2 = b'From: someone@evil.com\r\nSubject: New releases from Some Artist\r\n\r\n'
msg2 = email.message_from_bytes(raw2, policy=email.policy.default)
assert _is_bandcamp_notification(msg2) == False

# Lax mode accepts forwarded subjects
raw3 = b'From: me@example.com\r\nSubject: Fwd: New release from Some Artist\r\n\r\n'
msg3 = email.message_from_bytes(raw3, policy=email.policy.default)
assert _is_bandcamp_notification(msg3, lax=True) == True
assert _is_bandcamp_notification(msg3, lax=False) == False

print('PASS')
"
```
- [x] All four assertions pass

### 9.3 `--dry-run` against a live inbox

Configure real IMAP credentials in `.env`, then plant a test message in your inbox (forward yourself a real Bandcamp email, or send one with a matching subject from `noreply@bandcamp.com` using `--lax`).

```bash
python bandcamp_wacz/email_watcher.py --dry-run --debug
```
- [x] Connects to IMAP successfully
- [x] Detects the test email
- [x] Logs `[DRY RUN] Would run pipeline for: [...]`
- [x] Email is **not** moved to Trash
- [x] No pipeline subprocess is spawned

### 9.4 `--one-by-one` passed for new artists

```bash
python3 -c "
import sys; sys.path.insert(0, '.')
from unittest.mock import patch, MagicMock
from bandcamp_wacz.email_watcher import _run_pipeline

calls = []
def fake_run(cmd, **kw):
    calls.append(cmd)
    return MagicMock(returncode=0)

with patch('subprocess.run', fake_run), \
     patch('bandcamp_wacz.email_watcher._artist_json_exists', return_value=False):
    _run_pipeline(['https://x.com/album/y'], no_upload=True, dry_run=False,
                  debug=False, artist_root='https://x.com/', one_by_one=True)

cmd = calls[0]
assert '--one-by-one' in cmd, f'--one-by-one missing: {cmd}'
assert '--quick' not in cmd, f'--quick should not be set for new artist: {cmd}'
print('PASS')
"
```
- [ ] `--one-by-one` is in the command for a new artist
- [ ] `--quick` is not

### 9.5 `--one-by-one` not passed for known artists

```bash
python3 -c "
import sys; sys.path.insert(0, '.')
from unittest.mock import patch, MagicMock
from bandcamp_wacz.email_watcher import _run_pipeline

calls = []
def fake_run(cmd, **kw):
    calls.append(cmd)
    return MagicMock(returncode=0)

with patch('subprocess.run', fake_run), \
     patch('bandcamp_wacz.email_watcher._artist_json_exists', return_value=True):
    _run_pipeline(['https://x.com/album/y'], no_upload=True, dry_run=False,
                  debug=False, artist_root='https://x.com/', one_by_one=True)

cmd = calls[0]
assert '--quick' in cmd, f'--quick missing for known artist: {cmd}'
assert '--one-by-one' not in cmd, f'--one-by-one should not be set for known artist: {cmd}'
print('PASS')
"
```
- [ ] `--quick` is in the command for a known artist
- [ ] `--one-by-one` is not — it only applies to the full pipeline (new artists)

### 9.6 `--no-one-by-one` flag respected

```bash
python3 -c "
import sys; sys.path.insert(0, '.')
from unittest.mock import patch, MagicMock
from bandcamp_wacz.email_watcher import _run_pipeline

calls = []
def fake_run(cmd, **kw):
    calls.append(cmd)
    return MagicMock(returncode=0)

with patch('subprocess.run', fake_run), \
     patch('bandcamp_wacz.email_watcher._artist_json_exists', return_value=False):
    _run_pipeline(['https://x.com/album/y'], no_upload=True, dry_run=False,
                  debug=False, artist_root='https://x.com/', one_by_one=False)

cmd = calls[0]
assert '--one-by-one' not in cmd, f'--one-by-one should be absent when one_by_one=False: {cmd}'
print('PASS')
"
```
- [ ] `--one-by-one` absent when `one_by_one=False`

### 9.7 Reconnection on network error

```bash
# Start watcher in one terminal
python bandcamp_wacz/email_watcher.py --dry-run --debug

# In another terminal, block the IMAP port temporarily (Linux)
sudo iptables -I OUTPUT -p tcp --dport 993 -j DROP
sleep 35
sudo iptables -D OUTPUT -p tcp --dport 993 -j DROP
```
- [ ] Watcher logs a network error, waits 30 seconds, reconnects automatically
- [ ] Does not crash or require a restart

---

## 10. End-to-End Pipeline

A full run tying everything together. Use a small artist with 1–3 releases and `IA_COLLECTION=test_collection`.

### 10.1 Clean start to finished upload

```bash
# Clean state
rm -rf artists/ wacz_output/

# Full pipeline
python archive.py --url https://smallartist.bandcamp.com/

# Verify
```
- [x] `artists/` folder created with JSON and `.lst`
- [x] A `wacz_output/job_<pid>_<hex>/` subdirectory was created during the run
- [x] WACZ files created, then deleted after upload
- [x] Sidecar JSONs created, then deleted after upload
- [x] Artist JSON shows `archived: true`, `uploaded: true`, `ia_identifier` for every release
- [x] Items visible on archive.org under `test_collection`
- [x] Job subdirectory is removed automatically after successful upload (`wacz_output/` itself remains)

### 10.2 Re-run is idempotent

Run the exact same command again immediately:

```bash
python archive.py --url https://smallartist.bandcamp.com/
```
- [x] Logs "Nothing to archive — all releases are already up to date"
- [x] No new crawls, no new uploads
- [x] Artist JSON is unchanged

### 10.3 Single-release artist

Find a Bandcamp artist with only one release and no `/music` page:

```bash
python archive.py --url https://single-release-artist.bandcamp.com/album/only-album
```
- [x] Pipeline completes without erroring on missing music grid
- [x] Release is archived and uploaded

### 10.4 Custom domain end-to-end

```bash
python archive.py --url https://music.bucketheadpikes.com/album/live-from-bucketstein-manor --no-upload
```
- [x] `fetch_metadata.py` runs (or `update_metadata` if already known)
- [x] WACZ is produced with correct filename
- [x] Artist JSON created under `artists/Bucketheadland [3055507029]/` (or equivalent)

---

## 11. Security and Public Readiness

### 11.1 Repository scan

```bash
# Check for any secrets accidentally staged
git diff --cached

# Check the full commit history for anything sensitive
git log --all -p | grep -i "access_key\|secret_key\|password\|token" | grep -v "example\|your_\|placeholder\|getenv\|os.environ"
```
- [x] No real credentials appear anywhere in git history

### 11.2 Permissions check on `.env`

```bash
ls -la .env
```
- [x] Mode is `600` (readable only by owner), not `644`

### 11.3 `USER_AGENT` is honest

```bash
python3 -c "from bandcamp_wacz.config import USER_AGENT; print(USER_AGENT)"
```
- [x] Contains the real GitHub repository URL, not a placeholder
- [x] Identifies the tool honestly (not spoofing a browser)

### 11.4 `test_collection` removed before first real use

- [x] `IA_COLLECTION` in `.env.example` is `opensource_media` (or your intended collection), not `test_collection`
- [x] Your personal `.env` has `IA_COLLECTION` set correctly for production use

### 11.5 Default request delay is polite

```bash
python3 -c "from bandcamp_wacz.config import BC_REQUEST_DELAY; print(BC_REQUEST_DELAY)"
```
- [x] Default is `1000-3000` ms (at least 1 second between requests)
- [x] `.env.example` documents what the values mean

---

## 12. Documentation

- [x] `README.md` renders correctly on GitHub (check locally with a Markdown previewer or push to a private repo first)
- [x] All links in `README.md` to `usage/*.md` files resolve correctly — no broken anchors
- [x] `.env.example` matches every variable referenced in `config.py` — run: `grep "os.getenv" bandcamp_wacz/config.py | grep -oP '"[A-Z_]+"' | sort` and compare against `.env.example`
- [x] `usage/` directory contains a `.md` file for every script
- [ ] `--help` output is accurate for every script:
  ```bash
  python archive.py --help
  python fetch_metadata.py --help
  python update_metadata.py --help
  python upload.py --help
  python bandcamp_wacz/email_watcher.py --help
  python bandcamp_wacz/extract.py --help
  ```
  - [ ] No flags documented in `--help` that don't exist, no flags that exist but are undocumented
