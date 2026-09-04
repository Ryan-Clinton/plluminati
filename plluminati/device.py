"""Locating the MIDI device.

The ALSA card number is NOT stable across reboots or replugs, so it is resolved
at runtime and never hardcoded (SPEC 7.1).
"""

from __future__ import annotations

import glob
import os
import re
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class MidiDevice:
    path: str          # /dev/snd/midiC2D0
    card: int
    device: int
    name: str          # "USB Midi Cable"
    longname: str = ""

    @property
    def alsa_id(self) -> str:
        """The hw:C,D,S form used by amidi."""
        return f"hw:{self.card},{self.device},0"

    def __str__(self) -> str:
        return f"{self.name}  ({self.path}, {self.alsa_id})"


_CARD_LINE = re.compile(r"^\s*(\d+)\s+\[([^\]]+)\]\s*:\s*(.*)$")


def _card_names() -> dict[int, tuple[str, str]]:
    """Map card number -> (short id, description) from /proc/asound/cards."""
    out: dict[int, tuple[str, str]] = {}
    try:
        with open("/proc/asound/cards") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return out

    for i, line in enumerate(lines):
        m = _CARD_LINE.match(line)
        if not m:
            continue
        num, short, desc = int(m.group(1)), m.group(2).strip(), m.group(3).strip()
        # the following line holds the long name, when present
        longname = lines[i + 1].strip() if i + 1 < len(lines) else ""
        out[num] = (desc or short, longname)
    return out


def discover() -> list[MidiDevice]:
    """Every rawmidi device currently present, best candidate first."""
    names = _card_names()
    found: list[MidiDevice] = []

    for path in sorted(glob.glob("/dev/snd/midiC*D*")):
        m = re.search(r"midiC(\d+)D(\d+)$", path)
        if not m:
            continue
        card, dev = int(m.group(1)), int(m.group(2))
        name, longname = names.get(card, (f"card {card}", ""))
        found.append(MidiDevice(path=path, card=card, device=dev,
                                name=name, longname=longname))

    # Prefer USB devices - on this rig the keyboard is a USB-MIDI cable, while
    # onboard HDA cards may also expose rawmidi nodes.
    def rank(d: MidiDevice) -> tuple[int, int, int]:
        blob = f"{d.name} {d.longname}".lower()
        usb = 0 if ("usb" in blob or "midi" in blob) else 1
        return (usb, d.card, d.device)

    found.sort(key=rank)
    return found


def resolve(spec: str | None = None) -> MidiDevice:
    """Pick the device to use.

    `spec` may be a device path, an `hw:C,D` string, or a substring of the
    card name. Without it, the best-ranked discovered device wins.
    """
    devices = discover()

    if spec:
        # explicit path
        if spec.startswith("/dev/"):
            for d in devices:
                if d.path == spec:
                    return d
            raise DeviceNotFound(f"no rawmidi device at {spec!r}")

        # hw:C,D[,S]
        m = re.match(r"^hw:(\d+),(\d+)", spec)
        if m:
            card, dev = int(m.group(1)), int(m.group(2))
            for d in devices:
                if d.card == card and d.device == dev:
                    return d
            raise DeviceNotFound(f"no rawmidi device for {spec!r}")

        # name substring
        matches = [d for d in devices if spec.lower() in f"{d.name} {d.longname}".lower()]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise DeviceNotFound(f"no MIDI device matching {spec!r}")
        raise DeviceNotFound(
            f"{spec!r} is ambiguous: " + ", ".join(d.alsa_id for d in matches))

    if not devices:
        raise DeviceNotFound(
            "no MIDI device found. Is the USB-MIDI cable plugged in? "
            "Check with: amidi -l")
    return devices[0]


def amidi_ports() -> list[str]:
    """`amidi -l` output, purely as a cross-check for diagnostics."""
    try:
        res = subprocess.run(["amidi", "-l"], capture_output=True, text=True, timeout=5)
        return [ln for ln in res.stdout.splitlines()[1:] if ln.strip()]
    except (OSError, subprocess.SubprocessError):
        return []


def readable(dev: MidiDevice) -> tuple[bool, str]:
    """Can we actually open it? Returns (ok, explanation)."""
    if not os.path.exists(dev.path):
        return False, f"{dev.path} does not exist"
    if not os.access(dev.path, os.R_OK | os.W_OK):
        return False, (f"{dev.path} is not read/write for this user - "
                       f"you may need to be in the 'audio' group")
    return True, "ok"


class DeviceNotFound(RuntimeError):
    pass
