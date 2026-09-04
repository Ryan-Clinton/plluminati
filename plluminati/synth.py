"""Laptop-side sound, via FluidSynth through ctypes.

Why this exists: the EZ-150's keybed **cannot be silenced** (SPEC 3.2). Local
Control is unrecognised, and CC7 governs only received notes, so there is no
way to reproduce the built-in Lesson mode's "wrong notes make no sound".

The way around it is to move the sound off the keyboard entirely:

    turn the keyboard's volume dial down  ->  the keys are silent
    Plluminati plays the notes here       ->  only chosen notes are audible

which gives exactly the lesson feel, and a better piano into the bargain.

No pip install is involved. `libfluidsynth.so.3` is already present (a
PianoBooster dependency) and `/usr/share/sounds/sf2/FluidR3_GM.sf2` ships with
it, so this is a thin ctypes binding over a library that is already on disk.
Everything degrades gracefully: if the library or a soundfont is missing,
`available` is False and callers carry on silently.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import threading

SOUNDFONTS = [
    "/usr/share/sounds/sf2/FluidR3_GM.sf2",
    "/usr/share/sounds/sf2/default-GM.sf2",
    "/usr/share/soundfonts/default.sf2",
    "/usr/share/sounds/sf3/MuseScore_General.sf3",
]

#: Tried in order; the first that opens wins.
DRIVERS = [b"pulseaudio", b"pipewire", b"alsa", b"sdl3", b"file"]

GRAND_PIANO = 0

#: GM percussion lives on channel 10 (9 zero-based) with any GM soundfont.
DRUM_CHANNEL = 9

#: Short, dry hits for a wrong note. A sound beats silence: it tells a child
#: "not that one" without stopping them, and it never gets confused with the
#: piano because it is a different instrument entirely.
WRONG_SOUNDS = {
    "hihat":  42,   # closed hi-hat - short, muted "tss" (default)
    "stick":  37,   # side stick - dry click
    "clap":   39,   # hand clap
    "scratch": 30,  # scratch pull - DJ, if the soundfont maps it
    "thud":   41,   # low floor tom - soft thump
    "kick":   36,   # bass drum
}
DEFAULT_WRONG = "hihat"


def find_soundfont() -> str | None:
    for p in SOUNDFONTS:
        if os.path.exists(p):
            return p
    for base in ("/usr/share/sounds/sf2", "/usr/share/soundfonts"):
        if os.path.isdir(base):
            for fn in sorted(os.listdir(base)):
                if fn.lower().endswith((".sf2", ".sf3")):
                    return os.path.join(base, fn)
    return None


class Synth:
    """A small polyphonic voice living on the laptop."""

    def __init__(self, soundfont: str | None = None, gain: float = 0.8,
                 program: int = GRAND_PIANO) -> None:
        self.available = False
        self.reason = ""
        self._lock = threading.Lock()
        self._synth = None
        self._driver = None
        self._settings = None
        self._lib = None
        self.sounding: set[int] = set()

        sf = soundfont or find_soundfont()
        if sf is None:
            self.reason = "no soundfont found under /usr/share/sounds"
            return

        libname = ctypes.util.find_library("fluidsynth") or "libfluidsynth.so.3"
        try:
            lib = ctypes.CDLL(libname)
        except OSError as exc:
            self.reason = f"libfluidsynth not loadable ({exc})"
            return

        self._bind(lib)
        self._lib = lib

        settings = lib.new_fluid_settings()
        lib.fluid_settings_setnum(settings, b"synth.gain", ctypes.c_double(gain))
        lib.fluid_settings_setint(settings, b"synth.polyphony", 64)
        # Small buffers keep the echo close behind the key; the numbers are
        # conservative enough not to crackle on a laptop.
        lib.fluid_settings_setint(settings, b"audio.period-size", 256)
        lib.fluid_settings_setint(settings, b"audio.periods", 2)

        synth = lib.new_fluid_synth(settings)
        if lib.fluid_synth_sfload(synth, sf.encode(), 1) == -1:
            self.reason = f"could not load soundfont {sf}"
            return

        driver = None
        for drv in DRIVERS:
            lib.fluid_settings_setstr(settings, b"audio.driver", drv)
            driver = lib.new_fluid_audio_driver(settings, synth)
            if driver:
                self.driver_name = drv.decode()
                break
        if not driver:
            self.reason = "no working audio driver"
            return

        self._settings, self._synth, self._driver = settings, synth, driver
        self.soundfont = sf
        self.available = True
        self.program(program)

    # ------------------------------------------------------------- bindings

    @staticmethod
    def _bind(lib) -> None:
        vp, i, d, s = ctypes.c_void_p, ctypes.c_int, ctypes.c_double, ctypes.c_char_p
        lib.new_fluid_settings.restype = vp
        lib.new_fluid_synth.restype = vp
        lib.new_fluid_synth.argtypes = [vp]
        lib.new_fluid_audio_driver.restype = vp
        lib.new_fluid_audio_driver.argtypes = [vp, vp]
        lib.fluid_settings_setstr.argtypes = [vp, s, s]
        lib.fluid_settings_setnum.argtypes = [vp, s, d]
        lib.fluid_settings_setint.argtypes = [vp, s, i]
        lib.fluid_synth_sfload.argtypes = [vp, s, i]
        lib.fluid_synth_noteon.argtypes = [vp, i, i, i]
        lib.fluid_synth_noteoff.argtypes = [vp, i, i]
        lib.fluid_synth_program_change.argtypes = [vp, i, i]
        lib.fluid_synth_all_notes_off.argtypes = [vp, i]
        lib.delete_fluid_audio_driver.argtypes = [vp]
        lib.delete_fluid_synth.argtypes = [vp]
        lib.delete_fluid_settings.argtypes = [vp]

    # ---------------------------------------------------------------- notes

    def program(self, prog: int, channel: int = 0) -> None:
        if self.available:
            self._lib.fluid_synth_program_change(self._synth, channel, prog)

    def note_on(self, pitch: int, velocity: int = 90, channel: int = 0) -> None:
        if not self.available:
            return
        with self._lock:
            self._lib.fluid_synth_noteon(self._synth, channel, pitch, velocity)
            self.sounding.add(pitch)

    def note_off(self, pitch: int, channel: int = 0) -> None:
        if not self.available:
            return
        with self._lock:
            self._lib.fluid_synth_noteoff(self._synth, channel, pitch)
            self.sounding.discard(pitch)

    def hit(self, sound: str = DEFAULT_WRONG, velocity: int = 90) -> None:
        """One percussion hit - the 'not that key' sound.

        Drums are one-shots: the note-off goes out immediately and the sample
        rings out on its own, so nothing is left hanging and no bookkeeping is
        needed.
        """
        if not self.available:
            return
        note = WRONG_SOUNDS.get(sound, WRONG_SOUNDS[DEFAULT_WRONG])
        with self._lock:
            self._lib.fluid_synth_noteon(self._synth, DRUM_CHANNEL, note, velocity)
            self._lib.fluid_synth_noteoff(self._synth, DRUM_CHANNEL, note)

    def all_off(self, channel: int = 0) -> None:
        if not self.available:
            return
        with self._lock:
            self._lib.fluid_synth_all_notes_off(self._synth, channel)
            self.sounding.clear()

    def close(self) -> None:
        if not self.available:
            return
        self.all_off()
        self.available = False
        try:
            if self._driver:
                self._lib.delete_fluid_audio_driver(self._driver)
            if self._synth:
                self._lib.delete_fluid_synth(self._synth)
            if self._settings:
                self._lib.delete_fluid_settings(self._settings)
        except Exception:
            pass

    def __enter__(self) -> "Synth":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def describe(self) -> str:
        if not self.available:
            return f"laptop sound unavailable: {self.reason}"
        return (f"laptop sound via {self.driver_name}, "
                f"{os.path.basename(self.soundfont)}")
