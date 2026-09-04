"""The EZ-150 itself: two buses, ownership, and reset recovery.

Everything here rests on facts measured on the hardware (SPEC 3.2, 3.4, 3.6):

    cue bus            channel 1, velocity 1   lights a key, SILENTLY
    accompaniment bus  channel 2, any velocity sounds, lights NOTHING

Those two are orthogonal, which is what makes hand-separated practice possible:
light the hand being learned while playing the other one aloud.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .parser import Kind, Message
from .port import MidiPort, Priority

# --- the buses ---------------------------------------------------------------

CUE_CHANNEL = 1          # lights respond to channel 1 ONLY
CUE_VELOCITY = 1         # velocity 1 lights a key with no sound
ACCOMP_CHANNEL = 2       # sounds without lighting anything
ACCOMP_PROGRAM = 0       # Grand Piano; the keyboard powers up on 88, a slow pad

#: The EZ-150 recognises these channels and ignores 8, 9 and 11-16 entirely.
USABLE_CHANNELS = (1, 2, 3, 4, 5, 6, 7, 10)

#: Playable/lightable key range, MIDI note numbers. 61 keys, C2..C7.
#: Measured 2026-08-14: a partial keyboard sweep confirmed the low end at 36
#: and reached 72; lighting one candidate per octave from 24 to 108 lit SIX of
#: eight, which brackets the range at 36-96 (24 and 108 fall off the ends).
#: Treated as the default rather than gospel - `plluminati selftest` overrides
#: it with measured values, and notes outside it are handled defensively (never
#: silently octave-folded, which would teach wrong hand movement).
KEY_RANGE = (36, 96)

#: 16 voices shared across cues, accompaniment and the player's own hands.
#: Exceeding it means "some may not sound and/or others may be cancelled" -
#: including, possibly, our cues (SPEC 3.7).
POLYPHONY_LIMIT = 16
MAX_SIMULTANEOUS_CUES = 6


def _status(kind: int, channel: int) -> int:
    return kind | (channel - 1)


NOTE_ON = 0x90
NOTE_OFF = 0x80
PROGRAM_CHANGE = 0xC0
CONTROL_CHANGE = 0xB0


@dataclass
class ResetDetector:
    """Spots the unprompted setup dump.

    The keyboard emits a burst of bank-select / program-change / CC7 across
    channels 1-7 and 10 on power-on AND when leaving the attract display. That
    burst RESETS channel 2 to program 88, silently undoing our accompaniment
    voice - so it must be treated as a "keyboard was reset" event and answered
    by re-applying session setup (SPEC 3.4).
    """
    window: float = 1.0
    min_channels: int = 3
    _programs: dict[int, float] = field(default_factory=dict)

    def observe(self, msg: Message) -> bool:
        """True when this message completes a reset signature."""
        if msg.kind is not Kind.PROGRAM_CHANGE:
            return False
        now = time.monotonic()
        self._programs = {c: t for c, t in self._programs.items()
                          if now - t < self.window}
        self._programs[msg.channel] = now
        if len(self._programs) >= self.min_channels:
            self._programs.clear()
            return True
        return False


class Keyboard:
    """A live EZ-150.

    Owns two sets of outstanding notes so every note-on it emits gets an
    explicit matching note-off. That pairing is the ONLY cleanup guarantee
    available: All Notes Off (CC123), All Sound Off, Reset All Controllers and
    Local On/Off are all marked "not recognised" in the EZ-150's MIDI
    Implementation Chart (SPEC 7.6).
    """

    def __init__(self, port: MidiPort, on_message=None) -> None:
        self.port = port
        self._external_handler = on_message

        self._lock = threading.Lock()
        self.lit: set[int] = set()          # cue notes we asserted
        self.sounding: set[int] = set()     # accompaniment notes we started
        self.held: set[int] = set()         # keys the PLAYER is holding
        #: cues we wanted to extinguish while the player held the key; their
        #: release does it for us, so these need no further action
        self.deferred_offs: set[int] = set()

        self.player_velocity: int | None = None   # calibrated on first keypress
        self.resets_seen = 0
        self._reset = ResetDetector()
        self._on_reset = None
        self._last_sensing: float = 0.0
        self._local_muted = False

    # ------------------------------------------------------------------- setup

    def set_local_mute(self, on: bool) -> None:
        """Silence (or restore) the player's own keys.

        This is how the keyboard's own Lesson mode feels: wrong notes make no
        sound. We cannot disable Local Control (unrecognised on this model), so
        instead channel 1's volume goes to zero - which silences local playing
        while LIGHTS STILL WORK, the very first thing measured on this hardware
        (SPEC 3.2). Correct notes are then echoed back on channel 2 so they are
        the only thing audible.

        Restoring to 127 is not a guess: the keyboard's own power-on and
        wake-from-attract dumps both send `B0 07 7F`, so 127 is its default.
        """
        self._local_muted = on
        self.port.send(bytes([_status(CONTROL_CHANGE, CUE_CHANNEL), 7,
                              0 if on else 127]), Priority.CRITICAL)

    #: GM percussion. Channel 10 is in the EZ-150's usable set, and its
    #: power-on dump configures it as a drum kit (bank MSB 127), so hits land
    #: with no setup at all.
    DRUM_CHANNEL = 10

    #: Short noises for a wrong key, played THROUGH THE KEYBOARD alongside the
    #: player's own piano - nothing is muted, the noise simply sits on top.
    WRONG_HITS = {
        "hihat": 42,    # closed hi-hat - short, muted "tss"
        "stick": 37,    # side stick - dry click
        "clap": 39,
        "thud": 41,     # low tom
        "kick": 36,
        "scratch": 30,  # scratch pull, if the kit maps it
        "cowbell": 56,
        "crash": 49,
    }

    def hit_wrong(self, sound: str = "hihat", velocity: int = 100) -> None:
        """A short percussion hit for a wrong key.

        Fire and forget: note-off goes straight out and the sample rings on by
        itself, so nothing hangs and no bookkeeping is needed.
        """
        note = self.WRONG_HITS.get(sound, self.WRONG_HITS["hihat"])
        self.port.send(
            bytes([_status(NOTE_ON, self.DRUM_CHANNEL), note, velocity])
            + bytes([_status(NOTE_OFF, self.DRUM_CHANNEL), note, 0]),
            Priority.MUSIC)

    def echo(self, note: int, velocity: int = 90) -> None:
        """Sound a note on the accompaniment bus - used to voice correct
        notes back while local playing is muted."""
        self.play(note, velocity=velocity)

    def apply_session_setup(self) -> None:
        """(Re-)assert everything Plluminati needs.

        Safe to call at any time, and MUST be called again after a reset.
        Program changes do not disturb the player's own panel settings, per
        the chart's note *3 - confirmed safe.
        """
        self.port.send(bytes([_status(PROGRAM_CHANGE, ACCOMP_CHANNEL), ACCOMP_PROGRAM]),
                       Priority.CRITICAL)
        # A reset restores channel 1 to full volume, which would silently undo
        # the lesson-style mute mid-session.
        if self._local_muted:
            self.port.send(bytes([_status(CONTROL_CHANGE, CUE_CHANNEL), 7, 0]),
                           Priority.CRITICAL)

    def on_reset(self, callback) -> None:
        self._on_reset = callback

    # -------------------------------------------------------------- cue lights

    def cue_on(self, *notes: int) -> None:
        """Light keys silently. Chords light polyphonically."""
        payload = bytearray()
        with self._lock:
            for n in notes:
                payload += bytes([_status(NOTE_ON, CUE_CHANNEL), n, CUE_VELOCITY])
                self.lit.add(n)
        if payload:
            self.port.send(bytes(payload), Priority.CUE)

    def cue_off(self, *notes: int) -> None:
        """Extinguish cues - but NEVER for a key the player is holding.

        Measured 2026-08-14: our note-off cuts the player's own sounding note,
        because their key and our cue are the same logical note on the same
        channel. Sending one while they hold the key silences them mid-phrase.

        Skipping those is safe and needs no follow-up: releasing the key
        extinguishes the light by itself (SPEC 3.2).
        """
        payload = bytearray()
        with self._lock:
            for n in notes:
                self.lit.discard(n)
                if n in self.held:
                    self.deferred_offs.add(n)
                    continue
                payload += bytes([_status(NOTE_OFF, CUE_CHANNEL), n, 0])
        if payload:
            self.port.send(bytes(payload), Priority.CRITICAL)

    def cues_clear(self) -> None:
        with self._lock:
            notes = sorted(self.lit)
        if notes:
            self.cue_off(*notes)

    # ----------------------------------------------------------- accompaniment

    def play(self, *notes: int, velocity: int = 80) -> None:
        """Sound notes WITHOUT lighting any key."""
        payload = bytearray()
        with self._lock:
            for n in notes:
                payload += bytes([_status(NOTE_ON, ACCOMP_CHANNEL), n, velocity])
                self.sounding.add(n)
        if payload:
            self.port.send(bytes(payload), Priority.MUSIC)

    def stop(self, *notes: int) -> None:
        payload = bytearray()
        with self._lock:
            for n in notes:
                payload += bytes([_status(NOTE_OFF, ACCOMP_CHANNEL), n, 0])
                self.sounding.discard(n)
        if payload:
            self.port.send(bytes(payload), Priority.CRITICAL)

    def stop_all(self) -> None:
        with self._lock:
            notes = sorted(self.sounding)
        if notes:
            self.stop(*notes)

    # ------------------------------------------------------------------- input

    def live(self, threshold: float = 1.5) -> bool:
        """Is the keyboard actually reachable right now?

        Active Sensing stops when the keyboard is switched off AND when it
        drops into its idle attract display - and while the attract display
        runs, cue lights are ignored. So keepalive silence is the one reliable
        signal that output will not land (SPEC 3.4).

        It does NOT distinguish 'asleep' from 'switched off', but the remedy
        for both is the same thing the player can do: touch a key.
        """
        return (time.monotonic() - self._last_sensing) < threshold

    @property
    def is_live(self) -> bool:
        return self.live()

    def wait_until_live(self, timeout: float = 60.0, announce=None) -> bool:
        """Block until the keyboard is responsive, prompting once if not."""
        if self.is_live:
            return True
        if announce:
            announce()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.is_live:
                return True
            time.sleep(0.05)
        return False

    def handle(self, msg: Message) -> None:
        """Feed every inbound message through here."""
        if msg.is_active_sensing:
            self._last_sensing = time.monotonic()
            if self._external_handler:
                self._external_handler(msg)   # collectors still want to count it
            return

        if msg.kind is Kind.NOTE_ON:
            with self._lock:
                self.held.add(msg.note)
                # The keybed's velocity is fixed, but the fixed value is
                # per-VOICE, not always 84 - so calibrate, never hardcode.
                if msg.velocity != CUE_VELOCITY:
                    self.player_velocity = msg.velocity
        elif msg.kind is Kind.NOTE_OFF:
            with self._lock:
                self.held.discard(msg.note)
                # A key release extinguishes our cue for that note without us
                # sending anything, so our record of it is now stale - and it
                # also settles any note-off we deliberately withheld.
                self.lit.discard(msg.note)
                self.deferred_offs.discard(msg.note)

        if self._reset.observe(msg):
            self.resets_seen += 1
            self.apply_session_setup()
            if self._on_reset:
                self._on_reset()

        if self._external_handler:
            self._external_handler(msg)

    # ----------------------------------------------------------------- cleanup

    def panic(self) -> None:
        """Release everything we own. Explicit note-offs only - CC123 is a no-op
        on this keyboard and would not reach channel 2 even if it worked."""
        self.cues_clear()
        self.stop_all()
        if self._local_muted:
            self.set_local_mute(False)     # never leave the player silenced
        self.port.flush(1.0)
