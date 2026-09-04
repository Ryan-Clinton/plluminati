"""Matching what was played against what was written, and scoring it.

Headless and deterministic (SPEC 6 F10). Four quantities are tracked
separately - hits, missed targets, wrong attacks, timing error - because
compressing them into one "accuracy" number internally makes the tempo ramp
impossible to reason about. The number shown to a player can still be simple.

Two rules matter more than the arithmetic:

1. **An attack matches the EARLIEST unmatched target of that pitch whose
   window contains it.** "Nearest" is ambiguous when two same-pitch targets
   have overlapping windows - an attack can sit nearer the second while the
   first is still outstanding, and the choice changes both recall and ramp
   decisions. Earliest-outstanding is causal and matches what a player expects.

2. **Live and final semantics are identical.** The same object produces the
   feedback shown while playing and the summary shown afterwards, so the result
   screen can never retroactively disagree with what the child just saw.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from enum import Enum

#: Acceptance window either side of a target onset, at 100% speed.
DEFAULT_TOLERANCE = 0.150
#: A chord must complete within this, tighter than the note window.
DEFAULT_CHORD_SPREAD = 0.080


class Judgement(str, Enum):
    HIT = "hit"
    WRONG = "wrong"          # matched no outstanding target
    DUPLICATE = "duplicate"  # right pitch, but that target is already taken


@dataclass(frozen=True)
class Target:
    id: int
    pitch: int
    time: float
    step: int = 0
    hand: str = "unknown"


@dataclass
class Verdict:
    judgement: Judgement
    pitch: int
    time: float
    target_id: int | None = None
    timing_error: float = 0.0        # +late, -early, seconds

    @property
    def ok(self) -> bool:
        return self.judgement is Judgement.HIT


@dataclass
class Score:
    targets: int = 0
    hits: int = 0
    missed: int = 0
    wrong_attacks: int = 0
    timing_errors: list[float] = field(default_factory=list)
    chord_spreads: dict[int, float] = field(default_factory=dict)

    @property
    def attacks(self) -> int:
        return self.hits + self.wrong_attacks

    @property
    def recall(self) -> float:
        """Fraction of written notes actually played in time."""
        return self.hits / self.targets if self.targets else 0.0

    @property
    def wrong_rate(self) -> float:
        return self.wrong_attacks / self.attacks if self.attacks else 0.0

    @property
    def mean_abs_timing(self) -> float:
        return statistics.fmean(abs(e) for e in self.timing_errors) if self.timing_errors else 0.0

    @property
    def mean_timing(self) -> float:
        """Signed: negative means consistently rushing."""
        return statistics.fmean(self.timing_errors) if self.timing_errors else 0.0

    @property
    def worst_chord_spread(self) -> float:
        return max(self.chord_spreads.values(), default=0.0)

    def summary(self) -> str:
        return (f"{self.hits}/{self.targets} notes, {self.wrong_attacks} wrong, "
                f"±{self.mean_abs_timing * 1000:.0f} ms")


class Matcher:
    """Feed it attacks as they happen; ask it for a Score at any point."""

    def __init__(self, targets: list[Target],
                 tolerance: float = DEFAULT_TOLERANCE,
                 chord_spread: float = DEFAULT_CHORD_SPREAD,
                 speed: float = 1.0) -> None:
        # Slower practice stretches everything in wall time, so the window
        # stretches with it - a 60% run is not meant to be tighter than a full-
        # speed one.
        self.tolerance = tolerance / max(0.05, speed)
        self.chord_spread = chord_spread / max(0.05, speed)
        self.targets = sorted(targets, key=lambda t: (t.time, t.pitch))
        self._by_pitch: dict[int, list[Target]] = {}
        for t in self.targets:
            self._by_pitch.setdefault(t.pitch, []).append(t)
        self._matched: dict[int, Verdict] = {}      # target id -> verdict
        self.verdicts: list[Verdict] = []

    # ---------------------------------------------------------------- live

    def attack(self, pitch: int, time: float) -> Verdict:
        """Judge one physical note-on. This IS the live feedback."""
        candidates = self._by_pitch.get(pitch, ())
        chosen: Target | None = None
        saw_taken = False

        for t in candidates:                        # already in time order
            if t.id in self._matched:
                if abs(time - t.time) <= self.tolerance:
                    saw_taken = True
                continue
            if abs(time - t.time) <= self.tolerance:
                chosen = t                          # EARLIEST outstanding wins
                break

        if chosen is None:
            v = Verdict(Judgement.DUPLICATE if saw_taken else Judgement.WRONG,
                        pitch, time)
        else:
            v = Verdict(Judgement.HIT, pitch, time, chosen.id, time - chosen.time)
            self._matched[chosen.id] = v

        self.verdicts.append(v)
        return v

    # --------------------------------------------------------------- final

    def score(self) -> Score:
        s = Score(targets=len(self.targets))
        for t in self.targets:
            v = self._matched.get(t.id)
            if v is None:
                s.missed += 1
            else:
                s.hits += 1
                s.timing_errors.append(v.timing_error)

        s.wrong_attacks = sum(1 for v in self.verdicts if not v.ok)

        # Chord spread is judged per step, and is a SEPARATE criterion: notes
        # that each land inside the window but arrive too rolled stay hits, and
        # only the chord-timing criterion fails. "All the right notes, too
        # spread out" is a more useful diagnosis than calling them misses.
        by_step: dict[int, list[float]] = {}
        for t in self.targets:
            v = self._matched.get(t.id)
            if v is not None:
                by_step.setdefault(t.step, []).append(v.time)
        for step, times in by_step.items():
            if len(times) > 1:
                s.chord_spreads[step] = max(times) - min(times)
        return s

    @property
    def remaining(self) -> int:
        return len(self.targets) - len(self._matched)


# ------------------------------------------------------------- ramp gating

@dataclass
class RampPolicy:
    """When to speed up, hold, or slow down (SPEC 6 F5).

    Three DISJOINT classes - without a defined 'poor' band the state
    transition is not implementable. The percentages are tuning parameters;
    the three-way structure is the part that must be fixed.
    """
    clean_recall: float = 0.95
    clean_wrong_rate: float = 0.10
    clean_timing: float = 0.120        # mean absolute error, seconds
    poor_recall: float = 0.70
    poor_wrong_rate: float = 0.30
    step: float = 0.10
    floor: float = 0.40
    ceiling: float = 1.00

    def classify(self, score: Score) -> str:
        if (score.recall >= self.clean_recall
                and score.wrong_rate <= self.clean_wrong_rate
                and score.mean_abs_timing <= self.clean_timing):
            return "clean"
        if (score.recall < self.poor_recall
                or score.wrong_rate > self.poor_wrong_rate):
            return "poor"
        return "hold"

    def next_speed(self, current: float, score: Score) -> tuple[float, str]:
        verdict = self.classify(score)
        if verdict == "clean":
            return min(self.ceiling, round(current + self.step, 3)), verdict
        if verdict == "poor":
            return max(self.floor, round(current - self.step, 3)), verdict
        return current, verdict


def targets_from_steps(steps, hand: str | None = None) -> list[Target]:
    """Flatten Step objects into Targets, optionally for one hand only."""
    out: list[Target] = []
    for st in steps:
        for note in st.targets:
            if hand and hand != "both" and note.hand != hand:
                continue
            out.append(Target(id=note.id, pitch=note.pitch,
                              time=note.start_time, step=st.index,
                              hand=note.hand))
    return out
