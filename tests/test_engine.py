"""Learn-mode engine, driven against a fake keyboard."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plluminati.engine import LearnSession                     # noqa: E402
from plluminati.parser import Kind, Message                    # noqa: E402
from plluminati.song import Note, Step, load                   # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SONGS = os.path.join(os.path.dirname(HERE), "songs", "test")


class FakePort:
    def flush(self, timeout=None):
        return True


class FakeKeyboard:
    """Records what the engine asked the hardware to do."""

    def __init__(self, live=True):
        self.held = set()
        self.port = FakePort()
        self.lit = set()
        self.sounding = set()
        self.local_muted = False
        self.hits = []
        self.cue_on_calls = []
        self.played = []
        self.is_live = live
        self.asleep = not live          # cues sent while asleep are DROPPED
        self._on_reset = None

    def on_reset(self, cb):
        self._on_reset = cb

    def wake(self):
        self.is_live = True
        self.asleep = False

    def cue_on(self, *notes):
        self.cue_on_calls.append(tuple(notes))
        if self.asleep:
            return          # the real keyboard ignores everything while asleep
        self.lit.update(notes)

    def cue_off(self, *notes):
        self.lit.difference_update(notes)

    def play(self, *notes, velocity=80):
        self.played.append(tuple(notes))
        self.sounding.update(notes)

    def stop(self, *notes):
        self.sounding.difference_update(notes)

    def stop_all(self):
        self.sounding.clear()

    def echo(self, note, velocity=90):
        self.play(note, velocity=velocity)

    def hit_wrong(self, sound="hihat", velocity=100):
        self.hits.append(sound)

    def set_local_mute(self, on):
        self.local_muted = on

    # --- helpers that mimic Keyboard.handle's bookkeeping order -------------

    def press(self, session, note):
        self.held.add(note)
        self.lit.discard(note)          # hardware lights stay on while held
        session.handle(Message(kind=Kind.NOTE_ON, channel=1, data1=note, data2=84))

    def release(self, session, note):
        self.held.discard(note)
        self.lit.discard(note)          # release extinguishes the cue
        session.handle(Message(kind=Kind.NOTE_OFF, channel=1, data1=note))

    def tap(self, session, note):
        self.press(session, note)
        self.release(session, note)


def make_song(pitch_sequence, hand="right"):
    """Build a minimal Song-like object with one step per entry."""
    class S:
        pass
    song = S()
    song.steps = []
    nid = 0
    for i, pitches in enumerate(pitch_sequence):
        targets = []
        for p in (pitches if isinstance(pitches, (list, tuple)) else [pitches]):
            n = Note(id=nid, pitch=p, start_tick=i * 480, end_tick=i * 480 + 240,
                     channel=1, track=0, start_time=i * 0.5, end_time=i * 0.5 + 0.25,
                     hand=hand)
            nid += 1
            targets.append(n)
        song.steps.append(Step(index=i, tick=i * 480, time=i * 0.5, bar=1,
                               beat=1.0, targets=targets))
    song.steps_for_hand = lambda h: song.steps
    return song


class TestSatisfaction(unittest.TestCase):

    def test_single_note_advances(self):
        kb = FakeKeyboard()
        s = LearnSession(kb, make_song([60, 62]), accompany=False)
        s._activate(0)
        kb.tap(s, 60)
        self.assertEqual(s.report.steps_completed, 1)
        self.assertEqual(s.current.pitches, [62])

    def test_chord_needs_all_notes_held_together(self):
        kb = FakeKeyboard()
        s = LearnSession(kb, make_song([[60, 64, 67]]), accompany=False)
        s._activate(0)
        kb.press(s, 60)
        self.assertEqual(s.report.steps_completed, 0)
        kb.press(s, 64)
        self.assertEqual(s.report.steps_completed, 0)
        kb.press(s, 67)
        self.assertEqual(s.report.steps_completed, 1, "full chord should advance")

    def test_repeated_pitch_needs_a_fresh_attack(self):
        """THE bug: holding C must not satisfy a second C step.

        `required <= held` alone would advance here without a new keypress.
        """
        kb = FakeKeyboard()
        s = LearnSession(kb, make_song([60, 60, 60]), accompany=False)
        s._activate(0)
        kb.press(s, 60)                       # satisfies step 0
        self.assertEqual(s.report.steps_completed, 1)
        self.assertEqual(s.current.pitches, [60])
        # still holding C - must NOT advance
        self.assertEqual(s.report.steps_completed, 1)
        kb.release(s, 60)
        self.assertEqual(s.report.steps_completed, 1)
        kb.press(s, 60)                       # fresh attack
        self.assertEqual(s.report.steps_completed, 2)

    def test_held_key_at_activation_does_not_count(self):
        kb = FakeKeyboard()
        s = LearnSession(kb, make_song([60, 62]), accompany=False)
        kb.held.add(62)                       # already down before step 1 starts
        s._activate(0)
        kb.tap(s, 60)
        self.assertEqual(s.report.steps_completed, 1)
        self.assertEqual(s.report.steps_completed, 1,
                         "pre-held 62 must not auto-satisfy the next step")


class TestCueReassertion(unittest.TestCase):

    def test_release_relights_a_still_required_note(self):
        """Press C, release it, reach for E - C must light again."""
        kb = FakeKeyboard()
        s = LearnSession(kb, make_song([[60, 64]]), accompany=False)
        s._activate(0)
        self.assertIn(60, kb.lit)
        kb.press(s, 60)
        kb.release(s, 60)                     # hardware extinguishes C
        self.assertIn(60, kb.lit, "C should have been re-lit while still required")

    def test_cue_not_reasserted_once_step_done(self):
        kb = FakeKeyboard()
        s = LearnSession(kb, make_song([60]), accompany=False)
        s._activate(0)
        kb.press(s, 60)
        kb.release(s, 60)
        self.assertNotIn(60, kb.lit)

    def test_initial_cues_lit_on_activation(self):
        kb = FakeKeyboard()
        s = LearnSession(kb, make_song([[60, 64, 67]]), accompany=False)
        s._activate(0)
        self.assertEqual(kb.lit, {60, 64, 67})

    def test_cue_count_capped(self):
        from plluminati.keyboard import MAX_SIMULTANEOUS_CUES
        kb = FakeKeyboard()
        big = list(range(48, 48 + MAX_SIMULTANEOUS_CUES + 4))
        s = LearnSession(kb, make_song([big]), accompany=False)
        s._activate(0)
        self.assertLessEqual(len(kb.lit), MAX_SIMULTANEOUS_CUES)


class TestWrongNotes(unittest.TestCase):

    def test_wrong_note_counted_but_not_blocking(self):
        kb = FakeKeyboard()
        s = LearnSession(kb, make_song([60]), accompany=False)
        s._activate(0)
        kb.tap(s, 61)
        self.assertEqual(s.report.wrong_attacks, 1)
        self.assertEqual(s.report.steps_completed, 0)
        kb.tap(s, 60)
        self.assertEqual(s.report.steps_completed, 1)

    def test_step_records_attempts(self):
        kb = FakeKeyboard()
        s = LearnSession(kb, make_song([60]), accompany=False)
        s._activate(0)
        kb.tap(s, 61)
        kb.tap(s, 62)
        kb.tap(s, 60)
        self.assertEqual(s.report.per_step[0].attempts, 2)
        self.assertEqual(s.report.clean_steps, 0)

    def test_clean_step_has_no_attempts(self):
        kb = FakeKeyboard()
        s = LearnSession(kb, make_song([60, 62]), accompany=False)
        s._activate(0)
        kb.tap(s, 60)
        kb.tap(s, 62)
        self.assertEqual(s.report.clean_steps, 2)
        self.assertAlmostEqual(s.report.accuracy, 1.0)


class TestRangeAndAccompaniment(unittest.TestCase):

    def test_out_of_range_notes_excluded(self):
        kb = FakeKeyboard()
        s = LearnSession(kb, make_song([[24, 60, 108]]), accompany=False)
        s._activate(0)
        self.assertEqual(s._required, {60}, "24 and 108 cannot be lit or played")

    def test_steps_with_nothing_playable_are_dropped(self):
        kb = FakeKeyboard()
        s = LearnSession(kb, make_song([24, 60]), accompany=False)
        self.assertEqual(len(s.steps), 1)

    def test_accompaniment_plays_other_hand(self):
        kb = FakeKeyboard()
        song = make_song([[60, 48]])
        song.steps[0].targets[1].hand = "left"       # 48 is the other hand
        s = LearnSession(kb, song, hand="right", accompany=True)
        s._activate(0)
        self.assertEqual(s._required, {60}, "left-hand note is not waited for")
        self.assertIn((48,), kb.played, "left hand should sound")

    def test_no_accompaniment_when_both_hands(self):
        kb = FakeKeyboard()
        song = make_song([[60, 48]])
        song.steps[0].targets[1].hand = "left"
        s = LearnSession(kb, song, hand="both", accompany=True)
        s._activate(0)
        self.assertEqual(s._required, {48, 60})
        self.assertEqual(kb.played, [])


class TestSleepingKeyboard(unittest.TestCase):
    """Cues sent while the keyboard is in its attract display are dropped.

    Our `_asserted` set still claims they landed, so without an explicit
    resync the key stays dark for ever - which is exactly what happened on the
    first real run.
    """

    def test_cue_is_relit_after_waking(self):
        kb = FakeKeyboard(live=False)
        s = LearnSession(kb, make_song([60, 62]), accompany=False)
        s._activate(0)
        self.assertNotIn(60, kb.lit, "asleep keyboard ignores the cue")
        kb.wake()
        s._resync()
        self.assertIn(60, kb.lit, "cue must be re-sent once awake")

    def test_resync_clears_stale_assertions(self):
        kb = FakeKeyboard(live=False)
        s = LearnSession(kb, make_song([[60, 64]]), accompany=False)
        s._activate(0)
        self.assertEqual(kb.lit, set())
        kb.wake()
        s._resync()
        self.assertEqual(kb.lit, {60, 64})

    def test_reset_callback_is_registered_by_run(self):
        kb = FakeKeyboard()
        s = LearnSession(kb, make_song([60]), accompany=False)
        s._done.set()                       # make run() return immediately
        s.run(timeout=0.05)
        self.assertIsNotNone(kb._on_reset,
                             "a keyboard reset must trigger a cue resync")


class TestWrongNoteSound(unittest.TestCase):
    """A wrong key gets a short drum hit through the keyboard, over the top of
    its own piano. Nothing is muted - the EZ-150's keybed cannot be silenced
    and does not need to be."""

    def test_wrong_note_makes_a_noise(self):
        kb = FakeKeyboard()
        s = LearnSession(kb, make_song([60]), accompany=False,
                         wrong_sound="hihat")
        s._activate(0)
        kb.press(s, 61)
        self.assertEqual(kb.hits, ["hihat"])

    def test_off_unless_asked_for(self):
        """Layering a drum over the player's own piano is additive, not a
        replacement, so it is opt-in."""
        kb = FakeKeyboard()
        s = LearnSession(kb, make_song([60]), accompany=False)
        s._activate(0)
        kb.press(s, 61)
        self.assertEqual(kb.hits, [])

    def test_correct_note_makes_no_extra_noise_normally(self):
        """The keyboard's own piano is the sound of a right note - adding
        anything would double it."""
        kb = FakeKeyboard()
        s = LearnSession(kb, make_song([60]), accompany=False)
        s._activate(0)
        kb.press(s, 60)
        self.assertEqual(kb.hits, [])
        self.assertEqual(kb.played, [])

    def test_sound_is_selectable(self):
        kb = FakeKeyboard()
        s = LearnSession(kb, make_song([60]), accompany=False,
                         wrong_sound="scratch")
        s._activate(0)
        kb.press(s, 61)
        self.assertEqual(kb.hits, ["scratch"])

    def test_can_be_turned_off(self):
        kb = FakeKeyboard()
        s = LearnSession(kb, make_song([60]), accompany=False, wrong_sound="")
        s._activate(0)
        kb.press(s, 61)
        self.assertEqual(kb.hits, [])

    def test_hit_does_not_block_progress(self):
        kb = FakeKeyboard()
        s = LearnSession(kb, make_song([60]), accompany=False)
        s._activate(0)
        kb.tap(s, 61)
        kb.tap(s, 60)
        self.assertEqual(s.report.steps_completed, 1)
        self.assertEqual(s.report.wrong_attacks, 1)


class TestDjMode(unittest.TestCase):
    """The only way to make a wrong key sound like something else INSTEAD of
    piano: set the keybed to DJ Voice #98 so every key plays a sample, and let
    Plluminati supply the piano for correct notes only."""

    def test_correct_note_gets_the_piano(self):
        kb = FakeKeyboard()
        s = LearnSession(kb, make_song([60]), accompany=False, dj_mode=True)
        s._activate(0)
        kb.press(s, 60)
        self.assertIn((60,), kb.played, "we supply the piano for a right note")

    def test_wrong_note_gets_nothing_from_us(self):
        """Its DJ sample is the whole sound - we must not add to it."""
        kb = FakeKeyboard()
        s = LearnSession(kb, make_song([60]), accompany=False, dj_mode=True)
        s._activate(0)
        kb.press(s, 61)
        self.assertEqual(kb.played, [])
        self.assertEqual(kb.hits, [])

    def test_piano_stops_on_release(self):
        kb = FakeKeyboard()
        s = LearnSession(kb, make_song([60, 62]), accompany=False, dj_mode=True)
        s._activate(0)
        kb.press(s, 60)
        self.assertIn(60, kb.sounding)
        kb.release(s, 60)
        self.assertNotIn(60, kb.sounding)

    def test_drum_layer_suppressed_in_dj_mode(self):
        """Two wrong-note sounds at once would be a mess."""
        kb = FakeKeyboard()
        s = LearnSession(kb, make_song([60]), accompany=False, dj_mode=True,
                         wrong_sound="hihat")
        s._activate(0)
        kb.press(s, 61)
        self.assertEqual(kb.hits, [])


class TestRepeatedNoteStall(unittest.TestCase):
    """Steve's Lava Chicken is full of repeated Cs, which exposes this: after
    satisfying a step, the NEXT step often wants the same key. It cannot be lit
    while held (the light is already on, and re-triggering would cut the
    player's note), so the guidance looks stalled until they release."""

    def test_next_cue_waits_for_release_on_a_repeated_note(self):
        kb = FakeKeyboard()
        s = LearnSession(kb, make_song([60, 60]), accompany=False)
        hints = []
        s.on_hint = lambda kind, pitches: hints.append((kind, tuple(pitches)))
        s._activate(0)
        kb.press(s, 60)                       # satisfies step 0, still holding
        self.assertEqual(s.report.steps_completed, 1)
        self.assertEqual(hints[-1], ("release", (60,)),
                         "should tell them to let go, not show nothing")
        kb.release(s, 60)
        self.assertIn(60, kb.lit, "cue must appear the moment they release")

    def test_no_release_hint_for_a_different_note(self):
        kb = FakeKeyboard()
        s = LearnSession(kb, make_song([60, 62]), accompany=False)
        hints = []
        s.on_hint = lambda kind, pitches: hints.append(kind)
        s._activate(0)
        kb.press(s, 60)
        self.assertEqual(hints[-1], "", "a different key can just light up")
        self.assertIn(62, kb.lit)

    def test_cue_is_released_after_the_step(self):
        """The bookkeeping bug: pressing a key must not make us forget to turn
        its cue off, or it stays lit into the next step."""
        kb = FakeKeyboard()
        s = LearnSession(kb, make_song([60, 62]), accompany=False)
        s._activate(0)
        kb.tap(s, 60)                          # press AND release
        self.assertNotIn(60, kb.lit, "the finished cue must be extinguished")
        self.assertIn(62, kb.lit)


class TestWholeSong(unittest.TestCase):

    def test_plays_through_a_real_file(self):
        song = load(os.path.join(SONGS, "simple-piano.mid"))
        kb = FakeKeyboard()
        s = LearnSession(kb, song, hand="right", accompany=True)
        s._activate(0)
        guard = 0
        while s.current and guard < 200:
            guard += 1
            for p in s.current.pitches:
                if p in s._required:
                    kb.press(s, p)
            for p in list(kb.held):
                kb.release(s, p)
        self.assertEqual(s.report.steps_completed, len(s.steps))
        self.assertEqual(s.report.wrong_attacks, 0)
        self.assertEqual(s.report.clean_steps, len(s.steps))

    def test_skip_advances_and_is_recorded(self):
        kb = FakeKeyboard()
        s = LearnSession(kb, make_song([60, 62]), accompany=False)
        s._activate(0)
        s.skip()
        self.assertEqual(s.report.steps_skipped, 1)
        self.assertEqual(s.current.pitches, [62])
        self.assertEqual(s.report.clean_steps, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
