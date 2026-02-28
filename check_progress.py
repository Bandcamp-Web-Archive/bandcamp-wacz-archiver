#!/usr/bin/env python3
"""
check_progress.py — Inspect archival and upload progress for followed artists.

Usage:
    python check_progress.py                          # interactive artist picker
    python check_progress.py --all                    # check every artist
    python check_progress.py --path PATH              # specific file or folder
    python check_progress.py --id BAND_ID             # artist by band_id
    python check_progress.py --all --summary          # one-line-per-artist overview
    python check_progress.py --all --warnings-only    # only show incomplete artists
    python check_progress.py --id BAND_ID --verify-ia # verify identifiers on archive.org
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


# ── ANSI colour helpers ────────────────────────────────────────────────────────

RESET   = "\033[0m"
BOLD    = "\033[1m"
DIM     = "\033[2m"

BLACK   = "\033[30m"
RED     = "\033[31m"
GREEN   = "\033[32m"
YELLOW  = "\033[33m"
BLUE    = "\033[34m"
MAGENTA = "\033[35m"
CYAN    = "\033[36m"
WHITE   = "\033[37m"

BG_GREEN  = "\033[42m"
BG_RED    = "\033[41m"
BG_YELLOW = "\033[43m"
BG_BLUE   = "\033[44m"
BG_CYAN   = "\033[46m"

def bold(s):    return f"{BOLD}{s}{RESET}"
def dim(s):     return f"{DIM}{s}{RESET}"
def green(s):   return f"{GREEN}{s}{RESET}"
def red(s):     return f"{RED}{s}{RESET}"
def yellow(s):  return f"{YELLOW}{s}{RESET}"
def cyan(s):    return f"{CYAN}{s}{RESET}"
def magenta(s): return f"{MAGENTA}{s}{RESET}"
def blue(s):    return f"{BLUE}{s}{RESET}"

def badge(text, bg, fg=WHITE):
    return f"{BOLD}{bg}{fg} {text} {RESET}"


# ── Config ─────────────────────────────────────────────────────────────────────

DEFAULT_ARTISTS_DIR = Path(__file__).parent / "artists"

# Default delay between archive.org API calls (seconds). Overridden by --delay.
DEFAULT_IA_DELAY = 0.5

# Default retry count for transient network errors during --verify-ia.
DEFAULT_IA_RETRIES = 3


# ── Core logic ─────────────────────────────────────────────────────────────────

def find_artists_dir() -> Path:
    """Return the artists directory, preferring .env ARTISTS_DIR if set."""
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("ARTISTS_DIR") and "=" in line:
                _, _, val = line.partition("=")
                val = val.strip().strip('"').strip("'")
                if val:
                    return Path(val)
    return DEFAULT_ARTISTS_DIR


def find_json_in_folder(folder: Path) -> tuple[Path | None, list[Path]]:
    """
    Return (json_path, partial_files) for a given artist folder.
    json_path may be None if no JSON was found.
    """
    jsons    = sorted(folder.glob("*.json"))
    partials = sorted(folder.glob("*.json.partial"))
    return (jsons[0] if jsons else None, partials)


def load_json(path: Path) -> dict | None:
    """Load and return the JSON data, or None on failure."""
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        print(red(f"  \u2717 Could not parse {path.name}: {e}"))
        return None
    except OSError as e:
        print(red(f"  \u2717 Could not read {path}: {e}"))
        return None


def analyse(json_path: Path) -> dict | None:
    """
    Parse an artist JSON and return a stats dict, or None on error.

    Returns:
        name, total, archived, not_archived, uploaded, not_uploaded,
        complete, json_path, raw_data, artist_key
    """
    data = load_json(json_path)
    if data is None:
        return None

    # The top-level key is the artist name; _band_id is a sibling key
    artist_name = next(
        (k for k in data if not k.startswith("_") and isinstance(data[k], list)),
        None,
    )
    if artist_name is None:
        print(red(f"  \u2717 Unexpected JSON structure in {json_path.name}"))
        return None

    releases     = data[artist_name]
    total        = len(releases)
    archived     = sum(1 for r in releases if r.get("archived"))
    uploaded     = sum(1 for r in releases if r.get("uploaded"))
    not_archived = total - archived
    not_uploaded = total - uploaded

    return {
        "name"        : artist_name,
        "total"       : total,
        "archived"    : archived,
        "not_archived": not_archived,
        "uploaded"    : uploaded,
        "not_uploaded": not_uploaded,
        "complete"    : not_archived == 0 and not_uploaded == 0,
        "json_path"   : json_path,
        "raw_data"    : data,
        "artist_key"  : artist_name,
    }


# ── Internet Archive verification ──────────────────────────────────────────────

# Result types for each identifier check
_GHOST   = "ghost"      # item.exists is False — confirmed missing on IA
_OK      = "ok"         # item.exists is True — confirmed present
_NETERR  = "neterr"     # exception after all retries — network/service problem


def _ia_module():
    """Import and return the internetarchive module, or exit with a helpful error."""
    try:
        import internetarchive as ia
        return ia
    except ImportError:
        print()
        print(red("  \u2717 The 'internetarchive' package is not installed."))
        print(f"  {dim('Install it with:')}  {bold('pip install internetarchive')}")
        sys.exit(1)


def collect_ia_identifiers(stats: dict) -> list[dict]:
    """
    Walk a stats dict's raw_data and collect every ia_identifier to verify.

    Each record:
        identifier   : str
        title        : str
        release_idx  : int       - index into the artist's release list
        is_history   : bool      - True if from a _history snapshot
        history_idx  : int|None  - index into _history (if is_history)
        uploaded     : bool      - True if the release is flagged uploaded in JSON
    """
    records  = []
    releases = stats["raw_data"][stats["artist_key"]]

    for r_idx, release in enumerate(releases):
        # Current identifier
        ident = release.get("ia_identifier")
        if ident:
            records.append({
                "identifier" : ident,
                "title"      : release.get("title", f"release #{r_idx}"),
                "release_idx": r_idx,
                "is_history" : False,
                "history_idx": None,
                "uploaded"   : bool(release.get("uploaded")),
            })

        # History identifiers (_history is a list of prior snapshots)
        for h_idx, snapshot in enumerate(release.get("_history", [])):
            h_ident = snapshot.get("ia_identifier")
            if h_ident and h_ident != ident:
                records.append({
                    "identifier" : h_ident,
                    "title"      : snapshot.get("title", release.get("title", f"release #{r_idx}")),
                    "release_idx": r_idx,
                    "is_history" : True,
                    "history_idx": h_idx,
                    "uploaded"   : bool(snapshot.get("uploaded")),
                })

    return records


def _fmt_eta(seconds: float) -> str:
    """Format a number of seconds as a human-readable ETA string."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def check_identifiers_on_ia(
    records  : list[dict],
    artist_name: str,
    delay    : float = DEFAULT_IA_DELAY,
    retries  : int   = DEFAULT_IA_RETRIES,
) -> dict[str, str]:
    """
    Check each unique ia_identifier against archive.org.

    HOW IT WORKS
    ────────────
    The internetarchive library calls GET https://archive.org/metadata/{id}.
    That endpoint always returns HTTP 200. The response body contains either:
      • Full metadata ({"metadata": {...}, ...})  → item.exists is True  → _OK
      • Error notice ({"error": "...", "statuscode": 404, ...})
                                                  → item.exists is False → _GHOST

    Actual network/connectivity failures raise exceptions (requests.exceptions.*).
    These are retried up to `retries` times with exponential back-off.
    If still failing after all retries they are recorded as _NETERR, never _GHOST,
    because we cannot confirm non-existence without a successful response.

    Returns {identifier: _OK | _GHOST | _NETERR}
    """
    ia = _ia_module()

    unique = list(dict.fromkeys(r["identifier"] for r in records))
    total  = len(unique)
    results: dict[str, str] = {}

    total_time = (total - 1) * delay   # delay is between requests, not after last
    print()
    print(f"  {cyan('Verifying')} {bold(str(total))} "
          f"identifier{'s' if total != 1 else ''} on archive.org "
          f"for {bold(artist_name)}")
    print(f"  {dim(f'ETA: {_fmt_eta(total_time)}  ({delay}s between requests, up to {retries} retries on network error)')}")

    bar_width = 30
    start_time = time.monotonic()

    interrupted = False
    try:
        for i, ident in enumerate(unique, 1):
            result = None
            last_exc: Exception | None = None

            # ── Single identifier: try up to 1 + retries times ────────────
            for attempt in range(1 + retries):
                try:
                    item   = ia.get_item(ident)
                    result = _OK if item.exists else _GHOST
                    break   # definitive answer — no retry needed
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    last_exc = exc
                    if attempt < retries:
                        backoff = delay * (2 ** attempt)   # exponential back-off
                        # Show a brief inline retry notice before sleeping
                        print(f"\r  {yellow('?')}  network error on {dim(ident[:45])} "
                              f"— retry {attempt + 1}/{retries} in {backoff:.1f}s ..."
                              + " " * 10,
                              end="", flush=True)
                        time.sleep(backoff)
                    else:
                        result = _NETERR

            results[ident] = result

            # ── Progress bar + ETA ────────────────────────────────────────
            elapsed   = time.monotonic() - start_time
            remaining = max(0.0, total_time - elapsed + delay)   # re-estimate
            eta_str   = _fmt_eta(remaining) if i < total else "done"

            filled = round(i / total * bar_width)
            empty  = bar_width - filled
            bar    = f"{GREEN}{'█' * filled}{DIM}{'░' * empty}{RESET}"

            if result == _OK:
                icon = green("\u2713")
            elif result == _GHOST:
                icon = red("\u2717")
            else:
                icon = yellow("?")

            count_str  = dim(f" {i}/{total}")
            eta_display = dim(f" ETA {eta_str}")
            ident_trunc = dim(f"{ident[:48]:<50}")
            print(f"\r  [{bar}]{count_str}{eta_display}  {icon}  {ident_trunc}",
                  end="", flush=True)

            if i < total:
                time.sleep(delay)

    except KeyboardInterrupt:
        interrupted = True
        print()   # newline after partial progress line
        print(f"\n  {yellow('\u26a0')}  Verification interrupted after "
              f"{len(results)}/{total} identifiers.")
        # Fill remaining with _NETERR so callers have a complete dict
        for ident in unique:
            if ident not in results:
                results[ident] = _NETERR
        # Re-raise so the outer handler can decide whether to keep going
        raise

    if not interrupted:
        print()   # newline after final progress line
    return results


def print_ia_results(
    records    : list[dict],
    ia_results : dict[str, str],
    artist_name: str,
) -> list[dict]:
    """
    Print a summary of IA verification results.

    Returns 'fixable' records — current (non-history) releases marked
    uploaded=True whose identifier was confirmed _GHOST.
    """
    ghost_current  = [
        r for r in records
        if not r["is_history"]
        and r["uploaded"]
        and ia_results.get(r["identifier"]) == _GHOST
    ]
    ghost_history  = [
        r for r in records
        if r["is_history"]
        and ia_results.get(r["identifier"]) == _GHOST
    ]
    neterr_records = [
        r for r in records
        if ia_results.get(r["identifier"]) == _NETERR
    ]

    confirmed = sum(1 for r in records if ia_results.get(r["identifier"]) == _OK)
    total     = len(records)

    print()
    print(f"  {bold(cyan(artist_name))}  {dim('\u2014 archive.org verification')}")
    print(f"  {dim('\u2500' * 52)}")
    print(f"  {green('\u2713')} {confirmed}/{total} "
          f"identifier{'s' if total != 1 else ''} confirmed on archive.org")

    if neterr_records:
        print()
        label = (f"{len(neterr_records)} identifier"
                 + ("s" if len(neterr_records) != 1 else ""))
        print(f"  {badge('NETWORK ERROR', BG_YELLOW, BLACK)}  "
              f"{label} could not be verified after all retries:")
        for r in neterr_records[:5]:
            title_s = r["title"][:48] + ("\u2026" if len(r["title"]) > 48 else "")
            hist_tag = dim(" [history]") if r["is_history"] else ""
            print(f"    {yellow('?')}  {dim(r['identifier'])}{hist_tag}")
            print(f"        {dim(title_s)}")
        if len(neterr_records) > 5:
            print(f"    {dim(f'... and {len(neterr_records) - 5} more')}")
        print(f"  {dim('These are network failures, not confirmed missing. Re-run to retry.')}")

    if ghost_history:
        print()
        label = (f"{len(ghost_history)} historical identifier"
                 + ("s" if len(ghost_history) != 1 else ""))
        print(f"  {badge('HISTORY MISSING', BG_YELLOW, BLACK)}  "
              f"{label} confirmed absent from archive.org:")
        for r in ghost_history:
            title_s = r["title"][:48] + ("\u2026" if len(r["title"]) > 48 else "")
            print(f"    {dim('\u21b3')} {dim(r['identifier'])}  {dim(title_s)}")
        print(f"  {dim('History records are informational \u2014 no fix is offered.')}")

    if ghost_current:
        print()
        label = (f"{len(ghost_current)} release"
                 + ("s" if len(ghost_current) != 1 else ""))
        print(f"  {badge('NOT FOUND', BG_RED)}  "
              f"{label} marked {bold('uploaded=True')} but confirmed absent from archive.org:")
        for r in ghost_current:
            title_s = r["title"][:48] + ("\u2026" if len(r["title"]) > 48 else "")
            print(f"    {red('\u2717')}  {bold(r['identifier'])}")
            print(f"        {dim(title_s)}")
    else:
        if not ghost_history and not neterr_records:
            print(f"  {green('\u2713')} All identifiers verified \u2014 nothing is missing.")
        elif not ghost_history:
            print(f"  {green('\u2713')} No ghost items found \u2014 only network failures.")

    return ghost_current


def prompt_fix_missing(missing: list[dict], stats: dict) -> None:
    """
    Interactively ask the user to reset uploaded=False / ia_identifier=null
    for each release confirmed missing from archive.org, then write the JSON.
    """
    if not missing:
        return

    json_path  = stats["json_path"]
    data       = stats["raw_data"]
    artist_key = stats["artist_key"]
    releases   = data[artist_key]

    print()
    print(f"  {bold(yellow('Fix missing items?'))}")
    print(f"  {dim('Resetting')} {bold('uploaded=False')} {dim('and')} "
          f"{bold('ia_identifier=null')} {dim('will re-queue these releases')}")
    print(f"  {dim('so')} {bold('upload.py')} {dim('picks them up on the next run.')}")
    print()

    to_fix: list[dict] = []

    try:
        for r in missing:
            title_s = r["title"][:60] + ("\u2026" if len(r["title"]) > 60 else "")
            print(f"  {dim(r['identifier'])}")
            print(f"  {bold(title_s)}")
            ans = input(
                f"  {BOLD}{CYAN}  Reset this item? [y/N]{RESET} "
            ).strip().lower()
            if ans in ("y", "yes"):
                to_fix.append(r)
            print()
    except (KeyboardInterrupt, EOFError):
        print()
        print(dim("\n  Aborted \u2014 no changes written."))
        return

    if not to_fix:
        print(dim("  No changes requested."))
        return

    # Apply fixes in memory
    for r in to_fix:
        rel = releases[r["release_idx"]]
        rel["uploaded"]      = False
        rel["uploaded_at"]   = None
        rel["ia_identifier"] = None

    # Write back atomically via a .tmp file
    tmp = json_path.with_suffix(".json.tmp")
    try:
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=4),
            encoding="utf-8",
        )
        tmp.replace(json_path)
    except OSError as e:
        print(red(f"  \u2717 Failed to write {json_path}: {e}"))
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        return

    n = len(to_fix)
    print(f"  {green('\u2713')} {n} release{'s' if n != 1 else ''} "
          f"reset in {bold(json_path.name)}")
    print(f"  {dim('Run')} {bold('upload.py')} {dim('to re-upload to archive.org.')}")


# ── Display ────────────────────────────────────────────────────────────────────

def _bar(done: int, total: int, width: int = 28) -> str:
    """Return a compact progress bar string."""
    if total == 0:
        filled = width
    else:
        filled = round(done / total * width)
    filled = max(0, min(width, filled))
    empty  = width - filled
    bar    = f"{GREEN}{'█' * filled}{DIM}{'░' * empty}{RESET}"
    return f"[{bar}]"


def print_artist_result(stats: dict, partials: list[Path], summary: bool = False) -> None:
    """Print progress for a single artist."""

    name     = stats["name"]
    total    = stats["total"]
    archived = stats["archived"]
    uploaded = stats["uploaded"]
    na       = stats["not_archived"]
    nu       = stats["not_uploaded"]
    complete = stats["complete"]

    # ── partial file warnings ──────────────────────────────────────────────
    for p in partials:
        print(
            f"  {badge('PARTIAL', BG_YELLOW, BLACK)}  "
            f"{yellow(p.name)} \u2014 incomplete metadata fetch detected. "
            f"Re-run {bold('fetch_metadata.py')} to finish."
        )

    # ── summary mode (one line) ────────────────────────────────────────────
    if summary:
        if complete:
            status = badge("DONE", BG_GREEN)
        else:
            parts = []
            if na: parts.append(red(f"{na} not archived"))
            if nu: parts.append(yellow(f"{nu} not uploaded"))
            status = badge("PENDING", BG_RED) + "  " + dim(" \u00b7 ".join(parts))
        print(f"  {status}  {bold(name)}  {dim(f'({total} releases)')}")
        return

    # ── full mode ──────────────────────────────────────────────────────────
    print()
    print(f"  {bold(cyan(name))}")
    print(f"  {dim('\u2500' * max(len(name), 32))}")

    if complete:
        print(f"  {badge('COMPLETE', BG_GREEN)}  "
              f"{green('All releases are archived and uploaded.')}")
        print(f"  {dim(f'{total} releases total.')}")
        return

    arch_pct  = f"{archived/total*100:.0f}%" if total else "\u2014"
    arch_bar  = _bar(archived, total)
    arch_stat = (
        green(f"{archived} archived")
        if archived == total
        else red(f"{na} not archived") + dim(f"  ({archived}/{total})")
    )
    print(f"  {arch_bar}  archive  {arch_pct:>4}  {arch_stat}")

    up_pct   = f"{uploaded/total*100:.0f}%" if total else "\u2014"
    up_bar   = _bar(uploaded, total)
    up_stat  = (
        green(f"{uploaded} uploaded")
        if uploaded == total
        else yellow(f"{nu} not uploaded") + dim(f"  ({uploaded}/{total})")
    )
    print(f"  {up_bar}  upload   {up_pct:>4}  {up_stat}")

    # hint
    if na:
        print(f"\n  {dim('\u2192 run')} {bold('archive.py')} "
              f"{dim('to queue unarchived releases.')}")
    if nu and not na:
        print(f"\n  {dim('\u2192 run')} {bold('upload.py')} "
              f"{dim('to push finished WACZs to archive.org.')}")


def print_header(title: str) -> None:
    width = 60
    print()
    print(f"{BOLD}{CYAN}{'\u2500' * width}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'\u2500' * width}{RESET}")


def print_footer(all_stats: list[dict]) -> None:
    total_artists  = len(all_stats)
    complete       = sum(1 for s in all_stats if s["complete"])
    incomplete     = total_artists - complete
    total_releases = sum(s["total"] for s in all_stats)
    total_archived = sum(s["archived"] for s in all_stats)
    total_uploaded = sum(s["uploaded"] for s in all_stats)
    total_na       = total_releases - total_archived
    total_nu       = total_releases - total_uploaded

    print()
    print(f"{BOLD}{CYAN}{'\u2500' * 60}{RESET}")
    print(f"{BOLD}  Summary across {total_artists} "
          f"artist{'s' if total_artists != 1 else ''}{RESET}")
    print(f"{CYAN}{'\u2500' * 60}{RESET}")

    # artists line
    artists_line = (
        green(f"{complete}/{total_artists} complete")
        if incomplete == 0
        else f"{green(f'{complete} complete')}  {red(f'{incomplete} pending')}"
    )
    print(f"  Artists   {artists_line}")

    # releases line
    arch_line = (
        green(f"{total_archived}/{total_releases} archived")
        if total_na == 0
        else f"{dim(f'{total_archived}/{total_releases} archived')}"
             f"  {red(f'{total_na} remaining')}"
    )
    up_line = (
        green(f"{total_uploaded}/{total_releases} uploaded")
        if total_nu == 0
        else f"{dim(f'{total_uploaded}/{total_releases} uploaded')}"
             f"  {yellow(f'{total_nu} remaining')}"
    )
    print(f"  Artists   {artists_line}")
    print(f"  Releases  {arch_line}")
    print(f"            {up_line}")
    print()


# ── Resolve artist JSON paths ──────────────────────────────────────────────────

def resolve_path_arg(path_arg: str) -> list[tuple[Path, list[Path]]]:
    """
    Accept a path to either a .json file, a .json.partial file, or a directory.
    Returns list of (json_path, partials) tuples.
    """
    p = Path(path_arg).expanduser().resolve()
    if not p.exists():
        print(red(f"Error: path does not exist: {p}"))
        sys.exit(1)

    if p.is_file():
        if p.suffix == ".json":
            partials = sorted(p.parent.glob("*.json.partial"))
            return [(p, partials)]
        elif p.name.endswith(".json.partial"):
            print(yellow(f"Warning: {p.name} is a partial file \u2014 "
                         f"metadata fetch was interrupted."))
            print(yellow("         Re-run fetch_metadata.py to complete it. "
                         "No progress to show yet."))
            sys.exit(0)
        else:
            print(red(f"Error: expected a .json file, got: {p.name}"))
            sys.exit(1)

    if p.is_dir():
        json_path, partials = find_json_in_folder(p)
        if json_path is None:
            # Maybe it's the artists/ root dir — treat each subdirectory
            subdirs = [d for d in sorted(p.iterdir()) if d.is_dir()]
            results = []
            for d in subdirs:
                jp, pt = find_json_in_folder(d)
                if jp:
                    results.append((jp, pt))
                elif pt:
                    results.append((None, pt))
            if not results:
                print(red(f"Error: no artist JSON files found in {p}"))
                sys.exit(1)
            return results
        return [(json_path, partials)]

    print(red(f"Error: not a file or directory: {p}"))
    sys.exit(1)


def resolve_id_arg(band_id: str, artists_dir: Path) -> list[tuple[Path, list[Path]]]:
    """Find the artist folder whose name contains [band_id]."""
    band_id = band_id.strip()
    if not artists_dir.exists():
        print(red(f"Error: artists directory not found: {artists_dir}"))
        sys.exit(1)

    matches = [d for d in sorted(artists_dir.iterdir())
               if d.is_dir() and f"[{band_id}]" in d.name]

    if not matches:
        print(red(f"Error: no artist folder found for band_id {band_id}"))
        print(dim(f"       (searched in {artists_dir})"))
        sys.exit(1)

    results = []
    for folder in matches:
        jp, pt = find_json_in_folder(folder)
        if jp:
            results.append((jp, pt))
        elif pt:
            results.append((None, pt))
    return results


def resolve_all_arg(artists_dir: Path) -> list[tuple[Path, list[Path]]]:
    """Collect all artist JSONs from the artists directory."""
    if not artists_dir.exists():
        print(red(f"Error: artists directory not found: {artists_dir}"))
        sys.exit(1)

    subdirs = [d for d in sorted(artists_dir.iterdir(), key=lambda d: d.name.lower())
               if d.is_dir()]

    results = []
    for d in subdirs:
        jp, pt = find_json_in_folder(d)
        if jp or pt:
            results.append((jp, pt))

    if not results:
        print(yellow(f"No artist folders found in {artists_dir}"))
        sys.exit(0)

    return results


def interactive_pick(artists_dir: Path) -> list[tuple[Path, list[Path]]]:
    """Present a numbered list of artist folders and let the user pick."""
    if not artists_dir.exists():
        print(red(f"Error: artists directory not found: {artists_dir}"))
        sys.exit(1)

    folders = [d for d in sorted(artists_dir.iterdir(), key=lambda d: d.name.lower())
               if d.is_dir()]

    if not folders:
        print(yellow("No artist folders found."))
        sys.exit(0)

    print()
    print(f"{BOLD}{CYAN}  Artists in {artists_dir}{RESET}")
    print(f"{CYAN}  {'\u2500' * 50}{RESET}")
    for i, d in enumerate(folders, 1):
        jp, pt  = find_json_in_folder(d)
        marker  = dim(" [partial]") if pt and not jp else ""
        no_json = red(" [no json]") if not jp and not pt else ""
        print(f"  {dim(f'{i:>3}.')}  {d.name}{marker}{no_json}")

    print()
    print(f"  {dim('Enter number(s) separated by spaces/commas, or')} "
          f"{bold('a')} {dim('for all:')}")
    try:
        raw = input(f"  {BOLD}{CYAN}>{RESET} ").strip()
    except (KeyboardInterrupt, EOFError):
        print()
        sys.exit(0)

    if raw.lower() in ("a", "all"):
        chosen = folders
    else:
        tokens = [t.strip() for t in raw.replace(",", " ").split()]
        chosen = []
        for t in tokens:
            try:
                idx = int(t) - 1
                if 0 <= idx < len(folders):
                    chosen.append(folders[idx])
                else:
                    print(yellow(f"  Warning: {t} is out of range, skipping."))
            except ValueError:
                print(yellow(f"  Warning: '{t}' is not a valid number, skipping."))

    if not chosen:
        print(yellow("  No valid selection."))
        sys.exit(0)

    results = []
    for folder in chosen:
        jp, pt = find_json_in_folder(folder)
        results.append((jp, pt))
    return results


# ── Confirmation gates ─────────────────────────────────────────────────────────

def _count_all_identifiers(valid_pairs: list) -> int:
    """Count every ia_identifier (including _history) across all valid pairs."""
    total = 0
    for jp, _ in valid_pairs:
        if jp is None:
            continue
        data = load_json(jp)
        if data is None:
            continue
        artist_key = next(
            (k for k in data if not k.startswith("_") and isinstance(data[k], list)),
            None,
        )
        if artist_key is None:
            continue
        for r in data[artist_key]:
            if r.get("ia_identifier"):
                total += 1
            for snap in r.get("_history", []):
                if snap.get("ia_identifier"):
                    total += 1
    return total


def confirm_verify_all(valid_pairs: list, delay: float, retries: int) -> bool:
    """
    Warn the user about the cost of --verify-ia across all artists.
    Returns True only if the user types y/yes.
    """
    total_ids  = _count_all_identifiers(valid_pairs)
    est_s      = (total_ids - 1) * delay   # delay is between requests
    eta_str    = _fmt_eta(est_s)

    print()
    print(f"  {badge('WARNING', BG_YELLOW, BLACK)}")
    print()
    print(f"  {bold('--verify-ia')} with {bold('--all')} will make "
          f"{bold(str(total_ids))} request{'s' if total_ids != 1 else ''} "
          f"to archive.org,")
    print(f"  one per identifier across all {bold(str(len(valid_pairs)))} artists.")
    print()
    print(f"  {dim('Estimated time:')} {bold(eta_str)}  "
          f"{dim(f'({delay}s between requests, up to {retries} retries on network error)')}")
    print()
    print(f"  {dim('Tip: use')} {bold('--id BAND_ID')} {dim('or')} "
          f"{bold('--path PATH')} {dim('to verify a single artist instead.')}")
    print()

    try:
        ans = input(f"  {BOLD}{CYAN}Continue? [y/N]{RESET} ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        print()
        return False

    return ans in ("y", "yes")


def confirm_verify_interactive_all(valid_pairs: list, delay: float, retries: int) -> bool:
    """
    Same warning, but triggered when the interactive picker chose all artists
    and --verify-ia is active. Slightly different framing.
    """
    total_ids = _count_all_identifiers(valid_pairs)
    est_s     = (total_ids - 1) * delay
    eta_str   = _fmt_eta(est_s)

    print()
    print(f"  {badge('HEADS UP', BG_YELLOW, BLACK)}  "
          f"You selected all {bold(str(len(valid_pairs)))} artists with {bold('--verify-ia')}.")
    print()
    print(f"  This will make {bold(str(total_ids))} request{'s' if total_ids != 1 else ''} "
          f"to archive.org — estimated {bold(eta_str)}.")
    print(f"  {dim(f'({delay}s between requests, up to {retries} retries on network error)')}")
    print()

    try:
        ans = input(f"  {BOLD}{CYAN}Continue? [y/N]{RESET} ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        print()
        return False

    return ans in ("y", "yes")


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="check_progress.py",
        description="Check archival and upload progress for followed Bandcamp artists.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python check_progress.py                          interactive artist picker
  python check_progress.py --all                    every artist in artists/
  python check_progress.py --all --summary          one-line overview per artist
  python check_progress.py --all --warnings-only    only show incomplete artists
  python check_progress.py --path artists/Some\\ Artist\\ [123]/
  python check_progress.py --id 3774983561
  python check_progress.py --id 3774983561 --verify-ia
  python check_progress.py --id 3774983561 --verify-ia --delay 1 --retries 5
  python check_progress.py --all --verify-ia        (asks for confirmation)
        """,
    )

    src = parser.add_mutually_exclusive_group()
    src.add_argument(
        "--path", "-p",
        metavar="PATH",
        help="Path to an artist JSON file, an artist folder, or the artists/ root directory.",
    )
    src.add_argument(
        "--id", "-i",
        metavar="BAND_ID",
        help="Band ID to look up in the artists/ directory.",
    )
    src.add_argument(
        "--all", "-a",
        action="store_true",
        help="Check every artist in the artists/ directory.",
    )

    parser.add_argument(
        "--artists-dir",
        metavar="DIR",
        help="Override the artists/ directory (also read from ARTISTS_DIR in .env).",
    )
    parser.add_argument(
        "--summary", "-s",
        action="store_true",
        help="Print a compact one-line summary per artist instead of full detail.",
    )
    parser.add_argument(
        "--warnings-only", "-w",
        action="store_true",
        help="Only print artists that are not fully complete.",
    )
    parser.add_argument(
        "--verify-ia", "-V",
        action="store_true",
        dest="verify_ia",
        help=(
            "Verify every ia_identifier (including _history snapshots) exists on "
            "archive.org. item.exists=False means IA confirmed missing (ghost); "
            "network exceptions are retried and reported separately. "
            "Releases confirmed missing are flagged and you can reset them. "
            "When combined with --all, requires explicit confirmation."
        ),
    )
    parser.add_argument(
        "--delay",
        metavar="SECONDS",
        type=float,
        default=DEFAULT_IA_DELAY,
        help=(
            f"Seconds to wait between archive.org requests during --verify-ia "
            f"(default: {DEFAULT_IA_DELAY}). Lower values are faster but less polite."
        ),
    )
    parser.add_argument(
        "--retries",
        metavar="N",
        type=int,
        default=DEFAULT_IA_RETRIES,
        help=(
            f"Number of retries for transient network errors during --verify-ia "
            f"(default: {DEFAULT_IA_RETRIES}). Retries use exponential back-off. "
            f"Only network exceptions are retried; confirmed-missing items are not."
        ),
    )

    args = parser.parse_args()

    if args.delay < 0:
        print(red("Error: --delay must be >= 0"))
        sys.exit(1)
    if args.retries < 0:
        print(red("Error: --retries must be >= 0"))
        sys.exit(1)

    artists_dir = (
        Path(args.artists_dir).expanduser().resolve()
        if args.artists_dir
        else find_artists_dir()
    )

    # ── resolve which JSONs to check ──────────────────────────────────────
    interactive_all = False   # track if the user typed 'a' in the picker
    if args.path:
        pairs = resolve_path_arg(args.path)
    elif args.id:
        pairs = resolve_id_arg(args.id, artists_dir)
    elif args.all:
        pairs = resolve_all_arg(artists_dir)
    else:
        pairs = interactive_pick(artists_dir)
        # Detect if every folder was chosen (== all)
        all_folders = [
            d for d in artists_dir.iterdir()
            if d.is_dir()
        ] if artists_dir.exists() else []
        interactive_all = len(pairs) == len(all_folders) and len(pairs) > 1

    # ── warn about loose partial files with no accompanying JSON ──────────
    for jp, pt in pairs:
        if pt and not jp:
            for p in pt:
                print()
                print(
                    f"  {badge('PARTIAL', BG_YELLOW, BLACK)}  "
                    f"{yellow(bold(p.parent.name))}\n"
                    f"  {yellow(p.name)} \u2014 metadata fetch was interrupted before "
                    f"completing.\n"
                    f"  {dim('Re-run')} {bold('fetch_metadata.py')} "
                    f"{dim('with the artist URL to finish.')}"
                )

    # ── filter to pairs that have a JSON ──────────────────────────────────
    valid_pairs = [(jp, pt) for jp, pt in pairs if jp is not None]

    if not valid_pairs:
        print(yellow("\n  No complete artist JSONs to check."))
        sys.exit(0)

    # ── --verify-ia: confirmation gates ───────────────────────────────────
    if args.verify_ia:
        if args.all:
            if not confirm_verify_all(valid_pairs, args.delay, args.retries):
                print(dim("\n  Aborted."))
                sys.exit(0)
        elif interactive_all:
            if not confirm_verify_interactive_all(valid_pairs, args.delay, args.retries):
                print(dim("\n  Aborted."))
                sys.exit(0)

    # ── header ────────────────────────────────────────────────────────────
    multi = len(valid_pairs) > 1
    if multi:
        print_header(
            f"Progress \u2014 {len(valid_pairs)} "
            f"artist{'s' if len(valid_pairs) != 1 else ''}"
        )
        if args.summary:
            print()

    # ── process each artist ───────────────────────────────────────────────
    all_stats        = []
    interrupted      = False
    artists_verified = 0

    for json_path, partials in valid_pairs:
        if interrupted:
            break

        stats = analyse(json_path)
        if stats is None:
            continue

        all_stats.append(stats)

        # In warnings-only mode, skip complete artists UNLESS --verify-ia is
        # active (a complete artist may still have ghost IA items).
        if args.warnings_only and stats["complete"] and not args.verify_ia:
            continue

        print_artist_result(stats, partials, summary=args.summary)

        # ── IA verification ───────────────────────────────────────────────
        if args.verify_ia:
            records = collect_ia_identifiers(stats)

            if not records:
                print()
                print(f"  {dim('No ia_identifiers found for')} "
                      f"{bold(stats['name'])}{dim(' \u2014 nothing to verify.')}")
                artists_verified += 1
                continue

            try:
                ia_results = check_identifiers_on_ia(
                    records,
                    stats["name"],
                    delay=args.delay,
                    retries=args.retries,
                )
                artists_verified += 1
            except KeyboardInterrupt:
                interrupted = True
                # ia_results was partially filled inside check_identifiers_on_ia
                # before re-raising; we won't have it here, so skip the fix prompt
                print()
                print(f"  {yellow('\u26a0')}  Skipping fix prompt for "
                      f"{bold(stats['name'])} due to interrupt.")
                break

            fixable = print_ia_results(records, ia_results, stats["name"])

            if fixable:
                try:
                    prompt_fix_missing(fixable, stats)
                except KeyboardInterrupt:
                    interrupted = True
                    print()
                    print(dim("  Aborted \u2014 no changes written."))
                    break

    # ── graceful interrupt summary ────────────────────────────────────────
    if interrupted:
        remaining = len(valid_pairs) - artists_verified
        print()
        if remaining > 0:
            print(f"  {yellow('\u26a0')}  Interrupted. "
                  f"{artists_verified} artist{'s' if artists_verified != 1 else ''} "
                  f"verified, {remaining} skipped.")
        print(f"  {dim('Run again to resume from where you left off.')}")

    # ── footer ────────────────────────────────────────────────────────────
    if multi and all_stats:
        print_footer(all_stats)
    elif all_stats and not multi:
        # Single artist — just print a trailing newline for cleanliness
        print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # Top-level catch — handles Ctrl-C outside of --verify-ia loops
        # (e.g. during interactive picker, progress display, or fix prompts)
        print()
        print(f"\n  {dim('Interrupted.')}")
        sys.exit(130)
