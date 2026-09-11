"""Live voice during a screen share.

Everyone in a share — the sharer and each open viewer — is in that share's
voice room. An unmuted microphone is Opus-encoded into 40 ms packets and
published, encrypted, to screen-audio/<share_id>; everyone else in the room
plays them. Microphones always start muted, and capture only runs while
unmuted. During silence Opus DTX shrinks packets to a few bytes, and only
every tenth of those is sent.

Echo cancellation comes from PipeWire's echo-cancel module (WebRTC's
canceller), loaded privately while any microphone is unmuted: the microphone
is captured through it, and everyone else's voices play through it, so what
comes out of the speakers is removed from what the microphone sends. While
every microphone is muted there's nothing to cancel, so the module is
unloaded and voices play straight to the speakers. Nothing about the default
devices changes. Without pw-cli, voice still works, just without it.

Playback is a small jitter buffer. Each speaker's packets get timestamps
from their sequence numbers against a fixed playout delay, so the sink plays
them evenly and drops the ones that arrive after their slot. A speaker whose
packets start arriving late is re-anchored a playout delay from now, which
lets the delay grow to match the network.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
import traceback
import uuid
from typing import Callable, Optional

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

Gst.init(None)

FRAME_MS = 40
PLAYOUT_DELAY_MS = 120
SPEAKER_IDLE_SECONDS = 5  # a speaker's playback pipeline is torn down after this long without packets
TALKING_WINDOW_SECONDS = 0.6
_DTX_PACKET_BYTES = 3  # Opus DTX frames during silence are 1-3 bytes
_DTX_SEND_EVERY = 10
# Comfort-noise updates during silence still run to ~50 bytes; speech at
# 24 kbps is ~120 bytes a packet. Only packets above this count as talking.
_VOICE_PACKET_BYTES = 64
_NO_ECHO_CANCEL = "Echo cancellation isn't available — use headphones so others don't hear themselves"
_ECHO_CAPTURE_WATCHDOG_MS = 2000

# Kept as module settings so tests can use a tone and a silent sink instead
# of a real microphone and speakers (and skip echo cancellation with them).
MIC_SOURCE = "pipewiresrc"
SPEAKER_SINK = "pipewiresink"
ECHO_CANCEL = True

_PR_SET_PDEATHSIG = 1
_libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)


class VoiceError(RuntimeError):
    pass


def _die_with_parent() -> None:
    # Runs in the pw-cli child between fork and exec. If the app goes away
    # without unloading the canceller (a crash, a kill), pw-cli goes too
    # rather than leaving a module attached to the microphone.
    _libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)


class _ControlThread:
    """One long-lived thread for every microphone, playback, and canceller
    change. State changes on PipeWire elements can block while the graph
    settles, so they stay off GTK's and paho's threads. And a pw-cli child
    dies with the thread that started it, so that thread has to outlive
    every voice room."""

    def __init__(self) -> None:
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        threading.Thread(target=self._run, name="relay-voice", daemon=True).start()

    def submit(self, fn: Callable, *args) -> None:
        self._queue.put((fn, args))

    def _run(self) -> None:
        while True:
            fn, args = self._queue.get()
            try:
                fn(*args)
            except Exception:
                traceback.print_exc()


_control = _ControlThread()


class EchoCanceller:
    """PipeWire's echo-cancel module, loaded by a `pw-cli -m` child process
    and unloaded when that process ends. `available` says whether its nodes
    actually came up; check it once `ready` is set.

    Start it from a thread that lives as long as the canceller is needed
    (the voice control thread). capture_target/playback_target pin it to
    specific nodes instead of the default microphone and speakers (used for
    testing)."""

    def __init__(self, capture_target: Optional[str] = None, playback_target: Optional[str] = None) -> None:
        name = f"omarchy-relay-echo-cancel-{os.getpid()}-{uuid.uuid4().hex[:4]}"
        self.source_name = f"{name}-source"
        self.sink_name = f"{name}-sink"
        self.ready = threading.Event()
        self.available = False
        capture = f'node.name = "{name}-capture"'
        if capture_target:
            capture += f' target.object = "{capture_target}" stream.capture.sink = true'
        playback = f'node.name = "{name}-playback"'
        if playback_target:
            playback += f' target.object = "{playback_target}"'
        # Low priorities and node.virtual keep the canceller's source and sink
        # from ever being picked as a default device for other apps.
        hidden = "node.virtual = true priority.session = 0 priority.driver = 0"
        args = (
            "{ audio.channels = 1 audio.rate = 48000 "
            f"capture.props = {{ {capture} }} "
            f'source.props = {{ node.name = "{self.source_name}" node.description = "Omarchy Relay voice (echo cancelled)" {hidden} }} '
            f'sink.props = {{ node.name = "{self.sink_name}" node.description = "Omarchy Relay voice" {hidden} }} '
            f"playback.props = {{ {playback} }} }}"
        )
        try:
            self._process = subprocess.Popen(
                ["pw-cli", "-m", "load-module", "libpipewire-module-echo-cancel", args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=_die_with_parent,
            )
        except OSError:
            self._process = None
            self.ready.set()
            return
        threading.Thread(target=self._wait_for_nodes, daemon=True).start()

    def _wait_for_nodes(self) -> None:
        needle = f'"{self.source_name}"'
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and self._process.poll() is None:
            try:
                dump = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=2, check=False).stdout
            except (OSError, subprocess.SubprocessError):
                break
            if needle in dump:
                self.available = True
                break
            time.sleep(0.05)
        self.ready.set()

    def close(self) -> None:
        self.available = False
        if self._process is None or self._process.poll() is not None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._process.kill()


# One canceller shared by every unmuted microphone: loaded by the first and
# unloaded with the last. Only touched from the voice control thread.
_echo: Optional[EchoCanceller] = None
_echo_users = 0


def _acquire_echo_canceller() -> Optional[EchoCanceller]:
    global _echo, _echo_users
    if not ECHO_CANCEL or not shutil.which("pw-cli"):
        return None
    if _echo is None:
        _echo = EchoCanceller()
    _echo_users += 1
    return _echo


def _release_echo_canceller() -> None:
    global _echo, _echo_users
    _echo_users -= 1
    if _echo_users <= 0 and _echo is not None:
        echo, _echo, _echo_users = _echo, None, 0
        echo.close()


def _current_echo_canceller() -> Optional[EchoCanceller]:
    echo = _echo
    return echo if echo is not None and echo.available else None


class VoiceCapture:
    """Microphone -> Opus packets, delivered to `on_packet` on GStreamer's
    streaming thread. `on_error` is called on the GLib main loop."""

    def __init__(self, source: str, on_packet: Callable[[bytes], None], on_error: Callable[[str], None]) -> None:
        # The queue right after the source keeps a slow encoder from stalling
        # capture, and lets a test source end in its own caps.
        description = (
            f"{source} ! queue ! audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=1 ! "
            f"opusenc audio-type=voice bitrate=24000 frame-size={FRAME_MS} dtx=true ! "
            "appsink name=sink emit-signals=true sync=false"
        )
        try:
            self._pipeline = Gst.parse_launch(description)
        except GLib.Error as exc:
            raise VoiceError(f"couldn't set up the microphone: {exc.message}") from exc
        self._on_packet = on_packet
        self._on_error = on_error
        self._pipeline.get_by_name("sink").connect("new-sample", self._on_sample)
        # Called from the control thread, which has no main context of its
        # own, so the watch lands on the default one that GTK runs.
        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

    def start(self) -> None:
        if self._pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.stop()
            raise VoiceError("couldn't start the microphone — no audio input available?")

    def stop(self) -> None:
        self._pipeline.get_bus().remove_signal_watch()
        self._pipeline.set_state(Gst.State.NULL)

    def _on_sample(self, sink) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buffer = sample.get_buffer()
        ok, info = buffer.map(Gst.MapFlags.READ)
        if ok:
            try:
                packet = bytes(info.data)
            finally:
                buffer.unmap(info)
            self._on_packet(packet)
        return Gst.FlowReturn.OK

    def _on_bus_message(self, _bus, message) -> None:
        if message.type == Gst.MessageType.ERROR:
            err, _debug = message.parse_error()
            self._on_error(err.message)


class _SpeakerStream:
    """One remote speaker's playback: Opus packets in, timestamped against a
    playout delay, decoded, and played on the sink's clock."""

    def __init__(self, sink: str) -> None:
        try:
            self.pipeline = Gst.parse_launch(
                f"appsrc name=src is-live=true format=time ! opusdec plc=true ! audioconvert ! audioresample ! {sink}"
            )
        except GLib.Error as exc:
            raise VoiceError(f"couldn't set up audio playback: {exc.message}") from exc
        self._src = self.pipeline.get_by_name("src")
        self._src.set_property("caps", Gst.Caps.from_string("audio/x-opus,rate=48000,channels=1,channel-mapping-family=0"))
        self._clock = Gst.SystemClock.obtain()
        self.pipeline.use_clock(self._clock)
        self._stream_id: Optional[str] = None
        self._seq0: Optional[int] = None
        self._base = 0
        self._last_seq = -1
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.pipeline.set_state(Gst.State.NULL)
            raise VoiceError("couldn't start audio playback")

    def push(self, stream_id: str, seq: int, packet: bytes) -> None:
        if stream_id != self._stream_id:
            # The speaker muted and unmuted, or rejoined: a fresh sequence.
            self._stream_id, self._seq0, self._last_seq = stream_id, None, -1
        if seq <= self._last_seq:
            return  # duplicate, or older than what's already been scheduled
        self._last_seq = seq
        frame = FRAME_MS * Gst.MSECOND
        now = self._clock.get_time() - self.pipeline.get_base_time()
        expected = None if self._seq0 is None else self._base + (seq - self._seq0) * frame
        if expected is None or expected < now or expected > now + Gst.SECOND:
            self._seq0, self._base = seq, now + PLAYOUT_DELAY_MS * Gst.MSECOND
            expected = self._base
        buffer = Gst.Buffer.new_wrapped(packet)
        buffer.pts = expected
        buffer.duration = frame
        self._src.emit("push-buffer", buffer)

    def close(self) -> None:
        self.pipeline.set_state(Gst.State.NULL)


class VoiceSession:
    """This device's place in one share's voice room. Create it, call
    set_mic()/set_listening(), and close it on the GLib main thread; the
    audio work itself happens on the voice control thread.

    Call handle_packet() with every packet from the room's topic (from any
    thread). on_talking_changed(nicks), on_error(message) — the microphone
    couldn't start or stopped; it's muted again — and on_warning(message)
    are called on the GLib main loop. should_send gates outgoing audio, e.g.
    a sharer with nobody watching has nobody to talk to. Passing source or
    sink skips echo cancellation for that side."""

    def __init__(
        self,
        client,
        cfg,
        share_id: str,
        *,
        on_talking_changed: Callable[[list[str]], None],
        on_error: Callable[[str], None],
        on_warning: Optional[Callable[[str], None]] = None,
        should_send: Optional[Callable[[], bool]] = None,
        source: Optional[str] = None,
        sink: Optional[str] = None,
    ) -> None:
        self.client = client
        self.cfg = cfg
        self.share_id = share_id
        self._on_talking_changed = on_talking_changed
        self._on_error = on_error
        self._on_warning = on_warning
        self._should_send = should_send
        self._source = source
        self._sink = sink
        self._mic_wanted = False
        self._capture: Optional[VoiceCapture] = None  # control thread only
        self._echo: Optional[EchoCanceller] = None  # held while this session's mic is on
        self._capture_via_echo = False
        self._echo_failed = False  # capture through the canceller failed once; use the plain mic from now on
        self._got_packet = False
        self._stream_id = ""
        self._seq = 0
        self._lock = threading.Lock()
        # device_id -> {"stream": _SpeakerStream or None while opening, "echo", "nick", "last_packet", "last_voice"}
        self._speakers: dict[str, dict] = {}
        self._listening = True
        self._talking: list[str] = []
        self._closed = False
        client.join_screen_audio(share_id)
        self._timer_id = GLib.timeout_add(200, self._tick)

    @property
    def mic_on(self) -> bool:
        return self._mic_wanted

    @property
    def echo_cancelling(self) -> bool:
        return self._echo is not None and self._echo.available

    def set_mic(self, on: bool) -> None:
        if self._closed or on == self._mic_wanted:
            return
        self._mic_wanted = on
        _control.submit(self._apply_mic)

    def set_listening(self, on: bool) -> None:
        self._listening = on
        if not on:
            self._drop_speakers(list(self._speakers))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        GLib.source_remove(self._timer_id)
        self._mic_wanted = False
        _control.submit(self._apply_mic)
        self._drop_speakers(list(self._speakers))
        self.client.leave_screen_audio(self.share_id)

    def handle_packet(self, header: dict, packet: bytes) -> None:
        device_id = header.get("from")
        if self._closed or not self._listening or not device_id or device_id == self.cfg.device_id:
            return
        if header.get("share_id") != self.share_id:
            return
        want_echo = self._sink is None and _current_echo_canceller() is not None
        now = time.monotonic()
        with self._lock:
            speaker = self._speakers.get(device_id)
            if speaker is None:
                speaker = {"stream": None, "echo": want_echo, "last_voice": 0.0}
                self._speakers[device_id] = speaker
                _control.submit(self._open_speaker, device_id, speaker, None)
            elif speaker["stream"] is not None and speaker["echo"] != want_echo:
                # The canceller came or went: move this speaker's playback
                # onto its sink (or back off it). Packets meanwhile are dropped.
                old, speaker["stream"], speaker["echo"] = speaker["stream"], None, want_echo
                _control.submit(self._open_speaker, device_id, speaker, old)
            speaker["nick"] = header.get("nick", "?")
            speaker["last_packet"] = now
            if len(packet) > _VOICE_PACKET_BYTES:
                speaker["last_voice"] = now
            stream = speaker["stream"]
        if stream is not None:
            stream.push(header.get("stream", ""), header.get("seq", 0), packet)

    # -- control thread ------------------------------------------------------

    def _apply_mic(self) -> None:
        want = self._mic_wanted and not self._closed
        if want == (self._capture is not None):
            return
        if not want:
            capture, self._capture = self._capture, None
            capture.stop()
            if self._echo is not None:
                self._echo = None
                _release_echo_canceller()
            return
        echo = _acquire_echo_canceller() if self._source is None and not self._echo_failed else None
        if echo is not None:
            echo.ready.wait(4)
        if self._source is not None:
            source = self._source
        elif echo is not None and echo.available:
            source = f"{MIC_SOURCE} target-object={echo.source_name}"
        else:
            source = MIC_SOURCE
        self._stream_id, self._seq = uuid.uuid4().hex[:8], 0
        self._got_packet = False
        try:
            capture = VoiceCapture(source, self._on_packet, self._on_capture_error)
            capture.start()
        except VoiceError as exc:
            if echo is not None:
                _release_echo_canceller()
            self._mic_wanted = False
            GLib.idle_add(self._notify, self._on_error, str(exc))
            return
        self._capture, self._echo = capture, echo
        self._capture_via_echo = self._source is None and echo is not None and echo.available
        if self._capture_via_echo:
            # A route that errors lands in _on_capture_error; one that just
            # never delivers is caught here. Opus emits a packet every frame,
            # even in silence, so a working capture always has one by now.
            GLib.timeout_add(_ECHO_CAPTURE_WATCHDOG_MS, self._check_echo_capture, capture)
        if self._source is None and not self._capture_via_echo and self._on_warning is not None:
            GLib.idle_add(self._notify, self._on_warning, _NO_ECHO_CANCEL)

    def _check_echo_capture(self, capture: VoiceCapture) -> bool:
        # GLib main loop.
        if self._mic_wanted and self._capture is capture and self._capture_via_echo and not self._got_packet:
            self._capture_via_echo = False
            _control.submit(self._restart_without_echo)
        return False

    def _restart_without_echo(self) -> None:
        # The canceller's source wouldn't deliver audio: rather than leave the
        # person silent, capture the plain microphone and say so.
        self._echo_failed = True
        capture, self._capture = self._capture, None
        if capture is not None:
            capture.stop()
        if self._echo is not None:
            self._echo = None
            _release_echo_canceller()
        self._apply_mic()

    def _open_speaker(self, device_id: str, speaker: dict, old: Optional[_SpeakerStream]) -> None:
        if old is not None:
            old.close()
        echo = _current_echo_canceller() if speaker["echo"] else None
        if self._sink is not None:
            sink = self._sink
        elif echo is not None:
            sink = f"{SPEAKER_SINK} target-object={echo.sink_name}"
        else:
            sink, speaker["echo"] = SPEAKER_SINK, False
        try:
            stream = _SpeakerStream(sink)
        except VoiceError:
            with self._lock:
                if self._speakers.get(device_id) is speaker:
                    del self._speakers[device_id]
            return
        with self._lock:
            keep = not self._closed and self._speakers.get(device_id) is speaker
            if keep:
                speaker["stream"] = stream
        if not keep:
            stream.close()

    # -- other threads -------------------------------------------------------

    def _notify(self, callback: Callable[[str], None], message: str) -> bool:
        if not self._closed:
            callback(message)
        return False

    def _on_packet(self, packet: bytes) -> None:
        # GStreamer's streaming thread. The sequence counts every encoded
        # frame, sent or not, so receivers keep time across skipped silence.
        self._got_packet = True
        self._seq += 1
        if len(packet) <= _DTX_PACKET_BYTES and self._seq % _DTX_SEND_EVERY:
            return
        if self._should_send is not None and not self._should_send():
            return
        if not self.client.connected.is_set():
            return
        header = {
            "share_id": self.share_id,
            "from": self.cfg.device_id,
            "nick": self.cfg.nickname,
            "stream": self._stream_id,
            "seq": self._seq,
        }
        self.client.send_screen_audio(self.share_id, header, packet)

    def _on_capture_error(self, message: str) -> None:
        # GLib main loop, from the capture pipeline's bus.
        if self._mic_wanted and self._capture_via_echo and not self._got_packet:
            self._capture_via_echo = False
            _control.submit(self._restart_without_echo)
            return
        if self._mic_wanted:
            self._mic_wanted = False
            _control.submit(self._apply_mic)
            self._notify(self._on_error, message)

    def _drop_speakers(self, device_ids: list[str]) -> None:
        with self._lock:
            streams = [self._speakers.pop(d)["stream"] for d in device_ids if d in self._speakers]
        for stream in streams:
            if stream is not None:
                _control.submit(stream.close)

    def _tick(self) -> bool:
        if self._closed:
            return False
        now = time.monotonic()
        with self._lock:
            idle = [d for d, s in self._speakers.items() if now - s.get("last_packet", now) > SPEAKER_IDLE_SECONDS]
            talking = sorted(
                s["nick"]
                for d, s in self._speakers.items()
                if d not in idle and now - s["last_voice"] < TALKING_WINDOW_SECONDS
            )
        if idle:
            self._drop_speakers(idle)
        if talking != self._talking:
            self._talking = talking
            self._on_talking_changed(talking)
        return True
