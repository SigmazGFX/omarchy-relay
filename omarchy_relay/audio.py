"""Voice message recording/playback via GStreamer.

Recording writes Ogg Opus (small, good quality for speech) — reusing
transfer.py's existing chunked/encrypted send rather than inventing a
second wire format; a voice message is just a file with
meta["kind"] == "voice". Playback uses playbin, which handles
demux/decode/sink selection on its own.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Optional

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

Gst.init(None)

# PipeWire is this system's audio server (and Omarchy's default generally),
# and its GStreamer element is present without needing gst-plugins-good's
# autoaudiosrc/autoaudiosink, which aren't installed here. Kept as its own
# constant so a test can swap in a synthetic source without duplicating the
# rest of the pipeline.
RECORD_SOURCE = "pipewiresrc"
# The two `queue` elements put the live capture, the encoding, and the
# file write on separate threads. Without them, a hiccup anywhere
# downstream (encoder scheduling, disk I/O) stalls the capture thread
# too, which is heard as jitter/dropouts in the recording.
_RECORD_REST = (
    "queue ! audioconvert ! audioresample ! opusenc ! queue ! oggmux ! filesink name=sink"
)


class RecordingError(RuntimeError):
    pass


class Recorder:
    """One-shot: start(), then stop() to finalize the file and get its
    duration. Make a new Recorder per recording."""

    def __init__(self, path: Path, source: str = RECORD_SOURCE) -> None:
        self.path = path
        # Fires (on the GLib main loop, via the bus signal watch below — no
        # extra marshalling needed) if the pipeline errors out *after*
        # start() has already returned successfully, e.g. a live source
        # that negotiates fine but fails to actually produce data. Without
        # this, that failure would only surface once stop() is called.
        self.on_error: Optional[Callable[[str], None]] = None
        try:
            self._pipeline = Gst.parse_launch(f"{source} ! {_RECORD_REST}")
        except GLib.Error as exc:
            # Wrapped so callers only need to catch one exception type —
            # a missing/misconfigured GStreamer element surfaces the same
            # way as a runtime failure to start capturing.
            raise RecordingError(f"couldn't set up recording: {exc}") from exc
        self._pipeline.get_by_name("sink").set_property("location", str(path))
        self._started_at: Optional[float] = None
        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message)

    def _on_message(self, _bus, message) -> None:
        if message.type == Gst.MessageType.ERROR and self.on_error:
            err, _debug = message.parse_error()
            self.on_error(err.message)

    def start(self) -> None:
        ret = self._pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            self._pipeline.set_state(Gst.State.NULL)
            raise RecordingError("couldn't start recording — no audio input available?")
        self._started_at = time.time()

    def stop(self, timeout_s: float = 3.0) -> float:
        """Sends EOS and blocks (call this off the GTK main thread) until
        the Ogg container is properly finalized. Returns the elapsed
        recording time in seconds."""
        duration = time.time() - (self._started_at or time.time())
        self._pipeline.send_event(Gst.Event.new_eos())
        bus = self._pipeline.get_bus()
        bus.timed_pop_filtered(int(timeout_s * Gst.SECOND), Gst.MessageType.EOS | Gst.MessageType.ERROR)
        self._pipeline.set_state(Gst.State.NULL)
        return duration


class Player:
    """Wraps a playbin instance for one voice-message bubble. `on_finished`
    is called (on the GLib main loop — playbin's bus watch dispatches there
    directly, no extra marshalling needed) when playback ends or errors."""

    def __init__(self, path: Path, on_finished: Optional[Callable[[], None]] = None) -> None:
        self.path = path
        self.on_finished = on_finished
        self.playing = False
        self._playbin = Gst.ElementFactory.make("playbin", None)
        self._playbin.set_property("uri", Gst.filename_to_uri(str(path)))
        bus = self._playbin.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message)

    def play(self) -> None:
        self._playbin.set_state(Gst.State.PLAYING)
        self.playing = True

    def stop(self) -> None:
        self._playbin.set_state(Gst.State.NULL)
        self.playing = False

    def _on_message(self, _bus, message) -> None:
        if message.type in (Gst.MessageType.EOS, Gst.MessageType.ERROR):
            self._playbin.set_state(Gst.State.NULL)
            self.playing = False
            if self.on_finished:
                self.on_finished()


def format_duration(seconds: Optional[float]) -> str:
    if not seconds or seconds < 0:
        return "Voice message"
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"
