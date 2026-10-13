/**
 * File explorer: hosted songs plus a private, persistent browser library.
 *
 * The server returns one directory level at a time (never a recursive tree),
 * so this module holds the current location implicitly in what it last
 * rendered — clicking a folder or breadcrumb segment simply re-browses.
 */
(function () {
  const Guitarz = (window.Guitarz = window.Guitarz || {});

  const breadcrumbEl = document.getElementById("breadcrumb");
  const listEl = document.getElementById("filelist");
  const sourceEl = document.getElementById("library-source");
  const statusEl = document.getElementById("library-status");
  const filesInput = document.getElementById("import-files");
  const folderInput = document.getElementById("import-folder");
  const filesButton = document.getElementById("btn-import-files");
  const folderButton = document.getElementById("btn-import-folder");
  const clearButton = document.getElementById("btn-clear-library");
  const clearDialog = document.getElementById("clear-library-dialog");
  let onOpenFile = null;
  let selectedPath = null;
  let staticCatalog = null;
  let localSongs = [];
  let currentPath = "";
  let busy = true;

  /**
   * Enable controls only when no storage operation is pending.
   *
   * @param {boolean} pending - whether an import, removal or restore is active.
   * @returns {void}
   */
  function setBusy(pending) {
    busy = pending;
    filesButton.disabled = pending;
    folderButton.disabled = pending || !("webkitdirectory" in folderInput);
    sourceEl.disabled = pending;
    clearButton.disabled = pending || sourceEl.value !== "local" || !localSongs.length;
    listEl.querySelectorAll(".remove-song").forEach((button) => { button.disabled = pending; });
  }

  /**
   * Explain storage failures without reporting unsaved songs as imported.
   *
   * @param {Error} error - failed browser storage operation.
   * @returns {void}
   */
  function storageError(error) {
    statusEl.textContent = error.name === "QuotaExceededError"
      ? "Browser storage is full. Remove some saved songs and try again."
      : "Could not access your saved library: " + error.message;
  }

  /**
   * Remember the chosen collection without making preference storage required.
   *
   * @returns {void}
   */
  function rememberSource() {
    try { localStorage.setItem("guitarz.librarySource", sourceEl.value); } catch (e) { /* preferences are optional */ }
  }

  /**
   * Build one folder level from saved relative paths, with safe DOM labels.
   *
   * @param {string} path - folder within the imported library.
   * @returns {{path: string, dirs: Array, files: Array}} explorer listing.
   */
  function localListing(path) {
    const prefix = path ? path + "/" : "";
    const dirs = new Map();
    const files = [];
    for (const song of localSongs) {
      if (!song.path.startsWith(prefix)) continue;
      const rest = song.path.slice(prefix.length);
      const slash = rest.indexOf("/");
      if (slash >= 0) {
        const name = rest.slice(0, slash);
        dirs.set(name, { name, path: prefix + name });
      } else {
        files.push(song);
      }
    }
    return { path, dirs: [...dirs.values()].sort((a, b) => a.name.localeCompare(b.name)), files };
  }

  /**
   * Save picker selections and refresh the library only after commit succeeds.
   *
   * @param {HTMLInputElement} input - file or folder input with selected files.
   * @returns {Promise<void>} completes when the explorer reflects the import.
   */
  async function importSelection(input) {
    if (!input.files.length || busy) return;
    setBusy(true);
    statusEl.textContent = "Saving songs…";
    try {
      const result = await Guitarz.library.importFiles(input.files);
      if (result.saved) {
        localSongs = await Guitarz.library.list();
        sourceEl.value = "local";
        rememberSource();
        await browse("");
        statusEl.textContent = `Saved ${result.saved} song${result.saved === 1 ? "" : "s"} in this browser.`;
      } else {
        statusEl.textContent = "No supported Guitar Pro files selected.";
      }
      if (result.skipped) statusEl.textContent += ` Skipped ${result.skipped} unsupported or hidden file${result.skipped === 1 ? "" : "s"}.`;
    } catch (error) {
      storageError(error);
    } finally {
      input.value = ""; // allow reimporting the same file after editing it
      setBusy(false);
    }
  }

  /**
   * Delete saved copies and update the explorer after the transaction commits.
   *
   * @param {string|null} path - one song path, or null to clear the whole library.
   * @returns {Promise<void>} completes after refreshing the remaining library.
   */
  async function removeSaved(path) {
    if (busy) return;
    setBusy(true);
    try {
      if (path === null) await Guitarz.library.clear();
      else await Guitarz.library.remove(path);
      localSongs = await Guitarz.library.list();
      if (selectedPath === "local:" + path || path === null) selectedPath = null;
      await browse(path === null ? "" : currentPath);
      statusEl.textContent = path === null ? "Saved library cleared. Original files are unchanged." : "Saved song removed. Original file is unchanged.";
    } catch (error) {
      storageError(error);
    } finally {
      setBusy(false);
    }
  }

  /**
   * Format a byte count for the file list's size hint.
   *
   * Coarse units are enough here — the size exists only to distinguish a
   * full transcription from a fragment at a glance.
   *
   * @param {number} bytes - file size from the API.
   * @returns {string} human-readable size, e.g. "43 KB".
   */
  function fmtSize(bytes) {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + " KB";
    return (bytes / 1024 / 1024).toFixed(1) + " MB";
  }

  /**
   * Fetch one directory level from the server and render it.
   *
   * The path is encoded with encodeURIComponent because tab filenames
   * routinely contain "#" and spaces, which would otherwise truncate the
   * query string.
   *
   * @param {string} path - root-relative directory ("" for the root).
   * @returns {Promise<void>} resolves after the panel is re-rendered.
   */
  async function browse(path) {
    try {
      let data;
      currentPath = path || "";
      if (sourceEl.value === "local") {
        data = localListing(currentPath);
      } else if (window.GuitarzStatic) {
        if (!staticCatalog) {
          const res = await fetch("/catalog.json");
          if (!res.ok) throw new Error("catalog unavailable");
          staticCatalog = await res.json();
        }
        data = Object.hasOwn(staticCatalog, path || "") ? staticCatalog[path || ""] : null;
        if (!data) throw new Error("directory not found");
      } else {
        const res = await fetch("/api/browse?path=" + encodeURIComponent(path || ""));
        if (!res.ok) throw new Error("directory unavailable");
        data = await res.json();
      }
      renderBreadcrumb(data.path);
      renderList(data);
    } catch (e) {
      listEl.innerHTML = "<li>error loading directory</li>";
    }
  }

  /**
   * Rebuild the breadcrumb trail for the current directory.
   *
   * Every segment (including "root") is a link that re-browses that level,
   * which is the only "go up" affordance besides the ".." row.
   *
   * @param {string} path - root-relative path of the directory being shown.
   */
  function renderBreadcrumb(path) {
    breadcrumbEl.innerHTML = "";
    const mkLink = (label, target) => {
      const a = document.createElement("a");
      a.textContent = label;
      a.onclick = () => browse(target);
      return a;
    };
    breadcrumbEl.appendChild(mkLink(sourceEl.value === "local" ? "My library" : "root", ""));
    if (path) {
      let acc = "";
      for (const part of path.split("/")) {
        acc = acc ? acc + "/" + part : part;
        breadcrumbEl.appendChild(document.createTextNode(" / "));
        breadcrumbEl.appendChild(mkLink(part, acc));
      }
    }
  }

  /**
   * Render the directory listing: ".." row, folders, then files.
   *
   * Rows are DOM-built (not innerHTML-concatenated) so arbitrary filenames
   * can never inject markup. Clicking a file marks it selected and hands it
   * to the conductor via the callback registered in init().
   *
   * @param {{path: string, dirs: Array<{name: string, path: string}>,
   *          files: Array<{name: string, path: string, size: number}>}} data
   *        one directory level as returned by /api/browse.
   */
  function renderList(data) {
    listEl.innerHTML = "";
    if (data.path) {
      const up = document.createElement("li");
      up.className = "dir";
      up.textContent = "..";
      up.onclick = () => browse(data.path.split("/").slice(0, -1).join("/"));
      listEl.appendChild(up);
    }
    for (const d of data.dirs) {
      const li = document.createElement("li");
      li.className = "dir";
      li.textContent = d.name;
      li.onclick = () => browse(d.path);
      listEl.appendChild(li);
    }
    for (const f of data.files) {
      const li = document.createElement("li");
      li.className = "file";
      const selectionKey = sourceEl.value + ":" + f.path;
      if (selectionKey === selectedPath) li.classList.add("selected");
      li.textContent = f.name;
      const size = document.createElement("span");
      size.className = "size";
      size.textContent = fmtSize(f.size);
      li.appendChild(size);
      if (f.local) {
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "remove-song";
        remove.textContent = "×";
        remove.title = "Remove saved copy";
        remove.setAttribute("aria-label", "Remove " + f.name + " from library");
        remove.disabled = busy;
        remove.onclick = (event) => {
          event.stopPropagation();
          removeSaved(f.path);
        };
        li.appendChild(remove);
      }
      li.onclick = () => {
        selectedPath = selectionKey;
        listEl.querySelectorAll("li.selected").forEach((el) => el.classList.remove("selected"));
        li.classList.add("selected");
        if (onOpenFile) onOpenFile(f);
      };
      listEl.appendChild(li);
    }
    if (!data.dirs.length && !data.files.length) {
      const li = document.createElement("li");
      li.textContent = sourceEl.value === "local" ? "No saved songs here. Open files or a folder." : "(no Guitar Pro files here)";
      listEl.appendChild(li);
    }
  }

  Guitarz.explorer = {
    /**
     * Restore the browser library, wire pickers, and show the chosen collection.
     *
     * @param {(file: {name: string, path: string}) => void} openFileCallback
     *        invoked when the user clicks a song; receives the API file entry.
     * @returns {Promise<void>} resolves after restoring and rendering the library.
     */
    async init(openFileCallback) {
      onOpenFile = openFileCallback;
      if (!window.GuitarzStatic) sourceEl.options[0].textContent = "Server files";
      filesButton.onclick = () => filesInput.click();
      folderButton.onclick = () => folderInput.click();
      filesInput.onchange = () => importSelection(filesInput);
      folderInput.onchange = () => importSelection(folderInput);
      sourceEl.onchange = () => {
        rememberSource();
        browse("");
        setBusy(false);
      };
      clearButton.onclick = () => clearDialog.showModal();
      document.getElementById("cancel-clear-library").onclick = () => clearDialog.close();
      document.getElementById("confirm-clear-library").onclick = () => {
        clearDialog.close();
        removeSaved(null);
      };
      try {
        localSongs = await Guitarz.library.list();
        let saved;
        try { saved = localStorage.getItem("guitarz.librarySource"); } catch (e) { /* use default collection */ }
        sourceEl.value = saved === "local" || (saved !== "remote" && localSongs.length) ? "local" : "remote";
      } catch (error) {
        storageError(error);
      }
      await browse("");
      setBusy(false);
    },
  };
})();
