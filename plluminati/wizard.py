"""The on-screen hardware wizard.

The remaining hardware questions, asked on the screen behind the keyboard and
answered ON the keyboard - no terminal, no reading, no typing. Deliberately
short: only the checks whose answers actually change behaviour are asked.

Answering: a key BELOW middle C means yes, ABOVE means no. The player's hands
never leave the instrument.
"""

from __future__ import annotations

import threading
import time

from .keyboard import (ACCOMP_CHANNEL, CUE_CHANNEL, CUE_VELOCITY, KEY_RANGE,
                       POLYPHONY_LIMIT, Keyboard, _status)
from .parser import Kind, Message
from .port import Priority

MIDDLE_C = 60
NOTE_OFF = 0x80


class Wizard:
    """A short scripted sequence of hardware checks."""

    def __init__(self, kb: Keyboard, hub) -> None:
        self.kb = kb
        self.hub = hub
        self.results: dict = {}
        self._step = 0
        self._awaiting = None          # "answer" | "note" | None
        self._held_note: int | None = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------ plumbing

    def _say(self, title: str, detail: str = "", *, answer=False,
             progress=None, cue=None) -> None:
        self.hub.publish("wizard", title=title, detail=detail,
                         answer=answer, progress=progress or [self._step, 4],
                         cue=cue or [])

    def _ask(self, title: str, detail: str = "", cue=None) -> None:
        self._awaiting = "answer"
        self._say(title, detail, answer=True, cue=cue)

    def _wait_note(self, title: str, detail: str = "") -> None:
        self._awaiting = "note"
        self._say(title, detail)

    def _later(self, seconds: float, fn) -> None:
        threading.Timer(seconds, fn).start()

    # --------------------------------------------------------------- steps

    def start(self) -> None:
        self.results.clear()
        self._step = 0
        self.kb.cues_clear()
        self.kb.stop_all()
        if not self.kb.is_live:
            self._wait_note("Wake the keyboard",
                            "It has gone to sleep. Press any key on it.")
            self._pending_after_wake = self._step_range
            return
        self._step_range()

    # 1. lightable range -----------------------------------------------------

    def _step_range(self) -> None:
        self._step = 1
        lo, hi = KEY_RANGE
        self.kb.cue_on(lo, hi)
        self.kb.port.flush()
        self._ask("Are BOTH end keys lit?",
                  f"The lowest and highest keys ({lo} and {hi}). "
                  f"Below middle C = yes, above = no.",
                  cue=[lo, hi])

    def _done_range(self, value) -> None:
        lo, hi = KEY_RANGE
        self.kb.cue_off(lo, hi)
        self.results["lightable_range"] = {
            "value": value, "low": lo, "high": hi,
            "detail": ("full range lightable" if value else
                       "extremes did not light" if value is False else "skipped")}
        self._later(0.4, self._step_held)

    # 2. does our note-off cut a held note? ----------------------------------

    def _step_held(self) -> None:
        self._step = 2
        self._held_note = None
        self._wait_note("Press and HOLD middle C",
                        "Keep holding it until the next message.")

    def _held_pressed(self, note: int) -> None:
        self._held_note = note
        self._say("Keep holding...", "Testing whether our light housekeeping "
                                     "interferes with your note.")
        self._later(1.2, self._held_send_off)

    def _held_send_off(self) -> None:
        note = self._held_note or MIDDLE_C
        self.kb.port.send(bytes([_status(NOTE_OFF, CUE_CHANNEL), note, 0]),
                          Priority.CRITICAL)
        self.kb.port.flush()
        self._ask("Did YOUR note stop sounding?",
                  "You can let go now. Below middle C = yes it stopped, "
                  "above = no it kept sounding.")

    def _done_held(self, value) -> None:
        self.results["note_off_cuts_held"] = {
            "value": value, "note": self._held_note,
            "detail": ("our note-off cuts the player's held note - never send "
                       "note-off for a held pitch" if value else
                       "safe: our note-off leaves the player's note alone"
                       if value is False else "skipped")}
        self._later(0.4, self._step_polyphony)

    # 3. do cues steal polyphony voices? -------------------------------------

    def _step_polyphony(self) -> None:
        """Does adding cues steal a voice from something already sounding?

        Asked about ONE held note rather than a 16-note cluster. "Did any of
        these sixteen notes vanish?" is not a fair question for anybody's ears;
        "is that one note still going?" is.
        """
        self._step = 3
        self._anchor = 40                        # low E - long, obvious decay
        # fill the rest of the 16-voice budget so the next cue must displace
        # something if cues consume voices at all
        self._cluster = [60 + i for i in range(POLYPHONY_LIMIT - 1)]
        self.kb.play(self._anchor, velocity=100)
        self.kb.port.flush()
        self._say("Listen to that low note",
                  "Keep listening - it should keep sounding.")
        self._later(1.4, self._poly_fill)

    def _poly_fill(self) -> None:
        self.kb.play(*self._cluster, velocity=1)
        self._cues = [92, 93, 94, 95]
        self.kb.cue_on(*self._cues)
        self.kb.port.flush()
        self._ask("Is that low note STILL sounding?",
                  "The keyboard's 16 voices are now full and cue lights were "
                  "added on top. Below middle C = yes still going, "
                  "above = no it stopped.",
                  cue=self._cues)

    def _done_polyphony(self, value) -> None:
        self.kb.cue_off(*self._cues)
        self.kb.stop_all()
        # NB the question is inverted: "still sounding" = cues did NOT steal
        stole = None if value is None else (not value)
        self.results["cues_steal_voices"] = {
            "value": stole,
            "detail": ("cues consume voices - cap simultaneous cues and "
                       "prioritise musical output" if stole else
                       "cues do not steal voices from sounding notes"
                       if stole is False else "skipped")}
        self._later(0.4, self._finish)

    # ----------------------------------------------------------------- end

    def _finish(self) -> None:
        self._step = 4
        self._awaiting = None
        self.kb.cues_clear()
        self.kb.stop_all()
        path = self._write_profile()
        self.hub.publish("wizard_done", results=self.results, profile=path)
        self.hub.wizard = None

    def _write_profile(self) -> str:
        import json
        import os
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
        path = os.path.join(base, "plluminati", "hardware-profile.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        existing = {}
        if os.path.exists(path):
            try:
                with open(path) as fh:
                    existing = json.load(fh)
            except (OSError, json.JSONDecodeError):
                existing = {}
        existing.setdefault("probes", {})
        existing["wizard"] = {
            "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "results": self.results,
        }
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(existing, fh, indent=2)
        os.replace(tmp, path)      # atomic (SPEC 7.7)
        return path

    # --------------------------------------------------------------- input

    def answer(self, value) -> None:
        """From an on-screen button, or from a key press."""
        with self._lock:
            if self._awaiting != "answer":
                return
            self._awaiting = None
            step = self._step
        {1: self._done_range, 2: self._done_held,
         3: self._done_polyphony}.get(step, lambda v: None)(value)

    def on_midi(self, msg: Message) -> None:
        if msg.kind is not Kind.NOTE_ON or msg.velocity == CUE_VELOCITY:
            return
        with self._lock:
            waiting = self._awaiting
        if waiting == "note":
            with self._lock:
                self._awaiting = None
            if self._step == 0:
                nxt = getattr(self, "_pending_after_wake", self._step_range)
                self._later(0.3, nxt)
            elif self._step == 2:
                self._held_pressed(msg.note)
        elif waiting == "answer":
            if msg.note == MIDDLE_C:
                self.answer(None)
            else:
                self.answer(msg.note < MIDDLE_C)
