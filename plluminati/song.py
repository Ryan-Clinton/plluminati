"""The song model: notes, steps, bars and time.

A *step* is the set of notes that begin together - what the trainer lights and
waits for. Steps keep their source Note objects rather than bare pitches,
because the same pitch can occur simultaneously in two tracks and collapsing
early loses that (SPEC 7.2).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from . import smf
from .keyboard import KEY_RANGE

#: Notes starting within this many seconds of a group's FIRST note belong to it.
#: Anchored to the first note, never chained: with onsets at 0/25/50 ms a
#: chaining algorithm would merge a 50 ms spread into one chord (SPEC 7.2).
CHORD_WINDOW = 0.030

DEFAULT_TEMPO = 500_000        # microseconds per quarter note = 120 bpm


@dataclass
class Note:
    id: int
    pitch: int
    start_tick: int
    end_tick: int
    channel: int
    track: int
    start_time: float = 0.0
    end_time: float = 0.0
    hand: str = "unknown"      # left | right | other | unknown

    @property
    def duration(self) -> float:
        return max(0.0, self.end_time - self.start_time)

    @property
    def playable(self) -> bool:
        return KEY_RANGE[0] <= self.pitch <= KEY_RANGE[1]


@dataclass
class Step:
    index: int
    tick: int
    time: float
    bar: int
    beat: float
    targets: list[Note] = field(default_factory=list)

    @property
    def pitches(self) -> list[int]:
        """Unique physical keys - satisfaction collapses here, the model does not."""
        return sorted({n.pitch for n in self.targets})

    @property
    def hands(self) -> set[str]:
        return {n.hand for n in self.targets}

    @property
    def playable(self) -> bool:
        return all(n.playable for n in self.targets)


class TempoMap:
    """Tick <-> seconds, honouring mid-song tempo changes."""

    def __init__(self, division: int, changes: list[tuple[int, int]]):
        self.division = division
        self.changes = sorted(changes) or [(0, DEFAULT_TEMPO)]
        if self.changes[0][0] != 0:
            self.changes.insert(0, (0, DEFAULT_TEMPO))
        # precompute cumulative seconds at each change
        self._marks: list[tuple[int, float, int]] = []
        secs = 0.0
        for i, (tick, tempo) in enumerate(self.changes):
            if i:
                prev_tick, prev_tempo = self.changes[i - 1]
                secs += (tick - prev_tick) * prev_tempo / 1e6 / division
            self._marks.append((tick, secs, tempo))

    def seconds(self, tick: int) -> float:
        mark_tick, mark_secs, tempo = self._marks[0]
        for t, s, tp in self._marks:
            if t <= tick:
                mark_tick, mark_secs, tempo = t, s, tp
            else:
                break
        return mark_secs + (tick - mark_tick) * tempo / 1e6 / self.division

    def bpm_at(self, tick: int) -> float:
        tempo = self._marks[0][2]
        for t, _, tp in self._marks:
            if t <= tick:
                tempo = tp
            else:
                break
        return 60_000_000 / tempo


class BarMap:
    """Bar numbers from the time-signature map.

    Policy: bar 1 begins at tick 0 unless overridden. Parsing time signatures
    does not by itself reproduce a musician's printed bar numbers when there is
    a pickup - be deterministic and let the user correct it (SPEC 7.2).
    """

    def __init__(self, division: int, sigs: list[tuple[int, int, int]],
                 pickup_ticks: int = 0):
        self.division = division
        self.pickup = pickup_ticks
        self.sigs = sorted(sigs) or [(0, 4, 4)]
        if self.sigs[0][0] != 0:
            self.sigs.insert(0, (0, 4, 4))
        self._marks: list[tuple[int, int, int, int]] = []   # tick, bar, num, den
        bar = 1
        for i, (tick, num, den) in enumerate(self.sigs):
            if i:
                ptick, pnum, pden = self.sigs[i - 1]
                bar_ticks = int(division * 4 / pden * pnum)
                if bar_ticks > 0:
                    bar += max(0, (tick - ptick) // bar_ticks)
            self._marks.append((tick, bar, num, den))

    def _bar_ticks(self, num: int, den: int) -> int:
        return max(1, int(self.division * 4 / den * num))

    def position(self, tick: int) -> tuple[int, float]:
        """(bar number, beat within bar, 1-based)."""
        tick = max(0, tick + self.pickup)
        mark = self._marks[0]
        for m in self._marks:
            if m[0] <= tick:
                mark = m
            else:
                break
        mtick, mbar, num, den = mark
        bar_ticks = self._bar_ticks(num, den)
        offset = tick - mtick
        bar = mbar + offset // bar_ticks
        within = offset % bar_ticks
        beat = 1 + within / (self.division * 4 / den)
        return int(bar), round(beat, 3)


@dataclass
class Song:
    path: str
    fingerprint: str
    format: int
    division: int
    tempo_map: TempoMap
    bar_map: BarMap
    notes: list[Note] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    track_names: dict[int, str] = field(default_factory=dict)
    programs: dict[int, int] = field(default_factory=dict)
    hand_detection: dict = field(default_factory=dict)

    # ------------------------------------------------------------- properties

    @property
    def duration(self) -> float:
        return max((n.end_time for n in self.notes), default=0.0)

    @property
    def bars(self) -> int:
        return self.bar_map.position(max((n.start_tick for n in self.notes),
                                         default=0))[0]

    @property
    def pitch_range(self) -> tuple[int, int]:
        if not self.notes:
            return (0, 0)
        return (min(n.pitch for n in self.notes), max(n.pitch for n in self.notes))

    @property
    def out_of_range(self) -> list[Note]:
        return [n for n in self.notes if not n.playable]

    @property
    def channels(self) -> list[int]:
        return sorted({n.channel for n in self.notes})

    def steps_for_hand(self, hand: str | None) -> list[Step]:
        if hand in (None, "both"):
            return self.steps
        return [s for s in self.steps if hand in s.hands]

    def steps_in_bars(self, first: int, last: int) -> list[Step]:
        return [s for s in self.steps if first <= s.bar <= last]


def _pair_notes(track: smf.Track, tempo: TempoMap) -> list[Note]:
    """Match note-ons to note-offs within one track."""
    open_notes: dict[tuple[int, int], list[Note]] = {}
    out: list[Note] = []
    for ev in track.events:
        key = (ev.channel, ev.note)
        if ev.kind == "note_on":
            n = Note(id=0, pitch=ev.note, start_tick=ev.tick, end_tick=ev.tick,
                     channel=ev.channel, track=track.index)
            open_notes.setdefault(key, []).append(n)
            out.append(n)
        elif ev.kind == "note_off":
            pending = open_notes.get(key)
            if pending:
                pending.pop(0).end_tick = ev.tick
    # anything never released ends where it started, rather than lasting forever
    for pending in open_notes.values():
        for n in pending:
            if n.end_tick <= n.start_tick:
                n.end_tick = n.start_tick
    for n in out:
        n.start_time = tempo.seconds(n.start_tick)
        n.end_time = tempo.seconds(n.end_tick)
    return out


def _build_steps(notes: list[Note], bar_map: BarMap,
                 window: float = CHORD_WINDOW) -> list[Step]:
    steps: list[Step] = []
    for note in sorted(notes, key=lambda n: (n.start_time, n.pitch)):
        if steps and (note.start_time - steps[-1].time) <= window:
            steps[-1].targets.append(note)      # anchored to the group's first note
            continue
        bar, beat = bar_map.position(note.start_tick)
        steps.append(Step(index=len(steps), tick=note.start_tick,
                          time=note.start_time, bar=bar, beat=beat,
                          targets=[note]))
    return steps


def load(path: str, pickup_ticks: int = 0) -> Song:
    with open(path, "rb") as fh:
        raw = fh.read()
    parsed = smf.read(raw)

    tempos = [(e.tick, e.tempo) for t in parsed.tracks for e in t.events
              if e.kind == "meta_tempo"]
    sigs = [(e.tick, e.numerator, e.denominator) for t in parsed.tracks
            for e in t.events if e.kind == "meta_timesig"]
    tempo_map = TempoMap(parsed.division, tempos)
    bar_map = BarMap(parsed.division, sigs, pickup_ticks)

    notes: list[Note] = []
    for track in parsed.tracks:
        notes.extend(_pair_notes(track, tempo_map))
    for i, n in enumerate(sorted(notes, key=lambda n: (n.start_tick, n.pitch))):
        n.id = i

    song = Song(
        path=path,
        fingerprint=hashlib.sha256(raw).hexdigest()[:16],
        format=parsed.format,
        division=parsed.division,
        tempo_map=tempo_map,
        bar_map=bar_map,
        notes=notes,
        track_names={t.index: t.name for t in parsed.tracks if t.name},
        programs={e.channel: e.program for t in parsed.tracks
                  for e in t.events if e.kind == "program"},
    )

    from .hands import detect
    song.hand_detection = detect(song)
    song.steps = _build_steps(notes, bar_map)
    return song
