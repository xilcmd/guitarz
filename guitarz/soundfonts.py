"""The SoundFonts guitarz knows about: menu labels and license files.

Shared by the FastAPI server's ``/api/soundfonts`` catalog and the static
site builder. Standard library only, because the static build must run
without the server's dependencies (Cloudflare's build image has no FastAPI).
"""

#: SoundFont formats alphaTab's synth reads (.sf3 = Vorbis-compressed samples).
SOUNDFONT_EXTENSIONS = {".sf2", ".sf3"}

#: The small font alphaTab ships; fetch_vendor.sh always installs it, and it
#: is the static site's default so visitors only download a big font on demand.
CORE_SOUNDFONT = "sonivox.sf2"

#: Curated menu labels for the fonts fetch_vendor.sh installs; any other font
#: dropped into the folder is labeled by its file stem.
SOUNDFONT_LABELS = {
    "MuseScore_General.sf3": "MuseScore General",
    "GeneralUser-GS.sf2": "GeneralUser GS",
    CORE_SOUNDFONT: "Sonivox (light)",
}

#: License file shipped next to each known font (fetched by fetch_vendor.sh).
#: Publishing a font means publishing its license too: MuseScore General's MIT
#: notice must travel with it, and the info dialog links these files. Only
#: fonts listed here may be published by the static build.
SOUNDFONT_LICENSES = {
    CORE_SOUNDFONT: "LICENSE",
    "MuseScore_General.sf3": "MuseScore_General_License.md",
    "GeneralUser-GS.sf2": "GeneralUser-GS_LICENSE.txt",
}


def soundfont_label(file_name: str) -> str:
    """Return the menu label for a SoundFont file.

    Known fonts get their curated name; anything else falls back to the
    file name without its extension, so a user-added font still gets a
    readable entry.

    Args:
        file_name: the font's file name, e.g. ``"GeneralUser-GS.sf2"``.

    Returns:
        The label shown in the transport's SoundFont menu.
    """
    if file_name in SOUNDFONT_LABELS:
        return SOUNDFONT_LABELS[file_name]
    return file_name.rsplit(".", 1)[0]
