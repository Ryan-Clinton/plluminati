"""Parser tests - the pure-logic half, exercised against real captured bytes."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plluminati.parser import Kind, StreamParser  # noqa: E402


class TestParser(unittest.TestCase):

    def setUp(self):
        self.p = StreamParser()

    def test_note_on(self):
        [m] = self.p.feed(bytes([0x90, 0x3C, 0x54]))
        self.assertIs(m.kind, Kind.NOTE_ON)
        self.assertEqual((m.channel, m.note, m.velocity), (1, 60, 84))

    def test_note_on_velocity_zero_is_note_off(self):
        """How the EZ-150 actually reports key release (captured 90 45 00)."""
        [m] = self.p.feed(bytes([0x90, 0x45, 0x00]))
        self.assertIs(m.kind, Kind.NOTE_OFF)
        self.assertEqual(m.note, 69)

    def test_explicit_note_off(self):
        [m] = self.p.feed(bytes([0x80, 0x3C, 0x40]))
        self.assertIs(m.kind, Kind.NOTE_OFF)

    def test_running_status(self):
        msgs = self.p.feed(bytes([0x90, 0x3C, 0x40, 0x3E, 0x40, 0x40, 0x40]))
        self.assertEqual(len(msgs), 3)
        self.assertEqual([m.note for m in msgs], [60, 62, 64])
        self.assertTrue(all(m.kind is Kind.NOTE_ON for m in msgs))

    def test_active_sensing_between_data_bytes(self):
        """Realtime bytes may appear ANYWHERE and must not corrupt the message."""
        msgs = self.p.feed(bytes([0x90, 0xFE, 0x3C, 0xFE, 0x40]))
        notes = [m for m in msgs if m.kind is Kind.NOTE_ON]
        sensing = [m for m in msgs if m.is_active_sensing]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].note, 60)
        self.assertEqual(len(sensing), 2)

    def test_active_sensing_does_not_break_running_status(self):
        msgs = self.p.feed(bytes([0x90, 0x3C, 0x40, 0xFE, 0x3E, 0x40]))
        notes = [m.note for m in msgs if m.kind is Kind.NOTE_ON]
        self.assertEqual(notes, [60, 62])

    def test_split_across_reads(self):
        self.assertEqual(self.p.feed(bytes([0x90])), [])
        self.assertEqual(self.p.feed(bytes([0x3C])), [])
        [m] = self.p.feed(bytes([0x40]))
        self.assertEqual(m.note, 60)

    def test_channel_is_one_based(self):
        [m] = self.p.feed(bytes([0x91, 0x45, 0x64]))
        self.assertEqual(m.channel, 2)
        [m] = self.p.feed(bytes([0x99, 0x24, 0x64]))
        self.assertEqual(m.channel, 10)

    def test_program_change_is_one_data_byte(self):
        msgs = self.p.feed(bytes([0xC1, 0x58, 0xC2, 0x1B]))
        self.assertEqual(len(msgs), 2)
        self.assertIs(msgs[0].kind, Kind.PROGRAM_CHANGE)
        self.assertEqual((msgs[0].channel, msgs[0].data1), (2, 88))
        self.assertEqual((msgs[1].channel, msgs[1].data1), (3, 27))

    def test_control_change(self):
        [m] = self.p.feed(bytes([0xB0, 0x07, 0x7F]))
        self.assertIs(m.kind, Kind.CONTROL_CHANGE)
        self.assertEqual((m.controller, m.value), (7, 127))

    def test_sysex_consumed_whole(self):
        msgs = self.p.feed(bytes([0xF0, 0x7E, 0x7F, 0x09, 0x01, 0xF7, 0x90, 0x3C, 0x40]))
        self.assertIs(msgs[0].kind, Kind.SYSEX)
        self.assertIs(msgs[1].kind, Kind.NOTE_ON)

    def test_real_capture_power_on_dump(self):
        """The actual bytes captured when the keyboard left the attract display.

        Ends with the player's middle C at velocity 84, and resets channel 2 to
        program 88 - the burst that silently undoes our accompaniment voice.
        """
        raw = bytes.fromhex(
            "B00000 B02070 C000 B04000 B00A40 B0077F"
            "B9007F B92000 C900 B90773 B90A40 B94000"
            "B10000 B12000 C158 B10773 B10A40 B14000"
            "B20000 B22000 C21B B20773 B20A40 B24000"
            "903C54".replace(" ", ""))
        msgs = self.p.feed(raw)
        programs = {m.channel: m.data1 for m in msgs if m.kind is Kind.PROGRAM_CHANGE}
        self.assertEqual(programs[1], 0)     # ch1 grand piano
        self.assertEqual(programs[10], 0)    # ch10 drums
        self.assertEqual(programs[2], 88)    # ch2 reset to the pad
        notes = [m for m in msgs if m.kind is Kind.NOTE_ON]
        self.assertEqual(len(notes), 1)
        self.assertEqual((notes[0].note, notes[0].velocity), (60, 84))

    def test_orphan_data_bytes_ignored(self):
        self.assertEqual(self.p.feed(bytes([0x3C, 0x40])), [])


class TestResetDetector(unittest.TestCase):

    def test_detects_multi_channel_program_burst(self):
        from plluminati.keyboard import ResetDetector
        from plluminati.parser import Kind as K, Message
        det = ResetDetector()
        fired = []
        for ch in (1, 10, 2, 3):
            fired.append(det.observe(
                Message(kind=K.PROGRAM_CHANGE, channel=ch, data1=0)))
        self.assertEqual(fired[:2], [False, False])
        self.assertTrue(fired[2], "three distinct channels should trip it")

    def test_ignores_single_program_change(self):
        from plluminati.keyboard import ResetDetector
        from plluminati.parser import Kind as K, Message
        det = ResetDetector()
        for _ in range(5):
            self.assertFalse(det.observe(
                Message(kind=K.PROGRAM_CHANGE, channel=2, data1=0)))

    def test_ignores_notes(self):
        from plluminati.keyboard import ResetDetector
        from plluminati.parser import Kind as K, Message
        det = ResetDetector()
        self.assertFalse(det.observe(Message(kind=K.NOTE_ON, channel=1, data1=60)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
