/**
 * Left column (Files + Tracks) layout: collapse / expand / pin, and a drag
 * handle to resize it. Issues #9 and #10.
 *
 * Design: the column is the only thing that competes with the score for
 * width, so the player can give it back. Collapsed, it shrinks to a thin
 * rail with an expand button. Pinned (the default) it stays where the user
 * put it; unpinned, it gets out of the way when playback starts and slides
 * out *over* the score while the pointer is on the rail ("peek") — floating,
 * so the score does not re-lay out under the pointer. Width, collapsed and
 * pinned are saved per browser. Pure layout: works the same with the Python
 * server and in the static site.
 */
(function () {
  const Guitarz = (window.Guitarz = window.Guitarz || {});

  const main = document.getElementById("main");
  const sidebar = document.getElementById("sidebar");
  const rail = document.getElementById("sidebar-rail");
  const handle = document.getElementById("sidebar-handle");
  const btnCollapse = document.getElementById("btn-sidebar-collapse");
  const btnExpand = document.getElementById("btn-sidebar-expand");
  const btnPin = document.getElementById("btn-sidebar-pin");

  /** localStorage keys for the three saved choices. */
  const KEYS = {
    collapsed: "guitarz.sidebarCollapsed",
    pinned: "guitarz.sidebarPinned",
    width: "guitarz.sidebarWidth",
  };
  /** Default column width in CSS pixels (the original fixed width). */
  const DEFAULT_WIDTH = 290;
  /** Narrowest useful column: file names and rail buttons still fit. */
  const MIN_WIDTH = 200;
  /** Arrow-key step on the focused resize handle, in CSS pixels. */
  const KEY_STEP = 16;

  const state = {
    collapsed: load(KEYS.collapsed) === "1",
    pinned: load(KEYS.pinned) !== "0",
    width: Number(load(KEYS.width)) || DEFAULT_WIDTH,
  };

  let relayoutTimer = null;

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
   * Widest allowed column: half the window, so the score always keeps half.
   *
   * @returns {number} maximum width in CSS pixels.
   */
  function maxWidth() {
    return Math.max(MIN_WIDTH, Math.floor(window.innerWidth / 2));
  }

  /**
   * Clamp a requested width into the allowed range, in whole pixels.
   *
   * @param {number} w - requested width.
   * @returns {number} the width that will be used.
   */
  function clampWidth(w) {
    return Math.min(maxWidth(), Math.max(MIN_WIDTH, Math.round(w)));
  }

  /**
   * Ask alphaTab to re-lay the score out for the new viewport width.
   *
   * alphaTab re-lays out on a window resize when its container's width
   * changed, but a layout change inside the page fires no such event. A
   * synthetic one triggers the same path. Debounced so a drag re-lays out
   * once it settles rather than on every pointer move.
   */
  function relayout() {
    clearTimeout(relayoutTimer);
    relayoutTimer = setTimeout(() => window.dispatchEvent(new Event("resize")), 120);
  }

  /**
   * Push the state into the DOM: classes, width variable, ARIA, buttons.
   *
   * @param {boolean} [layoutChanged=true] - false when only non-layout
   *        details changed (pin), so alphaTab is not asked to re-lay out.
   */
  function apply(layoutChanged = true) {
    state.width = clampWidth(state.width);
    main.classList.toggle("sidebar-collapsed", state.collapsed);
    if (!state.collapsed) main.classList.remove("sidebar-peek");
    main.style.setProperty("--sidebar-w", state.width + "px");
    handle.setAttribute("aria-valuenow", state.width);
    handle.setAttribute("aria-valuemin", MIN_WIDTH);
    handle.setAttribute("aria-valuemax", maxWidth());
    btnPin.classList.toggle("on", state.pinned);
    btnPin.setAttribute("aria-pressed", String(state.pinned));
    btnPin.title = state.pinned
      ? "Pinned: this panel stays open while playing (click to unpin)"
      : "Unpinned: this panel hides when playback starts (click to pin)";
    if (layoutChanged) relayout();
  }

  /**
   * Collapse or expand the column, remembering the choice.
   *
   * @param {boolean} collapsed - true to collapse to the rail.
   */
  function setCollapsed(collapsed) {
    state.collapsed = collapsed;
    save(KEYS.collapsed, collapsed ? "1" : "0");
    apply();
  }

  /**
   * Set the column width, optionally saving it.
   *
   * @param {number} w - requested width (clamped).
   * @param {boolean} persist - save it now (false while a drag is ongoing).
   */
  function setWidth(w, persist) {
    state.width = clampWidth(w);
    if (persist) save(KEYS.width, String(state.width));
    apply();
  }

  /**
   * End a peek when the pointer leaves both the rail and the floating column.
   *
   * @param {MouseEvent} ev - the mouseleave event.
   */
  function endPeek(ev) {
    const to = ev.relatedTarget;
    if (to && (rail.contains(to) || sidebar.contains(to))) return;
    main.classList.remove("sidebar-peek");
  }

  /* ---- collapse / expand / pin (#9) ---- */

  btnCollapse.onclick = () => setCollapsed(true);
  btnExpand.onclick = () => setCollapsed(false);
  btnPin.onclick = () => {
    state.pinned = !state.pinned;
    save(KEYS.pinned, state.pinned ? "1" : "0");
    apply(false);
  };
  rail.addEventListener("mouseenter", () => {
    if (state.collapsed && !state.pinned) main.classList.add("sidebar-peek");
  });
  rail.addEventListener("mouseleave", endPeek);
  sidebar.addEventListener("mouseleave", endPeek);

  /* ---- resize handle (#10) ---- */

  let drag = null; // {startX, startWidth} while dragging

  handle.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0) return;
    ev.preventDefault();
    handle.setPointerCapture(ev.pointerId);
    drag = { startX: ev.clientX, startWidth: state.width };
    main.classList.add("sidebar-resizing");
  });
  handle.addEventListener("pointermove", (ev) => {
    if (drag) setWidth(drag.startWidth + ev.clientX - drag.startX, false);
  });
  const endDrag = () => {
    if (!drag) return;
    drag = null;
    main.classList.remove("sidebar-resizing");
    setWidth(state.width, true);
  };
  handle.addEventListener("pointerup", endDrag);
  handle.addEventListener("pointercancel", endDrag);
  handle.addEventListener("dblclick", () => setWidth(DEFAULT_WIDTH, true));
  handle.addEventListener("keydown", (ev) => {
    const delta = { ArrowLeft: -KEY_STEP, ArrowRight: KEY_STEP }[ev.key];
    if (ev.key === "Home") setWidth(MIN_WIDTH, true);
    else if (ev.key === "End") setWidth(maxWidth(), true);
    else if (delta) setWidth(state.width + delta, true);
    else return;
    ev.preventDefault();
  });
  window.addEventListener("resize", (ev) => {
    if (ev.isTrusted) apply(false); // re-clamp to the new window; not our own event
  });

  // Restore saved choices before alphaTab first renders, so the first
  // layout already has the right width.
  apply(false);

  /**
   * Hook the column to playback: unpinned, it collapses when a song starts.
   *
   * @param {object|null} api - AlphaTabApi instance; null leaves this inert.
   */
  function init(api) {
    if (!api) return;
    api.playerStateChanged.on((e) => {
      if (e.state === alphaTab.synth.PlayerState.Playing && !state.pinned && !state.collapsed) {
        setCollapsed(true);
      }
    });
  }

  Guitarz.sidebar = { init, state };
})();
