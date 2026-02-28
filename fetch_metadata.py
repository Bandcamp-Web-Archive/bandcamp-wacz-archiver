#!/usr/bin/env python3
"""
fetch_metadata.py - fetch Bandcamp metadata and write the artist JSON + URL list.

Scrapes a Bandcamp artist page or individual release URL and always:
  - fetches individual track pages to capture unique per-track cover art
  - saves the artist JSON to  artists/{Artist Name} [{band_id}]/{Artist Name} [{band_id}].json
  - saves the URL list to     artists/{Artist Name} [{band_id}]/bandcamp-dump.lst
  - records band_id in the JSON (top-level key alongside the artist's release list)

Usage
─────
  # Single album:
  python fetch_metadata.py https://artist-name.bandcamp.com/album/album-name

  # Whole discography via artist page:
  python fetch_metadata.py https://artist-name.bandcamp.com

  # Multiple URLs at once:
  python fetch_metadata.py https://artist-name.bandcamp.com/album/album-1 \\
                           https://artist-name.bandcamp.com/album/album-2

  # With options:
  python fetch_metadata.py https://artist-name.bandcamp.com --delay 1000-3000 --debug
"""

from __future__ import annotations

import datetime
import json
import logging
import re
import argparse
import os
import sys
import time
import random
from typing import Union, List, Optional
from urllib.parse import urljoin, urlparse, urlunparse

import bs4
import demjson3
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import create_urllib3_context

from bandcamp_wacz.config import BC_MAX_RETRIES, BC_RETRY_DELAY, BC_REQUEST_TIMEOUT


# ── Constants ─────────────────────────────────────────────────────────────────

USER_AGENT = (
    "bandcamp-wacz-archiver/1.0 "
    "(https://github.com/Bandcamp-Web-Archive/bandcamp-wacz-archiver)"
)


# ── SSL adapter ───────────────────────────────────────────────────────────────

class SSLAdapter(HTTPAdapter):
    def __init__(self, ssl_context=None, **kwargs):
        self.ssl_context = ssl_context
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self.ssl_context
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["ssl_context"] = self.ssl_context
        return super().proxy_manager_for(*args, **kwargs)


# ── BandcampJSON - extracts all embedded JSON blobs from a page ───────────────

class BandcampJSON:
    def __init__(self, body: bs4.BeautifulSoup):
        self.body = body
        self.json_data: list[str] = []
        self.logger = logging.getLogger(__name__).getChild("JSON")

    def generate(self) -> list[str]:
        self._get_pagedata()
        self._get_js()
        return self.json_data

    def _get_pagedata(self) -> None:
        """Extract the #pagedata data-blob - contains band_id among other things."""
        div = self.body.find("div", {"id": "pagedata"})
        if div:
            blob = div.get("data-blob")
            if blob:
                self.json_data.append(blob)

    def _get_js(self) -> None:
        """Extract ld+json and data-tralbum script blobs."""
        raw: list[str] = []

        ld = self.body.find("script", {"type": "application/ld+json"})
        if ld and ld.string:
            raw.append(ld.string)

        for script in self.body.find_all("script"):
            if script.has_attr("data-tralbum"):
                raw.append(script["data-tralbum"])

        for item in raw:
            self.json_data.append(self._js_to_json(item))

    def _js_to_json(self, js_data: str) -> str:
        try:
            return demjson3.encode(demjson3.decode(js_data))
        except demjson3.JSONDecodeError as e:
            self.logger.warning("Failed to decode JS blob: %s", e)
            return "{}"


# ── Main Bandcamp scraper ─────────────────────────────────────────────────────

class Bandcamp:
    def __init__(self, delay_arg=None, retries: int | None = None, retry_delay: int | None = None):
        self.headers = {"User-Agent": USER_AGENT}
        self.soup: Optional[bs4.BeautifulSoup] = None
        self.logger = logging.getLogger(__name__).getChild("Bandcamp")
        self.delay_arg   = delay_arg
        self.max_retries = retries    if retries    is not None else BC_MAX_RETRIES
        self.retry_delay = retry_delay if retry_delay is not None else BC_RETRY_DELAY

        ctx = create_urllib3_context()
        ctx.load_default_certs()
        ctx.set_ciphers(":".join([
            "ECDHE+AESGCM", "ECDHE+CHACHA20", "DHE+AESGCM", "DHE+CHACHA20",
            "ECDH+AESGCM", "DH+AESGCM", "ECDH+AES", "DH+AES", "RSA+AESGCM",
            "RSA+AES", "!aNULL", "!eNULL", "!MD5", "!DSS", "!AESCCM",
        ]))
        self.session = requests.Session()
        self.session.mount("https://", SSLAdapter(ssl_context=ctx))

    # ── HTTP ──────────────────────────────────────────────────────────────────

    def _apply_delay(self) -> None:
        if not self.delay_arg:
            return
        lo, hi = 1000.0, 3000.0
        s = str(self.delay_arg)
        if "-" in s:
            try:
                a, b = s.split("-", 1)
                lo, hi = float(a), float(b)
            except ValueError:
                self.logger.warning("Invalid delay range '%s'. Using 1000-3000 ms.", s)
        else:
            try:
                lo = hi = float(s)
            except ValueError:
                self.logger.warning("Invalid delay value '%s'. Using 1000 ms.", s)
        if lo < 0 or hi < 0 or hi < lo:
            lo, hi = 1000.0, 3000.0
        sleep = random.uniform(lo, hi)
        self.logger.info("Delaying %.0f ms ...", sleep)
        time.sleep(sleep / 1000)

    def _get(self, url: str, **kwargs) -> requests.Response:
        """GET with delay + retry on 429 / connection errors."""
        self._apply_delay()
        last_exc: Optional[Exception] = None
        try:
            for attempt in range(self.max_retries + 1):
                try:
                    resp = self.session.get(url, headers=self.headers, timeout=BC_REQUEST_TIMEOUT, **kwargs)
                    if resp.status_code == 429:
                        wait = self.retry_delay * (attempt + 1)
                        self.logger.warning("Rate-limited (429). Retrying in %ds ... (%d/%d)", wait, attempt + 1, self.max_retries)
                        time.sleep(wait)
                        continue
                    resp.raise_for_status()
                    return resp
                except KeyboardInterrupt:
                    raise
                except requests.RequestException as exc:
                    last_exc = exc
                    if attempt < self.max_retries:
                        wait = self.retry_delay * (attempt + 1)
                        self.logger.warning("Request failed (%s). Retry in %ds ...", exc, wait)
                        time.sleep(wait)
                    else:
                        self.logger.error("All retries exhausted.")
                        raise
            raise last_exc or requests.RequestException("Request failed after all retries.")
        except KeyboardInterrupt:
            raise KeyboardInterrupt() from None

    def _make_soup(self, html: str) -> bs4.BeautifulSoup:
        try:
            return bs4.BeautifulSoup(html, "lxml")
        except bs4.FeatureNotFound:
            return bs4.BeautifulSoup(html, "html.parser")

    def _merged_json(self, soup: bs4.BeautifulSoup) -> dict:
        """Merge all JSON blobs from the page into one dict."""
        blobs = BandcampJSON(soup).generate()
        out: dict = {}
        for blob in blobs:
            try:
                out.update(json.loads(blob))
            except json.JSONDecodeError:
                pass
        return out

    # ── Artist / discography discovery ───────────────────────────────────────

    def get_album_urls_from_artist_page(self, artist_url: str) -> list[str]:
        parsed = urlparse(artist_url)
        if not parsed.path or parsed.path in ("/", ""):
            music_url = urlunparse(parsed._replace(path="/music"))
        else:
            music_url = artist_url

        self.logger.info("Discovering releases at: %s", music_url)
        resp = self._get(music_url)
        soup = self._make_soup(resp.text)

        # If Bandcamp redirected us straight to an album/track page (which
        # happens when an artist has only one release), there will be no music
        # grid — but the redirected URL *is* the release we want.
        final_path = urlparse(resp.url).path
        if any(final_path.startswith(p) for p in ("/album/", "/track/")):
            self.logger.info(
                "Artist page redirected to single release: %s", resp.url
            )
            return [resp.url]

        album_urls: set[str] = set()
        grid = soup.find("ol", {"id": "music-grid"})
        if not grid:
            self.logger.warning("No music grid found on artist page.")
            return []

        # Prefer the JSON data attribute (more reliable than scraping <li> hrefs)
        if "data-client-items" in grid.attrs:
            try:
                items = json.loads(grid["data-client-items"])
                for item in items:
                    if "page_url" in item:
                        album_urls.add(urljoin(music_url, item["page_url"]))
            except (json.JSONDecodeError, TypeError) as e:
                self.logger.error("Failed to parse data-client-items: %s", e)

        # Fallback: scrape anchor tags
        for a in grid.select("li.music-grid-item a"):
            href = a.get("href")
            if href:
                album_urls.add(urljoin(music_url, href))

        self.logger.info("Found %d release URL(s).", len(album_urls))
        return list(album_urls)

    # ── band_id extraction ────────────────────────────────────────────────────

    def get_band_id(self, soup: bs4.BeautifulSoup, page_json: dict) -> Optional[int]:
        """
        Extract band_id, trying every location Bandcamp uses across page types.

        Album pages:  tralbum["current"]["band_id"]
        Artist pages: data-band attribute {"id": ...}  or  lo_querystr band_id=
        """
        # 1. tralbum blob (album pages)
        band_id = page_json.get("current", {}).get("band_id")
        if band_id:
            return int(band_id)

        # 2. pagedata root "id"
        div = soup.find("div", {"id": "pagedata"})
        blob: dict = {}
        if div:
            blob_str = div.get("data-blob", "{}")
            try:
                blob = json.loads(blob_str)
            except json.JSONDecodeError:
                try:
                    blob = demjson3.decode(blob_str)
                except Exception:
                    blob = {}
            band_id = blob.get("id")
            if band_id:
                return int(band_id)

        # 3. data-band attribute (artist pages, most reliable when logged out)
        tag = soup.find(attrs={"data-band": True})
        if tag:
            try:
                import html as _html
                band_data = json.loads(_html.unescape(tag["data-band"]))
                band_id = band_data.get("id")
                if band_id:
                    return int(band_id)
            except Exception:
                pass

        # 4. lo_querystr in pagedata blob (artist/music pages)
        lo = blob.get("lo_querystr", "") or ""
        m = re.search(r"band_id=(\d+)", lo)
        if m:
            return int(m.group(1))

        return None

    # ── Album parsing ─────────────────────────────────────────────────────────

    def parse(self, url: str) -> Optional[dict]:
        """
        Fetch *url* and return a fully populated album dict, including
        band_id and per-track metadata with individual track page fetches.
        """
        try:
            try:
                resp = self._get(url)
            except requests.RequestException as e:
                self.logger.error("Request failed for %s: %s", url, e)
                return None

            self.soup = self._make_soup(resp.text)
            page_json = self._merged_json(self.soup)

            if not page_json.get("trackinfo"):
                self.logger.error("No trackinfo found at %s - may not be an album/track page.", url)
                page_json["trackinfo"] = []

            tracks = page_json["trackinfo"]

            # Release date - try several locations Bandcamp uses
            release_date = (
                page_json.get("album_release_date")
                or page_json.get("current", {}).get("release_date")
                or page_json.get("embed_info", {}).get("item_public")
            )

            # Title
            title = page_json.get("current", {}).get("title")
            if not title:
                try:
                    title = tracks[0]["title"]
                except (IndexError, KeyError, TypeError):
                    pass
            if not title:
                ld = self.soup.find("script", {"type": "application/ld+json"})
                if ld and ld.string:
                    try:
                        title = json.loads(ld.string).get("name", "Untitled")
                    except json.JSONDecodeError:
                        title = "Untitled"
                else:
                    title = "Untitled"

            artist      = page_json.get("artist", "Unknown Artist")
            label       = self._get_label(self.soup, page_json)
            art_url     = self._get_art_url(self.soup)          # _0.jpg variant
            band_id     = self.get_band_id(self.soup, page_json)

            album = {
                "url": url,
                "title": title,
                "artist": artist,
                "label": label,
                "band_id": band_id,
                "classification": self._get_classification(page_json),
                "tags": page_json.get("keywords", []),
                "item_id": page_json.get("current", {}).get("id"),
                "art_id": page_json.get("art_id"),
                "is_preorder": page_json.get("is_preorder"),
                "datePublished": release_date,
                "about": self._get_about(),
                "credits": self._get_credits(),
                "license": self._get_license(self.soup),
                "coverUrl_0": art_url.replace(".jpg", "") if art_url else None,
                "trackinfo": [],
            }

            base_url = urlparse(url)._replace(query="", fragment="").geturl()

            for i, track_data in enumerate(tracks):
                if track_data.get("file"):
                    print(f"    -> Track {i + 1}/{len(tracks)}: {track_data.get('title')}")
                    album["trackinfo"].append(
                        self._get_track_metadata(track_data, art_url, base_url, artist, label)
                    )

            return album

        except KeyboardInterrupt:
            raise KeyboardInterrupt() from None

    # ── Track metadata (always fetches individual track page) ─────────────────

    def _get_track_metadata(
        self,
        track: dict,
        album_art_url: str,
        base_url: str,
        album_artist: str,
        album_label: Optional[str],
    ) -> dict:
        duration_s = track.get("duration", 0)
        if duration_s >= 3600:
            h = int(duration_s // 3600)
            m = int((duration_s % 3600) // 60)
            s = int(duration_s % 60)
            duration_str = f"{h:02d}:{m:02d}:{s:02d}"
        else:
            m = int(duration_s // 60)
            s = int(duration_s % 60)
            duration_str = f"{m:02d}:{s:02d}"

        # Lyrics from album page
        track_num = track.get("track_num")
        lyrics = None
        if track_num:
            row = self.soup.select_one(f"tr#lyrics_row_{track_num} div")
            if row:
                lyrics = self._text_with_linebreaks(row)

        # Clean up title (some tracks embed "Artist - Title")
        title = track.get("title", "Untitled")
        artist = track.get("artist")
        if artist and title.startswith(f"{artist} - "):
            title = title[len(artist) + 3:]
        if artist is None:
            artist = album_artist

        track_page_link = track.get("title_link")
        full_track_url = urljoin(base_url, track_page_link) if track_page_link else None

        metadata: dict = {
            "title": title,
            "duration": duration_str,
            "lyrics": lyrics,
            "label": album_label,
            "track_id": track.get("id"),
            "track_num": str(track.get("track_num", "N/A")),
            "artist": artist,
            "url": full_track_url,
        }

        # ── Fetch individual track page for unique art / credits ───────────────
        track_cover_url = album_art_url
        track_art_id    = None
        track_about     = None
        track_credits   = None
        track_license   = None

        if full_track_url:
            try:
                track_resp = self._get(full_track_url)
                track_soup = self._make_soup(track_resp.text)
                track_json = self._merged_json(track_soup)

                specific_art = self._get_art_url(track_soup)
                if specific_art and specific_art != "Album art not found":
                    track_cover_url = specific_art

                track_art_id  = track_json.get("art_id")
                track_about   = self._text_with_linebreaks(track_soup.select_one(".tralbumData.tralbum-about"))
                track_credits = self._text_with_linebreaks(track_soup.select_one(".tralbumData.tralbum-credits"))
                track_license = self._get_license(track_soup)

                track_label = self._get_label(track_soup, track_json)
                if track_label:
                    metadata["label"] = track_label

            except Exception as e:
                self.logger.warning("Could not fetch track page '%s': %s", title, e)

        metadata["trackCoverUrl_0"] = track_cover_url.replace(".jpg", "") if track_cover_url else None
        metadata["art_id"]          = track_art_id
        metadata["about"]           = track_about
        metadata["credits"]         = track_credits
        metadata["license"]         = track_license

        return metadata

    # ── HTML extraction helpers ───────────────────────────────────────────────

    def _get_art_url(self, soup: bs4.BeautifulSoup) -> str:
        """Return the _0.jpg cover URL, or 'Album art not found'."""
        try:
            link = soup.find(id="tralbumArt").find("a")
            if link and link.get("href"):
                href = link["href"]
                base, ext = href.rsplit(".", 1)
                if "_" in base:
                    parts = base.rsplit("_", 1)
                    if parts[1].isdigit():
                        return f"{parts[0]}_0.{ext}"
                return href
        except (AttributeError, IndexError, ValueError):
            self.logger.warning("Could not find album art.")
        return "Album art not found"

    def _get_classification(self, page_json: dict) -> str:
        try:
            nyp = self.soup.find("span", class_="buyItemExtra buyItemNyp secondaryText")
            if nyp and ("name your price" in nyp.text.lower() or "値段を決めて下さい" in nyp.text):
                return "nyp"
            free_btn = self.soup.select_one("h4.ft.compound-button.main-button button.download-link.buy-link")
            if free_btn and ("free download" in free_btn.text.lower() or "無料ダウンロード" in free_btn.text.lower()):
                return "free"
            if any(t.get("free_album_download") for t in page_json.get("trackinfo", [])):
                return "free"
            if page_json.get("current", {}).get("minimum_price") == 0:
                return "nyp"
        except Exception as e:
            self.logger.warning("Could not determine classification: %s", e)
        return "paid"

    def _get_label(self, soup: bs4.BeautifulSoup, page_json: dict) -> Optional[str]:
        try:
            el = soup.select_one("a.back-to-label-link span.back-link-text")
            if el:
                return el.get_text(separator="\n").split("\n")[-1].strip()
        except Exception as e:
            self.logger.warning("Could not extract label: %s", e)
        return page_json.get("item_sellers", {}).get(
            str(page_json.get("band_id")), {}
        ).get("name")

    def _text_with_linebreaks(self, element: Optional[bs4.element.Tag]) -> Optional[str]:
        if not element:
            return None
        for br in element.find_all("br"):
            br.replace_with("<<BR>>")
        raw = re.sub(r"(\n\s*)+", " ", element.get_text())
        lines = [l.strip() for l in raw.split("<<BR>>") if l.strip()]
        return "\n".join(lines)

    def _get_about(self) -> Optional[str]:
        return self._text_with_linebreaks(self.soup.select_one(".tralbumData.tralbum-about"))

    def _get_credits(self) -> Optional[str]:
        return self._text_with_linebreaks(self.soup.select_one(".tralbumData.tralbum-credits"))

    def _get_license(self, soup: bs4.BeautifulSoup) -> Optional[str]:
        try:
            div = soup.select_one("#license.info.license")
            if div:
                span = div.find("span")
                if span:
                    span.decompose()
                return div.text.strip()
        except Exception as e:
            self.logger.warning("Could not extract license: %s", e)
        return None


# ── Filename / path helpers ───────────────────────────────────────────────────

def create_safe_filename(name: str) -> str:
    s = re.sub(r"[\x00-\x1f\x7f\u200b-\u200d\ufeff]", "", name)
    s = s.replace(" | ", " - ")
    s = re.sub(r'[\\/?%*:|"<>]+', "-", s)
    s = re.sub(r" +", " ", s)
    s = re.sub(r"^\.+", "", s)
    s = re.sub(r"-+", "-", s)
    s = s.strip()
    while s and (s.endswith(".") or s.endswith(" ")):
        s = s[:-1]
    return s


def artist_folder_name(artist: str, band_id: Optional[int]) -> str:
    safe = create_safe_filename(artist)
    if band_id is not None:
        return f"{safe} [{band_id}]"
    return safe


def is_artist_page(url: str) -> bool:
    path = urlparse(url).path
    return path in ("", "/", "/music", "/music/")


# ── Output helpers ────────────────────────────────────────────────────────────

def save_json(data: dict, path: str) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        print(f"--- Saved JSON: {path} ---")
    except (IOError, TypeError) as e:
        print(f"--- Error saving JSON to {path}: {e} ---")


def save_url_list(urls: list[str], path: str) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            if urls:
                f.write("\n".join(urls) + "\n")
        print(f"--- Saved URL list: {path} ---")
    except IOError as e:
        print(f"--- Error saving URL list to {path}: {e} ---")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="fetch_metadata.py",
        description="Fetch Bandcamp metadata and write the artist JSON + URL list.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "urls",
        nargs="+",
        help="One or more Bandcamp URLs (artist page or direct album/track URLs).",
    )
    parser.add_argument(
        "-d", "--delay",
        type=str,
        default=None,
        metavar="MS",
        help=(
            "Delay between requests in ms.  "
            "Single value (e.g. '2000') or range (e.g. '1000-3000')."
        ),
    )
    parser.add_argument(
        "-r", "--retries",
        type=int,
        default=None,
        help=f"Max retries on failed requests (default: BC_MAX_RETRIES from env, currently {BC_MAX_RETRIES}).",
    )
    parser.add_argument(
        "-rd", "--retry-delay",
        type=int,
        default=None,
        metavar="SECS",
        help=f"Initial retry delay in seconds, multiplied by attempt number (default: BC_RETRY_DELAY from env, currently {BC_RETRY_DELAY}).",
    )
    parser.add_argument(
        "--band-id",
        type=int,
        default=None,
        metavar="ID",
        help="Override band_id instead of extracting it from the page (useful when the artist page does not expose it).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose debug logging.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logger = logging.getLogger(__name__)

    scraper = Bandcamp(
        delay_arg=args.delay,
        retries=args.retries,
        retry_delay=args.retry_delay,
    )

    # ── Discover all album URLs ───────────────────────────────────────────────
    album_urls: set[str] = set()
    for url in args.urls:
        if is_artist_page(url):
            print(f"\nDiscovering releases on artist page: {url}")
            found = scraper.get_album_urls_from_artist_page(url)
            album_urls.update(found)
        else:
            album_urls.add(url)

    unique_urls = sorted(album_urls)
    if not unique_urls:
        print("No album/track URLs found from the provided inputs.")
        sys.exit(0)

    # ── Determine primary artist name and band_id from the first URL ──────────
    print(f"\nDetermining primary artist from: {args.urls[0]}")
    primary_artist = "Unknown Artist"
    primary_band_id: Optional[int] = None

    try:
        resp = scraper._get(args.urls[0])
        soup = scraper._make_soup(resp.text)

        el = soup.select_one("p#band-name-location span.title")
        if el and el.text:
            primary_artist = el.text.strip()
            print(f"  Artist (from HTML): {primary_artist}")
        else:
            # Fall back to parsing the first album page
            first_data = scraper.parse(unique_urls[0])
            if first_data and first_data.get("artist"):
                primary_artist = first_data["artist"]
                print(f"  Artist (from first album): {primary_artist}")
            else:
                print("  Could not determine artist name. Using 'Unknown Artist'.")

        # band_id: honour --band-id override, then try the primary page,
        # then fall back to the first album URL (artist pages sometimes lack it).
        if args.band_id:
            primary_band_id = args.band_id
            print(f"  band_id: {primary_band_id} (from --band-id)")
        else:
            page_json_first = scraper._merged_json(soup)
            primary_band_id = scraper.get_band_id(soup, page_json_first)
            if primary_band_id:
                print(f"  band_id: {primary_band_id}")
            else:
                print("  band_id not found on primary page, trying first album URL...")
                try:
                    album_resp = scraper._get(unique_urls[0])
                    album_soup = scraper._make_soup(album_resp.text)
                    album_json = scraper._merged_json(album_soup)
                    primary_band_id = scraper.get_band_id(album_soup, album_json)
                except Exception:
                    primary_band_id = None
                if primary_band_id:
                    print(f"  band_id: {primary_band_id} (from first album)")
                else:
                    print("  Warning: could not extract band_id from any page.")

    except Exception as e:
        print(f"Fatal: could not fetch first URL to determine artist: {e}")
        sys.exit(1)

    # ── Create artist output folder ───────────────────────────────────────────
    folder_name = artist_folder_name(primary_artist, primary_band_id)
    artists_dir = os.path.join(os.getcwd(), "artists")
    artist_folder = os.path.join(artists_dir, folder_name)
    os.makedirs(artist_folder, exist_ok=True)
    print(f"\nOutput folder: {artist_folder}")

    json_name    = f"{create_safe_filename(folder_name)}.json"
    json_path    = os.path.join(artist_folder, json_name)
    partial_path = json_path + ".partial"

    # ── Resume from partial file if one exists ────────────────────────────────
    all_releases: list[dict] = []
    already_parsed: set[int] = set()   # item_ids already in the partial file

    if os.path.exists(partial_path):
        try:
            with open(partial_path, encoding="utf-8") as f:
                partial_data = json.load(f)
            # Partial file has the same structure as the final JSON
            for key, releases in partial_data.items():
                if key.startswith("_") or not isinstance(releases, list):
                    continue
                all_releases = releases
                already_parsed = {r["item_id"] for r in releases if r.get("item_id")}
            print(
                f"Resuming from partial file — {len(all_releases)} release(s) already fetched, "
                f"{len(unique_urls) - len(already_parsed)} remaining."
            )
        except Exception as e:
            print(f"Warning: could not read partial file ({e}) — starting fresh.")
            all_releases = []
            already_parsed = set()

    def _write_partial() -> None:
        """Atomically write current progress to the partial file."""
        tmp = partial_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    {primary_artist: all_releases, "_band_id": primary_band_id},
                    f, indent=4, ensure_ascii=False,
                )
            os.replace(tmp, partial_path)
        except Exception as e:
            print(f"Warning: could not write partial file: {e}")

    # ── Parse each album ──────────────────────────────────────────────────────
    skipped_urls: list[str] = []

    for i, url in enumerate(unique_urls, 1):
        # Check if this release was already fetched in a previous interrupted run
        # We match by URL since we don't know the item_id yet for skipped items.
        # A more precise check happens below once we have the parsed data.
        already_fetched_urls = {r.get("url") for r in all_releases}
        if url in already_fetched_urls:
            print(f"\n--- Release {i}/{len(unique_urls)}: {url} ---")
            print("  -> Already fetched in previous run — skipping.")
            continue

        print(f"\n--- Release {i}/{len(unique_urls)}: {url} ---")
        data = scraper.parse(url)

        if not data:
            print("  -> Failed to parse, skipping.")
            skipped_urls.append(url)
            continue

        # Also skip by item_id in case the URL changed (redirect, etc.)
        if data.get("item_id") in already_parsed:
            print("  -> Already fetched (matched by item_id) — skipping.")
            continue

        all_releases.append(data)
        if data.get("item_id"):
            already_parsed.add(data["item_id"])

        # Write progress after every release so an interrupt loses at most one
        _write_partial()

    # ── Write final outputs and clean up partial file ─────────────────────────
    if all_releases:
        output = {
            primary_artist: all_releases,
            "_band_id": primary_band_id,
        }
        save_json(output, json_path)

    # Remove partial file now that the real JSON is written
    try:
        if os.path.exists(partial_path):
            os.remove(partial_path)
    except Exception as e:
        print(f"Warning: could not remove partial file: {e}")

    # Write .lst with all successfully parsed URLs (including pre-orders and
    # description-only releases — they have archival value)
    lst_urls = [u for u in unique_urls if u not in set(skipped_urls)]

    if lst_urls:
        save_url_list(lst_urls, os.path.join(artist_folder, "bandcamp-dump.lst"))
    else:
        print("\nNo URLs to write to the URL list.")

    if skipped_urls:
        print(f"\nSkipped {len(skipped_urls)} URL(s) (parse failures):")
        for u in skipped_urls:
            print(f"  {u}")

    print(f"\nDone.  {len(all_releases)} release(s) processed.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
