/**
 * bandcamp.js - custom Browsertrix behavior for Bandcamp album and track pages.
 *
 * On album pages: force-fetches each track's MP3 in full via fetch({ mode:
 * 'no-cors' }) so Browsertrix captures complete HTTP 200 responses rather than
 * partial 206 chunks from autoplay. Clicks play at the end for the snapshot.
 *
 * On track pages that are part of an album (the page has a "from <album> by
 * <artist>" header): skips the audio fetch — the MP3 was already captured
 * from the album page. The page is still visited so its HTML, artwork,
 * credits, and license are recorded in the WACZ.
 *
 * On standalone single pages (no "from … by …" header): behaves the same as
 * an album page and fetches the audio in full.
 *
 * trackDelayMs (default: 100) — milliseconds to wait between consecutive track
 * fetches. Passed from Python via --behaviorOpts '{"trackDelayMs": N}' and
 * automatically increased after each rate-limit retry.
 */
class BandcampBehavior {
  static id = "BandcampPlay";
  static trackDelayMs = 100;

  static isMatch() {
    return window.location.hostname.endsWith("bandcamp.com");
  }

  // Receives opts from --behaviorOpts JSON passed by Browsertrix.
  static async init(opts) {
    if (opts && typeof opts.trackDelayMs === "number") {
      BandcampBehavior.trackDelayMs = opts.trackDelayMs;
    }
    return true;
  }

  async *run() {
    const tralbumScript = document.querySelector("script[data-tralbum]");
    let data = null;

    if (tralbumScript) {
      try {
        data = JSON.parse(tralbumScript.getAttribute("data-tralbum"));
      } catch (err) {
        yield `Error parsing track metadata: ${err.message}`;
      }
    }

    // Skip the audio fetch on track pages that belong to an album — the MP3
    // was already captured when the album page was crawled. Standalone singles
    // have no "from <album> by <artist>" header, so they go through the normal
    // fetch path below.
    const albumTitle = document.querySelector("#name-section h3.albumTitle");
    if (albumTitle && albumTitle.textContent.trim().startsWith("from ")) {
      yield "Track page (part of album) — skipping audio fetch, already captured from album page.";
      return;
    }

    if (data && data.trackinfo) {
      yield `Extracting audio stream URLs from page metadata... (inter-track delay: ${BandcampBehavior.trackDelayMs}ms)`;
      for (let i = 0; i < data.trackinfo.length; i++) {
        const track = data.trackinfo[i];

        if (track.file && track.file["mp3-128"]) {
          const audioUrl = track.file["mp3-128"];
          yield `[${i + 1}/${data.trackinfo.length}] Fetching: ${track.title}`;

          try {
            // no-cors bypasses the cross-origin block so Browsertrix
            // can record the full response body from t4.bcbits.com.
            await fetch(audioUrl, { mode: "no-cors" });
            yield `✓ Captured track ${i + 1}`;
          } catch (e) {
            yield `✗ Failed track ${i + 1}: ${e.message}`;
          }

          // Pace requests to avoid triggering Bandcamp's rate limiter.
          // Delay is skipped after the last track.
          if (i < data.trackinfo.length - 1 && BandcampBehavior.trackDelayMs > 0) {
            await new Promise(r => setTimeout(r, BandcampBehavior.trackDelayMs));
          }
        }
      }
    } else if (!data) {
      yield "No track metadata found on this page.";
    }

    // Click play to capture UI in playing state for the final snapshot
    const btn = document.querySelector(".playbutton");
    if (btn) {
      btn.click();
      yield "Clicked play button for final snapshot.";
      await new Promise(r => setTimeout(r, 4000));
    }
  }
}
