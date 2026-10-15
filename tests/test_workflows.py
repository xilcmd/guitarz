"""Spec for the GitHub Pages deploy workflow (#11).

The workflow is what puts the static site on https://xilcmd.github.io/guitarz/.
These checks read it as text (no YAML dependency) and pin the parts a careless
edit could break silently: when it runs, how it builds, and what it publishes.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PAGES = REPO_ROOT / ".github" / "workflows" / "pages.yml"


def _workflow() -> str:
    """Return the Pages workflow's text.

    Returns:
        The contents of ``.github/workflows/pages.yml``.
    """
    return PAGES.read_text(encoding="utf-8")


def test_pages_workflow_deploys_master_and_on_demand():
    """It runs on pushes to master (each merged PR) and can be run by hand."""
    text = _workflow()
    assert "push:" in text and "branches: [master]" in text
    assert "workflow_dispatch:" in text


def test_pages_workflow_builds_with_every_soundfont():
    """It fetches the full font set and builds with ``--all-soundfonts``.

    GitHub Pages allows 100 MB files and 1 GB sites, so unlike the
    Cloudflare-sized default build, the Pages site carries MuseScore General
    and GeneralUser GS like the Python server does. The builder still only
    reads ``samples/`` for songs — never a private collection.
    """
    text = _workflow()
    assert "./scripts/fetch_vendor.sh\n" in text and "fetch_vendor.sh --core" not in text
    assert "python -m guitarz.static_build --all-soundfonts" in text


def test_pages_workflow_caches_the_big_soundfonts():
    """The ~70 MB of optional fonts are cached between deploys.

    fetch_vendor.sh skips files that already exist, so restoring them from
    ``actions/cache`` keeps each deploy from re-downloading them from the
    font authors' servers (GeneralUser's author asks sites to host their own
    copy rather than lean on his downloads).
    """
    text = _workflow()
    assert "actions/cache@" in text
    for name in ("MuseScore_General.sf3", "GeneralUser-GS.sf2"):
        assert "static/vendor/alphatab/soundfont/" + name in text


def test_pages_workflow_publishes_the_build_output_with_least_privilege():
    """It uploads ``dist/static`` and deploys with only the Pages permissions.

    ``pages: write`` and ``id-token: write`` are what ``deploy-pages`` needs;
    repository contents stay read-only.
    """
    text = _workflow()
    assert "actions/upload-pages-artifact" in text and "path: dist/static" in text
    assert "actions/deploy-pages" in text
    assert "contents: read" in text and "pages: write" in text and "id-token: write" in text
