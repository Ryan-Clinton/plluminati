"""The MIDI transport: one fd, one writer, one reader.

SPEC 7.8 - exactly one component writes the byte stream. MIDI is a byte
stream, not a message channel: two threads interleaving a status byte and its
data bytes produce corrupt messages, and the failure is timing-dependent.

Outbound traffic is prioritised, because at 31250 baud the wire is the scarce
resource (SPEC 7.5): a three-byte message occupies ~0.96 ms and the total
budget is ~1042 messages/sec.
"""

from __future__ import annotations

import errno
import heapq
import itertools
import os
import select
import threading
import time
from enum import IntEnum

from .device import MidiDevice
from .parser import Message, StreamParser


class Priority(IntEnum):
    """Lower value wins. Never starve CRITICAL: a dropped note-off leaves a
    stuck note or a stuck light."""
    CRITICAL = 0     # note-off, cleanup
    MUSIC = 1        # accompaniment - musically time-critical
    CUE = 2          # cue lights - forgiving by tens of ms
    COSMETIC = 3     # metronome, decoration


class MidiPort:
    """Owns the rawmidi fd for its lifetime."""

    def __init__(self, device: MidiDevice, on_message=None) -> None:
        self.device = device
        self._on_message = on_message

        self._fd = os.open(device.path, os.O_RDWR | os.O_NONBLOCK)
        self._parser = StreamParser()

        self._heap: list[tuple[int, int, bytes]] = []
        self._heap_lock = threading.Lock()
        self._work = threading.Event()
        self._drained = threading.Event()
        self._drained.set()
        self._seq = itertools.count()

        self._stop = threading.Event()
        self._closed = False

        # stats, surfaced by selftest and diagnostics
        self.bytes_written = 0
        self.bytes_read = 0
        self.eagain_retries = 0

        self._writer = threading.Thread(target=self._write_loop,
                                        name="midi-writer", daemon=True)
        self._reader = threading.Thread(target=self._read_loop,
                                        name="midi-reader", daemon=True)
        self._writer.start()
        self._reader.start()

    # ------------------------------------------------------------------ output

    def send(self, data: bytes, priority: Priority = Priority.CUE) -> None:
        """Queue one or more COMPLETE messages. Never a partial message."""
        if self._closed:
            raise RuntimeError("port is closed")
        with self._heap_lock:
            heapq.heappush(self._heap, (int(priority), next(self._seq), bytes(data)))
            self._drained.clear()
        self._work.set()

    def flush(self, timeout: float = 2.0) -> bool:
        """Block until the outbound queue is empty. True if it drained."""
        return self._drained.wait(timeout)

    def _write_loop(self) -> None:
        while not self._stop.is_set():
            with self._heap_lock:
                item = heapq.heappop(self._heap) if self._heap else None
                if item is None:
                    self._drained.set()
                    self._work.clear()
            if item is None:
                self._work.wait(0.05)
                continue
            self._write_all(item[2])

    def _write_all(self, payload: bytes) -> None:
        """Write every byte, retrying the remainder on EAGAIN.

        ALSA rawmidi uses kernel ring buffers; a non-blocking fd can refuse
        bytes when the buffer is full. Retry the tail - never drop it, and
        never let another message interleave (only this thread writes).
        """
        view = memoryview(payload)
        while view:
            try:
                n = os.write(self._fd, view)
                view = view[n:]
                self.bytes_written += n
            except BlockingIOError:
                self.eagain_retries += 1
                select.select([], [self._fd], [], 0.05)
            except OSError as exc:
                if exc.errno in (errno.ENODEV, errno.EIO):
                    self._stop.set()      # cable pulled
                    return
                raise

    # ------------------------------------------------------------------- input

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                r, _, _ = select.select([self._fd], [], [], 0.05)
                if not r:
                    continue
                data = os.read(self._fd, 4096)
            except BlockingIOError:
                continue
            except OSError as exc:
                if exc.errno in (errno.ENODEV, errno.EIO, errno.EBADF):
                    self._stop.set()
                    return
                continue
            if not data:
                continue
            self.bytes_read += len(data)
            if self._on_message is None:
                continue
            for msg in self._parser.feed(data):
                try:
                    self._on_message(msg)
                except Exception:            # a bad handler must not kill input
                    pass

    # ------------------------------------------------------------------ closing

    def close(self, drain_timeout: float = 2.0) -> None:
        if self._closed:
            return
        self._closed = True
        self.flush(drain_timeout)
        self._stop.set()
        self._writer.join(timeout=1.0)
        self._reader.join(timeout=1.0)
        try:
            os.close(self._fd)
        except OSError:
            pass

    def __enter__(self) -> "MidiPort":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def wait_for_active_sensing(port_path: str, timeout: float = 2.0) -> bool:
    """Is the keyboard powered on?

    The EZ-150 emits Active Sensing (0xFE) ~27x/sec whenever it is on, so its
    presence separates 'switched off' from 'not responding'. Note this proves
    POWER, not readiness: it keeps arriving during the attract display, when
    lighting is blocked (SPEC 3.3, 3.4).
    """
    fd = os.open(port_path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            r, _, _ = select.select([fd], [], [], 0.05)
            if not r:
                continue
            try:
                if 0xFE in os.read(fd, 256):
                    return True
            except BlockingIOError:
                continue
        return False
    finally:
        os.close(fd)
