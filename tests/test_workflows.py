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


def test_pages_workflow_builds_the_core_static_site():
    """It fetches only the core alphaTab assets and runs the static builder.

    ``--core`` keeps the large optional SoundFonts out of the download, and
    the builder only ever reads ``samples/`` — never a private collection.
    """
    text = _workflow()
    assert "./scripts/fetch_vendor.sh --core" in text
    assert "python -m guitarz.static_build" in text


def test_pages_workflow_publishes_the_build_output_with_least_privilege():
    """It uploads ``dist/static`` and deploys with only the Pages permissions.

    ``pages: write`` and ``id-token: write`` are what ``deploy-pages`` needs;
    repository contents stay read-only.
    """
    text = _workflow()
    assert "actions/upload-pages-artifact" in text and "path: dist/static" in text
    assert "actions/deploy-pages" in text
    assert "contents: read" in text and "pages: write" in text and "id-token: write" in text
