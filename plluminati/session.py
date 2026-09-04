"""Opening and closing a keyboard safely.

A crash mid-song must not leave keys lit or notes sounding, and the only
mechanism that works on this hardware is explicit paired note-offs, so every
exit path has to run cleanup (SPEC 7.6).
"""

from __future__ import annotations

import signal
from contextlib import contextmanager

from . import device
from .keyboard import Keyboard
from .port import MidiPort


class _NullPort:
    """Swallows everything. Lets the app run with no keyboard attached."""

    def __init__(self):
        self.device = type("dev", (), {"path": "(none)", "alsa_id": "(none)",
                                       "name": "no keyboard"})()
        self.bytes_written = self.bytes_read = self.eagain_retries = 0

    def send(self, data, priority=None):
        pass

    def flush(self, timeout=None):
        return True

    def close(self, drain_timeout=None):
        pass


class OfflineKeyboard(Keyboard):
    """A keyboard-shaped object with no keyboard behind it.

    Exists so the UI can be opened, browsed and demonstrated by someone who
    does not own an EZ-150 - and so screenshots can be taken without one.
    Nothing is sent anywhere; `live` is forced true so the interface does not
    sit forever asking the player to wake a keyboard that is not there.
    """

    def __init__(self, on_message=None):
        super().__init__(_NullPort(), on_message=on_message)

    def live(self, threshold: float = 1.5) -> bool:
        return True


@contextmanager
def open_offline(on_message=None):
    """A session with no hardware. Same shape, so callers need no special case."""
    kb = OfflineKeyboard(on_message=on_message)
    try:
        yield kb
    finally:
        kb.lit.clear()
        kb.sounding.clear()


@contextmanager
def open_keyboard(spec: str | None = None, on_message=None, setup: bool = True,
                  offline: bool = False):
    """Yield a ready Keyboard, guaranteeing cleanup on every exit path."""
    if offline:
        with open_offline(on_message=on_message) as kb:
            yield kb
        return

    dev = device.resolve(spec)
    ok, why = device.readable(dev)
    if not ok:
        raise PermissionError(why)

    holder: dict[str, Keyboard] = {}
    port = MidiPort(dev, on_message=lambda m: holder["kb"].handle(m))
    kb = Keyboard(port, on_message=on_message)
    holder["kb"] = kb

    previous: dict[int, object] = {}

    def _bail(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            previous[sig] = signal.getsignal(sig)
            signal.signal(sig, _bail)
        except (ValueError, OSError):
            pass  # not on the main thread; caller handles its own signals

    try:
        if setup:
            kb.apply_session_setup()
        yield kb
    finally:
        try:
            kb.panic()
        finally:
            port.close()
            for sig, handler in previous.items():
                try:
                    signal.signal(sig, handler)
                except (ValueError, OSError, TypeError):
                    pass
