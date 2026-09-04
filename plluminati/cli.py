"""Command line entry point."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time

from . import __version__, device
from .keyboard import CUE_VELOCITY, KEY_RANGE
from .parser import Kind
from .port import wait_for_active_sensing
from .session import open_keyboard

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def note_name(n: int) -> str:
    return f"{NOTE_NAMES[n % 12]}{n // 12 - 1}"


def parse_note(token: str) -> int:
    """Accept a MIDI number (60) or a name (C4, F#3)."""
    token = token.strip()
    if token.isdigit():
        return int(token)
    import re
    m = re.fullmatch(r"([A-Ga-g])([#b]?)(-?\d+)", token)
    if not m:
        raise argparse.ArgumentTypeError(f"not a note: {token!r} (try 60 or C4)")
    base = NOTE_NAMES.index(m.group(1).upper())
    base += {"#": 1, "b": -1, "": 0}[m.group(2)]
    return base + (int(m.group(3)) + 1) * 12



def parse_bars(token: str) -> tuple[int, int]:
    """'17-24' or a single bar '17'."""
    import re
    m = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", token.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"not a bar range: {token!r} (try 17-24)")
    first = int(m.group(1))
    last = int(m.group(2)) if m.group(2) else first
    if last < first:
        raise argparse.ArgumentTypeError("bar range runs backwards")
    return (first, last)


# ---------------------------------------------------------------- commands

def cmd_devices(args) -> int:
    devices = device.discover()
    if not devices:
        print("No MIDI devices found.")
        print("Is the USB-MIDI cable plugged in?  Cross-check with: amidi -l")
        return 1

    print(f"{len(devices)} MIDI device(s):\n")
    for i, d in enumerate(devices):
        ok, why = device.readable(d)
        mark = "*" if i == 0 else " "
        print(f" {mark} {d.name}")
        print(f"     path   {d.path}")
        print(f"     alsa   {d.alsa_id}")
        if d.longname:
            print(f"     info   {d.longname}")
        print(f"     access {'ok' if ok else 'NO - ' + why}")

        if ok:
            live = wait_for_active_sensing(d.path, timeout=args.probe)
            print(f"     power  {'ON (Active Sensing seen)' if live else 'no keepalive seen'}")
        print()

    print("* = the device Plluminati will use by default")
    ports = device.amidi_ports()
    if ports:
        print("\namidi -l says:")
        for p in ports:
            print("   " + p)
    return 0


def cmd_light(args) -> int:
    notes = [parse_note(t) for t in args.notes]
    pretty = ", ".join(f"{note_name(n)} ({n})" for n in notes)

    with open_keyboard(args.device) as kb:
        if args.sound:
            print(f"Sounding {pretty} on the accompaniment bus (channel 2)")
            print("  -> should SOUND, and light nothing")
            kb.play(*notes, velocity=args.velocity)
        else:
            print(f"Lighting {pretty} on the cue bus (channel 1, velocity {CUE_VELOCITY})")
            print("  -> should LIGHT, silently")
            kb.cue_on(*notes)

        kb.port.flush()
        try:
            time.sleep(args.seconds)
        except KeyboardInterrupt:
            print("\ninterrupted")
        print("releasing")
    return 0


def cmd_listen(args) -> int:
    counts = {"active_sensing": 0, "messages": 0}
    start = time.monotonic()

    def show(msg):
        if msg.is_active_sensing:
            counts["active_sensing"] += 1
            if not args.all:
                return
        counts["messages"] += 1
        stamp = time.monotonic() - start
        extra = ""
        if msg.kind in (Kind.NOTE_ON, Kind.NOTE_OFF):
            extra = f"   {note_name(msg.note)}"
            if msg.kind is Kind.NOTE_ON and msg.velocity == CUE_VELOCITY:
                extra += "   <- velocity 1: this is a cue echo, not playing"
        print(f"{stamp:8.3f}  {msg}{extra}")

    limit = f"for {args.seconds:g}s" if args.seconds else "until Ctrl-C"
    print(f"Listening on the keyboard {limit}."
          f"{'' if args.all else '  (Active Sensing hidden; -a shows it)'}\n")
    sys.stdout.flush()
    deadline = start + args.seconds if args.seconds else None
    with open_keyboard(args.device, on_message=show, setup=False) as kb:
        try:
            while deadline is None or time.monotonic() < deadline:
                time.sleep(0.05)
        except KeyboardInterrupt:
            pass

    elapsed = time.monotonic() - start
    print(f"\n{counts['messages']} message(s) shown in {elapsed:.1f}s")
    rate = counts["active_sensing"] / elapsed if elapsed else 0
    print(f"Active Sensing: {counts['active_sensing']} ({rate:.0f}/sec) "
          f"-> keyboard was {'ON' if counts['active_sensing'] else 'OFF or silent'}")
    if kb.player_velocity is not None:
        print(f"Calibrated player velocity for the current voice: {kb.player_velocity}")
    return 0


def cmd_info(args) -> int:
    from .song import load
    song = load(args.file)
    d = song.hand_detection
    lo, hi = song.pitch_range

    print(f"{os.path.basename(args.file)}")
    print(f"  fingerprint  {song.fingerprint}")
    print(f"  format       {song.format}, {song.division} ticks/beat, "
          f"{song.tempo_map.bpm_at(0):.0f} bpm")
    print(f"  content      {len(song.notes)} notes in {len(song.steps)} steps, "
          f"{song.bars} bars, {song.duration:.1f}s")
    print(f"  pitch range  {lo}-{hi}  ({note_name(lo)}-{note_name(hi)})")
    print(f"  channels     {song.channels}")
    if song.track_names:
        print(f"  tracks       {dict(song.track_names)}")

    print(f"\n  hands: {d['strategy']}  confidence {d['confidence']}")
    print(f"         {d['detail']}")
    if d.get("needs_confirmation"):
        print("         ** LOW CONFIDENCE - check this before it counts toward scores")
        print("         override with:  --split 60   or   --hands 0=right,1=left")

    counts = {}
    for n in song.notes:
        counts[n.hand] = counts.get(n.hand, 0) + 1
    print(f"         notes per hand: {counts}")

    oor = song.out_of_range
    if oor:
        pitches = sorted({n.pitch for n in oor})
        print(f"\n  ** {len(oor)} notes outside the keyboard's {KEY_RANGE[0]}-"
              f"{KEY_RANGE[1]} range: {pitches}")
        print("     These cannot be lit. Affected sections are refused rather")
        print("     than octave-folded, which would teach the wrong movement.")
        bad_steps = [s for s in song.steps if not s.playable]
        print(f"     {len(bad_steps)} of {len(song.steps)} steps affected.")
    else:
        print(f"\n  all notes fit the keyboard ({KEY_RANGE[0]}-{KEY_RANGE[1]})")

    if args.steps:
        print("\n  steps:")
        for s in song.steps[:args.steps]:
            names = " ".join(note_name(p) for p in s.pitches)
            flag = "" if s.playable else "   ** out of range"
            print(f"    {s.index:>4}  bar {s.bar:>3} beat {s.beat:<5g} "
                  f"{s.time:>6.2f}s  {names}{flag}")
    return 0


def cmd_learn(args) -> int:
    from .engine import LearnSession
    from .song import load
    from . import hands as hands_mod

    song = load(args.file)
    if args.split:
        song.hand_detection = hands_mod.apply_split(song, args.split)
    d = song.hand_detection

    steps = song.steps_for_hand(args.hand)
    if args.bars:
        first, last = args.bars
        steps = [s for s in steps if first <= s.bar <= last]
        if not steps:
            print(f"error: no notes in bars {first}-{last}", file=sys.stderr)
            return 2

    print(f"{os.path.basename(args.file)}  -  {args.hand} hand")
    print(f"hands detected by {d['strategy']} (confidence {d['confidence']})")
    if d.get("needs_confirmation") and not args.split:
        print("** hand detection is uncertain; --split N overrides it")
    print(f"{len(steps)} steps"
          + (f", bars {args.bars[0]}-{args.bars[1]}" if args.bars else ""))
    print("\nPlay the lit keys. Ctrl-C to stop.\n")

    holder: dict = {}
    def dispatch(msg):
        s = holder.get("session")
        if s is not None:
            s.handle(msg)

    with open_keyboard(args.device, on_message=dispatch) as kb:
        session = LearnSession(kb, song, hand=args.hand, strict=args.strict,
                               accompany=not args.no_accompany, steps=steps)
        holder["session"] = session

        def show(step, index):
            names = " ".join(note_name(p) for p in step.pitches)
            done, total = session.progress
            print(f"\r  [{done + 1}/{total}]  bar {step.bar:<3} {names:<20}",
                  end="", flush=True)
        session.on_step = show

        if not kb.is_live:
            print("keyboard is asleep or off - press any key on it to wake it")
            kb.wait_until_live(120)

        try:
            report = session.run()
        except KeyboardInterrupt:
            report = session.report

    print("\n\n" + report.summary())
    if report.per_step:
        worst = sorted(report.per_step, key=lambda s: -s.attempts)[:3]
        tricky = [w for w in worst if w.attempts]
        if tricky:
            print("hardest steps:")
            for w in tricky:
                print(f"   bar {w.bar:<3} {' '.join(note_name(p) for p in w.pitches):<16}"
                      f" {w.attempts} wrong")
    return 0


def cmd_serve(args) -> int:
    import subprocess
    import webbrowser
    from .server import Hub, serve

    hub = Hub()
    url = f"http://127.0.0.1:{args.port}/"

    holder: dict = {}
    def dispatch(msg):
        hub.on_midi(msg)

    offline = args.offline
    if not offline:
        try:
            device.resolve(args.device)
        except device.DeviceNotFound:
            # Falling over because a cable is unplugged is a poor way to greet
            # anyone; the UI is worth browsing on its own.
            print("No keyboard found - starting in offline mode.")
            print("The UI works; nothing will light up.  Plug the cable in and")
            print("restart to play for real.\n")
            offline = True

    with open_keyboard(args.device, on_message=dispatch, offline=offline) as kb:
        hub.kb = kb
        httpd = serve(hub, port=args.port)
        print(f"Plluminati UI on {url}")
        print("Ctrl-C to stop.\n")

        if not args.no_browser:
            opened = False
            for browser in ("google-chrome", "chromium", "chromium-browser"):
                exe = shutil.which(browser)
                if exe:
                    # --app gives a clean window with no browser chrome, which
                    # suits a screen sitting behind a keyboard.
                    subprocess.Popen([exe, f"--app={url}", "--start-fullscreen"],
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
                    opened = True
                    break
            if not opened:
                webbrowser.open(url)

        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\nstopping")
        finally:
            hub.stop_session()
            httpd.shutdown()
    return 0


def cmd_play(args) -> int:
    """Just listen to it - no waiting, no scoring."""
    from .playback import Playback
    from .song import load

    song = load(args.file)
    with open_keyboard(args.device) as kb:
        if not kb.is_live:
            print("keyboard asleep - press any key on it to wake it...")
            kb.wait_until_live(60)
        pb = Playback(kb, song, speed=args.speed, hand=args.hand,
                      velocity=args.velocity, light=not args.no_lights)
        print(f"{os.path.basename(args.file)} - {song.bars} bars, "
              f"{song.duration / args.speed:.0f}s at {args.speed:.0%} speed")
        print("playing" + (" with lights" if not args.no_lights else "")
              + ".  Ctrl-C to stop.")
        try:
            n = pb.run()
        except KeyboardInterrupt:
            pb.stop(); n = 0
    print(f"done ({n} notes)")
    return 0


def cmd_selftest(args) -> int:
    from . import selftest

    print("Plluminati hardware selftest")
    print("=" * 60)
    if not args.auto_only:
        print("Some probes need you at the keyboard. Answer y / n, or s to skip.")
    print()

    col = selftest.Collector()
    with open_keyboard(args.device, on_message=col) as kb:
        results = selftest.run(kb, col, auto_only=args.auto_only, only=args.only,
                               answer_on_keyboard=args.answer_on_keyboard)

    print("\n" + "=" * 60)
    print("SUMMARY\n")
    width = max((len(r.title) for r in results), default=10)
    for r in results:
        print(f"  {r.status.upper():8}  {r.title:<{width}}  {r.detail}")

    path = selftest.write_profile(results, args.out)
    print(f"\nHardware profile written to:\n  {path}")

    failed = [r for r in results if r.status == "fail"]
    return 1 if failed else 0


# ------------------------------------------------------------------- plumbing

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="plluminati",
        description="Silent-light practice trainer for the Yamaha EZ-150.")
    p.add_argument("--version", action="version", version=f"plluminati {__version__}")
    p.add_argument("-d", "--device", metavar="SPEC",
                   help="device path, hw:C,D, or a name substring")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("devices", help="list MIDI devices and check the link")
    d.add_argument("--probe", type=float, default=1.0,
                   help="seconds to watch for Active Sensing (default 1.0)")
    d.set_defaults(func=cmd_devices)

    li = sub.add_parser("light", help="light keys silently (proves output)")
    li.add_argument("notes", nargs="+", help="MIDI numbers or names, e.g. 60 or C4")
    li.add_argument("-s", "--seconds", type=float, default=3.0)
    li.add_argument("--sound", action="store_true",
                    help="sound them on channel 2 instead (should NOT light)")
    li.add_argument("--velocity", type=int, default=80,
                    help="velocity for --sound (default 80)")
    li.set_defaults(func=cmd_light)

    inf = sub.add_parser("info", help="analyse a MIDI file: steps, bars, hands")
    inf.add_argument("file")
    inf.add_argument("-s", "--steps", type=int, default=0, metavar="N",
                     help="also list the first N steps")
    inf.set_defaults(func=cmd_info)

    lr = sub.add_parser("learn", help="light-and-wait practice (the core mode)")
    lr.add_argument("file")
    lr.add_argument("--hand", choices=("right", "left", "both"), default="right")
    lr.add_argument("--bars", type=parse_bars, metavar="A-B",
                    help="practise only these bars, e.g. 17-24")
    lr.add_argument("--split", type=int, metavar="NOTE",
                    help="override hand detection: split at this MIDI note")
    lr.add_argument("--strict", action="store_true",
                    help="require an exact match; wrong notes block progress")
    lr.add_argument("--no-accompany", action="store_true",
                    help="do not play the other hand")
    lr.set_defaults(func=cmd_learn)

    pl = sub.add_parser("play", help="play a MIDI file through the keyboard")
    pl.add_argument("file")
    pl.add_argument("--speed", type=float, default=1.0)
    pl.add_argument("--hand", choices=("right", "left", "both"), default="both")
    pl.add_argument("--velocity", type=int, default=90)
    pl.add_argument("--no-lights", action="store_true",
                    help="sound it on channel 2 so no keys light up")
    pl.set_defaults(func=cmd_play)

    sv = sub.add_parser("serve", help="run the web UI (the main way to use this)")
    sv.add_argument("-p", "--port", type=int, default=8420)
    sv.add_argument("--no-browser", action="store_true",
                    help="do not launch a browser window")
    sv.add_argument("--offline", action="store_true",
                    help="run without a keyboard - browse the UI, nothing lights up")
    sv.set_defaults(func=cmd_serve)

    st = sub.add_parser("selftest", help="guided hardware probe; writes a profile")
    st.add_argument("--auto-only", action="store_true",
                    help="skip probes that need you at the keyboard")
    st.add_argument("--only", nargs="+", metavar="PROBE",
                    help="run only these probes (link echo latency "
                         "keyboard_range channel_matrix polyphony cue_vs_held)")
    st.add_argument("--answer-on-keyboard", dest="answer_on_keyboard",
                    action="store_true", default=None,
                    help="answer prompts on the piano (below middle C = yes, "
                         "above = no) instead of typing; automatic when there "
                         "is no terminal")
    st.add_argument("--type-answers", dest="answer_on_keyboard",
                    action="store_false",
                    help="force typed y/n answers")
    st.add_argument("-o", "--out", metavar="PATH",
                    help="where to write the hardware profile JSON")
    st.set_defaults(func=cmd_selftest)

    ls = sub.add_parser("listen", help="decode incoming MIDI live (proves input)")
    ls.add_argument("-a", "--all", action="store_true",
                    help="include Active Sensing keepalives")
    ls.add_argument("-s", "--seconds", type=float, default=0,
                    help="stop after N seconds (default: run until Ctrl-C)")
    ls.set_defaults(func=cmd_listen)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except device.DeviceNotFound as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except PermissionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 13
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
