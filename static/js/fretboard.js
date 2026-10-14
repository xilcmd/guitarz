/**
 * Fretboard pane: a clean-room SVG guitar neck that lights up the notes of
 * one track as alphaTab plays them.
 *
 * Design: alphaTab knows nothing about fretboards, but every played beat it
 * reports carries notes with a string and fret number — exactly what a neck
 * diagram needs. This module owns the drawing (so wood, fret wire and inlay
 * styles are plain data in THEMES) and listens to alphaTab's
 * activeBeatsChanged event to place the dots. The geometry helpers are pure
 * and exported so the smoke test can pin the guitar math directly.
 */
(function () {
  const Guitarz = (window.Guitarz = window.Guitarz || {});

  const SVG_NS = "http://www.w3.org/2000/svg";

  /** localStorage key remembering whether the pane is shown ("1") or not. */
  const SHOW_KEY = "guitarz.fretboard";
  /** localStorage key remembering the chosen theme id. */
  const THEME_KEY = "guitarz.fretTheme";

  /** Fewest frets drawn; songs that climb higher get a longer neck. */
  const MIN_FRETS = 22;
  /** Most frets drawn, whatever a file claims (keeps fret spacing sane). */
  const MAX_FRETS = 30;

  /** Pitch-class names for labels; sharps, as tab players read them. */
  const NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];

  /**
   * Fret wire and string greys shared by every look (from Maple · blocks).
   *
   * Musicians reviewing the looks found these neutral greys read best on
   * every wood: bright, near-white strings and frets competed with the
   * colored note dots on the dark boards.
   */
  const FRET_GREY = "#8c8c8c";
  const STRING_GREY = "#5e5e5e";

  /**
   * Fretboard looks, keyed by id. Each theme is pure data: wood colors (a
   * two-stop gradient gives the board some depth), fret wire, nut, strings,
   * an inlay style, color and opacity, and optionally ``frets`` — the neck's
   * own fret count, so a look modeled on a real guitar keeps that guitar's
   * length even when the song stays low. ``inlayOpacity`` applies to the
   * whole inlay layer: dark boards keep it low (about 0.3) so pearl-white
   * markers stay a quiet guide instead of pulling the eye away from the note
   * numbers and names. Adding a look means adding an entry here — the
   * renderer has one drawing routine per inlay style.
   */
  const THEMES = {
    "rosewood-dots": {
      label: "Rosewood · dots",
      wood: ["#4a2c1f", "#2e1a12"],
      fret: FRET_GREY,
      nut: "#efe6d0",
      string: STRING_GREY,
      inlay: "dots",
      inlayColor: "#e9e4d8",
      inlayOpacity: 0.3,
    },
    "ebony-diamonds": {
      label: "Ebony · diamonds",
      wood: ["#221d19", "#0f0c0a"],
      fret: FRET_GREY,
      nut: "#f2ecdc",
      string: STRING_GREY,
      inlay: "diamonds",
      inlayColor: "#d6e2e8",
      inlayOpacity: 0.3,
    },
    "maple-blocks": {
      label: "Maple · blocks",
      wood: ["#e2c38c", "#c9a265"],
      fret: FRET_GREY,
      nut: "#2a2a2a",
      string: STRING_GREY,
      inlay: "blocks",
      inlayColor: "#1f1f1f",
      inlayOpacity: 0.85,
    },
    // The user's Ibanez JEM: rosewood, white vine, 24 frets.
    "rosewood-jem": {
      label: "Rosewood · tree of life · 24 (JEM)",
      wood: ["#4f2f20", "#2b170f"],
      fret: FRET_GREY,
      nut: "#efe6d0",
      string: STRING_GREY,
      inlay: "tree-of-life",
      inlayColor: "#ffffff",
      inlayOpacity: 0.3,
      frets: 24,
    },
  };
  const DEFAULT_THEME = "rosewood-dots";

  /* Drawing box (SVG viewBox units); the SVG scales to the pane width. */
  const VIEW_W = 1200;
  const VIEW_H = 168;
  const LABEL_W = 34; // string-name column
  const NUT_X = 66; // left edge of the board; open-string dots sit left of it
  const BOARD_TOP = 24; // room above for fret numbers
  const BOARD_BOTTOM = VIEW_H - 8;

  /**
   * Distance of a fret from the nut, by the equal-temperament rule.
   *
   * Each fret shortens the vibrating length by a factor of 2^(1/12), so fret
   * n sits at scale·(1 − 2^(−n/12)): fret 12 halves the string, fret 24 is
   * at three quarters. Using the real rule (not equal spacing) makes the
   * drawing match the hand's sense of where positions are.
   *
   * @param {number} n - fret number (0 = the nut).
   * @param {number} scale - scale length in any unit.
   * @returns {number} distance from the nut in the same unit.
   */
  function fretPosition(n, scale) {
    return scale * (1 - Math.pow(2, -n / 12));
  }

  /**
   * Frets that carry position markers on a neck with the given fret count.
   *
   * The standard pattern: single markers on 3, 5, 7, 9 (repeating an octave
   * up), double markers on 12 and 24 — the octave frets.
   *
   * @param {number} fretCount - highest fret drawn.
   * @returns {Array<{fret: number, double: boolean}>} markers in fret order.
   */
  function inlayFrets(fretCount) {
    const out = [];
    for (let f = 1; f <= fretCount; f++) {
      const m = f % 12;
      if (m === 0) out.push({ fret: f, double: true });
      else if ([3, 5, 7, 9].includes(m)) out.push({ fret: f, double: false });
    }
    return out;
  }

  /**
   * Map an alphaTab string number to a drawing row (0 = top line).
   *
   * alphaTab numbers strings from the lowest-pitched (string 1), while a
   * fretboard diagram — like tab — puts the highest string on top. The flip
   * also lines rows up with staff.tuning, which alphaTab orders high → low,
   * so tuning[stringRow(s)] is the open pitch of string s.
   *
   * @param {number} string - alphaTab note.string (1-based, 1 = lowest).
   * @param {number} stringCount - strings on the instrument.
   * @returns {number} 0-based row from the top.
   */
  function stringRow(string, stringCount) {
    return stringCount - string;
  }

  /**
   * Name a MIDI note in scientific pitch notation (60 = C4).
   *
   * @param {number} midi - MIDI note number.
   * @returns {string} e.g. "E2", "C#4".
   */
  function noteName(midi) {
    return NOTE_NAMES[midi % 12] + (Math.floor(midi / 12) - 1);
  }

  /**
   * The beat right after this one in the same voice, crossing bar lines.
   *
   * Walked by hand (index within the voice, then bar.nextBar) rather than
   * trusting a precomputed link, so the rule is exactly "same voice number,
   * next in time" and empty bars are skipped.
   *
   * @param {object} beat - alphaTab Beat model.
   * @returns {object|null} the following beat, or null at the song's end.
   */
  function followingBeat(beat) {
    const beats = beat.voice.beats;
    if (beat.index + 1 < beats.length) return beats[beat.index + 1];
    for (let bar = beat.voice.bar.nextBar; bar; bar = bar.nextBar) {
      const voice = bar.voices[beat.voice.index];
      if (voice && voice.beats.length) return voice.beats[0];
    }
    return null;
  }

  /**
   * The next beat that asks the hand to fret something new.
   *
   * Rests and beats made only of tie continuations (held notes) are
   * skipped: there is nothing new to place, so previewing them would just
   * repeat what is already lit. This drives the faded "coming up" dots.
   *
   * @param {object} beat - the beat sounding now (may itself be a rest).
   * @returns {object|null} the next beat with a fresh attack, or null.
   */
  function upcomingBeat(beat) {
    for (let b = followingBeat(beat); b; b = followingBeat(b)) {
      if (b.notes.some((n) => !n.isTieDestination)) return b;
    }
    return null;
  }

  /**
   * Every distinct position a bar asks for, across all its voices.
   *
   * Drawn as outlines under the live dots, this is the "shape" of the bar —
   * the chord grip or scale box the hand should settle into. Sorted by
   * string then fret so the result is stable and easy to compare.
   *
   * @param {object} bar - alphaTab Bar model.
   * @returns {Array<{string: number, fret: number}>} unique positions.
   */
  function barShape(bar) {
    const seen = new Map();
    for (const voice of bar.voices)
      for (const beat of voice.beats)
        for (const n of beat.notes) seen.set(n.string + ":" + n.fret, { string: n.string, fret: n.fret });
    return [...seen.values()].sort((a, b) => a.string - b.string || a.fret - b.fret);
  }

  /**
   * Name a pitch without its octave, as a dot label.
   *
   * On a dot the string already tells the octave, and "C#" fits inside a
   * dot where "C#4" would not.
   *
   * @param {number} midi - MIDI note number.
   * @returns {string} e.g. "A", "C#".
   */
  function pitchName(midi) {
    return NOTE_NAMES[midi % 12];
  }

  /**
   * The playback transpose now in effect, in semitones.
   *
   * Pitch labels (string names, note-name dots) add it so they name what
   * is heard — which, when the user plays an Eb song at +1 on a guitar in
   * standard, is exactly what their own strings and frets sound. Fret
   * numbers never change: the tab stays as written.
   *
   * @returns {number} the conductor's current transpose.
   */
  function transpose() {
    return Guitarz.conductor.state.transpose || 0;
  }

  /** Live pane state; module-private apart from what the API exposes. */
  const state = {
    api: null,
    shown: false,
    theme: DEFAULT_THEME,
    trackIndex: null, // selected track index, null when no string track
    geom: null, // geometry of the board currently drawn
    notesLayer: null, // <g> that holds the note dots
    showBar: false, // outline the whole current bar's shape
    lefty: false, // mirror the neck for a left-handed player
    names: false, // label dots with note names instead of fret numbers
    tuning: [], // open-string MIDI pitches of the board, top row first
    shownBar: null, // index of the bar whose shape is drawn, if any
  };

  const pane = document.getElementById("fretpane");
  const board = document.getElementById("fretboard");
  const btn = document.getElementById("btn-fretboard");
  const trackSel = document.getElementById("fret-track");
  const themeSel = document.getElementById("fret-theme");
  const barBtn = document.getElementById("fret-bar");

  const leftyBtn = document.getElementById("fret-lefty");

  /** localStorage key remembering the whole-bar shape toggle ("1" = on). */
  const BAR_KEY = "guitarz.fretBar";
  /** localStorage key remembering the left-handed toggle ("1" = on). */
  const LEFTY_KEY = "guitarz.fretLefty";
  const namesBtn = document.getElementById("fret-names");
  /** localStorage key remembering the note-name label toggle ("1" = on). */
  const NAMES_KEY = "guitarz.fretNames";

  /**
   * Read a saved preference; storage may throw in private windows.
   *
   * @param {string} key - localStorage key.
   * @returns {string|null} the saved value, or null.
   */
  function load(key) {
    try {
      return localStorage.getItem(key);
    } catch (e) {
      return null;
    }
  }

  /**
   * Save a preference (best effort; failure only loses it on reload).
   *
   * @param {string} key - localStorage key.
   * @param {string} value - value to store.
   */
  function save(key, value) {
    try {
      localStorage.setItem(key, value);
    } catch (e) {
      /* storage unavailable */
    }
  }

  /**
   * Create an SVG element with attributes.
   *
   * @param {string} tag - SVG tag name.
   * @param {Object<string, (string|number)>} attrs - attributes to set.
   * @param {Element} [parent] - optional parent to append to.
   * @returns {SVGElement} the new element.
   */
  function el(tag, attrs, parent) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    if (parent) parent.appendChild(node);
    return node;
  }

  /**
   * The first pitched, tuned staff of a track — the one the board shows.
   *
   * @param {object} track - alphaTab track model.
   * @returns {object|undefined} the staff, or undefined for drums/vocals
   *          without tuning.
   */
  function stringStaff(track) {
    return track.staves.find((s) => !s.isPercussion && s.tuning.length > 0);
  }

  /** @returns {object|null} the selected track model, if any. */
  function selectedTrack() {
    const score = Guitarz.conductor.state.score;
    if (!score || state.trackIndex === null) return null;
    return score.tracks[state.trackIndex] || null;
  }

  /**
   * Compute the board geometry for a staff.
   *
   * The scale length is chosen so the last drawn fret lands on the right
   * edge of the box. The neck has the theme's own fret count (MIN_FRETS by
   * default) and grows past it only when the track actually plays higher.
   *
   * @param {object} staff - alphaTab staff with a tuning.
   * @param {number} minFrets - the theme's neck length in frets.
   * @returns {{frets: number, strings: number, scale: number,
   *            fretX: function(number): number, noteX: function(number): number,
   *            stringY: function(number): number, rowGap: number}} geometry.
   */
  function geometry(staff, minFrets) {
    let maxFret = 0;
    for (const bar of staff.bars)
      for (const v of bar.voices)
        for (const b of v.beats) for (const n of b.notes) maxFret = Math.max(maxFret, n.fret);
    const frets = Math.min(MAX_FRETS, Math.max(minFrets, maxFret));
    const strings = staff.tuning.length;
    const boardW = VIEW_W - NUT_X - 6;
    const scale = boardW / (1 - Math.pow(2, -frets / 12));
    const rowGap = (BOARD_BOTTOM - BOARD_TOP) / strings;
    const fretX = (n) => NUT_X + fretPosition(n, scale);
    return {
      frets,
      strings,
      scale,
      rowGap,
      fretX,
      // Dots sit midway between a fret and the one before it; open strings
      // sit just left of the nut.
      noteX: (n) => (n === 0 ? NUT_X - 13 : (fretX(n - 1) + fretX(n)) / 2),
      stringY: (row) => BOARD_TOP + rowGap * (row + 0.5),
    };
  }

  /**
   * Draw the inlay markers for one theme.
   *
   * One routine per inlay style; everything decorative gets class fb-inlay
   * so tests (and CSS) can find it. The tree of life is a stylized vine
   * winding up the board centerline with a pair of leaves at each marker
   * fret — a nod to the classic inlay, not a copy of any maker's artwork.
   *
   * @param {SVGElement} g - layer to draw into.
   * @param {object} geom - board geometry from geometry().
   * @param {object} theme - THEMES entry.
   */
  function drawInlays(g, geom, theme) {
    const top = BOARD_TOP;
    const h = BOARD_BOTTOM - BOARD_TOP;
    const midY = top + h / 2;
    const color = theme.inlayColor;
    const marks = inlayFrets(geom.frets);
    const mid = (f) => (geom.fretX(f - 1) + geom.fretX(f)) / 2;
    const width = (f) => geom.fretX(f) - geom.fretX(f - 1);

    if (theme.inlay === "tree-of-life") {
      drawVine(g, geom, color);
      return;
    }

    for (const m of marks) {
      const x = mid(m.fret);
      const w = width(m.fret);
      if (theme.inlay === "blocks") {
        const bw = w * 0.62;
        const bh = h * (m.double ? 0.82 : 0.7);
        el("rect", { class: "fb-inlay", x: x - bw / 2, y: midY - bh / 2, width: bw,
          height: bh, rx: 2, fill: color }, g);
      } else if (theme.inlay === "diamonds") {
        const dw = Math.min(w * 0.36, h * 0.3);
        const dh = h * (m.double ? 0.78 : 0.56);
        const pts = [[x, midY - dh / 2], [x + dw, midY], [x, midY + dh / 2], [x - dw, midY]];
        el("polygon", { class: "fb-inlay", points: pts.map((p) => p.join(",")).join(" "),
          fill: color }, g);
      } else {
        const r = Math.min(w * 0.18, geom.rowGap * 0.32);
        const ys = m.double ? [top + h * 0.25, top + h * 0.75] : [midY];
        for (const y of ys) {
          el("circle", { class: "fb-inlay", cx: x, cy: y, r, fill: color }, g);
        }
      }
    }
  }

  /**
   * Draw a tree-of-life vine running the full length of the neck.
   *
   * A stylized take on the classic vine inlay, not a copy of any maker's
   * artwork. The stem is laid out in fret units rather than pixels: its
   * wave advances a fixed phase per fret, so the curves tighten toward the
   * body exactly as the frets do, the way a real inlay is cut to the neck.
   * One pointed leaf per fret alternates sides of the stem, sized to its
   * fret (big near the nut, small up high), and a small curled tendril
   * sprouts at each marker fret. Leaves carry class fb-inlay; stem and
   * tendrils (strokes, no fill) carry fb-inlay-vine.
   *
   * @param {SVGElement} g - layer to draw into.
   * @param {object} geom - board geometry from geometry().
   * @param {string} color - inlay color.
   */
  function drawVine(g, geom, color) {
    const h = BOARD_BOTTOM - BOARD_TOP;
    const midY = BOARD_TOP + h / 2;
    const amp = h * 0.2;
    const stemY = (t) => midY + amp * Math.sin(t * 1.1);
    const t0 = 0.35;
    const t1 = geom.frets - 0.15;

    let d = "";
    for (let i = 0; i <= 240; i++) {
      const t = t0 + ((t1 - t0) * i) / 240;
      d += (i ? "L" : "M") + geom.fretX(t).toFixed(1) + " " + stemY(t).toFixed(1);
    }
    el("path", { d, class: "fb-inlay-vine", stroke: color, "stroke-width": 2.4,
      "stroke-linecap": "round", fill: "none" }, g);

    for (let f = 1; f <= geom.frets; f++) {
      const t = f - 0.5;
      const bx = geom.fretX(t);
      const by = stemY(t);
      const fretW = geom.fretX(f) - geom.fretX(f - 1);
      const side = f % 2 ? -1 : 1; // -1 = leaf points up, +1 = down
      const len = Math.min(fretW * 0.85, h * 0.36);
      const wid = len * 0.42;
      const ang = side * (Math.PI / 4); // leaning toward the body
      const tx = bx + len * Math.cos(ang);
      const ty = by + len * Math.sin(ang);
      // Pointed leaf: two quadratic curves, bulging either side of the axis.
      const mx = (bx + tx) / 2;
      const my = (by + ty) / 2;
      const nx = -Math.sin(ang) * wid;
      const ny = Math.cos(ang) * wid;
      el("path", {
        class: "fb-inlay",
        d: `M${bx.toFixed(1)} ${by.toFixed(1)}` +
          `Q${(mx + nx).toFixed(1)} ${(my + ny).toFixed(1)} ${tx.toFixed(1)} ${ty.toFixed(1)}` +
          `Q${(mx - nx).toFixed(1)} ${(my - ny).toFixed(1)} ${bx.toFixed(1)} ${by.toFixed(1)}Z`,
        fill: color,
      }, g);
    }

    for (const m of inlayFrets(geom.frets)) {
      const t = m.fret - 0.15;
      const x = geom.fretX(t);
      const y = stemY(t);
      const side = m.fret % 2 ? 1 : -1; // opposite the leaf on that fret
      const r = Math.min((geom.fretX(m.fret) - geom.fretX(m.fret - 1)) * 0.22, h * 0.09);
      el("path", {
        class: "fb-inlay-vine",
        d: `M${x.toFixed(1)} ${y.toFixed(1)}` +
          `q${r} ${side * r * 1.6} ${r * 2.2} ${side * r * 0.9}` +
          `a${r * 0.7} ${r * 0.7} 0 1 0 ${-r * 0.9} ${-side * r * 0.9}`,
        stroke: color,
        "stroke-width": 1.4,
        "stroke-linecap": "round",
        fill: "none",
      }, g);
    }
  }

  /**
   * Rebuild the whole board SVG for the selected track and theme.
   *
   * Called when the pane opens, the track or theme changes, or a new song
   * loads — never per beat (dots live in their own layer, see showBeats).
   * Shows a short hint instead when the song has no string track.
   */
  function drawBoard() {
    board.innerHTML = "";
    state.geom = null;
    state.notesLayer = null;
    const track = selectedTrack();
    const staff = track && stringStaff(track);
    if (!staff) {
      board.innerHTML = '<p class="hint">No string track to show.</p>';
      return;
    }
    const theme = THEMES[state.theme] || THEMES[DEFAULT_THEME];
    const geom = geometry(staff, theme.frets || MIN_FRETS);
    state.geom = geom;
    state.tuning = staff.tuning.slice();

    const svg = el("svg", {
      viewBox: `0 0 ${VIEW_W} ${VIEW_H}`,
      preserveAspectRatio: "xMinYMin meet",
      "data-theme": state.theme,
      "data-frets": geom.frets,
      role: "img",
      "aria-label": "Fretboard for " + (track.name || "track"),
    });
    const defs = el("defs", {}, svg);
    const grad = el("linearGradient", { id: "fb-wood", x1: 0, y1: 0, x2: 0, y2: 1 }, defs);
    el("stop", { offset: "0%", "stop-color": theme.wood[0] }, grad);
    el("stop", { offset: "100%", "stop-color": theme.wood[1] }, grad);

    // Everything is drawn right-handed into one group; a left-handed neck
    // is the same drawing mirrored as a whole (label() keeps text readable).
    const root = el("g", { class: "fb-root" }, svg);
    if (state.lefty) {
      svg.setAttribute("data-lefty", "1");
      root.setAttribute("transform", `translate(${VIEW_W} 0) scale(-1 1)`);
    }

    const right = geom.fretX(geom.frets);
    el("rect", { x: NUT_X, y: BOARD_TOP, width: right - NUT_X,
      height: BOARD_BOTTOM - BOARD_TOP, fill: "url(#fb-wood)" }, root);

    drawInlays(el("g", { class: "fb-inlays", opacity: theme.inlayOpacity ?? 0.85 }, root), geom, theme);

    const frets = el("g", { class: "fb-frets" }, root);
    for (let f = 1; f <= geom.frets; f++) {
      const x = geom.fretX(f);
      el("line", { x1: x, y1: BOARD_TOP, x2: x, y2: BOARD_BOTTOM,
        stroke: theme.fret, "stroke-width": 2.2 }, frets);
      label({ x: (geom.fretX(f - 1) + x) / 2, y: BOARD_TOP - 8,
        class: "fb-fretnum", "text-anchor": "middle" }, frets, f);
    }
    el("rect", { x: NUT_X - 4, y: BOARD_TOP, width: 5,
      height: BOARD_BOTTOM - BOARD_TOP, fill: theme.nut }, root);

    const strings = el("g", { class: "fb-strings" }, root);
    for (let row = 0; row < geom.strings; row++) {
      const y = geom.stringY(row);
      // Lower strings are thicker, like the real thing.
      const w = 0.9 + (1.8 * row) / Math.max(1, geom.strings - 1);
      el("line", { class: "fb-string", x1: NUT_X - 26, y1: y, x2: right, y2: y,
        stroke: theme.string, "stroke-width": w }, strings);
      label({ x: LABEL_W - 4, y: y + 4, class: "fb-label", "text-anchor": "end" },
        strings, noteName(staff.tuning[row] + transpose()));
    }

    state.notesLayer = el("g", { class: "fb-notes" }, root);
    drawHitZones(root, geom);
    board.appendChild(svg);
  }

  /**
   * Create a text label that reads correctly on a mirrored (lefty) neck.
   *
   * Inside the mirrored group a plain <text> would come out backwards. So
   * each label gets its own mirror around its anchor x, which cancels the
   * group's mirror for the glyphs while keeping the position mirrored. The
   * start/end anchors swap too, so a label still grows away from the board.
   *
   * @param {Object<string, (string|number)>} attrs - text attributes (x, y, ...).
   * @param {Element} parent - element to append to.
   * @param {(string|number)} content - the label text.
   * @returns {SVGTextElement} the label.
   */
  function label(attrs, parent, content) {
    const t = el("text", attrs, parent);
    t.textContent = content;
    if (state.lefty) {
      t.setAttribute("transform", `translate(${2 * attrs.x} 0) scale(-1 1)`);
      const anchor = attrs["text-anchor"];
      if (anchor === "end") t.setAttribute("text-anchor", "start");
      else if (anchor === "start") t.setAttribute("text-anchor", "end");
    }
    return t;
  }

  /**
   * Draw one note dot into the notes layer.
   *
   * Each dot carries data-string / data-fret for inspection; color comes
   * from the track's rail chip so the board matches the staff it mirrors.
   * The label is the fret number, or the sounding note name (open pitch of
   * the row + fret + playback transpose, so it follows the track's own
   * tuning and names what is heard) when the names toggle is on. Open strings are rings, dead notes show an ×. Non-live
   * dots get an
   * extra class that CSS styles: "next" (faded preview), "bar" (outline
   * of the bar's shape) or "tap" (a clicked spot).
   *
   * @param {{string: number, fret: number, isDead: (boolean|undefined)}} note -
   *        alphaTab Note model, or a plain position from barShape().
   * @param {string} color - fill/stroke color.
   * @param {string} kind - "live", "next", "bar", or "tap".
   */
  function drawNote(note, color, kind) {
    const geom = state.geom;
    const row = stringRow(note.string, geom.strings);
    if (row < 0 || row >= geom.strings || note.fret > geom.frets) return;
    const r = Math.min(geom.rowGap * 0.42, 11);
    const x = geom.noteX(note.fret);
    const y = geom.stringY(row);
    const cls = "fb-note" + (note.isDead ? " dead" : "") + (kind === "live" ? "" : " " + kind);
    const g = el("g", { class: cls, "data-string": note.string, "data-fret": note.fret },
      state.notesLayer);
    el("circle", { cx: x, cy: y, r, fill: note.fret === 0 ? "none" : color,
      stroke: color, "stroke-width": 2 }, g);
    let text = note.fret;
    if (note.isDead) text = "×";
    else if (state.names) text = pitchName(state.tuning[row] + note.fret + transpose());
    label({ x, y: y + 4, "text-anchor": "middle",
      class: "fb-note-label" + (note.fret === 0 ? " open" : "") }, g, text);
  }

  /**
   * Play one string/fret of the selected track through the synth.
   *
   * alphaTab's playNote needs a note that sits in a beat with a voice, bar,
   * staff and track (for instrument, tuning and tempo). So a throwaway beat
   * borrows the voice of the staff's first bar for that context but is
   * never added to the voice's beat list — the score is not changed. It is
   * skipped while the song plays, because a one-off note would interrupt
   * playback.
   *
   * @param {number} string - alphaTab string number (1 = lowest).
   * @param {number} fret - fret number (0 = open).
   * @returns {object|null} the note that was played, or null when nothing
   *          was (no string track, or the song is playing).
   */
  function playPosition(string, fret) {
    const api = state.api;
    const track = selectedTrack();
    const staff = track && stringStaff(track);
    if (!api || !staff || api.playerState === alphaTab.synth.PlayerState.Playing) return null;
    const model = alphaTab.model;
    const beat = new model.Beat();
    beat.duration = model.Duration.Quarter;
    beat.voice = staff.bars[0].voices[0];
    const note = new model.Note();
    note.string = string;
    note.fret = fret;
    beat.addNote(note);
    beat.finish(api.settings, null);
    api.playNote(note);
    return note;
  }

  /**
   * Add one invisible click zone per string per fret, open strings included.
   *
   * Zones are drawn last so they sit on top of everything, and span the
   * whole cell between two frets and halfway to the next strings — easy to
   * hit with a mouse. A click plays the note and flashes a dot there.
   *
   * @param {SVGElement} parent - the board's root group.
   * @param {object} geom - board geometry from geometry().
   */
  function drawHitZones(parent, geom) {
    const hits = el("g", { class: "fb-hits" }, parent);
    for (let row = 0; row < geom.strings; row++) {
      const string = geom.strings - row; // inverse of stringRow
      for (let f = 0; f <= geom.frets; f++) {
        const x0 = f === 0 ? NUT_X - 26 : geom.fretX(f - 1);
        const x1 = f === 0 ? NUT_X : geom.fretX(f);
        const zone = el("rect", {
          class: "fb-hit",
          x: x0,
          y: geom.stringY(row) - geom.rowGap / 2,
          width: x1 - x0,
          height: geom.rowGap,
          fill: "transparent",
          "data-string": string,
          "data-fret": f,
        }, hits);
        zone.addEventListener("click", () => {
          playPosition(string, f);
          flashTap(string, f);
        });
      }
    }
  }

  /**
   * Briefly show a dot where the user clicked.
   *
   * @param {number} string - alphaTab string number.
   * @param {number} fret - fret number.
   */
  function flashTap(string, fret) {
    if (!state.notesLayer) return;
    const track = selectedTrack();
    const color = (track && Guitarz.conductor.state.trackColors.get(track.index)) || "#e8a33d";
    drawNote({ string, fret }, color, "tap");
    const dot = state.notesLayer.lastElementChild;
    setTimeout(() => dot && dot.remove(), 600);
  }

  /**
   * Light the notes the selected track is sounding, and preview the next.
   *
   * Takes the beats alphaTab reports as active (all tracks, all voices)
   * and keeps those on the selected track's board staff. Layers are drawn
   * back to front so live dots always win where they overlap: the bar's
   * whole shape as outlines (when the bar toggle is on), then the next
   * fresh attack (upcomingBeat) faded, then the notes sounding now. An
   * empty list (stop) simply clears the layer.
   *
   * @param {Array<object>} beats - alphaTab Beat models now sounding.
   */
  function showBeats(beats) {
    const layer = state.notesLayer;
    if (!layer || !state.geom) return;
    layer.innerHTML = "";
    state.shownBar = null;
    const track = selectedTrack();
    const staff = track && stringStaff(track);
    if (!staff) return;
    const color = Guitarz.conductor.state.trackColors.get(track.index) || "#e8a33d";
    const mine = (beats || []).filter((b) => b.voice.bar.staff === staff);
    const lead = mine.find((b) => b.voice.index === 0) || mine[0];
    if (lead && state.showBar) {
      state.shownBar = lead.voice.bar.index;
      for (const pos of barShape(lead.voice.bar)) drawNote(pos, color, "bar");
    }
    const next = lead && upcomingBeat(lead);
    if (next) for (const note of next.notes) if (!note.isTieDestination) drawNote(note, color, "next");
    for (const beat of mine) for (const note of beat.notes) drawNote(note, color, "live");
  }

  /**
   * Fill the track menu with the song's string tracks.
   *
   * Only tracks with a tuned, non-percussion staff can be shown on a neck.
   * The first such track is selected — usually the lead guitar.
   *
   * @param {object} score - loaded alphaTab score.
   */
  function buildTrackMenu(score) {
    trackSel.innerHTML = "";
    state.trackIndex = null;
    for (const track of score.tracks) {
      if (!stringStaff(track)) continue;
      const opt = document.createElement("option");
      opt.value = track.index;
      opt.textContent = track.name || "Track " + (track.index + 1);
      trackSel.appendChild(opt);
      if (state.trackIndex === null) state.trackIndex = track.index;
    }
    trackSel.disabled = state.trackIndex === null;
    if (state.trackIndex !== null) trackSel.value = state.trackIndex;
  }

  /**
   * Show or hide the pane, remembering the choice.
   *
   * The board is drawn on show (not kept up to date while hidden), so a
   * hidden pane costs nothing during playback.
   *
   * @param {boolean} shown - true to show the pane.
   */
  function setShown(shown) {
    state.shown = shown;
    pane.hidden = !shown;
    btn.classList.toggle("on", shown);
    save(SHOW_KEY, shown ? "1" : "0");
    if (shown) drawBoard();
  }

  /**
   * Wire the pane to a live alphaTab api and restore saved choices.
   *
   * Must run after Guitarz.conductor.init so the conductor's scoreLoaded
   * handler (which assigns track colors) has already run when ours does.
   *
   * @param {object|null} api - AlphaTabApi instance; null leaves the pane inert.
   */
  function init(api) {
    state.api = api;
    if (!api) return;

    for (const [id, theme] of Object.entries(THEMES)) {
      const opt = document.createElement("option");
      opt.value = id;
      opt.textContent = theme.label;
      themeSel.appendChild(opt);
    }
    const savedTheme = load(THEME_KEY);
    state.theme = THEMES[savedTheme] ? savedTheme : DEFAULT_THEME;
    themeSel.value = state.theme;

    api.scoreLoaded.on((score) => {
      buildTrackMenu(score);
      if (state.shown) drawBoard();
    });
    api.activeBeatsChanged.on((e) => {
      if (state.shown) showBeats(e.activeBeats);
    });

    btn.onclick = () => setShown(!state.shown);
    trackSel.onchange = () => {
      state.trackIndex = Number(trackSel.value);
      if (state.shown) drawBoard();
    };
    state.showBar = load(BAR_KEY) === "1";
    barBtn.classList.toggle("on", state.showBar);
    barBtn.onclick = () => {
      state.showBar = barBtn.classList.toggle("on");
      save(BAR_KEY, state.showBar ? "1" : "0");
    };
    // Pitch labels include the transpose, so redraw when it changes.
    document.addEventListener("guitarz:transpose", () => {
      if (state.shown) drawBoard();
    });
    state.names = load(NAMES_KEY) === "1";
    namesBtn.classList.toggle("on", state.names);
    namesBtn.onclick = () => {
      state.names = namesBtn.classList.toggle("on");
      save(NAMES_KEY, state.names ? "1" : "0");
      if (state.shown) drawBoard();
    };
    state.lefty = load(LEFTY_KEY) === "1";
    leftyBtn.classList.toggle("on", state.lefty);
    leftyBtn.onclick = () => {
      state.lefty = leftyBtn.classList.toggle("on");
      save(LEFTY_KEY, state.lefty ? "1" : "0");
      if (state.shown) drawBoard();
    };
    themeSel.onchange = () => {
      state.theme = themeSel.value;
      save(THEME_KEY, state.theme);
      if (state.shown) drawBoard();
    };

    setShown(load(SHOW_KEY) === "1");
  }

  Guitarz.fretboard = {
    init, fretPosition, inlayFrets, stringRow, noteName, pitchName, upcomingBeat, barShape, playPosition, THEMES, state,
  };
})();
