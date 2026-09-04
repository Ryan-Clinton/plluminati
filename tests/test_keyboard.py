"""Keyboard bus behaviour, ownership and the held-note protection."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plluminati.keyboard import (ACCOMP_CHANNEL, CUE_CHANNEL, CUE_VELOCITY,
                                 Keyboard, _status)                # noqa: E402
from plluminati.parser import Kind, Message                        # noqa: E402


class RecordingPort:
    def __init__(self):
        self.sent = []

    def send(self, data, priority=None):
        self.sent.append(bytes(data))

    def flush(self, timeout=None):
        return True

    @property
    def messages(self):
        """Split the byte stream into 3-byte messages for assertions."""
        blob = b"".join(self.sent)
        out, i = [], 0
        while i < len(blob):
            status = blob[i]
            n = 2 if status & 0xF0 != 0xC0 else 1
            out.append(tuple(blob[i:i + 1 + n]))
            i += 1 + n
        return out


def kb_with_port():
    port = RecordingPort()
    return Keyboard(port), port


class TestBuses(unittest.TestCase):

    def test_cue_uses_channel_1_velocity_1(self):
        kb, port = kb_with_port()
        kb.cue_on(60)
        self.assertEqual(port.messages, [(_status(0x90, CUE_CHANNEL), 60, 1)])

    def test_accompaniment_uses_channel_2(self):
        kb, port = kb_with_port()
        kb.play(48, velocity=70)
        self.assertEqual(port.messages, [(_status(0x90, ACCOMP_CHANNEL), 48, 70)])

    def test_chord_is_one_write(self):
        """Whole messages, batched - never a partial message on the wire."""
        kb, port = kb_with_port()
        kb.cue_on(60, 64, 67)
        self.assertEqual(len(port.sent), 1)
        self.assertEqual(len(port.messages), 3)


class TestHeldNoteProtection(unittest.TestCase):
    """Measured on hardware: our note-off cuts a note the player is holding,
    because their key and our cue are the same logical note."""

    def test_cue_off_skipped_while_player_holds_the_key(self):
        kb, port = kb_with_port()
        kb.cue_on(60)
        kb.handle(Message(kind=Kind.NOTE_ON, channel=1, data1=60, data2=84))
        port.sent.clear()
        kb.cue_off(60)
        self.assertEqual(port.sent, [],
                         "must not silence a note the player is holding")
        self.assertIn(60, kb.deferred_offs)

    def test_cue_off_sent_when_key_is_not_held(self):
        kb, port = kb_with_port()
        kb.cue_on(60)
        port.sent.clear()
        kb.cue_off(60)
        self.assertEqual(port.messages, [(_status(0x80, CUE_CHANNEL), 60, 0)])

    def test_release_settles_the_deferred_off(self):
        kb, port = kb_with_port()
        kb.cue_on(60)
        kb.handle(Message(kind=Kind.NOTE_ON, channel=1, data1=60, data2=84))
        kb.cue_off(60)
        self.assertIn(60, kb.deferred_offs)
        kb.handle(Message(kind=Kind.NOTE_OFF, channel=1, data1=60))
        self.assertNotIn(60, kb.deferred_offs,
                         "release extinguishes it; nothing left to do")
        self.assertNotIn(60, kb.lit)

    def test_panic_does_not_cut_a_held_note(self):
        kb, port = kb_with_port()
        kb.cue_on(60, 64)
        kb.handle(Message(kind=Kind.NOTE_ON, channel=1, data1=60, data2=84))
        port.sent.clear()
        kb.panic()
        offs = [m for m in port.messages if m[0] == _status(0x80, CUE_CHANNEL)]
        self.assertEqual([m[1] for m in offs], [64])


class TestBookkeeping(unittest.TestCase):

    def test_release_clears_our_stale_lit_record(self):
        """Pressing then releasing extinguishes the light with no message from
        us, so `lit` must not keep claiming it."""
        kb, _ = kb_with_port()
        kb.cue_on(60)
        self.assertIn(60, kb.lit)
        kb.handle(Message(kind=Kind.NOTE_ON, channel=1, data1=60, data2=84))
        kb.handle(Message(kind=Kind.NOTE_OFF, channel=1, data1=60))
        self.assertNotIn(60, kb.lit)

    def test_velocity_calibration_ignores_our_own_cues(self):
        kb, _ = kb_with_port()
        kb.handle(Message(kind=Kind.NOTE_ON, channel=1, data1=60,
                          data2=CUE_VELOCITY))
        self.assertIsNone(kb.player_velocity)
        kb.handle(Message(kind=Kind.NOTE_ON, channel=1, data1=60, data2=84))
        self.assertEqual(kb.player_velocity, 84)

    def test_reset_burst_reapplies_session_setup(self):
        """Leaving the attract display resets ch2 to a pad; we must undo that."""
        kb, port = kb_with_port()
        port.sent.clear()
        for ch in (1, 10, 2):
            kb.handle(Message(kind=Kind.PROGRAM_CHANGE, channel=ch, data1=0))
        self.assertEqual(kb.resets_seen, 1)
        self.assertIn((_status(0xC0, ACCOMP_CHANNEL), 0), port.messages)

    def test_liveness_follows_active_sensing(self):
        kb, _ = kb_with_port()
        self.assertFalse(kb.is_live)
        kb.handle(Message(kind=Kind.REALTIME, status=0xFE))
        self.assertTrue(kb.is_live)




class TestOfflineKeyboard(unittest.TestCase):
    """Running with no keyboard attached.

    The UI is worth browsing without an EZ-150 on the desk - for a screenshot,
    a demo, or anyone who cloned the repo out of curiosity.
    """

    def test_accepts_everything_and_sends_nothing(self):
        from plluminati.session import OfflineKeyboard
        kb = OfflineKeyboard()
        kb.cue_on(60, 64)
        kb.play(48)
        kb.hit_wrong("hihat")
        kb.apply_session_setup()
        kb.panic()                       # must not raise

    def test_reports_itself_live(self):
        """Otherwise the UI sits forever asking the player to wake a keyboard
        that is not there."""
        from plluminati.session import OfflineKeyboard
        self.assertTrue(OfflineKeyboard().is_live)

    def test_context_manager_cleans_up(self):
        from plluminati.session import open_offline
        with open_offline() as kb:
            kb.cue_on(60)
        self.assertEqual(kb.lit, set())

    def test_open_keyboard_offline_needs_no_device(self):
        from plluminati.session import open_keyboard
        with open_keyboard(offline=True) as kb:
            kb.cue_on(60)
            self.assertTrue(kb.is_live)


if __name__ == "__main__":
    unittest.main(verbosity=2)
