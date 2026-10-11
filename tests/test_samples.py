"""Spec for the original sample songs bundled in ``samples/``.

The browser smoke tests need real Guitar Pro songs, but the repo is public
and must never carry songs from the user's collection. These samples are
made-up tunes written by ``scripts/make_samples.py`` (no copyrighted music),
so CI and every fresh clone can run the full UI suite. Each test below pins
one property a browser test depends on, so a careless edit to the generator
fails here, in the fast unit loop, instead of as a vague UI timeout.
"""

import importlib.util
from pathlib import Path

import guitarpro
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLES = REPO_ROOT / "samples"
GENERATOR = REPO_ROOT / "scripts" / "make_samples.py"

STANDARD = [64, 59, 55, 50, 45, 40]
BASS = [43, 38, 33, 28]
DROP_D = [64, 59, 55, 50, 45, 38]


def _load_generator():
    """Import ``scripts/make_samples.py`` as a module.

    ``scripts/`` is not a package, so the file is loaded by path. This keeps
    the generator a plain runnable script while still letting tests call it.

    Returns:
        The loaded generator module.
    """
    spec = importlib.util.spec_from_file_location("make_samples", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tuning(track) -> list[int]:
    """Return a track's open-string pitches, highest string first.

    Args:
        track: a PyGuitarPro track.

    Returns:
        MIDI pitch per string, in Guitar Pro's string order (1 = highest).
    """
    return [s.value for s in track.strings]


def _notes(track) -> int:
    """Count the notes a track plays across the whole song.

    Args:
        track: a PyGuitarPro track.

    Returns:
        Total number of notes in all measures and voices.
    """
    return sum(
        len(beat.notes)
        for measure in track.measures
        for voice in measure.voices
        for beat in voice.beats
    )


@pytest.fixture(scope="module")
def songs():
    """Parse every bundled sample once for the whole module.

    Returns:
        Dict of sample key (``demo``, ``lyrics``, ``dropd``, ``eb``) to the
        parsed PyGuitarPro song.
    """
    gen = _load_generator()
    return {key: guitarpro.parse(SAMPLES / name) for key, name in gen.SAMPLE_FILES.items()}


def test_generator_output_matches_committed_samples(tmp_path):
    """Running the generator reproduces the committed files byte for byte.

    The samples are build outputs checked in for CI's sake; this keeps them
    from drifting away from the script that documents how they were made.
    """
    gen = _load_generator()
    gen.write_samples(tmp_path)
    for name in gen.SAMPLE_FILES.values():
        assert (tmp_path / name).read_bytes() == (SAMPLES / name).read_bytes(), (
            f"{name} is stale: run scripts/make_samples.py"
        )


def test_demo_song_has_seven_tracks_with_guitar_first(songs):
    """The main demo has 7 tracks, a standard-tuned guitar first, a bass, and drums.

    The load test counts 7 rail rows; the fretboard tests start on the first
    string track and expect a 6-string standard neck; the bass test looks for
    a track named "Bass"; the color test needs both string and drum tracks.
    """
    song = songs["demo"]
    assert len(song.tracks) == 7
    first = song.tracks[0]
    assert not first.isPercussionTrack and _tuning(first) == STANDARD
    bass = [t for t in song.tracks if "bass" in t.name.lower()]
    assert bass and _tuning(bass[0]) == BASS
    assert any(t.isPercussionTrack for t in song.tracks)
    for track in song.tracks:
        if not track.isPercussionTrack and len(track.strings) == 6:
            assert _tuning(track) == STANDARD, f"{track.name} must be standard (auto transpose 0)"


def test_demo_song_is_long_enough_to_play(songs):
    """The demo runs well past the count-in plus the 8 s the play tests wait.

    At its tempo every track must play notes in the first 16 bars, which the
    fretboard preview and bar-shape tests walk.
    """
    song = songs["demo"]
    seconds = len(song.measureHeaders) * 4 * 60 / song.tempo
    assert seconds >= 30
    for track in song.tracks:
        first16 = sum(
            len(b.notes) for m in track.measures[:16] for v in m.voices for b in v.beats
        )
        assert first16 > 0, f"{track.name} is silent in the first 16 bars"


def test_lyrics_song_puts_lyrics_on_the_vocal_track(songs):
    """The lyric sample has vocals first, then two pitched tracks, with "edge" in the words.

    The hide-vocals test hides row 0 and then row 1, expecting the lyrics to
    land on track 1 and then track 2, and finds them by the word "edge".
    """
    song = songs["lyrics"]
    assert "vocal" in song.tracks[0].name.lower()
    assert len(song.tracks) >= 3
    assert all(not t.isPercussionTrack for t in song.tracks[:3])
    assert song.lyrics.trackChoice == 1  # 1-based in GP5: the first track
    text = " ".join(line.lyrics for line in song.lyrics.lines).lower()
    assert "edge" in text.split()
    assert _notes(song.tracks[0]) > 0


def test_dropd_song_has_a_drop_d_guitar(songs):
    """The Drop D sample has a guitar with the low string tuned to D2 (MIDI 38)."""
    song = songs["dropd"]
    assert any(_tuning(t) == DROP_D and _notes(t) > 0 for t in song.tracks)


def test_eb_song_is_a_half_step_down_everywhere(songs):
    """Every pitched track of the Eb sample is one semitone below standard.

    That uniform shift is what makes auto transpose pick +1. The first track
    is the guitar the fretboard opens on, so its low string must be Eb2.
    """
    song = songs["eb"]
    assert _tuning(song.tracks[0]) == [p - 1 for p in STANDARD]
    for track in song.tracks:
        if track.isPercussionTrack:
            continue
        want = STANDARD if len(track.strings) == 6 else BASS
        assert _tuning(track) == [p - 1 for p in want], track.name


def test_sample_names_say_they_are_original(songs):
    """Every sample credits itself as an original made for guitarz.

    A visible credit makes it obvious to anyone browsing the repo that these
    are not songs from a commercial catalog.
    """
    for song in songs.values():
        assert song.artist == "guitarz samples"
