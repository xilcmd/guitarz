"""Export a self-contained static site using only standard-library tooling.

Python is needed at build time only. The output serves original bundled songs
and Sonivox directly, with JSON catalogs replacing the live filesystem API.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from .soundfonts import CORE_SOUNDFONT, SOUNDFONT_LICENSES, soundfont_label

REPO_ROOT = Path(__file__).resolve().parent.parent
EXTENSIONS = {".gp", ".gp3", ".gp4", ".gp5", ".gpx"}
BUILD_MARKER = ".guitarz-static.json"


def validate_output(output: Path, source: Path) -> None:
    """Refuse to replace source trees, unrelated folders or unmanaged files.

    New exports carry an inventory of their generated files and directories.
    The previous unmarked export is accepted only at the checkout's default
    dist/static location, with its identifying HTML flag and expected layout.

    Args:
        output: resolved destination directory.
        source: resolved source checkout.

    Raises:
        FileExistsError: destination is unsafe to replace.
    """
    for protected in (source, source / "static", source / "samples"):
        if protected == output or protected.is_relative_to(output):
            raise FileExistsError(f"Output overlaps source files: {output}")
    if output.is_relative_to(source / "static") or output.is_relative_to(source / "samples"):
        raise FileExistsError(f"Output overlaps source files: {output}")
    if not output.exists():
        return
    if not output.is_dir():
        raise FileExistsError(f"Output is not a directory: {output}")
    if not any(output.iterdir()):
        return
    entries = list(output.rglob("*"))
    if any(p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in entries):
        raise FileExistsError(f"Output contains links and cannot be replaced: {output}")
    marker = output / BUILD_MARKER
    if marker.is_file():
        try:
            inventory = json.loads(marker.read_text(encoding="utf-8"))
            files = {p.relative_to(output).as_posix() for p in entries if p.is_file() and p != marker}
            dirs = {p.relative_to(output).as_posix() for p in entries if p.is_dir()}
            if inventory["format"] == 1 and files == set(inventory["files"]) and dirs == set(inventory["dirs"]):
                return
        except (ValueError, KeyError, TypeError, OSError):
            pass
        raise FileExistsError(f"Output contains unmanaged files or an invalid build inventory: {output}")
    # Compatibility with exports generated before ownership inventories existed.
    legacy_names = {"css", "js", "licenses", "songs", "vendor", "index.html", "catalog.json", "soundfonts.json"}
    if output == source / "dist" / "static" and {p.name for p in output.iterdir()} <= legacy_names:
        try:
            if ("window.GuitarzStatic = true" in (output / "index.html").read_text(encoding="utf-8")
                    and isinstance(json.loads((output / "catalog.json").read_text(encoding="utf-8")), dict)
                    and json.loads((output / "soundfonts.json").read_text(encoding="utf-8"))["default"] == "sonivox.sf2"):
                return
        except (ValueError, KeyError, TypeError, OSError):
            pass
    raise FileExistsError(f"Output is not a recognized guitarz static build: {output}")


def build_static(output: Path, source: Path = REPO_ROOT, all_soundfonts: bool = False) -> Path:
    """Build and replace a generated static site without disturbing other data.

    A static-mode flag selects JSON catalogs and direct song URLs in the same
    frontend used by the local server. By default only the small Sonivox
    SoundFont ships (Cloudflare Pages caps files at 25 MiB); ``all_soundfonts``
    adds every known font with its license (GitHub Pages allows 100 MB files).
    Stage the complete export before replacing a recognized previous build.
    Failed builds leave the previous export intact; unrelated data is refused.

    Args:
        output: destination directory, including a previous generated export.
        source: source checkout containing static/ and samples/.
        all_soundfonts: also publish MuseScore General and GeneralUser GS.

    Returns:
        The absolute output path, ready to upload to a static host.

    Raises:
        FileExistsError: destination contains unrelated data or source files.
        FileNotFoundError: required player assets have not been downloaded.
    """
    if output.is_symlink() or getattr(output, "is_junction", lambda: False)():
        raise FileExistsError(f"Output must not be a filesystem link: {output}")
    output = output.resolve()
    source = source.resolve()
    validate_output(output, source)
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".guitarz-build-", dir=output.parent) as temporary:
        staging_root = Path(temporary).resolve()
        staged = staging_root / "site"
        previous = staging_root / "previous"
        write_static(staged, source, all_soundfonts)
        # The temporary root is created beside output. Only a validated export
        # can be moved inside it for cleanup, never a source or unrelated tree.
        assert staging_root.parent == output.parent and previous.is_relative_to(staging_root)
        existed = output.exists()
        if existed:
            output.rename(previous)
        try:
            staged.rename(output)
        except OSError:
            if existed:
                previous.rename(output)
            raise
    return output


def write_static(output: Path, source: Path, all_soundfonts: bool = False) -> None:
    """Assemble the static payload and ownership inventory in a staging folder.

    All reads, downloads checks and copies finish here before the public export
    is touched, so missing assets or copy errors cannot damage a previous build.

    Args:
        output: fresh staging directory.
        source: source checkout containing static/ and samples/.
        all_soundfonts: publish every font in ``SOUNDFONT_LICENSES``, each
            with its license file, instead of Sonivox alone.

    Raises:
        FileNotFoundError: required source assets are missing — including,
            with ``all_soundfonts``, any known font or its license, so a
            failed download fails the deploy instead of shrinking the menu.
    """
    static = source / "static"
    vendor = static / "vendor" / "alphatab"
    for asset in (vendor / "alphaTab.min.js", vendor / "font" / "Bravura.woff2",
                  vendor / "soundfont" / "sonivox.sf2", vendor / "soundfont" / "LICENSE"):
        if not asset.is_file():
            raise FileNotFoundError(f"Missing {asset}; run bash scripts/fetch_vendor.sh --core")
    published = sorted(SOUNDFONT_LICENSES) if all_soundfonts else [CORE_SOUNDFONT]
    missing = [
        name
        for font in published
        for name in (font, SOUNDFONT_LICENSES[font])
        if not (vendor / "soundfont" / name).is_file()
    ]
    if missing:
        raise FileNotFoundError(
            f"Missing SoundFont files {', '.join(missing)}; run bash scripts/fetch_vendor.sh"
        )
    # Copy the source UI separately from vendor assets so optional fonts can
    # never accidentally slip into the published output.
    shutil.copytree(static, output, ignore=shutil.ignore_patterns("vendor", BUILD_MARKER), dirs_exist_ok=True)
    target_vendor = output / "vendor" / "alphatab"
    shutil.copytree(vendor / "font", target_vendor / "font")
    shutil.copy2(vendor / "alphaTab.min.js", target_vendor / "alphaTab.min.js")
    (target_vendor / "soundfont").mkdir()
    for font in published:
        for name in (font, SOUNDFONT_LICENSES[font]):
            shutil.copy2(vendor / "soundfont" / name, target_vendor / "soundfont" / name)

    songs = source / "samples"
    catalog = {}
    for directory in [songs, *sorted(p for p in songs.rglob("*") if p.is_dir())]:
        relative = directory.relative_to(songs)
        if any(part.startswith(".") for part in relative.parts) or directory.is_symlink():
            continue
        key = "" if directory == songs else relative.as_posix()
        listing = {"path": key, "dirs": [], "files": []}
        for entry in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
            if entry.name.startswith(".") or entry.is_symlink():
                continue
            path = entry.relative_to(songs)
            item = {"name": entry.name, "path": path.as_posix()}
            if entry.is_dir():
                listing["dirs"].append(item)
            elif entry.suffix.lower() in EXTENSIONS:
                listing["files"].append({**item, "size": entry.stat().st_size})
                destination = output / "songs" / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(entry, destination)
        catalog[key] = listing
    (output / "catalog.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    # Same shape and label order as the server's /api/soundfonts. Sonivox
    # stays the default so visitors download a big font only when chosen.
    entries = [
        {"file": font, "label": soundfont_label(font), "url": "vendor/alphatab/soundfont/" + font}
        for font in published
    ]
    fonts = {
        "default": CORE_SOUNDFONT,
        "fonts": sorted(entries, key=lambda f: f["label"].lower()),
    }
    (output / "soundfonts.json").write_text(json.dumps(fonts, indent=2), encoding="utf-8")
    index = output / "index.html"
    html = index.read_text(encoding="utf-8").replace(
        '<script src=', '<script>window.GuitarzStatic = true;</script>\n<script src=', 1
    )
    index.write_text(add_content_versions(html, output), encoding="utf-8")
    entries = list(output.rglob("*"))
    inventory = {
        "format": 1,
        "files": sorted(p.relative_to(output).as_posix() for p in entries if p.is_file()),
        "dirs": sorted(p.relative_to(output).as_posix() for p in entries if p.is_dir()),
    }
    (output / BUILD_MARKER).write_text(json.dumps(inventory, indent=2), encoding="utf-8")


#: Local script and stylesheet references in index.html that get a version token.
_ASSET_REF = re.compile(
    r'(<script\b[^>]*\bsrc="|<link\b[^>]*\brel="stylesheet"[^>]*\bhref=")'
    r'((?![a-z][a-z0-9+.-]*:|//)[^"?#]+)(")'
)


def add_content_versions(html: str, root: Path) -> str:
    """Append ``?v=<content hash>`` to every local script and stylesheet URL.

    Static hosts give browsers no reason to re-check an unchanged URL (Python's
    ``http.server`` and GitHub Pages only send heuristic cache headers), so
    after a rebuild a browser could pair new HTML with an old ``style.css``:
    new buttons that seem to do nothing. A fingerprint of each file's bytes in
    its URL changes exactly when the file does, so updated files are fetched
    at once and unchanged ones stay cached. References may be relative
    (``js/app.js``) or root-absolute (``/js/app.js``); external URLs, ordinary
    links (license pages) and references to missing files are left as they are.

    Args:
        html: the page's HTML.
        root: folder the URLs are rooted at (the export).

    Returns:
        The HTML with version tokens added.
    """

    def version(match: re.Match) -> str:
        """Rewrite one matched reference, if its file exists."""
        path = root / match.group(2).lstrip("/")
        if not path.is_file():
            return match.group(0)
        token = hashlib.sha256(path.read_bytes()).hexdigest()[:10]
        return f"{match.group(1)}{match.group(2)}?v={token}{match.group(3)}"

    return _ASSET_REF.sub(version, html)


def main() -> None:
    """Build the static site from a checkout, defaulting to dist/static/.

    Runs without installing the Python backend or its dependencies, making
    the command suitable for Cloudflare's build environment.

    Raises:
        SystemExit: required assets are missing or output cannot safely be replaced.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=REPO_ROOT / "dist" / "static")
    parser.add_argument(
        "--all-soundfonts",
        action="store_true",
        help="also publish MuseScore General and GeneralUser GS (too big for Cloudflare; fine on GitHub Pages)",
    )
    args = parser.parse_args()
    try:
        result = build_static(args.output, all_soundfonts=args.all_soundfonts)
    except OSError as exc:
        parser.error(str(exc))
    print(f"Static site built in {result}")


if __name__ == "__main__":
    main()
