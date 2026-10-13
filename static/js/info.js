/** Asset license dialog, available independently of player initialization. */
(function () {
  const Guitarz = (window.Guitarz = window.Guitarz || {});
  const dialog = document.getElementById("asset-info");
  const button = document.getElementById("btn-info");
  button.onclick = () => dialog.showModal();
  document.getElementById("asset-info-close").onclick = () => {
    dialog.close();
    button.focus();
  };
  dialog.addEventListener("close", () => button.focus());

  Guitarz.info = {
    /**
     * Show credits for optional fonts only when they are available in the app.
     *
     * The static export ships Sonivox alone, while the development server can
     * offer additional fonts with notices already stored beside those assets.
     *
     * @param {Array<{file: string}>} fonts - available SoundFont catalog entries.
     * @returns {void}
     */
    setSoundFonts(fonts) {
      const names = new Set(fonts.map((font) => font.file));
      const museScore = names.has("MuseScore_General.sf3");
      const generalUser = names.has("GeneralUser-GS.sf2");
      document.getElementById("musescore-credit").hidden = !museScore;
      document.getElementById("generaluser-credit").hidden = !generalUser;
      document.getElementById("optional-font-credits").hidden = !(museScore || generalUser);
    },
  };
})();
