#!/usr/bin/env python3
"""
Generate Standard MIDI Files for testing Plluminati and PianoBooster.

Pure stdlib - no mido, no dependencies. Writes format-1 SMFs.

Files produced (see docs/SPEC.md for what each one probes):

  simple-piano.mid          2 tracks, BOTH on channel 1 - the typical MuseScore
                            export. Exercises the two-track hand heuristic AND
                            collides with the cue-light channel (SPEC 3.2).
  pianobooster-conv.mid     right hand ch4, left hand ch3 - PianoBooster's
                            documented convention (SPEC 7.3).
  channel-dense.mid         channels 1-10 all occupied. Designed to trigger
                            PianoBooster's findFreeChannel() fallback to
                            channels 15/16, which the EZ-150 cannot hear
                            (SPEC 5.2). THE decisive M0-8 test file.
  edge-cases.mid            repeated same pitches, rolled chords, simultaneous
                            duplicate pitches on two tracks, and notes outside
                            a 61-key range (SPEC 7.4, F10, section 8).
"""
import os, struct

TPB = 480          # ticks per beat
Q   = TPB          # quarter
H   = TPB * 2      # half
E   = TPB // 2     # eighth


def vlq(n):
    """MIDI variable-length quantity."""
    if n == 0:
        return b"\x00"
    out = []
    while n:
        out.append(n & 0x7F)
        n >>= 7
    out.reverse()
    return bytes([b | 0x80 for b in out[:-1]] + [out[-1]])


class Track:
    def __init__(self, name=None):
        self.ev = []            # (delta, bytes)
        if name:
            nb = name.encode("ascii", "replace")
            self.ev.append((0, b"\xFF\x03" + vlq(len(nb)) + nb))

    def tempo(self, bpm):
        us = int(60_000_000 / bpm)
        self.ev.append((0, b"\xFF\x51\x03" + struct.pack(">I", us)[1:]))
        return self

    def timesig(self, num=4, den=4):
        import math
        self.ev.append((0, bytes([0xFF, 0x58, 0x04, num, int(math.log2(den)), 24, 8])))
        return self

    def program(self, ch, prog):
        self.ev.append((0, bytes([0xC0 | ch, prog])))
        return self

    def note(self, ch, pitch, dur, vel=80, gap=0):
        """Append a sequential note (delta 0 from previous end)."""
        self.ev.append((0, bytes([0x90 | ch, pitch, vel])))
        self.ev.append((dur, bytes([0x80 | ch, pitch, 0])))
        if gap:
            self.ev.append((gap, b""))
        return self

    def chord(self, ch, pitches, dur, vel=80, spread=0):
        """Simultaneous (or rolled, if spread>0) notes."""
        for i, p in enumerate(pitches):
            self.ev.append((spread if i else 0, bytes([0x90 | ch, p, vel])))
        for i, p in enumerate(pitches):
            self.ev.append((dur if i == 0 else 0, bytes([0x80 | ch, p, 0])))
        return self

    def rest(self, dur):
        self.ev.append((dur, b""))
        return self

    def encode(self):
        data = bytearray()
        pending = 0
        for delta, payload in self.ev:
            pending += delta
            if not payload:
                continue
            data += vlq(pending) + payload
            pending = 0
        data += vlq(pending) + b"\xFF\x2F\x00"      # end of track
        return b"MTrk" + struct.pack(">I", len(data)) + bytes(data)


def write_smf(path, tracks, fmt=1):
    hdr = b"MThd" + struct.pack(">IHHH", 6, fmt, len(tracks), TPB)
    with open(path, "wb") as f:
        f.write(hdr)
        for t in tracks:
            f.write(t.encode())
    return os.path.getsize(path)


# ---------------------------------------------------------------- content

# "Twinkle Twinkle Little Star" - traditional/public domain, and song #41
# in the EZ-150's own ROM (reference/builtin-songs.json), which makes it a
# convenient cross-check against the keyboard's built-in Lesson mode.
RH = [(60,Q),(60,Q),(67,Q),(67,Q), (69,Q),(69,Q),(67,H),
      (65,Q),(65,Q),(64,Q),(64,Q), (62,Q),(62,Q),(60,H)]
LH = [(48,H),(43,H), (53,H),(48,H), (53,H),(48,H), (43,H),(48,H)]


def build(rh_ch, lh_ch, rh_name="Right Hand", lh_name="Left Hand"):
    t0 = Track("Tempo Map").tempo(100).timesig(4, 4)
    tr = Track(rh_name).program(rh_ch, 0)
    for p, d in RH:
        tr.note(rh_ch, p, d)
    tl = Track(lh_name).program(lh_ch, 0)
    for p, d in LH:
        tl.note(lh_ch, p, d)
    return [t0, tr, tl]


OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "songs", "test")
os.makedirs(OUT, exist_ok=True)
def p(n): return os.path.normpath(os.path.join(OUT, n))

results = []

# 1. typical MuseScore-style export: two tracks, both channel 1 (index 0)
results.append(("simple-piano.mid", write_smf(p("simple-piano.mid"), build(0, 0))))

# 2. PianoBooster convention: right=ch4 (idx3), left=ch3 (idx2)
results.append(("pianobooster-conv.mid", write_smf(p("pianobooster-conv.mid"), build(3, 2))))

# 3. channel-dense: occupy channels 1..10 (idx 0..9) to starve findFreeChannel()
dense = [Track("Tempo Map").tempo(100).timesig(4, 4)]
dense += build(0, 2)[1:]                       # melody + bass on ch1 / ch3
for idx, ch in enumerate([1, 3, 4, 5, 6, 7, 8, 9]):   # incl. ch10 drums (idx 9)
    t = Track(f"Filler ch{ch+1}").program(ch, 48 if ch != 9 else 0)
    base = 36 + idx * 2
    for bar in range(8):
        t.chord(ch, [base, base + 4], H, vel=55)
    dense.append(t)
results.append(("channel-dense.mid", write_smf(p("channel-dense.mid"), dense)))

# 4. edge cases for the matcher (SPEC F10 / 7.4)
t0 = Track("Tempo Map").tempo(90).timesig(4, 4)
ea = Track("Edge A").program(0, 0)
for _ in range(4):                       # repeated SAME pitch - the held-key bug
    ea.note(0, 60, Q)
ea.chord(0, [60, 64, 67], H)             # block chord
ea.chord(0, [60, 64, 67], H, spread=E//2)  # ROLLED chord - tests spread rule
ea.note(0, 24, Q)                        # BELOW a 61-key range (C1)
ea.note(0, 108, Q)                       # ABOVE a 61-key range (C8)
ea.note(0, 62, Q)
eb = Track("Edge B").program(1, 0)
eb.rest(Q * 4)
eb.chord(1, [60, 64, 67], H)             # DUPLICATE pitches, different track
results.append(("edge-cases.mid", write_smf(p("edge-cases.mid"), [t0, ea, eb])))

for name, size in results:
    print(f"  {name:<26} {size:>6} bytes")
print(f"\nwritten to: {os.path.normpath(OUT)}")
