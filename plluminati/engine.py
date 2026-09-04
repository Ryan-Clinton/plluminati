"""Learn mode: light the next notes silently, wait, advance.

The core idea, and the thing the keyboard could never do for its own MIDI
files. No clock runs here - tempo is meaningless when the trainer waits
forever, so it lives in play-along mode instead (SPEC 6 F4).

Two subtleties that took hardware measurement to get right:

**Satisfaction needs a fresh attack.** `required ⊆ held` is not enough: if step
N is C and step N+1 is also C, a player still holding C would satisfy N+1
without touching the key, and a repeated chord would collapse to one press.

**Cues must be re-asserted.** Releasing a key extinguishes its light without
Plluminati sending anything, so a player who presses C, releases it and reaches
for E would lose C's cue while C is still required. Our record of what is lit is
an intent log, not hardware state (SPEC 3.2, 7.4).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .keyboard import KEY_RANGE, MAX_SIMULTANEOUS_CUES, Keyboard
from .parser import Kind, Message
from .song import Song, Step

#: Backstop only - repair is event-driven off note-off. This catches missed
#: events and post-reset recovery without flooding the 31250 baud link.
REASSERT_INTERVAL = 0.10


@dataclass
class StepReport:
    index: int
    bar: int
    pitches: list[int]
    attempts: int = 0            # wrong attacks before getting it
    seconds: float = 0.0
    skipped: bool = False


@dataclass
class LearnReport:
    hand: str
    steps_total: int = 0
    steps_completed: int = 0
    steps_skipped: int = 0
    wrong_attacks: int = 0
    seconds: float = 0.0
    per_step: list[StepReport] = field(default_factory=list)

    @property
    def clean_steps(self) -> int:
        return sum(1 for s in self.per_step if s.attempts == 0 and not s.skipped)

    @property
    def accuracy(self) -> float:
        done = self.steps_completed or 1
        return self.clean_steps / done

    def summary(self) -> str:
        return (f"{self.steps_completed}/{self.steps_total} steps, "
                f"{self.clean_steps} first-time clean, "
                f"{self.wrong_attacks} wrong notes, {self.seconds:.0f}s")


class LearnSession:
    """Drive one hand (or both) through a song in wait mode."""

    def __init__(self, keyboard: Keyboard, song: Song, hand: str = "right",
                 strict: bool = False, accompany: bool = True,
                 steps: list[Step] | None = None, mute_wrong: bool = False,
                 synth=None, wrong_sound: str = "", dj_mode: bool = False) -> None:
        self.kb = keyboard
        self.song = song
        self.hand = hand
        self.strict = strict
        self.accompany = accompany
        #: Lesson-style gating: DOES NOT WORK on the EZ-150 and defaults off.
        #: The plan was to silence local playing with CC7=0 on channel 1 and
        #: echo correct notes back on channel 2. Measured 2026-08-14: CC7 only
        #: affects notes the keyboard RECEIVES - the player's own keys have a
        #: separate local path and stay at full volume. The result is the
        #: opposite of the intent: nothing is muted and every correct note is
        #: heard twice, once live and once echoed a few ms later.
        #: Left in place, off, because it is the right design if a mute is
        #: ever found - Local Control (CC122) is unrecognised on this model,
        #: so there is currently no way to silence the keybed over MIDI.
        self.mute_wrong = mute_wrong
        self._echoed: set[int] = set()
        #: When a laptop synth is supplied, correct notes are voiced HERE and
        #: wrong notes are voiced nowhere. Turn the keyboard's volume dial down
        #: and that is the built-in Lesson mode's behaviour, which the EZ-150
        #: will not give us over MIDI (SPEC 3.2).
        self.synth = synth
        #: A short drum hit layered over the player's own piano. Additive, not
        #: a replacement - the keybed cannot be silenced (SPEC 3.2).
        self.wrong_sound = wrong_sound

        #: DJ mode - the way to get a wrong note to sound like something else
        #: ENTIRELY rather than piano-plus-a-noise.
        #:
        #: The trick is to stop the keyboard playing piano at all. Select DJ
        #: Voice #98/#99 (or Drum Kit #00) on the panel and every key plays a
        #: different sample - "Uhh", "One More Time", "Pinpon", a hand clap.
        #: Plluminati then plays the PIANO itself, on channel 2, and only for
        #: correct notes.
        #:
        #:     wrong key  -> its DJ sample, no piano
        #:     right key  -> its DJ sample AND the piano note from us
        #:
        #: The voice has to be chosen on the panel: program changes received
        #: over MIDI do not affect what the keybed plays (chart note *3).
        self.dj_mode = dj_mode

        chosen = steps if steps is not None else song.steps_for_hand(hand)
        self.steps = [s for s in chosen if self._required_of(s)]

        self._lock = threading.RLock()
        self._index = -1
        self._required: set[int] = set()
        self._fresh: set[int] = set()
        self._asserted: set[int] = set()
        self._step_started = 0.0
        self._step_wrong = 0
        self._releases: list[tuple[float, int]] = []
        self._done = threading.Event()
        self._advanced = threading.Event()

        self.report = LearnReport(hand=hand, steps_total=len(self.steps))
        self.on_step = None          # callback(step, index) for a UI
        self.on_verdict = None       # callback(pitch, ok)
        self.on_hint = None          # callback(kind, pitches)

        #: step activation -> cue-on latency, for diagnosing "why is there a
        #: gap?" without asking anyone to run a test
        self.cue_latency: list[float] = []

    # ------------------------------------------------------------- helpers

    def _required_of(self, step: Step) -> set[int]:
        """Pitches the player must press for this step, within reach."""
        wanted = {n.pitch for n in step.targets
                  if (self.hand in ("both", None) or n.hand == self.hand)}
        return {p for p in wanted if KEY_RANGE[0] <= p <= KEY_RANGE[1]}

    def _accompaniment_of(self, step: Step) -> list:
        if not self.accompany or self.hand in ("both", None):
            return []
        return [n for n in step.targets
                if n.hand != self.hand and n.playable]

    # -------------------------------------------------------------- cueing

    def _sync_cues(self) -> None:
        """Invariant: every required pitch that is NOT held has a live cue.

        Held keys are already lit (by our own cue), and the hardware puts them
        out on release - which is exactly when this puts them back.
        """
        with self._lock:
            if self._index < 0 or self._done.is_set():
                return
            want = {p for p in self._required if p not in self.kb.held}
            missing = sorted(want - self._asserted)[:MAX_SIMULTANEOUS_CUES]
            if missing:
                self._asserted.update(missing)
        if missing:
            self.kb.cue_on(*missing)
            if self._step_started:
                self.cue_latency.append(time.monotonic() - self._step_started)

    def _resync(self) -> None:
        """Forget what we think is lit, and light it again.

        Needed whenever the keyboard was not listening: cues sent while it is
        asleep (attract display) or mid-reset are silently dropped, but
        `_asserted` still claims they landed - so the backstop skips them and
        the key stays dark forever. Anything that invalidates hardware state
        must invalidate our record of it too.
        """
        with self._lock:
            self._asserted.clear()
        self._sync_cues()

    def _clear_cues(self) -> None:
        with self._lock:
            lit = sorted(self._asserted)
            self._asserted.clear()
        if lit:
            self.kb.cue_off(*lit)

    # ------------------------------------------------------------ stepping

    def _activate(self, index: int) -> None:
        with self._lock:
            self._index = index
            step = self.steps[index]
            self._required = self._required_of(step)
            self._fresh.clear()          # a held key does NOT count; re-attack
            self._asserted.clear()
            self._step_started = time.monotonic()
            self._step_wrong = 0
        self._sync_cues()

        for note in self._accompaniment_of(step):
            self.kb.play(note.pitch, velocity=70)
            # Release on the notated duration in WALL time, never on the
            # player's progress - otherwise a pause becomes a ten-second drone.
            with self._lock:
                self._releases.append(
                    (time.monotonic() + max(0.15, min(note.duration, 4.0)),
                     note.pitch))

        if self.on_step:
            self.on_step(step, index)
        if self.on_hint:
            still_held = sorted(p for p in self._required if p in self.kb.held)
            # A held key cannot be lit again - the light is already on and
            # re-triggering it would cut the player's own note (SPEC 3.2).
            self.on_hint("release" if still_held else "", still_held)

    def _complete_step(self, skipped: bool = False) -> None:
        with self._lock:
            step = self.steps[self._index]
            rep = StepReport(index=step.index, bar=step.bar,
                             pitches=sorted(self._required),
                             attempts=self._step_wrong,
                             seconds=time.monotonic() - self._step_started,
                             skipped=skipped)
            self.report.per_step.append(rep)
            self.report.steps_completed += 1
            if skipped:
                self.report.steps_skipped += 1
            nxt = self._index + 1
        self._clear_cues()
        if nxt >= len(self.steps):
            self._done.set()
        else:
            self._activate(nxt)
        self._advanced.set()

    # --------------------------------------------------------------- input

    def handle(self, msg: Message) -> None:
        """Hook this to the Keyboard's message stream."""
        if self._done.is_set() or self._index < 0:
            return

        if msg.kind is Kind.NOTE_ON:
            with self._lock:
                ok = msg.note in self._required
                if ok:
                    self._fresh.add(msg.note)
                    # NB do NOT drop this from _asserted: it is our record of
                    # what to switch off later. Forgetting it here leaves the
                    # cue note-on outstanding and the key lit into the next
                    # step. Whether it needs re-lighting is decided by the
                    # `held` check in _sync_cues, not by this set.
                else:
                    self._step_wrong += 1
                    self.report.wrong_attacks += 1
                satisfied = (self._required <= self._fresh
                             and self._required <= self.kb.held | {msg.note})

            if ok and self.dj_mode:
                # the keybed is making noises, not piano - so we supply the
                # piano, and only for the right note
                self.kb.echo(msg.note, velocity=95)
                self._echoed.add(msg.note)
            elif not ok and self.wrong_sound and not self.dj_mode:
                self.kb.hit_wrong(self.wrong_sound)

            if self.on_verdict:
                self.on_verdict(msg.note, ok)
            if satisfied:
                self._complete_step()

        elif msg.kind is Kind.NOTE_OFF:
            if msg.note in self._echoed:
                self._echoed.discard(msg.note)
                if self.synth is not None and self.synth.available:
                    self.synth.note_off(msg.note)
                else:
                    self.kb.stop(msg.note)
            # The hardware just put this key's light out. If it is still
            # required, light it again immediately.
            with self._lock:
                self._asserted.discard(msg.note)
                needed = msg.note in self._required
            if needed:
                self._sync_cues()

    # ----------------------------------------------------------------- run

    def run(self, timeout: float | None = None, tick: float = 0.02) -> LearnReport:
        started = time.monotonic()
        if not self.steps:
            self.report.seconds = 0.0
            return self.report

        # A reset burst (power-on, or waking from the attract display) wipes
        # channel setup and drops anything in flight.
        self.kb.on_reset(self._resync)
        if self.mute_wrong:
            self.kb.set_local_mute(True)

        was_live = self.kb.is_live
        self._activate(0)
        last_reassert = 0.0
        try:
            while not self._done.is_set():
                live = self.kb.is_live
                if live and not was_live:
                    self._resync()          # just woke up - relight everything
                was_live = live

                if timeout and time.monotonic() - started > timeout:
                    break
                now = time.monotonic()

                due = []
                with self._lock:
                    keep = []
                    for deadline, pitch in self._releases:
                        (due if deadline <= now else keep).append((deadline, pitch))
                    self._releases = keep
                for _, pitch in due:
                    self.kb.stop(pitch)

                if now - last_reassert >= REASSERT_INTERVAL:
                    last_reassert = now
                    self._sync_cues()

                time.sleep(tick)
        finally:
            self._clear_cues()
            self.kb.stop_all()
            if self.mute_wrong:
                self.kb.set_local_mute(False)     # never leave them silenced
            self.report.seconds = time.monotonic() - started
        return self.report

    # ------------------------------------------------------------ controls

    def skip(self) -> None:
        """Give up on the current step and move on."""
        if not self._done.is_set() and self._index >= 0:
            self._complete_step(skipped=True)

    @property
    def current(self) -> Step | None:
        with self._lock:
            if 0 <= self._index < len(self.steps):
                return self.steps[self._index]
        return None

    @property
    def progress(self) -> tuple[int, int]:
        return (self.report.steps_completed, len(self.steps))
