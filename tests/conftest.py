"""Shared pytest fixtures for the guitarz test suite.

Every backend test runs against a throwaway music root built in ``tmp_path`` so
tests never depend on (or disturb) the developer's real tab collection, and so
each test starts from a known directory layout. The layout is deliberately
adversarial: it mixes allowed extensions, disallowed extensions, hidden
entries, and nested folders, because the browse/file endpoints' main job is
deciding what *not* to expose.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from guitarz.server import create_app


@pytest.fixture()
def music_root(tmp_path: Path) -> Path:
    """Build a disposable music-root directory tree for backend tests.

    The tree exercises every filtering rule the API must enforce:

    * ``song one.gp``           — allowed file at the root (space in name)
    * ``band/``                 — visible subfolder with a nested folder inside
    * ``band/Alpha song.gp5``   — allowed legacy-format file (sorting check)
    * ``band/riff.gp5``         — allowed legacy-format file
    * ``band/old.gp3``          — allowed GP3 legacy file
    * ``band/older.gp4``        — allowed GP4 legacy file
    * ``band/notes.txt``        — disallowed extension, must never be listed/served
    * ``.hidden.gp``            — hidden file, must never be listed
    * ``.hiddendir/``           — hidden folder, must never be listed
    * ``README.md``             — disallowed extension at the root

    Args:
        tmp_path: pytest's per-test temporary directory.

    Returns:
        The path to the populated music root.
    """
    (tmp_path / "band" / "nested").mkdir(parents=True)
    (tmp_path / ".hiddendir").mkdir()
    (tmp_path / "song one.gp").write_bytes(b"GP-BYTES-ROOT")
    (tmp_path / "band" / "Alpha song.gp5").write_bytes(b"GP5-ALPHA")
    (tmp_path / "band" / "riff.gp5").write_bytes(b"GP5-RIFF")
    (tmp_path / "band" / "old.gp3").write_bytes(b"GP3-OLD")
    (tmp_path / "band" / "older.gp4").write_bytes(b"GP4-OLDER")
    (tmp_path / "band" / "notes.txt").write_text("not a tab")
    (tmp_path / ".hidden.gp").write_bytes(b"secret")
    (tmp_path / "README.md").write_text("docs")
    return tmp_path


@pytest.fixture()
def client(music_root: Path) -> TestClient:
    """Create a FastAPI TestClient bound to an app serving ``music_root``.

    A fresh app instance per test keeps state (resolved root path) isolated,
    which matters because ``create_app`` captures the root in a closure.

    Args:
        music_root: the disposable directory tree fixture.

    Returns:
        A ``TestClient`` making in-process requests against the app.
    """
    return TestClient(create_app(music_root))
