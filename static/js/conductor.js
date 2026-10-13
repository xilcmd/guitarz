/**
 * Conductor view: the alphaTab engine, full-score rendering, and the
 * per-track rail (mute / solo / volume / visibility).
 *
 * Design: alphaTab owns parsing, layout, and the synth. This module's job is
 * to configure it into an "orchestra score" — every track rendered at once,
 * tablature for pitched instruments, percussion notation for drums — and to
 * translate rail-button clicks into alphaTab mixer calls.
 */
(function () {
  const Guitarz = (window.Guitarz = window.Guitarz || {});

  /**
   * Shared conductor state, exposed on Guitarz.conductor.state so the
   * transport module (and tests) can reach the live alphaTab api object.
   *
   * @property {object|null} api - the AlphaTabApi instance after init().
   * @property {object|null} score - the currently loaded score model.
   * @property {Set<number>} visible - indexes of tracks currently rendered.
   * @property {Map<number, string>} trackColors - track index → hex color
   *           for string-instrument tracks; drives both rail chips and the
   *           score-side staff tinting so they always match.
   * @property {number} transpose - global playback transpose in semitones
   *           (audio only, ±12). With autoTranspose on, each song load sets
   *           it from the song's tuning; off, it persists across loads.
   * @property {boolean} autoTranspose - derive transpose from each song's
   *           tuning on load (see tuningOffset); saved per browser.
   * @property {Set<number>} lyricTracks - indexes of tracks carrying lyrics;
   *           their words stay on stage even when the track is hidden.
   * @property {Array<{beat: object, original: (string[]|null)}>} lyricTransplants -
   *           journal of beats mutated by lyric transplantation, with each
   *           beat's pre-transplant lyrics for exact restoration.
   */
  const state = {
    api: null,
    score: null,
    visible: new Set(),
    trackColors: new Map(),
    transpose: 0,
    lyricTracks: new Set(),
    lyricTransplants: [],
    autoTranspose: true, // set from storage below, once its key is defined
  };

  /**
   * Read the saved auto-transpose choice; on unless explicitly turned off.
   *
   * Storage can throw in private windows; the feature then just stays on.
   *
   * @returns {boolean} true when auto-transpose is enabled.
   */
  function loadAutoTranspose() {
    try {
      return localStorage.getItem(AUTO_TRANSPOSE_KEY) !== "0";
    } catch (e) {
      return true;
    }
  }

  /**
   * Turn auto-transpose-from-tuning on or off, remembering the choice.
   *
   * Only affects the next song load; the current transpose is left as is,
   * so switching it off never yanks the key out from under the player.
   *
   * @param {boolean} on - true to derive transpose from each song's tuning.
   */
  function setAutoTranspose(on) {
    state.autoTranspose = on;
    try {
      localStorage.setItem(AUTO_TRANSPOSE_KEY, on ? "1" : "0");
    } catch (e) {
      /* storage unavailable: the choice just won't survive a reload */
    }
  }

  /**
   * Work out the transpose that makes a song sound as if played in standard.
   *
   * The user plays down-tuned songs (e.g. the D#/Eb folder) on a guitar in
   * standard tuning by raising playback instead of retuning. That shift is
   * in the file: compare every tuned, pitched staff string by string with
   * standard tuning for its string count. A song qualifies only when every
   * string of every such staff differs by the same amount up to whole
   * octaves (a 12-string's octave course is still "the same tuning"); the
   * smallest such shift is the answer (Eb → +1, D standard → +2, A
   * standard → +7). Anything mixed — Drop D, open tunings, an unknown
   * string count, one track Eb and another standard — returns null rather
   * than guessing.
   *
   * @param {object} score - alphaTab score (only tracks/staves/tuning read).
   * @returns {number|null} semitones to transpose playback, or null.
   */
  function tuningOffset(score) {
    const diffs = [];
    for (const track of score.tracks) {
      for (const staff of track.staves) {
        if (staff.isPercussion || !staff.tuning.length) continue;
        const ref = STANDARD_TUNINGS[staff.tuning.length];
        if (!ref) return null;
        staff.tuning.forEach((p, i) => diffs.push(ref[i] - p));
      }
    }
    if (!diffs.length) return null;
    const mod = (n) => ((n % 12) + 12) % 12;
    if (!diffs.every((d) => mod(d) === mod(diffs[0]))) return null;
    return diffs.reduce((best, d) => (Math.abs(d) < Math.abs(best) ? d : best));
  }

  /** Largest playback transpose allowed in either direction, in semitones. */
  const TRANSPOSE_LIMIT = 12;

  /** localStorage key for the auto-transpose-from-tuning toggle ("0" = off). */
  const AUTO_TRANSPOSE_KEY = "guitarz.autoTranspose";

  /**
   * Standard tuning per string count, as MIDI pitches in alphaTab's order
   * (highest string first): 6/7-string guitar and 4/5-string bass.
   */
  const STANDARD_TUNINGS = {
    6: [64, 59, 55, 50, 45, 40],
    7: [64, 59, 55, 50, 45, 40, 35],
    4: [43, 38, 33, 28],
    5: [43, 38, 33, 28, 23],
  };

  state.autoTranspose = loadAutoTranspose();

  /**
   * Palette for string-instrument tracks (guitars, bass, etc.).
   *
   * Hues chosen to stay legible on the dark stage background and to avoid
   * the transport amber (reserved for the playback cursor) — so a colored
   * staff can never be confused with the "you are here" highlight. Reused
   * cyclically if a song has more string tracks than colors.
   */
  const PALETTE = [
    "#e06c75", // red      — typically lead guitar
    "#61afef", // blue
    "#98c379", // green
    "#c678dd", // purple
    "#56b6c2", // teal
    "#d19a66", // orange
    "#e5c07b", // yellow
  ];

  const tracksEl = document.getElementById("tracks");
  const overlayEl = document.getElementById("overlay");
  const overlayMsg = document.getElementById("overlay-msg");
  const titleEl = document.getElementById("song-title");

  /**
   * Show the modal overlay with a status or error message.
   *
   * The overlay doubles as loading indicator and error surface so there is
   * exactly one place the user looks when the score area isn't ready.
   *
   * @param {string} msg - text to display over the score area.
   */
  function overlay(msg) {
    overlayMsg.textContent = msg;
    overlayEl.classList.remove("hidden");
  }

  /** Hide the overlay once rendering has finished. */
  function hideOverlay() {
    overlayEl.classList.add("hidden");
  }

  /**
   * Create and configure the alphaTab engine inside #alphatab.
   *
   * Notable settings: the Bravura font and SoundFont are served from our own
   * origin (vendored) so the app works offline and the render worker can load
   * them; display.resources recolors the score for the dark theme;
   * rhythmMode adds stems/beams below tab staves, without which tab-only
   * notation loses all timing information; the viewport element is handed to
   * the player for cursor auto-scroll. The SoundFont is chosen by the caller
   * (app.js resolves the saved menu choice or the server default) so the
   * synth boots straight into the right font instead of loading one and
   * immediately swapping it.
   *
   * @param {string} soundFontUrl - URL of the SoundFont (.sf2/.sf3) to boot with.
   * @returns {object|null} the AlphaTabApi instance, or null when the
   *          vendored alphaTab bundle is missing (a setup error, reported on
   *          the overlay rather than thrown).
   */
  function init(soundFontUrl) {
    if (typeof alphaTab === "undefined") {
      overlay("alphaTab assets missing — run scripts/fetch_vendor.sh and reload.");
      return null;
    }
    const el = document.getElementById("alphatab");
    const viewport = document.getElementById("viewport");
    const api = new alphaTab.AlphaTabApi(el, {
      core: {
        fontDirectory: "/vendor/alphatab/font/",
        logLevel: "warning",
      },
      display: {
        scale: 0.9,
        resources: {
          mainGlyphColor: "#d7dade",
          secondaryGlyphColor: "#8a919b",
          staffLineColor: "#4a5158",
          barSeparatorColor: "#6b727c",
          barNumberColor: "#e8a33d",
          scoreInfoColor: "#d7dade",
        },
      },
      notation: {
        // Tails (stems/beams under tab staves) start hidden: fret numbers +
        // cursor are the primary position reference; the transport's "tails"
        // toggle re-enables them for anyone who wants full rhythm notation.
        rhythmMode: "hidden",
      },
      player: {
        enablePlayer: true,
        soundFont: soundFontUrl,
        scrollElement: viewport,
        scrollOffsetY: -60,
      },
    });
    state.api = api;

    api.scoreLoaded.on((score) => {
      state.score = score;
      state.visible = new Set(score.tracks.map((t) => t.index));
      state.lyricTracks = findLyricTracks(score);
      state.lyricTransplants = []; // old journal points into the previous score

      for (const track of score.tracks) applyStaffProfile(track);
      state.trackColors = applyTrackColors(score);
      titleEl.textContent = [score.title, score.artist].filter(Boolean).join(" — ");
      buildTrackRail(score);
      // Auto: each song sets its own transpose from its tuning (Eb → +1,
      // anything mixed → 0). Manual: a persisted transpose carries over to
      // the new score's tracks — the synth offset is per track-instance,
      // and these are new instances.
      if (state.autoTranspose) applyTranspose(tuningOffset(score) ?? 0);
      else if (state.transpose !== 0) applyTranspose(state.transpose);
      api.renderTracks(score.tracks); // all tracks at once = the conductor view
    });

    api.renderStarted.on(() => overlay("Rendering score…"));
    api.renderFinished.on(() => hideOverlay());
    api.error.on((err) => {
      console.error("alphaTab error", err);
      overlay("Error: " + (err && err.message ? err.message : err));
    });

    return api;
  }

  /**
   * Fetch a song from the backend or static export and pass it to alphaTab.
   *
   * The file travels as raw bytes (application/octet-stream) and is parsed
   * entirely in the browser; format detection (.gp vs .gp5) is alphaTab's.
   * Failures — network, HTTP, or parse — all land on the overlay so the user
   * sees why the score didn't change.
   *
   * @param {{name: string, path: string, local?: boolean}} file - library or hosted song.
   * @returns {Promise<void>} resolves once loading has been handed off.
   */
  async function loadFile(file) {
    overlay("Loading " + file.name + " …");
    try {
      let buf;
      if (file.local) {
        const blob = await Guitarz.library.read(file.path);
        buf = await blob.arrayBuffer();
      } else {
        let url = "/api/file?path=" + encodeURIComponent(file.path);
        if (window.GuitarzStatic) {
          const parts = file.path.split("/");
          if (parts.some((part) => !part || part === "." || part === ".." || part.includes("\\"))) {
            throw new Error("invalid song path");
          }
          url = "/songs/" + parts.map(encodeURIComponent).join("/");
        }
        const res = await fetch(url);
        if (!res.ok) throw new Error("server returned " + res.status);
        buf = await res.arrayBuffer();
      }
      if (!state.api.load(new Uint8Array(buf))) {
        throw new Error("unsupported or corrupt file");
      }
    } catch (e) {
      overlay("Could not load " + file.name + ": " + e.message);
    }
  }

  /**
   * Assign a palette color to every string-instrument track and tint its
   * staff in the score model.
   *
   * "String track" means at least one non-percussion staff with a tuning
   * (6 strings = guitar, 4-5 = bass, ...). Percussion keeps the neutral theme
   * color: drum notation shares one staff style and coloring it would only
   * add noise. Tinting works through alphaTab's style API — a color map per
   * model element — so it must touch every note (fret numbers) and beat
   * (stems/flags/beams/rests, i.e. the "tails" when those are toggled on)
   * plus the track header (name + tuning). Mutating the model is enough:
   * the caller's renderTracks() picks the styles up, no extra render pass.
   *
   * @param {object} score - the freshly loaded alphaTab score model.
   * @returns {Map<number, string>} track index → assigned hex color, for the
   *          rail chips to mirror.
   */
  function applyTrackColors(score) {
    const model = alphaTab.model;
    const assigned = new Map();
    let next = 0;
    for (const track of score.tracks) {
      const isString = track.staves.some((s) => !s.isPercussion && s.tuning.length > 0);
      if (!isString) continue;
      const hex = PALETTE[next++ % PALETTE.length];
      assigned.set(track.index, hex);
      const color = model.Color.fromJson(hex);

      const trackStyle = new model.TrackStyle();
      trackStyle.colors.set(model.TrackSubElement.TrackName, color);
      trackStyle.colors.set(model.TrackSubElement.StringTuning, color);
      track.style = trackStyle;

      for (const staff of track.staves) {
        for (const bar of staff.bars) {
          for (const voice of bar.voices) {
            for (const beat of voice.beats) {
              const beatStyle = new model.BeatStyle();
              for (const el of [
                model.BeatSubElement.GuitarTabStem,
                model.BeatSubElement.GuitarTabFlags,
                model.BeatSubElement.GuitarTabBeams,
                model.BeatSubElement.GuitarTabTuplet,
                model.BeatSubElement.GuitarTabRests,
              ]) {
                beatStyle.colors.set(el, color);
              }
              beat.style = beatStyle;
              for (const note of beat.notes) {
                const noteStyle = new model.NoteStyle();
                noteStyle.colors.set(model.NoteSubElement.GuitarTabFretNumber, color);
                note.style = noteStyle;
              }
            }
          }
        }
      }
    }
    return assigned;
  }

  /**
   * Set the global playback transpose, in semitones (audio only).
   *
   * Applies alphaTab's live synth pitch offset to every pitched track —
   * percussion is excluded because transposing the MIDI drum channel remaps
   * which drum sounds play rather than shifting pitch. The written tab is
   * deliberately untouched: this is the play-along feature (match a detuned
   * recording, capo practice, accompany a singer), so you keep reading the
   * fingering you know while the band moves key. Values are clamped to
   * ±TRANSPOSE_LIMIT; the clamped value is stored and returned so the UI can
   * echo reality rather than the request. Every change is announced as a
   * "guitarz:transpose" event on document (detail = the value), so the
   * transport readout and the fretboard's note names follow it whether the
   * user or the song's tuning set it.
   *
   * @param {number} semitones - requested offset; may exceed the limit.
   * @returns {number} the clamped offset actually in effect.
   */
  function applyTranspose(semitones) {
    const clamped = Math.max(-TRANSPOSE_LIMIT, Math.min(TRANSPOSE_LIMIT, semitones));
    state.transpose = clamped;
    if (state.score) {
      const pitched = state.score.tracks.filter((t) =>
        t.staves.some((s) => !s.isPercussion && s.tuning.length > 0)
      );
      state.api.changeTrackTranspositionPitch(pitched, clamped);
    }
    document.dispatchEvent(new CustomEvent("guitarz:transpose", { detail: clamped }));
    return clamped;
  }

  /**
   * Apply the conductor view's standard staff profile to one track.
   *
   * Tab staves for pitched instruments, notation for percussion (drums have
   * no strings, so a tab staff would be blank). Also the restore path when a
   * lyric track's collapsed staff is re-shown, which is why it lives in a
   * helper rather than inline in scoreLoaded.
   *
   * @param {object} track - alphaTab track model to (re)profile.
   */
  function applyStaffProfile(track) {
    for (const staff of track.staves) {
      staff.showStandardNotation = !!staff.isPercussion;
      staff.showTablature = !staff.isPercussion;
      staff.showSlash = false;
      staff.showNumbered = false;
    }
  }

  /**
   * Undo every lyric transplant, restoring destination beats exactly.
   *
   * Transplants are undone in reverse insertion order: when two sources
   * wrote to the same beat, the later record holds the intermediate value
   * and the earliest record holds the true original, so reverse replay
   * lands back on the pristine state with no residue.
   */
  function undoTransplants() {
    for (let i = state.lyricTransplants.length - 1; i >= 0; i--) {
      const { beat, original } = state.lyricTransplants[i];
      beat.lyrics = original;
    }
    state.lyricTransplants = [];
  }

  /**
   * Re-attach one hidden track's lyrics onto a visible carrier track.
   *
   * Collapsing the vocal staff in place was tried and failed: alphaTab
   * skips a track's effect bands (lyrics included) when no staff type is
   * displayed. So the words are moved instead — each source beat's lyric
   * chunks are appended to the carrier's beat in the same bar (same beat
   * index where possible, last beat otherwise). Bar alignment keeps every
   * word in the right measure even though the two tracks' rhythms differ.
   * Every mutation is journaled in state.lyricTransplants so
   * undoTransplants() can restore the carrier exactly.
   *
   * @param {object} source - hidden lyric-bearing track (model untouched).
   * @param {object} dest - visible track that will display the words.
   */
  function transplantLyrics(source, dest) {
    const destBars = dest.staves[0].bars;
    for (const staff of source.staves) {
      for (const [bi, bar] of staff.bars.entries()) {
        const destBar = destBars[bi];
        if (!destBar) break;
        for (const voice of bar.voices) {
          for (const [bj, beat] of voice.beats.entries()) {
            if (!beat.lyrics || !beat.lyrics.length) continue;
            const destBeats = destBar.voices[0].beats;
            if (!destBeats.length) continue;
            const destBeat = destBeats[Math.min(bj, destBeats.length - 1)];
            state.lyricTransplants.push({ beat: destBeat, original: destBeat.lyrics });
            destBeat.lyrics = (destBeat.lyrics || []).concat(beat.lyrics);
          }
        }
      }
    }
  }

  /**
   * Find which tracks of a score carry lyrics on any beat.
   *
   * Computed once per load (scanning every beat) so the eye toggle can make
   * its keep-lyrics decision instantly. GP files attach lyrics to the beats
   * of the track they were entered on — almost always the vocal track.
   *
   * @param {object} score - the loaded alphaTab score model.
   * @returns {Set<number>} indexes of lyric-bearing tracks.
   */
  function findLyricTracks(score) {
    const found = new Set();
    for (const track of score.tracks) {
      outer: for (const staff of track.staves) {
        for (const bar of staff.bars) {
          for (const voice of bar.voices) {
            for (const beat of voice.beats) {
              if (beat.lyrics && beat.lyrics.length) {
                found.add(track.index);
                break outer;
              }
            }
          }
        }
      }
    }
    return found;
  }

  /**
   * Re-render the score according to the current visibility choices.
   *
   * Visible tracks get their normal staff profile. Hidden tracks leave the
   * render entirely — but a hidden track's lyrics are first transplanted
   * onto the topmost visible pitched track (any visible track as fallback),
   * because the words are the guitarist's roadmap even when the vocal tab
   * is not worth screen space. The transplant plan is rebuilt from scratch
   * on every call (undo, then re-apply per hidden lyric track), which
   * transparently handles the carrier itself being hidden later.
   */
  function renderVisible() {
    undoTransplants();
    const rendered = state.score.tracks.filter((t) => state.visible.has(t.index));
    const carrier =
      rendered.find((t) => t.staves.some((s) => !s.isPercussion && s.tuning.length > 0)) ||
      rendered[0];
    for (const track of state.score.tracks) {
      if (!state.visible.has(track.index) && state.lyricTracks.has(track.index) && carrier) {
        transplantLyrics(track, carrier);
      }
    }
    for (const track of rendered) applyStaffProfile(track);
    state.api.renderTracks(rendered);
  }

  /* ---- track rail ---- */

  /**
   * Rebuild the track rail, one row per track of the loaded score.
   *
   * Called on every score load; rows are cheap enough to recreate wholesale,
   * which also resets stale mute/solo/volume UI from the previous song.
   *
   * @param {object} score - the alphaTab score model.
   */
  function buildTrackRail(score) {
    tracksEl.innerHTML = "";
    for (const track of score.tracks) {
      tracksEl.appendChild(trackRow(track));
    }
  }

  /**
   * Build one rail row: name, visibility eye, mute, solo, volume fader.
   *
   * Mute/solo/volume map 1:1 onto alphaTab mixer calls and take effect
   * mid-playback. The eye toggle re-renders the score with a subset of
   * tracks — hidden tracks keep sounding, mirroring how a conductor can look
   * at fewer staves without silencing the orchestra. At least one track must
   * stay visible or there would be no score (and no cursor) at all.
   *
   * Initial mute/solo button states come from the file's own mixer settings
   * (track.playbackInfo), which the synth already honors on load.
   *
   * String tracks carry their palette color twice — a chip before the name
   * and a tinted left border — matching the color of their staff in the
   * score, so the eye can jump between rail and stage without reading names.
   *
   * @param {object} track - one alphaTab track model.
   * @returns {HTMLElement} the assembled row element.
   */
  function trackRow(track) {
    const api = state.api;
    const row = document.createElement("div");
    row.className = "track-row";

    const name = document.createElement("div");
    name.className = "tname";
    const isPerc = track.staves.some((s) => s.isPercussion);
    const hex = state.trackColors.get(track.index);
    if (hex) {
      row.style.borderLeftColor = hex;
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.style.backgroundColor = hex;
      name.appendChild(chip);
    }
    name.appendChild(
      document.createTextNode(track.name || "Track " + (track.index + 1))
    );
    if (isPerc) {
      const p = document.createElement("span");
      p.className = "perc";
      p.textContent = "🥁";
      name.appendChild(p);
    }
    name.title = name.textContent;
    row.appendChild(name);

    const eyeTip = state.lyricTracks.has(track.index)
      ? "Show/hide this track's staff (its lyrics stay visible)"
      : "Show/hide this track in the score";
    const eye = button("👁", "btn-eye", eyeTip);
    const mute = button("M", "btn-mute", "Mute this track");
    const solo = button("S", "btn-solo", "Solo this track");

    if (track.playbackInfo.isMute) mute.classList.add("on");
    if (track.playbackInfo.isSolo) solo.classList.add("on");

    mute.onclick = () => api.changeTrackMute([track], mute.classList.toggle("on"));
    solo.onclick = () => api.changeTrackSolo([track], solo.classList.toggle("on"));
    eye.onclick = () => {
      const vis = state.visible;
      if (vis.has(track.index)) {
        if (vis.size === 1) return; // keep at least one staff on stage
        vis.delete(track.index);
        row.classList.add("hidden-track");
      } else {
        vis.add(track.index);
        row.classList.remove("hidden-track");
      }
      renderVisible();
    };

    row.appendChild(eye);
    row.appendChild(mute);
    row.appendChild(solo);

    const vol = document.createElement("input");
    vol.type = "range";
    vol.className = "vol";
    vol.min = 0;
    vol.max = 100;
    vol.value = 100;
    vol.title = "Track volume";
    vol.oninput = () => api.changeTrackVolume([track], vol.value / 100);
    row.appendChild(vol);

    return row;
  }

  /**
   * Create a small labeled button for the track rail.
   *
   * @param {string} label - visible text (e.g. "M", "S").
   * @param {string} cls - CSS class controlling placement and on-state color.
   * @param {string} tip - tooltip explaining the control.
   * @returns {HTMLButtonElement} the button element.
   */
  function button(label, cls, tip) {
    const b = document.createElement("button");
    b.textContent = label;
    b.className = cls;
    b.title = tip;
    return b;
  }

  Guitarz.conductor = {
    init, loadFile, overlay, state, applyTranspose, tuningOffset, setAutoTranspose,
  };
})();
