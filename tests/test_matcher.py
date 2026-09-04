"""Matcher and ramp policy.

Deliberately heavy on the cases SPEC 11 M3 calls out: repeated same-pitch
targets, overlapping windows, rolled chords, early/late attacks, duplicate
pitches, wrong notes immediately before correct ones, and exact boundaries.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plluminati.matcher import (Judgement, Matcher, RampPolicy,  # noqa: E402
                               Score, Target)


def T(i, pitch, time, step=0):
    return Target(id=i, pitch=pitch, time=time, step=step)


class TestMatching(unittest.TestCase):

    def test_exact_hit(self):
        m = Matcher([T(0, 60, 1.0)])
        v = m.attack(60, 1.0)
        self.assertIs(v.judgement, Judgement.HIT)
        self.assertAlmostEqual(v.timing_error, 0.0)

    def test_late_within_window(self):
        m = Matcher([T(0, 60, 1.0)], tolerance=0.15)
        v = m.attack(60, 1.10)
        self.assertTrue(v.ok)
        self.assertAlmostEqual(v.timing_error, 0.10, places=6)

    def test_outside_window_is_wrong_not_hit(self):
        """Right pitch, bad rhythm, must NOT count as a hit - otherwise a
        player could trigger the ramp with terrible timing."""
        m = Matcher([T(0, 60, 1.0)], tolerance=0.15)
        v = m.attack(60, 1.40)
        self.assertIs(v.judgement, Judgement.WRONG)
        self.assertEqual(m.score().hits, 0)
        self.assertEqual(m.score().missed, 1)

    def test_boundary_is_inclusive(self):
        m = Matcher([T(0, 60, 1.0)], tolerance=0.15)
        self.assertTrue(m.attack(60, 1.15).ok)

    def test_just_outside_boundary(self):
        m = Matcher([T(0, 60, 1.0)], tolerance=0.15)
        self.assertFalse(m.attack(60, 1.1500001).ok)

    def test_wrong_pitch(self):
        m = Matcher([T(0, 60, 1.0)])
        self.assertIs(m.attack(61, 1.0).judgement, Judgement.WRONG)

    def test_earliest_outstanding_wins_not_nearest(self):
        """THE ambiguity that 'greedy nearest' left open.

        Two C4 targets 0.2s apart, windows overlapping. An attack at 1.12 is
        nearer the second target, but the first is still outstanding, so the
        first must claim it.
        """
        m = Matcher([T(0, 60, 1.0), T(1, 60, 1.2)], tolerance=0.15)
        v = m.attack(60, 1.12)
        self.assertEqual(v.target_id, 0, "should claim the EARLIEST outstanding")

    def test_second_attack_takes_second_target(self):
        m = Matcher([T(0, 60, 1.0), T(1, 60, 1.2)], tolerance=0.15)
        m.attack(60, 1.02)
        v = m.attack(60, 1.18)
        self.assertEqual(v.target_id, 1)
        self.assertEqual(m.score().hits, 2)

    def test_repeated_pitch_four_times(self):
        targets = [T(i, 60, 1.0 + i * 0.5, step=i) for i in range(4)]
        m = Matcher(targets, tolerance=0.15)
        for i in range(4):
            self.assertTrue(m.attack(60, 1.0 + i * 0.5).ok)
        s = m.score()
        self.assertEqual((s.hits, s.missed, s.wrong_attacks), (4, 0, 0))

    def test_one_attack_cannot_satisfy_two_targets(self):
        m = Matcher([T(0, 60, 1.0), T(1, 60, 1.05)], tolerance=0.15)
        m.attack(60, 1.02)
        s = m.score()
        self.assertEqual(s.hits, 1)
        self.assertEqual(s.missed, 1, "one keypress must not cover both")

    def test_extra_same_pitch_is_duplicate_not_wrong_pitch(self):
        m = Matcher([T(0, 60, 1.0)], tolerance=0.15)
        m.attack(60, 1.0)
        v = m.attack(60, 1.05)
        self.assertIs(v.judgement, Judgement.DUPLICATE)
        self.assertEqual(m.score().wrong_attacks, 1)

    def test_wrong_note_before_correct_one(self):
        m = Matcher([T(0, 60, 1.0)], tolerance=0.15)
        self.assertFalse(m.attack(59, 0.95).ok)
        self.assertTrue(m.attack(60, 1.0).ok)
        s = m.score()
        self.assertEqual((s.hits, s.wrong_attacks), (1, 1))


class TestChords(unittest.TestCase):

    def test_block_chord_all_hit(self):
        targets = [T(i, p, 1.0, step=0) for i, p in enumerate((60, 64, 67))]
        m = Matcher(targets, tolerance=0.15)
        for p in (60, 64, 67):
            self.assertTrue(m.attack(p, 1.0).ok)
        s = m.score()
        self.assertEqual(s.hits, 3)
        self.assertAlmostEqual(s.chord_spreads[0], 0.0)

    def test_rolled_chord_notes_still_hits(self):
        """Spread failure is a SEPARATE criterion - the notes remain hits.

        'All the right notes, too rolled' is a better diagnosis than calling
        them misses.
        """
        targets = [T(i, p, 1.0, step=0) for i, p in enumerate((60, 64, 67))]
        m = Matcher(targets, tolerance=0.15, chord_spread=0.08)
        m.attack(60, 0.95)
        m.attack(64, 1.02)
        m.attack(67, 1.09)
        s = m.score()
        self.assertEqual(s.hits, 3, "rolled notes must still count as hits")
        self.assertGreater(s.chord_spreads[0], 0.08, "spread should be flagged")
        self.assertEqual(s.missed, 0)

    def test_duplicate_pitch_two_sources_one_key(self):
        """Same pitch from two tracks: two targets, but a player has one finger."""
        m = Matcher([T(0, 60, 1.0, step=0), T(1, 60, 1.0, step=0)], tolerance=0.15)
        m.attack(60, 1.0)
        s = m.score()
        self.assertEqual(s.hits, 1)
        self.assertEqual(s.missed, 1)


class TestScore(unittest.TestCase):

    def test_recall_and_wrong_rate(self):
        targets = [T(i, 60 + i, 1.0 + i) for i in range(4)]
        m = Matcher(targets, tolerance=0.15)
        m.attack(60, 1.0)
        m.attack(61, 2.0)
        m.attack(99, 2.5)
        s = m.score()
        self.assertEqual(s.hits, 2)
        self.assertEqual(s.missed, 2)
        self.assertEqual(s.wrong_attacks, 1)
        self.assertAlmostEqual(s.recall, 0.5)
        self.assertAlmostEqual(s.wrong_rate, 1 / 3)

    def test_signed_timing_detects_rushing(self):
        targets = [T(i, 60, 1.0 + i, step=i) for i in range(3)]
        m = Matcher(targets, tolerance=0.15)
        for i in range(3):
            m.attack(60, 1.0 + i - 0.05)
        s = m.score()
        self.assertLess(s.mean_timing, 0, "consistently early should read negative")
        self.assertAlmostEqual(s.mean_abs_timing, 0.05, places=6)

    def test_empty_score_is_safe(self):
        s = Score()
        self.assertEqual(s.recall, 0.0)
        self.assertEqual(s.wrong_rate, 0.0)
        self.assertEqual(s.mean_abs_timing, 0.0)

    def test_speed_scales_the_window(self):
        """At half speed everything takes twice as long; the window follows."""
        fast = Matcher([T(0, 60, 1.0)], tolerance=0.15, speed=1.0)
        slow = Matcher([T(0, 60, 1.0)], tolerance=0.15, speed=0.5)
        self.assertAlmostEqual(slow.tolerance, 0.30, places=6)
        self.assertFalse(fast.attack(60, 1.25).ok)
        self.assertTrue(slow.attack(60, 1.25).ok)


class TestRampPolicy(unittest.TestCase):

    def _score(self, targets, hits, wrong=0, timing=0.0):
        s = Score(targets=targets, hits=hits, missed=targets - hits,
                  wrong_attacks=wrong)
        s.timing_errors = [timing] * hits
        return s

    def test_clean_speeds_up(self):
        p = RampPolicy()
        s = self._score(100, 100, wrong=0, timing=0.02)
        speed, verdict = p.next_speed(0.6, s)
        self.assertEqual(verdict, "clean")
        self.assertAlmostEqual(speed, 0.7)

    def test_poor_slows_down(self):
        p = RampPolicy()
        s = self._score(100, 50)
        speed, verdict = p.next_speed(0.6, s)
        self.assertEqual(verdict, "poor")
        self.assertAlmostEqual(speed, 0.5)

    def test_middle_band_holds(self):
        """The deliberate neutral band - neither clean nor poor."""
        p = RampPolicy()
        s = self._score(100, 85, wrong=5, timing=0.05)
        speed, verdict = p.next_speed(0.6, s)
        self.assertEqual(verdict, "hold")
        self.assertAlmostEqual(speed, 0.6)

    def test_good_notes_bad_rhythm_is_not_clean(self):
        """Every pitch right but sloppy timing must NOT trigger the ramp."""
        p = RampPolicy()
        s = self._score(100, 100, wrong=0, timing=0.30)
        self.assertNotEqual(p.classify(s), "clean")

    def test_never_below_floor(self):
        p = RampPolicy()
        s = self._score(100, 10)
        speed, _ = p.next_speed(0.40, s)
        self.assertAlmostEqual(speed, 0.40)

    def test_never_above_ceiling(self):
        p = RampPolicy()
        s = self._score(100, 100, timing=0.01)
        speed, _ = p.next_speed(1.0, s)
        self.assertAlmostEqual(speed, 1.0)

    def test_speed_is_monotonic_at_ceiling(self):
        """Rocksmith's annoyance: hitting max must NOT dump you back to the start."""
        p = RampPolicy()
        s = self._score(100, 100, timing=0.01)
        speed = 0.9
        for _ in range(5):
            speed, _ = p.next_speed(speed, s)
        self.assertAlmostEqual(speed, 1.0)

    def test_classes_are_disjoint(self):
        p = RampPolicy()
        for hits, wrong, timing in [(100, 0, 0.01), (85, 5, 0.05), (50, 20, 0.4),
                                    (95, 10, 0.11), (70, 0, 0.0)]:
            verdict = p.classify(self._score(100, hits, wrong, timing))
            self.assertIn(verdict, ("clean", "hold", "poor"))


class TestAgainstRealSong(unittest.TestCase):

    def test_perfect_play_of_real_file(self):
        from plluminati.matcher import targets_from_steps
        from plluminati.song import load
        here = os.path.dirname(os.path.abspath(__file__))
        song = load(os.path.join(os.path.dirname(here), "songs", "test",
                                 "simple-piano.mid"))
        targets = targets_from_steps(song.steps)
        m = Matcher(targets, tolerance=0.15)
        for t in targets:
            m.attack(t.pitch, t.time)
        s = m.score()
        self.assertEqual(s.missed, 0)
        self.assertEqual(s.wrong_attacks, 0)
        self.assertAlmostEqual(s.recall, 1.0)
        self.assertEqual(RampPolicy().classify(s), "clean")

    def test_one_hand_only(self):
        from plluminati.matcher import targets_from_steps
        from plluminati.song import load
        here = os.path.dirname(os.path.abspath(__file__))
        song = load(os.path.join(os.path.dirname(here), "songs", "test",
                                 "simple-piano.mid"))
        right = targets_from_steps(song.steps, hand="right")
        self.assertTrue(right)
        self.assertTrue(all(t.hand == "right" for t in right))
        self.assertLess(len(right), len(targets_from_steps(song.steps)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
