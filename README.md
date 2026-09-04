# Plluminati

A silent-light practice trainer for the **Yamaha EZ-150**.

It lights the next note or chord on the keyboard's own illuminated keys —
**silently** — waits for you to play it, and only then moves on. Later it will
drill one hand at a time, loop a section, and raise the tempo as you play clean.

The keyboard has had wait-mode lessons built in since 2003, but they only work
on the 100 songs in its ROM: MIDI is switched off entirely in Song mode. This
brings the same idea to **any MIDI file**.

## Status

**Feature complete for v1.** MIDI I/O, hardware probe, song model, hand
detection, scoring matcher, light-and-wait engine, play-along mode, the
performance-gated tempo ramp, the riff repeater, profiles, per-hand mastery and
the web UI — **140 tests passing, no dependencies**.

Written for one specific keyboard on one specific desk. Much of it generalises,
but the hardware quirks in §3 of the spec were measured, not assumed, and some
of them are peculiar to this model.

## How it works

Two facts about this keyboard, both measured rather than assumed, make the whole
thing possible:

| Bus | How | Effect |
| --- | --- | --- |
| **Cue** | channel 1, velocity 1 | lights a key, **makes no sound** |
| **Accompaniment** | channel 2, any velocity | **sounds**, lights nothing |

They are orthogonal, so the trainer can light the hand you're learning while
playing the other hand aloud — without ever playing you the answer.

## Requirements

Python 3.10+. **No dependencies** — MIDI goes straight to the ALSA rawmidi
character device, so nothing needs compiling.

## Usage

```bash
# the main way to use it: web UI on a screen behind the keyboard
python3 -m plluminati serve

# or from the terminal
python3 -m plluminati learn song.mid --hand right
python3 -m plluminati learn song.mid --hand left --bars 17-24
python3 -m plluminati info song.mid --steps 20     # what's in the file?

python3 -m plluminati play song.mid    # just listen to it, keys lighting up

# hardware
python3 -m plluminati devices          # find the cable, check the keyboard is on
python3 -m plluminati light C4 E4 G4   # light three keys, silently
python3 -m plluminati light C4 --sound # sound it instead (should light nothing)
python3 -m plluminati listen           # decode what the keyboard sends
python3 -m plluminati selftest         # guided hardware probe -> profile JSON
```

`learn` lights the notes for one hand, waits for you to play them, and plays the
other hand aloud as you go. It never advances without a fresh keypress, so a
repeated note really does need pressing again.

`selftest` is the one to run first on unfamiliar hardware, and the one to run
when lights misbehave. It measures the link, checks for MIDI echo, times the
output path, and — with you at the keyboard — reads the exact key range off the
keybed, maps which channels sound versus light, and checks whether cue lights
steal polyphony voices.

## Layout

```
plluminati/
  device.py      finding the rawmidi device (never hardcodes the card number)
  parser.py      MIDI byte-stream parser: running status, realtime bytes,
                 note-on velocity 0 as note-off
  port.py        one fd, one writer, priority queue, EAGAIN backpressure
  keyboard.py    the two buses, note ownership, keyboard-reset recovery
  session.py     open/close with guaranteed cleanup on every exit path
  smf.py         Standard MIDI File reader (hand-rolled: pip is unavailable here)
  song.py        notes, steps, bars, tempo and time maps
  hands.py       confidence-scored left/right detection
  matcher.py     hit/miss/wrong/timing scoring, and the tempo-ramp policy
  engine.py      the light-and-wait loop
  along.py       play-along mode: the clock, cue lead time, refcounted lights
  drill.py       the riff repeater - loop a section, let the tempo climb
  store.py       profiles and scores: atomic, UUID-keyed, versioned
  server.py      local HTTP + SSE for the UI (loopback only)
  web/           the UI itself
  wizard.py      on-screen hardware checks, answered on the piano
  selftest.py    the terminal version of the same probes
  playback.py    straight playback - listen without practising
  synth.py       optional laptop-side sound (FluidSynth via ctypes)
  cli.py         command line
tools/           test-MIDI generator
songs/test/      generated test files
tests/           unit tests for the pure-logic parts
```

## How practice works

**Learn mode** has no clock. It lights the next note, waits as long as you need,
and only advances on a genuinely fresh keypress — so a repeated note really does
need pressing again. The other hand plays aloud alongside you.

**Drill mode** is the riff repeater. Pick a section, and it loops at a reduced
speed while measuring what you actually played — hits, wrong attacks and timing
error, tracked separately. Play it clean and it speeds up a step; play it badly
and it drops back; in between it holds. Speed never dumps back to the start on
reaching the ceiling, which is the one thing Rocksmith gets wrong.

**Mastery rings** fill only on genuinely achieved clean speeds (40/60/80/90/100)
and are tracked per hand. Nothing is awarded for showing up.

**DJ mode** makes a wrong note sound like something else entirely rather than
piano. The keybed cannot be silenced over MIDI on this model, so instead you
select DJ Voice #98 (or Drum Kit #00) on the panel — every key then plays a
sample, "Uhh", "One More Time", a hand clap — and Plluminati supplies the piano
itself, on channel 2, only for correct notes. Wrong key: its noise, no piano.

## What is not in this repo

Some things stay on the machine that built this:

- **Design notes and the full spec** — working documents, including the
  measured hardware findings and a fair number of corrections to my own earlier
  assumptions. Useful to keep, nobody else's business. The parts worth knowing
  are in *Gotchas* below.
- **The Yamaha EZ-150 owner's manual** — Yamaha's copyright, and a free
  download from their site. The *facts* taken from it (the MIDI implementation
  chart, the built-in song list) are facts, and inform the code's comments.
- **Sheet music, and MIDI transcribed from it** — including the generator
  script that embeds a melody as pitch data, which is the composition however
  it is spelled.

`songs/test/` **is** included: those four files are generated by
`tools/make_test_midi.py` and the melody is *Twinkle Twinkle Little Star*, which
is traditional and out of copyright.

## Gotchas worth knowing

- **All Notes Off (CC123) does nothing on this keyboard** — nor do All Sound
  Off, Reset All Controllers or Local On/Off. Every note-on must be paired with
  an explicit note-off. That pairing is the only cleanup that works.
- **Only channels 1–7 and 10 exist.** Channels 8, 9 and 11–16 are ignored
  entirely, so silence from one proves nothing.
- **Playing a lit key extinguishes its light** on release, without the software
  sending anything. Cue bookkeeping is an intent log, not hardware state.
- **Leaving the attract display resets channel voices**, silently undoing the
  accompaniment setup. The reset is detectable and is re-applied automatically.
- **Incoming velocity is fixed but voice-dependent** — 84 on the default voice,
  not universally. Calibrate; never hardcode.

## Testing

```bash
python3 -m unittest discover -s tests -v
```

The parser tests run against real captured bytes, including the setup dump the
keyboard emits when it leaves the attract display.
