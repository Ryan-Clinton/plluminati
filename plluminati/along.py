"""Play-along mode: the clock runs, and the ramp lives here.

In learn mode there is no clock - the trainer waits forever, so "slow it down"
is meaningless. Fluency is a different exercise, and it is the one worth
speeding up (SPEC 6 F4/F5).

Cues lead the beat rather than appearing on it, and go out at
`target + tolerance` whether or not they were played, so a missed note never
leaves a key lit into the next bar. Two targets wanting the same physical key
at once are refcounted, so the first release does not blind the second.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .keyboard import KEY_RANGE, MAX_SIMULTANEOUS_CUES, Keyboard
from .matcher import (DEFAULT_CHORD_SPREAD, DEFAULT_TOLERANCE, Matcher,
                      RampPolicy, Score, Target, targets_from_steps)
from .parser import Kind, Message
from .song import Song, Step

#: How far ahead of its onset a cue lights, at 100% speed.
LEAD = 0.50
#: Count-in before a run or a loop restart, in beats.
COUNT_IN_BEATS = 4


@dataclass
class AlongReport:
    hand: str
    speed: float = 1.0
    score: Score = field(default_factory=Score)
    verdict: str = "hold"          # clean | hold | poor
    next_speed: float = 1.0
    seconds: float = 0.0
    stopped_early: bool = False

    def summary(self) -> str:
        s = self.score
        return (f"{s.hits}/{s.targets} notes, {s.wrong_attacks} wrong, "
                f"±{s.mean_abs_timing * 1000:.0f} ms  ->  {self.verdict}")


class AlongSession:
    """One timed run through a section, at one speed."""

    def __init__(self, keyboard: Keyboard, song: Song, hand: str = "right",
                 speed: float = 0.6, steps: list[Step] | None = None,
                 accompany: bool = True, count_in: bool = True,
                 policy: RampPolicy | None = None) -> None:
        self.kb = keyboard
        self.song = song
        self.hand = hand
        self.speed = max(0.1, min(1.0, speed))
        self.accompany = accompany
        self.count_in = count_in
        self.policy = policy or RampPolicy()

        self.steps = [s for s in (steps if steps is not None
                                  else song.steps_for_hand(hand))
                      if self._required_of(s)]
        self.targets = self._targets()
        self.matcher = Matcher(self.targets, tolerance=DEFAULT_TOLERANCE,
                               chord_spread=DEFAULT_CHORD_SPREAD,
                               speed=self.speed)

        self._lock = threading.RLock()
        self._t0 = 0.0
        self._cue_refs: dict[int, int] = {}     # pitch -> outstanding cue count
        self._stop = threading.Event()
        self.report = AlongReport(hand=hand, speed=self.speed)

        self.on_tick = None        # callback(song_time, bar)
        self.on_verdict = None     # callback(pitch, ok)
        self.on_cue = None         # callback(pitches, on)

    # ------------------------------------------------------------- helpers

    def _required_of(self, step: Step) -> set[int]:
        wanted = {n.pitch for n in step.targets
                  if (self.hand in ("both", None) or n.hand == self.hand)}
        return {p for p in wanted if KEY_RANGE[0] <= p <= KEY_RANGE[1]}

    def _targets(self) -> list[Target]:
        out = targets_from_steps(self.steps,
                                 None if self.hand == "both" else self.hand)
        base = min((t.time for t in out), default=0.0)
        # rebase to zero and stretch to the practice speed
        return [Target(id=t.id, pitch=t.pitch, time=(t.time - base) / self.speed,
                       step=t.step, hand=t.hand)
                for t in out if KEY_RANGE[0] <= t.pitch <= KEY_RANGE[1]]

    @property
    def _lead(self) -> float:
        return LEAD / self.speed

    def now(self) -> float:
        return time.monotonic() - self._t0

    # --------------------------------------------------------------- cueing

    def _cue_on(self, pitch: int) -> None:
        with self._lock:
            n = self._cue_refs.get(pitch, 0)
            self._cue_refs[pitch] = n + 1
        if n == 0:
            self.kb.cue_on(pitch)
            if self.on_cue:
                self.on_cue([pitch], True)

    def _cue_off(self, pitch: int) -> None:
        with self._lock:
            n = self._cue_refs.get(pitch, 0)
            if n <= 0:
                return
            self._cue_refs[pitch] = n - 1
            last = (n - 1) == 0
        if last:
            self.kb.cue_off(pitch)
            if self.on_cue:
                self.on_cue([pitch], False)

    def _all_cues_off(self) -> None:
        with self._lock:
            pitches = [p for p, n in self._cue_refs.items() if n > 0]
            self._cue_refs.clear()
        for p in pitches:
            self.kb.cue_off(p)
        if pitches and self.on_cue:
            self.on_cue(pitches, False)

    # ---------------------------------------------------------------- input

    def handle(self, msg: Message) -> None:
        if msg.kind is not Kind.NOTE_ON or self._t0 == 0.0:
            return
        v = self.matcher.attack(msg.note, self.now())
        if self.on_verdict:
            self.on_verdict(msg.note, v.ok)

    # ------------------------------------------------------------------ run

    def run(self) -> AlongReport:
        if not self.targets:
            return self.report

        tol = self.matcher.tolerance
        pending_on = sorted(self.targets, key=lambda t: t.time)
        pending_off = sorted(self.targets, key=lambda t: t.time)
        accomp = self._accompaniment()
        releases: list[tuple[float, int]] = []
        i_on = i_off = i_acc = 0

        if self.count_in:
            self._do_count_in()

        self.kb.on_reset(self._all_cues_off)
        self._t0 = time.monotonic()
        end = max(t.time for t in self.targets) + tol + 0.6

        try:
            while not self._stop.is_set():
                t = self.now()
                if t > end:
                    break

                while i_on < len(pending_on) and pending_on[i_on].time - self._lead <= t:
                    self._cue_on(pending_on[i_on].pitch)
                    i_on += 1

                # extinguish at target+tolerance whether or not it was played,
                # so a missed note never leaves a key lit into the next bar
                while i_off < len(pending_off) and pending_off[i_off].time + tol <= t:
                    self._cue_off(pending_off[i_off].pitch)
                    i_off += 1

                while i_acc < len(accomp) and accomp[i_acc][0] <= t:
                    when, pitch, dur = accomp[i_acc]
                    self.kb.play(pitch, velocity=70)
                    releases.append((t + dur, pitch))
                    i_acc += 1

                due = [p for deadline, p in releases if deadline <= t]
                if due:
                    releases = [r for r in releases if r[0] > t]
                    for p in due:
                        self.kb.stop(p)

                if self.on_tick:
                    self.on_tick(t, self._bar_at(t))
                time.sleep(0.005)
        finally:
            self._all_cues_off()
            self.kb.stop_all()
            self.report.seconds = self.now()
            self.report.stopped_early = self._stop.is_set()

        score = self.matcher.score()
        self.report.score = score
        nxt, verdict = self.policy.next_speed(self.speed, score)
        self.report.verdict = verdict
        self.report.next_speed = nxt
        return self.report

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------- details

    def _accompaniment(self) -> list[tuple[float, int, float]]:
        if not self.accompany or self.hand in ("both", None):
            return []
        base = min((s.time for s in self.steps), default=0.0)
        out = []
        for step in self.steps:
            for n in step.targets:
                if n.hand != self.hand and n.playable:
                    out.append(((n.start_time - base) / self.speed, n.pitch,
                                max(0.15, min(n.duration / self.speed, 4.0))))
        return sorted(out)

    def _bar_at(self, t: float) -> int:
        song_time = t * self.speed
        best = self.steps[0].bar if self.steps else 1
        base = min((s.time for s in self.steps), default=0.0)
        for s in self.steps:
            if s.time - base <= song_time:
                best = s.bar
            else:
                break
        return best

    def _do_count_in(self) -> None:
        """Four beats, so the first chord is not structurally harder than the
        rest - especially on a loop restart (SPEC 6 F3)."""
        bpm = self.song.tempo_map.bpm_at(0) * self.speed
        beat = 60.0 / max(20.0, bpm)
        for i in range(COUNT_IN_BEATS):
            if self._stop.is_set():
                return
            if self.on_tick:
                self.on_tick(-(COUNT_IN_BEATS - i) * beat, 0)
            self.kb.play(84 if i else 96, velocity=45)
            time.sleep(min(beat, 1.5) * 0.25)
            self.kb.stop(84 if i else 96)
            time.sleep(max(0.0, min(beat, 1.5) * 0.75))
