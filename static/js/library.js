/** Browser-owned song copies persisted in IndexedDB; no network access. */
(function () {
  const Guitarz = (window.Guitarz = window.Guitarz || {});
  const EXTENSIONS = /\.(gp|gp3|gp4|gp5|gpx)$/i;
  let database = null;

  /**
   * Open the library database, reusing one connection until it is upgraded.
   *
   * Failures reset the promise so a later user action can retry. Blocked
   * upgrades ask the user to close other tabs rather than waiting forever.
   *
   * @returns {Promise<IDBDatabase>} opened database connection.
   */
  function openDatabase() {
    if (!database) {
      database = new Promise((resolve, reject) => {
        const request = indexedDB.open("guitarz-library", 1);
        request.onupgradeneeded = () => request.result.createObjectStore("songs", { keyPath: "path" });
        request.onerror = () => reject(request.error);
        request.onblocked = () => reject(new Error("Close other guitarz tabs and try again."));
        request.onsuccess = () => {
          const db = request.result;
          db.onversionchange = () => {
            db.close();
            database = null;
          };
          resolve(db);
        };
      }).catch((error) => {
        database = null;
        throw error;
      });
    }
    return database;
  }

  /**
   * Complete an atomic database operation before reporting success.
   *
   * Imports queue all writes in one transaction. Storage exhaustion aborts
   * the entire batch, preserving the existing library without partial saves.
   *
   * @param {IDBTransactionMode} mode - readonly or readwrite.
   * @param {(store: IDBObjectStore) => (IDBRequest|undefined)} run - queues requests.
   * @returns {Promise<*>} request result after the transaction commits.
   */
  async function transact(mode, run) {
    const db = await openDatabase();
    return new Promise((resolve, reject) => {
      const transaction = db.transaction("songs", mode);
      let request;
      transaction.oncomplete = () => resolve(request ? request.result : undefined);
      transaction.onabort = () => reject(transaction.error || new Error("Song storage operation was aborted."));
      try {
        request = run(transaction.objectStore("songs"));
      } catch (error) {
        transaction.abort();
        reject(error);
      }
    });
  }

  Guitarz.library = {
    /**
     * Read lightweight library metadata for explorer navigation.
     *
     * Blob contents remain unread until the user opens a song. Removing blobs
     * from the returned records keeps file bytes out of explorer state.
     *
     * @returns {Promise<Array<{path: string, name: string, size: number, local: boolean}>>} songs.
     */
    async list() {
      const records = await transact("readonly", (store) => store.getAll());
      return records.map(({ path, name, size }) => ({ path, name, size, local: true }))
        .sort((a, b) => a.path.localeCompare(b.path));
    },

    /**
     * Save selected songs, retaining relative folder paths and replacing matches.
     *
     * Unsupported files and hidden entries are ignored. Same-path imports
     * replace earlier copies so users can refresh edited originals explicitly.
     *
     * @param {Iterable<File>} files - files from a file or directory picker.
     * @returns {Promise<{saved: number, skipped: number}>} import summary.
     */
    async importFiles(files) {
      const records = new Map();
      let skipped = 0;
      for (const file of files) {
        const path = file.webkitRelativePath || file.name;
        const parts = path.split("/");
        if (!EXTENSIONS.test(file.name) || parts.some((part) => !part || part.startsWith(".") || part.includes("\\"))) {
          skipped++;
          continue;
        }
        records.set(path, { path, name: file.name, size: file.size, blob: file.slice() });
      }
      if (records.size) {
        await transact("readwrite", (store) => {
          for (const record of records.values()) store.put(record);
        });
      }
      return { saved: records.size, skipped };
    },

    /**
     * Retrieve a saved song's bytes without accessing the original file.
     *
     * @param {string} path - library-relative path.
     * @returns {Promise<Blob>} stored song copy.
     */
    async read(path) {
      const record = await transact("readonly", (store) => store.get(path));
      if (!record) throw new Error("Song is no longer in your library.");
      return record.blob;
    },

    /**
     * Remove one browser-owned copy; the original disk file is untouched.
     *
     * @param {string} path - library-relative path to remove.
     * @returns {Promise<void>} resolves when removal commits.
     */
    async remove(path) {
      await transact("readwrite", (store) => store.delete(path));
    },

    /**
     * Remove all song copies stored by this site in the current browser.
     *
     * Playback preferences and original disk files are independent of this
     * database and remain available after clearing the song library.
     *
     * @returns {Promise<void>} resolves after the library is cleared.
     */
    async clear() {
      await transact("readwrite", (store) => store.clear());
    },
  };
})();
