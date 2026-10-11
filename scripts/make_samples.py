#!/usr/bin/env python3
"""Write the original sample songs in ``samples/`` used by the test suite.

The repo is public, so it can never carry songs from anyone's Guitar Pro
collection, yet the browser smoke tests need real multi-track songs to load,
render and play. These four tunes are composed right here, in code, from
plain chord shapes and patterns: no melody, riff or lyric is taken from an
existing song, so they are safe to commit and to run in CI.

Each sample exists to exercise one part of the app:

* ``guitarz-demo.gp5`` — seven tracks (four guitars, bass, organ, drums) in
  standard tuning: rendering, playback, track colors, fretboard, transpose.
* ``guitarz-lyrics.gp5`` — a vocal track carrying lyrics: the hide-vocals
  lyric hand-off. The word "edge" appears once, as a whole word.
* ``guitarz-dropd.gp5`` — a Drop D riff: the fretboard follows the tuning.
* ``guitarz-eb.gp5`` — every pitched track a half step down: auto transpose.

Output is deterministic (no timestamps, no randomness), so a test can check
that the committed files still match this script. Run it again after any
edit::

    .venv/bin/python scripts/make_samples.py
"""

from __future__ import annotations

from pathlib import Path

import guitarpro as gp

#: Sample key → file name in ``samples/``. Tests import this mapping.
SAMPLE_FILES = {
    "demo": "guitarz-demo.gp5",
    "lyrics": "guitarz-lyrics.gp5",
    "dropd": "guitarz-dropd.gp5",
    "eb": "guitarz-eb.gp5",
}

ARTIST = "guitarz samples"
CREDIT = "Original tune written in code for the guitarz tests"

STANDARD = [64, 59, 55, 50, 45, 40]
BASS = [43, 38, 33, 28]
DROP_D = [64, 59, 55, 50, 45, 38]

#: Open chord shapes, as {string: fret} with string 1 = high E.
CHORDS = {
    "G": {6: 3, 5: 2, 4: 0, 3: 0, 2: 0, 1: 3},
    "Em": {6: 0, 5: 2, 4: 2, 3: 0, 2: 0, 1: 0},
    "C": {5: 3, 4: 2, 3: 0, 2: 1, 1: 0},
    "D": {4: 0, 3: 2, 2: 3, 1: 2},
}

#: Root of each chord on a 4-string bass, as (string, fret).
BASS_ROOTS = {"G": (4, 3), "Em": (4, 0), "C": (3, 3), "D": (3, 5)}

#: Two-octave power-chord voicing per chord root, as {string: fret}.
POWER = {
    "G": {6: 3, 5: 5, 4: 5},
    "Em": {6: 0, 5: 2, 4: 2},
    "C": {5: 3, 4: 5, 3: 5},
    "D": {5: 5, 4: 7, 3: 7},
}

#: Lead-line motifs: per chord, two one-bar phrases of (string, fret, value).
#: ``value`` is the Guitar Pro duration (4 = quarter, 8 = eighth).
LEAD = {
    "G": [
        [(1, 3, 4), (2, 3, 8), (2, 0, 8), (3, 0, 4), (2, 3, 4)],
        [(2, 3, 8), (1, 0, 8), (1, 3, 4), (1, 5, 4), (1, 3, 4)],
    ],
    "Em": [
        [(1, 0, 4), (2, 0, 8), (2, 3, 8), (1, 0, 4), (3, 0, 4)],
        [(2, 5, 8), (2, 3, 8), (2, 0, 4), (3, 2, 4), (3, 0, 4)],
    ],
    "C": [
        [(2, 1, 4), (1, 0, 8), (2, 1, 8), (3, 0, 4), (2, 1, 4)],
        [(1, 0, 8), (1, 3, 8), (1, 0, 4), (2, 1, 4), (2, 3, 4)],
    ],
    "D": [
        [(1, 2, 4), (2, 3, 8), (1, 2, 8), (3, 2, 4), (2, 3, 4)],
        [(1, 5, 8), (1, 2, 8), (2, 3, 4), (3, 2, 4), (4, 0, 4)],
    ],
}

#: General MIDI drum keys used by the drum patterns.
KICK, SNARE, HIHAT, CRASH = 36, 38, 42, 49


# A beat is (duration value, [(string, fret), ...]); an empty list is a rest.
Beat = tuple[int, list[tuple[int, int]]]


def strum(chord: str) -> list[Beat]:
    """Four quarter-note strums of a full open chord.

    Args:
        chord: key into ``CHORDS``.

    Returns:
        One bar of beats.
    """
    notes = sorted(CHORDS[chord].items())
    return [(4, notes)] * 4


def arpeggio(chord: str) -> list[Beat]:
    """Eight eighth notes picking up and down through an open chord.

    The lowest string of the shape starts the bar, then the top three
    strings rock back and forth, a common acoustic picking pattern.

    Args:
        chord: key into ``CHORDS``.

    Returns:
        One bar of beats.
    """
    shape = CHORDS[chord]
    low = max(shape)
    order = [low, 3, 2, 1, 2, 3, 2, 1]
    return [(8, [(s, shape[s])]) for s in order]


def power_halves(chord: str) -> list[Beat]:
    """Two half-note power chords.

    Args:
        chord: key into ``POWER``.

    Returns:
        One bar of beats.
    """
    notes = sorted(POWER[chord].items())
    return [(2, notes), (2, notes)]


def bass_line(chord: str) -> list[Beat]:
    """Root, root, octave-up root, root, in quarter notes on a 4-string bass.

    Args:
        chord: key into ``BASS_ROOTS``.

    Returns:
        One bar of beats.
    """
    string, fret = BASS_ROOTS[chord]
    root = [(string, fret)]
    octave = [(string - 2, fret + 2)]
    return [(4, root), (4, root), (4, octave), (4, root)]


def organ_pad(chord: str) -> list[Beat]:
    """One whole-note chord held for the bar.

    Args:
        chord: key into ``CHORDS``.

    Returns:
        One bar of beats.
    """
    shape = CHORDS[chord]
    return [(1, sorted((s, f) for s, f in shape.items() if s <= 4))]


def drum_bar(crash: bool = False) -> list[Beat]:
    """A plain rock beat: hi-hat eighths, kick on 1 and 3, snare on 2 and 4.

    Drum notes store the General MIDI key as the "fret"; the string only has
    to be distinct within a beat, so hi-hat, snare and kick use strings 1-3.

    Args:
        crash: put a crash cymbal on the downbeat instead of the hi-hat.

    Returns:
        One bar of beats.
    """
    bar = []
    for i in range(8):
        hits = [(1, CRASH if crash and i == 0 else HIHAT)]
        if i in (0, 4):
            hits.append((3, KICK))
        if i in (2, 6):
            hits.append((2, SNARE))
        bar.append((8, hits))
    return bar


def lead_bar(chord: str, variant: int) -> list[Beat]:
    """One bar of the lead melody over a chord.

    Args:
        chord: key into ``LEAD``.
        variant: which of the chord's phrases to use (wraps around).

    Returns:
        One bar of beats.
    """
    phrase = LEAD[chord][variant % len(LEAD[chord])]
    return [(value, [(s, f)]) for s, f, value in phrase]


def new_song(title: str, tempo: int, bars: int) -> gp.Song:
    """Create an empty 4/4 song with numbered measure headers and no tracks.

    Args:
        title: song title.
        tempo: quarter notes per minute.
        bars: number of measures.

    Returns:
        A song ready for :func:`add_track` calls.
    """
    song = gp.Song(title=title, artist=ARTIST, copyright=CREDIT, tempo=tempo)
    song.measureHeaders = []
    song.tracks = []
    for i in range(bars):
        header = gp.MeasureHeader(number=i + 1, start=gp.Duration.quarterTime * (1 + 4 * i))
        song.addMeasureHeader(header)
    return song


def add_track(
    song: gp.Song,
    name: str,
    tuning: list[int],
    program: int,
    channel: int,
    bars: list[list[Beat]],
    drums: bool = False,
) -> gp.Track:
    """Append a track and fill its first voice bar by bar.

    Args:
        song: the song to extend; its measure headers must already exist.
        name: track name shown in the rail.
        tuning: open-string MIDI pitches, highest string first.
        program: General MIDI program (0-based).
        channel: MIDI channel; the next one is used for effects. Drums
            always use channel 9 (10 in 1-based MIDI numbering).
        bars: one list of beats per measure.
        drums: mark the track as percussion.

    Returns:
        The new track.
    """
    midi = gp.MidiChannel(
        channel=9 if drums else channel,
        effectChannel=9 if drums else channel + 1,
        instrument=0 if drums else program,
    )
    track = gp.Track(
        song,
        number=len(song.tracks) + 1,
        name=name,
        strings=[gp.GuitarString(n + 1, v) for n, v in enumerate(tuning)],
        channel=midi,
        isPercussionTrack=drums,
    )
    song.tracks.append(track)
    for measure, beats in zip(track.measures, bars):
        voice = measure.voices[0]
        for value, notes in beats:
            beat = gp.Beat(
                voice,
                duration=gp.Duration(value=value),
                status=gp.BeatStatus.normal if notes else gp.BeatStatus.rest,
            )
            beat.notes = [
                gp.Note(beat, value=fret, string=string, type=gp.NoteType.normal)
                for string, fret in notes
            ]
            voice.beats.append(beat)
    return track


def demo_song() -> gp.Song:
    """"Porch Light Shuffle": seven standard-tuned tracks over G-Em-C-D.

    24 bars at 112 bpm (about 51 seconds) so playback tests have plenty of
    song after the count-in.

    Returns:
        The demo song.
    """
    progression = ["G", "Em", "C", "D"] * 6
    song = new_song("Porch Light Shuffle", tempo=112, bars=len(progression))
    add_track(song, "Lead Guitar", STANDARD, 29, 0,
              [lead_bar(c, i // 4) for i, c in enumerate(progression)])
    add_track(song, "Rhythm Guitar", STANDARD, 27, 2, [strum(c) for c in progression])
    add_track(song, "Acoustic Guitar", STANDARD, 25, 4, [arpeggio(c) for c in progression])
    add_track(song, "Crunch Guitar", STANDARD, 30, 6, [power_halves(c) for c in progression])
    add_track(song, "Bass", BASS, 33, 10, [bass_line(c) for c in progression])
    add_track(song, "Organ", STANDARD, 16, 12, [organ_pad(c) for c in progression])
    add_track(song, "Drums", [0] * 6, 0, 9,
              [drum_bar(crash=i % 8 == 0) for i in range(len(progression))], drums=True)
    return song


#: Original words for the lyric sample, one word per sung note.
LYRICS = (
    "down by the river where the willows grow "
    "standing at the edge of the water slow "
    "sing a little song for the morning light "
    "carry it home through the summer night"
)

#: Vocal melody per chord: three quarter notes, as (string, fret).
VOCAL = {
    "G": [(3, 0), (2, 0), (2, 3)],
    "Em": [(2, 0), (3, 0), (3, 2)],
    "C": [(2, 1), (2, 3), (2, 1)],
    "D": [(3, 2), (2, 3), (1, 2)],
}


def lyrics_song() -> gp.Song:
    """"Edge of the Water": vocals with lyrics, two guitars and bass.

    The vocal sings three quarter notes and rests one beat per bar, one word
    per note, so the 36 words fill exactly 12 bars. Guitar Pro 5 stores the
    lyric track 1-based, so ``trackChoice=1`` binds them to the vocals.

    Returns:
        The lyric song.
    """
    progression = ["G", "Em", "C", "D"] * 3
    song = new_song("Edge of the Water", tempo=96, bars=len(progression))
    add_track(song, "Vocals", STANDARD, 52, 0,
              [[(4, [n]) for n in VOCAL[c]] + [(4, [])] for c in progression])
    add_track(song, "Guitar", STANDARD, 25, 2, [arpeggio(c) for c in progression])
    add_track(song, "Second Guitar", STANDARD, 27, 4, [strum(c) for c in progression])
    add_track(song, "Bass", BASS, 33, 6, [bass_line(c) for c in progression])
    lines = [gp.LyricLine(1, LYRICS)] + [gp.LyricLine() for _ in range(gp.Lyrics.maxLineCount - 1)]
    song.lyrics = gp.Lyrics(trackChoice=1, lines=lines)
    return song


def dropd_song() -> gp.Song:
    """"Low Tide Riff": a Drop D guitar riff on the open low D, with bass and drums.

    Returns:
        The Drop D song.
    """
    riff = [
        (8, [(6, 0), (5, 0), (4, 0)]), (8, [(6, 0)]), (8, [(6, 0)]), (8, [(6, 3), (5, 3), (4, 3)]),
        (8, [(6, 0)]), (8, [(6, 5), (5, 5), (4, 5)]), (8, [(6, 0)]), (8, [(6, 3), (5, 3), (4, 3)]),
    ]
    bass = [(4, [(3, 5)]), (4, [(3, 5)]), (4, [(3, 8)]), (4, [(3, 5)])]
    song = new_song("Low Tide Riff", tempo=100, bars=8)
    add_track(song, "Drop D Guitar", DROP_D, 30, 0, [riff] * 8)
    add_track(song, "Bass", BASS, 33, 2, [bass] * 8)
    add_track(song, "Drums", [0] * 6, 0, 9, [drum_bar(i == 0) for i in range(8)], drums=True)
    return song


def eb_song() -> gp.Song:
    """"Half Step Home": guitar and bass both tuned down a half step, plus drums.

    The shapes are the usual open chords; tuned down they sound in Gb, which
    auto transpose turns back into G (+1) for a standard-tuned guitar.

    Returns:
        The Eb song.
    """
    progression = ["G", "C", "D", "G"] * 2
    song = new_song("Half Step Home", tempo=104, bars=len(progression))
    add_track(song, "Guitar", [p - 1 for p in STANDARD], 27, 0, [strum(c) for c in progression])
    add_track(song, "Bass", [p - 1 for p in BASS], 33, 2, [bass_line(c) for c in progression])
    add_track(song, "Drums", [0] * 6, 0, 9,
              [drum_bar(i == 0) for i in range(len(progression))], drums=True)
    return song


BUILDERS = {"demo": demo_song, "lyrics": lyrics_song, "dropd": dropd_song, "eb": eb_song}


def write_samples(dest: Path) -> list[Path]:
    """Build every sample and write it as Guitar Pro 5 into ``dest``.

    Args:
        dest: folder to write into; created if missing.

    Returns:
        Paths of the written files, in ``SAMPLE_FILES`` order.
    """
    dest.mkdir(parents=True, exist_ok=True)
    written = []
    for key, name in SAMPLE_FILES.items():
        path = dest / name
        gp.write(BUILDERS[key](), str(path), version=(5, 1, 0))
        written.append(path)
    return written


def main() -> None:
    """Write the samples into the repo's ``samples/`` folder and list them.

    The folder is found from this script's own location, so it can be run
    from any directory. The written files must then be committed: a test
    checks that the committed samples match this script byte for byte.
    """
    dest = Path(__file__).resolve().parent.parent / "samples"
    for path in write_samples(dest):
        print(f"wrote {path.relative_to(dest.parent)}")


if __name__ == "__main__":
    main()
