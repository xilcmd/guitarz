"""Verify the static export contains everything needed without a backend."""

import hashlib
import json
import re
from pathlib import Path

import pytest

from guitarz import static_build
from guitarz.static_build import build_static


def test_cli_defaults_to_static_subdirectory(monkeypatch, tmp_path):
    """Reserve dist/ for multiple artifacts by exporting under dist/static/."""
    calls = []
    monkeypatch.setattr(static_build, "REPO_ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["guitarz.static_build"])
    monkeypatch.setattr(static_build, "build_static", lambda output: calls.append(output) or output)
    static_build.main()
    assert calls == [tmp_path / "dist" / "static"]


@pytest.fixture
def static_source(tmp_path):
    """Create a minimal checkout so build tests do not require downloaded assets."""
    source = tmp_path / "source"
    (source / "static" / "js").mkdir(parents=True)
    (source / "static" / "css").mkdir()
    (source / "static" / "css" / "style.css").write_text("body {}")
    (source / "static" / "index.html").write_text(
        '<link rel="stylesheet" href="/css/style.css">\n'
        '<script src="/vendor/alphatab/alphaTab.min.js"></script>\n'
        '<script src="/js/app.js"></script>\n'
        '<a href="/licenses/x.txt">license</a>'
    )
    (source / "static" / "js" / "app.js").write_text("// app")
    vendor = source / "static" / "vendor" / "alphatab"
    (vendor / "font").mkdir(parents=True)
    (vendor / "soundfont").mkdir()
    (vendor / "alphaTab.min.js").write_text("// player")
    (vendor / "font" / "Bravura.woff2").write_bytes(b"font")
    (vendor / "soundfont" / "sonivox.sf2").write_bytes(b"soundfont")
    (vendor / "soundfont" / "LICENSE").write_text("license")
    (vendor / "soundfont" / "huge.sf3").write_bytes(b"excluded")
    (source / "samples" / "Band").mkdir(parents=True)
    (source / "samples" / "Band" / "song #1.gp5").write_bytes(b"song")
    (source / "samples" / ".private.gp5").write_bytes(b"private")
    (source / "samples" / "notes.txt").write_text("notes")
    return source


def test_static_export(tmp_path, static_source):
    """Export nested, URL-sensitive songs and only the lightweight SoundFont."""
    output = tmp_path / "site"

    build_static(output, static_source)

    catalog = json.loads((output / "catalog.json").read_text())
    assert catalog[""]["dirs"] == [{"name": "Band", "path": "Band"}]
    assert catalog["Band"]["files"] == [{"name": "song #1.gp5", "path": "Band/song #1.gp5", "size": 4}]
    assert (output / "songs" / "Band" / "song #1.gp5").read_bytes() == b"song"
    assert sorted(p.name for p in (output / "vendor" / "alphatab" / "soundfont").iterdir()) == ["LICENSE", "sonivox.sf2"]
    assert not (output / "songs" / ".private.gp5").exists()
    assert not (output / "songs" / "notes.txt").exists()
    assert 'window.GuitarzStatic' in (output / "index.html").read_text()
    assert json.loads((output / "soundfonts.json").read_text())["default"] == "sonivox.sf2"


def test_missing_assets_leave_no_partial_export(tmp_path):
    """Fail clearly before creating an export when vendor assets are missing."""
    with pytest.raises(FileNotFoundError, match="fetch_vendor"):
        build_static(tmp_path / "site", tmp_path / "missing")
    assert not (tmp_path / "site").exists()


def test_refuse_nonempty_output(tmp_path):
    """Never overwrite an unrelated nonempty directory."""
    output = tmp_path / "site"
    output.mkdir()
    (output / "keep.txt").write_text("keep")
    with pytest.raises(FileExistsError):
        build_static(output, Path(__file__).resolve().parent.parent)
    assert (output / "keep.txt").read_text() == "keep"


def test_rebuild_replaces_generated_files_and_preserves_siblings(tmp_path, static_source):
    """Repeat builds refresh assets, remove stale files, and keep other artifacts."""
    checkout = static_source
    (checkout / "static" / "obsolete.txt").write_text("old")
    output = checkout / "dist" / "static"
    sibling = checkout / "dist" / "other"
    sibling.mkdir(parents=True)
    (sibling / "keep.txt").write_text("keep")
    build_static(output, checkout)
    (checkout / "static" / "obsolete.txt").unlink()
    (checkout / "static" / "js" / "app.js").write_text("new JS")
    build_static(output, checkout)
    assert not (output / "obsolete.txt").exists()
    assert (output / "js" / "app.js").read_text() == "new JS"
    assert (sibling / "keep.txt").read_text() == "keep"


def test_failed_rebuild_keeps_previous_export(tmp_path, static_source):
    """A missing dependency leaves the existing export intact."""
    output = tmp_path / "site"
    build_static(output, static_source)
    previous = (output / "index.html").read_bytes()
    with pytest.raises(FileNotFoundError, match="fetch_vendor"):
        build_static(output, tmp_path / "missing")
    assert (output / "index.html").read_bytes() == previous


def test_rebuild_refuses_unmanaged_files(tmp_path, static_source):
    """Adding unrelated data to a generated directory prevents its replacement."""
    output = tmp_path / "site"
    build_static(output, static_source)
    (output / "personal.txt").write_text("keep")
    with pytest.raises(FileExistsError, match="contains unmanaged files"):
        build_static(output, static_source)
    assert (output / "personal.txt").read_text() == "keep"


def test_previous_unmarked_export_can_be_rebuilt(static_source):
    """Upgrade the existing default export made before ownership markers existed."""
    output = static_source / "dist" / "static"
    build_static(output, static_source)
    (output / ".guitarz-static.json").unlink()
    (static_source / "static" / "js" / "app.js").write_text("updated JS")
    build_static(output, static_source)
    assert (output / "js" / "app.js").read_text() == "updated JS"


def _version(path: Path) -> str:
    """Return the cache-busting token the build should use for a file.

    Args:
        path: file whose bytes are fingerprinted.

    Returns:
        The first 10 hex digits of the file's SHA-256.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()[:10]


def test_scripts_and_styles_carry_a_content_version(tmp_path, static_source):
    """Every local script and stylesheet link gets ``?v=<content hash>``.

    Static hosts (python -m http.server, GitHub Pages) let browsers keep old
    copies of unchanged URLs, so after a rebuild a browser could run new HTML
    with an old style.css — new buttons that seemingly do nothing. A content
    fingerprint in the URL changes exactly when the file does, forcing a
    fresh download then and allowing caching otherwise. Ordinary links (like
    license pages) are left alone.
    """
    output = tmp_path / "site"
    build_static(output, static_source)
    html = (output / "index.html").read_text()
    for url, file in [
        ("/css/style.css", output / "css" / "style.css"),
        ("/vendor/alphatab/alphaTab.min.js", output / "vendor" / "alphatab" / "alphaTab.min.js"),
        ("/js/app.js", output / "js" / "app.js"),
    ]:
        assert f'"{url}?v={_version(file)}"' in html, (url, html)
    assert 'href="/licenses/x.txt"' in html
    assert len(re.findall(r"\?v=", html)) == 3


def test_content_version_changes_only_with_the_file(tmp_path, static_source):
    """Editing one file changes its version token and leaves the others alone."""
    output = tmp_path / "site"
    build_static(output, static_source)
    before = (output / "index.html").read_text()
    (static_source / "static" / "js" / "app.js").write_text("// app v2")
    build_static(output, static_source)
    after = (output / "index.html").read_text()
    assert f"/js/app.js?v={_version(output / 'js' / 'app.js')}" in after
    assert re.search(r"/js/app\.js\?v=\w+", before).group() != re.search(r"/js/app\.js\?v=\w+", after).group()
    assert re.search(r"/css/style\.css\?v=\w+", before).group() == re.search(r"/css/style\.css\?v=\w+", after).group()


def test_relative_and_external_references(tmp_path, static_source):
    """Relative asset links get versions; external URLs are never touched.

    The frontend now uses page-relative paths (``js/app.js``) so the export
    works inside a sub-folder such as GitHub Pages' ``/guitarz/`` (#11).
    Fingerprinting must handle those too, and must leave CDN or other
    external URLs alone because their bytes are not in the export.
    """
    (static_source / "static" / "index.html").write_text(
        '<link rel="stylesheet" href="css/style.css">\n'
        '<script src="js/app.js"></script>\n'
        '<script src="https://cdn.example.com/lib.js"></script>\n'
        '<script src="//cdn.example.com/other.js"></script>'
    )
    output = tmp_path / "site"
    build_static(output, static_source)
    html = (output / "index.html").read_text()
    assert f'"css/style.css?v={_version(output / "css" / "style.css")}"' in html
    assert f'"js/app.js?v={_version(output / "js" / "app.js")}"' in html
    assert '"https://cdn.example.com/lib.js"' in html
    assert '"//cdn.example.com/other.js"' in html


def test_soundfont_catalog_uses_a_page_relative_url(tmp_path, static_source):
    """The exported SoundFont URL has no leading slash, so it works in a sub-folder."""
    output = tmp_path / "site"
    build_static(output, static_source)
    fonts = json.loads((output / "soundfonts.json").read_text())
    assert fonts["fonts"][0]["url"] == "vendor/alphatab/soundfont/sonivox.sf2"
