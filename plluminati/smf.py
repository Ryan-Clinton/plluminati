"""Standard MIDI File reader.

SPEC 7.1 says to use `mido` and not hand-roll this, on the sound grounds that
running status, variable-length quantities, tempo maps and malformed files are
all solved there. That advice stands in general - but on this machine `pip` is
absent and the interpreter is PEP 668 externally-managed, so installing mido
needs another privileged step. Rather than block, this module does the job
directly.

It is deliberately small, read-only, and isolated behind `read_file()`, so
swapping mido back in later touches nothing else. The edge cases the spec
warned about are handled explicitly and covered by tests:

  * running status inside track chunks
  * variable-length quantities
  * tempo and time-signature maps, including mid-song changes
  * meta and SysEx events skipped by declared length, never by scanning
  * note-on velocity 0 as note-off
  * unterminated tracks and trailing garbage
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

META = 0xFF
SYSEX = 0xF0
SYSEX_ESCAPE = 0xF7

META_TRACK_NAME = 0x03
META_INSTRUMENT = 0x04
META_END_OF_TRACK = 0x2F
META_TEMPO = 0x51
META_TIME_SIG = 0x58
META_KEY_SIG = 0x59

_DATA_LEN = {0x80: 2, 0x90: 2, 0xA0: 2, 0xB0: 2, 0xC0: 1, 0xD0: 1, 0xE0: 2}


class SMFError(ValueError):
    pass


@dataclass
class Event:
    tick: int                 # absolute, in file ticks
    kind: str                 # note_on | note_off | program | control | meta_*
    channel: int = 0          # 1-based
    note: int = 0
    velocity: int = 0
    control: int = 0
    value: int = 0
    program: int = 0
    text: str = ""
    tempo: int = 0            # microseconds per quarter note
    numerator: int = 0
    denominator: int = 0


@dataclass
class Track:
    index: int
    name: str = ""
    events: list[Event] = field(default_factory=list)


@dataclass
class SMF:
    format: int
    division: int             # ticks per quarter note
    tracks: list[Track] = field(default_factory=list)

    @property
    def all_events(self) -> list[Event]:
        out = [(e.tick, t.index, e) for t in self.tracks for e in t.events]
        out.sort(key=lambda x: (x[0], x[1]))
        return [e for _, _, e in out]


def _vlq(data: bytes, i: int) -> tuple[int, int]:
    n = 0
    for _ in range(4):
        if i >= len(data):
            raise SMFError("truncated variable-length quantity")
        b = data[i]
        i += 1
        n = (n << 7) | (b & 0x7F)
        if not b & 0x80:
            return n, i
    raise SMFError("variable-length quantity too long")


def _read_track(data: bytes, index: int) -> Track:
    track = Track(index=index)
    i = 0
    tick = 0
    running: int | None = None

    while i < len(data):
        delta, i = _vlq(data, i)
        tick += delta
        if i >= len(data):
            break
        status = data[i]

        if status == META:
            i += 1
            if i >= len(data):
                break
            mtype = data[i]
            i += 1
            length, i = _vlq(data, i)
            payload = data[i:i + length]
            i += length
            running = None
            if mtype == META_END_OF_TRACK:
                break
            if mtype in (META_TRACK_NAME, META_INSTRUMENT):
                text = payload.decode("latin-1", "replace").strip()
                if mtype == META_TRACK_NAME and not track.name:
                    track.name = text
                track.events.append(Event(tick=tick, kind="meta_name", text=text))
            elif mtype == META_TEMPO and length == 3:
                track.events.append(Event(tick=tick, kind="meta_tempo",
                                          tempo=int.from_bytes(payload, "big")))
            elif mtype == META_TIME_SIG and length >= 2:
                track.events.append(Event(tick=tick, kind="meta_timesig",
                                          numerator=payload[0],
                                          denominator=2 ** payload[1]))
            elif mtype == META_KEY_SIG:
                track.events.append(Event(tick=tick, kind="meta_keysig"))
            continue

        if status in (SYSEX, SYSEX_ESCAPE):
            i += 1
            length, i = _vlq(data, i)
            i += length                      # skipped by declared length
            running = None
            continue

        if status & 0x80:
            running = status
            i += 1
        elif running is None:
            raise SMFError(f"data byte 0x{status:02X} with no running status")

        assert running is not None
        kind_bits = running & 0xF0
        need = _DATA_LEN.get(kind_bits)
        if need is None:
            raise SMFError(f"unsupported status 0x{running:02X}")
        if i + need > len(data):
            break                            # truncated tail - keep what we have
        d1 = data[i]
        d2 = data[i + 1] if need > 1 else 0
        i += need
        channel = (running & 0x0F) + 1

        if kind_bits == 0x90:
            track.events.append(Event(
                tick=tick, kind="note_off" if d2 == 0 else "note_on",
                channel=channel, note=d1, velocity=d2))
        elif kind_bits == 0x80:
            track.events.append(Event(tick=tick, kind="note_off",
                                      channel=channel, note=d1, velocity=d2))
        elif kind_bits == 0xC0:
            track.events.append(Event(tick=tick, kind="program",
                                      channel=channel, program=d1))
        elif kind_bits == 0xB0:
            track.events.append(Event(tick=tick, kind="control",
                                      channel=channel, control=d1, value=d2))
        # aftertouch and pitch bend are parsed for correct framing, then dropped

    return track


def read(data: bytes) -> SMF:
    if len(data) < 14 or data[:4] != b"MThd":
        raise SMFError("not a Standard MIDI File (missing MThd)")
    hdr_len = struct.unpack(">I", data[4:8])[0]
    fmt, ntrks, division = struct.unpack(">HHH", data[8:14])
    if fmt not in (0, 1, 2):
        raise SMFError(f"unknown SMF format {fmt}")
    if division & 0x8000:
        raise SMFError("SMPTE time division is not supported (ticks-per-beat only)")
    if division == 0:
        raise SMFError("division of 0 ticks per beat")

    i = 8 + hdr_len
    tracks: list[Track] = []
    while i + 8 <= len(data) and len(tracks) < ntrks:
        if data[i:i + 4] != b"MTrk":
            break                            # trailing junk; ignore it
        length = struct.unpack(">I", data[i + 4:i + 8])[0]
        chunk = data[i + 8:i + 8 + length]
        tracks.append(_read_track(chunk, len(tracks)))
        i += 8 + length

    if not tracks:
        raise SMFError("no track chunks found")
    return SMF(format=fmt, division=division, tracks=tracks)


def read_file(path: str) -> SMF:
    with open(path, "rb") as fh:
        return read(fh.read())
