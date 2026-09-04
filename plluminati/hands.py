"""Working out which notes belong to which hand.

Evidence-based with a confidence score, never first-match-wins (SPEC 7.3).
This is the part most likely to be wrong on real-world files, so it reports how
sure it is and says which evidence won - a silent wrong guess would quietly
corrupt scores, since a "right hand 100%" record for the wrong set of notes is
worse than no record.

Deliberately NOT implemented: "a two-track format-1 file is almost always right
hand then left hand." That elevates a heuristic to a property of SMF files.
Track ORDER is ignored; only pitch statistics decide, and low confidence is
reported rather than hidden.
"""

from __future__ import annotations

import re

MIDDLE_C = 60

_RIGHT_WORDS = re.compile(r"\b(right|rh|r\.h\.|treble|melody|upper|sopran)", re.I)
_LEFT_WORDS = re.compile(r"\b(left|lh|l\.h\.|bass|lower|accomp)", re.I)


def _assign(notes, hand: str) -> None:
    for n in notes:
        n.hand = hand


def detect(song) -> dict:
    """Assign `hand` to every note. Returns a report describing the decision."""
    notes = song.notes
    if not notes:
        return {"strategy": "none", "confidence": 0.0, "detail": "no notes"}

    groups: dict[int, list] = {}
    for n in notes:
        groups.setdefault(n.track, []).append(n)

    # ---- evidence 1: explicit names on the tracks ---------------------------
    # Read the names from the FILE, not from the note groups: an arrangement
    # may legitimately name both hands while one of them is silent (melody-only
    # easy editions do exactly this), and that track carries no notes.
    named: dict[int, str] = {}
    for track, name in song.track_names.items():
        if _RIGHT_WORDS.search(name):
            named[track] = "right"
        elif _LEFT_WORDS.search(name):
            named[track] = "left"

    bearing_named = {t: h for t, h in named.items() if groups.get(t)}
    if len(bearing_named) == 1 and len(named) >= 1:
        # one named hand actually plays - assign all its notes there, and say so
        only_track, only_hand = next(iter(bearing_named.items()))
        for track, members in groups.items():
            _assign(members, only_hand if track == only_track else "other")
        return {"strategy": "track_names_single",
                "confidence": 0.95,
                "detail": (f"melody-only arrangement: '"
                           f"{song.track_names.get(only_track, only_track)}' has all "
                           f"the notes, the other staff is rests"),
                "assignment": {only_track: only_hand}}

    if len(set(named.values())) == 2:
        for track, members in groups.items():
            _assign(members, named.get(track, "other"))
        return {"strategy": "track_names", "confidence": 0.95,
                "detail": f"track names identify both hands: "
                          f"{ {song.track_names.get(t, t): h for t, h in named.items()} }",
                "assignment": {t: named.get(t, "other") for t in groups}}

    # ---- evidence 2: two note-bearing tracks, split by pitch statistics -----
    bearing = {t: m for t, m in groups.items() if m}
    if len(bearing) == 2:
        stats = {t: sum(n.pitch for n in m) / len(m) for t, m in bearing.items()}
        lo_track, hi_track = sorted(stats, key=lambda t: stats[t])
        separation = stats[hi_track] - stats[lo_track]
        overlap = _overlap(bearing[lo_track], bearing[hi_track])
        # confident when the parts are well separated and barely overlap
        confidence = max(0.0, min(0.9, separation / 24)) * (1 - overlap)
        _assign(bearing[lo_track], "left")
        _assign(bearing[hi_track], "right")
        return {
            "strategy": "two_tracks_pitch",
            "confidence": round(confidence, 2),
            "detail": (f"two note tracks; mean pitch "
                       f"{stats[lo_track]:.1f} vs {stats[hi_track]:.1f} "
                       f"(separation {separation:.1f} semitones, "
                       f"range overlap {overlap:.0%})"),
            "assignment": {lo_track: "left", hi_track: "right"},
            "needs_confirmation": confidence < 0.5,
        }

    # ---- evidence 3: distinct channels, when there are exactly two ----------
    by_channel: dict[int, list] = {}
    for n in notes:
        by_channel.setdefault(n.channel, []).append(n)
    if len(by_channel) == 2:
        stats = {c: sum(n.pitch for n in m) / len(m) for c, m in by_channel.items()}
        lo_ch, hi_ch = sorted(stats, key=lambda c: stats[c])
        separation = stats[hi_ch] - stats[lo_ch]
        overlap = _overlap(by_channel[lo_ch], by_channel[hi_ch])
        confidence = max(0.0, min(0.85, separation / 24)) * (1 - overlap)
        _assign(by_channel[lo_ch], "left")
        _assign(by_channel[hi_ch], "right")
        note = ""
        if {lo_ch, hi_ch} == {3, 4}:
            note = ("  (channels 3/4 happen to match PianoBooster's convention, "
                    "but that is NOT assumed - the pitch evidence decided)")
        return {
            "strategy": "two_channels_pitch",
            "confidence": round(confidence, 2),
            "detail": (f"channels {lo_ch} and {hi_ch}; mean pitch "
                       f"{stats[lo_ch]:.1f} vs {stats[hi_ch]:.1f}{note}"),
            "assignment": {f"ch{lo_ch}": "left", f"ch{hi_ch}": "right"},
            "needs_confirmation": confidence < 0.5,
        }

    # ---- fallback: split at middle C ---------------------------------------
    crossings = _hand_crossings(notes)
    for n in notes:
        n.hand = "right" if n.pitch >= MIDDLE_C else "left"
    confidence = 0.4 if crossings < 0.05 else 0.2
    return {
        "strategy": "pitch_split",
        "confidence": confidence,
        "detail": (f"single part - split at middle C ({MIDDLE_C}). "
                   f"Lossy for hand crossing and wide left-hand parts; "
                   f"{crossings:.0%} of steps straddle the split."),
        "assignment": {"<60": "left", ">=60": "right"},
        "needs_confirmation": True,
    }


def apply_split(song, split: int) -> dict:
    """Manual override: everything at or above `split` is the right hand."""
    for n in song.notes:
        n.hand = "right" if n.pitch >= split else "left"
    return {"strategy": "manual_split", "confidence": 1.0,
            "detail": f"user-specified split at {split}",
            "assignment": {f"<{split}": "left", f">={split}": "right"}}


def apply_tracks(song, mapping: dict[int, str]) -> dict:
    """Manual override: explicit track -> hand mapping."""
    for n in song.notes:
        n.hand = mapping.get(n.track, "other")
    return {"strategy": "manual_tracks", "confidence": 1.0,
            "detail": f"user-specified track mapping {mapping}",
            "assignment": dict(mapping)}


# ------------------------------------------------------------------ helpers

def _overlap(a: list, b: list) -> float:
    """How much two parts share pitch territory, 0..1."""
    if not a or not b:
        return 0.0
    a_lo, a_hi = min(n.pitch for n in a), max(n.pitch for n in a)
    b_lo, b_hi = min(n.pitch for n in b), max(n.pitch for n in b)
    inter = max(0, min(a_hi, b_hi) - max(a_lo, b_lo))
    union = max(a_hi, b_hi) - min(a_lo, b_lo)
    return inter / union if union else 1.0


def _hand_crossings(notes: list) -> float:
    """Fraction of simultaneous groups that straddle middle C."""
    by_tick: dict[int, list[int]] = {}
    for n in notes:
        by_tick.setdefault(n.start_tick, []).append(n.pitch)
    if not by_tick:
        return 0.0
    straddle = sum(1 for ps in by_tick.values()
                   if min(ps) < MIDDLE_C <= max(ps))
    return straddle / len(by_tick)
