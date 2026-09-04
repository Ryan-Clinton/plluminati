"""Persistence, play-along timing, and the drill loop."""

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plluminati.along import AlongSession                        # noqa: E402
from plluminati.drill import DrillSession                        # noqa: E402
from plluminati.matcher import RampPolicy                        # noqa: E402
from plluminati.song import load                                 # noqa: E402
from plluminati.store import (MASTERY_SPEEDS, Profile, Result,   # noqa: E402
                              Store, definition_fingerprint)

HERE = os.path.dirname(os.path.abspath(__file__))
SONGS = os.path.join(os.path.dirname(HERE), "songs", "test")


class FakePort:
    def flush(self, timeout=None):
        return True


class FakeKeyboard:
    def __init__(self):
        self.held = set()
        self.port = FakePort()
        self.lit = set()
        self.sounding = set()
        self.local_muted = False
        self.is_live = True
        self.cue_events = []

    def on_reset(self, cb):
        pass

    def cue_on(self, *notes):
        self.lit.update(notes)
        self.cue_events.append(("on", notes))

    def cue_off(self, *notes):
        self.lit.difference_update(notes)
        self.cue_events.append(("off", notes))

    def cues_clear(self):
        self.lit.clear()

    def play(self, *notes, velocity=80):
        self.sounding.update(notes)

    def stop(self, *notes):
        self.sounding.difference_update(notes)

    def stop_all(self):
        self.sounding.clear()

    def hit_wrong(self, sound="hihat", velocity=100):
        pass

    def echo(self, note, velocity=90):
        self.play(note, velocity=velocity)

    def set_local_mute(self, on):
        self.local_muted = on


class TestStore(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = Store(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_create_and_reload(self):
        p = self.store.create("Alice")
        again = Store(self.dir)
        self.assertEqual([x.display_name for x in again.all()], ["Alice"])

    def test_file_is_named_by_uuid_not_display_name(self):
        p = self.store.create("Bobby Tables/../evil")
        files = os.listdir(os.path.join(self.dir, "profiles"))
        self.assertEqual(files, [f"{p.id}.json"])
        self.assertNotIn("evil", files[0])

    def test_rename_keeps_scores(self):
        p = self.store.create("Al")
        p.record("fp1", "Song", Result(hand="right", clean=True, speed=0.8))
        self.store.save(p)
        self.store.rename(p.id, "Alice")
        again = Store(self.dir).all()[0]
        self.assertEqual(again.display_name, "Alice")
        self.assertEqual(again.mastery("fp1", "right"), 3)

    def test_distinct_avatars_and_themes(self):
        a = self.store.create("A")
        b = self.store.create("B")
        self.assertNotEqual(a.avatar, b.avatar)
        self.assertNotEqual(a.theme, b.theme)

    def test_mastery_counts_only_clean_speeds(self):
        p = self.store.create("A")
        p.record("fp", "S", Result(hand="right", clean=False, speed=1.0))
        self.assertEqual(p.mastery("fp", "right"), 0,
                         "a non-clean run earns nothing, however fast")
        p.record("fp", "S", Result(hand="right", clean=True, speed=0.6))
        self.assertEqual(p.mastery("fp", "right"), 2)

    def test_mastery_is_per_hand(self):
        p = self.store.create("A")
        p.record("fp", "S", Result(hand="right", clean=True, speed=1.0))
        self.assertEqual(p.mastery("fp", "right"), len(MASTERY_SPEEDS))
        self.assertEqual(p.mastery("fp", "left"), 0)

    def test_best_clean_speed_never_regresses(self):
        p = self.store.create("A")
        p.record("fp", "S", Result(hand="right", clean=True, speed=0.9))
        p.record("fp", "S", Result(hand="right", clean=True, speed=0.5))
        self.assertAlmostEqual(
            p.songs["fp"]["hands"]["right"]["best_clean_speed"], 0.9)

    def test_continue_state(self):
        p = self.store.create("A")
        p.record("fp", "S", Result(hand="left", speed=0.7, mode="along"))
        c = p.continue_state("fp")
        self.assertEqual(c["hand"], "left")
        self.assertAlmostEqual(c["speed"], 0.7)

    def test_history_is_capped(self):
        p = self.store.create("A")
        for i in range(30):
            p.record("fp", "S", Result(hand="right", speed=0.5))
        self.assertLessEqual(len(p.songs["fp"]["hands"]["right"]["history"]), 20)

    def test_atomic_write_leaves_no_temp_files(self):
        p = self.store.create("A")
        self.store.save(p)
        leftovers = [f for f in os.listdir(os.path.join(self.dir, "profiles"))
                     if f.endswith(".tmp") or ".tmp" in f]
        self.assertEqual(leftovers, [])

    def test_definition_fingerprint_changes_with_hand(self):
        song = load(os.path.join(SONGS, "simple-piano.mid"))
        a = definition_fingerprint(song, "right")
        b = definition_fingerprint(song, "left")
        self.assertNotEqual(a, b)

    def test_definition_fingerprint_changes_with_section(self):
        song = load(os.path.join(SONGS, "simple-piano.mid"))
        a = definition_fingerprint(song, "right", None)
        b = definition_fingerprint(song, "right", (1, 2))
        self.assertNotEqual(a, b)


class TestAlong(unittest.TestCase):

    def setUp(self):
        self.song = load(os.path.join(SONGS, "simple-piano.mid"))
        self.kb = FakeKeyboard()

    def test_targets_are_stretched_by_speed(self):
        fast = AlongSession(self.kb, self.song, speed=1.0)
        slow = AlongSession(self.kb, self.song, speed=0.5)
        self.assertAlmostEqual(max(t.time for t in slow.targets),
                               max(t.time for t in fast.targets) * 2, places=5)

    def test_targets_start_at_zero(self):
        s = AlongSession(self.kb, self.song, speed=0.6)
        self.assertAlmostEqual(min(t.time for t in s.targets), 0.0, places=6)

    def test_lead_scales_with_speed(self):
        fast = AlongSession(self.kb, self.song, speed=1.0)
        slow = AlongSession(self.kb, self.song, speed=0.5)
        self.assertAlmostEqual(slow._lead, fast._lead * 2, places=6)

    def test_out_of_range_targets_dropped(self):
        song = load(os.path.join(SONGS, "edge-cases.mid"))
        s = AlongSession(self.kb, song, hand="both", speed=1.0)
        self.assertTrue(all(36 <= t.pitch <= 96 for t in s.targets))

    def test_cue_refcount_survives_duplicate_pitches(self):
        """Two targets on one key: the first release must not blind the second."""
        s = AlongSession(self.kb, self.song, speed=1.0)
        s._cue_on(60)
        s._cue_on(60)
        self.assertIn(60, self.kb.lit)
        s._cue_off(60)
        self.assertIn(60, self.kb.lit, "still wanted by the second target")
        s._cue_off(60)
        self.assertNotIn(60, self.kb.lit)

    def test_all_cues_off_clears_refcounts(self):
        s = AlongSession(self.kb, self.song, speed=1.0)
        s._cue_on(60); s._cue_on(60); s._cue_on(64)
        s._all_cues_off()
        self.assertEqual(self.kb.lit, set())

    def test_run_scores_a_silent_player_as_all_missed(self):
        song = load(os.path.join(SONGS, "simple-piano.mid"))
        s = AlongSession(self.kb, song, hand="right", speed=1.0,
                         steps=song.steps_for_hand("right")[:2],
                         count_in=False, accompany=False)
        rep = s.run()
        self.assertEqual(rep.score.hits, 0)
        self.assertGreater(rep.score.missed, 0)
        self.assertEqual(rep.verdict, "poor")

    def test_lights_do_not_outlast_the_run(self):
        song = load(os.path.join(SONGS, "simple-piano.mid"))
        s = AlongSession(self.kb, song, hand="right", speed=1.0,
                         steps=song.steps_for_hand("right")[:2],
                         count_in=False, accompany=False)
        s.run()
        self.assertEqual(self.kb.lit, set())
        self.assertEqual(self.kb.sounding, set())


class TestDrill(unittest.TestCase):

    def setUp(self):
        self.song = load(os.path.join(SONGS, "simple-piano.mid"))
        self.kb = FakeKeyboard()

    def test_section_selected_by_bars(self):
        d = DrillSession(self.kb, self.song, bars=(1, 1))
        self.assertTrue(all(s.bar == 1 for s in d.steps))
        self.assertLess(len(d.steps), len(self.song.steps_for_hand("right")))

    def test_section_persisted_as_ticks_not_indexes(self):
        d = DrillSession(self.kb, self.song, bars=(1, 2))
        ticks = d.section_ticks
        self.assertIsNotNone(ticks)
        self.assertEqual(ticks[0], d.steps[0].tick)

    def test_speed_clamped_into_policy_range(self):
        d = DrillSession(self.kb, self.song, speed=5.0)
        self.assertLessEqual(d.speed, RampPolicy().ceiling)
        d2 = DrillSession(self.kb, self.song, speed=0.01)
        self.assertGreaterEqual(d2.speed, RampPolicy().floor)

    def test_poor_play_drops_speed_and_stops_at_floor(self):
        """Nobody plays: every rep is 'poor', so speed falls to the floor and
        stays there rather than going negative."""
        d = DrillSession(self.kb, self.song, bars=(1, 1), speed=0.6,
                         max_reps=6, accompany=False, count_in=False, gap=0.0)
        d.run()
        self.assertEqual(d.report.reps, 6)
        self.assertAlmostEqual(d.report.speed, RampPolicy().floor)

    def test_stop_ends_the_loop(self):
        d = DrillSession(self.kb, self.song, bars=(1, 1), speed=1.0,
                         max_reps=0, accompany=False, count_in=False, gap=0.0)
        import threading
        threading.Timer(0.4, d.stop).start()
        rep = d.run()
        self.assertGreaterEqual(rep.reps, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
