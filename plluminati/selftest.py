"""Guided hardware probe - milestone M1.1.

This replaces the manual M0 battery from the spec. Every hardware question that
used to need ad-hoc scripts and someone staring at keys is answered here, in one
command, with the results written to a machine-readable profile.

Design note: wherever possible the probe uses THE KEYBOARD ITSELF as the answer
device. Asking "press the lowest key" and reading the note number is exact,
whereas asking someone to eyeball which key lit is slow and error-prone. Only
questions about sound and light - which the software genuinely cannot observe -
fall back to a yes/no prompt.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import threading
import time
from dataclasses import dataclass, field, asdict

from .keyboard import (CUE_CHANNEL, CUE_VELOCITY, POLYPHONY_LIMIT,
                       USABLE_CHANNELS, Keyboard, _status)
from .parser import Kind, Message
from .port import Priority

NOTE_ON = 0x90
NOTE_OFF = 0x80
PROGRAM_CHANGE = 0xC0

#: Channel 12 is not recognised by the EZ-150, so traffic on it exercises the
#: full write path with no light and no sound - ideal for timing measurement.
INERT_CHANNEL = 12


@dataclass
class Result:
    name: str
    title: str
    status: str = "unknown"        # pass | fail | unknown | skipped
    detail: str = ""
    data: dict = field(default_factory=dict)


class Collector:
    """Thread-safe sink for inbound messages, with waits."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.messages: list[Message] = []
        self.active_sensing = 0
        self._event = threading.Event()

    def __call__(self, msg: Message) -> None:
        if msg.is_active_sensing:
            with self._lock:
                self.active_sensing += 1
            return
        with self._lock:
            self.messages.append(msg)
        self._event.set()

    def clear(self) -> None:
        with self._lock:
            self.messages.clear()
        self._event.clear()

    def snapshot(self) -> list[Message]:
        with self._lock:
            return list(self.messages)

    def wait_note_on(self, timeout: float = 30.0) -> Message | None:
        """Block until the player presses a key (ignoring our own vel-1 cues)."""
        deadline = time.monotonic() + timeout
        seen = 0
        while time.monotonic() < deadline:
            msgs = self.snapshot()
            for m in msgs[seen:]:
                if m.kind is Kind.NOTE_ON and m.velocity != CUE_VELOCITY:
                    return m
            seen = len(msgs)
            self._event.wait(0.05)
            self._event.clear()
        return None

    def wait_quiet(self, seconds: float = 0.7) -> None:
        """Wait until nothing has arrived for `seconds` - i.e. keys released."""
        last = len(self.snapshot())
        stable = time.monotonic()
        while time.monotonic() - stable < seconds:
            time.sleep(0.05)
            now = len(self.snapshot())
            if now != last:
                last, stable = now, time.monotonic()


# ---------------------------------------------------------------- prompting

MIDDLE_C = 60


class StdinAsker:
    """Type the answer. Used when there is a terminal to type into."""

    def ask(self, question: str) -> bool | None:
        while True:
            try:
                raw = input(f"    {question} [y/n/s=skip] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return None
            if raw in ("y", "yes"):
                return True
            if raw in ("n", "no"):
                return False
            if raw in ("s", "skip", ""):
                return None


class KeyboardAsker:
    """Answer on the piano itself.

    Your hands are already on the keys and the screen is behind the keyboard,
    so reaching for a laptop is the wrong gesture. It also means the probe
    works with no terminal at all - which is how it runs under automation.
    """

    def __init__(self, collector: "Collector", timeout: float = 60.0) -> None:
        self.col = collector
        self.timeout = timeout

    def ask(self, question: str) -> bool | None:
        print(f"    {question}")
        print("      -> a key BELOW middle C = YES,  ABOVE = NO,  middle C = skip")
        self.col.clear()
        msg = self.col.wait_note_on(self.timeout)
        if msg is None:
            print("      (no answer - skipped)")
            return None
        self.col.wait_quiet(0.4)
        if msg.note < MIDDLE_C:
            print(f"      YES (note {msg.note})")
            return True
        if msg.note > MIDDLE_C:
            print(f"      NO (note {msg.note})")
            return False
        print("      skipped (middle C)")
        return None


#: Set by run(); probes call ask() without caring which one is in use.
_ASKER: object = StdinAsker()


def ask(question: str) -> bool | None:
    return _ASKER.ask(question)


def banner(n: int, total: int, title: str) -> None:
    print(f"\n[{n}/{total}] {title}")
    print("    " + "-" * (len(title) + 4))


# ------------------------------------------------------------------- probes

def probe_link(kb: Keyboard, col: Collector) -> Result:
    r = Result("link", "Link and power")
    col.clear()
    time.sleep(1.2)
    seen = col.active_sensing
    r.data = {"active_sensing_per_sec": round(seen / 1.2, 1),
              "device": kb.port.device.path,
              "alsa_id": kb.port.device.alsa_id}
    if seen:
        r.status = "pass"
        r.detail = (f"keyboard is ON - {seen} Active Sensing keepalives in 1.2s "
                    f"(~{seen/1.2:.0f}/sec)")
    else:
        r.status = "fail"
        r.detail = ("no Active Sensing - keyboard is OFF, or the DIN plugs are "
                    "swapped (cable OUT must go to keyboard IN)")
    return r


def probe_liveness(kb: Keyboard, col: Collector) -> Result:
    """Active Sensing silence detects the attract display.

    Confirmed 2026-08-14: when the EZ-150 drops into its idle light show it
    STOPS sending Active Sensing, and ignores cue lights while it does. That
    makes the attract display detectable - earlier drafts of the spec wrongly
    called it invisible.
    """
    r = Result("liveness", "Attract-display detection")
    live = kb.is_live
    r.status = "pass"
    r.data = {"live_now": live,
              "seconds_since_keepalive": round(time.monotonic() - kb._last_sensing, 2)}
    r.detail = ("keepalive flowing - keyboard awake and accepting lights" if live
                else "NO keepalive - asleep (attract display) or off; lights "
                     "will not land until a key is pressed")
    return r


def probe_echo(kb: Keyboard, col: Collector) -> Result:
    """Does MIDI IN get mirrored to MIDI OUT? If so, our own cues come back."""
    r = Result("echo", "MIDI echo")
    col.clear()
    kb.cue_on(60)
    kb.port.flush()
    time.sleep(0.6)
    echoed = [m for m in col.snapshot()
              if m.kind is Kind.NOTE_ON and m.velocity == CUE_VELOCITY]
    kb.cue_off(60)
    kb.port.flush()
    r.data = {"echoed_messages": len(echoed)}
    if echoed:
        r.status = "fail"
        r.detail = f"{len(echoed)} cue(s) echoed back - input must filter velocity 1"
    else:
        r.status = "pass"
        r.detail = "no echo - inbound notes are always genuine playing"
    return r


def probe_latency(kb: Keyboard, col: Collector) -> Result:
    """Output scheduling jitter, measured on a channel the keyboard ignores."""
    r = Result("latency", "Output timing")
    noop = bytes([_status(NOTE_ON, INERT_CHANNEL), 60, 1])

    durs = []
    for _ in range(200):
        a = time.perf_counter()
        kb.port.send(noop, Priority.COSMETIC)
        kb.port.flush(1.0)
        durs.append((time.perf_counter() - a) * 1000)

    n, interval = 100, 0.02
    t0 = time.monotonic()
    errs = []
    for i in range(n):
        target = t0 + i * interval
        while True:
            rem = target - time.monotonic()
            if rem <= 0:
                break
            time.sleep(rem / 2 if rem > 0.002 else 0)
        kb.port.send(noop, Priority.COSMETIC)
        errs.append((time.monotonic() - target) * 1000)
    kb.port.flush(2.0)

    durs.sort()
    errs.sort()
    r.status = "pass"
    r.data = {
        "send_ms_median": round(statistics.median(durs), 3),
        "send_ms_p95": round(durs[int(0.95 * len(durs))], 3),
        "jitter_ms_median": round(statistics.median(errs), 3),
        "jitter_ms_max": round(errs[-1], 3),
        "eagain_retries": kb.port.eagain_retries,
        "wire_ms_per_3byte_msg": 0.96,
    }
    r.detail = (f"send median {r.data['send_ms_median']:.3f} ms, "
                f"jitter median {r.data['jitter_ms_median']:+.3f} ms, "
                f"max {r.data['jitter_ms_max']:+.3f} ms "
                f"({kb.port.eagain_retries} EAGAIN retries)")
    return r


def require_live(kb: Keyboard) -> bool:
    """Attract display / power-off both silence Active Sensing, and both stop
    cue lights landing. Prompt rather than fail mysteriously (SPEC 3.4)."""
    return kb.wait_until_live(
        timeout=90,
        announce=lambda: print(
            "    !! keyboard is asleep (idle attract display) or switched off.\n"
            "       Press ANY key on it to wake it - waiting..."))


def probe_keyboard_range(kb: Keyboard, col: Collector,
                         duration: float = 25.0) -> Result:
    """Exact playable range, read straight off the keybed.

    Deliberately order-independent: it listens for a while and takes the
    extremes of whatever arrives. Sequential "now press the lowest key"
    prompting needs the player to see the screen at the right moment, which is
    exactly what this product cannot assume - their eyes are on their hands.
    A glissando answers it in one gesture.
    """
    r = Result("keyboard_range", "Physical key range")
    if not require_live(kb):
        r.status = "skipped"
        r.detail = "keyboard never woke up"
        return r

    col.clear()
    print(f"    Play the LOWEST and HIGHEST keys - or sweep a finger across")
    print(f"    the whole keyboard. Listening for {duration:.0f}s...")
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        time.sleep(0.2)
        notes = {m.note for m in col.snapshot()
                 if m.kind is Kind.NOTE_ON and m.velocity != CUE_VELOCITY}
        if len(notes) >= 2 and (max(notes) - min(notes)) >= 36:
            break        # three octaves apart: they have clearly swept it

    played = [m for m in col.snapshot()
              if m.kind is Kind.NOTE_ON and m.velocity != CUE_VELOCITY]
    if not played:
        r.status = "skipped"
        r.detail = "no keys played"
        return r

    notes = sorted({m.note for m in played})
    vels = sorted({m.velocity for m in played})
    lo, hi = notes[0], notes[-1]
    r.status = "pass"
    r.data = {"lowest_note": lo, "highest_note": hi,
              "span_semitones": hi - lo, "distinct_notes": len(notes),
              "velocities_seen": vels, "player_velocity": vels[0]}
    r.detail = (f"MIDI {lo}-{hi} ({hi - lo + 1} keys if contiguous), "
                f"{len(notes)} distinct notes played, "
                f"velocity {vels if len(vels) > 1 else vels[0]}")
    if len(vels) > 1:
        r.detail += "  <- velocity VARIES: keybed may be touch-sensitive after all"
    return r


def probe_light_map(kb: Keyboard, col: Collector,
                     duration: float = 40.0) -> Result:
    """Which keys can actually be LIT?

    Uses the keyboard as its own display AND its own answer device: light a
    spread of candidates, ask the player to press every key that is lit, and
    read back which notes arrive. Pressing a lit key extinguishes it, so the
    player gets natural feedback on what they have already reported.
    """
    r = Result("light_map", "Lightable range map")
    if not require_live(kb):
        r.status = "skipped"
        r.detail = "keyboard asleep"
        return r

    candidates = list(range(24, 109, 12))      # C1..C8, one per octave
    kb.cue_on(*candidates)
    kb.port.flush()
    col.clear()
    print(f"    Lit {len(candidates)} candidate keys, one per octave "
          f"({candidates[0]}-{candidates[-1]}).")
    print(f"    PRESS EVERY KEY THAT IS LIT. Listening {duration:.0f}s...")

    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        time.sleep(0.3)
        pressed = {m.note for m in col.snapshot()
                   if m.kind is Kind.NOTE_ON and m.velocity != CUE_VELOCITY}
        if len(pressed & set(candidates)) >= 5:
            break

    pressed = sorted({m.note for m in col.snapshot()
                      if m.kind is Kind.NOTE_ON and m.velocity != CUE_VELOCITY})
    kb.cue_off(*candidates)
    kb.port.flush()

    lit = sorted(set(pressed) & set(candidates))
    if not lit:
        r.status = "unknown"
        r.detail = "no keys reported as lit"
        r.data = {"candidates": candidates, "pressed": pressed}
        return r

    r.status = "pass"
    r.data = {"candidates": candidates, "pressed": pressed, "lit": lit,
              "lightable_low": lit[0], "lightable_high": lit[-1]}
    dark = [c for c in candidates if c not in lit]
    r.detail = f"lit octaves {lit}; not lit {dark}"
    return r


def probe_light_range(kb: Keyboard, col: Collector, rng: Result) -> Result:
    """Can the extremes actually be LIT, or only played?"""
    r = Result("light_range", "Lightable range")
    if rng.status != "pass":
        r.status = "skipped"
        r.detail = "needs the physical range first"
        return r
    lo, hi = rng.data["lowest_note"], rng.data["highest_note"]
    kb.cue_on(lo, hi)
    kb.port.flush()
    print(f"    Lit the two extreme keys ({lo} and {hi}), silently.")
    got = ask("Are BOTH end keys lit?")
    kb.cue_off(lo, hi)
    kb.port.flush()
    if got is None:
        r.status = "skipped"
    else:
        r.status = "pass" if got else "fail"
        r.data = {"extremes_lightable": got, "lowest": lo, "highest": hi}
        r.detail = ("full range is lightable" if got else
                    "extremes did NOT light - lightable range is narrower than playable")
    return r


def probe_channel_matrix(kb: Keyboard, col: Collector) -> Result:
    """sound x light for every channel the EZ-150 recognises."""
    r = Result("channel_matrix", "Channel sound x light matrix")
    if not require_live(kb):
        r.status = "skipped"
        r.detail = "keyboard asleep"
        return r
    matrix = {}
    print("    For each channel: does it SOUND, and does it LIGHT a key?")
    for ch in USABLE_CHANNELS:
        note = 60
        kb.port.send(bytes([_status(PROGRAM_CHANGE, ch), 0]), Priority.CRITICAL)
        kb.port.send(bytes([_status(NOTE_ON, ch), note, 80]), Priority.MUSIC)
        kb.port.flush()
        print(f"\n    channel {ch}: playing middle C at velocity 80")
        sounded = ask(f"channel {ch} - did you HEAR it?")
        lit = ask(f"channel {ch} - did a key LIGHT?")
        kb.port.send(bytes([_status(NOTE_OFF, ch), note, 0]), Priority.CRITICAL)
        kb.port.flush()
        matrix[ch] = {"sounds": sounded, "lights": lit}
        if sounded is None and lit is None:
            break
    r.data = {"matrix": matrix}
    r.status = "pass" if matrix else "skipped"
    silent_non_lighting = [c for c, v in matrix.items()
                           if v.get("sounds") and v.get("lights") is False]
    r.data["accompaniment_candidates"] = silent_non_lighting
    r.detail = (f"channels that sound WITHOUT lighting: {silent_non_lighting or 'none'}")
    return r


def probe_polyphony(kb: Keyboard, col: Collector) -> Result:
    """Do velocity-1 cues consume voices from the 16-note budget?"""
    r = Result("polyphony", "Polyphony budget")
    if not require_live(kb):
        r.status = "skipped"
        r.detail = "keyboard asleep"
        return r
    base = 48
    chord = [base + i for i in range(POLYPHONY_LIMIT)]
    kb.play(*chord, velocity=70)
    kb.port.flush()
    print(f"    Sounding {POLYPHONY_LIMIT} notes at once on the accompaniment bus.")
    full = ask("Can you hear a big cluster?")
    if full is None:
        kb.stop_all()
        r.status = "skipped"
        return r

    cues = [96, 97, 98, 99]
    kb.cue_on(*cues)
    kb.port.flush()
    print(f"    Added {len(cues)} silent cues on top, while those notes still sound.")
    dropped = ask("Did any of the sounding notes CUT OUT?")
    kb.cue_off(*cues)
    kb.stop_all()
    kb.port.flush()

    r.status = "pass"
    r.data = {"cluster_audible": full, "cues_steal_voices": dropped,
              "limit": POLYPHONY_LIMIT}
    if dropped:
        r.detail = ("cues DO consume voices - cap simultaneous cues and "
                    "prioritise musical output")
    elif dropped is False:
        r.detail = "cues appear not to steal voices from sounding notes"
    else:
        r.detail = "inconclusive"
    return r


def probe_cue_vs_held(kb: Keyboard, col: Collector) -> Result:
    """Does OUR note-off cut a note the PLAYER is holding? (ex-M0-2)"""
    r = Result("cue_vs_held", "Trainer note-off vs a held key")
    if not require_live(kb):
        r.status = "skipped"
        r.detail = "keyboard asleep"
        return r
    print("    Press and HOLD middle C. Keep holding it.")
    col.clear()
    m = col.wait_note_on(45)
    if m is None:
        r.status = "skipped"
        r.detail = "no keypress"
        return r
    note = m.note
    print(f"      holding note {note} - keep it down")
    time.sleep(1.0)
    kb.port.send(bytes([_status(NOTE_OFF, CUE_CHANNEL), note, 0]), Priority.CRITICAL)
    kb.port.flush()
    print("    Sent a cue note-off for that same note.")
    cut = ask("Did YOUR note stop sounding?")
    r.data = {"note": note, "player_sound_cut": cut}
    if cut is None:
        r.status = "skipped"
    else:
        r.status = "pass"
        r.detail = ("our note-off DOES cut the player's held note - never send "
                    "note-off for a held pitch" if cut else
                    "safe: our note-off does not disturb the player's held note")
    return r


# --------------------------------------------------------------------- driver

INTERACTIVE = {"keyboard_range", "light_range", "channel_matrix",
               "polyphony", "cue_vs_held"}


def run(kb: Keyboard, col: Collector, auto_only: bool = False,
        only: list[str] | None = None, answer_on_keyboard: bool | None = None
        ) -> list[Result]:
    global _ASKER
    if answer_on_keyboard is None:
        # No terminal to type into (piped, automated) -> use the piano.
        answer_on_keyboard = not sys.stdin.isatty()
    _ASKER = KeyboardAsker(col) if answer_on_keyboard else StdinAsker()
    if answer_on_keyboard and not auto_only:
        print("Answering ON THE KEYBOARD: below middle C = yes, above = no.\n")

    plan = [
        ("link", lambda: probe_link(kb, col)),
        ("liveness", lambda: probe_liveness(kb, col)),
        ("echo", lambda: probe_echo(kb, col)),
        ("latency", lambda: probe_latency(kb, col)),
        ("keyboard_range", lambda: probe_keyboard_range(kb, col)),
        ("light_map", lambda: probe_light_map(kb, col)),
        ("channel_matrix", lambda: probe_channel_matrix(kb, col)),
        ("polyphony", lambda: probe_polyphony(kb, col)),
        ("cue_vs_held", lambda: probe_cue_vs_held(kb, col)),
    ]
    if auto_only:
        plan = [p for p in plan if p[0] not in INTERACTIVE]
    if only:
        plan = [p for p in plan if p[0] in only]

    results: list[Result] = []

    for i, (name, fn) in enumerate(plan, 1):
        res = fn()
        banner(i, len(plan), res.title)
        print(f"    {res.status.upper():8} {res.detail}")
        results.append(res)
        if name == "keyboard_range":
            if not auto_only and res.status == "pass":
                lr = probe_light_range(kb, col, res)
                print(f"    {lr.status.upper():8} {lr.detail}")
                results.append(lr)
        if name == "link" and res.status == "fail":
            print("\n    Keyboard appears to be off - stopping here.")
            break
    return results


def write_profile(results: list[Result], path: str | None = None) -> str:
    if path is None:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
        path = os.path.join(base, "plluminati", "hardware-profile.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "probes": {r.name: asdict(r) for r in results},
    }
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(tmp, path)          # atomic (SPEC 7.7)
    return path
