"""Profiles and scores on disk.

SPEC 7.7 / F8 / F9. Three boring guarantees that matter more than they look:

* **Atomic writes** - temp file then `os.replace`. A power cut mid-write must
  never truncate a child's practice history.
* **Stable internal IDs** - `profiles/<uuid>.json` holding a `display_name`,
  never a filename derived from what someone typed. Renaming then costs
  nothing and cannot collide or orphan scores.
* **Schema version** on every file, checked on load.

A score is only comparable to another if the *practice definition* matches
too - hand assignment, quantisation, transposition and the target set all
affect what "right hand, 100%" even means. So each result carries a
definition fingerprint alongside the matcher version.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field

SCHEMA = 1

#: Bump when the matcher's semantics change. Old scores stay readable but are
#: shown as being from a superseded ruleset rather than silently compared.
MATCHER_VERSION = "1.0"

#: Speeds at which a hand's mastery ring gains a pip. Real achievement only -
#: nothing is awarded for showing up (SPEC 9.3).
MASTERY_SPEEDS = (0.40, 0.60, 0.80, 0.90, 1.00)

AVATARS = ["🐙", "🦊", "🐉", "🦉", "🐝", "🦕", "🐧", "🦁", "🐳", "🦀", "🦎", "🐢"]
THEMES = {
    "blue":   "#5aa9ff",
    "orange": "#ff8a3d",
    "purple": "#b47cff",
    "green":  "#35d68a",
    "pink":   "#ff6fa5",
    "gold":   "#ffc23d",
    "teal":   "#2fd4c8",
    "slate":  "#8b95ab",
}


def data_dir() -> str:
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "plluminati")


def _write_atomic(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _read(path: str) -> dict | None:
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("schema") != SCHEMA:
        data["_schema_mismatch"] = data.get("schema")
    return data


@dataclass
class Result:
    """One practice run."""
    when: float = field(default_factory=time.time)
    mode: str = "learn"              # learn | along
    hand: str = "right"
    speed: float = 1.0
    clean: bool = False
    steps: int = 0
    completed: int = 0
    clean_steps: int = 0
    wrong: int = 0
    recall: float = 0.0
    timing_ms: float = 0.0
    seconds: float = 0.0
    section: list | None = None      # [first_tick, last_tick] or None
    matcher: str = MATCHER_VERSION
    definition: str = ""             # practice-definition fingerprint


@dataclass
class Profile:
    id: str
    display_name: str
    avatar: str = AVATARS[0]
    theme: str = "blue"
    created: float = field(default_factory=time.time)
    prefs: dict = field(default_factory=lambda: {
        "hand": "right", "mode": "learn", "speed_floor": 0.4, "accompany": True})
    #: fingerprint -> {"title":…, "hands": {hand: {...}}, "section": [...]}
    songs: dict = field(default_factory=dict)

    # ------------------------------------------------------------- scoring

    def song(self, fingerprint: str, title: str = "") -> dict:
        entry = self.songs.setdefault(fingerprint, {
            "title": title, "hands": {}, "section": None, "last_played": 0.0,
        })
        if title and not entry.get("title"):
            entry["title"] = title
        return entry

    def record(self, fingerprint: str, title: str, result: Result) -> dict:
        entry = self.song(fingerprint, title)
        entry["last_played"] = result.when
        hand = entry["hands"].setdefault(result.hand, {
            "attempts": 0, "best_clean_speed": 0.0, "best_recall": 0.0,
            "last": None, "history": [],
        })
        hand["attempts"] += 1
        hand["last"] = asdict(result)
        hand["best_recall"] = max(hand["best_recall"], result.recall)
        if result.clean:
            hand["best_clean_speed"] = max(hand["best_clean_speed"], result.speed)
        hand["history"] = (hand["history"] + [asdict(result)])[-20:]
        return hand

    def mastery(self, fingerprint: str, hand: str) -> int:
        """Pips earned for this hand, 0..5 - by achieved clean speed only."""
        entry = self.songs.get(fingerprint)
        if not entry:
            return 0
        best = entry.get("hands", {}).get(hand, {}).get("best_clean_speed", 0.0)
        return sum(1 for s in MASTERY_SPEEDS if best >= s - 1e-9)

    def continue_state(self, fingerprint: str) -> dict | None:
        """What this player was last working on - the library's hook."""
        entry = self.songs.get(fingerprint)
        if not entry or not entry.get("last_played"):
            return None
        hands = entry.get("hands", {})
        if not hands:
            return None
        hand, data = max(hands.items(),
                         key=lambda kv: (kv[1].get("last") or {}).get("when", 0))
        last = data.get("last") or {}
        return {"hand": hand, "speed": last.get("speed", 1.0),
                "section": entry.get("section"),
                "mode": last.get("mode", "learn")}


class Store:
    """All profiles, loaded and saved on demand."""

    def __init__(self, root: str | None = None) -> None:
        self.root = root or data_dir()
        self.profiles_dir = os.path.join(self.root, "profiles")
        os.makedirs(self.profiles_dir, exist_ok=True)
        self._cache: dict[str, Profile] = {}
        self.load_all()

    # --------------------------------------------------------------- load

    def load_all(self) -> list[Profile]:
        self._cache.clear()
        for fn in sorted(os.listdir(self.profiles_dir)):
            if not fn.endswith(".json"):
                continue
            data = _read(os.path.join(self.profiles_dir, fn))
            if not data:
                continue
            try:
                p = Profile(
                    id=data["id"], display_name=data["display_name"],
                    avatar=data.get("avatar", AVATARS[0]),
                    theme=data.get("theme", "blue"),
                    created=data.get("created", time.time()),
                    prefs=data.get("prefs") or {},
                    songs=data.get("songs") or {},
                )
            except KeyError:
                continue
            self._cache[p.id] = p
        return self.all()

    def all(self) -> list[Profile]:
        return sorted(self._cache.values(), key=lambda p: p.created)

    def get(self, pid: str) -> Profile | None:
        return self._cache.get(pid)

    # -------------------------------------------------------------- write

    def create(self, display_name: str, avatar: str = "", theme: str = "") -> Profile:
        used_av = {p.avatar for p in self._cache.values()}
        used_th = {p.theme for p in self._cache.values()}
        p = Profile(
            id=uuid.uuid4().hex[:12],
            display_name=display_name.strip()[:40] or "Player",
            avatar=avatar or next((a for a in AVATARS if a not in used_av), AVATARS[0]),
            theme=theme or next((t for t in THEMES if t not in used_th), "blue"),
        )
        self._cache[p.id] = p
        self.save(p)
        return p

    def save(self, profile: Profile) -> str:
        path = os.path.join(self.profiles_dir, f"{profile.id}.json")
        _write_atomic(path, {
            "schema": SCHEMA,
            "id": profile.id,
            "display_name": profile.display_name,
            "avatar": profile.avatar,
            "theme": profile.theme,
            "created": profile.created,
            "prefs": profile.prefs,
            "songs": profile.songs,
        })
        return path

    def delete(self, pid: str) -> bool:
        p = self._cache.pop(pid, None)
        if not p:
            return False
        try:
            os.remove(os.path.join(self.profiles_dir, f"{pid}.json"))
        except OSError:
            pass
        return True

    def rename(self, pid: str, name: str) -> bool:
        """Free, because the filename never depended on the name."""
        p = self._cache.get(pid)
        if not p:
            return False
        p.display_name = name.strip()[:40] or p.display_name
        self.save(p)
        return True


def definition_fingerprint(song, hand: str, section=None) -> str:
    """What "this practice" means, so scores stay comparable.

    Changing hand detection, quantisation or the section must produce a
    different value - otherwise an old "right hand 100%" could silently stand
    as the record for a different set of notes.
    """
    import hashlib
    from .song import CHORD_WINDOW
    parts = [
        song.fingerprint, hand,
        song.hand_detection.get("strategy", "?"),
        f"{CHORD_WINDOW:.4f}",
        f"{len(song.steps)}",
        f"{section}",
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:12]
