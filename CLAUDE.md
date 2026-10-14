# guitarz — project conventions

Conductor-view Guitar Pro player: FastAPI backend (file explorer + static serving),
vanilla-JS frontend on alphaTab (score rendering + SoundFont playback).

## Commands

```bash
.venv/bin/pip install -e ".[dev]"      # install with test dependencies
.venv/bin/pytest                       # full suite (browser smoke auto-skips without Chrome)
.venv/bin/pytest -m "not browser"      # fast unit loop for red-green cycles
.venv/bin/ruff check guitarz/ tests/   # lint (CI runs this; must be clean)
.venv/bin/guitarz                      # run the app at http://127.0.0.1:8008 (serves $GUITARZ_ROOT or ~/gps)
./scripts/fetch_vendor.sh              # (re)download alphaTab assets into static/vendor/
```

## Convention 1: TDD — non-negotiable

- **Write the failing test first.** No production code change without a test that
  demanded it: red → green → refactor.
- **Bugfixes start with a regression test** that reproduces the bug, then the fix.
- **New endpoints/behaviors**: spec them as tests in `tests/` before implementing.
- **Frontend changes**: covered by the headless-Chrome smoke test
  (`tests/test_browser_smoke.py`, marker `browser`). Extend it when adding UI
  behavior; it drives the real app over the Chrome DevTools Protocol.
- Run the full suite before any commit; all tests green.

## Git workflow

- `master` is protected: work on a branch (`feat/…`, `fix/…`, `chore/…`) and merge
  by PR (squash). The required check is `test (ubuntu-latest, 3.13)` from
  `.github/workflows/test.yml`; the other OS/Python jobs are informational.
- CI runs the browser smoke tests on Ubuntu only (it fetches alphaTab with
  `fetch_vendor.sh --core`); still run the full suite locally before a PR.
- Every merge to `master` also deploys the static site to
  https://xilcmd.github.io/guitarz/ (`.github/workflows/pages.yml`).
- The repo is public: never commit Guitar Pro songs from the collection. Only
  generated fixtures belong in git: `samples/gp5-format-test.gp5` and the
  original test songs `samples/guitarz-*.gp5`, written by
  `scripts/make_samples.py` (re-run it after editing; a test checks they match).

## Convention 2: verbose docstrings — every function

- **Python**: Google style on every module, function, and method — one-line summary,
  a paragraph of rationale (the *why*, constraints, non-obvious behavior), then
  `Args:` / `Returns:` / `Raises:` as applicable. Test functions get docstrings
  stating the behavior they pin down.
- **JavaScript**: JSDoc block on every function — summary, rationale, `@param` /
  `@returns` as applicable.

## Layout

- `guitarz/server.py` — FastAPI app factory (`create_app`), `/api/browse`, `/api/file`,
  path-traversal guard, `EXTENSIONS` allowlist, `/api/soundfonts` catalog (scans
  `static/vendor/alphatab/soundfont/`; default preference in `DEFAULT_SOUNDFONTS`)
- `guitarz/__main__.py` — CLI entry point; `default_root()` = `$GUITARZ_ROOT` or `~/gps`
- `~/gps` — the user's Guitar Pro collection, kept OUT of the repo (`gps/` is gitignored)
- `static/js/` — `app.js` (boot; resolves the SoundFont before alphaTab init),
  `explorer.js` (file tree), `conductor.js` (alphaTab + track rail), `transport.js`
  (playback controls + SoundFont menu), `fretboard.js` (clean-room SVG neck pane;
  looks/inlays are data in `THEMES`; dots follow `activeBeatsChanged`),
  `sidebar.js` (left column collapse/pin/peek and resize handle)
- `tests/` — pytest suite; `conftest.py` holds the tmp-root and live-server fixtures
- `static/vendor/` — fetched alphaTab assets, never hand-edited, gitignored
