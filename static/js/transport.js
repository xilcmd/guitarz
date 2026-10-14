/**
 * Transport bar: play/pause/stop, count-in, loop, speed, and the position
 * readout (bar number, elapsed/total time, progress bar).
 *
 * Everything here is a thin mapping between DOM controls and alphaTab player
 * properties; no playback state is duplicated locally except the cached song
 * end time needed to convert progress-bar clicks into seek positions.
 */
(function () {
  const Guitarz = (window.Guitarz = window.Guitarz || {});

  const btnPlay = document.getElementById("btn-play");
  const btnStop = document.getElementById("btn-stop");
  const btnCountIn = document.getElementById("btn-countin");
  const btnTails = document.getElementById("btn-tails");
  const btnLoop = document.getElementById("btn-loop");
  const btnClearLoop = document.getElementById("btn-clearloop");
  const speed = document.getElementById("speed");
  const speedVal = document.getElementById("speed-val");
  const btnTransposeDown = document.getElementById("btn-transpose-down");
  const btnTransposeUp = document.getElementById("btn-transpose-up");
  const transposeVal = document.getElementById("transpose-val");
  const btnTransposeAuto = document.getElementById("btn-transpose-auto");
  const posBar = document.getElementById("pos-bar");
  const posTime = document.getElementById("pos-time");
  const progress = document.getElementById("progress");
  const progressFill = document.getElementById("progress-fill");
  const soundfontSel = document.getElementById("soundfont");

  /** localStorage key remembering the viewer's SoundFont menu choice. */
  const SOUNDFONT_KEY = "guitarz.soundfont";

  let api = null;
  let endTime = 0;
  /** True after Tab navigation, false after any pointer press; see spaceMeansPlay. */
  let navigatingByKeyboard = false;

  /**
   * Read the saved SoundFont choice, if any.
   *
   * Storage can be unavailable (private windows, blocked site data) and then
   * throws on access; the menu choice is a convenience, so any failure simply
   * means "no saved choice" and the server default is used.
   *
   * @returns {string|null} the saved font file name, or null.
   */
  function savedSoundFont() {
    try {
      return localStorage.getItem(SOUNDFONT_KEY);
    } catch (e) {
      return null;
    }
  }

  /**
   * Remember the SoundFont choice for the next page load (best effort).
   *
   * @param {string} file - font file name as listed by /api/soundfonts.
   */
  function saveSoundFont(file) {
    try {
      localStorage.setItem(SOUNDFONT_KEY, file);
    } catch (e) {
      /* storage unavailable: the choice just won't survive a reload */
    }
  }

  /**
   * Format milliseconds as m:ss for the time readout.
   *
   * @param {number} ms - a time position from the player, in milliseconds.
   * @returns {string} e.g. "3:46".
   */
  function fmt(ms) {
    const s = Math.floor(ms / 1000);
    return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
  }

  /**
   * Wire the transport controls to a live alphaTab api instance.
   *
   * Buttons start disabled in the HTML and are enabled only on playerReady
   * (SoundFont downloaded and synth initialized) so the user cannot press
   * play into silence. Count-in defaults to ON — the button is pre-lit in the
   * markup and the volume is set here to match. Click-to-seek on the score
   * and drag-to-select a loop range are alphaTab built-ins; the loop button
   * only has to flip api.isLooping.
   *
   * @param {object|null} alphaTabApi - instance from Guitarz.conductor.init();
   *        null (vendor assets missing) leaves the transport inert.
   * @param {Array<{file: string, label: string, url: string}>} fonts - the
   *        /api/soundfonts catalog for the SoundFont menu (may be empty).
   * @param {string|null} selectedFont - file name the synth booted with.
   */
  function init(alphaTabApi, fonts, selectedFont) {
    api = alphaTabApi;
    if (!api) return;
    initSoundFontMenu(fonts || [], selectedFont);

    api.countInVolume = 1; // count-in on by default (button starts lit)

    api.playerReady.on(() => {
      btnPlay.disabled = false;
      btnStop.disabled = false;
    });

    api.playerStateChanged.on((e) => {
      const playing = e.state === alphaTab.synth.PlayerState.Playing;
      btnPlay.innerHTML = playing ? "&#10074;&#10074;" : "&#9654;";
    });

    api.playerPositionChanged.on((e) => {
      endTime = e.endTime;
      posTime.textContent = fmt(e.currentTime) + " / " + fmt(e.endTime);
      progressFill.style.width = (e.endTime ? (100 * e.currentTime) / e.endTime : 0) + "%";
    });

    api.playedBeatChanged.on((beat) => {
      if (beat) posBar.textContent = "bar " + (beat.voice.bar.index + 1);
    });

    btnPlay.onclick = () => api.playPause();
    btnStop.onclick = () => {
      api.stop();
      posBar.textContent = "bar –";
    };

    btnCountIn.onclick = () => {
      api.countInVolume = btnCountIn.classList.toggle("on") ? 1 : 0;
    };

    // Tails = rhythm stems/beams under tab staves. Hidden by default (the
    // cursor + fret numbers are the position reference); toggling flips
    // notation.rhythmMode and needs updateSettings + a re-render because it
    // changes layout, unlike the purely-audio toggles around it.
    btnTails.onclick = () => {
      const show = btnTails.classList.toggle("on");
      api.settings.notation.rhythmMode = show
        ? alphaTab.TabRhythmMode.ShowWithBeams
        : alphaTab.TabRhythmMode.Hidden;
      api.updateSettings();
      api.render();
    };

    btnLoop.onclick = () => {
      api.isLooping = btnLoop.classList.toggle("on");
    };

    btnClearLoop.onclick = () => {
      api.playbackRange = null;
    };

    speed.oninput = () => {
      speedVal.textContent = speed.value + "%";
      api.playbackSpeed = speed.value / 100;
    };

    /**
     * Render the transpose readout for the value actually in effect.
     *
     * The conductor owns the value (it clamps to ±12, and auto-transpose
     * sets it on song load) and announces every change with a
     * "guitarz:transpose" event; the readout listens rather than echoing
     * clicks, so a value set by the song's tuning shows up too. A true minus
     * sign for flats-direction, accent color when the band is off its
     * original key.
     *
     * @param {number} n - the offset in effect, in semitones.
     */
    function showTranspose(n) {
      transposeVal.textContent = n < 0 ? "−" + -n : "+" + n;
      transposeVal.classList.toggle("active", n !== 0);
    }

    document.addEventListener("guitarz:transpose", (e) => showTranspose(e.detail));
    const apply = Guitarz.conductor.applyTranspose;
    btnTransposeDown.onclick = () => apply(Guitarz.conductor.state.transpose - 1);
    btnTransposeUp.onclick = () => apply(Guitarz.conductor.state.transpose + 1);
    transposeVal.onclick = () => apply(0);

    btnTransposeAuto.classList.toggle("on", Guitarz.conductor.state.autoTranspose);
    btnTransposeAuto.onclick = () =>
      Guitarz.conductor.setAutoTranspose(btnTransposeAuto.classList.toggle("on"));

    progress.onclick = (ev) => {
      if (!endTime) return;
      const rect = progress.getBoundingClientRect();
      api.timePosition = ((ev.clientX - rect.left) / rect.width) * endTime;
    };

    // Track how the user last moved around: Tab means keyboard navigation,
    // any pointer press means mouse/touch (see spaceMeansPlay).
    document.addEventListener("pointerdown", () => { navigatingByKeyboard = false; }, true);
    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Tab") navigatingByKeyboard = true;
    }, true);
    document.addEventListener("keydown", (ev) => {
      if (ev.code === "Space" && spaceMeansPlay(ev.target)) {
        ev.preventDefault(); // also stops a mouse-focused button from toggling
        if (!btnPlay.disabled) api.playPause();
      }
    });
  }

  /**
   * Decide whether a Space press should play/pause rather than go to the page.
   *
   * Text fields and menus need Space for themselves, and an open dialog owns
   * the keyboard. Buttons are the subtle case: a keyboard user who tabbed to
   * a button expects Space to press it, but a mouse user who just clicked a
   * toggle (loop, fretboard, …) expects Space to start the song — the old
   * global shortcut. ``:focus-visible`` cannot tell them apart here: Chrome
   * flips it to true on the very key press being handled. So the module
   * remembers the last way of moving around instead (Tab vs. a pointer
   * press), which also covers focus that a closing dialog hands back to its
   * button after a mouse click.
   *
   * @param {Element} target - the element the key event was sent to.
   * @returns {boolean} true when Space should toggle playback.
   */
  function spaceMeansPlay(target) {
    if (document.querySelector("dialog[open]")) return false;
    if (/INPUT|TEXTAREA|SELECT/.test(target.tagName)) return false;
    if (target.tagName === "BUTTON" && navigatingByKeyboard) return false;
    return true;
  }

  /**
   * Fill the SoundFont menu and wire live font switching.
   *
   * Switching swaps the synth's instrument samples in place: playback is
   * stopped first (a font change mid-note would glitch), the transport is
   * locked while the new font downloads and decodes (MuseScore General is
   * ~38 MB), and resetSoundFonts() precedes the load so the fonts replace
   * rather than layer. The loaded song, loop range, speed and transpose all
   * survive because they live on the api/score, not in the synth's font
   * bank. The choice is saved only once the font has actually loaded; if the
   * load fails (alphaTab reports it via api.error, which the conductor shows
   * on the overlay), the previous font is reloaded and reselected so the
   * player is never left without instruments.
   *
   * The menu stays disabled when the catalog is empty and until the boot
   * font is ready, so a switch can't race the initial load.
   *
   * @param {Array<{file: string, label: string, url: string}>} fonts - catalog.
   * @param {string|null} selected - file name the synth booted with.
   */
  function initSoundFontMenu(fonts, selected) {
    soundfontSel.innerHTML = "";
    for (const font of fonts) {
      const opt = document.createElement("option");
      opt.value = font.file;
      opt.textContent = font.label;
      soundfontSel.appendChild(opt);
    }
    if (!fonts.length) return;
    if (selected) soundfontSel.value = selected;

    let current = selected; // font the synth is known to hold
    let pending = null; // font being switched to, null when idle

    /**
     * Lock or unlock the controls that must not be used mid-switch.
     *
     * @param {boolean} busy - true while a font is loading.
     */
    function setSwitching(busy) {
      soundfontSel.disabled = busy;
      btnPlay.disabled = busy;
      btnStop.disabled = busy;
    }

    /**
     * Start loading one catalog font into the synth, replacing the current one.
     *
     * @param {string} file - catalog file name to load.
     */
    function loadFont(file) {
      const font = fonts.find((f) => f.file === file);
      if (!font) return;
      pending = file;
      setSwitching(true);
      api.stop();
      api.resetSoundFonts();
      api.loadSoundFont(Guitarz.conductor.siteUrl(font.url), false);
    }

    api.playerReady.on(() => {
      if (pending === null) soundfontSel.disabled = false;
    });

    api.soundFontLoaded.on(() => {
      if (pending === null) return; // the boot font; playerReady handles it
      current = pending;
      pending = null;
      saveSoundFont(current);
      setSwitching(false);
    });

    api.error.on(() => {
      if (pending === null) return;
      const failed = pending;
      pending = null;
      soundfontSel.value = current;
      if (current && current !== failed) {
        loadFont(current); // restore a working font
      } else {
        setSwitching(false);
      }
    });

    soundfontSel.onchange = () => loadFont(soundfontSel.value);
  }

  Guitarz.transport = { init, savedSoundFont };
})();
