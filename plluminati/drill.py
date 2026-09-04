"""The riff repeater: loop a section and let the tempo climb.

This is the part nothing else does - section drilling where the speed is gated
on measured MIDI performance rather than a slider (SPEC 6 F3/F5).

Loop restart is a real procedure, not just "go back to the start": owned lights
out, owned notes released, count-in, then arm the first target. Without it the
first chord of every repeat is structurally harder than the rest.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .along import AlongReport, AlongSession
from .keyboard import Keyboard
from .matcher import RampPolicy
from .song import Song, Step


@dataclass
class DrillReport:
    hand: str
    bars: tuple[int, int] | None = None
    reps: int = 0
    start_speed: float = 0.6
    speed: float = 0.6
    best_clean_speed: float = 0.0
    history: list[AlongReport] = field(default_factory=list)

    @property
    def climbed(self) -> float:
        return self.speed - self.start_speed

    def summary(self) -> str:
        return (f"{self.reps} reps, {self.start_speed:.0%} -> {self.speed:.0%}"
                + (f", clean at {self.best_clean_speed:.0%}"
                   if self.best_clean_speed else ""))


class DrillSession:
    """Repeat a section, adjusting speed on measured performance."""

    def __init__(self, keyboard: Keyboard, song: Song, hand: str = "right",
                 bars: tuple[int, int] | None = None, speed: float = 0.6,
                 max_reps: int = 0, policy: RampPolicy | None = None,
                 accompany: bool = True, count_in: bool = True,
                 gap: float = 0.6) -> None:
        self.kb = keyboard
        self.song = song
        self.hand = hand
        self.bars = bars
        self.policy = policy or RampPolicy()
        self.speed = max(self.policy.floor, min(self.policy.ceiling, speed))
        self.max_reps = max_reps
        self.accompany = accompany
        self.count_in = count_in
        self.gap = gap

        self.steps = self._section_steps()
        self.report = DrillReport(hand=hand, bars=bars,
                                  start_speed=self.speed, speed=self.speed)

        self._stop = threading.Event()
        self._current: AlongSession | None = None

        self.on_rep_start = None     # callback(rep, speed)
        self.on_rep_end = None       # callback(AlongReport, new_speed)
        self.on_verdict = None
        self.on_cue = None
        self.on_tick = None

    def _section_steps(self) -> list[Step]:
        steps = self.song.steps_for_hand(self.hand)
        if self.bars:
            first, last = self.bars
            steps = [s for s in steps if first <= s.bar <= last]
        return steps

    @property
    def section_ticks(self) -> tuple[int, int] | None:
        """Persist sections by SOURCE TICKS, never step indexes - indexes move
        if quantisation or hand assignment changes later (SPEC 7.7)."""
        if not self.steps:
            return None
        return (self.steps[0].tick, self.steps[-1].tick)

    # ------------------------------------------------------------------ run

    def handle(self, msg) -> None:
        if self._current is not None:
            self._current.handle(msg)

    def run(self) -> DrillReport:
        if not self.steps:
            return self.report

        while not self._stop.is_set():
            if self.max_reps and self.report.reps >= self.max_reps:
                break

            rep = self.report.reps + 1
            if self.on_rep_start:
                self.on_rep_start(rep, self.speed)

            session = AlongSession(
                self.kb, self.song, hand=self.hand, speed=self.speed,
                steps=self.steps, accompany=self.accompany,
                count_in=self.count_in, policy=self.policy)
            session.on_verdict = self.on_verdict
            session.on_cue = self.on_cue
            session.on_tick = self.on_tick
            self._current = session

            result = session.run()
            self._current = None
            self.report.reps = rep
            self.report.history.append(result)

            if result.stopped_early:
                break

            if result.verdict == "clean":
                self.report.best_clean_speed = max(self.report.best_clean_speed,
                                                   self.speed)
            # monotonic within a session unless performance genuinely drops -
            # never dump the player back to the start on reaching the ceiling
            self.speed = result.next_speed
            self.report.speed = self.speed

            if self.on_rep_end:
                self.on_rep_end(result, self.speed)

            self._restart_gap()

        return self.report

    def _restart_gap(self) -> None:
        """Clean slate between repetitions."""
        self.kb.cues_clear()
        self.kb.stop_all()
        self.kb.port.flush(0.5)
        deadline = time.monotonic() + self.gap
        while time.monotonic() < deadline:
            if self._stop.is_set():
                return
            time.sleep(0.02)

    def stop(self) -> None:
        self._stop.set()
        if self._current is not None:
            self._current.stop()
