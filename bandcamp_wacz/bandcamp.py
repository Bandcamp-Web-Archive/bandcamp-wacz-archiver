"""
bandcamp.py - minimal Bandcamp page fetcher for the crawl pipeline.

Fetches only what archive.py needs before handing off to Browsertrix:
band_id, artist name, high-quality cover URL, item_id, title, preorder status.

Full metadata parsing (trackinfo, lyrics, credits, etc.) lives in fetch_metadata.py.
"""

from __future__ import annotations

import json
import logging
import re
import random
import time
from typing import Optional
from urllib.parse import urlparse

import bs4
import demjson3
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import create_urllib3_context

from .config import BC_REQUEST_DELAY, BC_MAX_RETRIES, BC_RETRY_DELAY, BC_REQUEST_TIMEOUT, USER_AGENT

logger = logging.getLogger(__name__)


# ── SSL adapter ───────────────────────────────────────────────────────────────

_DEFAULT_CIPHERS = ":".join([
    "ECDHE+AESGCM", "ECDHE+CHACHA20", "DHE+AESGCM", "DHE+CHACHA20",
    "ECDH+AESGCM", "DH+AESGCM", "ECDH+AES", "DH+AES", "RSA+AESGCM",
    "RSA+AES", "!aNULL", "!eNULL", "!MD5", "!DSS", "!AESCCM",
])


class _SSLAdapter(HTTPAdapter):
    def __init__(self, ssl_context=None, **kwargs):
        self.ssl_context = ssl_context
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self.ssl_context
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["ssl_context"] = self.ssl_context
        return super().proxy_manager_for(*args, **kwargs)


def _build_session() -> requests.Session:
    ctx = create_urllib3_context()
    ctx.load_default_certs()
    ctx.set_ciphers(_DEFAULT_CIPHERS)
    session = requests.Session()
    session.mount("https://", _SSLAdapter(ssl_context=ctx))
    return session


_session = _build_session()


# ── Request delay ─────────────────────────────────────────────────────────────

def _apply_delay() -> None:
    s = BC_REQUEST_DELAY
    if "-" in s:
        try:
            lo, hi = s.split("-", 1)
            ms = random.uniform(float(lo), float(hi))
        except ValueError:
            ms = random.uniform(1000, 3000)
    else:
        try:
            ms = float(s)
        except ValueError:
            ms = 1000
    time.sleep(ms / 1000)


# ── HTTP GET with retries ─────────────────────────────────────────────────────

def fetch_url(url: str, max_retries: int | None = None, retry_delay: int | None = None) -> requests.Response:
    """GET *url* with exponential back-off on 429 and connection errors.
    Defaults are read from config (BC_MAX_RETRIES, BC_RETRY_DELAY, BC_REQUEST_TIMEOUT).
    """
    _max_retries  = max_retries  if max_retries  is not None else BC_MAX_RETRIES
    _retry_delay  = retry_delay  if retry_delay  is not None else BC_RETRY_DELAY
    _apply_delay()
    last_exc: Exception | None = None
    for attempt in range(_max_retries + 1):
        try:
            resp = _session.get(url, headers={"User-Agent": USER_AGENT}, timeout=BC_REQUEST_TIMEOUT)
            if resp.status_code == 429:
                wait = _retry_delay * (attempt + 1)
                logger.warning("Rate-limited (429). Retrying in %ds ...", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < _max_retries:
                wait = _retry_delay * (attempt + 1)
                logger.warning("Request failed (%s). Retry in %ds ...", exc, wait)
                time.sleep(wait)
    raise last_exc or requests.RequestException(f"All retries exhausted for {url}")


# ── Page parsing ──────────────────────────────────────────────────────────────

def _pagedata_blob(soup: bs4.BeautifulSoup) -> dict:
    div = soup.find("div", {"id": "pagedata"})
    if not div:
        return {}
    raw = div.get("data-blob", "{}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        try:
            return demjson3.decode(raw)
        except Exception:
            return {}


def _tralbum_blob(soup: bs4.BeautifulSoup) -> dict:
    script = soup.find("script", attrs={"data-tralbum": True})
    if not script:
        return {}
    raw = script["data-tralbum"]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        try:
            return demjson3.decode(raw)
        except Exception:
            return {}


def parse_page(url: str) -> dict:
    """
    Fetch *url* and return a minimal dict for the crawl pipeline:

        band_id      int | None
        artist       str
        cover_url_0  str | None   - _0 variant (full resolution, no extension)
        item_id      int | None
        title        str
        is_preorder  bool
    """
    resp = fetch_url(url)
    try:
        soup = bs4.BeautifulSoup(resp.text, "lxml")
    except bs4.FeatureNotFound:
        soup = bs4.BeautifulSoup(resp.text, "html.parser")

    pagedata = _pagedata_blob(soup)
    tralbum  = _tralbum_blob(soup)

    logger.debug("pagedata keys: %s", list(pagedata.keys()))
    logger.debug("tralbum keys: %s",  list(tralbum.keys()))

    # band_id: try every location Bandcamp uses, in order of reliability.
    # Album pages:  tralbum["current"]["band_id"]  or  fan_tralbum_data["band_id"]
    # Artist pages: data-band attribute {"id": ...}  or  lo_querystr band_id= param
    band_id: int | None = (
        (tralbum.get("current") or {}).get("band_id")
        or (pagedata.get("fan_tralbum_data") or {}).get("band_id")
        or pagedata.get("id")
    )
    if not band_id:
        # data-band attribute: <script data-band='{"id": 12345, ...}'> (artist pages)
        import re as _re, html as _html
        tag = soup.find(attrs={"data-band": True})
        if tag:
            try:
                import json as _json
                band_data = _json.loads(_html.unescape(tag["data-band"]))
                band_id = band_data.get("id")
            except Exception:
                pass
    if not band_id:
        # lo_querystr: "?...&band_id=12345&..." (present on artist/music pages)
        lo = pagedata.get("lo_querystr", "") or ""
        m = _re.search(r'band_id=(\d+)', lo)
        if m:
            band_id = int(m.group(1))
    if band_id:
        band_id = int(band_id)

    artist: str = (
        tralbum.get("artist")
        or pagedata.get("artist")
        or _artist_from_html(soup)
        or "Unknown Artist"
    )

    current    = tralbum.get("current") or {}
    item_id    = current.get("id")
    title      = current.get("title") or "Untitled"
    is_preorder = bool(tralbum.get("is_preorder"))

    return {
        "band_id":     band_id,
        "artist":      artist,
        "cover_url_0": _cover_url_0(soup),
        "item_id":     item_id,
        "title":       title,
        "is_preorder": is_preorder,
    }


def _artist_from_html(soup: bs4.BeautifulSoup) -> str | None:
    el = soup.select_one("p#band-name-location span.title")
    return el.text.strip() if el and el.text else None


def _cover_url_0(soup: bs4.BeautifulSoup) -> str | None:
    """
    Return the _0 (full-resolution, no extension) cover URL from the
    album art link, e.g. https://f4.bcbits.com/img/a0737321838_0
    """
    try:
        art_div = soup.find(id="tralbumArt")
        if not art_div:
            return None
        link = art_div.find("a")
        if not link or not link.get("href"):
            return None
        href: str = link["href"]
        no_ext = re.sub(r"\.\w+$", "", href)
        return re.sub(r"_\d+$", "_0", no_ext)
    except Exception as exc:
        logger.warning("Could not extract cover URL: %s", exc)
        return None


# ── Filename / path helpers ───────────────────────────────────────────────────

def create_safe_filename(name: str) -> str:
    """
    Sanitise *name* for use as a filesystem path component.
    Strips control characters and shell-unsafe characters, collapses whitespace.
    Logic kept in sync with fetch_metadata.py so folder names are consistent.
    """
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


def truncate_filename(display_name: str, suffix: str, max_bytes: int, style: str) -> str:
    """
    Ensure *display_name* + *suffix* fits within *max_bytes* when UTF-8 encoded.

    *display_name* is the title portion (without extension).
    *suffix*       is everything after the title, e.g. ' [3853844384].wacz'.
    *style*        is one of: 'end', 'middle', 'hash'.

    Returns the full filename (display_name + suffix), truncated if necessary.
    """
    import hashlib

    full = display_name + suffix
    if len(full.encode("utf-8")) <= max_bytes:
        return full

    suffix_bytes = len(suffix.encode("utf-8"))
    title_budget = max_bytes - suffix_bytes

    if title_budget <= 0:
        # Suffix alone exceeds limit — nothing sensible to do but warn and return as-is
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "Suffix '%s' alone (%d bytes) exceeds FILENAME_MAX_BYTES=%d — filename not truncated.",
            suffix, suffix_bytes, max_bytes,
        )
        return full

    if style == "hash":
        # Replace title entirely with a short SHA-1 hex digest
        digest = hashlib.sha1(display_name.encode("utf-8")).hexdigest()[:16]
        return digest + suffix

    elif style == "middle":
        # Keep as much of the start and end of the title as possible,
        # joining them with an ellipsis in the middle.
        ellipsis = "..."
        ellipsis_bytes = len(ellipsis.encode("utf-8"))
        half_budget = (title_budget - ellipsis_bytes) // 2

        # Encode and slice by bytes, then decode safely
        title_encoded = display_name.encode("utf-8")
        start = title_encoded[:half_budget].decode("utf-8", errors="ignore").rstrip()
        end   = title_encoded[-half_budget:].decode("utf-8", errors="ignore").lstrip()
        truncated_title = start + ellipsis + end

    else:
        # style == "end" (default): cut from the right
        title_encoded = display_name.encode("utf-8")
        truncated_title = title_encoded[:title_budget].decode("utf-8", errors="ignore").rstrip()

    # Strip trailing punctuation that looks odd after truncation
    truncated_title = truncated_title.rstrip(" -–—_")
    return truncated_title + suffix


def artist_folder_name(artist: str, band_id: int | None) -> str:
    """Return the canonical artist folder name, e.g. 'Akira Umeda [3774983561]'."""
    safe = create_safe_filename(artist)
    return f"{safe} [{band_id}]" if band_id is not None else safe


def subdomain_from_url(url: str) -> str:
    """Extract the subdomain from a Bandcamp URL, e.g. 'artist-name'."""
    host = urlparse(url).hostname or ""
    return host.split(".")[0]
