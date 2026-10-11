# guitarz

A conductor-view Guitar Pro player. Point it at a folder of tabs and it presents every
track of a song simultaneously as tablature — like a conductor's full score — with
synchronized playback, count-in, and per-track mute/solo/volume.

Built as a custom web UI on top of the open-source [alphaTab](https://alphatab.net)
rendering/playback library (MPL-2.0), with a small FastAPI backend that serves the
file explorer. All audio is synthesized in the browser from a SoundFont — no MIDI
hardware or drivers needed.

## Features

- **Conductor view** — all tracks rendered as stacked TAB staves (drums as percussion
  notation) with a synced playback cursor and auto-scroll
- **Color-coded tracks** — every string-instrument track (guitar, bass, …) gets its
  own color, shown as a chip in the track rail *and* as the tint of its staff in the
  score, so rail and stage match at a glance
- **Clean tab by default** — rhythm stems/beams ("tails") under the tab are hidden;
  the transport's `tails` button brings them back for full rhythm notation
- **Transpose** — shift playback up to ±12 semitones while the tab stays exactly as
  written (play along with detuned recordings, capo practice); drums are unaffected.
  Click the readout to reset to +0.
- **Auto transpose from tuning** — with `auto` on (the default), each song sets
  transpose from its own tuning, so you can play it on a guitar in standard tuning:
  a half-step-down (Eb/D#) song plays at +1, a D-standard song at +2, a standard
  song at 0. It only acts when every pitched track is shifted by the same amount;
  Drop D and other mixed tunings get 0. The fretboard's note names follow it, so an
  Eb song's open low string reads "E". With `auto` off, the transpose you set stays
  the same when you switch songs.
- **SoundFont menu** — pick the instrument sounds from the transport bar and compare
  them on the same song: MuseScore General (default), GeneralUser GS, or the tiny
  Sonivox fallback. Switching reloads the synth in place (song, loop, speed and
  transpose are kept) and the choice is remembered per browser. Any `.sf2`/`.sf3`
  dropped into `static/vendor/alphatab/soundfont/` appears in the menu too.
- **Fretboard pane** — the transport's `fretboard` button shows a guitar neck under
  the score that lights up the notes of one string track as it plays (pick the
  track in the pane). It follows each track's own tuning (Drop D, DADGAD, 4/5-string
  bass, …). In the pane:
  - faded dots preview the next notes to play
  - `bar` outlines every position the current bar uses (the chord shape or box)
  - `names` labels the dots with note names (A, C#, …) instead of fret numbers
  - `lefty` mirrors the neck for left-handed players
  - click a string at any fret (song stopped or paused) to hear that note
  - looks: rosewood/dots, ebony/diamonds, maple/blocks, and a 24-fret rosewood
    with a white tree-of-life vine (JEM style). Looks are plain data in `THEMES`
    in `static/js/fretboard.js`, so adding one is a small edit.

  All pane choices are remembered per browser.
- **Count-in** — one bar of clicks before playback (toggleable, on by default)
- **Per-track rail** — mute, solo, volume fader, and show/hide (eye) per track.
  Hiding a track that carries lyrics (the vocal track) removes its confusing tab
  staff but keeps the words on stage: the lyrics re-attach, bar-aligned, under the
  topmost visible track as a position reference, and return to the vocal staff
  when it is shown again
- **Transport** — play/pause (space bar), stop, 25–125% playback speed, bar + time
  position readout, click the progress bar to seek
- **Click-to-seek & loop** — click a beat in the score to jump there; drag-select a
  bar range and hit `loop` to practice a section
- **File explorer** — browses a configurable root folder (subfolders included) for
  `.gp` (Guitar Pro 7/8), `.gp5`, `.gp4`, and `.gp3` files
- **Deep links** — `/?open=<relative path>` auto-loads a song

## Setup

```bash
python3 -m venv .venv          # add --copies if the repo lives on /mnt/c under WSL
.venv/bin/pip install -e ".[dev]"
./scripts/fetch_vendor.sh      # downloads alphaTab JS, Bravura font, SoundFonts (one time, ~75 MB)
```

The last step is required: without it the page cannot find the alphaTab player
and every song fails to load. To try the app without your own tabs, run
`GUITARZ_ROOT=samples .venv/bin/guitarz` to browse the bundled sample songs.

## Development

This project is TDD: write the failing test first, then the code (see
`CLAUDE.md` for the full conventions, including the verbose-docstring rule).

```bash
.venv/bin/pytest                   # full suite, incl. headless-Chrome smoke test
.venv/bin/pytest -m "not browser"  # fast unit loop (no Chrome needed)
```

The browser smoke test (`tests/test_browser_smoke.py`) boots the real server on
a free port, drives the UI in headless Chrome over the DevTools protocol, and
asserts render + player-ready + playback actually advancing. It auto-skips if
Chrome or the vendored alphaTab assets are missing.

## Music folder (outside the repo)

Your Guitar Pro files are personal data, so they do not live in this repo.
guitarz serves `$GUITARZ_ROOT`, or `~/gps` when it is not set. Put this in
`~/.bashrc`:

```bash
export GUITARZ_ROOT=~/gps
```

If `GUITARZ_ROOT` names a folder that does not exist (for example a stale value
left in a long-running VS Code server under WSL), guitarz prints a warning and
uses `~/gps`. To clear a stale value for good, run `wsl --shutdown` from Windows
and reopen VS Code.

`gps/` is gitignored, so a stray copy in the project folder is never committed.
The browser smoke test copies its sample songs (Funk #49, Children Of The Sea,
and the first song in a `DropD` folder) from this folder and skips the tests
that need a song it cannot find.

## Run

As a managed background server (recommended day-to-day):

```bash
./scripts/server.sh start      # daemonize; logs to guitarz.log, pid in .guitarz.pid
./scripts/server.sh status
./scripts/server.sh restart
./scripts/server.sh stop
# configure via env: GUITARZ_ROOT=~/my-tabs GUITARZ_PORT=8008 ./scripts/server.sh start
```

Or in the foreground:

```bash
.venv/bin/guitarz --root ~/my-tabs        # default root: $GUITARZ_ROOT or ~/gps
# guitarz: serving /home/you/my-tabs at http://127.0.0.1:8008
```

Open <http://127.0.0.1:8008>, click a song in the Files panel, press play.

Options: `--root DIR` (music folder; default `$GUITARZ_ROOT` or `~/gps`), `--host` (default 127.0.0.1), `--port` (default 8008).

## gpinfo — collection inspector

The package also installs `gpinfo`, a CLI that identifies Guitar Pro files by
**content** (magic bytes, not extension) and reports version, artist, title, and
track names across all format generations — GP3/GP4/GP5 (binary), GPX/GP6
(recognized, not deep-parsed), and GP7/8 zip containers:

```bash
gpinfo ~/gps/                    # table: one line per file, recursive
gpinfo --json ~/gps/Standard    # machine-readable
gpinfo "some song.gp5" other.gp  # individual files
```

Corrupt or non-GP files produce a stderr diagnostic and never abort the scan.

`gpinfo --fix-ext <paths>` reports files whose extension mismatches their actual
content version (e.g. a GP5 binary named `.gp3`) — dry run by default, add
`--apply` to rename. GP7/8 files keep the standard `.gp` extension on purpose:
that is what the Guitar Pro app associates with.

## Notes

- Supported extensions are the `EXTENSIONS` constant in `guitarz/server.py`; alphaTab
  also reads `.gp3`/`.gp4`/`.gpx`, so adding them there is all it takes.
- The backend only ever serves files inside `--root` (path-traversal guarded).
- `samples/gp5-format-test.gp5` is a generated fixture used to verify legacy-format
  loading.
- `samples/guitarz-*.gp5` are original tunes composed in code by
  `scripts/make_samples.py` (no copyrighted music). The browser tests run on
  them, locally and in CI, so no private song collection is needed.
