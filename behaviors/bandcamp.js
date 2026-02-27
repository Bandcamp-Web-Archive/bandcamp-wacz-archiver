/**
 * bandcamp.js - custom Browsertrix behavior for Bandcamp album pages.
 *
 * Instead of relying on the built-in autoplay behavior (which only captures
 * partial HTTP 206 chunks), this behavior reads the track stream URLs directly
 * from the embedded JSON and force-fetches each one with fetch({ mode: 'no-cors' }).
 * This causes the browser to download the complete MP3 as a single HTTP 200
 * response, which Browsertrix captures in full and ReplayWeb.page can serve
 * correctly when replaying.
 *
 * The play button is clicked at the end purely to capture the UI in a playing
 * state for the final page snapshot.
 */
class BandcampBehavior {
  static id = "BandcampPlay";

  static isMatch() {
    return window.location.hostname.endsWith("bandcamp.com");
  }

  // Required by Browsertrix 1.11.4
  static async init() {
    return true;
  }

  async *run() {
    yield "Extracting audio stream URLs from page metadata...";

    const tralbumScript = document.querySelector("script[data-tralbum]");

    if (tralbumScript) {
      try {
        const data = JSON.parse(tralbumScript.getAttribute("data-tralbum"));

        if (data && data.trackinfo) {
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
            }
          }
        }
      } catch (err) {
        yield `Error parsing track metadata: ${err.message}`;
      }
    } else {
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
