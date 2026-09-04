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


@contextmanager
def open_keyboard(spec: str | None = None, on_message=None, setup: bool = True):
    """Yield a ready Keyboard, guaranteeing cleanup on every exit path."""
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
