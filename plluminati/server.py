"""Local web UI: stdlib HTTP + Server-Sent Events.

SPEC 9.1. No dependencies: `http.server` and SSE are both stdlib, and SSE is
one-way server->browser which is exactly the shape of live feedback. The other
direction is plain same-origin JSON POSTs; WebSockets are not needed.

Bound to 127.0.0.1 only, serving a fixed set of assets - never a generic file
server on the LAN.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .keyboard import KEY_RANGE, Keyboard
from .parser import Kind, Message
from .store import AVATARS, MASTERY_SPEEDS, THEMES, Result, Store, definition_fingerprint
from .keyboard import Keyboard as _KB

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
SONGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "songs")

ASSETS = {"app.js": "application/javascript", "app.css": "text/css"}


class Hub:
    """Fan-out to browsers, and the single owner of UI-facing state."""

    def __init__(self) -> None:
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self.kb: Keyboard | None = None
        self.wizard = None
        self.session = None            # LearnSession | DrillSession
        self.store = Store()
        self.profile = None
        self._thread: threading.Thread | None = None
        self._song_cache: dict[str, dict] = {}
        self._synth = None
        self.laptop_sound = False
        self.wrong_sound = "hihat"
        self.state = {"mode": "idle", "live": False}

    # ----------------------------------------------------------- broadcast

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=512)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, kind: str, **data) -> None:
        payload = json.dumps({"type": kind, **data})
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass

    # --------------------------------------------------------------- midi

    def on_midi(self, msg: Message) -> None:
        if msg.is_active_sensing:
            if not self.state.get("live"):
                self.state["live"] = True
                self.publish("live", live=True)
            return

        if msg.kind is Kind.NOTE_ON:
            self.publish("played", note=msg.note, on=True, velocity=msg.velocity)
        elif msg.kind is Kind.NOTE_OFF:
            self.publish("played", note=msg.note, on=False)

        if self.wizard is not None:
            self.wizard.on_midi(msg)
        if self.session is not None and hasattr(self.session, "handle"):
            self.session.handle(msg)

    def watch_liveness(self) -> None:
        while True:
            time.sleep(0.4)
            if self.kb is None:
                continue
            live = self.kb.is_live
            if live != self.state.get("live"):
                self.state["live"] = live
                self.publish("live", live=live)

    # ------------------------------------------------------------ commands

    def command(self, name: str, args: dict) -> dict:
        handler = getattr(self, "cmd_" + name.replace(".", "_"), None)
        if handler is None:
            return {"ok": False, "error": f"unknown command {name!r}"}
        return handler(args)

    # -- session / profiles --------------------------------------------------

    def cmd_hello(self, args) -> dict:
        from .synth import Synth
        probe = self._synth or Synth()
        if self._synth is None:
            self._synth = probe
        return {"ok": True, "state": self.state, "range": list(KEY_RANGE),
                "live": bool(self.kb and self.kb.is_live),
                "profile": self._profile_json(self.profile),
                "avatars": AVATARS, "themes": THEMES,
                "laptop_sound": self.laptop_sound,
                "wrong_sound": self.wrong_sound,
                "wrong_sounds": list(_KB.WRONG_HITS),
                "synth": {"available": probe.available,
                          "detail": probe.describe()}}

    def cmd_wrong_sound(self, args) -> dict:
        name = args.get("name", "hihat")
        self.wrong_sound = name if name in _KB.WRONG_HITS else "hihat"
        if self.kb is not None:
            self.kb.hit_wrong(self.wrong_sound)    # preview it on the keyboard
        return {"ok": True, "wrong_sound": self.wrong_sound}

    def cmd_sound_source(self, args) -> dict:
        """keyboard = its own speakers; laptop = only correct notes sound."""
        self.laptop_sound = bool(args.get("laptop"))
        if self._synth is not None:
            self._synth.all_off()
        return {"ok": True, "laptop_sound": self.laptop_sound,
                "available": bool(self._synth and self._synth.available)}

    def cmd_profiles(self, args) -> dict:
        return {"ok": True,
                "profiles": [self._profile_json(p) for p in self.store.all()],
                "avatars": AVATARS, "themes": THEMES}

    def cmd_profile_create(self, args) -> dict:
        p = self.store.create(args.get("name", "Player"),
                              args.get("avatar", ""), args.get("theme", ""))
        self.profile = p
        return {"ok": True, "profile": self._profile_json(p)}

    def cmd_profile_select(self, args) -> dict:
        p = self.store.get(args.get("id", ""))
        if not p:
            return {"ok": False, "error": "no such profile"}
        self.profile = p
        return {"ok": True, "profile": self._profile_json(p)}

    def cmd_profile_delete(self, args) -> dict:
        pid = args.get("id", "")
        if self.profile and self.profile.id == pid:
            self.profile = None
        return {"ok": self.store.delete(pid)}

    def _profile_json(self, p) -> dict | None:
        if p is None:
            return None
        return {"id": p.id, "name": p.display_name, "avatar": p.avatar,
                "theme": p.theme, "accent": THEMES.get(p.theme, "#5aa9ff"),
                "prefs": p.prefs}

    # -- library -------------------------------------------------------------

    def cmd_songs(self, args) -> dict:
        return {"ok": True, "songs": self.library(force=bool(args.get("rescan")))}

    def cmd_song_import(self, args) -> dict:
        src = args.get("path", "")
        if not os.path.isfile(src) or not src.lower().endswith((".mid", ".midi")):
            return {"ok": False, "error": "not a MIDI file"}
        os.makedirs(os.path.join(SONGS_DIR, "imported"), exist_ok=True)
        dest = os.path.join(SONGS_DIR, "imported", os.path.basename(src))
        shutil.copy2(src, dest)
        self._song_cache.clear()
        return {"ok": True, "path": dest}

    def library(self, force: bool = False) -> list[dict]:
        from .song import load
        out = []
        for base, _dirs, files in os.walk(SONGS_DIR):
            for fn in sorted(files):
                if not fn.lower().endswith((".mid", ".midi")):
                    continue
                path = os.path.join(base, fn)
                key = f"{path}:{os.path.getmtime(path)}"
                entry = None if force else self._song_cache.get(key)
                if entry is None:
                    entry = {"path": path, "name": os.path.splitext(fn)[0]}
                    try:
                        song = load(path)
                        entry.update(
                            fingerprint=song.fingerprint, bars=song.bars,
                            steps=len(song.steps), seconds=round(song.duration, 1),
                            hands=song.hand_detection.get("strategy"),
                            confidence=song.hand_detection.get("confidence"),
                            needs_check=bool(song.hand_detection.get("needs_confirmation")),
                            out_of_range=len(song.out_of_range),
                            playable=not song.out_of_range,
                        )
                    except Exception as exc:
                        entry["error"] = str(exc)
                    self._song_cache[key] = entry
                row = dict(entry)
                fp = row.get("fingerprint")
                if self.profile and fp:
                    row["mastery"] = {h: self.profile.mastery(fp, h)
                                      for h in ("right", "left", "both")}
                    row["continue"] = self.profile.continue_state(fp)
                row["mastery_levels"] = len(MASTERY_SPEEDS)
                out.append(row)
        return out

    # -- practice ------------------------------------------------------------

    def cmd_learn_start(self, args) -> dict:
        return self._start(args, mode="learn")

    def cmd_drill_start(self, args) -> dict:
        return self._start(args, mode="drill")

    def cmd_listen_start(self, args) -> dict:
        """Just play it - no waiting, no scoring. What the kids asked for."""
        from .playback import Playback
        from .song import load

        path = args.get("path")
        if not path or not os.path.exists(path):
            return {"ok": False, "error": "song not found"}
        self.stop_session()
        song = load(path)
        speed = float(args.get("speed", 1.0))
        pb = Playback(self.kb, song, speed=speed,
                      hand=args.get("hand") or None,
                      light=not args.get("no_lights"))
        pb.on_note = lambda p, on: self.publish("cue", notes=[p], on=on)
        self.session = pb
        self.state["mode"] = "listen"

        if not self.kb.is_live:
            self.publish("wake_needed",
                         message="Press any key on the keyboard to wake it")

        self.publish("started", mode="listen",
                     title=os.path.splitext(os.path.basename(path))[0],
                     hand=args.get("hand") or "both", bars=None,
                     steps=len(song.steps),
                     detection=song.hand_detection.get("strategy"),
                     confidence=song.hand_detection.get("confidence"),
                     speed=speed)

        def runner():
            try:
                n = pb.run()
            finally:
                self.state["mode"] = "idle"
            self.publish("listen_done", notes=n,
                         seconds=round(song.duration / speed, 1))
            self.session = None

        self._thread = threading.Thread(target=runner, daemon=True)
        self._thread.start()
        return {"ok": True}

    def cmd_stop(self, args) -> dict:
        self.stop_session()
        return {"ok": True}

    def cmd_wizard_start(self, args) -> dict:
        from .wizard import Wizard
        self.stop_session()
        self.wizard = Wizard(self.kb, self)
        self.wizard.start()
        self.state["mode"] = "wizard"
        return {"ok": True}

    def cmd_wizard_answer(self, args) -> dict:
        if self.wizard:
            self.wizard.answer(args.get("value"))
        return {"ok": True}

    def cmd_wizard_skip(self, args) -> dict:
        if self.wizard:
            self.wizard.answer(None)
        return {"ok": True}

    def _start(self, args: dict, mode: str) -> dict:
        from .drill import DrillSession
        from .engine import LearnSession
        from .song import load

        path = args.get("path")
        if not path or not os.path.exists(path):
            return {"ok": False, "error": "song not found"}

        self.stop_session()
        song = load(path)
        hand = args.get("hand", "right")
        bars = tuple(args["bars"]) if args.get("bars") else None
        title = os.path.splitext(os.path.basename(path))[0]

        if not self.kb.is_live:
            self.publish("wake_needed",
                         message="Press any key on the keyboard to wake it")

        if mode == "learn":
            steps = song.steps_for_hand(hand)
            if bars:
                steps = [s for s in steps if bars[0] <= s.bar <= bars[1]]
            if not steps:
                return {"ok": False, "error": "no notes for that hand or bars"}
            session = LearnSession(
                self.kb, song, hand=hand, steps=steps,
                accompany=args.get("accompany", True),
                wrong_sound=args.get("wrong_sound", self.wrong_sound),
                dj_mode=bool(args.get("dj_mode")))
            session.on_step = lambda st, i: self.publish(
                "step", index=i, done=session.progress[0], total=session.progress[1],
                bar=st.bar, pitches=st.pitches, cue=sorted(session._required))
            session.on_verdict = lambda p, ok: self.publish("verdict", note=p, ok=ok)
            session.on_hint = lambda kind, pitches: self.publish(
                "hint", kind=kind, notes=list(pitches))
        else:
            speed = float(args.get("speed", 0.6))
            session = DrillSession(self.kb, song, hand=hand, bars=bars,
                                   speed=speed, max_reps=int(args.get("reps", 0)),
                                   accompany=args.get("accompany", True))
            if not session.steps:
                return {"ok": False, "error": "no notes for that hand or bars"}
            session.on_verdict = lambda p, ok: self.publish("verdict", note=p, ok=ok)
            session.on_cue = lambda ps, on: self.publish("cue", notes=list(ps), on=on)
            session.on_rep_start = lambda rep, sp: self.publish(
                "rep_start", rep=rep, speed=sp)
            session.on_rep_end = lambda r, sp: self.publish(
                "rep_end", verdict=r.verdict, speed=r.speed, next_speed=sp,
                hits=r.score.hits, targets=r.score.targets,
                wrong=r.score.wrong_attacks,
                timing_ms=round(r.score.mean_abs_timing * 1000),
                recall=round(r.score.recall, 3))

        self.session = session
        self.state["mode"] = mode
        self.publish("started", mode=mode, title=title, hand=hand,
                     bars=list(bars) if bars else None,
                     steps=len(getattr(session, "steps", [])),
                     detection=song.hand_detection.get("strategy"),
                     confidence=song.hand_detection.get("confidence"))

        def runner():
            try:
                report = session.run()
            finally:
                self.state["mode"] = "idle"
            self._record(song, title, hand, mode, report, bars)
            if mode == "learn":
                self.publish("learn_done", completed=report.steps_completed,
                             total=report.steps_total, clean=report.clean_steps,
                             wrong=report.wrong_attacks,
                             seconds=round(report.seconds, 1))
            else:
                last = report.history[-1] if report.history else None
                self.publish("drill_done", reps=report.reps,
                             start_speed=report.start_speed, speed=report.speed,
                             best_clean=report.best_clean_speed,
                             verdict=last.verdict if last else "hold",
                             hits=last.score.hits if last else 0,
                             targets=last.score.targets if last else 0,
                             wrong=last.score.wrong_attacks if last else 0,
                             timing_ms=round(last.score.mean_abs_timing * 1000)
                             if last else 0)
            self.session = None

        self._thread = threading.Thread(target=runner, daemon=True)
        self._thread.start()
        return {"ok": True}

    def _record(self, song, title, hand, mode, report, bars) -> None:
        if self.profile is None:
            return
        definition = definition_fingerprint(song, hand, bars)
        if mode == "learn":
            res = Result(mode="learn", hand=hand, speed=1.0,
                         clean=report.clean_steps == report.steps_total
                               and report.steps_total > 0,
                         steps=report.steps_total, completed=report.steps_completed,
                         clean_steps=report.clean_steps, wrong=report.wrong_attacks,
                         recall=round(report.accuracy, 3),
                         seconds=round(report.seconds, 1), definition=definition)
        else:
            last = report.history[-1] if report.history else None
            res = Result(mode="along", hand=hand, speed=report.best_clean_speed
                         or report.speed,
                         clean=bool(report.best_clean_speed),
                         steps=last.score.targets if last else 0,
                         completed=last.score.hits if last else 0,
                         wrong=last.score.wrong_attacks if last else 0,
                         recall=round(last.score.recall, 3) if last else 0.0,
                         timing_ms=round(last.score.mean_abs_timing * 1000)
                         if last else 0.0,
                         seconds=round(sum(h.seconds for h in report.history), 1),
                         section=list(bars) if bars else None,
                         definition=definition)
        self.profile.record(song.fingerprint, title, res)
        if bars:
            self.profile.song(song.fingerprint)["section"] = list(bars)
        self.store.save(self.profile)
        self._song_cache.clear()

    def stop_session(self) -> None:
        s = self.session
        self.session = None
        if s is not None:
            if hasattr(s, "stop"):
                s.stop()
            else:
                s._done.set()
        if self.kb is not None:
            self.kb.cues_clear()
            self.kb.stop_all()
        self.state["mode"] = "idle"


class Handler(BaseHTTPRequestHandler):
    hub: Hub = None            # type: ignore[assignment]
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send_file("index.html", "text/html; charset=utf-8")
        if self.path == "/events":
            return self._sse()
        name = self.path.lstrip("/")
        if name in ASSETS:
            return self._send_file(name, ASSETS[name] + "; charset=utf-8")
        self.send_error(404)

    def _send_file(self, name: str, ctype: str):
        path = os.path.join(WEB_DIR, name)
        if not os.path.exists(path):
            return self.send_error(404)
        with open(path, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _sse(self):
        q = self.hub.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    payload = q.get(timeout=10)
                    self.wfile.write(f"data: {payload}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.hub.unsubscribe(q)

    def do_POST(self):
        if self.path != "/command":
            return self.send_error(404)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self.send_error(400)
        try:
            result = self.hub.command(body.get("command", ""),
                                      body.get("args") or {})
        except Exception as exc:
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        payload = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def serve(hub: Hub, port: int = 8420) -> ThreadingHTTPServer:
    Handler.hub = hub
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    threading.Thread(target=hub.watch_liveness, daemon=True).start()
    return httpd
