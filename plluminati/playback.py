"""Play a MIDI file through the keyboard - listening, not practising.

Useful before learning a piece: hear it, watch the keys light, get the tune in
your head. No waiting, no scoring.

Two routes, and the choice matters on this hardware (SPEC 3.6):

  channel 1, audible velocity  -> SOUNDS *and* LIGHTS. The demo you want.
  channel 2, any velocity      -> SOUNDS only, lights nothing.
"""

from __future__ import annotations

import threading
import time

from .keyboard import ACCOMP_CHANNEL, CUE_CHANNEL, KEY_RANGE, Keyboard, _status
from .port import Priority
from .song import Song

NOTE_ON = 0x90
NOTE_OFF = 0x80


class Playback:
    """Straight playback of a song, in time."""

    def __init__(self, keyboard: Keyboard, song: Song, speed: float = 1.0,
                 hand: str | None = None, velocity: int = 90,
                 light: bool = True) -> None:
        self.kb = keyboard
        self.song = song
        self.speed = max(0.1, min(2.0, speed))
        self.hand = hand
        self.velocity = max(1, min(127, velocity))
        # Lighting and sounding are the same message here: channel 1 at an
        # audible velocity does both. Channel 2 sounds without lighting.
        self.channel = CUE_CHANNEL if light else ACCOMP_CHANNEL
        self._stop = threading.Event()
        self.on_note = None          # callback(pitch, on)

    def notes(self):
        out = [n for n in self.song.notes
               if KEY_RANGE[0] <= n.pitch <= KEY_RANGE[1]
               and (self.hand in (None, "both") or n.hand == self.hand)]
        return sorted(out, key=lambda n: (n.start_time, n.pitch))

    def run(self) -> int:
        notes = self.notes()
        if not notes:
            return 0

        base = notes[0].start_time
        events: list[tuple[float, bool, int]] = []
        for n in notes:
            events.append(((n.start_time - base) / self.speed, True, n.pitch))
            end = max(n.end_time, n.start_time + 0.05)
            events.append(((end - base) / self.speed, False, n.pitch))
        events.sort(key=lambda e: (e[0], e[1]))   # note-offs before note-ons

        sounding: dict[int, int] = {}
        t0 = time.monotonic()
        played = 0
        try:
            for when, on, pitch in events:
                while not self._stop.is_set():
                    remaining = when - (time.monotonic() - t0)
                    if remaining <= 0:
                        break
                    time.sleep(min(remaining, 0.005))
                if self._stop.is_set():
                    break
                if on:
                    self.kb.port.send(
                        bytes([_status(NOTE_ON, self.channel), pitch, self.velocity]),
                        Priority.MUSIC)
                    sounding[pitch] = sounding.get(pitch, 0) + 1
                    played += 1
                else:
                    sounding[pitch] = max(0, sounding.get(pitch, 0) - 1)
                    if sounding[pitch] == 0:
                        self.kb.port.send(
                            bytes([_status(NOTE_OFF, self.channel), pitch, 0]),
                            Priority.CRITICAL)
                if self.on_note:
                    self.on_note(pitch, on)
        finally:
            for pitch, n in sounding.items():
                if n > 0:
                    self.kb.port.send(
                        bytes([_status(NOTE_OFF, self.channel), pitch, 0]),
                        Priority.CRITICAL)
            self.kb.port.flush(1.0)
        return played

    def stop(self) -> None:
        self._stop.set()
