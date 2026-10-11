"""Behavioral spec for the ``guitarz`` command-line entry point.

The CLI is a thin argparse wrapper around ``create_app`` + ``uvicorn.run``;
these tests verify argument validation and that user-supplied options actually
reach the server, with ``uvicorn.run`` monkeypatched so no socket is opened.
"""

import pytest

from guitarz import __main__ as cli


def _set_home(monkeypatch, path):
    """Point ``~`` at ``path`` on every OS for the duration of a test.

    ``Path.expanduser`` reads ``HOME`` on POSIX but ``USERPROFILE`` on
    Windows, so setting only ``HOME`` left the Windows CI jobs expanding
    ``~`` to the runner's real profile folder.

    Args:
        monkeypatch: pytest's monkeypatch fixture.
        path: directory to use as the home folder.
    """
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("USERPROFILE", str(path))


def test_nonexistent_root_exits_with_usage_error(monkeypatch, tmp_path):
    """``--root`` pointing at a missing directory aborts before serving.

    argparse's ``parser.error`` exits with status 2; getting that instead of a
    traceback is the difference between a usable CLI message and a crash.
    """
    monkeypatch.setattr(
        "sys.argv", ["guitarz", "--root", str(tmp_path / "nope")]
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2


def test_options_are_passed_through_to_uvicorn(monkeypatch, tmp_path):
    """``--host`` and ``--port`` reach ``uvicorn.run`` unchanged.

    ``uvicorn.run`` is stubbed to capture its arguments, proving the CLI wires
    user input into the server rather than serving hardcoded defaults.
    """
    captured = {}

    def fake_run(app, **kwargs):
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    monkeypatch.setattr(
        "sys.argv",
        ["guitarz", "--root", str(tmp_path), "--host", "0.0.0.0", "--port", "9001"],
    )
    cli.main()
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 9001
    assert captured["app"] is not None


def test_default_root_is_gps_in_home(monkeypatch, tmp_path):
    """With no ``GUITARZ_ROOT`` set, the music root is ``~/gps``.

    The tab collection is personal creative data and lives outside the source
    tree (like xil-pipeline's ``$XIL_PROJECTROOT``), so the default must point
    at the user's home, not at the repo or the current directory.
    """
    monkeypatch.delenv("GUITARZ_ROOT", raising=False)
    _set_home(monkeypatch, tmp_path)
    assert cli.default_root() == tmp_path / "gps"


def test_default_root_honors_env_var(monkeypatch, tmp_path):
    """``GUITARZ_ROOT`` overrides the ``~/gps`` default, with ``~`` expanded.

    Users keep the collection wherever they like; the env var is the one place
    to say so, shared by the CLI and both launcher scripts.
    """
    _set_home(monkeypatch, tmp_path)
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.setenv("GUITARZ_ROOT", "~/elsewhere")
    assert cli.default_root() == tmp_path / "elsewhere"


def test_stale_env_var_falls_back_to_gps_with_warning(monkeypatch, tmp_path, capsys):
    """A ``GUITARZ_ROOT`` naming a missing folder is ignored, loudly.

    Regression: on WSL the VS Code server kept a ``GUITARZ_ROOT`` from the
    old Windows-side layout (``/mnt/c/...``) after the collection moved to
    ``~/gps``. Every launch then died on "not a directory" and the browser
    smoke tests silently skipped. A stale env var must not shadow a working
    default, so ``default_root`` falls back to ``~/gps`` and says why on
    stderr — the user still learns their environment is wrong.
    """
    _set_home(monkeypatch, tmp_path)
    monkeypatch.setenv("GUITARZ_ROOT", "/mnt/c/no/such/guitarz")
    assert cli.default_root() == tmp_path / "gps"
    err = capsys.readouterr().err
    assert "/mnt/c/no/such/guitarz" in err and "~/gps" in err


def test_cli_serves_default_root_when_no_flag(monkeypatch, tmp_path):
    """Without ``--root`` the CLI serves ``default_root()``, not the cwd.

    The launcher scripts no longer pass ``--root``; they rely on this default,
    so it must reach ``create_app`` unchanged.
    """
    monkeypatch.setenv("GUITARZ_ROOT", str(tmp_path))
    served = {}
    monkeypatch.setattr(cli, "create_app", lambda root: served.setdefault("root", root))
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: None)
    monkeypatch.setattr("sys.argv", ["guitarz"])
    cli.main()
    assert served["root"] == tmp_path
