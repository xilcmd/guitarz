"""Spec for the package metadata that must agree with the repo's LICENSE file.

The repo is public under GPL-3.0 (``LICENSE``); the package metadata is what
pip, PyPI and license scanners read, so it must say the same thing rather
than leave the license unstated.
"""

from importlib.metadata import metadata
from pathlib import Path

import pytest

tomllib = pytest.importorskip("tomllib")  # stdlib from Python 3.11

REPO_ROOT = Path(__file__).resolve().parent.parent


def _pyproject() -> dict:
    """Parse the repo's ``pyproject.toml``.

    Returns:
        The parsed TOML document.
    """
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_license_expression_matches_license_file():
    """``project.license`` is the SPDX expression for the GPL-3.0 LICENSE file.

    The LICENSE file is the verbatim GNU GPL version 3 text; the project
    chose "or any later version", so the expression is ``GPL-3.0-or-later``.
    """
    assert _pyproject()["project"]["license"] == "GPL-3.0-or-later"
    text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "GNU GENERAL PUBLIC LICENSE" in text and "Version 3, 29 June 2007" in text


def test_build_backend_supports_license_expressions():
    """The build requires a setuptools that understands SPDX license strings.

    A plain-string ``license`` (PEP 639) is only accepted by setuptools 77
    and newer; an older floor would let a build environment fail on it.
    """
    requires = _pyproject()["build-system"]["requires"]
    assert "setuptools>=77" in requires


def test_installed_metadata_carries_the_license():
    """The installed package reports the license expression to tools.

    Read from the installed distribution (``pip install -e .``), which is
    what pip and license scanners actually see.
    """
    assert metadata("guitarz")["License-Expression"] == "GPL-3.0-or-later"
