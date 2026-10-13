/**
 * Application boot: resolve the playback SoundFont, create the alphaTab
 * engine, then wire the three panels together — explorer clicks feed the
 * conductor, the transport drives the conductor's api. Load order (see
 * index.html) guarantees the Guitarz namespace modules exist before this runs.
 */
(async function () {
  const Guitarz = window.Guitarz;

  /** Font used when the catalog can't be fetched; fetch_vendor.sh always installs it. */
  const FALLBACK_SOUNDFONT = "/vendor/alphatab/soundfont/sonivox.sf2";

  /**
   * Fetch the SoundFont catalog for the transport menu.
   *
   * Boot must never hinge on this: if the request fails, the app still plays
   * with the fallback font and the menu simply stays empty/disabled.
   *
   * @returns {Promise<{default: (string|null), fonts: Array<{file: string, label: string, url: string}>}>}
   *          the catalog, or an empty one on any failure.
   */
  async function fetchSoundFonts() {
    try {
      const res = await fetch(window.GuitarzStatic ? "/soundfonts.json" : "/api/soundfonts");
      if (!res.ok) throw new Error("server returned " + res.status);
      return await res.json();
    } catch (e) {
      console.warn("SoundFont catalog unavailable; using fallback", e);
      return { default: null, fonts: [] };
    }
  }

  // Boot font: the viewer's saved menu choice if it still exists, else the
  // server default — chosen before init so the synth loads exactly one font.
  const catalog = await fetchSoundFonts();
  Guitarz.info.setSoundFonts(catalog.fonts);
  const saved = Guitarz.transport.savedSoundFont();
  const chosen =
    catalog.fonts.find((f) => f.file === saved) ||
    catalog.fonts.find((f) => f.file === catalog.default) ||
    null;

  const api = Guitarz.conductor.init(chosen ? chosen.url : FALLBACK_SOUNDFONT);
  Guitarz.transport.init(api, catalog.fonts, chosen ? chosen.file : null);
  Guitarz.fretboard.init(api); // after conductor.init: needs its track colors
  Guitarz.explorer.init((file) => Guitarz.conductor.loadFile(file));

  // Deep link: /?open=<relative path> auto-loads a song. Useful for
  // bookmarking a piece and for the headless browser smoke test.
  const openPath = new URLSearchParams(location.search).get("open");
  if (openPath && api) {
    Guitarz.conductor.loadFile({ path: openPath, name: openPath.split("/").pop() });
  }
})();
