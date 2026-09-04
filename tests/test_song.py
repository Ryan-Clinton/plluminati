"""SMF reading, the song model, and hand detection."""

import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plluminati import smf, hands                              # noqa: E402
from plluminati.song import BarMap, TempoMap, load             # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SONGS = os.path.join(os.path.dirname(HERE), "songs", "test")


def vlq(n):
    if n == 0:
        return b"\x00"
    out = []
    while n:
        out.append(n & 0x7F)
        n >>= 7
    out.reverse()
    return bytes([b | 0x80 for b in out[:-1]] + [out[-1]])


def build(tracks, fmt=1, division=480):
    data = b"MThd" + struct.pack(">IHHH", 6, fmt, len(tracks), division)
    for events in tracks:
        body = b"".join(vlq(d) + p for d, p in events) + b"\x00\xFF\x2F\x00"
        data += b"MTrk" + struct.pack(">I", len(body)) + body
    return data


class TestSMF(unittest.TestCase):

    def test_rejects_non_smf(self):
        with self.assertRaises(smf.SMFError):
            smf.read(b"this is not a midi file at all")

    def test_rejects_smpte_division(self):
        data = b"MThd" + struct.pack(">IHHH", 6, 1, 1, 0xE728)
        with self.assertRaises(smf.SMFError):
            smf.read(data)

    def test_running_status_in_file(self):
        """Three note-ons sharing one status byte."""
        events = [(0, bytes([0x90, 60, 64])), (10, bytes([62, 64])),
                  (10, bytes([64, 64]))]
        parsed = smf.read(build([events]))
        notes = [e for e in parsed.tracks[0].events if e.kind == "note_on"]
        self.assertEqual([n.note for n in notes], [60, 62, 64])
        self.assertEqual([n.tick for n in notes], [0, 10, 20])

    def test_note_on_velocity_zero_is_note_off(self):
        events = [(0, bytes([0x90, 60, 64])), (48, bytes([0x90, 60, 0]))]
        parsed = smf.read(build([events]))
        kinds = [e.kind for e in parsed.tracks[0].events]
        self.assertEqual(kinds, ["note_on", "note_off"])

    def test_meta_and_sysex_skipped_by_length(self):
        """A SysEx containing 0x90 must not be mistaken for a note."""
        events = [
            (0, b"\xFF\x51\x03\x07\xA1\x20"),                 # tempo
            (0, b"\xF0\x04\x90\x3C\x40\xF7"),                 # sysex w/ note bytes
            (0, bytes([0x90, 60, 64])),
        ]
        parsed = smf.read(build([events]))
        notes = [e for e in parsed.tracks[0].events if e.kind == "note_on"]
        self.assertEqual(len(notes), 1, "sysex payload leaked into note parsing")
        tempos = [e for e in parsed.tracks[0].events if e.kind == "meta_tempo"]
        self.assertEqual(tempos[0].tempo, 500000)

    def test_truncated_track_does_not_explode(self):
        body = b"\x00\x90\x3C"                                 # cut mid-message
        data = (b"MThd" + struct.pack(">IHHH", 6, 0, 1, 480)
                + b"MTrk" + struct.pack(">I", len(body)) + body)
        parsed = smf.read(data)
        self.assertEqual(parsed.tracks[0].events, [])

    def test_end_of_track_stops_parsing(self):
        body = b"\x00\xFF\x2F\x00" + b"\x00\x90\x3C\x40"
        data = (b"MThd" + struct.pack(">IHHH", 6, 0, 1, 480)
                + b"MTrk" + struct.pack(">I", len(body)) + body)
        parsed = smf.read(data)
        self.assertEqual(parsed.tracks[0].events, [])


class TestTempoMap(unittest.TestCase):

    def test_default_120bpm(self):
        tm = TempoMap(480, [])
        self.assertAlmostEqual(tm.seconds(480), 0.5, places=6)
        self.assertAlmostEqual(tm.bpm_at(0), 120.0, places=3)

    def test_tempo_change_midway(self):
        # 120bpm for one beat, then 60bpm
        tm = TempoMap(480, [(0, 500000), (480, 1000000)])
        self.assertAlmostEqual(tm.seconds(480), 0.5, places=6)
        self.assertAlmostEqual(tm.seconds(960), 1.5, places=6)

    def test_monotonic(self):
        tm = TempoMap(480, [(0, 500000), (960, 250000)])
        times = [tm.seconds(t) for t in range(0, 2000, 100)]
        self.assertEqual(times, sorted(times))


class TestBarMap(unittest.TestCase):

    def test_four_four(self):
        bm = BarMap(480, [(0, 4, 4)])
        self.assertEqual(bm.position(0), (1, 1.0))
        self.assertEqual(bm.position(480)[0], 1)
        self.assertEqual(bm.position(1920)[0], 2)
        self.assertEqual(bm.position(3840)[0], 3)

    def test_three_four(self):
        bm = BarMap(480, [(0, 3, 4)])
        self.assertEqual(bm.position(1440)[0], 2)

    def test_time_signature_change(self):
        bm = BarMap(480, [(0, 4, 4), (1920, 3, 4)])
        self.assertEqual(bm.position(1920)[0], 2)
        self.assertEqual(bm.position(1920 + 1440)[0], 3)


class TestRealFiles(unittest.TestCase):

    def test_simple_piano(self):
        s = load(os.path.join(SONGS, "simple-piano.mid"))
        self.assertEqual(s.format, 1)
        self.assertEqual(len(s.notes), 22)
        self.assertEqual(s.channels, [1])
        self.assertEqual(s.pitch_range, (43, 69))
        self.assertEqual(s.hand_detection["strategy"], "track_names")
        self.assertEqual(s.out_of_range, [])
        self.assertTrue(all(n.hand in ("left", "right") for n in s.notes))

    def test_chords_group_into_steps(self):
        """22 notes but only 14 steps - the hands land together."""
        s = load(os.path.join(SONGS, "simple-piano.mid"))
        self.assertEqual(len(s.steps), 14)
        self.assertEqual(s.steps[0].pitches, [48, 60])
        self.assertEqual(s.steps[0].hands, {"left", "right"})

    def test_edge_cases_flags_out_of_range(self):
        s = load(os.path.join(SONGS, "edge-cases.mid"))
        oor = sorted({n.pitch for n in s.out_of_range})
        self.assertEqual(oor, [24, 108])
        self.assertFalse(all(st.playable for st in s.steps))

    def test_edge_cases_low_confidence_is_flagged(self):
        """Two overlapping parts must NOT be reported as confidently split."""
        s = load(os.path.join(SONGS, "edge-cases.mid"))
        d = s.hand_detection
        self.assertLess(d["confidence"], 0.5)
        self.assertTrue(d.get("needs_confirmation"))

    def test_repeated_pitch_stays_separate_steps(self):
        """Four repeated C4s must be four steps, not one."""
        s = load(os.path.join(SONGS, "edge-cases.mid"))
        first_four = s.steps[:4]
        self.assertTrue(all(st.pitches == [60] for st in first_four))
        self.assertEqual(len({st.index for st in first_four}), 4)

    def test_rolled_chord_does_not_chain(self):
        """A chord spread over 240 ticks must not collapse via chaining."""
        s = load(os.path.join(SONGS, "edge-cases.mid"))
        big = [st for st in s.steps if len(st.targets) >= 3]
        self.assertTrue(big, "expected at least the block chord as one step")

    def test_duplicate_pitch_across_tracks_kept(self):
        """Same pitch in two tracks: one physical key, two source notes."""
        s = load(os.path.join(SONGS, "edge-cases.mid"))
        multi = [st for st in s.steps if len(st.targets) > len(st.pitches)]
        self.assertTrue(multi, "duplicate-pitch step collapsed too early")

    def test_channel_dense_has_ten_channels(self):
        s = load(os.path.join(SONGS, "channel-dense.mid"))
        self.assertEqual(len(s.channels), 10)

    def test_fingerprint_is_stable_and_distinct(self):
        a = load(os.path.join(SONGS, "simple-piano.mid"))
        b = load(os.path.join(SONGS, "simple-piano.mid"))
        c = load(os.path.join(SONGS, "pianobooster-conv.mid"))
        self.assertEqual(a.fingerprint, b.fingerprint)
        self.assertNotEqual(a.fingerprint, c.fingerprint)

    def test_steps_for_hand_filters(self):
        s = load(os.path.join(SONGS, "simple-piano.mid"))
        right = s.steps_for_hand("right")
        left = s.steps_for_hand("left")
        self.assertTrue(right and left)
        self.assertLessEqual(len(left), len(s.steps))
        self.assertTrue(all("right" in st.hands for st in right))


class TestHandOverrides(unittest.TestCase):

    def test_manual_split(self):
        s = load(os.path.join(SONGS, "simple-piano.mid"))
        rep = hands.apply_split(s, 55)
        self.assertEqual(rep["confidence"], 1.0)
        self.assertTrue(all(n.hand == "right" for n in s.notes if n.pitch >= 55))
        self.assertTrue(all(n.hand == "left" for n in s.notes if n.pitch < 55))

    def test_manual_tracks(self):
        s = load(os.path.join(SONGS, "simple-piano.mid"))
        hands.apply_tracks(s, {1: "right", 2: "left"})
        self.assertTrue(any(n.hand == "right" for n in s.notes))

    def test_channels_34_not_assumed(self):
        """PianoBooster's convention must not be applied just because 3/4 exist."""
        s = load(os.path.join(SONGS, "pianobooster-conv.mid"))
        self.assertNotIn("pianobooster", s.hand_detection["strategy"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
