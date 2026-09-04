"""Streaming MIDI byte-stream parser.

Pure logic, no I/O, so it can be unit-tested exhaustively.

Handles the things this keyboard actually does (SPEC 3.3, 7.1):

  * running status
  * realtime bytes interleaved anywhere, including mid-message - the EZ-150
    emits Active Sensing (0xFE) roughly every 37 ms
  * note-on with velocity 0 as the note-off idiom, which is how the EZ-150
    reports key release
  * the unprompted setup dump on power-on and when leaving the attract display
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

NOTE_OFF = 0x80
NOTE_ON = 0x90
POLY_AFTERTOUCH = 0xA0
CONTROL_CHANGE = 0xB0
PROGRAM_CHANGE = 0xC0
CHANNEL_PRESSURE = 0xD0
PITCH_BEND = 0xE0

ACTIVE_SENSING = 0xFE
TIMING_CLOCK = 0xF8
SYSEX_START = 0xF0
SYSEX_END = 0xF7

#: data bytes expected per channel-message status nibble
_DATA_LEN = {
    NOTE_OFF: 2, NOTE_ON: 2, POLY_AFTERTOUCH: 2,
    CONTROL_CHANGE: 2, PROGRAM_CHANGE: 1,
    CHANNEL_PRESSURE: 1, PITCH_BEND: 2,
}


class Kind(str, Enum):
    NOTE_ON = "note_on"
    NOTE_OFF = "note_off"
    CONTROL_CHANGE = "control_change"
    PROGRAM_CHANGE = "program_change"
    PITCH_BEND = "pitch_bend"
    AFTERTOUCH = "aftertouch"
    REALTIME = "realtime"
    SYSEX = "sysex"
    OTHER = "other"


@dataclass(frozen=True)
class Message:
    kind: Kind
    channel: int = 0          # 1-based, matching SPEC's human numbering
    data1: int = 0            # note / controller / program
    data2: int = 0            # velocity / value
    status: int = 0           # raw status byte, for realtime and diagnostics
    raw: bytes = b""

    # convenience aliases - reads better at call sites
    @property
    def note(self) -> int:
        return self.data1

    @property
    def velocity(self) -> int:
        return self.data2

    @property
    def controller(self) -> int:
        return self.data1

    @property
    def value(self) -> int:
        return self.data2

    @property
    def is_active_sensing(self) -> bool:
        return self.kind is Kind.REALTIME and self.status == ACTIVE_SENSING

    def __str__(self) -> str:
        if self.kind is Kind.NOTE_ON:
            return f"note_on  ch{self.channel} note={self.note} vel={self.velocity}"
        if self.kind is Kind.NOTE_OFF:
            return f"note_off ch{self.channel} note={self.note}"
        if self.kind is Kind.CONTROL_CHANGE:
            return f"cc       ch{self.channel} cc={self.controller} val={self.value}"
        if self.kind is Kind.PROGRAM_CHANGE:
            return f"program  ch{self.channel} prog={self.data1}"
        if self.kind is Kind.REALTIME:
            name = {ACTIVE_SENSING: "active_sensing", TIMING_CLOCK: "clock"}.get(
                self.status, f"realtime 0x{self.status:02X}")
            return name
        if self.kind is Kind.SYSEX:
            return "sysex " + " ".join(f"{b:02X}" for b in self.raw)
        return f"{self.kind.value} 0x{self.status:02X}"


class StreamParser:
    """Feed it bytes, get back complete messages."""

    def __init__(self) -> None:
        self._running: int | None = None   # running status byte
        self._pending: list[int] = []      # data bytes collected so far
        self._sysex: list[int] | None = None

    def feed(self, data: bytes | bytearray) -> list[Message]:
        out: list[Message] = []
        for byte in data:
            msg = self._byte(byte)
            if msg is not None:
                out.append(msg)
        return out

    # ---------------------------------------------------------------- internals

    def _byte(self, b: int) -> Message | None:
        # Realtime bytes may appear ANYWHERE, including between the data bytes
        # of another message. They never disturb running status.
        if b >= 0xF8:
            return Message(kind=Kind.REALTIME, status=b, raw=bytes([b]))

        if self._sysex is not None:
            if b == SYSEX_END:
                payload = bytes(self._sysex + [b])
                self._sysex = None
                return Message(kind=Kind.SYSEX, status=SYSEX_START, raw=payload)
            if b & 0x80:
                # a status byte aborts an unterminated sysex
                self._sysex = None
                return self._byte(b)
            self._sysex.append(b)
            return None

        if b & 0x80:                      # status byte
            if b == SYSEX_START:
                self._sysex = [b]
                self._running = None
                return None
            if b > SYSEX_START:           # system common - no running status
                self._running = None
                self._pending = []
                return Message(kind=Kind.OTHER, status=b, raw=bytes([b]))
            self._running = b
            self._pending = []
            return None

        # data byte
        if self._running is None:
            return None                   # orphaned data, nothing to attach it to

        self._pending.append(b)
        need = _DATA_LEN[self._running & 0xF0]
        if len(self._pending) < need:
            return None

        status, data = self._running, self._pending
        self._pending = []                # running status stays armed
        return _build(status, data)


def _build(status: int, data: list[int]) -> Message:
    kind_bits = status & 0xF0
    channel = (status & 0x0F) + 1         # 1-based (SPEC 3.2)
    d1 = data[0]
    d2 = data[1] if len(data) > 1 else 0
    raw = bytes([status, *data])

    if kind_bits == NOTE_ON:
        # velocity 0 IS note-off - how the EZ-150 reports key release
        kind = Kind.NOTE_OFF if d2 == 0 else Kind.NOTE_ON
    elif kind_bits == NOTE_OFF:
        kind = Kind.NOTE_OFF
    elif kind_bits == CONTROL_CHANGE:
        kind = Kind.CONTROL_CHANGE
    elif kind_bits == PROGRAM_CHANGE:
        kind = Kind.PROGRAM_CHANGE
    elif kind_bits == PITCH_BEND:
        kind = Kind.PITCH_BEND
    elif kind_bits in (POLY_AFTERTOUCH, CHANNEL_PRESSURE):
        kind = Kind.AFTERTOUCH
    else:
        kind = Kind.OTHER

    return Message(kind=kind, channel=channel, data1=d1, data2=d2,
                   status=status, raw=raw)
